"""Unit tests — WebSocket framing (the layer everything else stands on)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import ws


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
