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


def client_state(install_dir, appid):
    """A game the Steam client installs (Farming Simulator): what its appmanifest in the library around install_dir
    (...\\steamapps\\common\\<game>) says - {"installed", "state", "percent", "done", "total"} - or None when there
    is no such manifest (not a Steam library, or Steam hasn't started on it)."""
    p = Path(install_dir)
    if p.parent.name.lower() != "common" or p.parent.parent.name.lower() != "steamapps":
        return None
    try:
        text = (p.parent.parent / f"appmanifest_{int(appid)}.acf").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    num = lambda key: int((re.search(r'"%s"\s+"(\d+)"' % key, text) or [0, "0"])[1])   # noqa: E731
    flags = num("StateFlags")
    estimated = False
    to_dl, dl, to_stage, staged = num("BytesToDownload"), num("BytesDownloaded"), num("BytesToStage"), num("BytesStaged")
    # EAppState: 4 fully installed; busy = update running / paused / started, reconfiguring, validating,
    # adding files, preallocating, downloading, staging, committing
    busy = 256 | 512 | 1024 | 65536 | 131072 | 262144 | 524288 | 1048576 | 2097152 | 4194304
    if flags & 4 and not flags & busy:
        return {"installed": True, "state": "installed", "percent": 100.0, "done": None, "total": None}
    if to_dl and dl < to_dl:
        state, done, total = "downloading", dl, to_dl
        est = _log_progress(p.parent.parent.parent, appid)   # the manifest's counters only move on pause / finish
        if est and est[1] == to_dl and est[0] > dl:
            done, estimated = est[0], True
    elif to_stage and staged < to_stage:
        state, done, total = ("installing", staged, to_stage) if staged else ("installing", None, None)
    else:
        state, done, total = ("queued" if not to_dl else "finishing"), None, None
    return {"installed": bool(flags & 4), "state": state, "done": done, "total": total, "estimated": estimated,
            "percent": round(done / total * 100, 1) if total else None}


_LOG_LINE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] (.*)$")


def _log_progress(steam_root, appid):
    """(downloaded, total) bytes of a Steam client download, estimated from logs\\content_log.txt: its
    "update started : download a/b" line, then the "Current download rate: N Mbps" it logs every minute added up
    (Steam rewrites the appmanifest's counters only when it pauses or finishes). None if the log doesn't say."""
    from datetime import datetime
    path = Path(steam_root) / "logs" / "content_log.txt"
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 1_000_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    start = None
    for i, ln in enumerate(lines):
        if re.search(r"AppID %d update started : download (\d+)/(\d+)" % int(appid), ln):
            start = i
    if start is None:
        return None
    m = re.search(r"download (\d+)/(\d+)", lines[start])
    done, total = int(m.group(1)), int(m.group(2))
    ts = lambda ln: datetime.strptime(_LOG_LINE.match(ln).group(1), "%Y-%m-%d %H:%M:%S").timestamp()   # noqa: E731
    last_t, rate = ts(lines[start]), None
    for ln in lines[start + 1:]:
        if not _LOG_LINE.match(ln):
            continue
        if re.search(r"AppID %d (update canceled|update paused|update finished|App update changed : None)" % int(appid), ln):
            return None if "finished" not in ln else (total, total)
        r = re.search(r"Current download rate: ([\d.]+) Mbps", ln)
        if r:
            t = ts(ln)
            done += float(r.group(1)) * 125_000 * (t - last_t)      # the rate it logs is the minute's average
            last_t, rate = t, float(r.group(1))
    if rate:
        done += rate * 125_000 * min(70, max(0, time.time() - last_t))
    return int(min(done, total * 0.99)), total


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


PROGRESS = re.compile(r"Update state \(0x[0-9a-fA-F]+\) ([a-z][a-z ]*), progress: ([\d.]+) \((\d+) / (\d+)\)")


def _watch(log_path, pos, stop, progress):
    """Hand SteamCMD's newest "Update state (0x61) downloading, progress: 45.23 (a / b)" line to
    progress(state, percent, done_bytes, total_bytes) every 2 s while it runs."""
    carry, last = "", None
    while not stop.wait(2):
        try:
            with open(log_path, "rb") as f:
                f.seek(pos)
                chunk = f.read()
        except OSError:
            continue
        pos += len(chunk)
        text = carry + chunk.decode("utf-8", "replace")
        carry = text[text.rfind("\n") + 1:]                 # a line SteamCMD hasn't finished writing
        found = [m for m in PROGRESS.findall(text[:len(text) - len(carry)]) if int(m[3])]
        if found and found[-1] != last:
            last = found[-1]
            try:
                progress(last[0], float(last[1]), int(last[2]), int(last[3]))
            except Exception:
                pass


def app_update(steamcmd, appid, log_path, validate=True, install_dir=None, timeout=7200, attempts=3, progress=None):
    """Run SteamCMD app_update, retrying the transient "state is 0x..." failures
    SteamCMD is known for. Returns (ok, tail_of_output). progress(state, percent, done, total)
    hears how far the download / verification is while it runs."""
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
                stop = threading.Event()
                watcher = threading.Thread(target=_watch, args=(log_path, start, stop, progress), daemon=True,
                                           name=f"steamcmd-progress-{appid}") if progress else None
                if watcher:
                    watcher.start()
                try:
                    winproc.run_hidden(args, timeout=timeout, cwd=str(Path(steamcmd).parent), capture_output=False,
                                       stdout=log, stderr=log)
                finally:
                    stop.set()
                    if watcher:
                        watcher.join(5)
        with open(log_path, "rb") as log:
            log.seek(start)
            text = log.read().decode("utf-8", "replace")[-8000:]
        ok = bool(re.search(r"Success! App '%d' (fully installed|already up to date)" % appid, text))
        if ok:
            break
    return ok, text
