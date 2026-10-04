"""Add / remove servers from templates (templates/*.json) and change general settings - the
"Servers" dialog. config.json is backed up before every change and ServerDeck restarts to load it.

A template has "fields" (asked in the UI) and a "server" block whose "{field}" placeholders
are filled in ("{backup_root}" too). A string that is only a placeholder takes the field's type
(ports stay numbers). In "args", a "+setting" whose value ends up empty is dropped (no GSLT given).
"""
import io
import json
import re
import secrets
import shutil
import threading
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

import winproc

ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,19}")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
RESERVED_IDS = {"deck", "settings", "setup"}
STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"


class Setup:
    def __init__(self, mgr, here, config_path):
        self.mgr, self.here, self.path = mgr, Path(here), Path(config_path)
        self.tdir = self.here / "templates"
        self.steamcmd_job = None

    # ---- config.json ----
    def load(self):
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def save(self, cfg, why):
        if self.path.exists():
            bak = self.path.with_name(f"config.json.bak-{datetime.now():%Y%m%d-%H%M%S}")
            n = 2
            while bak.exists():
                bak = self.path.with_name(f"config.json.bak-{datetime.now():%Y%m%d-%H%M%S}-{n}")
                n += 1
            shutil.copy2(self.path, bak)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        self.mgr.events.add("info", None, f"config.json changed: {why}")

    def backup_root(self, cfg=None):
        cfg = cfg if cfg is not None else self.load()
        return Path(cfg.get("backups", {}).get("root") or (self.here / "backups"))

    # ---- templates ----
    def templates(self):
        out = []
        for f in sorted(self.tdir.glob("*.json")):
            try:
                t = json.loads(f.read_text(encoding="utf-8"))
                out.append({k: t[k] for k in ("template", "title", "description", "fields")})
            except (ValueError, KeyError):
                pass
        return out

    def template(self, name):
        for f in self.tdir.glob("*.json"):
            t = json.loads(f.read_text(encoding="utf-8"))
            if t.get("template") == name:
                return t
        raise ValueError(f"unknown template '{name}'")

    def render(self, tpl, values, cfg):
        vals = {}
        for f in tpl["fields"]:
            v = values.get(f["key"], f.get("default", ""))
            if f.get("type") == "int":
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise ValueError(f"{f['label']} must be a number")
                if "port" in f["key"] and not 1 <= v <= 65535:
                    raise ValueError(f"{f['label']} must be 1-65535")
            else:
                v = str(v if v is not None else "").strip()
                if f.get("generate") and not v:
                    v = secrets.token_urlsafe(12)
            vals[f["key"]] = v
        vals["backup_root"] = str(self.backup_root(cfg))

        def sub(x):
            if isinstance(x, str):
                m = PLACEHOLDER.fullmatch(x)
                if m and m.group(1) in vals:
                    return vals[m.group(1)]
                return PLACEHOLDER.sub(lambda mm: str(vals[mm.group(1)]) if mm.group(1) in vals else mm.group(0), x)
            if isinstance(x, list):
                return [sub(i) for i in x]
            if isinstance(x, dict):
                return {k: sub(v) for k, v in x.items()}
            return x

        srv = sub(json.loads(json.dumps(tpl["server"])))
        args, kept, i = [str(a) for a in srv.get("args", [])], [], 0
        while i < len(args):
            if args[i].startswith("+") and i + 1 < len(args) and args[i + 1] == "":
                i += 2
                continue
            kept.append(args[i])
            i += 1
        srv["args"] = kept
        srv["template"] = tpl["template"]
        return srv

    # ---- actions ----
    def add(self, template, values):
        cfg = self.load()
        servers = cfg.setdefault("servers", [])
        srv = self.render(self.template(template), values or {}, cfg)
        if not ID_RE.fullmatch(srv["id"]) or srv["id"] in RESERVED_IDS:
            raise ValueError("the id must be 1-20 lowercase letters, numbers, - or _ (not deck/settings/setup)")
        if any(s["id"] == srv["id"] for s in servers):
            raise ValueError(f"there is already a server with the id '{srv['id']}'")
        if not Path(srv["install_dir"]).is_absolute():
            raise ValueError("the install folder must be a full path like C:\\GameServers\\Name")
        used = {(p, int(n)): s["name"] for s in servers for p, n in s.get("ports", [])}
        clash = [f"{p}/{n} ({used[(p, int(n))]})" for p, n in srv.get("ports", []) if (p, int(n)) in used]
        if clash:
            raise ValueError("ports already used by another server: " + ", ".join(clash))
        servers.append(srv)
        self.save(cfg, f"added {srv['name']} ({srv['id']}) from the {template} template")
        if srv.get("firewall") == "auto":
            self.firewall(srv)
        return srv

    def remove(self, sid):
        cfg = self.load()
        servers = cfg.get("servers", [])
        srv = next((s for s in servers if s["id"] == sid), None)
        if not srv:
            raise ValueError("unknown server")
        live = self.mgr.by_id.get(sid)
        if live and (live.busy() or live.is_running()):
            raise ValueError(f"stop {srv['name']} first")
        cfg["servers"] = [s for s in servers if s["id"] != sid]
        self.save(cfg, f"removed {srv['name']} ({sid}) - its files, saves and backups stay on disk")
        if srv.get("firewall") == "auto":
            self.firewall(srv, remove_only=True)
        return srv

    def general(self, body):
        cfg = self.load()
        if "steamcmd" in body:
            cfg["steamcmd"] = str(body["steamcmd"]).strip()
        if "backups_root" in body:
            root = str(body["backups_root"]).strip()
            if root and not Path(root).is_absolute():
                raise ValueError("the backups folder must be a full path")
            cfg.setdefault("backups", {})["root"] = root or None
        self.save(cfg, "general settings")

    def firewall(self, srv, remove_only=False):
        """Inbound rules for the server's ports - all of them except its RCON port (ServerDeck
        talks RCON over localhost; nobody else needs it). Needs admin (ServerDeck runs elevated)."""
        rcon_port = int((srv.get("rcon") or {}).get("port") or 0)
        for proto in ("udp", "tcp"):
            name = f"ServerDeck - {srv['id']} ({proto.upper()})"
            winproc.run_hidden(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"], timeout=30)
            if remove_only:
                continue
            ports = sorted({int(n) for p, n in srv.get("ports", []) if p == proto and int(n) != rcon_port})
            if ports:
                r = winproc.run_hidden(["netsh", "advfirewall", "firewall", "add", "rule", f"name={name}", "dir=in",
                                        "action=allow", f"protocol={proto.upper()}",
                                        "localport=" + ",".join(map(str, ports)), "profile=any"], timeout=30)
                self.mgr.events.add("info" if r.returncode == 0 else "warn", None,
                                    f"firewall: {name} for {', '.join(map(str, ports))}"
                                    + ("" if r.returncode == 0 else " FAILED (is ServerDeck elevated?)"))

    def install_steamcmd(self, folder):
        folder = Path(str(folder).strip() or r"C:\steamcmd")
        if not folder.is_absolute():
            raise ValueError("give a full path, e.g. C:\\steamcmd")
        if self.steamcmd_job and self.steamcmd_job.is_alive():
            raise ValueError("SteamCMD is already being installed")

        def work():
            try:
                folder.mkdir(parents=True, exist_ok=True)
                with urllib.request.urlopen(urllib.request.Request(STEAMCMD_URL, headers={"User-Agent": "ServerDeck"}),
                                            timeout=120) as r:
                    zipfile.ZipFile(io.BytesIO(r.read())).extractall(folder)
                exe = folder / "steamcmd.exe"
                self.mgr.events.add("info", None, f"SteamCMD downloaded to {folder} - letting it update itself")
                winproc.run_hidden([str(exe), "+quit"], timeout=1800, cwd=str(folder))
                cfg = self.load()
                cfg["steamcmd"] = str(exe)
                self.save(cfg, f"SteamCMD installed at {exe}")
                self.mgr.events.add("info", None, "SteamCMD is ready - restart ServerDeck to use it")
            except Exception as e:
                self.mgr.events.add("error", None, f"SteamCMD install failed: {e}")

        self.steamcmd_job = threading.Thread(target=work, daemon=True, name="steamcmd-install")
        self.steamcmd_job.start()

    # ---- API ----
    def overview(self):
        cfg = self.load()
        steamcmd = cfg.get("steamcmd") or ""
        return {
            "templates": self.templates(),
            "servers": [{"id": s["id"], "name": s["name"], "type": s.get("type"), "template": s.get("template"),
                         "install_dir": s.get("install_dir"), "loaded": s["id"] in self.mgr.by_id}
                        for s in cfg.get("servers", [])],
            "general": {"steamcmd": steamcmd, "steamcmd_found": bool(steamcmd) and Path(steamcmd).is_file(),
                        "steamcmd_installing": bool(self.steamcmd_job and self.steamcmd_job.is_alive()),
                        "backups_root": str(self.backup_root(cfg))},
        }

    def api(self, action, body):
        if not self.mgr.admin:
            return 403, {"error": "ServerDeck is not running elevated"}
        try:
            if action == "add":
                srv = self.add(str(body.get("template", "")), body.get("values") or {})
                note = self.reload_note()
                return 200, {"ok": True, "id": srv["id"], "name": srv["name"], "config_files": srv.get("config_files", []),
                             "note": note}
            if action == "remove":
                srv = self.remove(str(body.get("id", "")))
                return 200, {"ok": True, "name": srv["name"], "note": self.reload_note()}
            if action == "general":
                self.general(body)
                return 200, {"ok": True, "note": self.reload_note()}
            if action == "install_steamcmd":
                self.install_steamcmd(body.get("folder", ""))
                return 202, {"ok": True}
        except ValueError as e:
            return 400, {"error": str(e)}
        return 400, {"error": "unknown action"}

    def reload_note(self):
        busy = self.mgr.restart_self()
        if busy:
            return "Saved. ServerDeck will load it after a restart - wait for these to finish, then press Restart ServerDeck: " + "; ".join(busy)
        return "Saved - ServerDeck is restarting to load it (the page reconnects in about 10 seconds)."
