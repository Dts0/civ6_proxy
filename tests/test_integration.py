import socket
import threading
import time
import unittest

from civ6relay.client import RelayClient
from civ6relay.room import RoomManager


class UdpRelayHarness:
    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.settimeout(0.05)
        self.endpoint = self.socket.getsockname()
        self.manager = RoomManager()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=1)
        self.socket.close()

    def _run(self):
        last_cleanup = time.monotonic()
        while not self.stop_event.is_set():
            try:
                data, endpoint = self.socket.recvfrom(65535)
                outputs = self.manager.handle_datagram(data, endpoint)
                for output in outputs:
                    self.socket.sendto(output.data, output.endpoint)
            except socket.timeout:
                pass
            if time.monotonic() - last_cleanup >= 0.1:
                for output in self.manager.cleanup():
                    self.socket.sendto(output.data, output.endpoint)
                last_cleanup = time.monotonic()


class ClientIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.harness = UdpRelayHarness()
        self.harness.start()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.disconnect(notify_server=False)
        self.harness.close()

    def _client(self, received):
        client = RelayClient(
            self.harness.endpoint[0],
            self.harness.endpoint[1],
            data_callback=lambda source, sport, dport, payload: received.append((source, sport, dport, payload)),
        )
        self.clients.append(client)
        return client

    def test_three_clients_route_unicast_broadcast_and_fragments(self):
        host_rx, peer1_rx, peer2_rx, other_rx = [], [], [], []
        host = self._client(host_rx)
        peer1 = self._client(peer1_rx)
        peer2 = self._client(peer2_rx)
        other = self._client(other_rx)

        room = host.create_room("host")
        peer1.join_room(room["room_code"], "peer1")
        peer2.join_room(room["room_code"], "peer2")
        other.create_room("other")

        host.send_game_datagram(peer1.member_id, 62056, 62056, b"unicast")
        host.send_game_datagram(255, 50001, 62900, b"discover", broadcast=True)
        large = bytes(range(256)) * 20
        peer2.send_game_datagram(host.member_id, 62056, 62056, large)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if len(peer1_rx) >= 2 and len(peer2_rx) >= 1 and host_rx:
                break
            time.sleep(0.02)

        self.assertIn((host.member_id, 62056, 62056, b"unicast"), peer1_rx)
        self.assertIn((host.member_id, 50001, 62900, b"discover"), peer1_rx)
        self.assertIn((host.member_id, 50001, 62900, b"discover"), peer2_rx)
        self.assertIn((peer2.member_id, 62056, 62056, large), host_rx)
        self.assertEqual(other_rx, [])


if __name__ == "__main__":
    unittest.main()

