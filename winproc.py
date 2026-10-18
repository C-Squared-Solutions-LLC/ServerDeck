"""Thin ctypes wrappers over the Win32 process / network APIs ServerDeck needs.

Kept dependency-free on purpose (no psutil): the manager runs from a scheduled
task and should keep working after Python package upgrades.
"""
import ctypes
import json
import ctypes.wintypes as wt
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
iphlp = ctypes.WinDLL("iphlpapi", use_last_error=True)

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
DETACHED_PROCESS = 0x00000008
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0


class FILETIME(ctypes.Structure):
    _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]

    def value(self):
        return (self.hi << 32) | self.lo


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wt.DWORD),
        ("dwMemoryLoad", wt.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.Process32FirstW.restype = wt.BOOL
k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.Process32NextW.restype = wt.BOOL
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.CloseHandle.restype = wt.BOOL
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE
k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
k32.GetProcessTimes.restype = wt.BOOL
k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
k32.QueryFullProcessImageNameW.restype = wt.BOOL
k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
k32.GetExitCodeProcess.restype = wt.BOOL
k32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
k32.TerminateProcess.restype = wt.BOOL
k32.K32GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), wt.DWORD]
k32.K32GetProcessMemoryInfo.restype = wt.BOOL
k32.GetSystemTimes.argtypes = [ctypes.POINTER(FILETIME)] * 3
k32.GetSystemTimes.restype = wt.BOOL
k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
k32.GlobalMemoryStatusEx.restype = wt.BOOL
iphlp.GetExtendedUdpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD), wt.BOOL, wt.ULONG, ctypes.c_int, wt.ULONG]
iphlp.GetExtendedUdpTable.restype = wt.DWORD
iphlp.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD), wt.BOOL, wt.ULONG, ctypes.c_int, wt.ULONG]
iphlp.GetExtendedTcpTable.restype = wt.DWORD

EPOCH_AS_FILETIME = 116444736000000000
CPU_COUNT = os.cpu_count() or 1


def list_processes():
    """[(pid, ppid, exe_name)] for every process on the box."""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return []
    out = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def find_pids(exe_name):
    """Processes by image name; a name with * or ? is a pattern (e.g. "ConanSandboxServer-Win64-*.exe")."""
    name = exe_name.lower()
    if "*" in name or "?" in name:
        import fnmatch
        return [pid for pid, _, exe in list_processes() if fnmatch.fnmatch(exe.lower(), name)]
    return [pid for pid, _, exe in list_processes() if exe.lower() == name]


def _open(pid, access=PROCESS_QUERY_LIMITED_INFORMATION):
    h = k32.OpenProcess(access, False, pid)
    return h or None


def image_path(pid):
    h = _open(pid)
    if not h:
        return None
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        k32.CloseHandle(h)


def is_alive(pid):
    if not pid:
        return False
    h = _open(pid)
    if not h:
        return False
    try:
        code = wt.DWORD()
        return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def process_stats(pid):
    """(create_time_epoch, cpu_seconds, private_bytes, working_set) or None."""
    h = _open(pid)
    if not h:
        return None
    try:
        c, e, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
            return None
        mem = PROCESS_MEMORY_COUNTERS_EX()
        mem.cb = ctypes.sizeof(mem)
        k32.K32GetProcessMemoryInfo(h, ctypes.byref(mem), mem.cb)
        created = (c.value() - EPOCH_AS_FILETIME) / 1e7
        cpu = (kt.value() + ut.value()) / 1e7
        return created, cpu, mem.PrivateUsage, mem.WorkingSetSize
    finally:
        k32.CloseHandle(h)


def terminate(pid, code=1):
    h = _open(pid, PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return False
    try:
        return bool(k32.TerminateProcess(h, code))
    finally:
        k32.CloseHandle(h)


def kill_tree(pid):
    """Terminate a process and its descendants (e.g. a venv's python.exe is a
    launcher that runs the real interpreter as a child)."""
    root = process_stats(pid)
    children = {}
    for p, ppid, _ in list_processes():
        children.setdefault(ppid, []).append(p)
    order, stack = [], [pid]
    while stack:
        p = stack.pop()
        order.append(p)
        for c in children.get(p, []):
            cs = process_stats(c)
            # Guard against PID reuse: a real child can't predate its parent.
            if c not in order and (root is None or cs is None or cs[0] >= root[0] - 1):
                stack.append(c)
    for p in reversed(order):
        terminate(p)
    for p in order:
        wait_exit(p, 10)


def wait_exit(pid, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(1)
    return not is_alive(pid)


# Runs in a throwaway process: attach to the server's console and raise Ctrl+C
# there.  Unreal/Source servers treat that as a clean "save and quit".
_CTRL_C_HELPER = r"""
import ctypes, sys, time
k = ctypes.WinDLL('kernel32', use_last_error=True)
pid = int(sys.argv[1])
k.FreeConsole()
if not k.AttachConsole(pid):
    sys.exit(2)
k.SetConsoleCtrlHandler(None, True)
ok = k.GenerateConsoleCtrlEvent(0, 0)
time.sleep(0.5)
k.FreeConsole()
sys.exit(0 if ok else 3)
"""


def send_ctrl_c(pid):
    """Ask a console process to shut down gracefully. True if the event was sent."""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[:-len("pythonw.exe")] + "python.exe"
    if session_of(pid) not in (None, session_of(os.getpid())):
        # A console in another session (a game on the user's desktop) can only be reached from there.
        try:
            return bool(desktop_request({"action": "ctrl_c", "pid": pid}).get("ok"))
        except OSError:
            return False
    try:
        r = subprocess.run(
            [exe, "-c", _CTRL_C_HELPER, str(pid)],
            creationflags=DETACHED_PROCESS,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=20,
        )
        return r.returncode == 0
    except Exception:
        return False


def in_job():
    flag = wt.BOOL()
    k32.IsProcessInJob(k32.GetCurrentProcess(), None, ctypes.byref(flag))
    return bool(flag.value)


k32.IsProcessInJob.argtypes = [wt.HANDLE, wt.HANDLE, ctypes.POINTER(wt.BOOL)]
k32.GetCurrentProcess.restype = wt.HANDLE
k32.GetPriorityClass.argtypes = [wt.HANDLE]
k32.GetPriorityClass.restype = wt.DWORD
k32.SetPriorityClass.argtypes = [wt.HANDLE, wt.DWORD]
k32.GetProcessInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
k32.SetProcessInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
ntdll = ctypes.WinDLL("ntdll")
ntdll.NtQueryInformationProcess.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.ULONG, ctypes.c_void_p]
ntdll.NtQueryInformationProcess.restype = ctypes.c_long
ntdll.NtSetInformationProcess.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.ULONG]
ntdll.NtSetInformationProcess.restype = ctypes.c_long
NORMAL_PRIORITY_CLASS = 0x20
LOW_PRIORITY_CLASSES = (0x40, 0x4000)       # idle, below normal
PROCESS_MEMORY_PRIORITY = 0                 # GetProcessInformation class; 5 = normal
PROCESS_IO_PRIORITY = 33                    # NtQueryInformationProcess class; 2 = normal


def priorities():
    """This process's (CPU priority class, memory priority 1-5, I/O priority 0-3)."""
    h = k32.GetCurrentProcess()
    mem, io = wt.ULONG(), wt.ULONG()
    k32.GetProcessInformation(h, PROCESS_MEMORY_PRIORITY, ctypes.byref(mem), ctypes.sizeof(mem))
    ntdll.NtQueryInformationProcess(h, PROCESS_IO_PRIORITY, ctypes.byref(io), ctypes.sizeof(io), None)
    return k32.GetPriorityClass(h), mem.value, io.value


def normal_priority():
    """Task Scheduler starts tasks below normal (its default priority 7 lowers CPU, disk and memory
    priority) and every process started from here inherits that - the game servers too. Raise this
    process to normal where it is lower (never lowers it). Returns priorities() before and after."""
    before = priorities()
    h = k32.GetCurrentProcess()
    if before[0] in LOW_PRIORITY_CLASSES:
        k32.SetPriorityClass(h, NORMAL_PRIORITY_CLASS)
    if before[1] < 5:
        mem = wt.ULONG(5)
        k32.SetProcessInformation(h, PROCESS_MEMORY_PRIORITY, ctypes.byref(mem), ctypes.sizeof(mem))
    if before[2] < 2:
        io = wt.ULONG(2)
        ntdll.NtSetInformationProcess(h, PROCESS_IO_PRIORITY, ctypes.byref(io), ctypes.sizeof(io))
    return before, priorities()
k32.QueryInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
k32.QueryInformationJobObject.restype = wt.BOOL


def job_limits():
    """LimitFlags of the job this process runs in (JOBOBJECT_BASIC_LIMIT_INFORMATION)."""
    buf = ctypes.create_string_buffer(64)
    if not k32.QueryInformationJobObject(None, 2, buf, len(buf), None):  # JobObjectBasicLimitInformation
        return None
    flags = int.from_bytes(buf.raw[16:20], "little")
    return {"flags": hex(flags), "breakaway_ok": bool(flags & 0x800), "silent_breakaway": bool(flags & 0x1000),
            "kill_on_close": bool(flags & 0x2000)}


def launch_console_hidden(cmd, cwd, env=None):
    """Start a console program in its own hidden console (so Ctrl+C can reach it
    later), detached from this process and - where Windows allows it - outside
    the scheduled task's job object, so ending/restarting ServerDeck can never
    take the game server down with it. `env`: extra environment variables.
    Its standard input and output are that console's: a server that polls its console
    for typed commands (CS2, Source) can't do that on NUL - CS2 logged
    "CTextConsoleWin::GetLine: !GetNumberOfConsoleInputEvents" every tick.
    Returns (pid, broke_away)."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= STARTF_USESHOWWINDOW
    si.wShowWindow = SW_HIDE
    kw = dict(cwd=cwd, startupinfo=si, close_fds=True)
    if env:
        kw["env"] = {**os.environ, **env}
    try:
        p = subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE | CREATE_BREAKAWAY_FROM_JOB, **kw)
        return p.pid, True
    except OSError:
        p = subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE, **kw)
        return p.pid, False


# --------------------------------------------------------------------------
# The logged-on user's desktop. ServerDeck runs in session 0 (a scheduled task that works with nobody logged
# on); the Steam client runs on the user's desktop. A game that needs the Steam client (Farming Simulator's
# Steam version) has to run there as well. Starting it with a copy of Explorer's token (CreateProcessWithTokenW)
# doesn't work - it still lands in session 0 and dies with 0xC0000142 - so a scheduled task "only when the user
# is logged on" does it: deskhelper.py.
# --------------------------------------------------------------------------

advapi = ctypes.WinDLL("advapi32", use_last_error=True)
PROCESS_QUERY_INFORMATION = 0x0400
TOKEN_QUERY = 0x0008


k32.ProcessIdToSessionId.argtypes = [wt.DWORD, ctypes.POINTER(wt.DWORD)]
k32.ProcessIdToSessionId.restype = wt.BOOL
k32.WTSGetActiveConsoleSessionId.argtypes = []
k32.WTSGetActiveConsoleSessionId.restype = wt.DWORD
k32.GetCurrentProcess.restype = wt.HANDLE
advapi.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
advapi.OpenProcessToken.restype = wt.BOOL
advapi.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
advapi.GetTokenInformation.restype = wt.BOOL
advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
advapi.ConvertSidToStringSidW.restype = wt.BOOL
ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]


def session_of(pid):
    """Windows session a process runs in (0 = services, ServerDeck's own), or None."""
    s = wt.DWORD()
    return s.value if pid and k32.ProcessIdToSessionId(pid, ctypes.byref(s)) else None


def _token_user(token):
    size = wt.DWORD()
    advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))           # TokenUser
    buf = ctypes.create_string_buffer(size.value)
    if not advapi.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
        return None
    sid = ctypes.c_void_p.from_buffer(buf).value                                 # TOKEN_USER.User.Sid
    text = wt.LPWSTR()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        return None
    try:
        return text.value
    finally:
        ctypes.windll.kernel32.LocalFree(text)


def _process_token(pid, access):
    h = _open(pid, PROCESS_QUERY_INFORMATION) if pid else None
    if pid and not h:
        return None
    tok = wt.HANDLE()
    ok = advapi.OpenProcessToken(h or k32.GetCurrentProcess(), access, ctypes.byref(tok))
    if h:
        k32.CloseHandle(h)
    return tok if ok else None


def desktop_session():
    """(session, explorer pid) of this user's desktop - the one at the console if there are several - or None
    when they aren't logged on to Windows."""
    me = _process_token(None, TOKEN_QUERY)
    user = _token_user(me) if me else None
    if me:
        k32.CloseHandle(me)
    console, found = k32.WTSGetActiveConsoleSessionId(), []
    for pid in find_pids("explorer.exe"):
        s = session_of(pid)
        tok = _process_token(pid, TOKEN_QUERY) if s else None
        if tok:
            if _token_user(tok) == user:
                found.append((s != console, s, pid))
            k32.CloseHandle(tok)
    return min(found)[1:] if found else None


def running_on_desktop(image):
    """pids of `image` running on this user's desktop."""
    d = desktop_session()
    return [pid for pid in find_pids(image) if d and session_of(pid) == d[0]]


DESKTOP_TASK = "ServerDeck desktop helper"
_desktop_lock = threading.Lock()


def _desktop_task():
    """Register deskhelper.py as a scheduled task for this user that runs only while they're logged on - which
    puts it on their desktop, not elevated (the only way Windows lets session 0 start something there)."""
    here = Path(__file__).resolve().parent
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe") if py.with_name("pythonw.exe").exists() else py
    q = run_hidden(["schtasks", "/query", "/tn", DESKTOP_TASK, "/xml"], timeout=30)
    if q.returncode == 0 and str(here / "deskhelper.py").lower() in q.stdout.decode("utf-16", "replace").lower():
        return
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".strip("\\")
    ps = (f"$a = New-ScheduledTaskAction -Execute '{pyw}' -Argument '\"{here / 'deskhelper.py'}\"' -WorkingDirectory '{here}';"
          f"$p = New-ScheduledTaskPrincipal -UserId '{user}' -LogonType Interactive -RunLevel Limited;"
          "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
          "-ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances Queue;"
          f"Register-ScheduledTask -TaskName '{DESKTOP_TASK}' -Action $a -Principal $p -Settings $s -Force | Out-Null")
    r = run_hidden(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=60)
    if r.returncode != 0:
        raise OSError("couldn't register the desktop helper task: " + r.stderr.decode("utf-8", "replace").strip()[-300:])


def desktop_request(req, timeout=30):
    """Hand deskhelper.py one request on the user's desktop and return its answer (dict). OSError when nobody is
    logged on or it doesn't answer."""
    if not desktop_session():
        raise OSError("nobody is logged on to Windows")
    here = Path(__file__).resolve().parent
    with _desktop_lock:
        _desktop_task()
        req = dict(req, id=f"{os.getpid()}-{time.time()}")
        result = here / "desktop_result.json"
        result.unlink(missing_ok=True)
        tmp = here / "desktop_request.json.tmp"
        tmp.write_text(json.dumps(req), encoding="utf-8")
        tmp.replace(here / "desktop_request.json")
        r = run_hidden(["schtasks", "/run", "/tn", DESKTOP_TASK], timeout=30)
        if r.returncode != 0:
            raise OSError("couldn't run the desktop helper task: " + r.stdout.decode("utf-8", "replace").strip()[-200:])
        deadline = time.time() + timeout + (req.get("wait") or 0)
        while time.time() < deadline:
            time.sleep(0.3)
            try:
                out = json.loads(result.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if out.get("id") == req["id"]:
                return out
        raise OSError("the desktop helper didn't answer - is Windows logged on?")


def launch_in_desktop(cmd, cwd, env=None, hidden=True, wait=None):
    """Start a program on this user's desktop in a new (hidden) console, through deskhelper.py. `wait`: seconds
    to wait for it to exit. Returns (pid, exit code or None)."""
    out = desktop_request({"action": "start", "cmd": cmd, "cwd": cwd, "env": env, "hidden": hidden, "wait": wait})
    if not out.get("ok"):
        raise OSError(out.get("error") or "the desktop helper couldn't start it")
    return out["pid"], out.get("code")


def steam_client():
    """The Steam client on this user's desktop: {"running", "signed_in", "exe"} (registry + process list)."""
    import winreg
    out = {"running": False, "signed_in": False, "exe": None}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            out["exe"] = str(winreg.QueryValueEx(k, "SteamExe")[0]).replace("/", "\\")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess") as k:
            pid = int(winreg.QueryValueEx(k, "pid")[0] or 0)
            user = int(winreg.QueryValueEx(k, "ActiveUser")[0] or 0)
    except OSError:
        return out
    out["running"] = bool(running_on_desktop("steam.exe"))
    out["signed_in"] = out["running"] and bool(user) and pid in running_on_desktop("steam.exe")
    return out


def spawn_detached(cmd):
    """Fire-and-forget helper process, also outside our job if possible."""
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    try:
        subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError:
        subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW, **kw)


def run_hidden(cmd, timeout=60, **kw):
    """subprocess.run without flashing a console window (we run under pythonw)."""
    kw.setdefault("stdin", subprocess.DEVNULL)
    kw.setdefault("capture_output", True)
    return subprocess.run(cmd, creationflags=CREATE_NO_WINDOW, timeout=timeout, **kw)


def listening_ports():
    """{('udp'|'tcp', port): pid} for every IPv4 UDP socket / TCP listener."""
    out = {}
    for proto, fn, table_class, row_words, port_idx, pid_idx in (
        ("udp", iphlp.GetExtendedUdpTable, 1, 3, 1, 2),   # UDP_TABLE_OWNER_PID
        ("tcp", iphlp.GetExtendedTcpTable, 3, 6, 2, 5),   # TCP_TABLE_OWNER_PID_LISTENER
    ):
        size = wt.DWORD(0)
        fn(None, ctypes.byref(size), False, socket.AF_INET, table_class, 0)
        buf = ctypes.create_string_buffer(size.value + 4096)
        size = wt.DWORD(len(buf))
        if fn(buf, ctypes.byref(size), False, socket.AF_INET, table_class, 0) != 0:
            continue
        words = (wt.DWORD * (len(buf) // 4)).from_buffer(buf)
        count = words[0]
        for i in range(count):
            base = 1 + i * row_words
            port = socket.ntohs(words[base + port_idx] & 0xFFFF)
            out[(proto, port)] = words[base + pid_idx]
    return out


class CpuSampler:
    """Turns cumulative CPU seconds into a % (of the whole machine) between calls."""

    def __init__(self):
        self._last = {}

    def percent(self, key, cpu_seconds):
        now = time.monotonic()
        prev = self._last.get(key)
        self._last[key] = (now, cpu_seconds)
        if not prev or now <= prev[0]:
            return None
        return max(0.0, (cpu_seconds - prev[1]) / (now - prev[0]) / CPU_COUNT * 100.0)


_sys_last = None


def system_stats():
    global _sys_last
    idle, kern, user = FILETIME(), FILETIME(), FILETIME()
    k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
    sample = (idle.value(), kern.value() + user.value())
    cpu = None
    if _sys_last:
        d_idle = sample[0] - _sys_last[0]
        d_total = sample[1] - _sys_last[1]
        if d_total > 0:
            cpu = max(0.0, min(100.0, (1 - d_idle / d_total) * 100))
    _sys_last = sample
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    k32.GlobalMemoryStatusEx(ctypes.byref(m))
    return {
        "cpu": cpu,
        "mem_total": m.ullTotalPhys,
        "mem_used": m.ullTotalPhys - m.ullAvailPhys,
    }


def fixed_drives():
    """Local hard drives, e.g. ["C:\\", "E:\\"] (no USB sticks, DVD or network drives)."""
    mask = k32.GetLogicalDrives()
    out = []
    for i in range(26):
        if mask & (1 << i):
            root = f"{chr(65 + i)}:\\"
            if k32.GetDriveTypeW(ctypes.c_wchar_p(root)) == 3:      # DRIVE_FIXED
                out.append(root)
    return out
