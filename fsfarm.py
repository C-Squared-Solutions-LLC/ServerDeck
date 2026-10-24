"""Farming Simulator's farm, run from the dedicated server's web page.

dedicatedServer.exe is only the web page: the farm itself (the game, FarmingSimulator20xxGame.exe) runs when
someone presses Start there. ServerDeck logs on to that page to see whether the farm is running and to press
Start with the settings already on the page. The page listens on the PC's LAN address, not always on 127.0.0.1.
"""
import http.cookiejar
import re
import socket
import time
import urllib.parse
import urllib.request

PAGE = "/index.html?lang=en"
FIELDS = ("game_name", "admin_password", "game_password", "server_port", "auto_save_interval", "stats_interval")


def local_hosts():
    hosts = ["127.0.0.1"]
    try:
        hosts += [ip for ip in socket.gethostbyname_ex(socket.gethostname())[2] if ip not in hosts]
    except OSError:
        pass
    return hosts


def form_values(page):
    """The farm settings as the page shows them (inputs, selected options, crossplay box) - what Start submits."""
    vals = {}
    for m in re.finditer(r'<input[^>]*name\s*=\s*"([^"]+)"[^>]*>', page):
        if m.group(1) in FIELDS:
            v = re.search(r'value="([^"]*)"', m.group(0))
            vals[m.group(1)] = v.group(1) if v else ""
    for m in re.finditer(r'(?is)<select\s+name\s*=\s*"([^"]+)"[^>]*>(.*?)</select>', page):
        s = re.search(r'value="([^"]*)"\s*selected', m.group(2))
        if s:
            vals[m.group(1)] = s.group(1)
    if re.search(r'name\s*=\s*"crossplay_allowed"[^>]*checked', page):
        vals["crossplay_allowed"] = "on"
    return vals


class Farm:
    def __init__(self, port, user, password, cache_seconds=20):
        self.port, self.user, self.password = int(port), user, password
        self.cache_seconds = cache_seconds
        self.host = None
        self.checked = 0
        self.last = None

    def _page(self, data=None, timeout=10):
        """Log on (a fresh session each time) and return the page, after posting data if given."""
        for host in ([self.host] if self.host else []) + [h for h in local_hosts() if h != self.host]:
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            base = f"http://{host}:{self.port}"
            try:
                page = op.open(base + PAGE, urllib.parse.urlencode(
                    {"username": self.user, "password": self.password, "login": "Login"}).encode(), timeout=timeout).read()
            except OSError:
                continue
            self.host = host
            page = page.decode("utf-8", "replace")
            if 'name="username"' in page and 'name="password"' in page:
                raise RuntimeError("the farm's web page refused ServerDeck's admin login")
            if data is not None:
                page = op.open(base + PAGE, urllib.parse.urlencode(data).encode(), timeout=timeout).read().decode("utf-8", "replace")
            return page
        raise RuntimeError(f"the farm's web page (port {self.port}) isn't answering")

    @staticmethod
    def running(page):
        return 'name="stop_server"' in page

    def status(self, force=False):
        """{"running": bool} or {"error": text}; cached for a few seconds (the supervisor asks every 5 s)."""
        if not force and self.last is not None and time.time() - self.checked < self.cache_seconds:
            return self.last
        try:
            self.last = {"running": self.running(self._page())}
        except Exception as e:
            self.last = {"error": str(e)}
        self.checked = time.time()
        return self.last

    def start(self, wait=300):
        """Press Start with the page's own settings; True once the farm runs (False if it didn't within wait s)."""
        page = self._page()
        if self.running(page):
            return True
        self._page({**form_values(page), "start_server": "Start"}, timeout=60)
        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(10)
            if self.status(force=True).get("running"):
                return True
        return False
