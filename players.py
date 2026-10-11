"""Players: who is online, how long they have been around, kicks, bans and admin/moderator roles - for
every server type - plus device bans through a server's sidecar.

Who is online comes from what each game offers:
  SCUM             its login log (SaveFiles\\Logs\\login_*.log: "'<ip> <steamid>:<name>(<n>)' logged in/out")
  Space Engineers  the VRage Remote API (session/players)
  CS2              RCON "status_json"
  Rust             RCON "playerlist"
  Palworld         the REST API (players)
  anything else    a Steam A2S player query - names only (Enshrouded)

First seen / last seen / time played: ServerDeck notes everyone it sees online (players.json, every 30 s).
SCUM keeps its own history in SCUM.db (user_profile.creation_time and play_time, in seconds), which
goes back further, so for SCUM that is used where it is known.

Bans go into the game's own ban list:
  SCUM             BannedUsers.ini (one SteamID64 per line). SCUM reads it when it starts and has no remote
                   console, so a ban made while it runs takes effect at its next restart; it can't kick either.
  Space Engineers  Remote API admin/bannedPlayers (kicks them too)
  CS2              RCON banid + writeid, then kickid. CS2 keeps the list in memory and nothing execs
                   banned_user.cfg, so ServerDeck bans again after every start.
  Rust             RCON banid (+ kick, server.writecfg)
  Palworld         REST ban / unban
  Enshrouded       in-game only (its list holds hashed accounts, not Steam IDs)
ServerDeck keeps the bans it made in players.json - to list and lift them, to apply them to a server
that was offline at the time once it is back, and to re-apply CS2's after every start.

Device bans: a sidecar with an admin API (POST /ban {steam_id, ban_type, scope, reason_code, details},
POST /unban {ban_id}, GET /players, header X-Admin-Key = admin_api_key from its config.toml, or the
sidecar's "admin_key" setting) bans the player's PC rather than their account. The sidecar only runs
while its server is online and only knows the devices of players who verified with it.

Roles (admin / moderator) use the game's own lists:
  SCUM             AdminUsers.ini - admin = "<id>[SetGodMode, ShutdownServer]", moderator = "<id>" (the
                   standard admin commands); read when SCUM starts
  Space Engineers  <Administrators> in SpaceEngineers-Dedicated.cfg (admin only - moderators are promoted
                   in-game); read when the server starts
  CS2              CounterStrikeSharp's admins.json (flags per role: the server's "role_flags" setting, else
                   @css/root / the kick-ban-chat flags); loaded when CS2 starts
  Rust             ownerid / moderatorid over RCON while it runs (users.cfg while it is stopped)
  Palworld, Enshrouded: password-based in-game, nothing per player
"""
import calendar
import glob
import json
import logging
import re
import sqlite3
import threading
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import a2s
import palworld
import se_api

log = logging.getLogger("serverdeck")

STEAM_ID = re.compile(r"7656\d{13}")
STEAM64_BASE = (1 << 56) | (1 << 52) | (1 << 32)      # universe 1, individual account, instance 1, account 0
POLL_SECONDS = 30          # who is online, from the supervisor loop
RETRY_SECONDS = 300        # a ban that failed is tried again this often
SAVE_SECONDS = 60          # players.json is written at most this often (sooner for new players / bans)
SCUM_LOGIN = re.compile(r"^(\d{4}\.\d\d\.\d\d-\d\d\.\d\d\.\d\d): '(?:\S+ )?(7656\d{13}):(.*?)\((\d+)\)' logged (in|out)\b")
ROLES = ("admin", "moderator")


def source(cfg):
    """How a server reports its players: an explicit "players" setting, else what its config implies."""
    if cfg.get("players"):
        return cfg["players"]
    if (cfg.get("perf") or {}).get("kind") == "scum_log":
        return "scum_log"
    if cfg.get("remote_api"):
        return "se_remote"
    if cfg.get("rest_api"):
        return "palworld_rest"
    kind = (cfg.get("rcon") or {}).get("kind")
    if kind == "source":
        return "cs2_rcon"
    if kind == "web":
        return "rust_rcon"
    if cfg.get("query"):
        return "a2s"
    return None


def clean(text, n=120):
    """For a quoted console argument: no quotes, newlines or command separators."""
    return re.sub(r'["\r\n;]', "'", str(text or "")).strip()[:n]


def steam_forms(steam_id):
    """The ways Source engines write one account: SteamID64, [U:1:n], STEAM_1:y:z."""
    acct = int(steam_id) - STEAM64_BASE
    return [steam_id, f"[U:1:{acct}]", f"STEAM_1:{acct & 1}:{acct >> 1}"]


def to_steam64(ident):
    """SteamID64 from a SteamID64, [U:1:n] or STEAM_x:y:z (None if it is none of those)."""
    ident = str(ident or "").strip()
    if STEAM_ID.fullmatch(ident):
        return ident
    m = re.fullmatch(r"\[U:1:(\d+)\]", ident)
    if m:
        return str(STEAM64_BASE + int(m.group(1)))
    m = re.fullmatch(r"STEAM_[0-5]:([01]):(\d+)", ident)
    if m:
        return str(STEAM64_BASE + int(m.group(2)) * 2 + int(m.group(1)))
    return None


def _listed(forms, text):
    return any(re.search(r"(?<![\d:])" + re.escape(f) + r"(?!\d)", text) for f in forms)


def read_text(path):
    """(text, encoding) - SCUM writes some of its files as UTF-16, with or without a BOM."""
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    if len(raw) >= 2 and raw[1] == 0:
        return raw.decode("utf-16-le", "replace"), "utf-16-le"
    return raw.decode("utf-8-sig", "replace"), "utf-8"


def _iso(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp() if text else None
    except ValueError:
        return None


def _edit_lines(path, steam_id, new_line=None):
    """Drop the lines of a one-player-per-line file that are about steam_id ("<id>...", or "<cmd> <id> ..."
    in Rust's users.cfg); add new_line. Keeps the file's encoding; CRLF like the games write them."""
    path = Path(path)
    text, enc = read_text(path) if path.exists() else ("", "utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    keep = [ln for ln in lines if not ((m := re.match(r"\s*(?:[a-z]+\s+)?(7656\d{13})", ln)) and m.group(1) == steam_id)]
    if new_line:
        keep.append(new_line)
    if keep != lines:
        path.write_text("".join(ln + "\r\n" for ln in keep), encoding=enc, newline="")


# --------------------------------------------------------------------------
# One adapter per server type
# --------------------------------------------------------------------------

class Game:
    can_kick = False
    can_ban = False
    needs_online = True        # bans go through the running server (otherwise: a file, any time)
    reapply = False            # the game forgets bans when it restarts
    ban_note = ""
    roles = ()                 # which of ROLES can be given from here
    roles_offline = False      # role changes must wait until the server is stopped
    role_note = ""

    def __init__(self, server):
        self.s = server

    def online(self):
        return []

    def history(self):
        """steam id -> {"name", "first", "last", "played"} from the game's own records (if it keeps any)."""
        return {}

    def staff(self):
        """[{"steam_id", "role", "name"?, "detail"?}] - who has a role now."""
        return []


class Scum(Game):
    can_ban = True
    needs_online = False
    ban_note = ("SCUM has no remote console: a ban goes into BannedUsers.ini, which SCUM reads when it starts - "
                "the player can stay until the next restart, and nobody can be kicked from here.")
    roles = ROLES
    role_note = ("AdminUsers.ini, read when SCUM starts: an admin gets every admin command plus god mode and "
                 "shutdown ([SetGodMode, ShutdownServer]), a moderator the standard admin commands.")
    ADMIN = "[SetGodMode, ShutdownServer]"

    def _dir(self):
        return Path(self.s.cfg["install_dir"]) / r"SCUM\Saved"

    def online(self):
        st = self.s.status
        if not st.get("running") or self.s.perf.get("players") == 0:
            return []
        pattern = (self.s.cfg.get("logs") or {}).get("login") or str(self._dir() / r"SaveFiles\Logs\login_*.log")
        files = glob.glob(pattern)
        if not files:
            return []
        path = max(files, key=lambda p: Path(p).stat().st_mtime)
        stamp = re.search(r"login_(\d{14})", Path(path).name)       # UTC, when this SCUM run started
        started = time.time() - (st.get("uptime") or 0)
        if stamp and calendar.timegm(time.strptime(stamp.group(1), "%Y%m%d%H%M%S")) < started - 120:
            return []                                                 # the last run's log: nobody yet
        on = {}
        for line in read_text(path)[0].splitlines():
            m = SCUM_LOGIN.match(line.strip())
            if not m:
                continue
            sid = m.group(2)
            if m.group(5) == "in":
                on[sid] = {"name": m.group(3), "steam_id": sid,
                           "since": calendar.timegm(time.strptime(m.group(1), "%Y.%m.%d-%H.%M.%S"))}
            else:
                on.pop(sid, None)
        return sorted(on.values(), key=lambda p: p["since"])

    def history(self):
        db = self._dir() / r"SaveFiles\SCUM.db"
        con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            rows = con.execute("SELECT user_id, name, creation_time, last_login_time, last_logout_time, play_time "
                               "FROM user_profile").fetchall()
        finally:
            con.close()
        out = {}
        for uid, name, created, login, logout, played in rows:
            if STEAM_ID.fullmatch(str(uid)):
                seen = [t for t in (_iso(login), _iso(logout)) if t]
                out[str(uid)] = {"name": name, "first": _iso(created), "last": max(seen) if seen else None,
                                 "played": int(played) if played is not None else None}
        return out

    # bans
    def path(self):
        return self._dir() / r"Config\WindowsServer\BannedUsers.ini"

    def banned(self):
        p = self.path()
        text = read_text(p)[0] if p.exists() else ""
        return [m.group(0) for line in text.splitlines() if (m := STEAM_ID.match(line.strip()))]

    def ban(self, steam_id, name, reason):
        _edit_lines(self.path(), steam_id, steam_id)

    def unban(self, steam_id):
        _edit_lines(self.path(), steam_id)

    # roles
    def _admins(self):
        return self._dir() / r"Config\WindowsServer\AdminUsers.ini"

    def staff(self):
        p = self._admins()
        out = []
        for line in (read_text(p)[0] if p.exists() else "").splitlines():
            m = re.match(r"\s*(7656\d{13})\s*(?:\[(.*?)\])?", line)
            if m:
                perms = [x.strip() for x in (m.group(2) or "").split(",") if x.strip()]
                detail = ", ".join(perms)
                if "RestartServer" in perms:
                    detail += " - RestartServer is called ShutdownServer since SCUM 1.0.2: give the role again to fix it"
                out.append({"steam_id": m.group(1), "role": "admin" if "SetGodMode" in perms else "moderator",
                            "detail": detail})
        return out

    def set_role(self, steam_id, name, role):
        _edit_lines(self._admins(), steam_id, steam_id + self.ADMIN if role == "admin" else steam_id if role else None)
        return "takes effect when SCUM next starts"


class SpaceEngineers(Game):
    can_kick = can_ban = True
    ban_note = "Banned players are kicked right away."
    roles = ("admin",)
    roles_offline = True       # it rewrites its cfg while running (every ban saves it): edit only while stopped
    role_note = ("The Administrators list in SpaceEngineers-Dedicated.cfg. The server rewrites that file while it runs, "
                 "so a change waits until it stops or restarts (ServerDeck writes it just before the next start). "
                 "Moderators and space masters are promoted in-game (admin screen, Alt+F10).")

    def _call(self, method, path):
        api = self.s.cfg["remote_api"]
        return se_api.call(api["port"], self.s.mgr.secret(api["key"]), method, path, timeout=10)

    def _settle(self, steam_id, banned):
        """The Remote API queues bans for the game thread: wait until the list shows the change."""
        for _ in range(20):
            if (steam_id in self._banned()) == banned:
                return
            time.sleep(0.5)
        raise RuntimeError(f"Space Engineers did not {'list' if banned else 'drop'} the ban within 10 s")

    def online(self):
        d = self._call("GET", "/vrageremote/v1/session/players")
        return [{"name": p.get("DisplayName") or "?", "steam_id": str(p.get("SteamID") or ""), "ping": p.get("Ping"),
                 "info": p.get("FactionTag") or p.get("FactionName") or None}
                for p in (d.get("data") or {}).get("Players") or []]

    def _banned(self):
        d = self._call("GET", "/vrageremote/v1/admin/bannedPlayers")
        return {str(p.get("SteamId") or p.get("SteamID")) for p in (d.get("data") or {}).get("BannedPlayers") or []}

    def ban(self, steam_id, name, reason):
        self._call("POST", f"/vrageremote/v1/admin/bannedPlayers/{steam_id}")
        self._settle(steam_id, True)

    def unban(self, steam_id):
        self._call("DELETE", f"/vrageremote/v1/admin/bannedPlayers/{steam_id}")
        self._settle(steam_id, False)

    def kick(self, steam_id, reason):
        self._call("POST", f"/vrageremote/v1/admin/kickedPlayers/{steam_id}")

    def _cfg(self):
        for p in self.s.cfg.get("config_files", []):
            if p.lower().endswith("spaceengineers-dedicated.cfg"):
                return Path(p)
        raise RuntimeError("SpaceEngineers-Dedicated.cfg is not in this server's config_files")

    def staff(self):
        m = re.search(r"<Administrators>(.*?)</Administrators>", self._cfg().read_text(encoding="utf-8-sig"), re.S)
        return [{"steam_id": x, "role": "admin"} for x in re.findall(r"<unsignedLong>\s*(\d+)\s*</unsignedLong>", m.group(1))] if m else []

    def set_role(self, steam_id, name, role):
        path = self._cfg()
        raw = path.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        nl = "\r\n" if "\r\n" in text else "\n"
        m = re.search(r"([ \t]*)<Administrators\s*/>|([ \t]*)<Administrators>(.*?)</Administrators>", text, re.S)
        if not m:
            raise RuntimeError("no <Administrators> section in SpaceEngineers-Dedicated.cfg")
        indent = m.group(1) if m.group(1) is not None else m.group(2)
        ids = re.findall(r"<unsignedLong>\s*(\d+)\s*</unsignedLong>", m.group(3) or "")
        ids = [x for x in ids if x != steam_id] + ([steam_id] if role == "admin" else [])
        inner = re.search(r"\n([ \t]*)<unsignedLong>", m.group(3) or "")
        inner = inner.group(1) if inner else indent + "  "
        block = (f"{indent}<Administrators>{nl}" + "".join(f"{inner}<unsignedLong>{x}</unsignedLong>{nl}" for x in ids)
                 + f"{indent}</Administrators>") if ids else f"{indent}<Administrators />"
        path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + (text[:m.start()] + block + text[m.end():]).encode("utf-8"))
        return "takes effect when Space Engineers next starts"


class CS2(Game):
    can_kick = can_ban = True
    reapply = True
    ban_note = ("Banned players are kicked right away; CS2 forgets bans when it restarts, so ServerDeck "
                "bans again after every start.")
    roles = ROLES
    role_note = ("CounterStrikeSharp's admins.json, loaded when CS2 starts: an admin gets @css/root, a moderator the "
                 "kick/ban/chat flags that admin plugins check (plugin flags: the server's role_flags setting).")
    FLAGS = {"admin": ["@css/root"],
             "moderator": ["@css/generic", "@css/kick", "@css/ban", "@css/unban", "@css/chat", "@css/slay"]}

    def online(self):
        out = self.s.rcon("status_json", timeout=5)
        d = json.loads(out[out.index("{"):])
        clients = (d.get("server") or {}).get("clients") or d.get("clients") or []
        return [{"name": c.get("name") or "?", "steam_id": str(c.get("steamid64")), "ping": c.get("ping")}
                for c in clients if not c.get("bot") and STEAM_ID.fullmatch(str(c.get("steamid64") or ""))]

    def _listed(self, forms):
        return _listed(forms, self.s.rcon("listid", timeout=5))

    def ban(self, steam_id, name, reason):
        forms = steam_forms(steam_id)
        if not self._listed(forms):
            for f in forms:                                  # whichever form this build's banid takes
                self.s.rcon(f"banid 0 {f}", timeout=5)
                if self._listed(forms):
                    break
            else:
                raise RuntimeError("CS2 did not take the ban (banid) - is sv_banid_enabled off?")
        self.s.rcon("writeid", timeout=5)
        self.kick(steam_id, reason or "Banned")

    def unban(self, steam_id):
        forms = steam_forms(steam_id)
        for f in forms:
            if not self._listed(forms):
                break
            self.s.rcon(f"removeid {f}", timeout=5)
        if self._listed(forms):
            raise RuntimeError("CS2 still lists the ban after removeid")
        self.s.rcon("writeid", timeout=5)

    def kick(self, steam_id, reason):
        self.s.rcon(f'kickid {steam_id} "{clean(reason) or "Kicked by an admin"}"', timeout=5)

    def _admins(self):
        return Path(self.s.cfg["install_dir"]) / r"game\csgo\addons\counterstrikesharp\configs\admins.json"

    def _load_admins(self):
        p = self._admins()
        return json.loads(p.read_text(encoding="utf-8-sig") or "{}") if p.exists() else {}

    def staff(self):
        out = []
        for label, a in self._load_admins().items():
            sid = to_steam64((a or {}).get("identity"))
            flags = (a or {}).get("flags") or []
            if sid:
                admin = any(f == "@css/root" or f.endswith(("/root", "/admin")) for f in flags)
                out.append({"steam_id": sid, "name": label, "role": "admin" if admin else "moderator",
                            "detail": ", ".join(flags)})
        return out

    def set_role(self, steam_id, name, role):
        admins = {k: v for k, v in self._load_admins().items() if to_steam64((v or {}).get("identity")) != steam_id}
        if role:
            label = clean(name, 40) or steam_id
            if label in admins:
                label = f"{label} ({steam_id})"
            flags = (self.s.cfg.get("role_flags") or {}).get(role) or self.FLAGS[role]
            admins[label] = {"identity": steam_id, "flags": flags}
        self._admins().write_text(json.dumps(admins, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return "takes effect when CS2 next starts"


class Rust(Game):
    can_kick = can_ban = True
    ban_note = "Banned players are kicked right away."
    roles = ROLES
    role_note = ("Rust's owner (admin) and moderator lists: given right away while Rust runs (they may need to "
                 "reconnect), written to users.cfg while it is stopped.")

    def online(self):
        now = time.time()
        return [{"name": p.get("DisplayName") or "?", "steam_id": str(p.get("SteamID") or ""), "ping": p.get("Ping"),
                 "since": now - float(p.get("ConnectedSeconds") or 0)}
                for p in json.loads(self.s.rcon("playerlist", timeout=5) or "[]")]

    def ban(self, steam_id, name, reason):
        self.s.rcon(f'banid {steam_id} "{clean(name) or "unnamed"}" "{clean(reason) or "no reason"}"', timeout=5)
        if steam_id not in self.s.rcon("banlistex", timeout=5):
            raise RuntimeError("Rust did not list the ban after banid")
        self.kick(steam_id, reason or "Banned")
        self.s.rcon("server.writecfg", timeout=10)

    def unban(self, steam_id):
        self.s.rcon(f"unban {steam_id}", timeout=5)
        if steam_id in self.s.rcon("banlistex", timeout=5):
            raise RuntimeError("Rust still lists the ban after unban")
        self.s.rcon("server.writecfg", timeout=10)

    def kick(self, steam_id, reason):
        self.s.rcon(f'kick {steam_id} "{clean(reason) or "Kicked by an admin"}"', timeout=5)

    def _users_cfg(self):
        args = self.s.cfg.get("args", [])
        ident = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "+server.identity"), "my_server_identity")
        return Path(self.s.cfg["install_dir"]) / "server" / ident / "cfg" / "users.cfg"

    def staff(self):
        p = self._users_cfg()
        out = []
        for line in (read_text(p)[0] if p.exists() else "").splitlines():
            m = re.match(r'\s*(ownerid|moderatorid)\s+(7656\d{13})\s*(?:"(.*?)")?', line)
            if m:
                out.append({"steam_id": m.group(2), "role": "admin" if m.group(1) == "ownerid" else "moderator",
                            "name": m.group(3) or None})
        return out

    def set_role(self, steam_id, name, role):
        nm = clean(name, 40) or "unnamed"
        if self.s.status.get("online"):
            cur = {x["steam_id"]: x["role"] for x in self.staff()}.get(steam_id)
            if cur == "admin" and role != "admin":
                self.s.rcon(f"removeowner {steam_id}", timeout=5)
            if cur == "moderator" and role != "moderator":
                self.s.rcon(f"removemoderator {steam_id}", timeout=5)
            if role == "admin":
                self.s.rcon(f'ownerid {steam_id} "{nm}" "ServerDeck"', timeout=5)
            elif role == "moderator":
                self.s.rcon(f'moderatorid {steam_id} "{nm}" "ServerDeck"', timeout=5)
            self.s.rcon("server.writecfg", timeout=10)
            return "given right away - they may need to reconnect"
        line = {"admin": f'ownerid {steam_id} "{nm}" "ServerDeck"', "moderator": f'moderatorid {steam_id} "{nm}" "ServerDeck"'}.get(role)
        _edit_lines(self._users_cfg(), steam_id, line)
        return "Rust is stopped - written to users.cfg, takes effect when it starts"


class Palworld(Game):
    can_kick = can_ban = True
    ban_note = "Banned players are kicked right away."
    role_note = "Palworld has no per-player roles: an admin types /AdminPassword <password> in the in-game chat."

    def _call(self, method, path, body=None):
        api = self.s.cfg["rest_api"]
        return palworld.call(api["port"], self.s.mgr.secret(api["password"]), method, path, body, timeout=10)

    def online(self):
        out = []
        for p in self._call("GET", "players").get("players") or []:
            uid = str(p.get("userId") or "")
            out.append({"name": p.get("name") or "?", "steam_id": uid[6:] if uid.startswith("steam_") else None,
                        "ping": round(p["ping"]) if isinstance(p.get("ping"), (int, float)) else None,
                        "info": f"level {p['level']}" if p.get("level") else None})
        return out

    def ban(self, steam_id, name, reason):
        self._call("POST", "ban", {"userid": f"steam_{steam_id}", "message": reason or "You are banned from this server."})

    def unban(self, steam_id):
        self._call("POST", "unban", {"userid": f"steam_{steam_id}"})

    def kick(self, steam_id, reason):
        self._call("POST", "kick", {"userid": f"steam_{steam_id}", "message": reason or "Kicked by an admin."})


class Query(Game):
    ban_note = "This game can't ban from here: an admin bans in-game (Enshrouded's list holds hashed accounts)."
    role_note = "Roles come with the server's passwords (Enshrouded: the Admin, Friend and Guest group passwords)."

    def online(self):
        q = self.s.cfg["query"]
        now = time.time()
        return [{"name": p["name"], "steam_id": None, "since": now - p["duration"]}
                for p in a2s.players(q.get("host", "127.0.0.1"), q["port"]) if p["name"]]


GAMES = {"scum_log": Scum, "se_remote": SpaceEngineers, "cs2_rcon": CS2, "rust_rcon": Rust,
         "palworld_rest": Palworld, "a2s": Query}


# --------------------------------------------------------------------------
# A sidecar's admin API (device bans)
# --------------------------------------------------------------------------

class SidecarAdmin:
    """The sidecar's admin API on 127.0.0.1. Its key stays where it is (resolved per call, never stored)."""

    def __init__(self, server):
        self.s = server
        sc = server.cfg["sidecar"]
        self.port = sc.get("port")
        self.label = sc.get("label", "Sidecar")
        self.config = Path(sc["dir"]) / sc.get("config", "config.toml")
        self.key_ref = sc.get("admin_key")

    @staticmethod
    def offered(server):
        """Does this server's sidecar have an admin API (an admin_key setting or admin_api_key in its config)?"""
        sc = server.cfg.get("sidecar")
        if not sc:
            return False
        if sc.get("admin_key"):
            return True
        try:
            with open(Path(sc["dir"]) / sc.get("config", "config.toml"), "rb") as f:
                return bool(tomllib.load(f).get("admin_api_key"))
        except (OSError, tomllib.TOMLDecodeError):
            return False

    def running(self):
        return bool(self.s.sidecar and self.s.sidecar.alive())

    def _key(self):
        if self.key_ref:
            return self.s.mgr.secret(self.key_ref)
        with open(self.config, "rb") as f:
            return str(tomllib.load(f).get("admin_api_key") or "")

    def call(self, method, path, body=None, timeout=20):
        key = self._key()
        if not key:
            raise RuntimeError("the sidecar has no admin key")
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Admin-Key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                j = json.loads(e.read() or b"{}")
            except ValueError:
                j = {}
            raise RuntimeError(j.get("message") or j.get("error") or f"HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(f"the sidecar is not reachable ({getattr(e, 'reason', e)})") from None

    def verified(self):
        """Steam IDs whose device the sidecar knows right now (verified and not banned)."""
        return sorted((self.call("GET", "/players", timeout=5).get("players") or {}).keys())

    def ban(self, steam_id, ban_type, scope, reason):
        return self.call("POST", "/ban", {"steam_id": steam_id, "ban_type": ban_type, "scope": scope,
                                          "reason_code": "manual_ban", "details": reason or None})

    def unban(self, ban_id):
        return self.call("POST", "/unban", {"ban_id": ban_id})


# --------------------------------------------------------------------------
# Players: tracking, bans, roles (players.json)
# --------------------------------------------------------------------------

class Players:
    def __init__(self, mgr, path):
        self.mgr = mgr
        self.path = Path(path)
        self.lock = threading.RLock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        for k in ("seen", "since", "bans", "roles"):      # roles: changes waiting for the server to stop
            self.data.setdefault(k, {})
        self.cache = {}            # server id -> {"at", "online", "error"}
        self.last_poll = {}        # server id -> (time, keys online then)
        self.saved_at = 0

    def _save(self):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            tmp.replace(self.path)
            self.saved_at = time.time()

    def game(self, s):
        kind = source(s.cfg)
        return GAMES[kind](s) if kind in GAMES else None

    # ---- who is online + how long they have been around ----
    def poll(self, s, force=False):
        g = self.game(s)
        old = self.cache.get(s.id) or {}
        if not g or not s.status.get("online"):
            self.cache[s.id] = {"at": time.time(), "online": [], "error": None}
        elif force or time.time() - old.get("at", 0) >= POLL_SECONDS:
            try:
                online = g.online()
                self.cache[s.id] = {"at": time.time(), "online": online, "error": None}
                self._track(s, online)
            except Exception as e:
                self.cache[s.id] = {"at": time.time(), "online": old.get("online", []), "error": str(e)}
                log.info("players %s: %s", s.id, e)
        return self.cache[s.id]

    def _track(self, s, online):
        now = time.time()
        with self.lock:
            seen = self.data["seen"].setdefault(s.id, {})
            new = s.id not in self.data["since"]
            self.data["since"].setdefault(s.id, now)
            prev_t, prev_keys = self.last_poll.get(s.id, (0, set()))
            keys = set()
            for p in online:
                k = p.get("steam_id") or "name:" + p["name"]
                keys.add(k)
                e = seen.get(k)
                if e is None:
                    e = seen[k] = {"name": p["name"], "first": min(now, p.get("since") or now), "played": 0}
                    new = True
                elif k in prev_keys and 0 < now - prev_t <= 3 * POLL_SECONDS:
                    e["played"] = round(e.get("played", 0) + now - prev_t)
                e.update(name=p["name"], last=now)
            self.last_poll[s.id] = (now, keys)
            if new or (keys and now - self.saved_at >= SAVE_SECONDS):
                self._save()

    def names(self, s):
        return [p["name"] for p in (self.cache.get(s.id) or {}).get("online", [])]

    def known(self, s, g=None):
        """Everyone seen on this server: ServerDeck's own notes merged with the game's history."""
        g = g or self.game(s)
        try:
            hist = g.history() if g else {}
        except Exception as e:
            log.info("player history %s: %s", s.id, e)
            hist = {}
        with self.lock:
            seen = json.loads(json.dumps(self.data["seen"].get(s.id, {})))
        out = {}
        for k in set(seen) | set(hist):
            t, h = seen.get(k, {}), hist.get(k, {})
            firsts = [x for x in (t.get("first"), h.get("first")) if x]
            lasts = [x for x in (t.get("last"), h.get("last")) if x]
            out[k] = {"key": k, "steam_id": k if STEAM_ID.fullmatch(k) else None, "name": t.get("name") or h.get("name") or "?",
                      "first": min(firsts) if firsts else None, "last": max(lasts) if lasts else None,
                      "played": h["played"] if h.get("played") is not None else t.get("played"),
                      "from_game": k in hist}
        return out

    def _name(self, steam_id, fallback="?"):
        for c in list(self.cache.values()):
            for p in c.get("online", []):
                if p.get("steam_id") == steam_id:
                    return p["name"]
        with self.lock:
            for seen in self.data["seen"].values():
                if steam_id in seen:
                    return seen[steam_id].get("name") or fallback
            return (self.data["bans"].get(steam_id) or {}).get("name") or fallback

    # ---- bans ----
    @staticmethod
    def _who(e):
        return f"{e.get('name') or '?'} ({e['steam_id']})"

    def _apply(self, s, g, e):
        """Try one server's pending ban or unban now. Returns (ok, message); updates e["servers"]."""
        st = e["servers"][s.id]
        lift = st["want"] == "lift"
        if g.needs_online and not s.status.get("online"):
            return True, f"{s.name} is offline - {'lifted' if lift else 'banned'} as soon as it is back online"
        st["tried"] = time.time()
        try:
            if lift:
                g.unban(e["steam_id"])
            else:
                g.ban(e["steam_id"], e.get("name"), e.get("reason"))
        except Exception as ex:
            if not st.get("error"):
                self.mgr.events.add("warn", s.id, f"could not {'lift the ban on' if lift else 'ban'} {self._who(e)}: "
                                                  f"{ex} - trying again every {RETRY_SECONDS // 60} min")
            st["error"] = str(ex)
            return False, f"failed: {ex} (ServerDeck tries again every {RETRY_SECONDS // 60} min)"
        if lift:
            del e["servers"][s.id]
            self.mgr.events.add("info", s.id, f"ban lifted: {self._who(e)}")
            return True, "ban lifted" + (" - SCUM lets them in after its next restart" if isinstance(g, Scum) else "")
        st.update(done=True, error=None, pid=s.status.get("pid"), at=time.time())
        why = f": {e['reason']}" if e.get("reason") else ""
        if isinstance(g, Scum):
            self.mgr.events.add("warn", s.id, f"banned {self._who(e)}{why} - in BannedUsers.ini, takes effect at the next restart")
            nr = s.next_restart()
            return True, ("added to BannedUsers.ini - takes effect when SCUM next starts"
                          + (f" ({nr:%a %H:%M}, or restart it now)" if nr and s.status.get("running") else ""))
        self.mgr.events.add("warn", s.id, f"banned {self._who(e)}{why}")
        return True, "banned" + (" and kicked" if g.can_kick else "")

    def ban(self, steam_id, name, reason, server_ids, device_ids=(), ban_type="cheat", scope="game"):
        results = []
        with self.lock:
            bans = self.data["bans"]
            e = bans.setdefault(steam_id, {"steam_id": steam_id, "at": time.time(), "servers": {}, "devices": {}})
            e["name"] = name or e.get("name") or self._name(steam_id, "")
            e["reason"] = reason or e.get("reason") or ""
            for sid in server_ids:
                s = self.mgr.by_id.get(sid)
                g = s and self.game(s)
                if not g or not g.can_ban:
                    results.append({"target": s.name if s else sid, "ok": False, "msg": "can't ban from ServerDeck"})
                    continue
                e["servers"][sid] = {"want": "ban", "done": False, "at": time.time()}
                ok, msg = self._apply(s, g, e)
                results.append({"target": s.name, "ok": ok, "msg": msg})
            for sid in device_ids:
                s = self.mgr.by_id.get(sid)
                if not s or not SidecarAdmin.offered(s):
                    results.append({"target": f"device ban ({sid})", "ok": False, "msg": "no sidecar with an admin API"})
                    continue
                sa = SidecarAdmin(s)
                label = f"device ban ({sa.label})"
                if not sa.running():
                    results.append({"target": label, "ok": False, "msg": f"the sidecar only runs while {s.name} is online"})
                    continue
                try:
                    r = sa.ban(steam_id, ban_type, scope, e["reason"])
                except Exception as ex:
                    results.append({"target": label, "ok": False, "msg": str(ex)})
                    continue
                e["devices"][sid] = {"ban_id": r.get("ban_id"), "ban_type": ban_type, "scope": scope, "at": time.time()}
                self.mgr.events.add("warn", sid, f"device ban ({ban_type}, {scope}) through the {sa.label}: {self._who(e)}"
                                                 f"{' - ' + e['reason'] if e['reason'] else ''}")
                kicked = len(r.get("removed_steam_ids") or [])
                results.append({"target": label, "ok": True, "msg": "device banned" + (f", {kicked} account(s) on it kicked" if kicked else "")})
            if not e["servers"] and not e["devices"]:
                bans.pop(steam_id, None)
            self._save()
        return results

    def unban(self, steam_id):
        results = []
        with self.lock:
            bans = self.data["bans"]
            e = bans.get(steam_id)
            if not e:
                return [{"target": "ServerDeck", "ok": False, "msg": "no ban for that Steam ID"}]
            for sid, st in list(e["servers"].items()):
                s = self.mgr.by_id.get(sid)
                g = s and self.game(s)
                if not g or (st["want"] == "ban" and not st.get("done")):
                    del e["servers"][sid]               # never applied (or the server is gone): just drop it
                    results.append({"target": s.name if s else sid, "ok": True, "msg": "dropped (it had not been applied yet)"})
                    continue
                st.update(want="lift", done=False, error=None, tried=0)
                ok, msg = self._apply(s, g, e)
                results.append({"target": s.name, "ok": ok, "msg": msg})
            for sid, db in list(e["devices"].items()):
                s = self.mgr.by_id.get(sid)
                label = f"device ban ({SidecarAdmin(s).label if s and s.cfg.get('sidecar') else sid})"
                try:
                    if not s or not SidecarAdmin(s).running():
                        raise RuntimeError(f"the sidecar only runs while {s.name if s else sid} is online - try again then")
                    SidecarAdmin(s).unban(db["ban_id"])
                except Exception as ex:
                    results.append({"target": label, "ok": False, "msg": str(ex)})
                    continue
                del e["devices"][sid]
                self.mgr.events.add("info", sid, f"device ban lifted: {self._who(e)}")
                results.append({"target": label, "ok": True, "msg": "device ban revoked"})
            if not e["servers"] and not e["devices"]:
                del bans[steam_id]
            self._save()
        return results

    def sync(self, s):
        """Supervisor: role changes waiting for this server to stop, bans/unbans waiting for it to be
        online, and re-bans where the game forgot them."""
        g = self.game(s)
        if not g:
            return
        if not s.status.get("running") and self.data["roles"].get(s.id):
            self._apply_roles(s, g)
        if not g.can_ban or (g.needs_online and not s.status.get("online")):
            return
        pid = s.status.get("pid")
        again = 0
        with self.lock:
            bans = self.data["bans"]
            changed = False
            for steam_id, e in list(bans.items()):
                st = e["servers"].get(s.id)
                if not st:
                    continue
                stale = g.reapply and st["want"] == "ban" and st.get("done") and st.get("pid") != pid
                if (st.get("done") and not stale) or time.time() - st.get("tried", 0) < RETRY_SECONDS:
                    continue
                changed = True
                if stale:
                    st["tried"] = time.time()
                    try:
                        g.ban(steam_id, e.get("name"), e.get("reason"))
                        st.update(error=None, pid=pid)
                        again += 1
                    except Exception as ex:
                        st.update(done=False, error=str(ex))
                        self.mgr.events.add("warn", s.id, f"could not re-apply the ban on {self._who(e)}: {ex}")
                else:
                    self._apply(s, g, e)
                if not e["servers"] and not e["devices"]:
                    del bans[steam_id]
            if changed:
                self._save()
        if again:
            self.mgr.events.add("info", s.id, f"re-applied {again} ban(s) after the restart")

    def before_start(self, s):
        """Just before a server starts: role changes that waited for it to stop, and bans kept in a file
        (SCUM) written again in case the game rewrote it."""
        g = self.game(s)
        if not g:
            return
        self._apply_roles(s, g)
        if not g.can_ban or g.needs_online:
            return
        with self.lock:
            bans = json.loads(json.dumps(self.data["bans"]))
        for steam_id, e in bans.items():
            st = e["servers"].get(s.id)
            if st and st["want"] == "ban":
                try:
                    g.ban(steam_id, e.get("name"), e.get("reason"))
                except Exception as ex:
                    self.mgr.events.add("warn", s.id, f"could not write the ban on {self._who(e)}: {ex}")

    # ---- kick, roles ----
    def kick(self, s, steam_id, reason):
        g = self.game(s)
        if not g or not g.can_kick:
            return 400, {"error": f"{s.name} can't kick players from here"}
        if not s.status.get("online"):
            return 409, {"error": f"{s.name} is not online"}
        try:
            g.kick(steam_id, reason)
        except Exception as e:
            return 502, {"error": f"kick failed: {e}"}
        self.mgr.events.add("info", s.id, f"kicked {self._name(steam_id)} ({steam_id})" + (f": {reason}" if reason else ""))
        self.poll(s, force=True)
        return 200, {"ok": True}

    def set_role(self, s, steam_id, name, role):
        g = self.game(s)
        if not g or not g.roles or (role and role not in g.roles):
            return 400, {"error": f"{s.name} can't give that role from here"}
        name = name or self._name(steam_id, "")
        if g.roles_offline and s.status.get("running"):
            with self.lock:
                self.data["roles"].setdefault(s.id, {})[steam_id] = {"role": role, "name": name, "at": time.time()}
                self._save()
            note = f"{s.name} rewrites that file while it runs, so it is written when the server next stops or restarts"
            self.mgr.events.add("info", s.id, f"role: {name or '?'} ({steam_id}) will be {role or 'a regular player'} - {note}")
            return 200, {"ok": True, "note": note, "pending": True}
        try:
            note = g.set_role(steam_id, name, role)
        except Exception as e:
            return 502, {"error": f"could not change the role: {e}"}
        with self.lock:
            if self.data["roles"].get(s.id, {}).pop(steam_id, None) is not None:
                self._save()
        self.mgr.events.add("info", s.id, f"role: {name or '?'} ({steam_id}) is now {role or 'a regular player'} - {note}")
        return 200, {"ok": True, "note": note}

    def _apply_roles(self, s, g):
        """Write role changes that had to wait for the server to stop (Space Engineers)."""
        with self.lock:
            pending = self.data["roles"].get(s.id)
            if not pending:
                return
            for steam_id, r in list(pending.items()):
                try:
                    g.set_role(steam_id, r.get("name"), r.get("role"))
                except Exception as e:
                    self.mgr.events.add("warn", s.id, f"could not write the role of {r.get('name') or '?'} ({steam_id}): {e}")
                    continue
                del pending[steam_id]
                self.mgr.events.add("info", s.id, f"role: {r.get('name') or '?'} ({steam_id}) is now {r.get('role') or 'a regular player'}")
            if not pending:
                del self.data["roles"][s.id]
            self._save()

    # ---- API ----
    def api_players(self, s):
        g = self.game(s)
        c = self.poll(s, force=True)
        known = self.known(s, g)
        try:
            staff = g.staff() if g else []
            staff_error = None
        except Exception as e:
            staff, staff_error = [], str(e)
        roles = {x["steam_id"]: x["role"] for x in staff}
        with self.lock:
            since = self.data["since"].get(s.id)
            pending = {k: v["role"] for k, v in (self.data["roles"].get(s.id) or {}).items()}
            pending_names = {k: v.get("name") for k, v in (self.data["roles"].get(s.id) or {}).items()}
        for sid, role in pending.items():                     # waiting for the server to stop
            if sid not in roles:
                staff.append({"steam_id": sid, "role": None, "name": pending_names.get(sid)})
        for x in staff:
            x["name"] = x.get("name") or (known.get(x["steam_id"]) or {}).get("name") or self._name(x["steam_id"], "")
        online = []
        for p in c["online"]:
            k = known.get(p.get("steam_id") or "name:" + p["name"]) or {}
            online.append(dict(p, first=k.get("first"), played=k.get("played"), role=roles.get(p.get("steam_id"))))
        for k in known.values():
            k["role"] = roles.get(k["steam_id"])
        out = {"source": source(s.cfg), "online": online, "error": c["error"], "server_online": bool(s.status.get("online")),
               "known": sorted(known.values(), key=lambda k: -(k["last"] or 0)), "tracked_since": since,
               "history_from_game": any(k["from_game"] for k in known.values()),
               "can_ban": bool(g and g.can_ban), "can_kick": bool(g and g.can_kick), "ban_note": g.ban_note if g else "",
               "roles": list(g.roles) if g else [], "role_note": g.role_note if g else "", "pending_roles": pending,
               "staff": staff, "staff_error": staff_error, "device": self._device_status(s)}
        return out

    def _device_status(self, s):
        if not SidecarAdmin.offered(s):
            return None
        sa = SidecarAdmin(s)
        d = {"id": s.id, "name": s.name, "label": sa.label, "running": sa.running(), "verified": [], "error": None}
        if d["running"]:
            try:
                d["verified"] = sa.verified()
            except Exception as e:
                d["error"] = str(e)
        return d

    def api_bans(self):
        targets = []
        for s in self.mgr.servers:
            g = self.game(s)
            if g and g.can_ban:
                nr = s.next_restart() if isinstance(g, Scum) else None
                targets.append({"id": s.id, "name": s.name, "online": bool(s.status.get("online")),
                                "needs_online": g.needs_online, "scum": isinstance(g, Scum),
                                "next_restart": nr.timestamp() if nr else None})
        devices = [d for d in (self._device_status(s) for s in self.mgr.servers) if d]
        with self.lock:
            bans = sorted(json.loads(json.dumps(self.data["bans"])).values(), key=lambda e: -e.get("at", 0))
        for e in bans:
            for part in ("servers", "devices"):
                for sid, st in e.get(part, {}).items():
                    st["server"] = self.mgr.by_id[sid].name if sid in self.mgr.by_id else sid
        return {"bans": bans, "targets": targets, "devices": devices}

    def api(self, body):
        act = body.get("action")
        steam_id = str(body.get("steam_id", "")).strip()
        if not STEAM_ID.fullmatch(steam_id):
            return 400, {"error": "that is not a SteamID64 (17 digits, starting 7656)"}
        if act == "ban":
            servers = [str(x) for x in body.get("servers") or []]
            devices = [str(x) for x in body.get("devices") or []]
            if not servers and not devices:
                return 400, {"error": "choose at least one server or device ban"}
            ban_type = body.get("ban_type") if body.get("ban_type") in ("cheat", "social") else "cheat"
            scope = body.get("scope") if body.get("scope") in ("game", "publisher") else "game"
            name = str(body.get("name") or "").strip()[:64]
            reason = str(body.get("reason") or "").strip()[:200]
            return 200, {"results": self.ban(steam_id, name, reason, servers, devices, ban_type, scope)}
        if act == "unban":
            return 200, {"results": self.unban(steam_id)}
        return 400, {"error": "unknown action"}
