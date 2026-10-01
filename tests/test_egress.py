import asyncio
import contextlib
import json
import socket

import aiohttp
import pytest
from aiohttp import web

from helpers import free_port
from klepa_core.events import EventLog
from klepa_core.host import egress as egress_module
from klepa_core.host.egress import EgressProxy, open_tcp


class FakeAlerts:
    def __init__(self):
        self.raised = []

    def raise_(self, name, **fields):
        self.raised.append((name, fields))
        return True


@pytest.fixture
async def upstream():
    """A plain HTTP server standing in for the model API and for the gatekeeper."""
    seen = []

    async def handler(request):
        seen.append({"path": request.path_qs, "host": request.headers.get("Host"), "body": await request.read()})
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    yield int(runner.addresses[0][1]), seen
    await runner.cleanup()


@pytest.fixture
async def proxy(core_db, upstream):
    _, conn, _ = core_db
    port, _ = upstream
    lookups = []
    dials = []

    async def resolve(host, port):
        lookups.append(host)
        return {
            "model.test": ["93.184.216.34"],
            "private.test": ["10.0.0.5"],
            "mixed.test": ["93.184.216.34", "127.0.0.1"],
        }[host]

    async def connect(address, port_):
        dials.append((address, port_))
        return await open_tcp("127.0.0.1", port)  # every allowed dial lands on the local upstream

    egress = EgressProxy(
        0,
        [("model.test", 443), ("private.test", 443), ("mixed.test", 443)],
        [("127.0.0.1", port)],
        EventLog(conn),
        resolve=resolve,
        connect=connect,
    )
    await egress.start()
    yield egress, lookups, dials, conn
    await egress.stop()


async def raw(port, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    writer.close()
    return head


async def tunnel_get(proxy_port, authority, path="/v1/messages"):
    """CONNECT, then a plain HTTP request through the tunnel, as the host does even for the loopback gatekeeper."""
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    if not head.startswith(b"HTTP/1.1 200"):
        writer.close()
        return head, b""
    writer.write(f"GET {path} HTTP/1.1\r\nHost: {authority}\r\nConnection: close\r\n\r\n".encode())
    body = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return head, body


async def test_a_name_not_on_the_list_never_reaches_dns(proxy):
    egress, lookups, dials, conn = proxy
    head, _ = await tunnel_get(egress.port, "evil.example:443")
    assert head.startswith(b"HTTP/1.1 403")
    assert (lookups, dials) == ([], [])
    [row] = conn.execute("SELECT data FROM event_log WHERE kind='egress_denied'").fetchall()
    data = json.loads(row[0])
    assert (data["port"], data["reason"], len(data["host_sha256"])) == (443, "not_allowed", 12)
    assert "evil" not in row[0]


@pytest.mark.parametrize("authority", ["private.test:443", "mixed.test:443"])
async def test_an_allowed_name_must_resolve_to_public_addresses_only(proxy, authority):
    egress, _, dials, _ = proxy
    head, _ = await tunnel_get(egress.port, authority)
    assert head.startswith(b"HTTP/1.1 403")
    assert dials == []


async def test_an_allowed_name_tunnels_to_its_public_address(proxy, upstream):
    egress, lookups, dials, _ = proxy
    _, seen = upstream
    head, body = await tunnel_get(egress.port, "MODEL.test.:443")
    assert head.startswith(b"HTTP/1.1 200")
    assert b'"ok": true' in body
    assert (lookups, dials) == (["model.test"], [("93.184.216.34", 443)])
    assert seen[0]["path"] == "/v1/messages"


async def test_the_gatekeeper_is_reached_by_connect_and_by_plain_http_without_dns(proxy, upstream):
    egress, lookups, _, _ = proxy
    port, seen = upstream
    head, _ = await tunnel_get(egress.port, f"127.0.0.1:{port}", "/botX/getUpdates")
    assert head.startswith(b"HTTP/1.1 200")
    async with (
        aiohttp.ClientSession() as session,
        session.post(
            f"http://127.0.0.1:{port}/botX/sendMessage?a=1",
            json={"text": "hi"},
            proxy=f"http://127.0.0.1:{egress.port}",
        ) as resp,
    ):
        assert resp.status == 200
    assert lookups == []
    assert [item["path"] for item in seen] == ["/botX/getUpdates", "/botX/sendMessage?a=1"]
    assert seen[1]["host"] == f"127.0.0.1:{port}"
    assert json.loads(seen[1]["body"]) == {"text": "hi"}


@pytest.mark.parametrize(
    "request_line",
    [
        "CONNECT 93.184.216.34:443 HTTP/1.1",  # an address, not a name
        "CONNECT model.test:80 HTTP/1.1",  # an allowed name on another port
        "CONNECT [::1]:443 HTTP/1.1",
        "GET http://model.test/ HTTP/1.1",  # plain HTTP to the model's name, port 80
        "GET http://127.0.0.2:1/ HTTP/1.1",
    ],
)
async def test_everything_else_is_refused(proxy, request_line):
    egress, lookups, _, _ = proxy
    head = await raw(egress.port, f"{request_line}\r\nHost: x\r\n\r\n".encode())
    assert head.startswith(b"HTTP/1.1 403")
    assert lookups == []


@pytest.mark.parametrize(
    "request_line", ["GARBAGE", "GET https://model.test/ HTTP/1.1", "CONNECT model.test HTTP/1.1", "GET / HTTP/1.1"]
)
async def test_malformed_requests_get_400(proxy, request_line):
    egress, *_ = proxy
    assert (await raw(egress.port, f"{request_line}\r\n\r\n".encode())).startswith(b"HTTP/1.1 400")


async def test_a_refusal_is_logged_once_per_ten_minutes(proxy):
    egress, _, _, conn = proxy
    for _ in range(3):
        await tunnel_get(egress.port, "telemetry.example:443")
    assert conn.execute("SELECT COUNT(*) FROM event_log WHERE kind='egress_denied'").fetchone()[0] == 1


async def test_a_proxy_that_cannot_listen_raises_an_alert(core_db):
    _, conn, _ = core_db
    port = free_port()
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", port))
        taken.listen()
        alerts = FakeAlerts()
        egress = EgressProxy(port, [], [], EventLog(conn), alerts=alerts)
        with pytest.raises(OSError):  # noqa: PT011 - the errno depends on the platform
            await egress.start()
    assert alerts.raised == [("egress_failed", {"error": "OSError"})]
    with contextlib.suppress(Exception):
        await egress.stop()


async def test_stop_closes_open_tunnels_at_once(proxy, upstream):
    egress, *_ = proxy
    port, _ = upstream
    reader, writer = await asyncio.open_connection("127.0.0.1", egress.port)
    writer.write(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
    assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
    started = asyncio.get_running_loop().time()
    await egress.stop()
    assert asyncio.get_running_loop().time() - started < 2
    assert await asyncio.wait_for(reader.read(), 2) == b""
    writer.close()


async def test_an_idle_tunnel_is_closed(core_db, upstream):
    _, conn, _ = core_db
    port, _ = upstream
    egress = EgressProxy(0, [], [("127.0.0.1", port)], EventLog(conn), idle_timeout=0.3)
    await egress.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", egress.port)
        writer.write(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        assert await asyncio.wait_for(reader.read(), 3) == b""  # nothing either way: closed
        writer.close()
    finally:
        await egress.stop()


async def test_a_failing_or_hanging_resolver_gets_502(core_db, monkeypatch):
    _, conn, _ = core_db
    monkeypatch.setattr(egress_module, "DNS_TIMEOUT_SECONDS", 0.2)

    async def failing(host, port):
        raise OSError("no such name")

    async def hanging(host, port):
        await asyncio.sleep(10)
        return ["93.184.216.34"]

    for resolver in (failing, hanging):
        egress = EgressProxy(0, [("model.test", 443)], [], EventLog(conn), resolve=resolver)
        await egress.start()
        try:
            head = await raw(egress.port, b"CONNECT model.test:443 HTTP/1.1\r\n\r\n")
            assert head.startswith(b"HTTP/1.1 502")
        finally:
            await egress.stop()


async def test_every_public_address_is_tried_in_turn(core_db, upstream):
    _, conn, _ = core_db
    port, _ = upstream
    dials = []

    async def resolve(host, port_):
        return ["93.184.216.34", "93.184.216.35"]

    async def connect(address, port_):
        dials.append(address)
        if address.endswith(".34"):
            raise OSError("unreachable")
        return await open_tcp("127.0.0.1", port)

    egress = EgressProxy(0, [("model.test", 443)], [], EventLog(conn), resolve=resolve, connect=connect)
    await egress.start()
    try:
        head, _ = await tunnel_get(egress.port, "model.test:443")
        assert head.startswith(b"HTTP/1.1 200")
        assert dials == ["93.184.216.34", "93.184.216.35"]
    finally:
        await egress.stop()


async def test_connections_beyond_the_limit_are_closed(core_db, upstream):
    _, conn, _ = core_db
    port, _ = upstream
    egress = EgressProxy(0, [], [("127.0.0.1", port)], EventLog(conn), max_clients=1)
    await egress.start()
    try:
        first_reader, first_writer = await asyncio.open_connection("127.0.0.1", egress.port)
        first_writer.write(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
        await first_reader.readuntil(b"\r\n\r\n")
        reader, writer = await asyncio.open_connection("127.0.0.1", egress.port)
        assert await asyncio.wait_for(reader.read(), 2) == b""
        writer.close()
        first_writer.close()
    finally:
        await egress.stop()


async def test_refusals_of_ever_new_names_stop_being_logged_after_a_budget(proxy, monkeypatch):
    egress, _, _, conn = proxy
    monkeypatch.setattr(egress_module, "MAX_DENIED_LOGS_PER_HOUR", 2)
    for i in range(4):
        await tunnel_get(egress.port, f"leak-{i}.example:443")
    assert conn.execute("SELECT COUNT(*) FROM event_log WHERE kind='egress_denied'").fetchone()[0] == 2
