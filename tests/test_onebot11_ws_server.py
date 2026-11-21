"""反向 WebSocket 服务端测试（真实本地 socket，端口 0 自动分配）。"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from adapters.transports.ws import WSServerConnection


def _port(conn: WSServerConnection) -> int:
    assert conn._server is not None
    return conn._server.sockets[0].getsockname()[1]


async def test_reverse_ws_accepts_api_and_event_connections() -> None:
    conn = WSServerConnection(
        host="127.0.0.1", port=0, path="/", access_token="tok"
    )
    received: list[dict] = []

    async def on_message(payload: dict) -> None:
        received.append(payload)

    conn.on_message(on_message)
    await conn.start()
    port = _port(conn)
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/?access_token=tok",
            additional_headers={"X-Self-ID": "100", "X-Client-Role": "Event"},
        ) as event_ws:
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/?access_token=tok",
                additional_headers={"X-Self-ID": "100", "X-Client-Role": "API"},
            ) as api_ws:
                await event_ws.send(json.dumps({"post_type": "meta_event"}))
                for _ in range(50):
                    if received:
                        break
                    await asyncio.sleep(0.02)
                assert received == [{"post_type": "meta_event"}]

                send_task = asyncio.create_task(
                    conn.send({"action": "get_status", "echo": "e1"})
                )
                out = json.loads(
                    await asyncio.wait_for(api_ws.recv(), timeout=3.0)
                )
                assert out == {"action": "get_status", "echo": "e1"}
                await api_ws.send(
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "echo": "e1",
                            "data": {"online": True},
                        }
                    )
                )
                await asyncio.wait_for(send_task, timeout=3.0)
                assert conn.is_connected is True
    finally:
        await conn.stop()


async def test_reverse_ws_rejects_bad_token() -> None:
    conn = WSServerConnection(
        host="127.0.0.1", port=0, path="/", access_token="tok"
    )
    await conn.start()
    port = _port(conn)
    try:
        ws = await websockets.connect(
            f"ws://127.0.0.1:{port}/?access_token=wrong"
        )
        with pytest.raises(
            (websockets.ConnectionClosed, websockets.ConnectionClosedError)
        ):
            await asyncio.wait_for(ws.recv(), timeout=2.0)
        await ws.close()
    finally:
        await conn.stop()


async def test_send_with_self_id_falls_back_when_header_missing() -> None:
    conn = WSServerConnection(host="127.0.0.1", port=0, path="/")
    await conn.start()
    port = _port(conn)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/") as ws:
            task = asyncio.create_task(
                conn.send({"action": "get_status", "echo": "e1"}, self_id="123")
            )
            out = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
            assert out["echo"] == "e1"
            await ws.send(
                json.dumps({"status": "ok", "retcode": 0, "echo": "e1", "data": {}})
            )
            await asyncio.wait_for(task, timeout=3.0)
    finally:
        await conn.stop()
