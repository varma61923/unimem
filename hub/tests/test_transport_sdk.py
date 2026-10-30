"""Use the pinned MCP SDK's HTTP implementation and own every client."""

from contextlib import asynccontextmanager

import pytest

httpx2 = pytest.importorskip("httpx2")
streamable = pytest.importorskip("mcp.client.streamable_http")

from hub import smoke  # noqa: E402
from unimem import hub_client  # noqa: E402


@pytest.mark.asyncio
async def test_sync_transport_uses_sdk_native_client(monkeypatch):
    seen = []

    @asynccontextmanager
    async def transport(url, *, http_client):
        seen.append(http_client)
        yield (None, None, None)

    monkeypatch.setattr(streamable, "streamable_http_client", transport)
    manager, _session, client, _probe = await hub_client._open_session("http://127.0.0.1:8420/mcp", "test", 3)
    try:
        assert isinstance(client, httpx2.AsyncClient)
        async with manager:
            assert seen == [client]
            assert client.headers["Authorization"] == "Bearer test"
            assert client.timeout.connect <= 3
            assert client.follow_redirects is False
    finally:
        await client.aclose()
    assert client.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_smoke_owns_client_on_success_and_transport_failure(monkeypatch, fail):
    seen = []

    @asynccontextmanager
    async def transport(url, *, http_client):
        seen.append(http_client)
        assert isinstance(http_client, httpx2.AsyncClient)
        if fail:
            raise RuntimeError("injected connection failure")
        yield (None, None, None)

    monkeypatch.setattr(streamable, "streamable_http_client", transport)
    if fail:
        with pytest.raises(RuntimeError, match="injected"):
            async with smoke._session("http://127.0.0.1:8420/mcp", "test"):
                pytest.fail("transport should fail before yielding")
    else:
        async with smoke._session("http://127.0.0.1:8420/mcp", "test") as streams:
            assert streams == (None, None, None)
            assert not seen[0].is_closed
    assert seen and seen[0].is_closed
