import unittest

from civ6relay.protocol import (
    Frame,
    FrameFlags,
    FrameKind,
    FragmentReassembler,
    ProtocolError,
    ReplayWindow,
    split_payload,
)


class ProtocolTests(unittest.TestCase):
    def test_authenticated_frame_round_trip(self):
        key = bytes(range(32))
        original = Frame(
            FrameKind.DATA,
            room_id=123,
            source_member=2,
            target_member=3,
            sequence=99,
            packet_id=42,
            source_port=62056,
            target_port=62056,
            payload=b"civ6-data",
        )
        decoded = Frame.decode(original.encode(key))
        self.assertEqual(decoded.payload, b"civ6-data")
        self.assertTrue(decoded.flags & FrameFlags.AUTHENTICATED)
        self.assertTrue(decoded.verify(key))
        self.assertFalse(decoded.verify(b"x" * 32))

    def test_tamper_is_detected(self):
        key = b"k" * 32
        encoded = bytearray(Frame(FrameKind.PING, sequence=1, payload=b"{}").encode(key))
        encoded[-17] ^= 1
        self.assertFalse(Frame.decode(encoded).verify(key))

    def test_structural_errors_are_rejected(self):
        with self.assertRaises(ProtocolError):
            Frame.decode(b"short")
        encoded = bytearray(Frame(FrameKind.PING).encode())
        encoded[0:4] = b"NOPE"
        with self.assertRaises(ProtocolError):
            Frame.decode(encoded)

    def test_replay_window_accepts_reordering_once(self):
        window = ReplayWindow(8)
        self.assertTrue(window.accept(10))
        self.assertTrue(window.accept(12))
        self.assertTrue(window.accept(11))
        self.assertFalse(window.accept(11))
        self.assertFalse(window.accept(3))
        self.assertTrue(window.accept(20))

    def test_fragment_reassembly_handles_out_of_order(self):
        payload = bytes(range(256)) * 20
        chunks = split_payload(payload)
        self.assertGreater(len(chunks), 1)
        reassembler = FragmentReassembler()
        result = None
        for index in reversed(range(len(chunks))):
            result = reassembler.add(
                Frame(
                    FrameKind.DATA,
                    room_id=1,
                    source_member=2,
                    target_member=3,
                    packet_id=7,
                    fragment_index=index,
                    fragment_count=len(chunks),
                    source_port=62056,
                    target_port=62056,
                    payload=chunks[index],
                    flags=FrameFlags.FRAGMENTED,
                )
            )
        self.assertEqual(result, payload)

    def test_fragment_expiry(self):
        current = [1.0]
        reassembler = FragmentReassembler(timeout=2.0, clock=lambda: current[0])
        frame = Frame(
            FrameKind.DATA,
            room_id=1,
            source_member=2,
            target_member=3,
            packet_id=8,
            fragment_index=0,
            fragment_count=2,
            payload=b"first",
        )
        self.assertIsNone(reassembler.add(frame))
        current[0] = 3.0
        self.assertEqual(reassembler.expire(), 1)


if __name__ == "__main__":
    unittest.main()

