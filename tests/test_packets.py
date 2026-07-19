import socket
import struct
import unittest

from civ6relay.packets import build_ipv4_udp, internet_checksum, parse_ipv4_udp


class PacketTests(unittest.TestCase):
    def test_build_and_parse_ipv4_udp(self):
        packet = build_ipv4_udp("10.250.250.2", "192.168.1.10", 62900, 50321, b"payload", identification=7)
        parsed = parse_ipv4_udp(packet)
        self.assertEqual(parsed.source_ip, "10.250.250.2")
        self.assertEqual(parsed.target_ip, "192.168.1.10")
        self.assertEqual(parsed.source_port, 62900)
        self.assertEqual(parsed.target_port, 50321)
        self.assertEqual(parsed.payload, b"payload")
        self.assertEqual(internet_checksum(packet[:20]), 0)

        udp_len = struct.unpack_from("!H", packet, 24)[0]
        pseudo = packet[12:20] + struct.pack("!BBH", 0, socket.IPPROTO_UDP, udp_len)
        self.assertEqual(internet_checksum(pseudo + packet[20 : 20 + udp_len]), 0)


if __name__ == "__main__":
    unittest.main()

