"""Space Engineers dedicated server "VRage Remote API" client.

Every request is signed: Authorization = "<nonce>:<base64 HMAC-SHA1(key, url\\r\\nnonce\\r\\ndate\\r\\n)>"
with the base64 RemoteSecurityKey from SpaceEngineers-Dedicated.cfg.
"""
import base64
import email.utils
import hashlib
import hmac
import json
import os
import urllib.request


def call(port, key_b64, method, path, body=None, timeout=10):
    nonce = str(int.from_bytes(os.urandom(4), "big"))
    date = email.utils.formatdate(usegmt=True)
    message = f"{path}\r\n{nonce}\r\n{date}\r\n".encode()
    sig = base64.b64encode(hmac.new(base64.b64decode(key_b64), message, hashlib.sha1).digest()).decode()
    headers = {"Date": date, "Authorization": f"{nonce}:{sig}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def server_info(port, key_b64):
    return call(port, key_b64, "GET", "/vrageremote/v1/server").get("data", {})


def chat(port, key_b64, message):
    call(port, key_b64, "POST", "/vrageremote/v1/session/chat", message)
