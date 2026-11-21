"""OneBot 11 HTTP API 传输测试。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from adapters.base import AdapterAPIError
from adapters.transports.http import HTTPAPITransport


async def test_http_api_success_and_auth_header() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = request.content
        return httpx.Response(
            200,
            json={"status": "ok", "retcode": 0, "data": {"message_id": 123}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HTTPAPITransport(
        "http://onebot.local:3000/", "secret", timeout=5, client=client
    )
    await transport.start()
    try:
        data = await transport.call("send_msg", {"message": "hi"})
    finally:
        await transport.stop()
        await client.aclose()

    assert data == {"message_id": 123}
    assert seen["path"] == "/send_msg"
    assert seen["auth"] == "Bearer secret"
    assert b'"message": "hi"' in seen["body"] or b'"message":"hi"' in seen["body"]


async def test_http_api_error_retcode() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "failed", "retcode": 1404, "message": "not found"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HTTPAPITransport("http://x", client=client)
    await transport.start()
    try:
        with pytest.raises(AdapterAPIError) as exc_info:
            await transport.call("get_msg", {"message_id": 1})
    finally:
        await transport.stop()
        await client.aclose()
    assert "1404" in str(exc_info.value)


async def test_http_api_http_status_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HTTPAPITransport("http://x", client=client)
    await transport.start()
    try:
        with pytest.raises(AdapterAPIError):
            await transport.call("send_msg", {})
    finally:
        await transport.stop()
        await client.aclose()


async def test_http_api_timeout() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=None)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HTTPAPITransport("http://x", client=client)
    await transport.start()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await transport.call("send_msg", {})
    finally:
        await transport.stop()
        await client.aclose()


async def test_access_token_never_appears_in_logs(caplog) -> None:
    import logging

    caplog.set_level(logging.DEBUG)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=None)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HTTPAPITransport("http://x", "SUPER_SECRET", client=client)
    await transport.start()
    try:
        with pytest.raises(AdapterAPIError):
            await transport.call("send_msg", {})
    finally:
        await transport.stop()
        await client.aclose()
    assert "SUPER_SECRET" not in caplog.text
