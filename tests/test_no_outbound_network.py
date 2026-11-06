"""The test suite refuses to reach any host but this machine (tests/conftest.py)."""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest
from conftest import OutboundNetworkInTest, _is_loopback


@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.9.1", "::1", "[::1]", "localhost", None])
def test_loopback_hosts_are_allowed(host):
    assert _is_loopback(host)


@pytest.mark.parametrize("host", ["boards.example.com", "93.184.216.34", "10.0.0.5", "2001:db8::1"])
def test_other_hosts_are_not(host):
    assert not _is_loopback(host)


def test_a_dns_lookup_of_a_public_name_is_refused():
    with pytest.raises(OutboundNetworkInTest):
        socket.getaddrinfo("boards.example.com", 443)


def test_a_direct_connection_to_a_public_address_is_refused():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(OutboundNetworkInTest):
            s.connect(("93.184.216.34", 443))


def test_an_async_http_client_cannot_reach_a_public_host():
    async def go():
        async with httpx.AsyncClient() as client:
            await client.get("https://boards.example.com/v1/jobs")

    with pytest.raises((OutboundNetworkInTest, httpx.ConnectError)) as info:
        asyncio.run(go())
    assert "boards.example.com" in str(info.value)


def test_loopback_still_connects():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=2) as client:
            conn, _ = server.accept()
            with conn:
                client.sendall(b"ok")
                assert conn.recv(2) == b"ok"


def test_this_machines_own_name_is_allowed():
    assert _is_loopback(socket.gethostname())
    socket.getaddrinfo(socket.gethostname(), None)
