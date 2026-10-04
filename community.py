"""Community features for ServerDeck (2026-10-02).

* Events - SCUM's "Blood Moon": a weekly window (default Saturday 18:00 for 12 h) during which
  SCUM runs a horde-heavy settings profile. ServerDeck restarts SCUM when the window opens and
  closes (normally the same moments as its 6-hourly restarts), writes the profile into
  ServerSettings.ini just before SCUM starts, and puts the normal values back afterwards.
* SCUM notifications - restart warnings, rotating survival tips and event notices, written to
  SCUM's Notifications.json before every start (SCUM reads that file when it starts).
* Discord feed (webhook) - restarts/updates/alarms of the chosen servers, Blood Moon, SCUM quest
  completions and walker-kill awards, and a weekly leaderboard.

The webhook URL is a secret: it is kept only in state.json, never logged, never sent back to
the browser.
"""
import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import hooks

log = logging.getLogger("serverdeck")

WEBHOOK_RE = re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+")
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
FEEDS = ("restarts", "alarms", "events", "quests", "milestones", "leaderboard")
NPC_CODES = {"AR": "Armorer", "DC": "Doctor", "GG": "General Goods", "MC": "Mechanic", "RH": "Hunter",
             "MH": "Master Hunter", "BT": "Bartender", "FM": "Fisherman", "BK": "Banker", "BB": "Barber"}
E = {"restart": "\U0001F504", "ok": "\u2705", "warn": "\u26A0\uFE0F", "stop": "\u23F9\uFE0F", "fail": "\U0001F6D1",
     "update": "\u2B07\uFE0F", "red": "\U0001F534", "green": "\U0001F7E2", "blood": "\U0001FA78", "dawn": "\U0001F305",
     "quest": "\U0001F4DC", "zombie": "\U0001F9DF", "trophy": "\U0001F3C6", "star": "\u2B50"}


def hhmm(text):
    h, m = map(int, str(text).split(":"))
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(text)
    return h, m


def scum_time(dt):
    return f"{dt.hour}:{dt.minute:02d}"            # SCUM's own style: "5:50", "23:59"


def scum_restart_label(dt):
    return f"00:{dt.minute:02d}" if dt.hour == 0 else f"{dt.hour}:{dt.minute:02d}"


def md(text):
    """Player names are user input: no Discord markdown tricks."""
    return re.sub(r"([\\*_~`|>])", r"\\\1", str(text))


def fmt_day_time(dt):
    return f"{DAYS[dt.weekday()]} {dt:%H:%M}"


def split_message(text, limit=1800):
    """Discord takes 2000 characters per message: split on blank lines, then on lines."""
    parts, cur = [], ""
    for block in text.split("\n\n"):
        pieces = [block] if len(block) <= limit else block.split("\n")
        for i, piece in enumerate(pieces):
            sep = "" if not cur else ("\n\n" if i == 0 else "\n")
            if cur and len(cur) + len(sep) + len(piece) > limit:
                parts.append(cur)
                cur = piece[:limit]
            else:
                cur += sep + piece[:limit]
    if cur:
        parts.append(cur)
    return parts


# --------------------------------------------------------------------------
# Weekly event windows
# --------------------------------------------------------------------------

class Event:
    def __init__(self, server, cfg, overrides):
        self.server = server
        self.cfg = dict(cfg, **{k: v for k, v in (overrides or {}).items() if v is not None})
        self.id = self.cfg["id"]
        self.name = self.cfg.get("name", self.id)

    @property
    def enabled(self):
        return bool(self.cfg.get("enabled", True))

    @property
    def weekday(self):
        return int(self.cfg.get("weekday", 5))

    @property
    def hours(self):
        return max(1, min(24, float(self.cfg.get("hours", 12))))

    def _start_on(self, day):
        h, m = hhmm(self.cfg.get("start", "18:00"))
        return day.replace(hour=h, minute=m, second=0, microsecond=0)

    def window_at(self, at):
        """(start, end) of the occurrence covering `at`, else None."""
        for back in range(0, 3):
            start = self._start_on(at - timedelta(days=back))
            if start.weekday() == self.weekday and start <= at < start + timedelta(hours=self.hours):
                return start, start + timedelta(hours=self.hours)
        return None

    def next_window(self, after):
        for fwd in range(0, 9):
            start = self._start_on(after + timedelta(days=fwd))
            if start.weekday() == self.weekday and start > after:
                return start, start + timedelta(hours=self.hours)
        return None

    def boundaries(self, after, until):
        out = []
        for back in range(-1, 9):
            start = self._start_on(after + timedelta(days=back))
            if start.weekday() != self.weekday:
                continue
            for b in (start, start + timedelta(hours=self.hours)):
                if after < b <= until:
                    out.append(b)
        return sorted(set(out))

    def fill(self, text, window):
        start, end = window
        return text.format(start=f"{start:%H:%M}", end=f"{end:%H:%M}", day=DAYS[start.weekday()],
                           end_day=DAYS[end.weekday()], name=self.name)

    def describe(self, now):
        cur = self.window_at(now)
        nxt = self.next_window(now)
        return {"id": self.id, "name": self.name, "enabled": self.enabled, "active": bool(self.enabled and cur),
                "weekday": self.weekday, "start": self.cfg.get("start", "18:00"), "hours": self.hours,
                "window": [cur[0].timestamp(), cur[1].timestamp()] if cur else None,
                "next": [nxt[0].timestamp(), nxt[1].timestamp()] if nxt else None}


# --------------------------------------------------------------------------
# Discord webhook
# --------------------------------------------------------------------------

class Discord:
    def __init__(self, community):
        self.c = community
        self.q = queue.Queue(maxsize=200)
        self.last_error = None
        self.last_ok = None
        self.sent = 0

    def webhook(self):
        return (self.c.settings().get("discord") or {}).get("webhook")

    def wants(self, feed, server_id=None):
        d = self.c.settings().get("discord") or {}
        if not d.get("webhook") or not d.get("feeds", {}).get(feed, True):
            return False
        return server_id is None or server_id in d.get("servers", [])

    def post(self, text, feed, server_id=None):
        if not self.wants(feed, server_id):
            return
        try:
            self.q.put_nowait(text)
        except queue.Full:
            log.warning("discord queue full - message dropped")

    def send(self, text, ping_everyone=False):
        url = self.webhook()
        if not url:
            return False, "no webhook configured"
        # Mentions stay off (player names are user input) - except an announcement that asks for @everyone.
        body = json.dumps({"content": text[:1990], "username": self.c.cfg.get("discord_username", "ServerDeck"),
                           "allowed_mentions": {"parse": ["everyone"] if ping_everyone else []}}).encode()
        for attempt in range(4):
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": "ServerDeck"})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    r.read()
                self.last_ok, self.last_error = time.time(), None
                self.sent += 1
                return True, "sent"
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    try:
                        wait = float(json.loads(e.read() or b"{}").get("retry_after", 2))
                    except ValueError:
                        wait = 2
                    time.sleep(min(30, wait + 0.5))
                    continue
                self.last_error = f"Discord refused the message (HTTP {e.code})" + \
                    (" - is the webhook deleted?" if e.code in (401, 403, 404) else "")
                return False, self.last_error
            except (urllib.error.URLError, OSError) as e:
                self.last_error = f"cannot reach Discord ({getattr(e, 'reason', e)})"
                time.sleep(5 * (attempt + 1))
        return False, self.last_error

    def worker(self):
        while True:
            text = self.q.get()
            ok, why = self.send(text)
            if not ok:
                log.warning("discord: %s", why)       # never the URL
            time.sleep(1.2)                          # stay far below Discord's rate limit


# --------------------------------------------------------------------------
# SCUM gameplay logs -> quest completions, walker kills, fame
# --------------------------------------------------------------------------

QUEST_RE = re.compile(r"\[LogQuestStatus\]\s*(?P<who>.+?)\s+completed quest\s+(?P<quest>.+?)\s*$")
SUMMARY_RE = re.compile(r"Player (?P<who>.+?) was awarded (?P<pts>-?[\d.]+) fame points in 10 minutes")
AWARD_RE = re.compile(r"Player (?P<who>.+?) was awarded (?P<pts>-?[\d.]+) fame points for (?P<reason>\w+)")


def who_key(who):
    s = who.strip().strip("'")
    sid = re.search(r"\d{17}", s)
    name = re.sub(r"^.*?\d{17}:", "", s)                  # "1.2.3.4 7656...:Name(1)"
    name = re.sub(r"\((?:\d{17}|\d{1,6})\)\s*$", "", name).strip() or s
    return (sid.group(0) if sid else name.lower()), name


class ScumFeed:
    PREFIXES = ("famepoints", "quests", "gameplay")

    def __init__(self, community, server):
        self.c, self.s = community, server
        self.dir = Path(server.cfg["install_dir"]) / "SCUM" / "Saved" / "SaveFiles" / "Logs"
        self.quest_dir = Path(server.cfg["install_dir"]) / "SCUM" / "Saved" / "Config" / "WindowsServer" / "Quests"
        self.pos = {}
        self.quests = {}
        self.quests_at = 0
        self.seen_lines = 0

    def poll(self):
        for p in self.PREFIXES:
            files = sorted(self.dir.glob(f"{p}_*.log"))          # timestamped names sort by time
            if not files:
                continue
            newest = files[-1]
            cur = self.pos.get(p)
            if cur is None:                                      # first look: skip what's already there
                self.pos[p] = [newest, newest.stat().st_size, b""]
                continue
            if cur[0] != newest:                                 # SCUM restarted: finish the old file
                self._read(p, cur)
                self.pos[p] = cur = [newest, 0, b""]
            self._read(p, cur)

    def _read(self, prefix, cur):
        path, off, carry = cur
        try:
            size = path.stat().st_size
            if size < off:
                off, carry = 0, b""
            if size == off:
                return
            with open(path, "rb") as f:
                f.seek(off)
                data = f.read(size - off)
        except OSError:
            return
        cur[1] = off + len(data)
        buf = carry + data
        odd = b""
        if len(buf) % 2:                                        # UTF-16LE: whole code units only
            buf, odd = buf[:-1], buf[-1:]
        text = buf.decode("utf-16-le", "replace").lstrip("\ufeff")
        lines = text.split("\n")
        cur[2] = lines.pop().encode("utf-16-le") + odd         # keep the unfinished last line
        for line in lines:
            self.seen_lines += 1
            try:
                self._line(line.rstrip("\r"))
            except Exception:
                log.exception("scum feed line failed")

    def _quest_info(self, quest):
        """'Quests/Override/WL_BB_T3_Wear_The_Dead.json', 'T1_AR_Fetch_M9' or a title -> (title, npc, tier)."""
        if time.time() - self.quests_at > 600:
            index = {}
            for f in (self.quest_dir / "Override").glob("*.json"):
                try:
                    q = {k.lower(): v for k, v in json.loads(f.read_text(encoding="utf-8-sig")).items()}
                    info = (q.get("title") or f.stem, q.get("associatednpc"), q.get("tier"))
                    index[f.stem.lower()] = info
                    index[str(q.get("title", "")).lower()] = info
                except (OSError, ValueError):
                    pass
            self.quests, self.quests_at = index, time.time()
        key = Path(quest.replace("\\", "/")).stem.lower() if ("/" in quest or quest.endswith(".json")) else quest.lower()
        if key in self.quests:
            return self.quests[key]
        m = re.match(r"T(\d)_([A-Z]{2})_(.+)", quest)
        if m:
            return m.group(3).replace("_", " "), NPC_CODES.get(m.group(2), m.group(2)), int(m.group(1))
        return quest, None, None

    def _line(self, line):
        m = QUEST_RE.search(line)
        if m:
            key, name = who_key(m.group("who"))
            title, npc, tier = self._quest_info(m.group("quest"))
            self.c.count(key, name, quests=1)
            by = f" for the {npc}" if npc else ""
            self.c.discord.post(f"{E['quest']} **{md(name)}** completed *{md(title)}*{by}"
                                f"{f' (tier {tier})' if tier else ''}", "quests", self.s.id)
            return
        m = SUMMARY_RE.search(line)
        if m:
            key, name = who_key(m.group("who"))
            self.c.count(key, name, fame=float(m.group("pts")))
            return
        m = AWARD_RE.search(line)
        if m:
            key, name = who_key(m.group("who"))
            reason = m.group("reason")
            if reason == "PuppetKills":
                self.c.count(key, name, walkers=1)
            elif re.fullmatch(r"PuppetKills(\d+)", reason):
                n = re.fullmatch(r"PuppetKills(\d+)", reason).group(1)
                if int(n) >= 100:
                    self.c.discord.post(f"{E['zombie']} **{md(name)}** earned the {n}-walker kill award",
                                        "milestones", self.s.id)


# --------------------------------------------------------------------------
# The feature hub
# --------------------------------------------------------------------------

class Community:
    def __init__(self, mgr, path):
        self.mgr = mgr
        self.cfg = mgr.config.get("community", {})
        self.lock = threading.RLock()
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.discord = Discord(self)
        self.feeds = [ScumFeed(self, s) for s in mgr.servers if s.cfg.get("scum_feed")]
        self.dirty = False

    # ---- settings + weekly stats (community.json, holds the webhook secret) ----
    def settings(self):
        with self.lock:
            st = self.data
            d = st.setdefault("discord", {})
            d.setdefault("feeds", {f: True for f in FEEDS})
            for f in FEEDS:
                d["feeds"].setdefault(f, True)
            d.setdefault("servers", list(self.cfg.get("announce_servers", [])))
            st.setdefault("leaderboard", dict(self.cfg.get("leaderboard", {"weekday": 6, "time": "20:00"})))
            st.setdefault("events", {})
            st.setdefault("tips", {})
            st.setdefault("event_state", {})
            st.setdefault("week", {"start": time.time(), "players": {}})
            return st

    def save(self, st=None):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            self.dirty = False

    def count(self, key, name, walkers=0, quests=0, fame=0.0):
        with self.lock:
            st = self.settings()
            p = st["week"]["players"].setdefault(key, {"name": name, "walkers": 0, "quests": 0, "fame": 0.0})
            p["name"] = name
            p["walkers"] += walkers
            p["quests"] += quests
            p["fame"] = round(p["fame"] + fame, 2)
            self.dirty = True                             # saved by the loop (debounced)

    # ---- events ----
    def events(self, server):
        ov = self.settings()["events"]
        return [Event(server, e, ov.get(e["id"])) for e in server.cfg.get("events", [])]

    def active_event(self, server, at):
        for ev in self.events(server):
            if ev.enabled and ev.window_at(at):
                return ev
        return None

    def next_boundary(self, server, now):
        """Next moment an enabled event opens or closes (ServerDeck restarts the server then)."""
        times = [b for ev in self.events(server) if ev.enabled for b in ev.boundaries(now, now + timedelta(days=8))]
        return min(times) if times else None

    def server_events(self, server):
        now = datetime.now()
        return [ev.describe(now) for ev in self.events(server)]

    def upcoming_restarts(self, server, now, hours=24):
        out, t = [], now
        end = now + timedelta(hours=hours)
        while len(out) < 50:
            nxt = server.next_restart(t)
            if not nxt or nxt > end:
                break
            out.append(nxt)
            t = nxt + timedelta(seconds=1)
        return out

    # ---- before SCUM starts ----
    def prestart(self, server, now=None):
        """Called from SCUM's pre_start hook: event profile in or out, then notifications."""
        now = now or datetime.now()
        ev = self.active_event(server, now + timedelta(minutes=2))   # restarts land on the boundary
        self.apply_profile(server, ev, now)
        self.write_scum_notifications(server, now)

    def _ini_path(self, server, ev_cfg):
        return Path(server.cfg["install_dir"]) / ev_cfg.get("ini", r"SCUM\Saved\Config\WindowsServer\ServerSettings.ini")

    @staticmethod
    def _ini_edit(path, values):
        """Set key=value lines (keys as written in the file, e.g. 'scum.FameGainMultiplier');
        returns the previous values. Keeps SCUM's encoding and line endings."""
        raw = path.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        nl = "\r\n" if "\r\n" in text else "\n"
        lines, old = text.split(nl), {}
        for i, line in enumerate(lines):
            key = line.split("=", 1)[0]
            if key in values and "=" in line:
                old[key] = line.split("=", 1)[1]
                lines[i] = f"{key}={values[key]}"
        out = nl.join(lines).encode("utf-8")
        path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + out)
        return old

    def apply_profile(self, server, ev, now):
        with self.lock:
            st = self.settings()
            es = st["event_state"].setdefault(server.id, {"active": None, "baseline": {}})
            if ev and es.get("active") == ev.id:
                return
            if es.get("active") and (not ev or es["active"] != ev.id):
                old_cfg = next((e.cfg for e in self.events(server) if e.id == es["active"]), None)
                if old_cfg and es.get("baseline"):
                    self._ini_edit(self._ini_path(server, old_cfg), es["baseline"])
                ended = es["active"]
                es.update(active=None, baseline={}, since=None)
                self.save(st)
                self.mgr.events.add("info", server.id, f"event over: {ended} - normal settings restored")
                if old_cfg:
                    self.discord.post(old_cfg.get("discord", {}).get(
                        "end", f"{E['dawn']} **Dawn.** The event is over."), "events", server.id)
            if ev:
                window = ev.window_at(now + timedelta(minutes=2))
                settings = {k: ev.fill(v, window) if isinstance(v, str) else v for k, v in ev.cfg["settings"].items()}
                baseline = self._ini_edit(self._ini_path(server, ev.cfg), settings)
                es.update(active=ev.id, baseline=baseline, since=time.time())
                self.save(st)
                self.mgr.events.add("info", server.id, f"{ev.name} is on until {window[1]:%a %H:%M} - "
                                                       f"{len(settings)} settings switched for the night")
                self.discord.post(ev.fill(ev.cfg.get("discord", {}).get(
                    "start", "{name} has begun."), window), "events", server.id)

    # ---- SCUM's Notifications.json ----
    def write_scum_notifications(self, server, now=None):
        """Restart warnings + survival tips + event notices; SCUM reads the file when it starts.
        Times in the file are daily clock times, so only what happens before the next restart
        (at most 24 h ahead) is written - the file is rewritten before every start."""
        if server.cfg.get("announce") != "scum_notifications":
            return False
        now = now or datetime.now()
        path = Path(server.cfg["install_dir"]) / "SCUM" / "Saved" / "Config" / "WindowsServer" / "Notifications.json"
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        data = json.loads(raw) if raw.strip() else {}
        with self.lock:
            st = self.settings()
            ours_before = set(st.get("scum_notes_written", []))
        keep = [n for n in data.get("Notifications", [])
                if not str(n.get("message", "")).startswith("#RestartAt(") and n.get("message") not in ours_before]
        entries, ours = [], []
        restarts = self.upcoming_restarts(server, now)
        horizon = restarts[0] if restarts else now + timedelta(hours=24)
        sch = server.schedule()
        offsets = hooks.warning_offsets(sch["warn_minutes"])
        busy = set()                                   # minutes of the day already used by a message
        for r in restarts:
            times = [r - timedelta(minutes=o) for o in offsets]
            if times:
                entries.append({"color": "255-180-50", "message": f"#RestartAt({scum_restart_label(r)})",
                                "time": [scum_time(t) for t in times]})
                busy.update((t.hour * 60 + t.minute) for t in times)
        # event notices for this run
        for ev in self.events(server):
            if not ev.enabled:
                continue
            nc = ev.cfg.get("notices", {})
            color = ev.cfg.get("notice_color", "220-40-40")
            cur = ev.window_at(now + timedelta(minutes=2))
            nxt = ev.next_window(now)
            plan = []
            if cur:
                t = now.replace(second=0, microsecond=0) + timedelta(minutes=5)
                while t < min(cur[1], horizon):
                    if t.minute in (5, 35):
                        plan.append((t, ev.fill(nc.get("during", "{name} until {end}"), cur)))
                    t += timedelta(minutes=1)
                for mins in (30, 10):
                    t = cur[1] - timedelta(minutes=mins)
                    if now < t <= horizon:
                        plan.append((t, ev.fill(nc.get("ending", "{name} ends at {end}"), cur)))
            if nxt and now < nxt[0] <= horizon + timedelta(minutes=1):
                for mins in (60, 30, 15):                 # (the restart warning covers the last minutes)
                    t = nxt[0] - timedelta(minutes=mins)
                    if t > now:
                        plan.append((t, ev.fill(nc.get("before", "{name} begins at {start}"), nxt)))
            by_msg = {}
            for t, msg in plan:
                by_msg.setdefault(msg, []).append(t)
                busy.add(t.hour * 60 + t.minute)
            for msg, times in by_msg.items():
                entries.append({"color": color, "message": msg, "time": [scum_time(t) for t in sorted(times)]})
                ours.append(msg)
        # rotating survival tips
        tips = dict(server.cfg.get("tips", {}), **{k: v for k, v in st["tips"].items() if v is not None})
        messages = [m for m in tips.get("messages", []) if m]
        if tips.get("enabled", True) and messages:
            every = max(10, int(tips.get("every_minutes", 30)))
            first = int(tips.get("offset_minute", 15)) % every
            slots = []
            for minute in range(first, 1440, every):
                near = any(abs(minute - b) <= 3 or 0 < (b - minute) % 1440 <= 3 for b in busy)
                restart_soon = any(0 <= ((r.hour * 60 + r.minute) - minute) % 1440 <= 12 for r in restarts)
                if not near and not restart_soon:
                    slots.append(minute)
            rotate = (now.timetuple().tm_yday * 7 + now.hour) % len(messages)
            by_tip = {}
            for i, minute in enumerate(slots):
                by_tip.setdefault(messages[(i + rotate) % len(messages)], []).append(minute)
            for msg, minutes in by_tip.items():
                entries.append({"color": tips.get("color", "150-200-255"), "message": msg,
                                "time": [f"{m // 60}:{m % 60:02d}" for m in sorted(minutes)]})
                ours.append(msg)
        data["Notifications"] = keep + entries
        text = json.dumps(data, indent=4)
        with self.lock:
            st = self.settings()
            st["scum_notes_written"] = ours
            self.save(st)
        if raw.replace("\r\n", "\n").strip() == text:
            return False
        backup = path.with_name("Notifications.json.before-serverdeck")
        if path.exists() and not backup.exists():
            backup.write_bytes(path.read_bytes())
        path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        return True

    # ---- ServerDeck events -> Discord ----
    def on_event(self, entry):
        sid, msg = entry.get("server"), entry.get("msg", "")
        if not self.discord.webhook():
            return
        srv = self.mgr.by_id.get(sid) if sid else None
        name = md(srv.name) if srv else "This PC"
        if srv is None and not msg.startswith(("PC STRUGGLING", "PC recovered")):
            return
        text, feed = None, "restarts"
        m = re.match(r"(scheduled restart (\d\d:\d\d)|restart|auto-start|start|stop|update|auto-update): (started|done)$", msg)
        if m:
            what, slot, phase = m.group(1), m.group(2), m.group(3)
            if phase == "started":
                if slot:
                    text = f"{E['restart']} **{name}** - scheduled restart ({slot}), back in a few minutes"
                elif what == "restart":
                    text = f"{E['restart']} **{name}** is restarting"
                elif what == "stop":
                    text = f"{E['stop']} **{name}** is shutting down"
                elif what in ("update", "auto-update"):
                    text = f"{E['update']} **{name}** is installing an update"
            else:
                text = f"{E['stop']} **{name}** is offline" if what == "stop" else f"{E['ok']} **{name}** is online"
        elif msg == "server is down but should be online - starting it":
            text = f"{E['warn']} **{name}** went down unexpectedly - bringing it back up"
        elif re.match(r"(scheduled restart \d\d:\d\d|restart|auto-start|start|update|auto-update) failed: ", msg):
            text, feed = f"{E['fail']} **{name}**: {msg.split(' failed:', 1)[0]} failed - the admins have been told", "alarms"
        elif msg.startswith("crash loop"):
            text, feed = f"{E['fail']} **{name}** keeps crashing - automatic restarts paused for 30 minutes", "alarms"
        elif msg.startswith(("STRUGGLING: ", "PC STRUGGLING: ")):
            text, feed = f"{E['red']} **{name}** is struggling: {md(msg.split(': ', 1)[1])}", "alarms"
        elif msg.startswith("recovered - no longer struggling") or msg.startswith("PC recovered"):
            text, feed = f"{E['green']} **{name}** recovered", "alarms"
        if text:
            self.discord.post(text, feed, sid)

    # ---- background loop: SCUM logs, leaderboard, "event soon" ----
    def start(self):
        threading.Thread(target=self.discord.worker, daemon=True, name="discord").start()
        threading.Thread(target=self.loop, daemon=True, name="community").start()

    def loop(self):
        time.sleep(15)
        while True:
            try:
                for f in self.feeds:
                    f.poll()
                now = datetime.now()
                self.leaderboard_tick(now)
                self.event_soon_tick(now)
                if self.dirty:
                    self.save()
            except Exception:
                log.exception("community loop error")
            time.sleep(10)

    def leaderboard_due(self, now):
        lb = self.settings()["leaderboard"]
        h, m = hhmm(lb.get("time", "20:00"))
        due = now.replace(hour=h, minute=m, second=0, microsecond=0)
        while due.weekday() != int(lb.get("weekday", 6)) or due > now:
            due -= timedelta(days=1)
        return due

    def leaderboard_tick(self, now):
        due = self.leaderboard_due(now)
        with self.lock:
            st = self.settings()
            week = st["week"]
            if week.get("start", 0) >= due.timestamp():
                return                                    # this week's board already went out
            text = self.leaderboard_text(week, datetime.fromtimestamp(week.get("start", time.time())), due)
            st["week"] = {"start": due.timestamp(), "players": {}, "last": {"text": text, "t": time.time()}}
            self.save(st)
        if text:
            self.discord.post(text, "leaderboard")

    def leaderboard_text(self, week, since, until):
        players = list(week.get("players", {}).values())
        if not any(p["walkers"] or p["quests"] or p["fame"] for p in players):
            return None
        title = self.cfg.get("leaderboard_title", "Weekly leaderboard")
        out = [f"{E['trophy']} **{md(title)}** - {since:%b %d} to {until:%b %d}"]
        for key, title, unit, n, icon in (("walkers", "Walker hunters", "walkers", 10, E["zombie"]),
                                           ("quests", "Quest runners", "quests", 5, E["quest"]),
                                           ("fame", "Fame earned", "fame", 5, E["star"])):
            top = sorted((p for p in players if p[key]), key=lambda p: -p[key])[:n]
            if top:
                out.append(f"\n{icon} **{title}**")
                out += [f"{i}. {md(p['name'])} - {int(round(p[key])):,} {unit if int(round(p[key])) != 1 else unit.rstrip('s')}"
                        for i, p in enumerate(top, 1)]
        return "\n".join(out)

    def event_soon_tick(self, now):
        for s in self.mgr.servers:
            for ev in self.events(s):
                nxt = ev.next_window(now)
                if not ev.enabled or not nxt or not (timedelta(minutes=55) < nxt[0] - now <= timedelta(minutes=60)):
                    continue
                key = f"{ev.id}:{nxt[0]:%Y-%m-%d %H:%M}"
                with self.lock:
                    st = self.settings()
                    if st.get("soon_posted") == key:
                        continue
                    st["soon_posted"] = key
                    self.save(st)
                text = ev.cfg.get("discord", {}).get("soon")
                if text:
                    self.discord.post(ev.fill(text, nxt), "events", s.id)

    # ---- API ----
    def summary(self):
        with self.lock:                     # the feed thread may be counting at the same moment
            st = json.loads(json.dumps(self.settings()))
        d = st["discord"]
        week = st["week"]
        players = sorted(week.get("players", {}).values(), key=lambda p: (-p["walkers"], -p["quests"], -p["fame"]))
        tips_cfg = {}
        for s in self.mgr.servers:
            if s.cfg.get("tips"):
                tips_cfg = dict(s.cfg["tips"], **{k: v for k, v in st["tips"].items() if v is not None})
        return {
            "discord": {"configured": bool(d.get("webhook")), "feeds": d["feeds"], "servers": d.get("servers", []),
                        "last_error": self.discord.last_error, "last_ok": self.discord.last_ok, "sent": self.discord.sent,
                        "queued": self.discord.q.qsize()},
            "leaderboard": {**st["leaderboard"], "next": self.next_leaderboard().timestamp()},
            "week": {"start": week.get("start"), "top": players[:10], "players": len(players),
                     "last": (week.get("last") or {}).get("t")},
            "tips": {"enabled": tips_cfg.get("enabled", True), "every_minutes": tips_cfg.get("every_minutes", 30),
                     "count": len(tips_cfg.get("messages", []))},
            "feed_lines": sum(f.seen_lines for f in self.feeds),
        }

    def next_leaderboard(self):
        return self.leaderboard_due(datetime.now()) + timedelta(days=7)

    def api(self, body):
        act = body.get("action")
        if act == "webhook":
            url = str(body.get("url", "")).strip()
            if not WEBHOOK_RE.fullmatch(url):
                return 400, {"error": "that is not a Discord webhook URL (https://discord.com/api/webhooks/...)"}
            with self.lock:
                st = self.settings()
                st["discord"]["webhook"] = url
                self.save(st)
            self.mgr.events.add("info", None, "Discord webhook saved")
            ok, why = self.discord.send(f"{E['ok']} ServerDeck is connected - server news will be posted here.")
            return (200, {"ok": True}) if ok else (502, {"error": f"saved, but the test message failed: {why}"})
        if act == "remove_webhook":
            with self.lock:
                st = self.settings()
                st["discord"].pop("webhook", None)
                self.save(st)
            self.mgr.events.add("info", None, "Discord webhook removed")
            return 200, {"ok": True}
        if act == "test":
            ok, why = self.discord.send(f"{E['ok']} Test message from ServerDeck.")
            return (200, {"ok": True}) if ok else (502, {"error": why})
        if act == "announce":
            text = str(body.get("text", "")).strip()
            if not text or len(text) > 8000:
                return 400, {"error": "write 1-8000 characters"}
            ping = bool(body.get("ping"))
            parts = split_message(text)
            for i, part in enumerate(parts):
                ok, why = self.discord.send(("@everyone\n" + part) if ping and i == 0 else part, ping_everyone=ping and i == 0)
                if not ok:
                    return 502, {"error": f"message {i + 1} of {len(parts)} failed: {why}"}
                time.sleep(1.2)
            self.mgr.events.add("info", None, f"announcement posted to Discord ({len(parts)} message(s)"
                                              f"{', @everyone' if ping else ''})")
            return 200, {"ok": True, "messages": len(parts)}
        if act == "leaderboard_preview":
            with self.lock:
                week = json.loads(json.dumps(self.settings()["week"]))
            text = self.leaderboard_text(week, datetime.fromtimestamp(week.get("start", time.time())), datetime.now())
            if body.get("post"):
                if not text:
                    return 409, {"error": "no activity recorded this week yet"}
                ok, why = self.discord.send(text)
                return (200, {"ok": True, "text": text}) if ok else (502, {"error": why})
            return 200, {"text": text or "(no activity recorded this week yet)"}
        if act == "save":
            try:
                with self.lock:
                    st = self.settings()
                    d = st["discord"]
                    if "feeds" in body:
                        d["feeds"] = {f: bool(body["feeds"].get(f, d["feeds"].get(f, True))) for f in FEEDS}
                    if "servers" in body:
                        d["servers"] = [s for s in body["servers"] if s in self.mgr.by_id]
                    if "leaderboard" in body:
                        lb = body["leaderboard"]
                        hhmm(lb.get("time", "20:00"))
                        st["leaderboard"] = {"weekday": int(lb.get("weekday", 6)) % 7, "time": str(lb.get("time", "20:00"))}
                    for eid, e in (body.get("events") or {}).items():
                        hhmm(e.get("start", "18:00"))
                        hours = float(e.get("hours", 12))
                        if not 1 <= hours <= 24:
                            raise ValueError("hours")
                        st["events"][eid] = {"enabled": bool(e.get("enabled", True)), "weekday": int(e.get("weekday", 5)) % 7,
                                             "start": str(e.get("start", "18:00")), "hours": hours}
                    if "tips" in body:
                        t = body["tips"]
                        st["tips"] = {"enabled": bool(t.get("enabled", True)),
                                      "every_minutes": max(10, min(180, int(t.get("every_minutes", 30))))}
                    self.save(st)
            except (ValueError, TypeError, KeyError) as e:
                return 400, {"error": f"invalid setting ({e})"}
            self.mgr.events.add("info", None, "community settings saved")
            notes = []
            for s in self.mgr.servers:
                if s.cfg.get("announce") == "scum_notifications" and self.write_scum_notifications(s):
                    notes.append(f"{s.name} picks up the new tips/event messages when it next starts.")
            return 200, {"ok": True, "note": " ".join(notes) or None}
        return 400, {"error": "unknown action"}
