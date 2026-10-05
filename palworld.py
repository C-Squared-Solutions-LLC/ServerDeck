"""Palworld dedicated server REST API client.

Turned on in PalWorldSettings.ini (RESTAPIEnabled=True, RESTAPIPort); HTTP Basic auth as "admin" with
the server's AdminPassword. Plain HTTP - Pocketpair says not to expose it to the internet, so ServerDeck
only talks to it on 127.0.0.1 and never opens its port in the firewall.
"""
import base64
import json
import urllib.request


def call(port, password, method, path, body=None, timeout=10):
    auth = base64.b64encode(f"admin:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif method == "POST":
        data = b""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/api/{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}          # announce/save/shutdown answer with plain text


def metrics(port, password):
    """{"serverfps", "currentplayernum", "serverframetime", "maxplayernum", "uptime", "basecampnum", "days"}"""
    return call(port, password, "GET", "metrics", timeout=5)


def info(port, password):
    """{"version", "servername", "description", "worldguid"}"""
    return call(port, password, "GET", "info", timeout=5)


def announce(port, password, message):
    call(port, password, "POST", "announce", {"message": message}, timeout=5)


def save(port, password):
    call(port, password, "POST", "save", timeout=120)


def shutdown(port, password, wait_seconds, message):
    call(port, password, "POST", "shutdown", {"waittime": int(wait_seconds), "message": message})
