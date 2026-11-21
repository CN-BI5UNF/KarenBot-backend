"""OneBot 11 HTTP POST Webhook 测试。"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from adapters.transports.webhook import WebhookServer, verify_signature


def test_verify_signature() -> None:
    body = b'{"post_type":"message"}'
    signature = "sha1=" + hmac.new(b"secret", body, hashlib.sha1).hexdigest()
    assert verify_signature("secret", body, signature)
    assert not verify_signature("secret", body, "sha1=deadbeef")
    assert not verify_signature("secret", body, None)
    assert verify_signature(None, body, None)


async def test_webhook_dispatch_and_204() -> None:
    received: list[dict] = []

    async def handler(payload: dict) -> dict | None:
        received.append(payload)
        return None

    server = WebhookServer(handler, secret="topsecret")
    transport = httpx.ASGITransport(app=server.app)
    body = json.dumps({"post_type": "message", "message_id": 1}).encode()
    signature = "sha1=" + hmac.new(b"topsecret", body, hashlib.sha1).hexdigest()

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.post(
            "/onebot11",
            content=body,
            headers={"X-Signature": signature, "Content-Type": "application/json"},
        )

    assert response.status_code == 204
    assert received == [{"post_type": "message", "message_id": 1}]


async def test_webhook_rejects_bad_signature() -> None:
    async def handler(_payload: dict) -> dict | None:
        return None

    server = WebhookServer(handler, secret="topsecret")
    transport = httpx.ASGITransport(app=server.app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.post(
            "/onebot11",
            content=b"{}",
            headers={"X-Signature": "sha1=bad"},
        )

    assert response.status_code == 401


async def test_webhook_quick_operation_result() -> None:
    async def handler(_payload: dict) -> dict | None:
        return {"approve": True}

    server = WebhookServer(handler)
    transport = httpx.ASGITransport(app=server.app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.post("/onebot11", json={"post_type": "request"})

    assert response.status_code == 200
    assert response.json() == {"approve": True}


async def test_webhook_rejects_invalid_json() -> None:
    async def handler(_payload: dict) -> dict | None:
        return None

    server = WebhookServer(handler)
    transport = httpx.ASGITransport(app=server.app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.post(
            "/onebot11",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 400


async def test_webhook_rejects_oversized_body() -> None:
    async def handler(_payload: dict) -> dict | None:
        return None

    server = WebhookServer(handler, max_body_bytes=16)
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.post("/onebot11", content=b"x" * 32)
    assert response.status_code == 413
