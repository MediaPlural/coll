"""Minimal RFC 6455 WebSocket client — stdlib only.

Why hand-rolled: the Engine's law is "python3 stdlib, no heavy dependency tree".
The Chrome DevTools Protocol speaks WebSocket, so the lane needs a client. This
is the smallest correct one: text frames out (masked, as the spec requires from
clients), text frames in (with continuation reassembly), ping/pong, close.

Scope is deliberately narrow — no extensions, no permessage-deflate, no TLS.
CDP endpoints are always `ws://127.0.0.1:<port>/devtools/...` in practice, which
is exactly the case this covers. Anything else raises UnsupportedEndpoint.
"""
from __future__ import annotations

import base64
import hashlib
import os
import select
import socket
import struct
from urllib.parse import urlparse

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(RuntimeError):
    pass


class UnsupportedEndpoint(WebSocketError):
    pass


def encode_frame(payload, opcode: int = OP_TEXT, mask: bool = True,
                 fin: bool = True) -> bytes:
    """Build one WebSocket frame. Clients MUST mask; the mask bit is honoured."""
    if isinstance(payload, str):
        payload = payload.encode()
    head = bytearray()
    head.append((0x80 if fin else 0x00) | (opcode & 0x0F))
    n = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if n < 126:
        head.append(mask_bit | n)
    elif n < 65536:
        head.append(mask_bit | 126)
        head += struct.pack("!H", n)
    else:
        head.append(mask_bit | 127)
        head += struct.pack("!Q", n)
    if not mask:
        return bytes(head) + payload
    key = os.urandom(4)
    head += key
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bytes(head) + masked


def decode_frame(buf: bytes):
    """Parse one frame from `buf`. Returns (opcode, fin, payload, consumed) or
    None when the buffer does not yet hold a whole frame."""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    n = b1 & 0x7F
    idx = 2
    if n == 126:
        if len(buf) < idx + 2:
            return None
        n = struct.unpack("!H", buf[idx:idx + 2])[0]
        idx += 2
    elif n == 127:
        if len(buf) < idx + 8:
            return None
        n = struct.unpack("!Q", buf[idx:idx + 8])[0]
        idx += 8
    key = b""
    if masked:
        if len(buf) < idx + 4:
            return None
        key = buf[idx:idx + 4]
        idx += 4
    if len(buf) < idx + n:
        return None
    payload = buf[idx:idx + n]
    if masked:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return opcode, fin, payload, idx + n


class WebSocket:
    """Blocking client with an explicit timeout on every read."""

    def __init__(self, url: str, timeout: float = 30.0):
        u = urlparse(url)
        if u.scheme != "ws":
            raise UnsupportedEndpoint("only ws:// is supported, got %r" % url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        self.url = url
        self.timeout = timeout
        self._buf = b""
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n" % (path, host, port, key)
        )
        self.sock.sendall(req.encode())
        header = self._read_headers()
        status = header.split("\r\n", 1)[0]
        if "101" not in status:
            raise WebSocketError("handshake failed: %s" % status)
        expect = base64.b64encode(
            hashlib.sha1((key + _GUID).encode()).digest()).decode()
        for line in header.split("\r\n")[1:]:
            if line.lower().startswith("sec-websocket-accept:"):
                if line.split(":", 1)[1].strip() != expect:
                    raise WebSocketError("bad Sec-WebSocket-Accept")
                break

    def _read_headers(self, limit: int = 65536) -> str:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            data += chunk
            if len(data) > limit:
                raise WebSocketError("handshake headers too large")
        head, _, rest = data.partition(b"\r\n\r\n")
        self._buf = rest
        return head.decode("utf-8", "replace")

    # -- frame I/O ---------------------------------------------------------
    def send_text(self, text: str) -> None:
        self.sock.sendall(encode_frame(text, OP_TEXT))

    def _next_frame(self, timeout: float):
        """Pull one frame off the wire, answering pings. None on timeout."""
        while True:
            parsed = decode_frame(self._buf)
            if parsed:
                opcode, fin, payload, used = parsed
                self._buf = self._buf[used:]
                if opcode == OP_PING:
                    self.sock.sendall(encode_frame(payload, OP_PONG))
                    continue
                if opcode == OP_CLOSE:
                    raise WebSocketError("peer closed the socket")
                return opcode, fin, payload
            ready, _, _ = select.select([self.sock], [], [], timeout)
            if not ready:
                return None
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed")
            self._buf += chunk

    def recv_text(self, timeout=None):
        """Return one complete text message, or None if nothing arrived."""
        t = self.timeout if timeout is None else timeout
        got = self._next_frame(t)
        if got is None:
            return None
        opcode, fin, payload = got
        acc = payload
        while not fin:
            more = self._next_frame(t)
            if more is None:
                raise WebSocketError("truncated fragmented message")
            opcode, fin, payload = more
            acc += payload
            if len(acc) > 64 * 1024 * 1024:
                raise WebSocketError("message too large")
        return acc.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.sendall(encode_frame(b"", OP_CLOSE))
        except Exception:  # noqa: BLE001 — best-effort politeness
            pass
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass
