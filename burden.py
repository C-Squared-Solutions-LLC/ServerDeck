"""Server burden: the memory and CPU each server needs, added up for the servers that are turned on and held
against what this PC has left after Windows and its other programs. More than that puts a banner on the page.

Every template estimates a full server of its game ("burden", next to "category"):

    "burden": {"ram_gb": 9, "ram_gb_per_player": 0.06, "cpu_cores": 1, "cpu_cores_per_player": 0.025,
               "players": 64, "note": "..."}

    memory = ram_gb + ram_gb_per_player x player slots                (GB)
    CPU    = cpu_cores + cpu_cores_per_player x player slots          (CPU cores kept busy)

Player slots: what the running server reports (remembered for while it's off), else "max_players" in its config,
else the template's "players" - at most 200. A "burden" block in a server's own config.json entry overrides the
template's numbers, all or some. Servers set up before templates existed are matched to theirs by Steam app id.
A running server counts with what it really uses when that's more: its memory now (with the processes it started,
e.g. Empyrion's playfield servers) and its CPU averaged over the last hour.

Turned on = "keep online" ticked, or running. What the PC has for servers = its RAM and logical processors minus what
everything else uses (Windows, Steam, browsers...): the median of the last 30 minutes, so a program opened for a
moment doesn't flip it. Once over, it has to get 0.5 GB / 0.25 cores under before the banner goes (no flapping).
"""
import collections
import json
import statistics
import threading
import time
from pathlib import Path

import winproc

GB = 1024 ** 3
MAX_SLOTS = 200
OTHER_WINDOW = 1800            # s: what everything else uses is the median over this long
CPU_WINDOW = 3600              # s: a running server's CPU is its average over this long
MIN_SAMPLES = 6                # 30 s of samples before judging (no verdict right after ServerDeck starts)
FLOOR_RAM, FLOOR_CORES = 2.0, 0.25      # Windows itself needs at least this much
CLEAR_RAM, CLEAR_CORES = 0.5, 0.25      # how far under it has to get before "too much" clears


def slots_of(value):
    """A player-slot count from a status, state or config value (20, "20", 20.0); None if there isn't one."""
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return min(n, MAX_SLOTS) if n > 0 else None


def estimate(spec, slots):
    """(GB, cores) of a server with these burden numbers and this many player slots."""
    n = slots or 0
    ram = float(spec.get("ram_gb", 0)) + float(spec.get("ram_gb_per_player", 0)) * n
    cores = float(spec.get("cpu_cores", 0)) + float(spec.get("cpu_cores_per_player", 0)) * n
    return ram, cores


class Burden:
    def __init__(self, mgr, templates_dir):
        self.mgr = mgr
        self.specs, self.titles, self.by_appid = {}, {}, {}
        for f in sorted(Path(templates_dir).glob("*.json")):
            try:
                t = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            name = t.get("template")
            if not name or not isinstance(t.get("burden"), dict):
                continue
            self.specs[name], self.titles[name] = t["burden"], t.get("title", name)
            appid = (t.get("server") or {}).get("appid")
            if appid:
                self.by_appid[int(appid)] = name
        self.lock = threading.Lock()
        self.other = collections.deque()        # (t, GB, cores) everything that isn't a game server uses
        self.cpu = {}                           # server id -> deque of (t, cores) while it runs
        self.used = {}                          # server id -> {"ram": GB, "ws": GB, "cores": now}
        self.child_cpu = {}                     # pid -> (t, CPU seconds) of the servers' child processes
        self.flags = {"ram": False, "cores": False}
        self.last = None

    # ---- what a server is ------------------------------------------------
    def spec(self, s):
        """(template name or None, its burden numbers with the server's own "burden" on top, or None)."""
        name = s.cfg.get("template")
        if name not in self.specs:
            try:
                name = self.by_appid.get(int(s.cfg.get("appid") or 0))
            except (TypeError, ValueError):
                name = None
        spec = dict(self.specs.get(name) or {})
        spec.update(s.cfg.get("burden") or {})
        return name, (spec if "ram_gb" in spec or "cpu_cores" in spec else None)

    def slots(self, s, spec):
        """(player slots, "server" / "config" / "template")."""
        st = s.status
        live = slots_of(st.get("max_players")) if st.get("running") else None
        if live:
            if live != slots_of(self.mgr.state.get(s.id, "slots")):
                self.mgr.state.set(s.id, slots=live)            # still known after it stops
            return live, "server"
        for value, source in ((self.mgr.state.get(s.id, "slots"), "server"), (s.cfg.get("max_players"), "config"),
                              ((spec or {}).get("players"), "template")):
            n = slots_of(value)
            if n:
                return n, source
        return 0, None

    # ---- measuring -------------------------------------------------------
    def _children(self, pid, kids):
        out, todo, seen = [], list(kids.get(pid, ())), {pid}
        while todo and len(out) < 200:
            p = todo.pop()
            if p in seen:
                continue
            seen.add(p)
            out.append(p)
            todo.extend(kids.get(p, ()))
        return out

    def sample(self, now=None):
        """Once per supervisor round, after the servers' status is fresh: what each running server and everything
        else use - then the verdict."""
        now = now or time.time()
        ncpu = winproc.CPU_COUNT
        kids, used, child_cpu = None, {}, {}
        for s in self.mgr.servers:
            st = s.status
            pid = st.get("pid") if st.get("running") else None
            if not pid:
                continue
            ram = max(st.get("mem") or 0, st.get("mem_ws") or 0) / GB
            ws = (st.get("mem_ws") or 0) / GB
            cores = (st.get("cpu") or 0) * ncpu / 100
            if kids is None:
                kids = collections.defaultdict(list)
                for p, parent, _ in winproc.list_processes():
                    if p != parent:
                        kids[parent].append(p)
            born = now - st["uptime"] if st.get("uptime") is not None else None
            for child in self._children(pid, kids):
                ps = winproc.process_stats(child)
                if not ps or (born is not None and ps[0] < born - 2):
                    continue                    # gone, or a recycled pid older than the server: not its child
                _, cpu_s, private, child_ws = ps
                ram += max(private, child_ws) / GB
                ws += child_ws / GB
                prev = self.child_cpu.get(child)
                child_cpu[child] = (now, cpu_s)
                if prev and now > prev[0]:
                    cores += max(0.0, (cpu_s - prev[1]) / (now - prev[0]))
            used[s.id] = {"ram": ram, "ws": ws, "cores": cores}
        sy = self.mgr.sys or {}
        with self.lock:
            self.used, self.child_cpu = used, child_cpu
            for sid, u in used.items():
                self.cpu.setdefault(sid, collections.deque()).append((now, u["cores"]))
            for q in self.cpu.values():
                while q and q[0][0] < now - CPU_WINDOW:
                    q.popleft()
            if sy.get("mem_used") and sy.get("cpu") is not None:
                self.other.append((now, max(0.0, sy["mem_used"] / GB - sum(u["ws"] for u in used.values())),
                                   max(0.0, sy["cpu"] * ncpu / 100 - sum(u["cores"] for u in used.values()))))
            while self.other and self.other[0][0] < now - OTHER_WINDOW:
                self.other.popleft()
        self.evaluate()

    # ---- the verdict -----------------------------------------------------
    def compute(self):
        sy = self.mgr.sys or {}
        with self.lock:
            other = list(self.other)
            cpu = {sid: [c for _, c in q] for sid, q in self.cpu.items() if q}
            used = dict(self.used)
        pc_ram, pc_cores = (sy.get("mem_total") or 0) / GB, float(winproc.CPU_COUNT)
        other_ram = max(FLOOR_RAM, statistics.median(r for _, r, _ in other)) if other else FLOOR_RAM
        other_cores = max(FLOOR_CORES, statistics.median(c for _, _, c in other)) if other else FLOOR_CORES
        servers, need_ram, need_cores = {}, 0.0, 0.0
        for s in self.mgr.servers:
            name, spec = self.spec(s)
            running = s.is_running()
            on = bool(s.keep_online() or running)
            slots, source = self.slots(s, spec)
            est = estimate(spec, slots) if spec else None
            u = used.get(s.id) if running else None
            u_cores = statistics.fmean(cpu[s.id]) if u and cpu.get(s.id) else None
            ram = max(est[0] if est else 0.0, u["ram"] if u else 0.0)
            cores = max(est[1] if est else 0.0, u_cores or 0.0)
            servers[s.id] = {
                "name": s.name, "game": self.titles.get(name), "on": on, "known": spec is not None,
                "ram": round(ram, 2), "cores": round(cores, 2),
                "est": {"ram": round(est[0], 2), "cores": round(est[1], 2)} if est else None,
                "used": {"ram": round(u["ram"], 2), "cores": None if u_cores is None else round(u_cores, 2)} if u else None,
                "slots": slots or None, "slots_from": source, "spec": spec, "note": (spec or {}).get("note"),
            }
            if on:
                need_ram += ram
                need_cores += cores
        have_ram, have_cores = max(0.0, pc_ram - other_ram), max(0.0, pc_cores - other_cores)
        return {
            "ready": len(other) >= MIN_SAMPLES and pc_ram > 0,
            "need": {"ram": round(need_ram, 2), "cores": round(need_cores, 2)},
            "have": {"ram": round(have_ram, 2), "cores": round(have_cores, 2)},
            "pc": {"ram": round(pc_ram, 2), "cores": pc_cores},
            "other": {"ram": round(other_ram, 2), "cores": round(other_cores, 2)},
            "excess": {"ram": round(need_ram - have_ram, 2), "cores": round(need_cores - have_cores, 2)},
            "servers": servers,
        }

    def evaluate(self):
        b = self.compute()
        if b["ready"]:
            was = self.flags["ram"] or self.flags["cores"]
            self.flags["ram"] = b["excess"]["ram"] > (-CLEAR_RAM if self.flags["ram"] else 0)
            self.flags["cores"] = b["excess"]["cores"] > (-CLEAR_CORES if self.flags["cores"] else 0)
            over = self.flags["ram"] or self.flags["cores"]
            need, have = b["need"], b["have"]
            if over and not was:
                too = [f"{b['excess']['ram']:.1f} GB of memory" if b["excess"]["ram"] > 0 else "",
                       f"{b['excess']['cores']:.1f} CPU cores" if b["excess"]["cores"] > 0 else ""]
                self.mgr.events.add("warn", None, f"too much server burden turned on - {' and '.join(filter(None, too))} "
                                                  f"more than this PC can handle: the servers that are on need about "
                                                  f"{need['ram']:.1f} GB and {need['cores']:.1f} cores when full, this PC "
                                                  f"has {have['ram']:.1f} GB and {have['cores']:.1f} cores for them")
            elif was and not over:
                self.mgr.events.add("info", None, f"server burden fits this PC again: {need['ram']:.1f} of "
                                                  f"{have['ram']:.1f} GB, {need['cores']:.1f} of {have['cores']:.1f} cores")
        b["over"] = bool(b["ready"] and (self.flags["ram"] or self.flags["cores"]))
        b["over_ram"], b["over_cores"] = b["over"] and self.flags["ram"], b["over"] and self.flags["cores"]
        self.last = b

    def summary(self):
        return self.last or {"ready": False, "over": False, "servers": {}}
