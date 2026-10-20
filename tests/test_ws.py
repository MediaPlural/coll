"""Unit tests — WebSocket framing (the layer everything else stands on)."""
import base64
import hashlib
import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import ws


class _FakePeer:
    """One-shot TCP peer that answers the upgrade by hand.

    A real socket is the only honest way to test the handshake: the finding was
    that a 101 *without* `Sec-WebSocket-Accept` was accepted, and that can only
    be reproduced against a peer that sends one.
    """

    def __init__(self, responder):
        self.responder = responder
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.error = None
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn = None
        try:
            conn, _ = self.srv.accept()
            conn.settimeout(5)
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                req += chunk
            for chunk in self.responder(req):
                conn.sendall(chunk)
            try:
                conn.recv(1)          # hold the socket open until the client quits
            except OSError:
                pass
        except Exception as e:        # noqa: BLE001 — surfaced by the test body
            self.error = e
        finally:
            if conn is not None:
                conn.close()
            self.srv.close()

    @property
    def url(self):
        return "ws://127.0.0.1:%d/devtools/page/x" % self.port


def _accept_for(req: bytes) -> str:
    for line in req.decode("utf-8", "replace").split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
            return base64.b64encode(
                hashlib.sha1((key + ws._GUID).encode()).digest()).decode()
    raise AssertionError("client sent no Sec-WebSocket-Key")


def _upgrade(req, accept=True, value=None, frames=()):
    head = ["HTTP/1.1 101 Switching Protocols", "Upgrade: websocket",
            "Connection: Upgrade"]
    if accept:
        head.append("Sec-WebSocket-Accept: %s" % (value or _accept_for(req)))
    return [("\r\n".join(head) + "\r\n\r\n").encode()] + list(frames)


class TestHandshake(unittest.TestCase):
    def test_valid_101_connects(self):
        peer = _FakePeer(lambda req: _upgrade(req))
        sock = ws.WebSocket(peer.url, timeout=3)
        sock.close()
        self.assertIsNone(peer.error)

    def test_101_without_accept_header_is_rejected(self):
        peer = _FakePeer(lambda req: _upgrade(req, accept=False))
        with self.assertRaises(ws.WebSocketError) as ctx:
            ws.WebSocket(peer.url, timeout=3)
        self.assertIn("Sec-WebSocket-Accept", str(ctx.exception))

    def test_101_with_wrong_accept_header_is_rejected(self):
        peer = _FakePeer(lambda req: _upgrade(req, value="bm90LXRoZS1rZXk="))
        with self.assertRaises(ws.WebSocketError):
            ws.WebSocket(peer.url, timeout=3)


class TestOpcodeLaw(unittest.TestCase):
    """RFC 6455 §5.2 — reserved opcodes are protocol errors, not payloads."""

    def test_reserved_opcode_0xB_raises(self):
        peer = _FakePeer(lambda req: _upgrade(
            req, frames=[ws.encode_frame(b"payload", opcode=0xB, mask=False)]))
        sock = ws.WebSocket(peer.url, timeout=3)
        self.addCleanup(sock.close)
        with self.assertRaises(ws.WebSocketError) as ctx:
            sock.recv_text(timeout=2)
        self.assertIn("0xB", str(ctx.exception))

    def test_reserved_noncontrol_opcode_0x3_raises(self):
        peer = _FakePeer(lambda req: _upgrade(
            req, frames=[ws.encode_frame(b"x", opcode=0x3, mask=False)]))
        sock = ws.WebSocket(peer.url, timeout=3)
        self.addCleanup(sock.close)
        with self.assertRaises(ws.WebSocketError):
            sock.recv_text(timeout=2)

    def test_text_frame_still_accepted(self):
        peer = _FakePeer(lambda req: _upgrade(
            req, frames=[ws.encode_frame(b'{"id":1}', mask=False)]))
        sock = ws.WebSocket(peer.url, timeout=3)
        self.assertEqual(sock.recv_text(timeout=2), '{"id":1}')
        sock.close()

    def test_fragmentation_with_interleaved_ping_still_reassembles(self):
        frames = [ws.encode_frame(b"he", opcode=ws.OP_TEXT, mask=False, fin=False),
                  ws.encode_frame(b"p", opcode=ws.OP_PING, mask=False),
                  ws.encode_frame(b"llo", opcode=ws.OP_CONT, mask=False, fin=True)]
        peer = _FakePeer(lambda req: _upgrade(req, frames=frames))
        sock = ws.WebSocket(peer.url, timeout=3)
        self.assertEqual(sock.recv_text(timeout=2), "hello")
        sock.close()

    def test_valid_opcode_set_matches_rfc(self):
        self.assertEqual(ws.VALID_OPCODES,
                         {0x0, 0x1, 0x2, 0x8, 0x9, 0xA})


class TestFraming(unittest.TestCase):
    def test_roundtrip_small(self):
        payload = b'{"id":1,"method":"Page.enable"}'
        frame = ws.encode_frame(payload)
        parsed = ws.decode_frame(frame)
        self.assertIsNotNone(parsed)
        opcode, fin, data, used = parsed
        self.assertEqual(opcode, ws.OP_TEXT)
        self.assertTrue(fin)
        self.assertEqual(data, payload)
        self.assertEqual(used, len(frame))

    def test_roundtrip_medium_length_prefix(self):
        payload = os.urandom(200)          # 126..65535 -> 2-byte length
        opcode, fin, data, used = ws.decode_frame(ws.encode_frame(payload))
        self.assertEqual(data, payload)
        self.assertEqual(used, 2 + 2 + 4 + 200)

    def test_roundtrip_large_length_prefix(self):
        payload = b"x" * 70000             # >65535 -> 8-byte length
        frame = ws.encode_frame(payload)
        opcode, fin, data, used = ws.decode_frame(frame)
        self.assertEqual(len(data), 70000)
        self.assertEqual(used, len(frame))

    def test_client_frames_are_masked(self):
        frame = ws.encode_frame(b"hello")
        self.assertTrue(frame[1] & 0x80, "client->server frames must set the mask bit")
        self.assertNotEqual(frame[6:], b"hello", "payload must not travel in clear")
        self.assertEqual(ws.decode_frame(frame)[2], b"hello")

    def test_unmasked_server_frame_decodes(self):
        frame = ws.encode_frame(b"hello", mask=False)
        self.assertFalse(frame[1] & 0x80)
        self.assertEqual(ws.decode_frame(frame)[2], b"hello")

    def test_partial_frame_returns_none(self):
        frame = ws.encode_frame(b"y" * 300)
        for cut in (1, 2, 5, len(frame) - 1):
            self.assertIsNone(ws.decode_frame(frame[:cut]),
                              "truncated frame at %d must not parse" % cut)

    def test_two_frames_in_one_buffer(self):
        a = ws.encode_frame(b"one", mask=False)
        b = ws.encode_frame(b"two", mask=False)
        opcode, fin, data, used = ws.decode_frame(a + b)
        self.assertEqual(data, b"one")
        self.assertEqual(ws.decode_frame((a + b)[used:])[2], b"two")

    def test_rejects_tls_endpoint(self):
        with self.assertRaises(ws.UnsupportedEndpoint):
            ws.WebSocket("wss://127.0.0.1:1/devtools/page/x", timeout=0.1)


if __name__ == "__main__":
    unittest.main()
