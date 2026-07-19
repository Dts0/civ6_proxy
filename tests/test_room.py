import base64
import unittest

from civ6relay.constants import BROADCAST_MEMBER_ID
from civ6relay.protocol import Frame, FrameKind, decode_control, encode_control
from civ6relay.room import RoomManager


class RoomManagerTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.manager = RoomManager(clock=lambda: self.now[0])

    def _handshake(self, kind, endpoint, **payload):
        payload.setdefault("request_id", f"{endpoint[1]:024x}")
        frame = Frame(kind, payload=encode_control(payload))
        outputs = self.manager.handle_datagram(frame.encode(), endpoint)
        response = Frame.decode(outputs[0].data)
        return outputs, response, decode_control(response.payload)

    def _create(self):
        _, _, welcome = self._handshake(FrameKind.CREATE, ("127.0.0.1", 10001), name="host")
        return welcome

    def test_room_capacity_is_eight(self):
        host = self._create()
        for offset in range(7):
            _, response, _ = self._handshake(
                FrameKind.JOIN,
                ("127.0.0.1", 11000 + offset),
                room_code=host["room_code"],
                name=f"p{offset}",
            )
            self.assertEqual(response.kind, FrameKind.WELCOME)
        _, response, payload = self._handshake(
            FrameKind.JOIN,
            ("127.0.0.1", 12000),
            room_code=host["room_code"],
            name="too-many",
        )
        self.assertEqual(response.kind, FrameKind.ERROR)
        self.assertEqual(payload["code"], "room_full")

    def test_authenticated_unicast_and_replay(self):
        host = self._create()
        _, _, peer = self._handshake(
            FrameKind.JOIN,
            ("127.0.0.1", 10002),
            room_code=host["room_code"],
            name="peer",
        )
        key = base64.urlsafe_b64decode(host["token"])
        frame = Frame(
            FrameKind.DATA,
            int(host["room_id"], 16),
            source_member=host["member_id"],
            target_member=peer["member_id"],
            sequence=10,
            packet_id=3,
            source_port=62056,
            target_port=62056,
            payload=b"hello",
        )
        datagram = frame.encode(key)
        first = self.manager.handle_datagram(datagram, ("127.0.0.1", 10001))
        second = self.manager.handle_datagram(datagram, ("127.0.0.1", 10001))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        forwarded = Frame.decode(first[0].data)
        self.assertEqual(forwarded.source_member, host["member_id"])
        self.assertEqual(forwarded.target_member, peer["member_id"])
        self.assertEqual(forwarded.payload, b"hello")

    def test_broadcast_excludes_sender_and_other_rooms(self):
        host = self._create()
        for port in (10002, 10003):
            self._handshake(FrameKind.JOIN, ("127.0.0.1", port), room_code=host["room_code"], name=str(port))
        self._handshake(FrameKind.CREATE, ("127.0.0.1", 20001), name="other")
        key = base64.urlsafe_b64decode(host["token"])
        frame = Frame(
            FrameKind.DATA,
            int(host["room_id"], 16),
            source_member=host["member_id"],
            target_member=BROADCAST_MEMBER_ID,
            sequence=20,
            source_port=50000,
            target_port=62900,
            payload=b"discover",
        )
        outputs = self.manager.handle_datagram(frame.encode(key), ("127.0.0.1", 10001))
        self.assertEqual({item.endpoint for item in outputs}, {("127.0.0.1", 10002), ("127.0.0.1", 10003)})

    def test_offline_then_expire(self):
        host = self._create()
        room_id = int(host["room_id"], 16)
        self.now[0] += 31
        self.manager.cleanup()
        self.assertFalse(self.manager.rooms_by_id[room_id].host.online)
        self.now[0] += 30
        self.manager.cleanup()
        self.assertNotIn(room_id, self.manager.rooms_by_id)

    def test_resume_rotates_token_and_accepts_new_sequence(self):
        host = self._create()
        old_token = base64.urlsafe_b64decode(host["token"])
        _, _, resumed = self._handshake(
            FrameKind.JOIN,
            ("127.0.0.1", 13001),
            room_code=host["room_code"],
            name="host",
            resume_member=host["member_id"],
            resume_token=host["token"],
        )
        new_token = base64.urlsafe_b64decode(resumed["token"])
        self.assertNotEqual(old_token, new_token)
        room_id = int(resumed["room_id"], 16)
        ping = Frame(
            FrameKind.PING,
            room_id,
            source_member=resumed["member_id"],
            sequence=1,
            payload=encode_control({"sent_ns": 1}),
        )
        outputs = self.manager.handle_datagram(ping.encode(new_token), ("127.0.0.1", 13001))
        self.assertEqual(len(outputs), 1)
        self.assertEqual(Frame.decode(outputs[0].data).kind, FrameKind.PONG)

    def test_authenticated_packet_updates_nat_endpoint(self):
        host = self._create()
        key = base64.urlsafe_b64decode(host["token"])
        new_endpoint = ("127.0.0.1", 14001)
        ping = Frame(
            FrameKind.PING,
            int(host["room_id"], 16),
            source_member=host["member_id"],
            sequence=33,
            payload=encode_control({"sent_ns": 2}),
        )
        outputs = self.manager.handle_datagram(ping.encode(key), new_endpoint)
        self.assertEqual(outputs[0].endpoint, new_endpoint)
        room = self.manager.rooms_by_id[int(host["room_id"], 16)]
        self.assertEqual(room.host.endpoint, new_endpoint)

    def test_host_leave_closes_room(self):
        host = self._create()
        _, _, peer = self._handshake(
            FrameKind.JOIN,
            ("127.0.0.2", 10002),
            room_code=host["room_code"],
            name="peer",
        )
        key = base64.urlsafe_b64decode(host["token"])
        leave = Frame(
            FrameKind.LEAVE,
            int(host["room_id"], 16),
            source_member=host["member_id"],
            sequence=44,
            payload=encode_control({}),
        )
        outputs = self.manager.handle_datagram(leave.encode(key), ("127.0.0.1", 10001))
        self.assertNotIn(int(host["room_id"], 16), self.manager.rooms_by_id)
        messages = [Frame.decode(output.data) for output in outputs]
        self.assertTrue(any(message.kind is FrameKind.ROOM_CLOSED and message.target_member == peer["member_id"] for message in messages))

    def test_bad_hmac_does_not_rebind_or_route(self):
        host = self._create()
        frame = Frame(
            FrameKind.PING,
            int(host["room_id"], 16),
            source_member=host["member_id"],
            sequence=50,
            payload=encode_control({"sent_ns": 3}),
        )
        self.assertEqual(self.manager.handle_datagram(frame.encode(b"wrong-key" * 4), ("127.0.0.1", 15001)), [])
        room = self.manager.rooms_by_id[int(host["room_id"], 16)]
        self.assertEqual(room.host.endpoint, ("127.0.0.1", 10001))


if __name__ == "__main__":
    unittest.main()
