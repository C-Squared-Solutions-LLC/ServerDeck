"""Per-game maintenance hooks: keep mods and plugins working across updates.

Hooks are referenced from config.json as [name, *args] under "pre_start" and
"post_update".  Each is called as fn(server, changed, force, *args) where
`changed` means SteamCMD just installed a different build.
"""
import json
import re
import secrets
import shutil
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

import winproc

UA = {"User-Agent": "ServerDeck/1.0"}
_version_cache = {}


def http_get(url, timeout=60):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def download(url, dest):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def github_release(repo, tag=None):
    path = f"tags/{tag}" if tag else "latest"
    return json.loads(http_get(f"https://api.github.com/repos/{repo}/releases/{path}", timeout=30))


def extract(zip_path, dest, skip=lambda rel, target: False):
    """Unzip into dest, refusing paths that escape it."""
    dest = Path(dest).resolve()
    with zipfile.ZipFile(zip_path) as z:
        for m in z.infolist():
            if m.is_dir():
                continue
            target = (dest / m.filename).resolve()
            if dest not in target.parents:
                raise RuntimeError(f"unsafe path in archive: {m.filename}")
            if skip(m.filename, target):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(m) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def assembly_version(path):
    """.NET assembly version ("1.0.373"), cached per file mtime."""
    p = Path(path)
    try:
        key = (str(p), p.stat().st_mtime)
    except OSError:
        return None
    if key not in _version_cache:
        ps = f"[Reflection.AssemblyName]::GetAssemblyName('{p}').Version.ToString()"
        r = winproc.run_hidden(["powershell.exe", "-NoProfile", "-Command", ps], timeout=60)
        parts = r.stdout.decode("utf-8", "replace").strip().split(".")
        if len(parts) == 4 and parts[3] == "0":
            parts = parts[:3]
        _version_cache[key] = ".".join(parts) if parts != [""] else None
    return _version_cache[key]


def marker(server, name, value=None):
    """Small version notes ServerDeck keeps next to an install (.serverdeck/)."""
    d = Path(server.cfg["install_dir"]) / ".serverdeck"
    f = d / name
    if value is not None:
        d.mkdir(exist_ok=True)
        f.write_text(value)
        return value
    return f.read_text().strip() if f.exists() else None


# --------------------------------------------------------------------------
# generic
# --------------------------------------------------------------------------

def remove(server, changed, force, rel):
    p = Path(server.cfg["install_dir"]) / rel
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)
        server.note(f"removed {rel}")


def has_avx2():
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))   # PF_AVX2_INSTRUCTIONS_AVAILABLE
    except Exception:
        return True


def remove_without_avx2(server, changed, force, rel):
    """Some game plugins (SCUM's SDSpeechTools) crash servers on CPUs without AVX2,
    e.g. AMD FX: remove them there, leave them alone everywhere else."""
    if not has_avx2():
        remove(server, changed, force, rel)


def rotate(server, changed, force, rel, max_mb=20, keep=3):
    """Logs that a server appends to forever (CS2's -condebug console.log):
    move them aside before a start once they pass max_mb."""
    p = Path(server.cfg["install_dir"]) / rel
    if not p.exists() or p.stat().st_size < max_mb * 1024 * 1024:
        return
    for i in range(keep - 1, 0, -1):
        older = p.with_name(f"{p.name}.{i}")
        if older.exists():
            older.replace(p.with_name(f"{p.name}.{i + 1}"))
    p.replace(p.with_name(f"{p.name}.1"))
    p.with_name(f"{p.name}.{keep + 1}").unlink(missing_ok=True)


# --------------------------------------------------------------------------
# CS2: Metamod:Source + CounterStrikeSharp (+ whatever plugins they load)
# --------------------------------------------------------------------------

def _csgo(server):
    return Path(server.cfg["install_dir"]) / "game" / "csgo"


def cs2_gameinfo(server, changed=False, force=False):
    """Every CS2 update rewrites gameinfo.gi and drops the Metamod search path,
    which silently stops Metamod - and with it CSS and its plugins - loading."""
    gi = _csgo(server) / "gameinfo.gi"
    raw = gi.read_bytes().decode("utf-8", "replace")
    if "csgo/addons/metamod" in raw:
        return
    eol = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(eol)
    for i, line in enumerate(lines):
        if re.match(r"^\s*Game_LowViolence\b", line):
            indent = re.match(r"^(\s*)", line).group(1)
            shutil.copy2(gi, str(gi) + ".bak")
            lines.insert(i + 1, f"{indent}Game\tcsgo/addons/metamod")
            gi.write_bytes(eol.join(lines).encode("utf-8"))
            server.note("gameinfo.gi: Metamod search path re-added")
            return
    raise RuntimeError("gameinfo.gi: SearchPaths anchor not found - Metamod will not load")


def cs2_metamod(server):
    base = "https://mms.alliedmods.net/mmsdrop/2.0"
    name = http_get(f"{base}/mmsource-latest-windows").decode().strip()
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / name
        download(f"{base}/{name}", archive)
        # Keep a customised metaplugins.ini; CSS registers itself via its own .vdf.
        extract(archive, _csgo(server), skip=lambda rel, t: rel.endswith("metaplugins.ini") and t.exists())
    version = name.replace("mmsource-", "").replace("-windows.zip", "")
    marker(server, "metamod", version)
    server.note(f"Metamod {version} installed")


def css_installed(server):
    return assembly_version(_csgo(server) / "addons" / "counterstrikesharp" / "api" / "CounterStrikeSharp.API.dll")


def cs2_css(server, version):
    rel = github_release("roflmuffin/CounterStrikeSharp", f"v{version}")
    asset = next(a for a in rel["assets"] if "with-runtime" in a["name"] and "windows" in a["name"])
    configs = "addons/counterstrikesharp/configs/"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / asset["name"]
        download(asset["browser_download_url"], archive)
        # Never overwrite the server's own CSS configs (only refresh *.example.json).
        extract(archive, _csgo(server),
                skip=lambda r, t: r.startswith(configs) and t.exists() and not r.endswith(".example.json"))
    server.note(f"CounterStrikeSharp {version} installed")


def cs2_addons(server, changed, force):
    csgo = _csgo(server)
    if changed or force or not (csgo / "addons" / "metamod").exists():
        cs2_metamod(server)          # Metamod's CS2 builds track game updates
    want = server.mgr.secret(server.cfg.get("css_version", ""))
    if want == "latest":
        # CS2 updates routinely break older CSS builds until CSS ships a fix,
        # so follow its releases; plugins built for an older API keep loading.
        want = github_release("roflmuffin/CounterStrikeSharp")["tag_name"].lstrip("v")
    if want and css_installed(server) != want:
        cs2_css(server, want)
    cs2_gameinfo(server)
    for name in (server.cfg.get("plugin_check") or {}).get("expect", []):
        if not (csgo / "addons" / "counterstrikesharp" / "plugins" / name / f"{name}.dll").exists():
            server.note(f"warning: the {name} plugin is missing from addons/counterstrikesharp/plugins")


def cs2_mods(server):
    return {"metamod": marker(server, "metamod") or "installed", "css": css_installed(server)}


# --------------------------------------------------------------------------
# Rust: Oxide (uMod)
# --------------------------------------------------------------------------

def _oxide_dll(server):
    return Path(server.cfg["install_dir"]) / "RustDedicated_Data" / "Managed" / "Oxide.Rust.dll"


def oxide_installed(server):
    return assembly_version(_oxide_dll(server)) if _oxide_dll(server).exists() else None


def rust_oxide(server, changed, force):
    """A game update replaces the Oxide-patched DLLs, so Oxide goes back on after
    every update; the Oxide zip only carries those DLLs, so oxide/plugins,
    config and data are never touched."""
    try:
        rel = github_release("OxideMod/Oxide.Rust")
        latest = rel["tag_name"]
        url = next(a["browser_download_url"] for a in rel["assets"] if a["name"] == "Oxide.Rust.zip")
    except Exception as e:
        if not (changed or force):
            server.note(f"Oxide release lookup failed ({e}); keeping the installed build")
            return
        latest, url = "latest", "https://github.com/OxideMod/Oxide.Rust/releases/latest/download/Oxide.Rust.zip"
    if not (changed or force or oxide_installed(server) != latest):
        return
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "Oxide.Rust.zip"
        download(url, archive)
        extract(archive, server.cfg["install_dir"])
    server.note(f"Oxide {oxide_installed(server) or latest} installed")


def rust_oxide_present(server, changed, force):
    if not _oxide_dll(server).exists():
        rust_oxide(server, True, False)


def rust_mods(server):
    return {"oxide": oxide_installed(server)}


# --------------------------------------------------------------------------
# SCUM: restart warnings through its own scheduled notifications
# --------------------------------------------------------------------------

WARN_MARKS_MIN = (60, 30, 15, 10, 5, 3, 1)


def warning_offsets(warn_minutes):
    """Minutes before a restart at which players are warned, e.g. 10 -> [10, 5, 3, 1]."""
    if warn_minutes <= 0:
        return []
    return sorted({warn_minutes} | {m for m in WARN_MARKS_MIN if m <= warn_minutes}, reverse=True)


def _scum_time(minutes):
    minutes %= 1440
    return f"{minutes // 60}:{minutes % 60:02d}"          # SCUM's own style: "5:50", "23:59"


def _scum_restart_label(minutes):
    h, m = divmod(minutes % 1440, 60)
    return f"00:{m:02d}" if h == 0 else f"{h}:{m:02d}"     # as in the original file: "6:00", "00:00"


def scum_notifications(server, restart_minutes, warn_minutes):
    """Rewrite the "#RestartAt(..)" entries of SCUM's Notifications.json for the
    given restart times (minutes after midnight), keeping every other
    notification. SCUM reads the file when it starts. Returns True if changed."""
    path = Path(server.cfg["install_dir"]) / "SCUM" / "Saved" / "Config" / "WindowsServer" / "Notifications.json"
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    data = json.loads(raw) if raw.strip() else {}
    entries = data.get("Notifications", [])
    ours = [n for n in entries if str(n.get("message", "")).startswith("#RestartAt(")]
    color = ours[0].get("color", "255-180-50") if ours else "255-180-50"
    entries = [n for n in entries if n not in ours]
    offsets = warning_offsets(warn_minutes)
    if offsets:
        for t in sorted(restart_minutes):
            entries.append({"color": color, "message": f"#RestartAt({_scum_restart_label(t)})",
                            "time": [_scum_time(t - o) for o in offsets]})
    data["Notifications"] = entries
    text = json.dumps(data, indent=4)
    if raw.replace("\r\n", "\n").strip() == text:
        return False
    backup = path.with_name("Notifications.json.before-serverdeck")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))   # same CRLF / no-BOM format as SCUM's
    return True


# --------------------------------------------------------------------------
# Palworld: PalWorldSettings.ini
# --------------------------------------------------------------------------

PAL_INI = Path("Pal") / "Saved" / "Config" / "WindowsServer" / "PalWorldSettings.ini"


def _read_text(path):
    """(text, encoding) of a settings file, line endings untouched (UTF-16 if it has that BOM)."""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    return raw.decode("utf-8-sig", errors="replace"), "utf-8"


def _pal_items(body):
    """'A=1,B="x,y",C=(P,Q)' -> [["A", "1"], ["B", '"x,y"'], ["C", "(P,Q)"]]: commas inside quotes
    or parentheses (CrossplayPlatforms=(Steam,Xbox,...)) don't split."""
    items, depth, quoted, start = [], 0, False, 0
    for i, ch in enumerate(body + ","):
        if ch == '"':
            quoted = not quoted
        elif quoted:
            continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            part = body[start:i].strip()
            if part:
                key, _, value = part.partition("=")
                items.append([key.strip(), value])
            start = i + 1
    return items


def palworld_settings(server, changed=False, force=False):
    """Palworld reads PalWorldSettings.ini - every setting on one "OptionSettings=(...)" line - which
    stays empty until someone copies DefaultPalWorldSettings.ini into it. Create it on the first start
    (server name, port and player cap from the Servers dialog) and before every start keep the REST API
    on that ServerDeck uses for player counts, warnings and clean stops (port + AdminPassword)."""
    install = Path(server.cfg["install_dir"])
    ini, api, first = install / PAL_INI, server.cfg["rest_api"], server.cfg.get("palworld", {})
    password = server.mgr.secret(api["password"])
    if not password or '"' in password:
        raise ValueError("the AdminPassword in config.json must be set and can't contain a double quote")
    want = {"RESTAPIEnabled": "True", "RESTAPIPort": str(int(api["port"])), "AdminPassword": f'"{password}"'}
    text, enc = _read_text(ini) if ini.exists() else ("", "utf-8")
    if "OptionSettings=(" not in text:
        default = install / "DefaultPalWorldSettings.ini"
        text, enc = _read_text(default) if default.exists() else ("", "utf-8")
        if "OptionSettings=(" not in text:
            text = "[/Script/Pal.PalGameWorldSettings]\r\nOptionSettings=()\r\n"
        name = str(first.get("server_name") or "Palworld Server").replace('"', "'")
        want.update(ServerName=f'"{name}"', PublicPort=str(int(first.get("port") or 8211)),
                    ServerPlayerMaxNum=str(int(first.get("max_players") or 32)))
        server.note("PalWorldSettings.ini created from DefaultPalWorldSettings.ini")
    m = re.search(r"^\s*OptionSettings=\((.*)\)\s*$", text, re.M)
    items = _pal_items(m.group(1))
    index = {k: i for i, (k, _) in enumerate(items)}
    for k, v in want.items():
        if k in index:
            items[index[k]][1] = v
        else:
            items.append([k, v])
    new = text[:m.start(1)] + ",".join(f"{k}={v}" for k, v in items) + text[m.end(1):]
    if new != text or not ini.exists():
        ini.parent.mkdir(parents=True, exist_ok=True)
        ini.write_bytes(new.encode(enc))


# --------------------------------------------------------------------------
# Enshrouded: enshrouded_server.json
# --------------------------------------------------------------------------

# The game's own groups and permissions; the password a player joins with picks the group.
ENSHROUDED_GROUPS = (
    ("Admin", dict(canKickBan=True, canAccessInventories=True, canEditWorld=True, canEditBase=True,
                   canExtendBase=True)),
    ("Friend", dict(canKickBan=False, canAccessInventories=True, canEditWorld=True, canEditBase=True,
                    canExtendBase=False)),
    ("Guest", dict(canKickBan=False, canAccessInventories=False, canEditWorld=True, canEditBase=False,
                   canExtendBase=False)),
    ("Visitor", dict(canKickBan=False, canAccessInventories=False, canEditWorld=False, canEditBase=False,
                     canExtendBase=False)),
)


def enshrouded_config(server, changed=False, force=False):
    """Enshrouded keeps its settings in enshrouded_server.json, and the one it writes itself on the
    first start has no passwords. So write it before the first start - name, port, slots and group
    passwords (Guest's may stay empty: then anyone can join as a guest) - and afterwards only keep the
    port in step with ServerDeck's firewall rule and query; edit the rest under Configs."""
    c = server.cfg.get("enshrouded", {})
    path = Path(server.cfg["install_dir"]) / "enshrouded_server.json"
    port = int(c.get("port") or 15637)
    if not path.exists():
        pw = {name: server.mgr.secret(c.get(f"{name.lower()}_password") or "") for name, _ in ENSHROUDED_GROUPS}
        for name in ("Admin", "Friend", "Visitor"):
            pw[name] = pw[name] or secrets.token_urlsafe(12)
        data = {"name": c.get("server_name") or "Enshrouded Server", "saveDirectory": "./savegame",
                "logDirectory": "./logs", "ip": "0.0.0.0", "queryPort": port, "slotCount": int(c.get("slots") or 16),
                "gameSettingsPreset": "Default",
                "userGroups": [{"name": name, "password": pw[name], **perms, "reservedSlots": 0}
                               for name, perms in ENSHROUDED_GROUPS]}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        server.note("enshrouded_server.json written: name, port, slots and group passwords")
        return
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("queryPort") != port:
        data["queryPort"] = port
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        server.note(f"enshrouded_server.json: queryPort set to {port}, the port ServerDeck opened and queries")
    for g in data.get("userGroups", []):
        pw = str(g.get("password") or "")
        if g.get("canKickBan") and (not pw or re.fullmatch(r"[A-Za-z]*X{6,}", pw)):
            server.mgr.events.add("warn", server.id, f"enshrouded_server.json: the {g.get('name')} group can "
                                  "kick and ban but has no real password - set one under Configs")


HOOKS = {
    "remove": remove,
    "remove_without_avx2": remove_without_avx2,
    "rotate": rotate,
    "cs2_gameinfo": cs2_gameinfo,
    "cs2_addons": cs2_addons,
    "rust_oxide": rust_oxide,
    "rust_oxide_present": rust_oxide_present,
    "palworld_settings": palworld_settings,
    "enshrouded_config": enshrouded_config,
}
MODS = {"cs2": cs2_mods, "rust": rust_mods}
