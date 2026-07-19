"""Windows-specific process, route, elevation, and network helpers."""

from __future__ import annotations

import ctypes
import ipaddress
import os
import socket
import struct
import subprocess
import sys
from ctypes import wintypes

from .constants import CIV6_PROCESS_NAMES, VIRTUAL_SUBNET_CANDIDATES


class WindowsPlatformError(RuntimeError):
    pass


class _SockAddrIn(ctypes.Structure):
    _fields_ = [
        ("sin_family", wintypes.USHORT),
        ("sin_port", wintypes.USHORT),
        ("sin_addr", wintypes.ULONG),
        ("sin_zero", ctypes.c_ubyte * 8),
    ]


def is_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def relaunch_as_admin() -> bool:
    if os.name != "nt":
        return False
    executable = sys.executable
    if getattr(sys, "frozen", False):
        parameters = subprocess.list2cmdline(sys.argv[1:])
    else:
        parameters = subprocess.list2cmdline([os.path.abspath(sys.argv[0]), *sys.argv[1:]])
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, parameters, os.getcwd(), 1)
    return result > 32


def find_civ6_processes() -> list[tuple[int, str]]:
    try:
        import psutil
    except ImportError:
        return []
    matches: list[tuple[int, str]] = []
    for process in psutil.process_iter(("pid", "name")):
        try:
            name = process.info.get("name") or ""
            if name.lower() in CIV6_PROCESS_NAMES:
                matches.append((int(process.info["pid"]), name))
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return sorted(matches)


def route_to(remote_ip: str) -> tuple[str, int, int]:
    """Return local IPv4, interface index, and sub-interface index for a remote IPv4."""
    if os.name != "nt":
        raise WindowsPlatformError("route lookup is only supported on Windows")
    remote_ip = str(ipaddress.IPv4Address(remote_ip))
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((remote_ip, 9))
        local_ip = probe.getsockname()[0]
    finally:
        probe.close()

    address = _SockAddrIn()
    address.sin_family = socket.AF_INET
    address.sin_port = 0
    address.sin_addr = struct.unpack("=I", socket.inet_aton(remote_ip))[0]
    interface_index = wintypes.DWORD()
    result = ctypes.windll.iphlpapi.GetBestInterfaceEx(ctypes.byref(address), ctypes.byref(interface_index))
    if result != 0:
        raise WindowsPlatformError(f"GetBestInterfaceEx failed with code {result}")
    return local_ip, int(interface_index.value), 0


def local_ipv4_networks() -> list[ipaddress.IPv4Network]:
    networks: set[ipaddress.IPv4Network] = set()
    try:
        import psutil

        for addresses in psutil.net_if_addrs().values():
            for address in addresses:
                if address.family == socket.AF_INET and address.address and address.netmask:
                    networks.add(ipaddress.IPv4Network((address.address, address.netmask), strict=False))
    except (ImportError, ValueError, OSError):
        pass
    if os.name == "nt":
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-NetRoute -AddressFamily IPv4 | ForEach-Object DestinationPrefix",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
            for line in result.stdout.splitlines():
                try:
                    networks.add(ipaddress.IPv4Network(line.strip(), strict=False))
                except ValueError:
                    continue
        except (OSError, subprocess.SubprocessError):
            pass
    return sorted(networks, key=lambda network: (int(network.network_address), network.prefixlen))


def select_virtual_subnet() -> ipaddress.IPv4Network:
    existing = local_ipv4_networks()
    for candidate_text in VIRTUAL_SUBNET_CANDIDATES:
        candidate = ipaddress.IPv4Network(candidate_text)
        if not any(candidate.overlaps(network) for network in existing if network.prefixlen != 0):
            return candidate
    raise WindowsPlatformError("all reserved Civ 6 virtual subnets conflict with local routes")


def virtual_member_ip(subnet: ipaddress.IPv4Network, member_id: int) -> str:
    if subnet.prefixlen != 24 or not (2 <= member_id <= 9):
        raise ValueError("virtual member address requires a /24 subnet and member id 2..9")
    return str(ipaddress.IPv4Address(int(subnet.network_address) + member_id))

