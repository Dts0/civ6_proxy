"""Minimal IPv4/UDP parsing and packet synthesis."""

from __future__ import annotations

import ipaddress
import secrets
import socket
import struct
from dataclasses import dataclass

IPV4_HEADER = struct.Struct("!BBHHHBBH4s4s")
UDP_HEADER = struct.Struct("!HHHH")


class PacketError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UdpDatagram:
    source_ip: str
    target_ip: str
    source_port: int
    target_port: int
    payload: bytes


def internet_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def build_ipv4_udp(
    source_ip: str,
    target_ip: str,
    source_port: int,
    target_port: int,
    payload: bytes,
    *,
    ttl: int = 128,
    identification: int | None = None,
) -> bytes:
    source = socket.inet_aton(str(ipaddress.IPv4Address(source_ip)))
    target = socket.inet_aton(str(ipaddress.IPv4Address(target_ip)))
    if not (0 <= source_port <= 65535 and 0 <= target_port <= 65535):
        raise PacketError("UDP port out of range")
    if len(payload) > 65507:
        raise PacketError("UDP payload too large")
    udp_len = UDP_HEADER.size + len(payload)
    total_len = IPV4_HEADER.size + udp_len
    packet_id = secrets.randbits(16) if identification is None else identification
    if not (0 <= packet_id <= 65535):
        raise PacketError("IPv4 identification out of range")

    ip_header = IPV4_HEADER.pack(
        0x45,
        0,
        total_len,
        packet_id,
        0,
        ttl,
        socket.IPPROTO_UDP,
        0,
        source,
        target,
    )
    ip_checksum = internet_checksum(ip_header)
    ip_header = IPV4_HEADER.pack(
        0x45,
        0,
        total_len,
        packet_id,
        0,
        ttl,
        socket.IPPROTO_UDP,
        ip_checksum,
        source,
        target,
    )

    udp_header = UDP_HEADER.pack(source_port, target_port, udp_len, 0)
    pseudo_header = source + target + struct.pack("!BBH", 0, socket.IPPROTO_UDP, udp_len)
    udp_checksum = internet_checksum(pseudo_header + udp_header + payload)
    if udp_checksum == 0:
        udp_checksum = 0xFFFF
    udp_header = UDP_HEADER.pack(source_port, target_port, udp_len, udp_checksum)
    return ip_header + udp_header + payload


def parse_ipv4_udp(packet: bytes | bytearray | memoryview) -> UdpDatagram:
    raw = bytes(packet)
    if len(raw) < IPV4_HEADER.size + UDP_HEADER.size:
        raise PacketError("packet is too short")
    version = raw[0] >> 4
    ihl = (raw[0] & 0x0F) * 4
    if version != 4 or ihl < IPV4_HEADER.size or len(raw) < ihl + UDP_HEADER.size:
        raise PacketError("invalid IPv4 header")
    total_len = struct.unpack_from("!H", raw, 2)[0]
    if total_len < ihl + UDP_HEADER.size or total_len > len(raw):
        raise PacketError("invalid IPv4 total length")
    if raw[9] != socket.IPPROTO_UDP:
        raise PacketError("packet is not UDP")
    flags_fragment = struct.unpack_from("!H", raw, 6)[0]
    if flags_fragment & 0x3FFF:
        raise PacketError("fragmented IPv4 packets are not supported")
    source_ip = socket.inet_ntoa(raw[12:16])
    target_ip = socket.inet_ntoa(raw[16:20])
    source_port, target_port, udp_len, _ = UDP_HEADER.unpack_from(raw, ihl)
    if udp_len < UDP_HEADER.size or ihl + udp_len > total_len:
        raise PacketError("invalid UDP length")
    return UdpDatagram(
        source_ip,
        target_ip,
        source_port,
        target_port,
        raw[ihl + UDP_HEADER.size : ihl + udp_len],
    )

