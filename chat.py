"""In-game chat -> Discord (2026-10-17): admin alerts and a chat log, each on its own webhook.

Where the chat comes from:
  SCUM             its chat log (SaveFiles\\Logs\\chat_*.log - one per run, UTF-16, times in UTC):
                   "<time>: '<steamid>:<name>(<n>)' '<Global|Local|Squad|Admin>: <text>'"
  Space Engineers  the Remote API (GET session/chat - the latest messages it keeps)
CS2 would need its server log (it has stopped writing one here) and Rust a lasting WebRCON connection, so
they aren't watched yet.

* Admin alerts: a message with one of the alert words (whole words, any case; "admin*" also matches admins
  and administrator; a phrase like "need help" matches with any spacing) goes to the admin-alerts webhook
  with a ping (@here unless set otherwise; one ping per player per 5 minutes - later requests are posted
  without one) and into ServerDeck's event list. Players on the server's own admin list, SCUM's Admin
  channel and commands ("#...", "/...") don't raise alerts.
* Chat log: every message, several lines per post, to the chat-log webhook.

The two webhook URLs are secrets kept in community.json (never logged, never sent to the browser). Each must
be its own webhook in its own channel, apart from the Discord feed's (server news): ServerDeck refuses a
webhook that another of the three uses, or one that posts into the same channel (it asks Discord which
channel a webhook belongs to when one is saved). Where each server's chat stands is kept there too, so a
ServerDeck restart loses nothing; messages older than 15 minutes (ServerDeck was off) aren't posted.
"""
import calendar
import glob
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path

import community
import players as playerslib
import se_api

log = logging.getLogger("serverdeck")

POLL_SECONDS = 5
MAX_AGE = 15 * 60          # older messages are skipped, not posted late
LATE = 120                 # a message posted later than this says when it was written
PING_EVERY = 5 * 60        # one ping per player per this long
STAFF_SECONDS = 60         # the admin list is read again this often
DEFAULT_WORDS = ["admin*", "moderator*", "mod", "mods", "staff", "help", "stuck", "bugged", "glitch*",
                 "cheat*", "hacker*", "hacking", "grief*"]
DEFAULT_MENTION = "@here"
TARGETS = {"alerts": "admin alerts", "log": "chat log"}
HOOK_NAMES = {"feed": "Discord feed (server news)", "alerts": "admin alerts", "log": "chat log"}
CONNECTED = {"alerts": "✅ ServerDeck is connected - players asking for help or an admin in game chat will show up here.",
             "log": "✅ ServerDeck is connected - in-game chat will be posted here."}
SOS = "\U0001F198"
SCUM_CHAT = re.compile(r"^(\d{4}\.\d\d\.\d\d-\d\d\.\d\d\.\d\d): '(?:\S+ )?(7656\d{13}):(.*?)\(\d+\)' '(\w+): (.*)'$")


def compile_words(words):
    """Alert words -> one regex: whole words, any case; "word*" also matches longer words; a space in a
    phrase matches any spacing."""
    parts = []
    for w in words or []:
        w = str(w).strip().lower()
        star = w.endswith("*")
        w = w.rstrip("*").strip()
        if w:
            parts.append(r"\s+".join(re.escape(p) for p in w.split()) + (r"\w*" if star else ""))
    return re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(parts), re.I) if parts else None


def mention(text):
    """The ping in front of an alert -> (text, Discord allowed_mentions): nothing, @here, @everyone,
    a role as <@&id> or a person as <@id>."""
    t = str(text or "").strip()
    if not t:
        return "", {"parse": []}
    if t in ("@here", "@everyone"):
        return t, {"parse": ["everyone"]}
    m = re.fullmatch(r"<@&(\d{15,22})>", t)
    if m:
        return t, {"parse": [], "roles": [m.group(1)]}
    m = re.fullmatch(r"<@!?(\d{15,22})>", t)
    if m:
        return f"<@{m.group(1)}>", {"parse": [], "users": [m.group(1)]}
    raise ValueError("the ping must be @here, @everyone, a role as <@&id> or a person as <@id> - or nothing")


def said(text, n=400):
    """What a player typed, shown as typed: no Discord markdown, masked links, mention pills or line breaks."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()[:n]
    return re.sub(r"([\[\]<@])", r"\\\1", community.md(t))


def hook_id(url):
    m = re.search(r"/webhooks/(\d+)/", url or "")
    return m.group(1) if m else url


def webhook_channel(url):
    """The Discord channel a webhook posts into; None if Discord can't be asked right now.
    ValueError if Discord says there is no such webhook."""
    req = urllib.request.Request(url, headers={"User-Agent": "ServerDeck"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            info = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise ValueError("Discord doesn't know that webhook (was it deleted?) - copy its URL again") from None
        return None
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return str(info.get("channel_id") or "") or None


# --------------------------------------------------------------------------
# Where each game's chat comes from: poll() -> new messages
# {"t": when (epoch), "steam_id", "name", "channel", "text"}
# --------------------------------------------------------------------------

class ScumChat:
    """SCUM's chat log, read as it grows: one file per run (chat_<UTC start>.log), UTF-16LE."""
    kind = "chat log"

    def __init__(self, watch, server):
        self.w, self.s = watch, server
        self.pattern = ((server.cfg.get("logs") or {}).get("chat")
                        or str(Path(server.cfg["install_dir"]) / r"SCUM\Saved\SaveFiles\Logs\chat_*.log"))
        self.cur = None                                    # [path, bytes read, unfinished line]

    def poll(self):
        files = sorted(glob.glob(self.pattern))            # named after when the run started: oldest first
        if not files:
            return []
        if self.cur is None:
            saved = self.w.position(self.s.id)             # where it stood when ServerDeck stopped
            path = next((f for f in files if saved and Path(f).name == saved[0]), None)
            if path is None:                               # first look: what is there already is history
                self.cur = [files[-1], os.path.getsize(files[-1]), b""]
                self._remember()
                return []
            self.cur = [path, int(saved[1]), b""]
        out = []
        if self.cur[0] != files[-1]:                       # SCUM restarted: finish the runs before first
            out += self._read(self.cur)
            for f in [f for f in files if f > self.cur[0]][:-1]:
                out += self._read([f, 0, b""])
            self.cur = [files[-1], 0, b""]
        out += self._read(self.cur)
        self._remember()
        return out

    def _remember(self):
        self.w.remember(self.s.id, [Path(self.cur[0]).name, self.cur[1] - len(self.cur[2])])

    @staticmethod
    def _read(cur):
        path, off, carry = cur
        try:
            size = os.path.getsize(path)
            if size < off:
                off, carry = 0, b""
            if size == off:
                return []
            with open(path, "rb") as f:
                f.seek(off)
                data = f.read(size - off)
        except OSError:
            return []
        cur[1] = off + len(data)
        buf, odd = carry + data, b""
        if len(buf) % 2:                                   # UTF-16LE: whole code units only
            buf, odd = buf[:-1], buf[-1:]
        lines = buf.decode("utf-16-le", "replace").split("\n")
        cur[2] = lines.pop().encode("utf-16-le") + odd     # the line SCUM is still writing
        out = []
        for line in lines:
            m = SCUM_CHAT.match(line.strip().lstrip("﻿"))
            if m:
                out.append({"t": calendar.timegm(time.strptime(m.group(1), "%Y.%m.%d-%H.%M.%S")),
                            "steam_id": m.group(2), "name": m.group(3), "channel": m.group(4), "text": m.group(5)})
        return out


class SeChat:
    """Space Engineers' Remote API lists its latest chat messages; new ones are those it didn't list before.
    (Messages the server sends itself, like ServerDeck's restart warnings, have no player and are skipped.)"""
    kind = "Remote API"

    def __init__(self, watch, server):
        self.w, self.s = watch, server
        self.seen = None

    def poll(self):
        if not self.s.status.get("online"):
            return []
        api = self.s.cfg["remote_api"]
        d = se_api.call(api["port"], self.s.mgr.secret(api["key"]), "GET", "/vrageremote/v1/session/chat", timeout=10)
        msgs = [m for m in (d.get("data") or {}).get("Messages") or [] if isinstance(m, dict)]
        keys = [hashlib.sha1(json.dumps([m.get("Timestamp"), m.get("SteamID"), m.get("Content")],
                                        default=str).encode()).hexdigest()[:16] for m in msgs]
        if self.seen is None:
            saved = set(self.w.position(self.s.id) or [])
            # Nothing in common with what was seen before ServerDeck stopped (or a first look): all history.
            self.seen = saved if saved & set(keys) else set(keys)
        now = time.time()
        out = [{"t": now, "steam_id": str(m.get("SteamID")), "name": str(m.get("DisplayName") or "?"),
                "channel": "Global", "text": str(m.get("Content") or "")}
               for k, m in zip(keys, msgs) if k not in self.seen and playerslib.STEAM_ID.fullmatch(str(m.get("SteamID") or ""))]
        if set(keys) != self.seen:
            self.seen = set(keys)
            self.w.remember(self.s.id, keys[-200:])
        return out


SOURCES = {"scum_log": ScumChat, "se_remote": SeChat}


# --------------------------------------------------------------------------
# The watcher
# --------------------------------------------------------------------------

class ChatWatch:
    def __init__(self, mgr):
        self.mgr, self.c = mgr, mgr.community
        self.sources = {s.id: SOURCES[k](self, s) for s in mgr.servers if (k := playerslib.source(s.cfg)) in SOURCES}
        self.to_alerts = community.Discord(self.c, lambda: self.settings().get("alerts_webhook"))
        self.to_log = community.Discord(self.c, lambda: self.settings().get("log_webhook"))
        self.recent = deque(maxlen=20)       # help requests since ServerDeck started (newest last)
        self.pinged = {}                     # (server id, steam id) -> when their last alert pinged
        self.staff = {}                      # server id -> (read at, {steam ids on its admin list})
        self.errors = {}                     # server id -> why its chat can't be read right now
        self.seen = 0

    # ---- settings (community.json "chat"; holds the two webhook secrets) ----
    def settings(self):
        with self.c.lock:
            ch = self.c.settings().setdefault("chat", {})
            ch.setdefault("alerts_on", True)
            ch.setdefault("log_on", True)
            ch.setdefault("mention", DEFAULT_MENTION)
            ch.setdefault("words", list(DEFAULT_WORDS))
            ch.setdefault("servers", list(self.sources))
            ch.setdefault("pos", {})
            return ch

    def copy(self):
        with self.c.lock:
            return json.loads(json.dumps(self.settings()))

    def position(self, sid):
        with self.c.lock:
            return json.loads(json.dumps(self.settings()["pos"].get(sid)))

    def remember(self, sid, pos):
        with self.c.lock:
            p = self.settings()["pos"]
            if p.get(sid) != pos:
                p[sid] = pos
                self.c.dirty = True                      # saved by the community loop (debounced)

    # ---- the loop ----
    def start(self):
        threading.Thread(target=self.to_alerts.worker, daemon=True, name="discord-alerts").start()
        threading.Thread(target=self.to_log.worker, daemon=True, name="discord-chat").start()
        threading.Thread(target=self.loop, daemon=True, name="chat").start()

    def loop(self):
        time.sleep(10)
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("chat watch error")
            time.sleep(POLL_SECONDS)

    def tick(self, now=None):
        cfg = self.copy()
        watched = [sid for sid in cfg["servers"] if sid in self.sources]
        rx = compile_words(cfg["words"])
        try:
            ping, allowed = mention(cfg["mention"])
        except ValueError:
            ping, allowed = "", {"parse": []}
        for sid, src in self.sources.items():
            try:
                msgs = src.poll()                        # (also keeps unwatched servers' place current)
                self.errors.pop(sid, None)
            except Exception as e:
                if self.errors.get(sid) != str(e):
                    log.info("chat %s: %s", sid, e)
                self.errors[sid] = str(e)
                continue
            if sid not in watched:
                continue
            t = now or time.time()
            lines = []
            for m in msgs:
                if t - m["t"] > MAX_AGE:
                    continue
                self.seen += 1
                lines.append(self.line(src.s, m, t, len(watched) > 1))
                word = self.alert_word(src.s, m, rx)
                if word:
                    self.alert(src.s, m, word, t, cfg, ping, allowed)
            if lines and cfg.get("log_on") and cfg.get("log_webhook"):
                for part in community.split_message("\n".join(lines)):
                    self.to_log.queue(part, no_embeds=True)

    def staff_ids(self, s):
        at, ids = self.staff.get(s.id, (0, set()))
        if time.time() - at >= STAFF_SECONDS:
            try:
                g = self.mgr.players.game(s)
                ids = {p["steam_id"] for p in (g.staff() if g else [])}
            except Exception as e:
                log.info("chat %s: admin list not readable: %s", s.id, e)
            self.staff[s.id] = (time.time(), ids)
        return ids

    def alert_word(self, s, m, rx):
        """The alert word a message contains - None for no alert."""
        if not rx or m["channel"].lower() == "admin" or m["text"].lstrip().startswith(("#", "/")):
            return None
        hit = rx.search(m["text"])
        if not hit or m["steam_id"] in self.staff_ids(s):
            return None
        return hit.group(0)

    def alert(self, s, m, word, now, cfg, ping, allowed):
        self.recent.append({"t": m["t"], "server": s.id, "server_name": s.name, "name": m["name"],
                            "channel": m["channel"], "text": m["text"][:300], "word": word})
        self.mgr.events.add("warn", s.id, f"help request from {m['name']} ({m['channel']} chat): {m['text'][:200]}")
        if not (cfg.get("alerts_on") and cfg.get("alerts_webhook")):
            return
        key = (s.id, m["steam_id"])
        loud = bool(ping) and now - self.pinged.get(key, 0) >= PING_EVERY
        if loud:
            self.pinged[key] = now
        when = f", {datetime.fromtimestamp(m['t']):%H:%M}" if now - m["t"] > LATE else ""
        text = (f"{ping + ' ' if loud else ''}{SOS} **Help / admin request** - **{community.md(m['name'])}** on "
                f"**{community.md(s.name)}** ({m['channel']} chat{when}):\n> {said(m['text'])}\n"
                f"-# matched \"{said(word, 60)}\" · Steam profile <https://steamcommunity.com/profiles/{m['steam_id']}>")
        self.to_alerts.queue(text, allowed=allowed if loud else {"parse": []}, no_embeds=True)

    @staticmethod
    def line(s, m, now, multi):
        where = f"{s.name} · {m['channel']}" if multi else m["channel"]
        when = f" ({datetime.fromtimestamp(m['t']):%H:%M})" if now - m["t"] > LATE else ""
        return f"`{where.replace('`', '')}` **{community.md(m['name'])}**{when}: {said(m['text'])}"

    # ---- webhooks: one job each, one channel each ----
    def check_unique(self, target, url):
        """Refuse a webhook that the Discord feed / admin alerts / chat log (another of them) already uses, or
        one that posts into the same channel. Returns the channel it posts into (None: Discord unreachable)."""
        with self.c.lock:
            ch = self.settings()
            hooks = {"feed": ((self.c.settings().get("discord") or {}).get("webhook"), None),
                     "alerts": (ch.get("alerts_webhook"), ch.get("alerts_channel")),
                     "log": (ch.get("log_webhook"), ch.get("log_channel"))}
        hooks.pop(target, None)
        for k, (other, _) in hooks.items():
            if other and hook_id(other) == hook_id(url):
                raise ValueError(f"that webhook is already used for the {HOOK_NAMES[k]} - make a separate webhook "
                                 "in a different channel")
        mine = webhook_channel(url)
        if mine:
            for k, (other, known) in hooks.items():
                if not other:
                    continue
                try:
                    theirs = known or webhook_channel(other)
                except ValueError:                      # that one was deleted in Discord: no clash
                    theirs = None
                if theirs == mine:
                    raise ValueError(f"that webhook posts into the same Discord channel as the {HOOK_NAMES[k]} - "
                                     "use a webhook from a different channel")
        return mine

    # ---- API ----
    def summary(self):
        cfg = self.copy()

        def target(d, key):
            return {"configured": bool(cfg.get(f"{key}_webhook")), "on": bool(cfg.get(f"{key}_on")), "sent": d.sent,
                    "last_error": d.last_error, "last_ok": d.last_ok, "queued": d.q.qsize()}
        return {"sources": [{"id": sid, "name": src.s.name, "kind": src.kind, "error": self.errors.get(sid)}
                            for sid, src in self.sources.items()],
                "servers": [sid for sid in cfg["servers"] if sid in self.sources],
                "mention": cfg["mention"], "words": cfg["words"],
                "alerts": target(self.to_alerts, "alerts"), "log": target(self.to_log, "log"),
                "recent": list(self.recent)[::-1], "seen": self.seen}

    def api(self, body):
        act, target = body.get("action"), body.get("target")
        if act in ("connect", "remove", "test") and target not in TARGETS:
            return 400, {"error": "target must be 'alerts' or 'log'"}
        d = self.to_alerts if target == "alerts" else self.to_log
        if act == "connect":
            url = str(body.get("url", "")).strip()
            if not community.WEBHOOK_RE.fullmatch(url):
                return 400, {"error": "that is not a Discord webhook URL (https://discord.com/api/webhooks/...)"}
            try:
                channel = self.check_unique(target, url)
            except ValueError as e:
                return 400, {"error": str(e)}
            with self.c.lock:
                self.settings().update({f"{target}_webhook": url, f"{target}_channel": channel, f"{target}_on": True})
                self.c.save()
            self.mgr.events.add("info", None, f"Discord webhook for {TARGETS[target]} saved")
            ok, why = d.send(CONNECTED[target])
            return (200, {"ok": True}) if ok else (502, {"error": f"saved, but the test message failed: {why}"})
        if act == "remove":
            with self.c.lock:
                ch = self.settings()
                ch.pop(f"{target}_webhook", None)
                ch.pop(f"{target}_channel", None)
                self.c.save()
            self.mgr.events.add("info", None, f"Discord webhook for {TARGETS[target]} removed")
            return 200, {"ok": True}
        if act == "test":
            ok, why = d.send(f"✅ Test message from ServerDeck ({TARGETS[target]}).")
            return (200, {"ok": True}) if ok else (502, {"error": why})
        if act == "try":                                 # would this message raise an alert?
            words = body.get("words") if isinstance(body.get("words"), list) else self.copy()["words"]
            rx = compile_words(words)
            hit = rx.search(str(body.get("text", ""))) if rx else None
            return 200, {"match": hit.group(0) if hit else None}
        if act == "save":
            try:
                new = {}
                for k in ("alerts_on", "log_on"):
                    if k in body:
                        new[k] = bool(body[k])
                if "mention" in body:
                    new["mention"] = mention(body["mention"])[0]
                if "words" in body:
                    words = [str(w).strip()[:60] for w in body["words"] if str(w).strip().rstrip("*").strip()][:100]
                    if not words:
                        raise ValueError("give at least one alert word")
                    new["words"] = words
                if "servers" in body:
                    new["servers"] = [s for s in body["servers"] if s in self.sources]
            except (ValueError, TypeError) as e:
                return 400, {"error": str(e)}
            with self.c.lock:
                self.settings().update(new)
                self.c.save()
            self.mgr.events.add("info", None, "chat watch settings saved")
            return 200, {"ok": True}
        return 400, {"error": "unknown action"}
