"""OneBot11Adapter 端到端单测（假连接，不依赖真实协议端）。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from adapters.base import AdapterNotConnectedError
from adapters.onebot11.adapter import OneBot11Adapter
from adapters.onebot11.dialects import GENERIC_DIALECT, NAPCAT_DIALECT
from adapters.types import Target


class FakeConnection:
    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.sent: list[dict[str, Any]] = []
        self._message_cb = None
        self._lost_callbacks: list[Any] = []

    def on_message(self, callback) -> None:
        self._message_cb = callback

    def on_connection_lost(self, callback) -> None:
        self._lost_callbacks.append(callback)

    async def start(self) -> None:
        self.connected = True

    async def stop(self) -> None:
        self.connected = False

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def wait_connected(self, timeout: float | None = None) -> bool:
        return self.connected

    async def send(self, data: dict, self_id: str | None = None) -> None:
        self.sent.append(data)
        if "echo" not in data or self._message_cb is None:
            return
        # 模拟协议端立即回包，让 api_call 的 Future 完成。
        await self._message_cb(
            {
                "status": "ok",
                "retcode": 0,
                "echo": data["echo"],
                "data": {"message_id": 123},
            }
        )

    async def deliver(self, payload: dict) -> None:
        assert self._message_cb is not None
        await self._message_cb(payload)


async def test_send_text_array_format() -> None:
    conn = FakeConnection()
    adapter = OneBot11Adapter(
        "default", conn, dialect=NAPCAT_DIALECT, message_format="array"
    )
    msg_id = await adapter.send_text(
        Target(adapter="default", scope="private", target_id="42"),
        "hi[CQ:at,qq=7]",
    )

    assert msg_id == "123"
    params = conn.sent[0]["params"]
    assert conn.sent[0]["action"] == "send_msg"
    assert params["message_type"] == "private"
    assert params["user_id"] == 42
    assert params["message"] == [
        {"type": "text", "data": {"text": "hi"}},
        {"type": "at", "data": {"qq": "7"}},
    ]


async def test_send_text_string_format() -> None:
    conn = FakeConnection()
    adapter = OneBot11Adapter(
        "default", conn, dialect=GENERIC_DIALECT, message_format="string"
    )
    await adapter.send_text(
        Target(adapter="default", scope="group", target_id="99"),
        "hi[CQ:at,qq=7]",
    )
    params = conn.sent[0]["params"]
    assert params["message"] == "hi[CQ:at,qq=7]"
    assert params["group_id"] == 99


async def test_send_poke_uses_dialect_action() -> None:
    conn = FakeConnection()
    adapter = OneBot11Adapter("default", conn, dialect=NAPCAT_DIALECT)
    await adapter.send_poke(
        Target(adapter="default", scope="group", target_id="99"), "42"
    )
    assert conn.sent[0]["action"] == "send_poke"
    assert conn.sent[0]["params"] == {"user_id": 42, "group_id": 99}


async def test_event_delivery_through_adapter() -> None:
    conn = FakeConnection()
    adapter = OneBot11Adapter("default", conn, dialect=NAPCAT_DIALECT)
    received: list[Any] = []

    async def on_event(event) -> None:
        received.append(event)

    adapter.subscribe(on_event)
    await conn.deliver(
        {
            "post_type": "message",
            "message_type": "private",
            "time": 1,
            "self_id": 100,
            "message_id": 5,
            "user_id": 42,
            "raw_message": "hello",
            "message": [{"type": "text", "data": {"text": "hello"}}],
            "sender": {"nickname": "Alice"},
        }
    )

    assert len(received) == 1
    event = received[0]
    assert event.adapter == "default"
    assert event.text == "hello"
    assert event.source_target == Target(
        adapter="default", scope="private", target_id="42", self_id="100"
    )


async def test_stop_discards_pending_calls() -> None:
    conn = FakeConnection()
    adapter = OneBot11Adapter("default", conn, dialect=NAPCAT_DIALECT)

    # 不让 FakeConnection 回包，模拟挂起请求。
    async def no_reply(_data: dict, self_id: str | None = None) -> None:
        return None

    conn.send = no_reply  # type: ignore[method-assign]
    task = asyncio.create_task(
        adapter.send_text(
            Target(adapter="default", scope="private", target_id="42"), "hi"
        )
    )
    await asyncio.sleep(0)
    await adapter.stop()
    with pytest.raises(AdapterNotConnectedError):
        await asyncio.wait_for(task, timeout=1.0)


async def test_self_sent_event_is_ignored() -> None:
    conn = FakeConnection()
    adapter = OneBot11Adapter("default", conn, dialect=NAPCAT_DIALECT)
    received: list[Any] = []

    async def on_event(event) -> None:
        received.append(event)

    adapter.subscribe(on_event)
    payload = {
        "post_type": "message_sent",
        "message_type": "private",
        "time": 1,
        "self_id": 100,
        "message_id": 5,
        "user_id": 42,
        "raw_message": "hello",
        "message": [{"type": "text", "data": {"text": "hello"}}],
        "sender": {"nickname": "Bot"},
    }
    await conn.deliver(payload)
    assert received == []


async def test_structured_action_reply_and_face() -> None:
    from adapters.outbound import action_to_segments

    conn = FakeConnection()
    adapter = OneBot11Adapter("default", conn, dialect=NAPCAT_DIALECT)
    segments = action_to_segments(
        {
            "kind": "face",
            "face_id": "178",
            "content": "",
            "reply_to": "9",
            "mentions": ["7"],
        }
    )
    await adapter.send_message(
        Target(adapter="default", scope="group", target_id="99"), segments
    )
    message = conn.sent[0]["params"]["message"]
    assert message[0] == {"type": "reply", "data": {"id": "9"}}
    assert message[1] == {"type": "at", "data": {"qq": "7"}}
    assert message[-1] == {"type": "face", "data": {"id": "178"}}
