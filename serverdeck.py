"""ServerDeck - runs the game servers on a Windows PC from one web page.

Servers are listed in config.json (add them from templates in the UI: SCUM, Space Engineers,
Counter-Strike 2, Rust). ServerDeck keeps the ones that should be online running, installs game
updates with SteamCMD and re-applies mods afterwards (hooks.py: Metamod/CounterStrikeSharp,
Oxide), restarts servers on a schedule with player warnings, backs up configs and worlds and
restores them (backups.py), runs optional "sidecar" programs only while their server is online,
watches performance, and posts to Discord (community.py). UI: http://127.0.0.1:8787.

Run it elevated (install.ps1 sets up a scheduled task that does): Windows services and
elevated game servers can only be controlled with admin rights.
"""
import collections
import ctypes
import glob
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tomllib
import traceback
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import a2s  # noqa: E402
import backups  # noqa: E402
import community  # noqa: E402
import hooks  # noqa: E402
import setupwiz  # noqa: E402
import rcon as rconlib  # noqa: E402
import se_api  # noqa: E402
import steam  # noqa: E402
import winproc  # noqa: E402

LOG_DIR = HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)
SEVEN_ZIP = Path(r"C:\Program Files\7-Zip\7z.exe")
log = logging.getLogger("serverdeck")


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def tail_file(path, n):
    try:
        with open(path, "rb") as f:
            head = f.read(2)
            f.seek(0, 2)
            size = f.tell()
            # SCUM writes UTF-16 logs: detect from the file start, read whole code units.
            utf16 = head in (b"\xff\xfe", b"\xfe\xff") or (len(head) == 2 and head[1] == 0)
            start = max(0, size - max(400_000, min(n * 400, 8_000_000)))
            if utf16:
                start -= start % 2
            f.seek(start)
            raw = f.read()
    except OSError as e:
        return [f"(cannot read {path}: {e})"]
    text = raw.decode("utf-16-le" if utf16 else "utf-8", "replace").lstrip("﻿")
    lines = text.replace("\r", "").split("\n")
    if start and len(lines) > 1:
        lines = lines[1:]          # first line is probably cut in half
    return lines[-n:]


def log_view(path, n):
    try:
        st = Path(path).stat()
        meta = {"path": str(path), "size": st.st_size, "mtime": st.st_mtime}
    except OSError:
        return {"path": str(path), "size": None, "mtime": None, "lines": ["(this log does not exist yet)"]}
    return dict(meta, lines=tail_file(path, n))


def newest(pattern):
    files = glob.glob(pattern)
    return max(files, key=lambda p: Path(p).stat().st_mtime) if files else None


class Cancelled(Exception):
    pass


def fmt_left(seconds):
    seconds = int(round(seconds))
    if seconds >= 60:
        m = round(seconds / 60)
        return f"{m} minute{'s' if m != 1 else ''}"
    return f"{seconds} seconds"


# Countdown marks (seconds before a restart) at which players are warned.
WARN_MARKS = (3600, 1800, 900, 600, 300, 180, 60, 30, 10)


# --------------------------------------------------------------------------
# Persistent state + event log
# --------------------------------------------------------------------------

class State:
    def __init__(self, path, defaults):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        servers = self.data.setdefault("servers", {})
        for sid, d in defaults.items():
            entry = servers.setdefault(sid, {})
            for k, v in d.items():
                entry.setdefault(k, v)
        self.save()

    def get(self, sid, key, default=None):
        with self.lock:
            return self.data["servers"].get(sid, {}).get(key, default)

    def set(self, sid, **kw):
        with self.lock:
            entry = self.data["servers"].setdefault(sid, {})
            if all(entry.get(k) == v for k, v in kw.items()):
                return
            entry.update(kw)
            self._save()

    def save(self):
        with self.lock:
            self._save()

    def top(self, key, default=None):
        """Manager-wide values (not per server), e.g. backup retention settings."""
        with self.lock:
            return self.data.get(key, default)

    def set_top(self, key, value):
        with self.lock:
            self.data[key] = value
            self._save()

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)


class Events:
    """Activity feed; persisted so history survives ServerDeck/PC restarts."""

    def __init__(self, path):
        self.path = path
        self.items = collections.deque(maxlen=500)
        self.lock = threading.Lock()
        self.listeners = []            # e.g. the Discord feed; called after each event
        try:
            for line in path.read_text(encoding="utf-8").splitlines()[-500:]:
                self.items.append(json.loads(line))
        except (OSError, ValueError):
            pass
        if path.exists() and path.stat().st_size > 2_000_000:
            path.write_text("".join(json.dumps(e) + "\n" for e in self.items), encoding="utf-8")

    def add(self, level, server, msg):
        entry = {"t": time.time(), "level": level, "server": server, "msg": msg}
        with self.lock:
            self.items.append(entry)
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
            except OSError:
                pass
        log.log(logging.ERROR if level == "error" else logging.WARNING if level == "warn" else logging.INFO,
                "[%s] %s", server or "-", msg)
        for fn in list(self.listeners):
            try:
                fn(entry)
            except Exception:
                log.exception("event listener failed")

    def recent(self, n=150):
        with self.lock:
            return list(self.items)[-n:]


class LogWatch:
    """Follows the newest log matching a glob and remembers whether the current
    server session has reached its "ready" or "failed" marker."""

    def __init__(self, pattern, ready, fail):
        self.pattern = pattern
        self.ready_re = re.compile(ready)
        self.fail_re = re.compile(fail) if fail else None
        self.path, self.offset, self.ready, self.failed = None, 0, False, None

    def poll(self):
        path = newest(self.pattern)
        if not path:
            return
        if path != self.path:
            self.path, self.offset, self.ready, self.failed = path, 0, False, None
        try:
            with open(path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return
        end = data.rfind(b"\n")
        if end < 0:
            return
        self.offset += end + 1
        text = data[:end].decode("utf-8", "replace")
        if self.ready_re.search(text):
            self.ready = True
        if self.fail_re:
            m = self.fail_re.search(text)
            if m:
                line = text[text.rfind("\n", 0, m.start()) + 1:].split("\n", 1)[0]
                self.failed = line.split("->", 1)[-1].strip()[:200]


# --------------------------------------------------------------------------
# Sidecar: a helper program that may only run while its game server is online
# --------------------------------------------------------------------------

class Sidecar:
    """A Python helper (e.g. an anti-cheat/identity bridge) that may only run while its
    game server is online: started once the server answers queries, stopped when it stops
    (or has stopped answering for GRACE seconds). Config: {"dir", "script", "config",
    "port", "label"}."""
    GRACE = 60

    def __init__(self, server, cfg):
        self.server = server
        self.mgr = server.mgr
        self.cfg = cfg
        self.dir = Path(cfg["dir"])
        self.pid = self.mgr.state.get(server.id, "sidecar_pid")
        self.offline_since = None
        self.last_start = 0

    def alive(self):
        if not self.pid or not winproc.is_alive(self.pid):
            return False
        return Path(winproc.image_path(self.pid) or "python").name.lower().startswith("python")

    def reconcile(self):
        st = self.server.status
        if st.get("online"):
            self.offline_since = None
            if not self.alive() and time.time() - self.last_start > 30:
                self.start()
        elif self.alive():
            if not st.get("running"):
                self.stop("server is not running")
            else:
                self.offline_since = self.offline_since or time.time()
                if time.time() - self.offline_since > self.GRACE:
                    self.stop("server stopped answering queries")

    def start(self):
        self.last_start = time.time()
        env = dict(os.environ, PYTHONUNBUFFERED="1",
                   SIDECAR_CONFIG=str(self.dir / self.cfg.get("config", "config.toml")))
        # Its normal log goes to sidecar.log (and stdout); stderr only carries
        # crashes/tracebacks, which would otherwise vanish.
        with open(self.errors_path(), "ab") as err:
            err.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} sidecar started by ServerDeck =====\n".encode())
            err.flush()
            p = subprocess.Popen([self.mgr.config.get("sidecar_python") or sys.executable,
                                  str(self.dir / self.cfg.get("script", "sidecar.py"))],
                                 cwd=str(self.dir), env=env, creationflags=winproc.CREATE_NO_WINDOW,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
        self.pid = p.pid
        self.mgr.state.set(self.server.id, sidecar_pid=p.pid)
        self.mgr.events.add("info", self.server.id, f"sidecar started - server is online (pid {p.pid})")

    def stop(self, reason):
        if self.pid:
            winproc.kill_tree(self.pid)
        self.pid = None
        self.mgr.state.set(self.server.id, sidecar_pid=None)
        self.mgr.events.add("info", self.server.id, f"sidecar stopped - {reason}")

    @property
    def label(self):
        return self.cfg.get("label", "Sidecar")

    def status(self, ports):
        alive = self.alive()
        return {"state": "running" if alive else "stopped", "port": self.cfg.get("port"), "label": self.label,
                "listening": bool(alive and ("tcp", self.cfg.get("port")) in ports)}

    def log_path(self):
        return str(self.dir / "sidecar.log")

    def errors_path(self):
        return str(self.dir / "sidecar.errors.log")


# --------------------------------------------------------------------------
# Servers
# --------------------------------------------------------------------------

class Server:
    def __init__(self, mgr, cfg):
        self.mgr = mgr
        self.cfg = cfg
        self.id = cfg["id"]
        self.name = cfg["name"]
        self.lock = threading.Lock()
        self.job = None
        self.status = {"state": "unknown"}
        self.cpu = winproc.CpuSampler()
        self.autostarts = collections.deque(maxlen=10)
        self.held_until = 0
        self.pending_stop = False
        self.sidecar = Sidecar(self, cfg["sidecar"]) if cfg.get("sidecar") else None
        self.plugin_status = None
        self.plugin_checked = 0
        self.plugin_pid = None
        self.cancel = threading.Event()
        self.perf = {}
        self.perf_checked = 0
        self.last_a2s = None           # (time, pid, info) of the last query answer
        self.health = {"ok": True, "reasons": []}
        self.bad_since = None
        self.good_since = None

    # ---- restart schedule -----------------------------------------------
    def schedule(self):
        """Effective schedule: config defaults overlaid with what was saved from the UI."""
        sch = {"enabled": False, "every_hours": 6, "start": "00:00", "warn_minutes": 10}
        sch.update(self.cfg.get("schedule", {}))
        sch.update(self.mgr.state.get(self.id, "schedule") or {})
        return sch

    def schedule_times(self):
        """Restart times as minutes after midnight, e.g. every 6 h from 00:00 -> [0, 360, 720, 1080]."""
        sch = self.schedule()
        step = max(60, int(round(float(sch["every_hours"]) * 60)))
        h, m = map(int, sch["start"].split(":"))
        first = h * 60 + m
        return sorted({(first + k * step) % 1440 for k in range(-(-1440 // step))})

    def next_restart(self, now=None):
        now = now or datetime.now()
        slot = None
        if self.schedule()["enabled"]:
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            slot = next((midnight + timedelta(days=day, minutes=t) for day in (0, 1) for t in self.schedule_times()
                         if midnight + timedelta(days=day, minutes=t) > now), None)
        # Events (SCUM's Blood Moon) change server settings, so the server also
        # restarts when one opens and when it closes.
        boundary = self.mgr.community.next_boundary(self, now) if self.cfg.get("events") else None
        found = [x for x in (slot, boundary) if x]
        return min(found) if found else None

    def can_announce(self):
        """Can players be warned live (chat broadcast)?  SCUM can't, but its own
        Notifications.json warns them ahead of scheduled restarts."""
        return self.cfg.get("announce") in ("rcon_say", "se_chat")

    def _announce(self, msg):
        kind = self.cfg.get("announce")
        try:
            if kind == "rcon_say":
                self.rcon(f"say {msg}", timeout=5)
            elif kind == "se_chat":
                api = self.cfg["remote_api"]
                se_api.chat(api["port"], self.mgr.secret(api["key"]), msg)
        except Exception as e:
            self.mgr.events.add("warn", self.id, f"could not warn players: {e}")

    def _countdown(self, deadline, what="Server restart", skip_if_empty=True):
        """Warn players at 10/5/3/1 min, 30 s and 10 s before `deadline`, then
        return at `deadline`. Raises Cancelled if the user cancels."""
        total = deadline - time.time()
        if total <= 0:
            return
        if skip_if_empty and self.status.get("players") is not None and not self.status.get("humans"):
            self.step(f"{what.lower()}: nobody online - not waiting")
            return
        if self.job is not None:
            self.job["countdown_until"] = deadline
        marks = [m for m in WARN_MARKS if m <= total + 1]
        if self.can_announce() and (not marks or marks[0] < total - 30):
            marks.insert(0, total)                 # odd-length countdown: warn right away too
        self.cancel.clear()
        for mark in marks + [0]:
            while time.time() < deadline - mark:
                if self.cancel.wait(min(1.0, max(0.05, deadline - mark - time.time()))):
                    if self.can_announce():
                        self._announce(f"{what} cancelled")
                    raise Cancelled(f"{what.lower()} cancelled")
            if mark and self.can_announce():
                self._announce(f"{what} in {fmt_left(mark)}")
                self.step(f"warned players: {what.lower()} in {fmt_left(mark)}")
        if self.job is not None:
            self.job.pop("countdown_until", None)

    # ---- settings / versions --------------------------------------------
    def keep_online(self):
        return bool(self.mgr.state.get(self.id, "keep_online"))

    def auto_update(self):
        return bool(self.mgr.state.get(self.id, "auto_update"))

    def installed_build(self):
        return steam.installed_build(self.cfg["manifest"]) if self.cfg.get("manifest") else None

    def latest_build(self):
        return self.mgr.state.get(self.id, "latest_build")

    def game_behind(self):
        inst, latest = self.installed_build(), self.latest_build()
        return bool(inst and latest and int(latest) > int(inst))

    def mods(self):
        return self.status.get("mods") or {}

    def mods_behind(self):
        latest = self.mgr.state.get(self.id, "mods_latest") or {}
        installed = self.mods()
        return [k for k, v in latest.items() if v and installed.get(k) and installed[k] != v]

    def update_available(self):
        return self.game_behind() or bool(self.mods_behind())

    def note(self, msg):
        self.mgr.events.add("info", self.id, msg)

    # ---- jobs -----------------------------------------------------------
    def busy(self):
        return self.job is not None

    def run_job(self, name, fn, *args):
        if not self.lock.acquire(blocking=False):
            return False
        self.job = {"name": name, "step": "", "started": time.time()}
        self.mgr.events.add("info", self.id, f"{name}: started")

        def runner():
            try:
                fn(*args)
                self.mgr.events.add("info", self.id, f"{name}: done")
            except Cancelled as e:
                self.mgr.events.add("warn", self.id, f"{name}: {e}")
            except Exception as e:
                self.mgr.events.add("error", self.id, f"{name} failed: {e}")
                log.error("job %s/%s crashed:\n%s", self.id, name, traceback.format_exc())
            finally:
                self.job = None
                self.lock.release()
                try:
                    self.refresh()
                except Exception:
                    pass

        threading.Thread(target=runner, daemon=True, name=f"{self.id}:{name}").start()
        return True

    def step(self, text):
        if self.job is not None:
            self.job["step"] = text
        self.mgr.events.add("info", self.id, text)

    def job_start(self):
        self.mgr.backups.snapshot_quiet(self.id, "start")      # config files, if they changed
        self.step("starting")
        self._start()

    def job_stop(self):
        self.step("stopping")
        self._stop()

    def job_restart(self, warn_minutes=0, deadline=None, scheduled=False):
        """Restart; with a deadline (scheduled) or warn_minutes (manual) players get
        a countdown first. Scheduled restarts happen exactly on their slot."""
        if self.is_running():
            if deadline is None and warn_minutes and self.can_announce():
                deadline = time.time() + warn_minutes * 60
            if deadline:
                self._countdown(deadline, "Server restart", skip_if_empty=not scheduled)
            self.step("stopping")
            self._stop()
        self._backup("restart")
        if self.auto_update() and self.update_available():
            self._apply_update()
        self.step("starting")
        self._start()

    def job_update(self, force=False):
        if not force and not self.update_available():
            self.step("already up to date")
            return
        was_running = self.is_running()
        if was_running:
            if self.can_announce():
                minutes = min(self.schedule()["warn_minutes"], 5)
                if minutes:
                    self._countdown(time.time() + minutes * 60, "Server restart for an update")
            self.step("stopping for update")
            self._stop()
        self._backup("update")
        self._apply_update(force)
        if self.keep_online() or was_running:
            self.step("starting")
            self._start()

    def _apply_update(self, force=False):
        changed = False
        if force or self.game_behind():
            before = self.installed_build()
            # Config files that ship with the game (CS2's cfg/server.cfg) are put
            # back to Valve's defaults by `validate`; keep the server's own copy.
            preserved = {}
            for rel in self.cfg.get("preserve", []):
                p = Path(self.cfg["install_dir"]) / rel
                if p.is_file():
                    preserved[p] = p.read_bytes()
            self.step(f"steamcmd app_update {self.cfg['appid']} validate")
            ok, _ = steam.app_update(self.mgr.steamcmd, self.cfg["appid"], LOG_DIR / f"steamcmd-{self.id}.log",
                                     install_dir=self.cfg.get("steam_install_dir"))
            for p, content in preserved.items():
                if not p.exists() or p.read_bytes() != content:
                    p.write_bytes(content)
                    self.note(f"restored {p.name} (the update had reset it to the game's default)")
            if not ok:
                raise RuntimeError(f"steamcmd did not report success (see logs/steamcmd-{self.id}.log)")
            changed = self.installed_build() != before
            self.step(f"game build {before} -> {self.installed_build()}" if changed
                      else f"game files validated (build {self.installed_build()})")
        self._hooks("post_update", changed, force)

    def _hooks(self, phase, changed=False, force=False):
        """Run maintenance hooks; a failing hook is reported but never keeps
        the server offline (same "vanilla fallback" idea as the old scripts)."""
        for name, *args in self.cfg.get(phase, []):
            try:
                hooks.HOOKS[name](self, changed, force, *args)
            except Exception as e:
                self.mgr.events.add("error", self.id, f"{phase} step '{name}' failed: {e}")
                log.error("hook %s/%s failed:\n%s", self.id, name, traceback.format_exc())

    def evaluate_health(self):
        """Is the server struggling? (crashed, not answering, or its tick rate /
        sim speed below the configured floor). A condition has to last a minute
        before it counts, and 30 s of good readings clear it."""
        st, h, now = self.status, self.cfg.get("health", {}), time.time()
        reasons = []
        if self.busy() or not st.get("running"):
            pass   # stopped, or mid start/stop/update: not "struggling"
        else:
            if st.get("state") == "crashed":
                reasons.append(st.get("note") or "crashed")
            if not st.get("online") and (st.get("uptime") or 0) > h.get("startup_grace", 600):
                reasons.append("running but not answering")
            pf = self.perf if st.get("online") else {}
            if pf.get("sim_speed") is not None and pf["sim_speed"] < h.get("min_sim_speed", 0):
                reasons.append(f"sim speed {pf['sim_speed']:.2f} (should be 1.00)")
            if pf.get("fps") is not None:
                humans = st.get("humans") or 0
                floor = h.get("min_fps_busy" if humans else "min_fps_idle")
                if floor and pf["fps"] < floor:
                    reasons.append(f"server FPS {pf['fps']:.1f}" + (f" with {humans} player(s) on" if humans else ""))
        if reasons:
            self.good_since = None
            self.bad_since = self.bad_since or now
            if self.health["ok"] and now - self.bad_since >= h.get("for_seconds", 60):
                self.mgr.events.add("error", self.id, "STRUGGLING: " + "; ".join(reasons))
            if not self.health["ok"] or now - self.bad_since >= h.get("for_seconds", 60):
                self.health = {"ok": False, "reasons": reasons, "since": self.bad_since}
        else:
            self.bad_since = None
            if not self.health["ok"]:
                self.good_since = self.good_since or now
                if now - self.good_since >= 30:
                    self.health = {"ok": True, "reasons": []}
                    self.mgr.events.add("info", self.id, "recovered - no longer struggling")

    def check_perf(self):
        """Performance numbers that show a server bogging down (every 30 s)."""
        kind = (self.cfg.get("perf") or {}).get("kind")
        if not kind or not self.status.get("online"):
            self.perf = {}
            return
        if time.time() - self.perf_checked < 30:
            return
        self.perf_checked = time.time()
        try:
            if kind == "se_remote":
                api = self.cfg["remote_api"]
                d = se_api.server_info(api["port"], self.mgr.secret(api["key"]))
                self.perf = {"sim_speed": d.get("SimSpeed"), "sim_cpu": d.get("SimulationCpuLoad")}
            elif kind == "rust_serverinfo":
                d = json.loads(self.rcon("serverinfo", timeout=5))
                self.perf = {"fps": d.get("Framerate"), "entities": d.get("EntityCount")}
            elif kind == "cs2_stats":
                # "CPU   In    Out   Uptime  Users   FPS    Players" / "31.72  0.00 ... 62.42  10"
                rows = [r.split() for r in self.rcon("stats", timeout=5).strip().splitlines() if r.strip()]
                d = dict(zip(rows[0], rows[1])) if len(rows) >= 2 else {}
                self.perf = {"fps": float(d["FPS"])} if "FPS" in d else {}
            elif kind == "scum_log":
                # SCUM logs "Global Stats: 32.5ms ( 30.8FPS), ... | P:   1 (  1) ..." every 5 s:
                # its tick rate and player count (it drops to MinServerTickRate when empty).
                # It writes 4000+ lines a minute (more with players on), so look well back.
                lines = tail_file(Path(self.cfg["install_dir"]) / self.cfg["perf"]["log"], 6000)
                line = next((x for x in reversed(lines) if "LogSCUM: Global Stats" in x), "")
                fps = re.search(r"Global Stats:\s*[\d.]+ms \(\s*([\d.]+)FPS\)", line)
                players = re.search(r"[|,]\s*P:\s*(\d+)", line)
                if fps:
                    self.perf = {"fps": float(fps.group(1)),
                                 "players": int(players.group(1)) if players else None,
                                 "at": time.time()}
                elif time.time() - self.perf.get("at", 0) > 120:
                    self.perf = {}         # keep the last reading through a short gap
                ini = Path(self.cfg["install_dir"]) / self.cfg["perf"]["settings"]
                cap = re.search(r"^scum\.MaxPlayers=(\d+)", ini.read_text(encoding="utf-8", errors="replace"), re.M)
                self.perf["max_players"] = int(cap.group(1)) if cap else None
        except Exception as e:
            log.info("perf %s: %s", self.id, e)

    def _backup(self, label):
        self.mgr.backups.snapshot_quiet(self.id, label)        # config files, if they changed
        b = self.cfg.get("backup")
        if not b:
            return
        dest = Path(b["dest"])
        dest.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = dest / f"serverdeck_{self.id}_{stamp}_{label}.zip"
        paths = [str(Path(self.cfg.get("install_dir", "")) / p) for p in b["paths"]]
        paths = [p for p in paths if Path(p).exists()]
        if not paths:
            return
        self.step(f"backing up to {target.name}")
        if SEVEN_ZIP.exists():
            cmd = [str(SEVEN_ZIP), "a", "-tzip", "-mx=3", str(target)] + paths
            cmd += [f"-xr!{x}" for x in b.get("exclude", [])]
            r = winproc.run_hidden(cmd, timeout=1800)
            if r.returncode > 1:
                raise RuntimeError("backup failed: " + r.stdout.decode("utf-8", "replace")[-400:])
        else:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
                for root in paths:
                    rootp = Path(root)
                    for f in (rootp.rglob("*") if rootp.is_dir() else [rootp]):
                        if f.is_file() and not any(x in f.parts for x in b.get("exclude", [])):
                            z.write(f, Path(rootp.name) / f.relative_to(rootp) if rootp.is_dir() else f.name)
        # Retention: older than N days goes, but the newest few always stay (backups.py).
        removed, freed = self.mgr.backups.prune(only=self.id)
        if removed:
            self.note(f"retention: removed {removed} old backup(s), {freed / 1e6:.0f} MB")

    def check_plugins(self):
        """Ask the server (over RCON) whether the plugins it depends on ("plugin_check":
        {"command", "expect"}) are actually loaded; a game update can silently knock them out."""
        pc = self.cfg.get("plugin_check")
        if not pc:
            return
        if not self.status.get("online") or self.status.get("pid") != self.plugin_pid:
            # New (or no) server process: earlier results no longer apply.
            self.plugin_status, self.plugin_checked = None, 0
            self.plugin_pid = self.status.get("pid")
            if not self.status.get("online"):
                return
        if (self.status.get("uptime") or 0) < 60 or time.time() - self.plugin_checked < pc.get("interval", 120):
            return
        self.plugin_checked = time.time()
        try:
            out = self.rcon(pc["command"], timeout=5)
            missing = [name for name in pc["expect"] if name not in out]
            result = {"ok": not missing, "missing": missing, "checked": time.time()}
        except Exception as e:
            result = {"ok": None, "error": str(e), "checked": time.time()}
        prev, self.plugin_status = self.plugin_status, result
        if result["ok"] is False and (not prev or prev.get("ok") is not False):
            self.mgr.events.add("error", self.id, "plugin(s) NOT loaded: " + ", ".join(result["missing"]))
        elif result["ok"] and (not prev or prev.get("ok") is not True):
            self.note("plugins loaded: " + ", ".join(pc["expect"]))

    # ---- RCON -------------------------------------------------------------
    def rcon(self, command, timeout=10):
        r = self.cfg.get("rcon")
        if not r:
            raise RuntimeError(f"{self.name} has no RCON")
        password = self.mgr.secret(r["password"])
        fn = rconlib.source if r["kind"] == "source" else rconlib.web
        return fn("127.0.0.1", r["port"], password, command, timeout)

    # ---- status -----------------------------------------------------------
    def is_running(self):
        return bool(self.status.get("running"))

    def _proc_status(self, pid, ports):
        st = {"pid": pid, "running": bool(pid)}
        if pid:
            ps = winproc.process_stats(pid)
            if ps:
                created, cpu, private, ws = ps
                st["uptime"] = max(0, time.time() - created)
                st["cpu"] = self.cpu.percent(pid, cpu)
                st["mem"] = private        # reserved: committed memory (RAM + page file), Task Manager's "commit size"
                st["mem_ws"] = ws          # in use: physical RAM it occupies right now (working set)
        st["ports"] = [{"proto": p, "port": n, "up": ports.get((p, n)) == pid if pid else False}
                       for p, n in self.cfg.get("ports", [])]
        return st

    def _query(self, st):
        q = self.cfg.get("query")
        if q and st.get("running"):
            try:
                info = a2s.info(q.get("host", "127.0.0.1"), q["port"])
                self.last_a2s = (time.time(), st.get("pid"), info)
            except Exception:
                # A busy server (SCUM under load) can miss one query: ride out a short
                # gap with the last answer from the same process instead of flapping.
                last = self.last_a2s
                info = last[2] if last and last[1] == st.get("pid") and time.time() - last[0] < 45 else None
            if info:
                # Steam answers queries early in startup (CS2 does so before its
                # config/RCON are up), so "online" also needs every port listening.
                st.update(players=info["players"], bots=info["bots"], max_players=info["max_players"],
                          map=info["map"], server_name=info["name"],
                          online=all(p["up"] for p in st.get("ports", [])),
                          humans=max(0, info["players"] - info["bots"]))
            else:
                st["online"] = False
        elif st.get("running"):
            st["online"] = all(p["up"] for p in st.get("ports", [])) if st.get("ports") else True
        else:
            st["online"] = False

    def _extras(self, st, ports):
        if self.cfg.get("mods") in hooks.MODS:
            try:
                st["mods"] = hooks.MODS[self.cfg["mods"]](self)
            except Exception as e:
                log.warning("mods %s: %s", self.id, e)
        if self.sidecar:
            st["sidecar"] = self.sidecar.status(ports)
        if st.get("players") is None and self.perf.get("players") is not None and st.get("running"):
            # No Steam query (SCUM): use the player count from its own log.
            st.update(players=self.perf["players"], humans=self.perf["players"], bots=0,
                      max_players=self.perf.get("max_players"))
        if not st.get("server_name") and self.cfg.get("name_setting"):
            # No Steam query to ask (SCUM): show the name from the server's own settings file.
            st["server_name"] = self._setting(self.cfg["name_setting"])

    def _setting(self, ref):
        """{"file": <ini, relative to install_dir or absolute>, "key": "scum.ServerName"} -> its value
        (re-read only when the file changes)."""
        path = Path(self.cfg.get("install_dir", "")) / ref["file"]
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        cache = getattr(self, "_setting_cache", {})
        if cache.get(ref["key"], (None,))[0] != mtime:
            m = re.search(r"^%s=(.*)$" % re.escape(ref["key"]), path.read_text(encoding="utf-8-sig", errors="replace"), re.M)
            cache[ref["key"]] = (mtime, m.group(1).strip() if m else None)
            self._setting_cache = cache
        return cache[ref["key"]][1]

    def log_sources(self):
        """Ordered {label: file}: the server's own logs (newest file for globs),
        then its sidecar's, then its SteamCMD update log."""
        out = {}
        for name, pattern in self.cfg.get("logs", {}).items():
            path = newest(pattern) if any(c in pattern for c in "*?") else pattern
            if path:
                out[name] = path
        if self.sidecar:
            out[self.sidecar.label] = self.sidecar.log_path()
            if Path(self.sidecar.errors_path()).exists():
                out["Sidecar errors"] = self.sidecar.errors_path()
        update_log = LOG_DIR / f"steamcmd-{self.id}.log"
        if update_log.exists():
            out["Updates (SteamCMD)"] = str(update_log)
        return out

    def log_file(self, source):
        sources = self.log_sources()
        return sources.get(source) or next(iter(sources.values()), None)

    def log_lines(self, source, n):
        path = self.log_file(source)
        return log_view(path, n) if path else {"path": None, "lines": ["(no log yet)"]}

    def summary(self):
        st = dict(self.status)
        st.update(
            id=self.id, name=self.name, kind=self.cfg.get("kind_label", "Windows"),
            keep_online=self.keep_online(), auto_update=self.auto_update(),
            installed_build=self.installed_build(), latest_build=self.latest_build(),
            update_available=self.update_available(), mods_behind=self.mods_behind(),
            mods_latest=self.mgr.state.get(self.id, "mods_latest") or {},
            latest_checked=self.mgr.state.get(self.id, "latest_checked"),
            job=self.job, held=self.held_until > time.time(), pending_stop=self.pending_stop,
            has_rcon=bool(self.cfg.get("rcon")),
            plugins=self.plugin_status, plugin_names=(self.cfg.get("plugin_check") or {}).get("expect", []),
            logs=list(self.log_sources()), perf=self.perf,
            schedule=self.schedule(), schedule_times=[f"{t // 60:02d}:{t % 60:02d}" for t in self.schedule_times()],
            next_restart=nr.timestamp() if (nr := self.next_restart()) else None,
            can_announce=self.can_announce(), announce=self.cfg.get("announce"),
            health=self.health, events=self.mgr.community.server_events(self),
            installed=Path(self.cfg["manifest"]).exists() if self.cfg.get("manifest") else True,
        )
        return st


class WinProcessServer(Server):
    """A server that runs as a plain process (SCUM, CS2, Rust)."""

    def exe(self):
        return Path(self.cfg["install_dir"]) / self.cfg["exe"]

    def find_pid(self):
        exe = self.exe()
        for pid in winproc.find_pids(exe.name):
            # A process that has just exited can linger in the process list
            # (e.g. while another tool still holds a handle to it) - skip those.
            if not winproc.is_alive(pid):
                continue
            path = winproc.image_path(pid)
            if path is None or Path(path).resolve() == exe.resolve():
                return pid
        return None

    def refresh(self, ports=None):
        ports = ports if ports is not None else winproc.listening_ports()
        st = self._proc_status(self.find_pid(), ports)
        self._query(st)
        st["state"] = "stopped" if not st["running"] else ("running" if st["online"] else "starting")
        self._extras(st, ports)
        self.status = st

    def _start(self):
        if self.find_pid():
            return
        self._hooks("pre_start")
        exe = self.exe()
        args = [self.mgr.secret(a) for a in self.cfg.get("args", [])]
        pid, detached = winproc.launch_console_hidden([str(exe)] + args, cwd=str(exe.parent))
        self.note(f"launched {exe.name} (pid {pid}{', outside the task job' if detached else ''})")
        deadline = time.time() + self.cfg.get("start_timeout", 600)
        while time.time() < deadline:
            time.sleep(5)
            self.refresh()
            if not self.status["running"]:
                raise RuntimeError("the server process exited during startup - check its log")
            if self.status["online"]:
                return
        self.mgr.events.add("warn", self.id, "still not answering after the start timeout")

    def _stop(self):
        pid = self.find_pid()
        if not pid:
            return
        method = self.cfg.get("stop_method", "terminate")
        timeout = self.cfg.get("stop_timeout", 90)
        if method == "rcon":
            try:
                self.rcon("quit", timeout=10)
            except (ConnectionRefusedError, rconlib.AuthError) as e:
                self.mgr.events.add("warn", self.id, f"RCON quit not possible ({e}) - terminating")
                timeout = 0
            except Exception:
                pass  # "quit" usually drops the connection before any reply
            if timeout and winproc.wait_exit(pid, timeout):
                self.note("stopped cleanly via RCON quit")
                return
            if timeout:
                self.mgr.events.add("warn", self.id, f"no clean exit after {timeout}s - terminating")
        elif method == "ctrl_c":
            if winproc.send_ctrl_c(pid) and winproc.wait_exit(pid, timeout):
                self.note("stopped cleanly")
                return
            self.mgr.events.add("warn", self.id, "no clean exit - terminating")
        # SCUMServer.exe has no console and no windows: terminating is the only
        # way (and what the old 6-hourly kill did); it writes its DB continuously.
        winproc.terminate(pid)
        winproc.wait_exit(pid, 30)
        if method == "terminate":
            self.note("process terminated")


class WinServiceServer(Server):
    """A server that runs as a Windows service (Space Engineers)."""

    def __init__(self, mgr, cfg):
        super().__init__(mgr, cfg)
        self.logwatch = LogWatch(cfg["log_glob"], cfg.get("ready_pattern", "."), cfg.get("fail_pattern"))

    def svc(self):
        r = winproc.run_hidden(["sc.exe", "queryex", self.cfg["service"]], timeout=15)
        out = r.stdout.decode("utf-8", "replace")
        state = re.search(r"STATE\s*:\s*(\d+)", out)
        pid = re.search(r"PID\s*:\s*(\d+)", out)
        return (int(state.group(1)) if state else 0), (int(pid.group(1)) if pid else 0)

    def refresh(self, ports=None):
        ports = ports if ports is not None else winproc.listening_ports()
        code, pid = self.svc()
        st = self._proc_status(pid or None, ports)
        self._query(st)
        self.logwatch.poll()
        # The SE server answers queries before its world has loaded, and after
        # a failed load it restarts itself - so the log is the source of truth.
        st["online"] = st["online"] and self.logwatch.ready
        st["service_state"] = {1: "stopped", 2: "start pending", 3: "stop pending", 4: "running"}.get(code, "unknown")
        if code == 4 and pid and self.logwatch.failed and not self.logwatch.ready:
            st["state"] = "crashed"
            st["note"] = "world failed to load: " + self.logwatch.failed
        elif code == 4 and pid:
            st["state"] = "running" if st["online"] else "starting"
        elif code == 2:
            st["state"] = "starting"
        elif code == 3:
            st["state"] = "stopping"
        else:
            st["state"] = "stopped"
            st["running"] = False
        self._extras(st, ports)
        self.status = st

    def _sc(self, verb):
        r = winproc.run_hidden(["sc.exe", verb, self.cfg["service"]], timeout=30)
        return r.returncode, r.stdout.decode("utf-8", "replace")

    def _start(self):
        code, _ = self.svc()
        if code != 4:
            rc, out = self._sc("start")
            if rc not in (0, 1056):  # 1056 = already running
                raise RuntimeError(f"sc start failed ({rc}): {out.strip()[-300:]}")
        deadline = time.time() + self.cfg.get("start_timeout", 900)
        while time.time() < deadline:
            time.sleep(5)
            self.refresh()
            if self.status["state"] == "stopped":
                raise RuntimeError("service stopped during startup - check the server log")
            if self.status["state"] == "crashed":
                raise RuntimeError(self.status.get("note", "server failed to start"))
            if self.status["online"]:
                return
        self.mgr.events.add("warn", self.id, "still not accepting connections after start timeout")

    def _stop(self):
        code, pid = self.svc()
        if code == 1:
            return
        self._sc("stop")
        deadline = time.time() + self.cfg.get("stop_timeout", 180)
        while time.time() < deadline:
            time.sleep(3)
            code, _ = self.svc()
            if code == 1:
                return
        self.mgr.events.add("warn", self.id, "service did not stop in time - killing the process")
        if pid:
            winproc.terminate(pid)
            winproc.wait_exit(pid, 30)


# --------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------

KINDS = {"win_process": WinProcessServer, "win_service": WinServiceServer}


class Manager:
    def __init__(self, config_path):
        self.config_path = Path(config_path)
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.config.setdefault("servers", [])
        self.steamcmd = self.config.get("steamcmd") or r"C:\steamcmd\steamcmd.exe"
        self.events = Events(LOG_DIR / "events.jsonl")
        defaults = {s["id"]: dict(s.get("defaults", {})) for s in self.config["servers"]}
        self.state = State(HERE / "state.json", defaults)
        self.servers = [KINDS[s["type"]](self, s) for s in self.config["servers"]]
        self.by_id = {s.id: s for s in self.servers}
        # Blood Moon, SCUM tips/notices and the Discord feed (community.py)
        self.community = community.Community(self, HERE / "community.json")
        self.events.listeners.append(self.community.on_event)
        self.backups = backups.Backups(self, HERE)
        self.setup = setupwiz.Setup(self, HERE, self.config_path)
        hooks.HOOKS["scum_live"] = lambda s, changed=False, force=False: s.mgr.community.prestart(s)
        self.sys = {}
        self.check_lock = threading.Lock()
        self.last_check = max([self.state.get(s.id, "latest_checked") or 0 for s in self.servers] + [0])
        self.admin = is_admin()
        self.in_job = winproc.in_job()
        self.sys_health = {"ok": True, "reasons": []}
        self.sys_bad_since = self.sys_good_since = None
        for s in self.servers:
            attempt = self.state.get(s.id, "auto_update_attempt") or {}
            if attempt and not attempt.get("done", True):
                # ServerDeck stopped mid-update: that isn't a failure, so retry.
                self.state.set(s.id, auto_update_attempt=None)
                self.events.add("warn", s.id, "an update was interrupted when ServerDeck stopped - it will be retried")

    def secret(self, ref):
        """Resolve "{bat:<file>:<VAR>}" / "{toml:<file>:<key>}" so credentials stay in
        the files that already hold them (_env.bat, sidecar config.toml)."""
        m = re.fullmatch(r"\{(bat|toml|xml):(.+):(\w+)\}", ref)
        if not m:
            return ref
        kind, path, key = m.groups()
        if kind == "toml":
            with open(path, "rb") as f:
                return str(tomllib.load(f)[key])
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        if kind == "xml":
            found = re.search(r"<%s>(.*?)</%s>" % (key, key), text, re.S)
            if not found:
                raise KeyError(f"<{key}> not found in {path}")
            return found.group(1).strip()
        found = re.search(r'^\s*set "%s=(.*)"\s*$' % re.escape(key), text, re.M)
        if not found:
            raise KeyError(f"{key} not set in {path}")
        return found.group(1)

    # ---- background loops --------------------------------------------------
    def start(self):
        if self.admin:
            for s in self.servers:
                self.sync_scum_notifications(s)
        for target, name in ((self.supervise_loop, "supervisor"), (self.update_loop, "updates"),
                             (self.schedule_loop, "scheduler")):
            threading.Thread(target=target, daemon=True, name=name).start()
        self.community.start()
        self.backups.start()

    def refresh_all(self):
        ports = winproc.listening_ports()
        for s in self.servers:
            try:
                s.refresh(ports)
            except Exception as e:
                log.warning("refresh %s failed: %s", s.id, e)
        self.sys = winproc.system_stats()
        for d in winproc.fixed_drives():
            try:
                u = shutil.disk_usage(d)
                self.sys[f"disk_{d[0]}"] = {"free": u.free, "total": u.total}
            except OSError:
                pass

    def supervise_loop(self):
        time.sleep(3)
        while True:
            try:
                self.refresh_all()
                for s in self.servers:
                    if s.pending_stop and not s.busy():
                        s.pending_stop = False
                        s.run_job("stop", s.job_stop)
                    self._keep_alive(s)
                    if s.sidecar and self.admin:
                        s.sidecar.reconcile()
                    if self.admin and not s.busy():
                        s.check_plugins()
                        s.check_perf()
                    s.evaluate_health()
                self.evaluate_system()
            except Exception:
                log.error("supervisor error:\n%s", traceback.format_exc())
            time.sleep(5)

    def evaluate_system(self):
        """The PC itself running out of RAM/CPU makes every server struggle."""
        lim, now = self.config.get("system_health", {}), time.time()
        total, used, cpu = self.sys.get("mem_total"), self.sys.get("mem_used"), self.sys.get("cpu")
        reasons = []
        if total and used and used / total * 100 > lim.get("max_ram_percent", 92):
            reasons.append(f"PC memory {used / total * 100:.0f}% used")
        if cpu is not None and cpu > lim.get("max_cpu_percent", 95):
            reasons.append(f"PC CPU at {cpu:.0f}%")
        if reasons:
            self.sys_good_since = None
            self.sys_bad_since = self.sys_bad_since or now
            if now - self.sys_bad_since >= 60:
                if self.sys_health["ok"]:
                    self.events.add("error", None, "PC STRUGGLING: " + "; ".join(reasons))
                self.sys_health = {"ok": False, "reasons": reasons, "since": self.sys_bad_since}
        else:
            self.sys_bad_since = None
            if not self.sys_health["ok"]:
                self.sys_good_since = self.sys_good_since or now
                if now - self.sys_good_since >= 30:
                    self.sys_health = {"ok": True, "reasons": []}
                    self.events.add("info", None, "PC recovered - memory/CPU back to normal")

    def can_control(self, s):
        return self.admin

    def _keep_alive(self, s):
        if not s.keep_online() or s.busy() or s.is_running() or s.status.get("state") == "unknown":
            return
        if not self.can_control(s):
            return
        now = time.time()
        if s.held_until > now:
            return
        recent = [t for t in s.autostarts if now - t < 1800]
        if len(recent) >= 4:
            s.held_until = now + 1800
            self.events.add("error", s.id, "crash loop: 4 restarts in 30 min - auto-restart paused for 30 min")
            return
        s.autostarts.append(now)
        self.events.add("warn", s.id, "server is down but should be online - starting it")
        s.run_job("auto-start", s.job_start)

    def update_loop(self):
        time.sleep(60)
        while True:
            try:
                self.check_updates()
            except Exception:
                log.error("update check error:\n%s", traceback.format_exc())
            time.sleep(self.config.get("update_check_minutes", 30) * 60)

    def check_updates(self, apply=True):
        if not self.check_lock.acquire(blocking=False):
            return
        try:
            appids = sorted({s.cfg["appid"] for s in self.servers if s.cfg.get("appid")})
            if not appids:
                return
            latest = steam.latest_builds(self.steamcmd, appids)
            for s in self.servers:
                b = latest.get(s.cfg.get("appid"))
                if b:
                    self.state.set(s.id, latest_build=b, latest_checked=time.time())
                mods_latest = {}
                for mod, repo in s.cfg.get("mod_releases", {}).items():
                    try:
                        mods_latest[mod] = hooks.github_release(repo)["tag_name"].lstrip("v")
                    except Exception as e:
                        log.warning("latest %s lookup failed: %s", mod, e)
                if mods_latest:
                    self.state.set(s.id, mods_latest=mods_latest)
            self.last_check = time.time()
            if apply:
                for s in self.servers:
                    if s.auto_update() and s.update_available() and not s.busy() and self.can_control(s):
                        # Don't retry the very same update more than every 6 h:
                        # a broken download must not bounce a live server every 30 min.
                        target = f"{s.latest_build()}|{json.dumps(self.state.get(s.id, 'mods_latest'))}"
                        last = self.state.get(s.id, "auto_update_attempt") or {}
                        if last.get("target") == target and time.time() - last.get("t", 0) < 6 * 3600:
                            continue
                        self.state.set(s.id, auto_update_attempt={"target": target, "t": time.time(), "done": False})
                        what = (f"build {s.installed_build()} -> {s.latest_build()}" if s.game_behind()
                                else "mods: " + ", ".join(s.mods_behind()))
                        self.events.add("info", s.id, f"update available ({what}) - updating")

                        def auto_update(s=s, target=target):
                            try:
                                s.job_update()
                            finally:   # finished or failed: the 6 h backoff applies
                                self.state.set(s.id, auto_update_attempt={"target": target, "t": time.time(),
                                                                          "done": True})
                        s.run_job("auto-update", auto_update)
        finally:
            self.check_lock.release()

    def schedule_loop(self):
        """Fire each server's restart cycle. Servers that can warn players live
        start their countdown warn_minutes early, so the restart itself lands on
        the slot; SCUM's warnings come from its Notifications.json."""
        while True:
            time.sleep(10)
            try:
                self.schedule_tick(datetime.now())
            except Exception:
                log.error("scheduler error:\n%s", traceback.format_exc())

    def schedule_tick(self, now):
        for s in self.servers:
            slot = s.next_restart(now)
            if not slot:
                continue
            lead = s.schedule()["warn_minutes"] * 60 if s.can_announce() else 0
            # One 10 s tick early, so the countdown can open with the full "10 minutes".
            if (slot - now).total_seconds() > max(lead, 30) + 10:
                continue
            key = slot.strftime("%Y-%m-%d %H:%M")
            if self.state.get(s.id, "last_slot") == key:
                continue
            self.state.set(s.id, last_slot=key)
            if not (s.keep_online() and s.is_running() and self.can_control(s)):
                continue
            if s.busy():
                self.events.add("warn", s.id, f"scheduled restart {slot:%H:%M} skipped - busy ({s.job['name']})")
                continue
            if (s.status.get("uptime") or 0) < 600:
                self.events.add("info", s.id, f"scheduled restart {slot:%H:%M} skipped - "
                                              "it was (re)started less than 10 minutes ago")
                continue
            s.run_job(f"scheduled restart {slot:%H:%M}", s.job_restart, 0, slot.timestamp(), True)

    def sync_scum_notifications(self, s):
        """Keep SCUM's own Notifications.json (restart warnings, tips, event notices) in step
        with its schedule; it is rewritten again right before every SCUM start."""
        if s.cfg.get("announce") != "scum_notifications":
            return None
        try:
            changed = self.community.write_scum_notifications(s)
        except Exception as e:
            self.events.add("error", s.id, f"could not update SCUM's Notifications.json: {e}")
            return "could not update SCUM's Notifications.json"
        if changed:
            self.events.add("info", s.id, "restart warnings written to SCUM's Notifications.json "
                                          "(SCUM loads them when it next starts)")
            return "SCUM loads the new warnings when it next starts - restart it to apply them now."
        return None

    def restart_self(self, force=False):
        """Restart ServerDeck through its scheduled task (keeps it elevated).
        Game servers and sidecars are separate processes and keep running, but
        a running job (update, restart countdown...) would be cut off - so wait."""
        busy = [f"{s.name}: {s.job['name']}" for s in self.servers if s.busy()]
        if busy and not force:
            return busy
        self.events.add("warn", None, "ServerDeck is restarting")
        # (`timeout` refuses to run without a console, hence the ping delay.) Started by hand
        # rather than by the scheduled task? Then relaunch the same way it is running now.
        py = Path(sys.executable)
        pyw = py.with_name("pythonw.exe") if py.with_name("pythonw.exe").exists() else py
        relaunch = f'start "" "{pyw}" "{Path(__file__).resolve()}"'
        winproc.spawn_detached(["cmd.exe", "/c", f"ping -n 5 127.0.0.1 >nul & (schtasks /run /tn ServerDeck >nul 2>&1 || {relaunch})"])
        threading.Timer(1.0, lambda: os._exit(0)).start()
        return None

    # ---- API ---------------------------------------------------------------
    def status(self):
        return {
            "time": time.time(),
            "admin": self.admin,
            "system": self.sys,
            "system_health": self.sys_health,
            "last_check": self.last_check,
            "servers": [s.summary() for s in self.servers],
            "events": self.events.recent(),
            "community": self.community.summary(),
            "first_run": not self.servers,
        }

    def action(self, sid, action, body):
        s = self.by_id.get(sid)
        if not s:
            return 404, {"error": "unknown server"}
        if action == "settings":
            changes = {k: bool(body[k]) for k in ("keep_online", "auto_update") if k in body}
            self.state.set(sid, **changes)
            self.events.add("info", sid, "settings: " + ", ".join(f"{k}={v}" for k, v in changes.items()))
            return 200, {"ok": True}
        if action == "rcon":
            if not self.admin:
                return 403, {"error": "ServerDeck is not running elevated"}
            command = str(body.get("command", "")).strip()
            if not command:
                return 400, {"error": "empty command"}
            try:
                out = s.rcon(command)
            except Exception as e:
                return 502, {"error": f"RCON failed: {e}"}
            self.events.add("info", sid, f"rcon: {command}")
            return 200, {"output": out}
        if action == "schedule":
            try:
                sch = {"enabled": bool(body.get("enabled")), "every_hours": float(body.get("every_hours", 6)),
                       "start": str(body.get("start", "00:00")), "warn_minutes": int(body.get("warn_minutes", 10))}
                h, m = map(int, sch["start"].split(":"))
                if not (1 <= sch["every_hours"] <= 24 and 0 <= h < 24 and 0 <= m < 60 and 0 <= sch["warn_minutes"] <= 60):
                    raise ValueError
                sch["start"] = f"{h:02d}:{m:02d}"
            except (ValueError, TypeError):
                return 400, {"error": "invalid schedule: every 1-24 hours, start HH:MM, warning 0-60 minutes"}
            self.state.set(sid, schedule=sch)
            times = ", ".join(f"{t // 60:02d}:{t % 60:02d}" for t in s.schedule_times())
            self.events.add("info", sid, f"restart schedule: every {sch['every_hours']:g} h from {sch['start']} "
                                         f"({times}), {sch['warn_minutes']} min warning" if sch["enabled"]
                            else "restart schedule: off")
            return 200, {"ok": True, "note": self.sync_scum_notifications(s)}
        if action == "cancel":
            if s.job and s.job.get("countdown_until"):
                s.cancel.set()
                return 202, {"ok": True}
            return 409, {"error": "no countdown to cancel"}
        if action == "sidecar_restart":
            # e.g. after updating sidecar.py: stop it, and the supervisor starts it
            # again within seconds - but only if its server is online.
            if not self.admin or not s.sidecar:
                return 400, {"error": "no sidecar to restart" if not s.sidecar else "not running elevated"}
            if s.sidecar.alive():
                s.sidecar.stop("restarting it on request")
            s.sidecar.last_start = 0
            return 202, {"ok": True, "note": None if s.status.get("online") else
                         "the server is offline, so the sidecar stays off until it is online"}
        jobs = {"start": s.job_start, "stop": s.job_stop, "restart": s.job_restart, "update": s.job_update}
        if action not in jobs:
            return 400, {"error": "unknown action"}
        if not self.can_control(s):
            return 403, {"error": "ServerDeck is not running elevated - start it from its scheduled task"}
        if s.busy():
            if action == "stop":
                # Don't make the user wait out an update/start: stop right after it
                # (a restart countdown is simply cancelled first).
                self.state.set(sid, keep_online=False)
                s.pending_stop = True
                if s.job.get("countdown_until"):
                    s.cancel.set()
                self.events.add("info", sid, f"stop queued until '{s.job['name']}' finishes")
                return 202, {"ok": True, "queued": True}
            return 409, {"error": f"{s.name} is busy ({s.job['name']})"}
        # "Start" means bring it online and keep it there; "Stop" means keep it off.
        if action == "start":
            self.state.set(sid, keep_online=True)
            s.held_until = 0
            s.autostarts.clear()
        elif action == "stop":
            self.state.set(sid, keep_online=False)
        if action == "restart":
            warn = max(0, min(60, int(body.get("warn_minutes") or 0)))
            args = (warn,)
        else:
            args = (True,) if action == "update" else ()
        if not s.run_job(action, jobs[action], *args):
            return 409, {"error": f"{s.name} is busy"}
        return 202, {"ok": True}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    mgr: Manager = None

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(200, (HERE / "web" / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/status":
            return self._send(200, self.mgr.status())
        q = parse_qs(url.query)
        n = max(10, min(int(q.get("n", ["500"])[0]), 10000))
        source = q.get("source", [""])[0]
        m = re.fullmatch(r"/api/servers/([\w-]+)/(log|logfile)", url.path)
        if m and m.group(1) in self.mgr.by_id:
            s = self.mgr.by_id[m.group(1)]
            if m.group(2) == "log":
                return self._send(200, s.log_lines(source, n))
            return self._send_file(s.log_file(source))
        if url.path == "/api/setup":
            return self._send(200, self.mgr.setup.overview())
        m = re.fullmatch(r"/api/backups(?:/([\w-]+)(?:/(file|manifest|download))?)?", url.path)
        if m:
            b = self.mgr.backups
            tid, sub = m.group(1), m.group(2)
            if not tid:
                return self._send(200, b.overview())
            if tid not in b.targets:
                return self._send(404, {"error": "unknown backup target"})
            try:
                if sub is None:
                    return self._send(200, b.detail(tid))
                if sub == "file":
                    return self._send(200, b.read_text(b.targets[tid], q.get("rel", [""])[0]))
                if sub == "manifest":
                    return self._send(200, b.manifest(b.targets[tid], q.get("name", [""])[0]))
                p = b.download_path(tid, q.get("kind", [""])[0], q.get("name", [""])[0])
                return self._send_file(p) if p else self._send(404, {"error": "not found"})
            except (FileNotFoundError, ValueError, KeyError, OSError, zipfile.BadZipFile) as e:
                return self._send(404, {"error": str(e)})
        if url.path == "/api/deck/log":
            return self._send(200, log_view(LOG_DIR / "serverdeck.log", n))
        if url.path == "/api/deck/logfile":
            return self._send_file(LOG_DIR / "serverdeck.log")
        self._send(404, {"error": "not found"})

    def _send_file(self, path):
        """Whole log as a download (opened with shared access - servers keep writing)."""
        try:
            f = open(path, "rb")
        except (OSError, TypeError):
            return self._send(404, {"error": "log not found"})
        with f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(0)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{Path(path).name}"')
            self.send_header("Content-Length", str(size))
            self.end_headers()
            remaining = size
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self):
        # Blocks cross-site form posts: browsers can't add custom headers to
        # cross-origin requests without a CORS preflight, which we never allow.
        if self.headers.get("X-ServerDeck") != "1":
            return self._send(403, {"error": "missing X-ServerDeck header"})
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        if url.path == "/api/check-updates":
            threading.Thread(target=self.mgr.check_updates, daemon=True).start()
            return self._send(202, {"ok": True})
        if url.path == "/api/deck/restart":
            busy = self.mgr.restart_self(force=bool(body.get("force")))
            if busy:
                return self._send(409, {"error": "wait for these to finish first: " + "; ".join(busy)})
            return self._send(202, {"ok": True})
        if url.path == "/api/community":
            code, payload = self.mgr.community.api(body)
            return self._send(code, payload)
        m = re.fullmatch(r"/api/setup/(\w+)", url.path)
        if m:
            code, payload = self.mgr.setup.api(m.group(1), body)
            return self._send(code, payload)
        m = re.fullmatch(r"/api/backups/([\w-]+)/(\w+)", url.path)
        if m:
            code, payload = self.mgr.backups.api(m.group(1), m.group(2), body)
            return self._send(code, payload)
        m = re.fullmatch(r"/api/servers/([\w-]+)/(\w+)", url.path)
        if m:
            code, payload = self.mgr.action(m.group(1), m.group(2), body)
            return self._send(code, payload)
        self._send(404, {"error": "not found"})


class DeckHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        # A second ServerDeck must fail to bind, not silently share the port.
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    handler = logging.handlers.RotatingFileHandler(LOG_DIR / "serverdeck.log", maxBytes=5_000_000, backupCount=5,
                                                   encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
    config = HERE / "config.json"
    if not config.exists():                        # first run: start empty, add servers in the UI
        example = HERE / "config.example.json"
        config.write_text(example.read_text(encoding="utf-8") if example.exists()
                          else json.dumps({"servers": []}, indent=2), encoding="utf-8")
    mgr = Manager(config)
    ui = mgr.config.get("ui", {})
    Handler.mgr = mgr
    try:
        httpd = DeckHTTPServer((ui.get("host", "127.0.0.1"), ui.get("port", 8787)), Handler)
    except OSError as e:
        log.error("cannot bind the UI port (already running?): %s", e)
        sys.exit(3)
    mgr.events.add("info", None, f"ServerDeck started (admin={mgr.admin})")
    if not mgr.admin:
        mgr.events.add("warn", None, "not running elevated: servers can be monitored but not controlled")
    mgr.start()
    httpd.serve_forever()


if __name__ == "__main__":
    main()
