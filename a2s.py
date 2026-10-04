"""Minimal Steam A2S_INFO client (with the 2020+ challenge handshake)."""
import socket
import struct

_REQ = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"


def info(host, port, timeout=1.5):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(_REQ, (host, port))
        data = s.recv(4096)
        if len(data) >= 9 and data[4] == 0x41:  # S2C_CHALLENGE
            s.sendto(_REQ + data[5:9], (host, port))
            data = s.recv(4096)
    finally:
        s.close()
    if len(data) < 6 or data[:4] != b"\xFF\xFF\xFF\xFF" or data[4] != 0x49:
        raise ValueError("unexpected A2S reply")
    pos = 6

    def cstr():
        nonlocal pos
        end = data.index(b"\x00", pos)
        val = data[pos:end].decode("utf-8", "replace")
        pos = end + 1
        return val

    name, map_, _folder, game = cstr(), cstr(), cstr(), cstr()
    pos += 2  # app id (short)
    players, max_players, bots = data[pos], data[pos + 1], data[pos + 2]
    return {
        "name": name,
        "map": map_,
        "game": game,
        "players": players,
        "max_players": max_players,
        "bots": bots,
    }
