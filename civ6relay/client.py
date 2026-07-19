"""Authenticated UDP tunnel client independent of the Windows packet adapter."""

from __future__ import annotations

import base64
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .constants import BROADCAST_MEMBER_ID, KEEPALIVE_SECONDS
from .protocol import (
    Frame,
    FrameFlags,
    FrameKind,
    FragmentReassembler,
    ProtocolError,
    ReplayWindow,
    SequenceCounter,
    decode_control,
    encode_control,
    split_payload,
)

EventCallback = Callable[[str, dict], None]
DataCallback = Callable[[int, int, int, bytes], None]


class RelayClientError(RuntimeError):
    def __init__(self, message: str, code: str = "client_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class ClientMetrics:
    tunnel_tx_packets: int = 0
    tunnel_rx_packets: int = 0
    tunnel_tx_bytes: int = 0
    tunnel_rx_bytes: int = 0
    game_tx_datagrams: int = 0
    game_rx_datagrams: int = 0
    dropped_packets: int = 0
    auth_failures: int = 0
    replay_drops: int = 0
    rtt_ms: float | None = None

    def snapshot(self) -> dict:
        return {
            "tunnel_tx_packets": self.tunnel_tx_packets,
            "tunnel_rx_packets": self.tunnel_rx_packets,
            "tunnel_tx_bytes": self.tunnel_tx_bytes,
            "tunnel_rx_bytes": self.tunnel_rx_bytes,
            "game_tx_datagrams": self.game_tx_datagrams,
            "game_rx_datagrams": self.game_rx_datagrams,
            "dropped_packets": self.dropped_packets,
            "auth_failures": self.auth_failures,
            "replay_drops": self.replay_drops,
            "rtt_ms": self.rtt_ms,
        }


@dataclass(slots=True)
class _PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict | None = None
    error: RelayClientError | None = None


class RelayClient:
    def __init__(
        self,
        relay_host: str,
        relay_port: int,
        *,
        event_callback: EventCallback | None = None,
        data_callback: DataCallback | None = None,
    ) -> None:
        self.relay_host = relay_host
        self.relay_port = relay_port
        self.event_callback = event_callback or (lambda _event, _data: None)
        self.data_callback = data_callback or (lambda _source, _sport, _dport, _payload: None)

        self._socket: socket.socket | None = None
        self._relay_endpoint: tuple[str, int] | None = None
        self._stop = threading.Event()
        self._recv_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._sequence_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._metrics_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, _PendingRequest] = {}

        self.room_id = 0
        self.room_code = ""
        self.member_id = 0
        self.is_host = False
        self.token: bytes | None = None
        self.peers: list[dict] = []
        self._sequence = SequenceCounter()
        self._server_replay = ReplayWindow()
        self._reassembler = FragmentReassembler()
        self._packet_id = secrets.randbits(32)
        self._last_server_rx = 0.0
        self.metrics = ClientMetrics()

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return bool(self.token and self.room_id and self.member_id)

    @property
    def relay_endpoint(self) -> tuple[str, int] | None:
        return self._relay_endpoint

    def peer_snapshot(self) -> list[dict]:
        with self._state_lock:
            return [dict(peer) for peer in self.peers]

    def start(self) -> None:
        if self._socket is not None:
            return
        try:
            info = socket.getaddrinfo(self.relay_host, self.relay_port, socket.AF_INET, socket.SOCK_DGRAM)[0]
        except socket.gaierror as exc:
            raise RelayClientError(f"cannot resolve relay host: {exc}", "resolve_failed") from exc
        relay_endpoint = (info[4][0], info[4][1])
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        sock.settimeout(0.25)
        self._relay_endpoint = relay_endpoint
        self._socket = sock
        self._stop.clear()
        self._recv_thread = threading.Thread(target=self._receive_loop, name="relay-recv", daemon=True)
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name="relay-heartbeat", daemon=True)
        self._recv_thread.start()
        self._heartbeat_thread.start()
        self._emit("transport_started", {"relay": f"{relay_endpoint[0]}:{relay_endpoint[1]}"})

    def create_room(self, name: str, timeout: float = 5.0) -> dict:
        return self._handshake(FrameKind.CREATE, {"name": name}, timeout)

    def join_room(self, room_code: str, name: str, timeout: float = 5.0) -> dict:
        payload: dict[str, object] = {"room_code": room_code, "name": name}
        with self._state_lock:
            if self.token and self.room_id and self.member_id and self.room_code == room_code.replace("-", "").upper():
                payload["resume_member"] = self.member_id
                payload["resume_token"] = base64.urlsafe_b64encode(self.token).decode("ascii")
        return self._handshake(FrameKind.JOIN, payload, timeout)

    def send_game_datagram(
        self,
        target_member: int,
        source_port: int,
        target_port: int,
        payload: bytes,
        *,
        broadcast: bool = False,
    ) -> bool:
        with self._state_lock:
            if not self.token:
                return False
            token = self.token
            room_id = self.room_id
            member_id = self.member_id
        target = BROADCAST_MEMBER_ID if broadcast else target_member
        chunks = split_payload(payload)
        packet_id = self._next_packet_id()
        flags = FrameFlags.BROADCAST if broadcast else FrameFlags.NONE
        if len(chunks) > 1:
            flags |= FrameFlags.FRAGMENTED
        for index, chunk in enumerate(chunks):
            with self._sequence_lock:
                sequence = self._sequence.next()
            frame = Frame(
                FrameKind.DATA,
                room_id,
                source_member=member_id,
                target_member=target,
                sequence=sequence,
                packet_id=packet_id,
                fragment_index=index,
                fragment_count=len(chunks),
                source_port=source_port,
                target_port=target_port,
                payload=chunk,
                flags=flags,
            )
            if not self._send(frame.encode(token)):
                return False
        with self._metrics_lock:
            self.metrics.game_tx_datagrams += 1
        return True

    def disconnect(self, notify_server: bool = True) -> None:
        if notify_server and self.connected:
            try:
                self._send_authenticated(FrameKind.LEAVE, {})
            except Exception:
                pass
        self._stop.set()
        sock = self._socket
        self._socket = None
        if sock is not None:
            sock.close()
        current = threading.current_thread()
        for thread in (self._recv_thread, self._heartbeat_thread):
            if thread and thread is not current:
                thread.join(timeout=1.0)
        self._recv_thread = None
        self._heartbeat_thread = None
        with self._state_lock:
            self.room_id = 0
            self.room_code = ""
            self.member_id = 0
            self.is_host = False
            self.token = None
            self.peers = []
        self._emit("disconnected", {})

    def metrics_snapshot(self) -> dict:
        with self._metrics_lock:
            return self.metrics.snapshot()

    def _handshake(self, kind: FrameKind, payload: dict, timeout: float) -> dict:
        self.start()
        request_id = secrets.token_hex(12)
        request = _PendingRequest()
        with self._pending_lock:
            self._pending[request_id] = request
        body = dict(payload)
        body["request_id"] = request_id
        frame = Frame(kind, payload=encode_control(body), packet_id=secrets.randbits(32))
        datagram = frame.encode()
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline and not request.event.is_set():
                self._send(datagram)
                request.event.wait(min(0.5, max(0.0, deadline - time.monotonic())))
            if not request.event.is_set():
                raise RelayClientError("relay did not answer the room request", "timeout")
            if request.error:
                raise request.error
            if request.response is None:
                raise RelayClientError("relay returned an empty response", "invalid_response")
            return request.response
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def _receive_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._socket
            if sock is None:
                break
            try:
                datagram, endpoint = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if not self._stop.is_set():
                    self._emit("transport_error", {"message": "relay socket closed unexpectedly"})
                break
            if endpoint != self._relay_endpoint:
                continue
            with self._metrics_lock:
                self.metrics.tunnel_rx_packets += 1
                self.metrics.tunnel_rx_bytes += len(datagram)
            try:
                frame = Frame.decode(datagram)
                if frame.kind in (FrameKind.WELCOME, FrameKind.ERROR):
                    self._handle_handshake_response(frame)
                else:
                    self._handle_authenticated_frame(frame)
            except ProtocolError:
                with self._metrics_lock:
                    self.metrics.dropped_packets += 1

    def _handle_handshake_response(self, frame: Frame) -> None:
        payload = decode_control(frame.payload)
        request_id = payload.get("request_id")
        if not isinstance(request_id, str):
            raise ProtocolError("handshake response is missing request_id")
        with self._pending_lock:
            pending = self._pending.get(request_id)
        if pending is None:
            return
        if pending.event.is_set():
            return
        if frame.kind is FrameKind.ERROR:
            pending.error = RelayClientError(str(payload.get("message", "relay rejected request")), str(payload.get("code", "relay_error")))
            pending.event.set()
            return
        try:
            token = base64.urlsafe_b64decode(str(payload["token"]).encode("ascii"))
            room_id = int(str(payload["room_id"]), 16)
            member_id = int(payload["member_id"])
            room_code = str(payload["room_code"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ProtocolError("invalid welcome payload") from exc
        if len(token) != 32 or not (2 <= member_id <= 9):
            raise ProtocolError("invalid welcome credentials")
        with self._state_lock:
            self.token = token
            self.room_id = room_id
            self.room_code = room_code
            self.member_id = member_id
            self.is_host = bool(payload.get("host"))
            self.peers = list(payload.get("members", []))
            self._sequence = SequenceCounter()
            self._server_replay = ReplayWindow()
            self._last_server_rx = time.monotonic()
        pending.response = payload
        pending.event.set()
        self._emit("welcome", payload)

    def _handle_authenticated_frame(self, frame: Frame) -> None:
        with self._state_lock:
            token = self.token
            room_id = self.room_id
            member_id = self.member_id
        if token is None or frame.room_id != room_id or frame.target_member != member_id:
            with self._metrics_lock:
                self.metrics.dropped_packets += 1
            return
        if not frame.verify(token):
            with self._metrics_lock:
                self.metrics.auth_failures += 1
            return
        if not self._server_replay.accept(frame.sequence):
            with self._metrics_lock:
                self.metrics.replay_drops += 1
            return
        self._last_server_rx = time.monotonic()
        if frame.kind is FrameKind.PEERS:
            payload = decode_control(frame.payload)
            peers = payload.get("members")
            if not isinstance(peers, list):
                raise ProtocolError("invalid peer list")
            with self._state_lock:
                self.peers = peers
            self._emit("peers", {"members": peers})
        elif frame.kind is FrameKind.PONG:
            payload = decode_control(frame.payload)
            sent_ns = int(payload.get("sent_ns", 0))
            if sent_ns:
                rtt_ms = max(0.0, (time.monotonic_ns() - sent_ns) / 1_000_000)
                with self._metrics_lock:
                    self.metrics.rtt_ms = rtt_ms
                self._emit("rtt", {"rtt_ms": rtt_ms})
        elif frame.kind is FrameKind.ROOM_CLOSED:
            payload = decode_control(frame.payload)
            with self._state_lock:
                self.token = None
                self.room_id = 0
                self.member_id = 0
                self.peers = []
            self._emit("room_closed", payload)
        elif frame.kind is FrameKind.DATA:
            payload = self._reassembler.add(frame)
            if payload is not None:
                with self._metrics_lock:
                    self.metrics.game_rx_datagrams += 1
                self.data_callback(frame.source_member, frame.source_port, frame.target_port, payload)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(KEEPALIVE_SECONDS):
            self._reassembler.expire()
            if not self.connected:
                continue
            try:
                self._send_authenticated(FrameKind.PING, {"sent_ns": time.monotonic_ns()})
            except Exception as exc:
                self._emit("transport_error", {"message": str(exc)})
            if self._last_server_rx and time.monotonic() - self._last_server_rx > 15.0:
                self._emit("relay_silent", {"seconds": round(time.monotonic() - self._last_server_rx, 1)})

    def _send_authenticated(self, kind: FrameKind, payload: dict) -> bool:
        with self._state_lock:
            token = self.token
            room_id = self.room_id
            member_id = self.member_id
        if token is None:
            return False
        with self._sequence_lock:
            sequence = self._sequence.next()
        frame = Frame(
            kind,
            room_id,
            source_member=member_id,
            target_member=0,
            sequence=sequence,
            payload=encode_control(payload),
        )
        return self._send(frame.encode(token))

    def _send(self, datagram: bytes) -> bool:
        sock = self._socket
        endpoint = self._relay_endpoint
        if sock is None or endpoint is None:
            return False
        try:
            with self._send_lock:
                sent = sock.sendto(datagram, endpoint)
        except OSError as exc:
            self._emit("transport_error", {"message": str(exc)})
            return False
        with self._metrics_lock:
            self.metrics.tunnel_tx_packets += 1
            self.metrics.tunnel_tx_bytes += sent
        return sent == len(datagram)

    def _next_packet_id(self) -> int:
        with self._send_lock:
            value = self._packet_id
            self._packet_id = (self._packet_id + 1) & 0xFFFFFFFF
            return value

    def _emit(self, event: str, data: dict) -> None:
        try:
            self.event_callback(event, data)
        except Exception:
            pass
