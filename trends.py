"""Player trends: how many people were on each server, for the graph on its card (the last 24 hours,
48 hours or week).

Every supervisor pass (about every 5 s) adds the server's player count - people, not bots - to the
current 5-minute bucket. A bucket keeps the most players seen in it, the average while the server was
online and how much of the bucket it was online. Eight days are kept in trends.json (out of git); the
bucket in progress is saved too, so a ServerDeck restart loses nothing.

Time from before ServerDeck was watching, or while it wasn't running, is filled in from the game's own
record of who was on where it keeps one - SCUM's login log (players.py: Scum.sessions) - so its week
starts full. Buckets ServerDeck saw itself are never overwritten.
"""
import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("serverdeck")

STEP = 300                     # seconds per stored bucket
KEEP = 8 * 86400               # how long buckets are kept
RANGES = {"24h": (86400, 300), "48h": (2 * 86400, 600), "7d": (7 * 86400, 1800)}   # window, width of one graph point


class Trends:
    def __init__(self, mgr, path):
        self.mgr = mgr
        self.path = Path(path)
        self.lock = threading.Lock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("servers", {})
        self.dirty = False
        self.filled = set()        # servers whose gaps were filled from the game's records since ServerDeck started

    # ---- recording ----
    def sample(self, s, now=None):
        st = s.status
        if st.get("state") == "unknown":
            return                                         # not looked at yet
        now = now or time.time()
        t = int(now // STEP * STEP)
        n = st.get("humans") if st.get("humans") is not None else st.get("players")
        with self.lock:
            srv = self.data["servers"].setdefault(s.id, {"b": {}})
            o = srv.get("open")
            if o and o["t"] != t:
                if o["n"]:
                    srv["b"][str(o["t"])] = self._entry(o)
                    self.dirty = True
                o = None
            if not o:
                o = srv["open"] = {"t": t, "n": 0, "on": 0, "k": 0, "sum": 0, "max": 0}
            o["n"] += 1
            if st.get("online"):
                o["on"] += 1
                if n is not None:
                    o["k"] += 1
                    o["sum"] += n
                    o["max"] = max(o["max"], n)
        if s.id not in self.filled:
            self.filled.add(s.id)
            self._fill(s, now)

    @staticmethod
    def _entry(o):
        """[most players, average while online, share of the time online]; the counts are None when it was
        online without saying how many were on."""
        up = round(o["on"] / o["n"], 3)
        if o["k"]:
            return [o["max"], round(o["sum"] / o["k"], 2), up]
        return [None, None, up] if o["on"] else [0, 0, 0]

    def _fill(self, s, now):
        """Buckets with no reading of their own, from the game's record of who was on (SCUM's login log)."""
        g = self.mgr.players.game(s)
        if not g or not hasattr(g, "sessions"):
            return
        lo = int((now - KEEP) // STEP * STEP) + STEP
        hi = int(now // STEP * STEP)                       # the bucket in progress is left to the samples
        try:
            first, visits = g.sessions(lo)
        except Exception as e:
            log.info("trends %s: %s", s.id, e)
            return
        if first is None:
            return
        lo = max(lo, int(-(-first // STEP)) * STEP)         # only whole buckets the record covers
        ev = sorted([(a, 1) for a, _ in visits] + [(now if b is None else b, -1) for _, b in visits])
        filled = 0
        with self.lock:
            b = self.data["servers"].setdefault(s.id, {"b": {}})["b"]
            i = cur = 0
            for t in range(lo, hi, STEP):
                while i < len(ev) and ev[i][0] <= t:        # everything up to the bucket's first moment
                    cur += ev[i][1]
                    i += 1
                end, peak, area, at = t + STEP, cur, 0.0, t
                while i < len(ev) and ev[i][0] < end:
                    area += cur * (ev[i][0] - at)
                    at = ev[i][0]
                    cur += ev[i][1]
                    peak = max(peak, cur)
                    i += 1
                area += cur * (end - at)
                if str(t) not in b:
                    b[str(t)] = [peak, round(area / STEP, 2), None]
                    filled += 1
            self.dirty = self.dirty or bool(filled)
        if filled:
            log.info("trends %s: %d five-minute buckets filled in from the game's own log", s.id, filled)

    def flush(self, force=False):
        """Write trends.json once a bucket has closed (or now, before ServerDeck restarts)."""
        with self.lock:
            if not (force or self.dirty):
                return
            cut = time.time() - KEEP
            for sid in list(self.data["servers"]):
                srv = self.data["servers"][sid]
                srv["b"] = {k: v for k, v in srv["b"].items() if int(k) >= cut}
                if not srv["b"] and not srv.get("open"):
                    del self.data["servers"][sid]
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.data, separators=(",", ":")), encoding="utf-8")
                tmp.replace(self.path)
                self.dirty = False
            except OSError as e:
                log.warning("could not save %s: %s", self.path.name, e)

    # ---- API ----
    def api(self, rng):
        """One range for every server: points [most players, average, share online] (None = no record),
        plus the range's peak, average, share online (measured from uptime_from) and how many different
        players were on."""
        rng = rng if rng in RANGES else "24h"
        window, step = RANGES[rng]
        now = time.time()
        end = int(now // step * step) + step               # the last point is the one in progress
        start = end - window
        out = {}
        for s in self.mgr.servers:
            with self.lock:
                srv = self.data["servers"].get(s.id) or {"b": {}}
                b, o = srv["b"], srv.get("open")
                cur = (str(o["t"]), self._entry(o)) if o and o["n"] else (None, None)
                keys = [int(k) for k in b]
                pts, peak, peak_at, up_from = [], None, None, None
                wsum = asum = usum = ucount = 0
                for t in range(start, end, step):
                    subs = [cur[1] if str(k) == cur[0] else b.get(str(k)) for k in range(t, t + step, STEP)]
                    subs = [x for x in subs if x]
                    if not subs:
                        pts.append(None)
                        continue
                    counts = [x[0] for x in subs if x[0] is not None]
                    ups = [x[2] for x in subs if x[2] is not None]
                    weighted = [(x[1], 1 if x[2] is None else x[2]) for x in subs if x[1] is not None]
                    w = sum(wt for _, wt in weighted)
                    a = sum(v * wt for v, wt in weighted)
                    p = max(counts) if counts else None
                    pts.append([p, round(a / w, 2) if w else None, round(sum(ups) / len(ups), 3) if ups else None])
                    if p is not None and (peak is None or p >= peak):
                        peak, peak_at = p, t
                    if ups and up_from is None:
                        up_from = t                            # filled-in history doesn't say when it was down
                    wsum, asum, usum, ucount = wsum + w, asum + a, usum + sum(ups), ucount + len(ups)
            since = min(keys + ([int(cur[0])] if cur[0] else []), default=None)
            g = self.mgr.players.game(s)
            unique = sum(1 for k in self.mgr.players.known(s, g).values() if k.get("last") and k["last"] >= start) if g else None
            out[s.id] = {"points": pts, "peak": peak, "peak_at": peak_at, "avg": round(asum / wsum, 2) if wsum else None,
                         "uptime": round(usum / ucount, 3) if ucount else None, "uptime_from": up_from, "unique": unique,
                         "since": since}
        return {"range": rng, "start": start, "end": end, "step": step, "now": now, "servers": out}
