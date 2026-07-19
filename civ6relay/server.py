"""Asyncio UDP relay server entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import socket
from collections.abc import Sequence

from .constants import DEFAULT_RELAY_PORT
from .room import OutboundDatagram, RoomManager

LOGGER = logging.getLogger("civ6relay.server")


class RelayServerProtocol(asyncio.DatagramProtocol):
    def __init__(self, manager: RoomManager | None = None) -> None:
        self.manager = manager or RoomManager(logger=LOGGER)
        self.transport: asyncio.DatagramTransport | None = None
        self.cleanup_task: asyncio.Task[None] | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())
        LOGGER.info("relay ready local=%s", transport.get_extra_info("sockname"))

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._send_all(self.manager.handle_datagram(data, addr))

    def error_received(self, exc: Exception) -> None:
        LOGGER.warning("UDP transport error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if self.cleanup_task:
            self.cleanup_task.cancel()
        if exc:
            LOGGER.error("relay transport closed: %s", exc)

    def _send_all(self, outputs: list[OutboundDatagram]) -> None:
        if self.transport is None:
            return
        for output in outputs:
            self.transport.sendto(output.data, output.endpoint)

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                self._send_all(self.manager.cleanup())
        except asyncio.CancelledError:
            pass


def parse_endpoint(value: str) -> tuple[str, int]:
    value = value.strip()
    if value.startswith(":"):
        host, port_text = "0.0.0.0", value[1:]
    else:
        try:
            host, port_text = value.rsplit(":", 1)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("listen address must be HOST:PORT") from exc
    try:
        port = int(port_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not host or not (1 <= port <= 65535):
        raise argparse.ArgumentTypeError("invalid listen address")
    return host, port


async def serve(endpoint: tuple[str, int]) -> None:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(RelayServerProtocol, local_addr=endpoint, family=socket.AF_INET)
    stop = asyncio.Event()
    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop.wait()
    finally:
        transport.close()
        await asyncio.sleep(0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Civ 6 UDP room relay")
    parser.add_argument(
        "--listen",
        type=parse_endpoint,
        default=("0.0.0.0", DEFAULT_RELAY_PORT),
        metavar="HOST:PORT",
        help=f"UDP listen address (default: 0.0.0.0:{DEFAULT_RELAY_PORT})",
    )
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(serve(args.listen))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
