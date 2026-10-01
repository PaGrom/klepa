"""The egress proxy for all of the host's HTTP (spec 4.3, D29; spike report, points 9-13).

The host sends every request here (proxy.proxyUrl), its calls to the gatekeeper included: CONNECT tunnels, and
plain requests with an absolute URI. The exact host name and port are checked against the allow-list before
any DNS lookup, so a refused name never reaches DNS. An allowed name is resolved and must give public addresses
only; the one loopback destination is the gatekeeper, named by address. Anything else, or anything unexpected,
is refused: the proxy fails closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import logging
import socket
import time
from collections.abc import Awaitable, Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..alerts import Alerts
from ..events import EventLog

MAX_HEAD_BYTES = 16 * 1024
HEAD_TIMEOUT_SECONDS = 10.0
CONNECT_TIMEOUT_SECONDS = 10.0
DNS_TIMEOUT_SECONDS = 10.0
IDLE_TIMEOUT_SECONDS = 600.0  # a tunnel with no byte either way for ten minutes is closed
MAX_CLIENTS = 64
DENIED_LOG_SECONDS = 600.0
MAX_DENIED_LOGS_PER_HOUR = 60  # refusals of names that never repeat must not flood the log either
# Lookups get threads of their own: a resolver that hangs must not take the threads that write Core's files.
_DNS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="klepa-dns")
_HOP_BY_HOP = ("proxy-", "connection:", "keep-alive:")
log = logging.getLogger("klepa_core")

Streams = tuple[asyncio.StreamReader, asyncio.StreamWriter]
Resolve = Callable[[str, int], Awaitable[list[str]]]
Connect = Callable[[str, int], Awaitable[Streams]]


class Refused(Exception):
    def __init__(self, reason: str, status: int = 403) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class Target:
    host: str
    port: int


async def system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().run_in_executor(
        _DNS, lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    )
    return [str(info[4][0]) for info in infos]


async def open_tcp(host: str, port: int) -> Streams:
    return await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT_SECONDS)


def _public(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def _name(host: str) -> str:
    return host.lower().removesuffix(".")


def _authority(text: str) -> Target:
    """host:port of a CONNECT request; an IPv6 literal comes in brackets."""
    host, sep, port = text.rpartition(":")
    if not sep or not (port.isascii() and port.isdigit()) or not host or not 0 < int(port) < 65536:
        raise Refused("bad_request", 400)
    return Target(_name(host.removeprefix("[").removesuffix("]")), int(port))


def _absolute(text: str) -> tuple[Target, str]:
    """A plain HTTP request names its destination in the request line."""
    parts = urlsplit(text)
    if parts.scheme != "http" or not parts.hostname:
        raise Refused("bad_request", 400)
    try:
        port = parts.port or 80
    except ValueError:
        raise Refused("bad_request", 400) from None
    path = parts.path or "/"
    return Target(_name(parts.hostname), port), path + (f"?{parts.query}" if parts.query else "")


class EgressProxy:
    def __init__(
        self,
        port: int,
        allow: Iterable[tuple[str, int]],
        loopback: Iterable[tuple[str, int]],
        events: EventLog,
        *,
        alerts: Alerts | None = None,
        resolve: Resolve = system_resolve,
        connect: Connect = open_tcp,
        mono: Callable[[], float] = time.monotonic,
        max_clients: int = MAX_CLIENTS,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
    ) -> None:
        self.port = port
        self.allow = frozenset((_name(host), port) for host, port in allow)
        self.loopback = frozenset(loopback)
        self.events = events
        self.alerts = alerts
        self.resolve = resolve
        self.connect = connect
        self.mono = mono
        self.max_clients = max_clients
        self.idle_timeout = idle_timeout
        self._clients = 0
        self._denied_at: dict[tuple[str, int, str], float] = {}
        self._denied_hour: list[float] = []
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        try:
            self._server = await asyncio.start_server(self._client, "127.0.0.1", self.port, limit=MAX_HEAD_BYTES)
        except OSError as exc:
            if self.alerts is not None:
                self.alerts.raise_("egress_failed", error=type(exc).__name__)
            raise
        self.port = int(self._server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        """Close the port and every open tunnel: an open tunnel must not hold Core's stop."""
        if self._server is not None:
            self._server.close()
            self._server.close_clients()
            await self._server.wait_closed()
            self._server = None

    async def serve_forever(self, stop: asyncio.Event) -> None:
        await self.start()
        try:
            await stop.wait()
        finally:
            await self.stop()

    def _denied(self, target: Target, reason: str) -> None:
        """Logged once per destination and reason every ten minutes. The event carries a hash of the name: a name
        may itself carry data. The service log, which stays on this Mac, names it."""
        digest = hashlib.sha256(target.host.encode()).hexdigest()[:12]
        key = (digest, target.port, reason)
        now = self.mono()
        if now - self._denied_at.get(key, float("-inf")) < DENIED_LOG_SECONDS:
            return
        self._denied_hour = [at for at in self._denied_hour if now - at < 3600]
        if len(self._denied_hour) >= MAX_DENIED_LOGS_PER_HOUR:
            return
        self._denied_hour.append(now)
        if len(self._denied_at) > 1000:
            self._denied_at.clear()
        self._denied_at[key] = now
        self.events.log("egress_denied", {"host_sha256": digest, "port": target.port, "reason": reason})
        log.info("egress: refused %s:%d (%s)", target.host, target.port, reason)

    async def _open(self, target: Target) -> Streams:
        key = (target.host, target.port)
        if key in self.loopback:
            addresses = [target.host]
        elif key in self.allow:
            try:
                addresses = await asyncio.wait_for(self.resolve(target.host, target.port), DNS_TIMEOUT_SECONDS)
            except (OSError, TimeoutError):
                raise Refused("dns", 502) from None
            if not addresses or not all(_public(address) for address in addresses):
                raise Refused("not_public")
        else:
            raise Refused("not_allowed")  # before any DNS lookup
        for address in dict.fromkeys(addresses):  # each address once, in the resolver's order
            with contextlib.suppress(OSError, TimeoutError):
                return await self.connect(address, target.port)
        raise Refused("unreachable", 502)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._clients >= self.max_clients:
            writer.close()
            return
        self._clients += 1
        try:
            await self._serve(reader, writer)
        except Exception as exc:  # one broken connection never touches the others
            log.debug("egress: connection ended with %s", type(exc).__name__)
        finally:
            self._clients -= 1
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_SECONDS)
        lines = head.decode("latin-1").split("\r\n")
        words = lines[0].split(" ")
        target: Target | None = None
        try:
            if len(words) != 3:
                raise Refused("bad_request", 400)
            method, uri, version = words
            if method.upper() == "CONNECT":
                target = _authority(uri)
                up_reader, up_writer = await self._open(target)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                target, path = _absolute(uri)
                up_reader, up_writer = await self._open(target)
                headers = [line for line in lines[1:] if line and not line.lower().startswith(_HOP_BY_HOP)]
                request = "\r\n".join([f"{method} {path} {version}", *headers, "Connection: close", "", ""])
                up_writer.write(request.encode("latin-1"))
        except Refused as exc:
            if target is not None and exc.status == 403:
                self._denied(target, exc.reason)
            reason = "Forbidden" if exc.status == 403 else "Bad Request" if exc.status == 400 else "Bad Gateway"
            writer.write(f"HTTP/1.1 {exc.status} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
            return
        last = [self.mono()]  # the last byte either way
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._copy(reader, up_writer, last))
                tasks.create_task(self._copy(up_reader, writer, last))
        finally:
            up_writer.close()

    async def _copy(self, source: asyncio.StreamReader, sink: asyncio.StreamWriter, last: list[float]) -> None:
        while True:
            try:
                data = await asyncio.wait_for(source.read(65536), self.idle_timeout)
            except TimeoutError:
                if self.mono() - last[0] < self.idle_timeout:
                    continue  # the other direction is busy
                raise
            if not data:
                break
            last[0] = self.mono()
            sink.write(data)
            await sink.drain()
        with contextlib.suppress(OSError):
            if sink.can_write_eof():
                sink.write_eof()
