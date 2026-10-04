"""Backups for ServerDeck.

* Config backups - every server lists its config files ("config_files" globs in config.json,
  relative to its install folder or absolute). ServerDeck zips them, only when something changed,
  before every start/restart/update, before a config edit or restore from the UI, once a day,
  and on demand. They restore file by file.
* Full backups - the zips servers make before restarts/updates ("backup" in config.json: world
  saves + configs). They restore as a whole: the server is stopped, the current folders are moved
  aside (<folder>.before-restore-<time>), the backup is unpacked, the server is started again.
* Retention - backups older than N days are deleted, but the newest M of each kind always stay.

ServerDeck's own config.json / community.json are a target too ("ServerDeck").
"""
import fnmatch
import glob
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger("serverdeck")
DEFAULTS = {"keep_days": 14, "keep_min": 3, "include_other": False, "daily_time": "04:30"}
MAX_FILE = 5_000_000          # nothing that big is a config file
MAX_EDIT = 1_000_000
STAMP = "%Y%m%d_%H%M%S"
ARCHIVES = (".zip", ".7z")


class Conflict(Exception):
    pass


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tag(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:40] or "manual"


def norm(p):
    return os.path.normcase(os.path.abspath(str(p)))


def detect_encoding(raw):
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8", b"\xef\xbb\xbf"
    if raw.startswith(b"\xff\xfe"):
        return "utf-16-le", b"\xff\xfe"
    if raw.startswith(b"\xfe\xff"):
        return "utf-16-be", b"\xfe\xff"
    if len(raw) >= 4 and raw[1:4:2] == b"\x00\x00":
        return "utf-16-le", b""
    return "utf-8", b""


def by_mtime(paths):
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)


class Target:
    """Something whose config files get backed up: a game server, or ServerDeck itself."""

    def __init__(self, tid, name, base, patterns, server=None, full=None, extra=None):
        self.id, self.name, self.base, self.server = tid, name, Path(base), server
        self.patterns = [p if Path(p).is_absolute() else str(self.base / p) for p in patterns]
        self.full = full                 # {"dest": dir, "paths": [abs folders]} or None
        self.extra = [p if Path(p).is_absolute() else str(self.base / p) for p in (extra or [])]

    def files(self):
        found = {}
        for pat in self.patterns:
            for f in glob.glob(pat, recursive=True):
                p = Path(f)
                try:
                    if p.is_file() and p.stat().st_size <= MAX_FILE:
                        found[norm(p)] = p
                except OSError:
                    pass
        return sorted(found.values(), key=lambda p: str(p).lower())

    def allowed(self, path):
        """Restores/edits may only touch paths the config_files patterns cover."""
        n = norm(path)
        return any(fnmatch.fnmatch(n, norm(pat)) for pat in self.patterns)

    def rel(self, path):
        """Name shown in the UI and used inside the zip: relative to the install folder,
        or drive-rooted ("C/ProgramData/...") for files elsewhere."""
        p = Path(os.path.abspath(path))
        try:
            return p.relative_to(Path(os.path.abspath(self.base))).as_posix()
        except ValueError:
            return (p.drive.rstrip(":") + "/" + p.relative_to(p.anchor).as_posix()) if p.drive else p.as_posix()


class Backups:
    def __init__(self, mgr, here):
        self.mgr, self.here = mgr, Path(here)
        self.root = Path(mgr.config.get("backups", {}).get("root") or (self.here / "backups"))
        self.lock = threading.RLock()
        self.targets = {}
        for s in mgr.servers:
            b = s.cfg.get("backup")
            full = None
            if b and b.get("dest"):
                base = Path(s.cfg.get("install_dir", ""))
                full = {"dest": b["dest"], "paths": [str(p if Path(p).is_absolute() else base / p) for p in b["paths"]]}
            self.targets[s.id] = Target(s.id, s.name, s.cfg.get("install_dir") or self.here, s.cfg.get("config_files", []),
                                        s, full, s.cfg.get("prune_extra"))
        self.targets["deck"] = Target("deck", "ServerDeck", self.here, ["config.json", "community.json"])

    # ---- settings ----------------------------------------------------------
    def settings(self):
        st = dict(DEFAULTS)
        st.update({k: v for k, v in self.mgr.config.get("backups", {}).items() if k in DEFAULTS})
        st.update(self.mgr.state.top("backups") or {})
        return st

    def save_settings(self, body):
        st = self.settings()
        try:
            st["keep_days"] = max(1, min(3650, int(body.get("keep_days", st["keep_days"]))))
            st["keep_min"] = max(1, min(100, int(body.get("keep_min", st["keep_min"]))))
            st["include_other"] = bool(body.get("include_other", st["include_other"]))
            t = str(body.get("daily_time", st["daily_time"]))
            h, m = map(int, t.split(":"))
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError
            st["daily_time"] = f"{h:02d}:{m:02d}"
        except (ValueError, TypeError):
            raise ValueError("keep 1-3650 days, keep at least 1, daily time HH:MM")
        self.mgr.state.set_top("backups", st)
        return st

    # ---- config files (view / edit) ------------------------------------------
    def find(self, t, rel):
        for f in t.files():
            if t.rel(f) == rel:
                return f
        raise FileNotFoundError(rel)

    def file_list(self, t):
        out = []
        for f in t.files():
            st = f.stat()
            out.append({"rel": t.rel(f), "path": str(f), "size": st.st_size, "mtime": st.st_mtime})
        return out

    def read_text(self, t, rel):
        f = self.find(t, rel)
        raw = f.read_bytes()
        info = {"rel": rel, "path": str(f), "size": len(raw), "mtime": f.stat().st_mtime}
        if len(raw) > MAX_EDIT:
            return dict(info, text=None, why="too big to edit here - download it instead")
        enc, bom = detect_encoding(raw)
        try:
            text = raw[len(bom):].decode(enc)
        except UnicodeDecodeError:
            return dict(info, text=None, why="not a text file")
        return dict(info, text=text.replace("\r\n", "\n"), encoding=enc)

    def write_text(self, t, rel, text, mtime):
        f = self.find(t, rel)
        if abs(f.stat().st_mtime - float(mtime)) > 0.01:
            raise Conflict(f"{f.name} changed on disk since you opened it - reopen it and redo the edit")
        raw = f.read_bytes()
        enc, bom = detect_encoding(raw)
        self.snapshot(t, f"before editing {f.name}")          # (unchanged since the last one = already saved)
        out = text.replace("\r\n", "\n")
        if b"\r\n" in raw or (enc.startswith("utf-16") and "\r\n".encode(enc) in raw):
            out = out.replace("\n", "\r\n")
        tmp = f.with_name(f.name + ".serverdeck-tmp")
        tmp.write_bytes(bom + out.encode(enc))
        tmp.replace(f)
        self.mgr.events.add("info", t.server.id if t.server else None, f"config edited in ServerDeck: {rel}")
        return f.stat().st_mtime

    # ---- config backups -------------------------------------------------------
    def snap_dir(self, t):
        return self.root / "configs" / t.id

    def snapshot(self, t, reason, force=False):
        """Zip the target's config files; skipped when nothing changed since the last one."""
        files = t.files()
        if not files:
            return None
        with self.lock:
            current = {}
            for f in files:
                try:
                    current[str(f)] = sha256(f)
                except OSError:
                    pass
            last = self.list_snapshots(t)[:1]
            if last and not force:
                try:
                    prev = {e["path"]: e["sha256"] for e in self.manifest(t, last[0]["name"])["files"]}
                    if prev == current:
                        return None
                except (OSError, KeyError, ValueError, zipfile.BadZipFile):
                    pass
            d = self.snap_dir(t)
            d.mkdir(parents=True, exist_ok=True)
            name = f"{datetime.now():{STAMP}}_{tag(reason)}.zip"
            while (d / name).exists():
                time.sleep(1)
                name = f"{datetime.now():{STAMP}}_{tag(reason)}.zip"
            manifest = {"target": t.id, "name": t.name, "time": time.time(), "reason": reason, "files": []}
            tmp = d / (name + ".tmp")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for f in files:
                    if str(f) not in current:
                        continue
                    arc = "files/" + t.rel(f)
                    try:
                        z.write(f, arc)
                        st = f.stat()
                    except OSError:
                        continue
                    manifest["files"].append({"path": str(f), "rel": t.rel(f), "arc": arc, "sha256": current[str(f)],
                                              "size": st.st_size, "mtime": st.st_mtime})
                z.writestr("manifest.json", json.dumps(manifest, indent=1))
            tmp.replace(d / name)
        return d / name

    def snapshot_quiet(self, tid, reason):
        t = self.targets.get(tid)
        if not t:
            return
        try:
            p = self.snapshot(t, reason)
            if p:
                log.info("config backup %s: %s", tid, p.name)
        except Exception:
            log.exception("config backup of %s failed", tid)

    def list_snapshots(self, t):
        out = []
        for z in sorted(self.snap_dir(t).glob("*.zip"), reverse=True):
            m = re.match(r"(\d{8}_\d{6})_(.*)\.zip$", z.name)
            try:
                when = datetime.strptime(m.group(1), STAMP).timestamp() if m else z.stat().st_mtime
                out.append({"name": z.name, "time": when, "reason": (m.group(2) if m else "").replace("-", " "),
                            "size": z.stat().st_size})
            except (OSError, ValueError):
                pass
        return out

    def manifest(self, t, name):
        if "/" in name or "\\" in name:
            raise ValueError("bad name")
        with zipfile.ZipFile(self.snap_dir(t) / name) as z:
            return json.loads(z.read("manifest.json"))

    def restore_snapshot(self, t, name, rels=None, restart=True):
        m = self.manifest(t, name)
        chosen = [e for e in m["files"] if rels is None or e["rel"] in rels]
        chosen = [e for e in chosen if t.allowed(e["path"])]
        if not chosen:
            raise ValueError("nothing to restore")
        s = t.server

        def work():
            self.snapshot(t, "before restore", force=True)
            was = bool(s and s.is_running())
            if s and restart and was:
                s.step("stopping to restore config files")
                s._stop()
            with zipfile.ZipFile(self.snap_dir(t) / name) as z:
                for e in chosen:
                    dest = Path(e["path"])
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_name(dest.name + ".serverdeck-tmp")
                    tmp.write_bytes(z.read(e["arc"]))
                    tmp.replace(dest)
            note = f"restored {len(chosen)} config file(s) from the {name[:15]} backup"
            if s:
                s.step(note)
                if restart and was:
                    s.step("starting")
                    s._start()
                elif s.is_running():
                    s.note("the server reads its config when it starts - restart it to use the restored files")
            else:
                busy = self.mgr.restart_self()
                self.mgr.events.add("info", None, note + (" - restart ServerDeck once these finish: " + "; ".join(busy)
                                                          if busy else " - ServerDeck is restarting to load them"))

        if s:
            if not s.run_job("restore configs", work):
                raise RuntimeError(f"{s.name} is busy")
        else:
            threading.Thread(target=work, daemon=True, name="restore-deck").start()
        return len(chosen)

    # ---- full backups ---------------------------------------------------------
    def list_full(self, t):
        if not t.full:
            return []
        d = Path(t.full["dest"])
        out = []
        for f in by_mtime([p for p in d.glob("*") if p.is_file() and p.suffix.lower() in ARCHIVES]):
            m = re.match(rf"serverdeck_{re.escape(t.id)}_(\d{{8}}_\d{{6}})_(.*)\.zip$", f.name)
            try:
                out.append({"name": f.name, "time": datetime.strptime(m.group(1), STAMP).timestamp() if m
                            else f.stat().st_mtime, "reason": m.group(2).replace("-", " ") if m else "older backup",
                            "size": f.stat().st_size, "ours": bool(m)})
            except (OSError, ValueError):
                pass
        return out

    def full_backup_now(self, t):
        s = t.server
        if not (s and t.full):
            raise ValueError("this server has no full backups configured")
        if not s.run_job("full backup", s._backup, "manual"):
            raise RuntimeError(f"{s.name} is busy")

    def restore_full(self, t, name):
        s = t.server
        if not (s and t.full):
            raise ValueError("this server has no full backups configured")
        f = Path(t.full["dest"]) / name
        if "/" in name or "\\" in name or not f.is_file() or not name.startswith(f"serverdeck_{t.id}_"):
            raise ValueError("unknown backup")

        def work():
            was = s.is_running()
            if was:
                s.step("stopping")
                s._stop()
            stamp = datetime.now().strftime(STAMP)
            with zipfile.ZipFile(f) as z:
                names = z.namelist()
                for p in map(Path, t.full["paths"]):
                    members = [n for n in names if n.replace("\\", "/").split("/")[0] == p.name]
                    if not members:
                        continue
                    if p.exists():
                        aside = p.with_name(f"{p.name}.before-restore-{stamp}")
                        s.step(f"moving the current {p.name} folder aside ({aside.name})")
                        p.rename(aside)
                    s.step(f"unpacking {p.name} from {name}")
                    root = norm(p)
                    for n in members:
                        target = p.parent / n.replace("\\", "/")
                        if not (norm(target) == root or norm(target).startswith(root + os.sep)):
                            continue                         # never write outside the folder
                        if n.endswith("/"):
                            target.mkdir(parents=True, exist_ok=True)
                            continue
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with z.open(n) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst, 1 << 20)
            s.step(f"restored the full backup {name}")
            if was or s.keep_online():
                s.step("starting")
                s._start()

        if not s.run_job("restore full backup", work):
            raise RuntimeError(f"{s.name} is busy")

    # ---- retention ------------------------------------------------------------
    def prune(self, only=None):
        """Delete backups older than keep_days, keeping the newest keep_min of each kind."""
        st = self.settings()
        cutoff = time.time() - st["keep_days"] * 86400
        removed, freed = 0, 0

        def drop(paths):
            nonlocal removed, freed
            for p in by_mtime(paths)[st["keep_min"]:]:
                try:
                    if p.stat().st_mtime >= cutoff:
                        continue
                    size = p.stat().st_size if p.is_file() else sum(x.stat().st_size for x in p.rglob("*") if x.is_file())
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
                    removed, freed = removed + 1, freed + size
                except OSError as e:
                    log.warning("retention: cannot delete %s: %s", p, e)

        with self.lock:
            for t in self.targets.values():
                if only and t.id != only:
                    continue
                drop([p for p in self.snap_dir(t).glob("*.zip")])
                if t.full:
                    d = Path(t.full["dest"])
                    drop([p for p in d.glob(f"serverdeck_{t.id}_*.zip")])
                    if st["include_other"]:
                        drop([p for p in d.glob("*") if p.is_file() and p.suffix.lower() in ARCHIVES
                              and not p.name.startswith("serverdeck_")])
                    for p in map(Path, t.full["paths"]):
                        drop([x for x in p.parent.glob(p.name + ".before-restore-*") if x.is_dir()])
                for pat in t.extra:
                    drop([Path(x) for x in glob.glob(pat)])
        return removed, freed

    def usage(self):
        """Space used per backup folder (for the UI)."""
        out = []
        for t in self.targets.values():
            entry = {"id": t.id, "name": t.name, "config_bytes": 0, "config_count": 0, "full_bytes": 0, "full_count": 0,
                     "other_bytes": 0, "other_count": 0}
            for p in self.snap_dir(t).glob("*.zip"):
                entry["config_bytes"] += p.stat().st_size
                entry["config_count"] += 1
            if t.full:
                for p in Path(t.full["dest"]).glob("*"):
                    if p.is_file() and p.suffix.lower() in ARCHIVES:
                        k = "full" if p.name.startswith(f"serverdeck_{t.id}_") else "other"
                        entry[f"{k}_bytes"] += p.stat().st_size
                        entry[f"{k}_count"] += 1
            out.append(entry)
        return out

    def loop(self):
        time.sleep(120)
        while True:
            try:
                st = self.settings()
                now = datetime.now()
                h, m = map(int, st["daily_time"].split(":"))
                day = now.strftime("%Y-%m-%d")
                if (now.hour, now.minute) >= (h, m) and self.mgr.state.top("backups_daily") != day:
                    made = sum(1 for t in self.targets.values() if self.snapshot(t, "daily"))
                    removed, freed = self.prune()
                    self.mgr.state.set_top("backups_daily", day)
                    self.mgr.events.add("info", None, f"daily backups: {made} config backup(s) made; retention removed "
                                                      f"{removed} old backup(s), {freed / 1e6:.0f} MB freed")
            except Exception:
                log.exception("backup loop error")
            time.sleep(60)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="backups").start()

    # ---- API ------------------------------------------------------------------
    def overview(self):
        return {"settings": self.settings(), "root": str(self.root), "usage": self.usage(),
                "targets": [{"id": t.id, "name": t.name, "has_full": bool(t.full), "patterns": t.patterns}
                            for t in self.targets.values()]}

    def detail(self, tid):
        t = self.targets[tid]
        return {"id": t.id, "name": t.name, "files": self.file_list(t), "snapshots": self.list_snapshots(t),
                "full": self.list_full(t), "has_full": bool(t.full),
                "full_paths": t.full["paths"] if t.full else [], "busy": bool(t.server and t.server.busy())}

    def download_path(self, tid, kind, name):
        t = self.targets[tid]
        if "/" in name or "\\" in name:
            return None
        if kind == "snapshot":
            p = self.snap_dir(t) / name
        elif kind == "full" and t.full:
            p = Path(t.full["dest"]) / name
        elif kind == "file":
            return next((f for f in t.files() if t.rel(f) == name), None)
        else:
            return None
        return p if p.is_file() else None

    def api(self, tid, action, body):
        if tid == "settings":
            if action == "save":
                try:
                    return 200, {"ok": True, "settings": self.save_settings(body)}
                except ValueError as e:
                    return 400, {"error": str(e)}
            if action == "prune":
                removed, freed = self.prune()
                self.mgr.events.add("info", None, f"backup clean-up: removed {removed} old backup(s), {freed / 1e6:.0f} MB freed")
                return 200, {"ok": True, "removed": removed, "freed": freed}
            return 400, {"error": "unknown action"}
        t = self.targets.get(tid)
        if not t:
            return 404, {"error": "unknown backup target"}
        if not self.mgr.admin:
            return 403, {"error": "ServerDeck is not running elevated"}
        try:
            if action == "snapshot":
                p = self.snapshot(t, "manual", force=True)
                return 200, {"ok": True, "name": p.name if p else None}
            if action == "full":
                self.full_backup_now(t)
                return 202, {"ok": True}
            if action == "save_file":
                mtime = self.write_text(t, str(body.get("rel", "")), str(body.get("text", "")), body.get("mtime", 0))
                return 200, {"ok": True, "mtime": mtime}
            if action == "restore":
                if body.get("kind") == "full":
                    self.restore_full(t, str(body.get("name", "")))
                    return 202, {"ok": True}
                rels = body.get("files")
                n = self.restore_snapshot(t, str(body.get("name", "")), set(rels) if rels is not None else None,
                                          restart=bool(body.get("restart", True)))
                return 202, {"ok": True, "files": n}
        except Conflict as e:
            return 409, {"error": str(e)}
        except FileNotFoundError as e:
            return 404, {"error": f"not found: {e}"}
        except (ValueError, RuntimeError, OSError, zipfile.BadZipFile) as e:
            return 400, {"error": str(e)}
        return 400, {"error": "unknown action"}
