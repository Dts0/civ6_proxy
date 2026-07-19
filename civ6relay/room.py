"""In-memory room state and datagram routing for the relay server."""

from __future__ import annotations

import base64
import hmac
import logging
import secrets
import string
import time
from dataclasses import dataclass, field
from typing import Callable

from .constants import (
    BROADCAST_MEMBER_ID,
    FIRST_MEMBER_ID,
    MAX_ROOM_MEMBERS,
    MAX_SERVER_ROOMS,
    MEMBER_RETENTION_SECONDS,
    OFFLINE_AFTER_SECONDS,
)
from .protocol import (
    Frame,
    FrameFlags,
    FrameKind,
    ProtocolError,
    ReplayWindow,
    SequenceCounter,
    decode_control,
    encode_control,
)

Endpoint = tuple[str, int]
ROOM_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


@dataclass(frozen=True, slots=True)
class OutboundDatagram:
    endpoint: Endpoint
    data: bytes


@dataclass(slots=True)
class TokenBucket:
    packet_rate: float = 5000.0
    byte_rate: float = 1_250_000.0
    burst_seconds: float = 2.0
    packet_tokens: float = field(init=False)
    byte_tokens: float = field(init=False)
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        self.packet_tokens = self.packet_rate * self.burst_seconds
        self.byte_tokens = self.byte_rate * self.burst_seconds

    def allow(self, size: int, now: float) -> bool:
        if self.updated_at == 0.0:
            self.updated_at = now
        elapsed = max(0.0, now - self.updated_at)
        self.updated_at = now
        self.packet_tokens = min(self.packet_rate * self.burst_seconds, self.packet_tokens + elapsed * self.packet_rate)
        self.byte_tokens = min(self.byte_rate * self.burst_seconds, self.byte_tokens + elapsed * self.byte_rate)
        if self.packet_tokens < 1.0 or self.byte_tokens < size:
            return False
        self.packet_tokens -= 1.0
        self.byte_tokens -= size
        return True


@dataclass(slots=True)
class Member:
    member_id: int
    name: str
    token: bytes
    endpoint: Endpoint
    is_host: bool
    last_seen: float
    online: bool = True
    replay: ReplayWindow = field(default_factory=ReplayWindow)
    outbound_sequence: SequenceCounter = field(default_factory=SequenceCounter)
    rate_limit: TokenBucket = field(default_factory=TokenBucket)


@dataclass(slots=True)
class Room:
    room_id: int
    code: str
    members: dict[int, Member]
    created_at: float

    @property
    def host(self) -> Member:
        return next(member for member in self.members.values() if member.is_host)

    def next_member_id(self) -> int | None:
        for member_id in range(FIRST_MEMBER_ID, FIRST_MEMBER_ID + MAX_ROOM_MEMBERS):
            if member_id not in self.members:
                return member_id
        return None


@dataclass(slots=True)
class _CachedHandshake:
    expires_at: float
    data: bytes


@dataclass(slots=True)
class _HandshakeLimit:
    bucket: TokenBucket
    last_seen: float


class RoomManager:
    """Pure room state machine used by the asyncio UDP adapter and tests."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self.clock = clock
        self.logger = logger or logging.getLogger(__name__)
        self.rooms_by_id: dict[int, Room] = {}
        self.rooms_by_code: dict[str, Room] = {}
        self._handshake_cache: dict[tuple[Endpoint, int, str], _CachedHandshake] = {}
        self._handshake_limits: dict[str, _HandshakeLimit] = {}

    def handle_datagram(self, datagram: bytes, endpoint: Endpoint) -> list[OutboundDatagram]:
        try:
            frame = Frame.decode(datagram)
        except ProtocolError:
            return []
        if frame.kind in (FrameKind.CREATE, FrameKind.JOIN):
            if frame.flags & FrameFlags.AUTHENTICATED:
                return []
            if not self._allow_handshake(endpoint[0], len(datagram)):
                return []
            return self._handle_handshake(frame, endpoint)
        return self._handle_authenticated(frame, endpoint, len(datagram))

    def cleanup(self) -> list[OutboundDatagram]:
        now = self.clock()
        outputs: list[OutboundDatagram] = []
        for key, item in list(self._handshake_cache.items()):
            if item.expires_at <= now:
                del self._handshake_cache[key]
        for address, item in list(self._handshake_limits.items()):
            if now - item.last_seen >= 300.0:
                del self._handshake_limits[address]

        for room in list(self.rooms_by_id.values()):
            changed = False
            close_for_host_timeout = False
            for member in list(room.members.values()):
                idle = now - member.last_seen
                if idle >= OFFLINE_AFTER_SECONDS and member.online:
                    member.online = False
                    changed = True
                    self.logger.info("member offline room=%s member=%d", room.code, member.member_id)
                if idle >= MEMBER_RETENTION_SECONDS:
                    if member.is_host:
                        close_for_host_timeout = True
                        break
                    del room.members[member.member_id]
                    changed = True
                    self.logger.info("member expired room=%s member=%d", room.code, member.member_id)
            if close_for_host_timeout:
                outputs.extend(self._close_room(room, "host_timeout"))
            elif changed:
                outputs.extend(self._peer_updates(room))
        return outputs

    def _handle_handshake(self, frame: Frame, endpoint: Endpoint) -> list[OutboundDatagram]:
        try:
            payload = decode_control(frame.payload)
            request_id = self._request_id(payload)
        except ProtocolError:
            return []
        cache_key = (endpoint, int(frame.kind), request_id)
        cached = self._handshake_cache.get(cache_key)
        if cached and cached.expires_at > self.clock():
            return [OutboundDatagram(endpoint, cached.data)]
        try:
            if frame.kind is FrameKind.CREATE:
                outputs = self._create_room(payload, endpoint)
            else:
                outputs = self._join_room(payload, endpoint)
        except ProtocolError as exc:
            outputs = [self._error(endpoint, request_id, "invalid_request", str(exc))]
        except RoomRequestError as exc:
            outputs = [self._error(endpoint, request_id, exc.code, exc.message)]
        if outputs:
            self._handshake_cache[cache_key] = _CachedHandshake(self.clock() + 10.0, outputs[0].data)
        return outputs

    def _create_room(self, payload: dict, endpoint: Endpoint) -> list[OutboundDatagram]:
        request_id = self._request_id(payload)
        name = self._member_name(payload)
        if len(self.rooms_by_id) >= MAX_SERVER_ROOMS:
            raise RoomRequestError("server_full", "relay has reached its room limit")
        room_id = self._new_room_id()
        code = self._new_room_code()
        now = self.clock()
        host = Member(FIRST_MEMBER_ID, name, secrets.token_bytes(32), endpoint, True, now)
        room = Room(room_id, code, {host.member_id: host}, now)
        self.rooms_by_id[room_id] = room
        self.rooms_by_code[code] = room
        self.logger.info("room created room=%s member=%d endpoint=%s", code, host.member_id, endpoint[0])
        return [self._welcome(room, host, request_id)]

    def _join_room(self, payload: dict, endpoint: Endpoint) -> list[OutboundDatagram]:
        request_id = self._request_id(payload)
        code = self._room_code(payload)
        room = self.rooms_by_code.get(code)
        if room is None:
            raise RoomRequestError("room_not_found", "room does not exist")

        resumed = self._try_resume(room, payload, endpoint)
        if resumed is not None:
            self.logger.info("member resumed room=%s member=%d endpoint=%s", code, resumed.member_id, endpoint[0])
            return [self._welcome(room, resumed, request_id), *self._peer_updates(room)]

        if not room.host.online:
            raise RoomRequestError("host_offline", "room host is temporarily offline")
        member_id = room.next_member_id()
        if member_id is None:
            raise RoomRequestError("room_full", "room already has eight members")
        name = self._member_name(payload)
        member = Member(member_id, name, secrets.token_bytes(32), endpoint, False, self.clock())
        room.members[member_id] = member
        self.logger.info("member joined room=%s member=%d endpoint=%s", code, member_id, endpoint[0])
        return [self._welcome(room, member, request_id), *self._peer_updates(room)]

    def _try_resume(self, room: Room, payload: dict, endpoint: Endpoint) -> Member | None:
        try:
            member_id = int(payload.get("resume_member", 0))
            token_text = payload.get("resume_token", "")
            token = base64.urlsafe_b64decode(token_text.encode("ascii")) if token_text else b""
        except (ValueError, TypeError, UnicodeEncodeError):
            return None
        member = room.members.get(member_id)
        if member is None or not token or not hmac.compare_digest(member.token, token):
            return None
        member.token = secrets.token_bytes(32)
        member.endpoint = endpoint
        member.last_seen = self.clock()
        member.online = True
        member.replay = ReplayWindow()
        member.outbound_sequence = SequenceCounter()
        return member

    def _handle_authenticated(self, frame: Frame, endpoint: Endpoint, datagram_size: int) -> list[OutboundDatagram]:
        room = self.rooms_by_id.get(frame.room_id)
        if room is None:
            return []
        member = room.members.get(frame.source_member)
        if member is None or not frame.verify(member.token) or not member.replay.accept(frame.sequence):
            return []
        became_online = not member.online
        member.endpoint = endpoint
        member.last_seen = self.clock()
        member.online = True
        outputs: list[OutboundDatagram] = self._peer_updates(room) if became_online else []

        if frame.kind is FrameKind.PING:
            pong = Frame(
                FrameKind.PONG,
                room.room_id,
                source_member=0,
                target_member=member.member_id,
                payload=frame.payload,
            )
            outputs.append(self._send_authenticated(member, pong))
        elif frame.kind is FrameKind.LEAVE:
            if member.is_host:
                outputs.extend(self._close_room(room, "host_left"))
            else:
                del room.members[member.member_id]
                outputs.extend(self._peer_updates(room))
                self.logger.info("member left room=%s member=%d", room.code, member.member_id)
        elif frame.kind is FrameKind.DATA:
            outputs.extend(self._route_data(room, member, frame, datagram_size))
        return outputs

    def _route_data(self, room: Room, sender: Member, frame: Frame, datagram_size: int) -> list[OutboundDatagram]:
        if not sender.rate_limit.allow(datagram_size, self.clock()):
            return []
        if frame.target_member == BROADCAST_MEMBER_ID:
            targets = [member for member in room.members.values() if member.member_id != sender.member_id and member.online]
        else:
            target = room.members.get(frame.target_member)
            targets = [target] if target is not None and target.online and target.member_id != sender.member_id else []
        outputs: list[OutboundDatagram] = []
        for target in targets:
            forwarded = Frame(
                FrameKind.DATA,
                room.room_id,
                source_member=sender.member_id,
                target_member=target.member_id,
                packet_id=frame.packet_id,
                fragment_index=frame.fragment_index,
                fragment_count=frame.fragment_count,
                source_port=frame.source_port,
                target_port=frame.target_port,
                payload=frame.payload,
                flags=frame.flags & (FrameFlags.BROADCAST | FrameFlags.FRAGMENTED),
            )
            outputs.append(self._send_authenticated(target, forwarded))
        return outputs

    def _welcome(self, room: Room, member: Member, request_id: str) -> OutboundDatagram:
        payload = {
            "request_id": request_id,
            "room_code": room.code,
            "room_id": f"{room.room_id:016x}",
            "member_id": member.member_id,
            "host": member.is_host,
            "token": base64.urlsafe_b64encode(member.token).decode("ascii"),
            "members": self._peer_list(room),
        }
        frame = Frame(
            FrameKind.WELCOME,
            room.room_id,
            source_member=0,
            target_member=member.member_id,
            payload=encode_control(payload),
        )
        return OutboundDatagram(member.endpoint, frame.encode())

    def _peer_updates(self, room: Room) -> list[OutboundDatagram]:
        payload = encode_control({"members": self._peer_list(room)})
        outputs: list[OutboundDatagram] = []
        for member in room.members.values():
            if not member.online:
                continue
            frame = Frame(
                FrameKind.PEERS,
                room.room_id,
                source_member=0,
                target_member=member.member_id,
                payload=payload,
            )
            outputs.append(self._send_authenticated(member, frame))
        return outputs

    def _close_room(self, room: Room, reason: str) -> list[OutboundDatagram]:
        outputs: list[OutboundDatagram] = []
        payload = encode_control({"reason": reason})
        for member in room.members.values():
            if member.online:
                frame = Frame(
                    FrameKind.ROOM_CLOSED,
                    room.room_id,
                    source_member=0,
                    target_member=member.member_id,
                    payload=payload,
                )
                outputs.append(self._send_authenticated(member, frame))
        self.rooms_by_id.pop(room.room_id, None)
        self.rooms_by_code.pop(room.code, None)
        self.logger.info("room closed room=%s reason=%s", room.code, reason)
        return outputs

    @staticmethod
    def _peer_list(room: Room) -> list[dict]:
        return [
            {"id": member.member_id, "name": member.name, "host": member.is_host, "online": member.online}
            for member in sorted(room.members.values(), key=lambda item: item.member_id)
        ]

    @staticmethod
    def _send_authenticated(member: Member, frame: Frame) -> OutboundDatagram:
        sequenced = frame.with_sequence(member.outbound_sequence.next(), target_member=member.member_id)
        return OutboundDatagram(member.endpoint, sequenced.encode(member.token))

    @staticmethod
    def _error(endpoint: Endpoint, request_id: str, code: str, message: str) -> OutboundDatagram:
        frame = Frame(
            FrameKind.ERROR,
            payload=encode_control({"request_id": request_id, "code": code, "message": message}),
        )
        return OutboundDatagram(endpoint, frame.encode())

    @staticmethod
    def _request_id(payload: dict) -> str:
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not (8 <= len(request_id) <= 64):
            raise ProtocolError("request_id is required")
        if any(char not in string.hexdigits for char in request_id):
            raise ProtocolError("request_id must be hexadecimal")
        return request_id.lower()

    @staticmethod
    def _member_name(payload: dict) -> str:
        name = payload.get("name")
        if not isinstance(name, str):
            raise ProtocolError("name is required")
        name = name.strip()
        if not (1 <= len(name) <= 24) or any(ord(char) < 32 for char in name):
            raise ProtocolError("name must contain 1 to 24 printable characters")
        return name

    @staticmethod
    def _room_code(payload: dict) -> str:
        code = payload.get("room_code")
        if not isinstance(code, str):
            raise ProtocolError("room_code is required")
        code = code.replace("-", "").strip().upper()
        if len(code) != 10 or any(char not in ROOM_CODE_ALPHABET for char in code):
            raise ProtocolError("invalid room code")
        return code

    def _new_room_id(self) -> int:
        while True:
            room_id = secrets.randbits(64)
            if room_id and room_id not in self.rooms_by_id:
                return room_id

    def _new_room_code(self) -> str:
        while True:
            code = "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(10))
            if code not in self.rooms_by_code:
                return code

    def _allow_handshake(self, address: str, size: int) -> bool:
        now = self.clock()
        state = self._handshake_limits.get(address)
        if state is None:
            state = _HandshakeLimit(TokenBucket(packet_rate=5.0, byte_rate=12_000.0, burst_seconds=2.0), now)
            self._handshake_limits[address] = state
        state.last_seen = now
        return state.bucket.allow(size, now)


class RoomRequestError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
