"""ServerDeck's hand on the user's desktop. ServerDeck runs in session 0 (it works with nobody logged on), but a
game that needs the Steam client (Farming Simulator's Steam version) has to run on the desktop, where Steam is.
Windows only lets a program reach that desktop through Task Scheduler: winproc registers the task "ServerDeck
desktop helper" (this user, "only when logged on", not elevated) and runs it after writing desktop_request.json;
this script does what it asks and answers in desktop_result.json.

    {"id", "action": "start", "cmd": [...] | "line", "cwd", "env": {...}, "hidden": true, "wait": seconds}
        -> {"id", "ok", "pid", "code"}: the program in a new (hidden) console of its own, like ServerDeck's own
           servers - outside the task's job, so it keeps running when this script ends
    {"id", "action": "ctrl_c", "pid"} -> {"id", "ok"}: Ctrl+C to that program's console (a clean stop)
"""
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CREATE_NEW_CONSOLE, CREATE_BREAKAWAY_FROM_JOB = 0x00000010, 0x01000000
STARTF_USESHOWWINDOW, SW_HIDE, SW_SHOWMINNOACTIVE = 0x00000001, 0, 7


def start(req):
    si = subprocess.STARTUPINFO()
    si.dwFlags |= STARTF_USESHOWWINDOW
    si.wShowWindow = SW_HIDE if req.get("hidden", True) else SW_SHOWMINNOACTIVE
    kw = dict(cwd=req.get("cwd") or None, startupinfo=si, close_fds=True)
    if req.get("env"):
        kw["env"] = {**os.environ, **req["env"]}
    try:
        p = subprocess.Popen(req["cmd"], creationflags=CREATE_NEW_CONSOLE | CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError:
        p = subprocess.Popen(req["cmd"], creationflags=CREATE_NEW_CONSOLE, **kw)
    out = {"ok": True, "pid": p.pid, "code": None}
    if req.get("wait"):
        try:
            out["code"] = p.wait(req["wait"])
        except subprocess.TimeoutExpired:
            pass
    return out


def ctrl_c(req):
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.FreeConsole()
    if not k.AttachConsole(int(req["pid"])):
        return {"ok": False, "error": f"can't attach to its console (error {ctypes.get_last_error()})"}
    k.SetConsoleCtrlHandler(None, True)
    ok = bool(k.GenerateConsoleCtrlEvent(0, 0))
    time.sleep(0.5)
    k.FreeConsole()
    return {"ok": ok}


def main():
    req = json.loads((HERE / "desktop_request.json").read_text(encoding="utf-8"))
    try:
        out = {"start": start, "ctrl_c": ctrl_c}[req["action"]](req)
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    out.update(id=req.get("id"), session=_session())
    tmp = HERE / "desktop_result.json.tmp"
    tmp.write_text(json.dumps(out), encoding="utf-8")
    tmp.replace(HERE / "desktop_result.json")


def _session():
    s = ctypes.c_ulong()
    return s.value if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(s)) else None


if __name__ == "__main__":
    sys.exit(main())
