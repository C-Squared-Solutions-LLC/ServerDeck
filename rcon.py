"""RCON clients: Source RCON (CS2) and Rust's WebSocket RCON.

The WebSocket client is a minimal stdlib implementation (RFC 6455, text
frames only) so ServerDeck needs no third-party packages.
"""
import base64
import json
import os
import socket
import struct
import time
import urllib.parse


class AuthError(Exception):
    pass


# --------------------------------------------------------------------------
# Source RCON (TCP)
# --------------------------------------------------------------------------

def source(host, port, password, command, timeout=10):
    sock = socket.create_connection((host, port), timeout=timeout)

    def recvn(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("RCON connection closed")
            buf += chunk
        return buf

    def send(req_id, kind, body):
        payload = struct.pack("<ii", req_id, kind) + body.encode("utf-8") + b"\x00\x00"
        sock.sendall(struct.pack("<i", len(payload)) + payload)

    def recv():
        size = struct.unpack("<i", recvn(4))[0]
        data = recvn(size)
        req_id, kind = struct.unpack("<ii", data[:8])
        return req_id, kind, data[8:-2].decode("utf-8", "replace")

    try:
        send(1, 3, password)                      # SERVERDATA_AUTH
        while True:
            req_id, kind, _ = recv()
            if kind == 2:                         # SERVERDATA_AUTH_RESPONSE
                if req_id == -1:
                    raise AuthError("RCON password rejected")
                break
        send(2, 2, command)                       # SERVERDATA_EXECCOMMAND
        return recv()[2]
    finally:
        sock.close()


# --------------------------------------------------------------------------
# Rust WebRCON (WebSocket, JSON messages)
# --------------------------------------------------------------------------

def _send_frame(sock, payload, opcode=0x1):
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    mask = os.urandom(4)
    header += mask
    sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


class _Frames:
    def __init__(self, sock, initial=b""):
        self.sock = sock
        self.buf = initial

    def _take(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WebRCON connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self):
        b0, b1 = self._take(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._take(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._take(8))[0]
        mask = self._take(4) if b1 & 0x80 else None
        data = self._take(n)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return bool(b0 & 0x80), b0 & 0x0F, data

    def message(self):
        fin, opcode, data = self._frame()
        while not fin:
            fin, _, more = self._frame()
            data += more
        return opcode, data


def web(host, port, password, command, timeout=10):
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        path = "/" + urllib.parse.quote(password, safe="")
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise AuthError("WebRCON handshake refused (wrong password?)")
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise AuthError("WebRCON handshake refused (wrong password?)")
        ident = 1000 + int.from_bytes(os.urandom(2), "big")
        _send_frame(sock, json.dumps({"Identifier": ident, "Message": command, "Name": "ServerDeck"}).encode())
        frames = _Frames(sock, rest)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            opcode, data = frames.message()
            if opcode == 0x8:
                raise ConnectionError("WebRCON closed by server")
            if opcode == 0x9:
                _send_frame(sock, data, opcode=0xA)
                continue
            if opcode == 0x1:
                msg = json.loads(data.decode("utf-8", "replace"))
                if msg.get("Identifier") == ident:   # the server also streams its console
                    return msg.get("Message", "")
        raise TimeoutError("no WebRCON reply")
    finally:
        sock.close()
