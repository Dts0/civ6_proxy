"""Persistent non-secret client configuration."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .constants import DEFAULT_RELAY_PORT


@dataclass(slots=True)
class ClientConfig:
    relay_host: str = "127.0.0.1"
    relay_port: int = DEFAULT_RELAY_PORT
    display_name: str = os.environ.get("USERNAME", "Player")[:24] or "Player"


def config_path() -> Path:
    root = Path(os.environ.get("APPDATA", Path.home())) / "Civ6Relay"
    return root / "config.json"


def load_config(path: Path | None = None) -> ClientConfig:
    target = path or config_path()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
        host = str(value.get("relay_host", "127.0.0.1")).strip()
        port = int(value.get("relay_port", DEFAULT_RELAY_PORT))
        name = str(value.get("display_name", "Player")).strip()
        if not host or not (1 <= port <= 65535) or not (1 <= len(name) <= 24):
            raise ValueError
        return ClientConfig(host, port, name)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return ClientConfig()


def save_config(config: ClientConfig, path: Path | None = None) -> None:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(config), ensure_ascii=True, indent=2), encoding="utf-8")
    temporary.replace(target)


def parse_relay_address(value: str) -> tuple[str, int]:
    value = value.strip()
    if not value:
        raise ValueError("中转地址不能为空")
    if ":" not in value:
        return value, DEFAULT_RELAY_PORT
    host, port_text = value.rsplit(":", 1)
    if not host:
        raise ValueError("中转主机不能为空")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("中转端口必须是数字") from exc
    if not (1 <= port <= 65535):
        raise ValueError("中转端口必须在 1 到 65535 之间")
    return host, port

