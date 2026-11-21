"""Telegram Bot API 客户端测试。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from adapters.telegram.client import TelegramAPIClient, TelegramAPIError


async def test_client_ok_response() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TelegramAPIClient("http://tg.local", "secret-token", client=http)
    await client.start()
    try:
        result = await client.call("sendMessage", {"chat_id": 1, "text": "hi"})
    finally:
        await client.stop()
        await http.aclose()

    assert result == {"message_id": 1}
    assert seen["path"] == "/botsecret-token/sendMessage"
    assert b'"text": "hi"' in seen["body"] or b'"text":"hi"' in seen["body"]


async def test_client_api_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"ok": False, "error_code": 400, "description": "Bad Request"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TelegramAPIClient("http://tg.local", "token", client=http)
    await client.start()
    try:
        with pytest.raises(TelegramAPIError) as exc_info:
            await client.call("sendMessage", {"chat_id": 1, "text": "hi"})
    finally:
        await client.stop()
        await http.aclose()
    assert "Bad Request" in str(exc_info.value)
    assert exc_info.value.error_code == 400


async def test_client_timeout() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=None)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TelegramAPIClient("http://tg.local", "token", client=http)
    await client.start()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await client.call("sendMessage", {})
    finally:
        await client.stop()
        await http.aclose()


async def test_client_rejects_non_json() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TelegramAPIClient("http://tg.local", "token", client=http)
    await client.start()
    try:
        with pytest.raises(TelegramAPIError):
            await client.call("getMe")
    finally:
        await client.stop()
        await http.aclose()


async def test_client_retries_429_once() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 0},
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 2}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TelegramAPIClient("http://tg.local", "token", client=http)
    await client.start()
    try:
        result = await client.call("sendMessage", {"chat_id": 1, "text": "hi"})
    finally:
        await client.stop()
        await http.aclose()
    assert result == {"message_id": 2}
    assert calls["n"] == 2


async def test_bot_token_never_appears_in_logs(caplog) -> None:
    import logging

    caplog.set_level(logging.DEBUG)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=None)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TelegramAPIClient("http://tg.local", "123456:SUPER_SECRET", client=http)
    await client.start()
    try:
        with pytest.raises(TelegramAPIError):
            await client.call("sendMessage", {"chat_id": 1, "text": "hi"})
    finally:
        await client.stop()
        await http.aclose()
    assert "SUPER_SECRET" not in caplog.text
