"""WinDivert adapter that bridges Civ 6 UDP packets into the relay tunnel."""

from __future__ import annotations

import ipaddress
import os
import queue
import threading
from dataclasses import dataclass
from typing import Callable

from .client import RelayClient
from .constants import BROADCAST_MEMBER_ID, DISCOVERY_PORT_MAX, DISCOVERY_PORT_MIN
from .packets import build_ipv4_udp
from .windows import is_admin, virtual_member_ip

LogCallback = Callable[[str, str], None]


class DivertError(RuntimeError):
    pass


@dataclass(slots=True)
class BridgeStats:
    discovery_copies: int = 0
    virtual_captured: int = 0
    injected: int = 0
    dropped: int = 0

    def snapshot(self) -> dict:
        return {
            "discovery_copies": self.discovery_copies,
            "virtual_captured": self.virtual_captured,
            "injected": self.injected,
            "dropped": self.dropped,
        }


class WinDivertBridge:
    def __init__(
        self,
        relay: RelayClient,
        virtual_subnet: ipaddress.IPv4Network,
        local_ip: str,
        interface_index: int,
        subinterface_index: int = 0,
        log_callback: LogCallback | None = None,
    ) -> None:
        if virtual_subnet.prefixlen != 24:
            raise ValueError("virtual subnet must be /24")
        self.relay = relay
        self.virtual_subnet = virtual_subnet
        self.local_ip = local_ip
        self.interface_index = interface_index
        self.subinterface_index = subinterface_index
        self.log_callback = log_callback or (lambda _level, _message: None)
        self.stats = BridgeStats()
        self._stats_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._inject_thread: threading.Thread | None = None
        self._inject_queue: queue.Queue[tuple[int, int, int, bytes]] = queue.Queue(maxsize=2048)
        self._handle = None
        self._startup_error: Exception | None = None
        self._pydivert = None

    @property
    def running(self) -> bool:
        return bool(
            self._thread
            and self._thread.is_alive()
            and self._inject_thread
            and self._inject_thread.is_alive()
            and self._ready.is_set()
        )

    def start(self, timeout: float = 5.0) -> None:
        if os.name != "nt":
            raise DivertError("WinDivert client only supports Windows")
        if not is_admin():
            raise DivertError("administrator privileges are required for WinDivert")
        if self.running:
            return
        try:
            import pydivert
        except ImportError as exc:
            raise DivertError("pydivert is not installed") from exc
        self._pydivert = pydivert
        self._stop.clear()
        self._ready.clear()
        self._startup_error = None
        while True:
            try:
                self._inject_queue.get_nowait()
            except queue.Empty:
                break
        self._thread = threading.Thread(target=self._capture_loop, name="civ6-windivert", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            raise DivertError("timed out while opening WinDivert")
        if self._startup_error:
            error = self._startup_error
            self.stop()
            raise DivertError(str(error)) from error
        self._inject_thread = threading.Thread(target=self._inject_loop, name="civ6-inject", daemon=True)
        self._inject_thread.start()

    def stop(self) -> None:
        self._stop.set()
        handle = self._handle
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        current = threading.current_thread()
        for thread in (self._thread, self._inject_thread):
            if thread and thread is not current:
                thread.join(timeout=2.0)
        self._thread = None
        self._inject_thread = None
        self._handle = None
        self._ready.clear()

    def inject_game_datagram(self, source_member: int, source_port: int, target_port: int, payload: bytes) -> None:
        try:
            self._inject_queue.put_nowait((source_member, source_port, target_port, payload))
        except queue.Full:
            self._increment("dropped")

    def _inject_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._inject_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._inject_one(*item)

    def _inject_one(self, source_member: int, source_port: int, target_port: int, payload: bytes) -> None:
        handle = self._handle
        pydivert = self._pydivert
        if handle is None or pydivert is None or not self.running:
            self._increment("dropped")
            return
        try:
            source_ip = virtual_member_ip(self.virtual_subnet, source_member)
            raw = build_ipv4_udp(source_ip, self.local_ip, source_port, target_port, payload)
            packet = pydivert.Packet(
                raw,
                interface=(self.interface_index, self.subinterface_index),
                direction=pydivert.Direction.INBOUND,
            )
            with self._send_lock:
                handle.send(packet, recalculate_checksum=True)
            self._increment("injected")
        except Exception as exc:
            self._increment("dropped")
            self.log_callback("error", f"packet injection failed: {exc}")

    def stats_snapshot(self) -> dict:
        with self._stats_lock:
            return self.stats.snapshot()

    def _capture_loop(self) -> None:
        first = self.virtual_subnet.network_address
        last = self.virtual_subnet.broadcast_address
        packet_filter = (
            "outbound and ip and udp and ("
            f"(ip.DstAddr == 255.255.255.255 and udp.DstPort >= {DISCOVERY_PORT_MIN} "
            f"and udp.DstPort <= {DISCOVERY_PORT_MAX}) or "
            f"(ip.DstAddr >= {first} and ip.DstAddr <= {last}))"
        )
        try:
            handle = self._pydivert.WinDivert(packet_filter)
            handle.open()
            self._handle = handle
            self._ready.set()
            self.log_callback("info", f"WinDivert active on virtual subnet {self.virtual_subnet}")
            while not self._stop.is_set():
                try:
                    packet = handle.recv()
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                self._handle_packet(handle, packet)
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()
            if not self._stop.is_set():
                self.log_callback("error", f"WinDivert stopped: {exc}")
        finally:
            handle = self._handle
            self._handle = None
            if handle is not None and getattr(handle, "is_open", False):
                try:
                    handle.close()
                except Exception:
                    pass

    def _handle_packet(self, handle, packet) -> None:
        payload = bytes(packet.payload or b"")
        source_port = int(packet.src_port or 0)
        target_port = int(packet.dst_port or 0)
        target_ip = str(packet.dst_addr or "")
        if target_ip == "255.255.255.255":
            try:
                if self.relay.send_game_datagram(
                    BROADCAST_MEMBER_ID,
                    source_port,
                    target_port,
                    payload,
                    broadcast=True,
                ):
                    self._increment("discovery_copies")
            finally:
                with self._send_lock:
                    handle.send(packet)
            return

        try:
            address = ipaddress.IPv4Address(target_ip)
            target_member = int(address) - int(self.virtual_subnet.network_address)
        except ipaddress.AddressValueError:
            target_member = 0
        if 2 <= target_member <= 9 and target_member != self.relay.member_id:
            if self.relay.send_game_datagram(target_member, source_port, target_port, payload):
                self._increment("virtual_captured")
            else:
                self._increment("dropped")
        else:
            self._increment("dropped")

    def _increment(self, field: str) -> None:
        with self._stats_lock:
            setattr(self.stats, field, getattr(self.stats, field) + 1)
