"""适配器契约 / 能力过滤 / 配置迁移测试。"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from adapters.base import AdapterCapabilities
from adapters.base import IAdapter as _BaseAdapter
from adapters.onebot11.adapter import OneBot11Adapter
from adapters.onebot11.dialects import GENERIC_DIALECT, NAPCAT_DIALECT, resolve_dialect
from adapters.registry import build_adapter
from adapters.types import Target
from app_config.schema import (
    AdapterConfig,
    NapCatAdapterConfig,
    OneBot11AdapterConfig,
    WhitelistConfig,
)
from tools import build_default_registry


class _Secrets:
    def get(self, _key: str) -> None:
        return None


def _test_config():
    from app_config.schema import RootConfig

    cfg = RootConfig.model_construct()
    cfg.behavior = cfg.behavior.model_copy(update={"tool_schema_profile": "compact"})
    return cfg


def test_dialect_resolution_defaults() -> None:
    assert resolve_dialect("auto", "napcat") is NAPCAT_DIALECT
    assert resolve_dialect("auto", "onebot11") is GENERIC_DIALECT
    assert resolve_dialect("napcat", "onebot11") is NAPCAT_DIALECT
    assert resolve_dialect("unknown", "onebot11") is GENERIC_DIALECT


def test_napcat_and_generic_capabilities_differ() -> None:
    assert NAPCAT_DIALECT.capabilities.poke is True
    assert NAPCAT_DIALECT.capabilities.voice_stt is True
    assert NAPCAT_DIALECT.capabilities.group_history is True
    assert GENERIC_DIALECT.capabilities.poke is False
    assert GENERIC_DIALECT.capabilities.voice_stt is False
    assert GENERIC_DIALECT.capabilities.group_history is False
    assert GENERIC_DIALECT.capabilities.get_message is True


def test_build_adapter_uses_type_and_dialect() -> None:
    napcat = build_adapter(
        "default",
        "napcat",
        cfg=NapCatAdapterConfig(),
        secrets=_Secrets(),
    )
    assert isinstance(napcat, OneBot11Adapter)
    assert napcat.dialect is NAPCAT_DIALECT
    assert napcat.capabilities.poke is True

    generic = build_adapter(
        "alt",
        "onebot11",
        cfg=OneBot11AdapterConfig(),
        secrets=_Secrets(),
    )
    assert generic.dialect is GENERIC_DIALECT
    assert generic.capabilities.poke is False


async def test_unsupported_optional_methods_raise() -> None:
    adapter = OneBot11Adapter("generic", None, dialect=GENERIC_DIALECT)
    target = Target(adapter="generic", scope="group", target_id="1")

    with pytest.raises(NotImplementedError):
        await adapter.send_poke(target, "2")
    with pytest.raises(NotImplementedError):
        await adapter.fetch_voice_text("3")
    with pytest.raises(NotImplementedError):
        await adapter.get_group_history("1")


def test_tool_registry_filters_by_capability() -> None:
    cfg = _test_config()

    generic = build_default_registry(
        cfg, capabilities=AdapterCapabilities(poke=False, group_admin=False)
    )
    assert "send_poke" not in generic.names()
    assert "set_group_ban" not in generic.names()

    napcat = build_default_registry(
        cfg, capabilities=AdapterCapabilities(poke=True, group_admin=True)
    )
    assert "send_poke" in napcat.names()
    assert "set_group_ban" in napcat.names()


def test_config_discriminated_union_and_legacy_mode() -> None:
    adapter = TypeAdapter(AdapterConfig)

    napcat = adapter.validate_python({"type": "napcat", "mode": "server"})
    assert isinstance(napcat, NapCatAdapterConfig)
    assert napcat.transport == "ws_server"
    assert napcat.mode == "server"

    onebot = adapter.validate_python({"type": "onebot11", "transport": "http"})
    assert isinstance(onebot, OneBot11AdapterConfig)
    assert onebot.transport == "http"
    assert onebot.mode == "client"


def test_whitelist_ids_are_strings() -> None:
    whitelist = WhitelistConfig(qq_ids=[123, "456"], group_ids=["789"])
    assert whitelist.qq_ids == ["123", "456"]
    assert whitelist.group_ids == ["789"]
    assert whitelist.user_ids == ["123", "456"]


def test_adapter_capabilities_supports_unknown_is_false() -> None:
    caps = AdapterCapabilities()
    assert caps.supports("send_image") is True
    assert caps.supports("not_a_capability") is False


def test_negative_telegram_group_ids_are_supported() -> None:
    from app_config.schema import GroupTriggerRuleConfig
    from core.group_chat_selection import build_group_chat_selection

    whitelist = WhitelistConfig(group_ids=[-100123])
    assert whitelist.group_ids == ["-100123"]

    selection = build_group_chat_selection(mode="selected", group_ids=["-100123"])
    assert selection.allows("-100123", is_mention=False) is True
    assert selection.allows("-100999", is_mention=False) is False

    rule = GroupTriggerRuleConfig(group_id=-100123)
    assert rule.group_id == -100123


class _LegacyAdapter(_BaseAdapter):
    """模拟只实现 send_text/send_image 的旧适配器。"""

    def __init__(self) -> None:
        _BaseAdapter.__init__(self, "legacy")
        self.sent_text: list[str] = []
        self.sent_images: list[str] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    @property
    def is_connected(self) -> bool:
        return True

    async def send_text(self, target, content: str):
        self.sent_text.append(content)
        return "t1"

    async def send_image(self, target, *, image_path=None, image_url=None, image_b64=None):
        self.sent_images.append(str(image_path or image_url or image_b64))
        return "i1"

    async def recall(self, message_id: str) -> bool:
        return True

    async def list_friends(self):
        return []

    async def list_groups(self):
        return []

    async def list_group_members(self, group_id: str):
        return []

    async def get_user_info(self, user_id: str):
        raise NotImplementedError

    async def handle_friend_request(self, flag, approve, remark=""):
        return None

    async def handle_group_request(self, flag, sub_type, approve, reason=""):
        return None

    async def call_api(self, action, **params):
        return {}


async def test_base_send_message_falls_back_to_legacy_methods() -> None:
    from adapters.types import ImageSegment, MentionSegment, TextSegment

    adapter = _LegacyAdapter()
    target = Target(adapter="legacy", scope="private", target_id="1")

    msg_id = await adapter.send_message(
        target, [TextSegment(text="hi"), MentionSegment(user_id="2")]
    )
    assert msg_id == "t1"
    assert adapter.sent_text == ["hi@2"]

    msg_id = await adapter.send_message(
        target, [ImageSegment(url="https://example.com/a.jpg")]
    )
    assert msg_id == "i1"
    assert adapter.sent_images == ["https://example.com/a.jpg"]


def test_root_config_injects_legacy_adapter_type() -> None:
    from app_config.schema import RootConfig

    cfg = RootConfig.model_validate(
        {
            "agents": {"chat": {"provider": "x", "model": "y"}},
            "providers": {"x": {"protocol": "openai_compat", "base_url": "http://localhost:1"}},
            "adapters": {"default": {"mode": "server"}},
        }
    )
    assert cfg.adapters["default"].type == "napcat"
    assert cfg.adapters["default"].transport == "ws_server"


def test_non_loopback_ws_server_requires_token() -> None:
    cfg = OneBot11AdapterConfig(transport="ws_server", host="0.0.0.0")
    with pytest.raises(ValueError):
        build_adapter("x", "onebot11", cfg=cfg, secrets=_Secrets())


def test_non_loopback_webhook_requires_secret() -> None:
    cfg = OneBot11AdapterConfig(
        transport="http",
        http_api_url="http://127.0.0.1:3000",
        webhook_enabled=True,
        webhook_host="0.0.0.0",
        webhook_path="/hook",
    )
    with pytest.raises(ValueError):
        build_adapter("x", "onebot11", cfg=cfg, secrets=_Secrets())


def test_voice_source_url_must_be_public() -> None:
    from core.media_pipeline import _is_public_http_url

    assert _is_public_http_url("https://example.com/a.ogg") is True
    assert _is_public_http_url("http://127.0.0.1/a.ogg") is False
    assert _is_public_http_url("http://192.168.1.2/a.ogg") is False
    assert _is_public_http_url("file:///etc/passwd") is False
    assert _is_public_http_url("http://localhost/a.ogg") is False


def test_thread_id_only_for_matching_group() -> None:
    from types import SimpleNamespace

    from tools.messaging import _thread_id_for_target

    ctx = SimpleNamespace(
        extras={
            "default_reply_target": {
                "target_type": "group",
                "target_id": "-100",
                "thread_id": "77",
            }
        }
    )
    assert _thread_id_for_target(ctx, "group", "-100") == "77"
    assert _thread_id_for_target(ctx, "group", "-200") == ""
    assert _thread_id_for_target(ctx, "private", "42") == ""
