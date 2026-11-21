"""TelegramAdapter 发送/撤回/表情回应测试（假 API 客户端）。"""

from __future__ import annotations

from typing import Any

from adapters.base import AdapterCapabilities
from adapters.telegram.adapter import TelegramAdapter
from adapters.types import (
    ImageSegment,
    MentionSegment,
    ReplySegment,
    Target,
    TextSegment,
)
from app_config.schema import TelegramAdapterConfig


class FakeTelegramClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], Any]] = []
        self.started = False
        self._message_id = 0

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    @property
    def is_ready(self) -> bool:
        return self.started

    async def call(self, method: str, params=None, *, files=None, timeout=None):
        self.calls.append((method, dict(params or {}), files))
        if method == "getMe":
            return {"id": 999, "is_bot": True, "username": "karen_bot"}
        self._message_id += 1
        return {"message_id": self._message_id}


def _adapter(**kwargs) -> tuple[TelegramAdapter, FakeTelegramClient]:
    client = FakeTelegramClient()
    return (
        TelegramAdapter("tg", client, **kwargs),
        client,
    )


async def test_send_text_with_reply_and_mention() -> None:
    adapter, client = _adapter(parse_mode="HTML")
    target = Target(adapter="tg", scope="private", target_id="42")
    msg_id = await adapter.send_message(
        target,
        [
            ReplySegment(message_id="7"),
            TextSegment(text="hi "),
            MentionSegment(user_id="9", display_name="Bob"),
        ],
    )

    assert msg_id == "1"
    method, params, files = client.calls[-1]
    assert method == "sendMessage"
    assert params["chat_id"] == "42"
    assert params["parse_mode"] == "HTML"
    assert params["reply_parameters"]["message_id"] == 7
    assert '<a href="tg://user?id=9">@Bob</a>' in params["text"]
    assert files is None


async def test_send_group_thread_parameter() -> None:
    adapter, client = _adapter()
    target = Target(
        adapter="tg", scope="group", target_id="-100", thread_id="77"
    )
    await adapter.send_text(target, "topic")
    method, params, _ = client.calls[-1]
    assert method == "sendMessage"
    assert params["message_thread_id"] == 77


async def test_long_text_is_chunked() -> None:
    adapter, client = _adapter(parse_mode="none")
    target = Target(adapter="tg", scope="private", target_id="42")
    await adapter.send_text(target, "x" * 5000)
    sends = [call for call in client.calls if call[0] == "sendMessage"]
    assert len(sends) == 2
    assert all(len(call[1]["text"]) <= 4096 for call in sends)
    assert sum(len(call[1]["text"]) for call in sends) == 5000


async def test_send_image_url_and_reply() -> None:
    adapter, client = _adapter()
    target = Target(adapter="tg", scope="private", target_id="42")
    await adapter.send_message(
        target,
        [
            ReplySegment(message_id="5"),
            ImageSegment(url="https://example.com/a.jpg"),
        ],
    )
    method, params, files = client.calls[-1]
    assert method == "sendPhoto"
    assert params["photo"] == "https://example.com/a.jpg"
    assert params["reply_parameters"]["message_id"] == 5
    assert files is None


async def test_recall_uses_cached_chat() -> None:
    adapter, client = _adapter()
    target = Target(adapter="tg", scope="private", target_id="42")
    msg_id = await adapter.send_text(target, "hi")
    assert await adapter.recall(str(msg_id)) is True
    method, params, _ = client.calls[-1]
    assert method == "deleteMessage"
    assert params == {"chat_id": "42", "message_id": 1}


async def test_recall_unknown_message_returns_false() -> None:
    adapter, _ = _adapter()
    assert await adapter.recall("404") is False


async def test_set_reaction() -> None:
    adapter, client = _adapter()
    target = Target(adapter="tg", scope="group", target_id="-100")
    await adapter.set_reaction(target, "8", "👍")
    method, params, _ = client.calls[-1]
    assert method == "setMessageReaction"
    assert params["reaction"] == [{"type": "emoji", "emoji": "👍"}]

    await adapter.set_reaction(target, "8", "👍", remove=True)
    _, params, _ = client.calls[-1]
    assert params["reaction"] == []


def test_capabilities_disable_qq_only_features() -> None:
    caps = TelegramAdapter("tg", FakeTelegramClient()).capabilities
    assert isinstance(caps, AdapterCapabilities)
    assert caps.send_voice is True
    assert caps.emoji_reaction is True
    assert caps.poke is False
    assert caps.group_admin is False
    assert caps.group_history is False


def test_config_defaults() -> None:
    cfg = TelegramAdapterConfig(bot_token_id="tg_main")
    assert cfg.type == "telegram"
    assert cfg.transport == "long_polling"
    assert cfg.parse_mode == "HTML"
    assert cfg.api_timeout_seconds == 30.0


class _Secrets:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, key: str):
        return self.values.get(key)


async def test_from_config_builds_webhook_with_secret() -> None:
    import httpx

    from app_config.schema import TelegramAdapterConfig

    cfg = TelegramAdapterConfig(
        type="telegram",
        bot_token_id="tg_token",
        transport="webhook",
        webhook_port=18443,
        webhook_path="/tg-hook",
        webhook_secret_token_id="tg_secret",
    )
    adapter = TelegramAdapter.from_config(
        "tg",
        cfg,
        _Secrets({"tg_token": "123:abc", "tg_secret": "s3cret"}),
    )
    assert adapter._webhook is not None

    transport = httpx.ASGITransport(app=adapter._webhook.app)
    body = '{"update_id": 1, "message": {"message_id": 1, "date": 1, "chat": {"id": 1, "type": "private"}, "from": {"id": 1, "first_name": "A"}, "text": "hi"}}'
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        denied = await client.post("/tg-hook", content=body)
        allowed = await client.post(
            "/tg-hook",
            content=body,
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )
    assert denied.status_code == 401
    assert allowed.status_code == 204


async def test_media_resolver_returns_local_path() -> None:
    import tempfile
    from pathlib import Path

    class _MediaClient(FakeTelegramClient):
        async def call(self, method, params=None, *, files=None, timeout=None):
            self.calls.append((method, dict(params or {}), files))
            if method == "getFile":
                return {"file_id": "f1", "file_path": "photos/file_1.jpg"}
            return await super().call(method, params, files=files, timeout=timeout)

        async def download_file(self, file_path: str) -> bytes:
            assert file_path == "photos/file_1.jpg"
            return b"fake-image-bytes"

    adapter = TelegramAdapter("tg", _MediaClient())
    path = await adapter.get_image_url("f1")
    assert path is not None
    local = Path(path)
    assert local.is_file()
    assert local.read_bytes() == b"fake-image-bytes"
    assert local.parent == Path(tempfile.gettempdir()) / "karenbot-telegram"
    local.unlink(missing_ok=True)


async def test_recall_refuses_ambiguous_message_id_across_chats() -> None:
    adapter, client = _adapter()
    # 两个不同 chat 的消息 ID 都是 1（Telegram 的 message_id 只在会话内唯一）
    await adapter.send_text(Target(adapter="tg", scope="private", target_id="42"), "a")
    await adapter.send_text(Target(adapter="tg", scope="private", target_id="43"), "b")
    # Fake 客户端每次返回递增 ID，这里手动构造冲突
    adapter._remember_chat("1", "-100")
    assert await adapter.recall("1") is False
    assert all(call[0] != "deleteMessage" for call in client.calls)


async def test_html_entities_are_not_split() -> None:
    adapter, client = _adapter(parse_mode="HTML")
    target = Target(adapter="tg", scope="private", target_id="42")
    await adapter.send_text(target, "&" * 5000)
    sends = [call for call in client.calls if call[0] == "sendMessage"]
    assert sends
    for _, params, _ in sends:
        text = params["text"]
        assert len(text) <= 4096
        assert text.replace("&amp;", "") == ""


async def test_long_emoji_text_uses_utf16_length() -> None:
    from adapters.telegram.adapter import _utf16_len

    adapter, client = _adapter(parse_mode="none")
    target = Target(adapter="tg", scope="private", target_id="42")
    await adapter.send_text(target, "😀" * 3000)
    sends = [call for call in client.calls if call[0] == "sendMessage"]
    assert len(sends) >= 2
    assert all(_utf16_len(params["text"]) <= 4096 for _, params, _ in sends)
    assert sum(_utf16_len(params["text"]) for _, params, _ in sends) == 6000


def test_non_loopback_telegram_webhook_requires_secret() -> None:
    import pytest

    from adapters.base import AdapterAPIError

    cfg = TelegramAdapterConfig(
        bot_token_id="tg_token",
        transport="webhook",
        webhook_host="0.0.0.0",
        webhook_path="/tg",
    )
    with pytest.raises(AdapterAPIError):
        TelegramAdapter.from_config(
            "tg", cfg, _Secrets({"tg_token": "123:abc"})
        )


async def test_markdownv2_chunks_do_not_end_with_lone_backslash() -> None:
    adapter, client = _adapter(parse_mode="MarkdownV2")
    target = Target(adapter="tg", scope="private", target_id="42")
    await adapter.send_text(target, "!" * 5000)
    sends = [call for call in client.calls if call[0] == "sendMessage"]
    assert sends
    for _, params, _ in sends:
        text = params["text"]
        assert not text.endswith("\\")
        # 每个特殊字符都被转义；未出现孤立反斜杠
        assert text.count("!") == text.count("\\!")
