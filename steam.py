"""SteamCMD helpers: read installed build ids, look up the latest public build,
and run app_update."""
import json
import re
import threading
import time
import urllib.request
from pathlib import Path

import winproc

# One SteamCMD install can only run one instance at a time.
steamcmd_lock = threading.Lock()


def installed_build(manifest_path):
    try:
        text = Path(manifest_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'"buildid"\s+"(\d+)"', text)
    return m.group(1) if m else None


def _tokens(text, pos):
    tok = re.compile(r'\s*(?:"((?:[^"\\]|\\.)*)"|(\{)|(\}))', re.S)
    while True:
        m = tok.match(text, pos)
        if not m:
            return
        pos = m.end()
        if m.group(2):
            yield "{", pos
        elif m.group(3):
            yield "}", pos
        else:
            yield ("str", m.group(1)), pos


def parse_vdf_block(text, start):
    """Parse the KeyValues object whose '{' is at/after `start`."""
    stack = [{}]
    key = None
    first = True
    for tok, _pos in _tokens(text, start):
        if first:
            first = False
            if tok != "{":
                return None
            continue
        if tok == "{":
            child = {}
            stack[-1][key] = child
            stack.append(child)
            key = None
        elif tok == "}":
            done = stack.pop()
            if not stack:
                return done
        else:
            if key is None:
                key = tok[1]
            else:
                stack[-1][key] = tok[1]
                key = None
    return None


def _public_build_from_appinfo(text, appid):
    m = re.search(r'"%d"\s*\{' % appid, text)
    if not m:
        return None
    block = parse_vdf_block(text, m.end() - 1)
    try:
        return block["depots"]["branches"]["public"]["buildid"]
    except (KeyError, TypeError):
        return None


def latest_builds_steamcmd(steamcmd, appids, timeout=300):
    args = [steamcmd, "+login", "anonymous", "+app_info_update", "1"]
    for a in appids:
        args += ["+app_info_print", str(a)]
    args += ["+quit"]
    with steamcmd_lock:
        r = winproc.run_hidden(args, timeout=timeout, cwd=str(Path(steamcmd).parent))
    text = r.stdout.decode("utf-8", "replace")
    return {a: _public_build_from_appinfo(text, a) for a in appids}


def latest_build_web(appid, timeout=15):
    """Fallback: the community steamcmd.net mirror of the same app info."""
    req = urllib.request.Request(
        f"https://api.steamcmd.net/v1/info/{appid}", headers={"User-Agent": "ServerDeck/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return data["data"][str(appid)]["depots"]["branches"]["public"]["buildid"]


def latest_builds(steamcmd, appids):
    result = {}
    try:
        result = latest_builds_steamcmd(steamcmd, appids)
    except Exception:
        result = {}
    for a in appids:
        if not result.get(a):
            try:
                result[a] = latest_build_web(a)
            except Exception:
                result[a] = None
    return result


def app_update(steamcmd, appid, log_path, validate=True, install_dir=None, timeout=7200, attempts=3):
    """Run SteamCMD app_update, retrying the transient "state is 0x..." failures
    SteamCMD is known for. Returns (ok, tail_of_output)."""
    args = [steamcmd]
    if install_dir:
        args += ["+force_install_dir", str(install_dir)]
    args += ["+login", "anonymous", "+app_update", str(appid)]
    if validate:
        args.append("validate")
    args.append("+quit")
    ok, text = False, ""
    for attempt in range(attempts):
        if attempt:
            time.sleep(15)
        with steamcmd_lock:
            with open(log_path, "ab") as log:
                start = log.tell()
                log.write(f"\n===== ServerDeck: app_update {appid} attempt {attempt + 1} =====\n".encode())
                log.flush()
                winproc.run_hidden(args, timeout=timeout, cwd=str(Path(steamcmd).parent), capture_output=False,
                                   stdout=log, stderr=log)
        with open(log_path, "rb") as log:
            log.seek(start)
            text = log.read().decode("utf-8", "replace")[-8000:]
        ok = bool(re.search(r"Success! App '%d' (fully installed|already up to date)" % appid, text))
        if ok:
            break
    return ok, text
