"""Binary wire protocol, authentication, replay, and fragmentation helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import struct
import time
from dataclasses import dataclass, replace
from enum import IntEnum, IntFlag
from typing import Any, Callable

from .constants import (
    AUTH_TAG_SIZE,
    MAX_OUTER_DATAGRAM,
    MAX_UDP_PAYLOAD,
    PROTOCOL_VERSION,
    REASSEMBLY_TIMEOUT_SECONDS,
)

MAGIC = b"C6R1"
HEADER = struct.Struct("!4sBBHQBBHHHHHQI")
HEADER_SIZE = HEADER.size
MAX_FRAGMENT_PAYLOAD = MAX_OUTER_DATAGRAM - HEADER_SIZE - AUTH_TAG_SIZE
MAX_FRAGMENT_COUNT = math.ceil(MAX_UDP_PAYLOAD / MAX_FRAGMENT_PAYLOAD)


class ProtocolError(ValueError):
    """Raised when a datagram is not a valid relay frame."""


class FrameKind(IntEnum):
    CREATE = 1
    JOIN = 2
    WELCOME = 3
    PEERS = 4
    PING = 5
    PONG = 6
    LEAVE = 7
    DATA = 8
    ERROR = 9
    ROOM_CLOSED = 10


class FrameFlags(IntFlag):
    NONE = 0
    AUTHENTICATED = 1 << 0
    BROADCAST = 1 << 1
    FRAGMENTED = 1 << 2


@dataclass(frozen=True, slots=True)
class Frame:
    kind: FrameKind
    room_id: int = 0
    source_member: int = 0
    target_member: int = 0
    sequence: int = 0
    packet_id: int = 0
    fragment_index: int = 0
    fragment_count: int = 1
    source_port: int = 0
    target_port: int = 0
    payload: bytes = b""
    flags: FrameFlags = FrameFlags.NONE
    auth_tag: bytes = bytes(AUTH_TAG_SIZE)

    def _header(self, flags: FrameFlags) -> bytes:
        if not (0 <= self.room_id <= 0xFFFFFFFFFFFFFFFF):
            raise ProtocolError("room_id out of range")
        if not (0 <= self.source_member <= 0xFF and 0 <= self.target_member <= 0xFF):
            raise ProtocolError("member id out of range")
        if not (0 <= self.source_port <= 0xFFFF and 0 <= self.target_port <= 0xFFFF):
            raise ProtocolError("port out of range")
        if not (0 <= self.sequence <= 0xFFFFFFFFFFFFFFFF):
            raise ProtocolError("sequence out of range")
        if not (0 <= self.packet_id <= 0xFFFFFFFF):
            raise ProtocolError("packet_id out of range")
        if not (1 <= self.fragment_count <= MAX_FRAGMENT_COUNT):
            raise ProtocolError("invalid fragment_count")
        if not (0 <= self.fragment_index < self.fragment_count):
            raise ProtocolError("invalid fragment_index")
        if len(self.payload) > MAX_FRAGMENT_PAYLOAD:
            raise ProtocolError("frame payload is too large")
        return HEADER.pack(
            MAGIC,
            PROTOCOL_VERSION,
            int(self.kind),
            int(flags),
            self.room_id,
            self.source_member,
            self.target_member,
            self.fragment_index,
            self.fragment_count,
            self.source_port,
            self.target_port,
            len(self.payload),
            self.sequence,
            self.packet_id,
        )

    def encode(self, key: bytes | None = None) -> bytes:
        flags = self.flags
        if key is None:
            flags &= ~FrameFlags.AUTHENTICATED
        else:
            if len(key) < 16:
                raise ProtocolError("authentication key is too short")
            flags |= FrameFlags.AUTHENTICATED
        body = self._header(flags) + self.payload
        tag = hmac.new(key, body, hashlib.sha256).digest()[:AUTH_TAG_SIZE] if key else bytes(AUTH_TAG_SIZE)
        encoded = body + tag
        if len(encoded) > MAX_OUTER_DATAGRAM:
            raise ProtocolError("encoded frame exceeds datagram limit")
        return encoded

    def authenticated_bytes(self) -> bytes:
        return self._header(self.flags) + self.payload

    def verify(self, key: bytes) -> bool:
        if not self.flags & FrameFlags.AUTHENTICATED:
            return False
        expected = hmac.new(key, self.authenticated_bytes(), hashlib.sha256).digest()[:AUTH_TAG_SIZE]
        return hmac.compare_digest(expected, self.auth_tag)

    def with_sequence(self, sequence: int, target_member: int | None = None) -> "Frame":
        changes: dict[str, Any] = {"sequence": sequence}
        if target_member is not None:
            changes["target_member"] = target_member
        return replace(self, **changes)

    @classmethod
    def decode(cls, datagram: bytes) -> "Frame":
        if len(datagram) < HEADER_SIZE + AUTH_TAG_SIZE:
            raise ProtocolError("datagram is too short")
        if len(datagram) > MAX_OUTER_DATAGRAM:
            raise ProtocolError("datagram is too large")
        values = HEADER.unpack_from(datagram)
        magic, version, kind_value, flags_value = values[:4]
        if magic != MAGIC:
            raise ProtocolError("invalid magic")
        if version != PROTOCOL_VERSION:
            raise ProtocolError("unsupported protocol version")
        try:
            kind = FrameKind(kind_value)
            flags = FrameFlags(flags_value)
        except ValueError as exc:
            raise ProtocolError("invalid kind or flags") from exc
        (
            room_id,
            source_member,
            target_member,
            fragment_index,
            fragment_count,
            source_port,
            target_port,
            payload_len,
            sequence,
            packet_id,
        ) = values[4:]
        expected_len = HEADER_SIZE + payload_len + AUTH_TAG_SIZE
        if len(datagram) != expected_len:
            raise ProtocolError("payload length mismatch")
        if not (1 <= fragment_count <= MAX_FRAGMENT_COUNT):
            raise ProtocolError("invalid fragment_count")
        if fragment_index >= fragment_count:
            raise ProtocolError("invalid fragment_index")
        payload_end = HEADER_SIZE + payload_len
        return cls(
            kind=kind,
            room_id=room_id,
            source_member=source_member,
            target_member=target_member,
            sequence=sequence,
            packet_id=packet_id,
            fragment_index=fragment_index,
            fragment_count=fragment_count,
            source_port=source_port,
            target_port=target_port,
            payload=datagram[HEADER_SIZE:payload_end],
            flags=flags,
            auth_tag=datagram[payload_end:],
        )


def encode_control(value: dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("control payload is not JSON serializable") from exc
    if len(encoded) > MAX_FRAGMENT_PAYLOAD:
        raise ProtocolError("control payload is too large")
    return encoded


def decode_control(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid control payload") from exc
    if not isinstance(value, dict):
        raise ProtocolError("control payload must be an object")
    return value


class ReplayWindow:
    """A fixed-width sliding replay window for unsigned 64-bit sequences."""

    def __init__(self, width: int = 256) -> None:
        if width <= 0:
            raise ValueError("width must be positive")
        self.width = width
        self.highest = -1
        self.bitmap = 0
        self._mask = (1 << width) - 1

    def accept(self, sequence: int) -> bool:
        if sequence < 0 or sequence > 0xFFFFFFFFFFFFFFFF:
            return False
        if self.highest < 0:
            self.highest = sequence
            self.bitmap = 1
            return True
        if sequence > self.highest:
            shift = sequence - self.highest
            self.bitmap = 1 if shift >= self.width else ((self.bitmap << shift) | 1) & self._mask
            self.highest = sequence
            return True
        distance = self.highest - sequence
        if distance >= self.width:
            return False
        bit = 1 << distance
        if self.bitmap & bit:
            return False
        self.bitmap |= bit
        return True


class SequenceCounter:
    def __init__(self, initial: int | None = None) -> None:
        self.value = initial if initial is not None else secrets.randbits(31) + 1

    def next(self) -> int:
        current = self.value
        if current >= 0xFFFFFFFFFFFFFFFF:
            raise OverflowError("sequence exhausted")
        self.value += 1
        return current


def split_payload(payload: bytes) -> list[bytes]:
    if len(payload) > MAX_UDP_PAYLOAD:
        raise ProtocolError("UDP payload is too large")
    if not payload:
        return [b""]
    return [payload[offset : offset + MAX_FRAGMENT_PAYLOAD] for offset in range(0, len(payload), MAX_FRAGMENT_PAYLOAD)]


@dataclass(slots=True)
class _Assembly:
    created_at: float
    fragment_count: int
    chunks: dict[int, bytes]
    total_size: int = 0


class FragmentReassembler:
    def __init__(
        self,
        timeout: float = REASSEMBLY_TIMEOUT_SECONDS,
        max_inflight: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.timeout = timeout
        self.max_inflight = max_inflight
        self.clock = clock
        self._assemblies: dict[tuple[int, int, int, int, int], _Assembly] = {}

    def add(self, frame: Frame) -> bytes | None:
        if frame.kind is not FrameKind.DATA:
            raise ProtocolError("only DATA frames can be reassembled")
        if frame.fragment_count == 1:
            return frame.payload
        now = self.clock()
        self.expire(now)
        key = (
            frame.room_id,
            frame.source_member,
            frame.packet_id,
            frame.source_port,
            frame.target_port,
        )
        assembly = self._assemblies.get(key)
        if assembly is None:
            if len(self._assemblies) >= self.max_inflight:
                oldest = min(self._assemblies, key=lambda item: self._assemblies[item].created_at)
                del self._assemblies[oldest]
            assembly = _Assembly(now, frame.fragment_count, {})
            self._assemblies[key] = assembly
        elif assembly.fragment_count != frame.fragment_count:
            del self._assemblies[key]
            raise ProtocolError("fragment count changed during reassembly")
        if frame.fragment_index not in assembly.chunks:
            assembly.chunks[frame.fragment_index] = frame.payload
            assembly.total_size += len(frame.payload)
        if assembly.total_size > MAX_UDP_PAYLOAD:
            del self._assemblies[key]
            raise ProtocolError("reassembled payload is too large")
        if len(assembly.chunks) != assembly.fragment_count:
            return None
        try:
            payload = b"".join(assembly.chunks[index] for index in range(assembly.fragment_count))
        except KeyError:
            return None
        del self._assemblies[key]
        return payload

    def expire(self, now: float | None = None) -> int:
        current = self.clock() if now is None else now
        expired = [key for key, item in self._assemblies.items() if current - item.created_at >= self.timeout]
        for key in expired:
            del self._assemblies[key]
        return len(expired)

