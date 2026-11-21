"""OneBot 11 实现端方言（Dialect）能力表。

OneBot 11 的 **标准公开 API** 是共同基线；各协议端（NapCat / LLOneBot /
Lagrange / go-cqhttp）在此之上提供不同的扩展动作。本模块把差异收敛为：

    logical_action  →  wire_action（或 None 表示该方言不支持）

业务层只调用 ``OneBot11Adapter`` 的语义化方法，永远看不到 wire action。
新增方言只需在这里加一条 ``DialectSpec``，不需要改 ``core/`` 或 ``tools/``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

from adapters.base import AdapterCapabilities

logger = logging.getLogger(__name__)

# 逻辑动作名（适配器内部使用）
ACTION_GET_MESSAGE = "get_message"
ACTION_SEND_POKE = "send_poke"
ACTION_SET_REACTION = "set_reaction"
ACTION_VOICE_STT = "voice_stt"
ACTION_GROUP_HISTORY = "group_history"
ACTION_UPLOAD_GROUP_FILE = "upload_group_file"
ACTION_UPLOAD_PRIVATE_FILE = "upload_private_file"
ACTION_GET_FILE = "get_file"
ACTION_GET_IMAGE = "get_image"

# OneBot 11 标准公开 API 共同能力
STANDARD_CAPABILITIES = AdapterCapabilities(
    send_image=True,
    send_voice=True,
    send_video=True,
    upload_file=False,
    recall=True,
    reply=True,
    mention=True,
    poke=False,
    emoji_reaction=False,
    voice_stt=False,
    forward_read=True,
    group_history=False,
    get_message=True,
    list_friends=True,
    list_groups=True,
    list_group_members=True,
    friend_request=True,
    group_request=True,
    group_admin=True,
    proactive_private=True,
    proactive_group=True,
)


@dataclass(frozen=True, slots=True)
class DialectSpec:
    name: str
    display_name: str
    actions: dict[str, str] = field(default_factory=dict)
    capabilities: AdapterCapabilities = STANDARD_CAPABILITIES

    def action(self, logical_name: str) -> str | None:
        return self.actions.get(logical_name)


NAPCAT_DIALECT = DialectSpec(
    name="napcat",
    display_name="NapCatQQ",
    actions={
        ACTION_GET_MESSAGE: "get_msg",
        ACTION_SEND_POKE: "send_poke",
        ACTION_SET_REACTION: "set_msg_emoji_like",
        ACTION_VOICE_STT: "fetch_ptt_text",
        ACTION_GROUP_HISTORY: "get_group_msg_history",
        ACTION_UPLOAD_GROUP_FILE: "upload_group_file",
        ACTION_UPLOAD_PRIVATE_FILE: "upload_private_file",
        ACTION_GET_FILE: "get_file",
        ACTION_GET_IMAGE: "get_image",
    },
    capabilities=replace(
        STANDARD_CAPABILITIES,
        send_image=True,
        send_voice=True,
        send_video=True,
        upload_file=True,
        recall=True,
        poke=True,
        emoji_reaction=True,
        voice_stt=True,
        forward_read=True,
        group_history=True,
        get_message=True,
        group_admin=True,
    ),
)


LLONEBOT_DIALECT = DialectSpec(
    name="llonebot",
    display_name="LLOneBot",
    actions={
        ACTION_GET_MESSAGE: "get_msg",
        ACTION_SEND_POKE: "send_poke",
        ACTION_GROUP_HISTORY: "get_group_msg_history",
        ACTION_UPLOAD_GROUP_FILE: "upload_group_file",
        ACTION_UPLOAD_PRIVATE_FILE: "upload_private_file",
        ACTION_GET_FILE: "get_file",
        ACTION_GET_IMAGE: "get_image",
    },
    capabilities=replace(
        STANDARD_CAPABILITIES,
        upload_file=True,
        poke=True,
        group_history=True,
    ),
)


LAGRANGE_DIALECT = DialectSpec(
    name="lagrange",
    display_name="Lagrange.OneBot",
    actions={
        ACTION_GET_MESSAGE: "get_msg",
        ACTION_GROUP_HISTORY: "get_group_msg_history",
        ACTION_GET_FILE: "get_file",
        ACTION_GET_IMAGE: "get_image",
    },
    capabilities=replace(STANDARD_CAPABILITIES, group_history=True),
)


GOCQHTTP_DIALECT = DialectSpec(
    name="gocqhttp",
    display_name="go-cqhttp",
    actions={
        ACTION_GET_MESSAGE: "get_msg",
        ACTION_SEND_POKE: "send_poke",
        ACTION_GROUP_HISTORY: "get_group_msg_history",
        ACTION_UPLOAD_GROUP_FILE: "upload_group_file",
        ACTION_UPLOAD_PRIVATE_FILE: "upload_private_file",
        ACTION_GET_FILE: "get_file",
    },
    capabilities=replace(
        STANDARD_CAPABILITIES,
        upload_file=True,
        poke=True,
        group_history=True,
    ),
)


GENERIC_DIALECT = DialectSpec(
    name="generic",
    display_name="通用 OneBot 11",
    actions={
        ACTION_GET_MESSAGE: "get_msg",
        ACTION_GET_FILE: "get_file",
        ACTION_GET_IMAGE: "get_image",
    },
    capabilities=STANDARD_CAPABILITIES,
)


DIALECTS: dict[str, DialectSpec] = {
    "napcat": NAPCAT_DIALECT,
    "llonebot": LLONEBOT_DIALECT,
    "lagrange": LAGRANGE_DIALECT,
    "gocqhttp": GOCQHTTP_DIALECT,
    "generic": GENERIC_DIALECT,
}

DIALECT_NAMES = ("auto", "napcat", "llonebot", "lagrange", "gocqhttp", "generic")


def resolve_dialect(name: str | None, adapter_type: str = "onebot11") -> DialectSpec:
    """解析配置中的 dialect。

    ``auto`` 的降级规则：
      - ``type: napcat``  → NapCat 方言（保证存量用户零配置拿到全部扩展能力）；
      - ``type: onebot11`` → generic 方言（严格标准，任何实现端都能跑）。
    """
    key = str(name or "auto").strip().lower()
    if key in {"", "auto"}:
        key = "napcat" if str(adapter_type).lower() == "napcat" else "generic"
    dialect = DIALECTS.get(key)
    if dialect is None:
        logger.warning("未知 OneBot11 方言 %r，回退到 generic", name)
        return GENERIC_DIALECT
    return dialect


__all__ = [
    "ACTION_GET_MESSAGE",
    "ACTION_SEND_POKE",
    "ACTION_SET_REACTION",
    "ACTION_VOICE_STT",
    "ACTION_GROUP_HISTORY",
    "ACTION_UPLOAD_GROUP_FILE",
    "ACTION_UPLOAD_PRIVATE_FILE",
    "ACTION_GET_FILE",
    "ACTION_GET_IMAGE",
    "STANDARD_CAPABILITIES",
    "DialectSpec",
    "NAPCAT_DIALECT",
    "LLONEBOT_DIALECT",
    "LAGRANGE_DIALECT",
    "GOCQHTTP_DIALECT",
    "GENERIC_DIALECT",
    "DIALECTS",
    "DIALECT_NAMES",
    "resolve_dialect",
]
