"""OneBot 11 适配器（标准协议 + 方言）。

模块组成：
    adapter.py   —— OneBot11Adapter（IAdapter 实现）
    api_call.py  —— WS echo / HTTP API 双通道调用器
    events.py    —— OneBot 11 JSON → 统一事件
    segments.py  —— 数组 / CQ 字符串 ↔ MessageSegment IR
    dialects.py  —— NapCat / LLOneBot / Lagrange / go-cqhttp / generic 能力表
    process.py   —— 可选的协议端进程托管

导入本模块时自动注册两种适配器类型：
    - ``onebot11``：严格标准，默认 generic 方言
    - ``napcat``  ：向后兼容别名，默认 NapCat 方言
"""

from __future__ import annotations

from adapters.registry import known_adapter_types, register_adapter_type
from adapters.transports.ws import (
    ForwardWSConnection,
    NapCatConnection,
    ReverseWSConnection,
    WSClientConnection,
    WSConnection,
    WSServerConnection,
)

from .adapter import OneBot11Adapter
from .api_call import NapCatApiCaller, OneBot11ApiCaller
from .dialects import (
    DIALECT_NAMES,
    DIALECTS,
    GENERIC_DIALECT,
    NAPCAT_DIALECT,
    DialectSpec,
    resolve_dialect,
)
from .events import parse_napcat_event, parse_onebot11_event
from .segments import (
    cq_to_segments,
    segment_from_onebot,
    segment_to_cq,
    segment_to_onebot,
    segment_to_text,
    segments_from_onebot,
    segments_to_cq,
    segments_to_message,
    segments_to_onebot,
    segments_to_text,
)

# 兼容旧名称：NapCatAdapter 现在就是 OneBot11Adapter + napcat 方言
NapCatAdapter = OneBot11Adapter

# 注册适配器类型（幂等）
if "onebot11" not in known_adapter_types():
    register_adapter_type("onebot11", OneBot11Adapter.from_config)
if "napcat" not in known_adapter_types():
    register_adapter_type("napcat", OneBot11Adapter.from_config)


__all__ = [
    "OneBot11Adapter",
    "NapCatAdapter",
    "OneBot11ApiCaller",
    "NapCatApiCaller",
    "WSConnection",
    "WSClientConnection",
    "WSServerConnection",
    "NapCatConnection",
    "ReverseWSConnection",
    "ForwardWSConnection",
    "parse_onebot11_event",
    "parse_napcat_event",
    "DialectSpec",
    "DIALECTS",
    "DIALECT_NAMES",
    "NAPCAT_DIALECT",
    "GENERIC_DIALECT",
    "resolve_dialect",
    "cq_to_segments",
    "segments_from_onebot",
    "segments_to_cq",
    "segments_to_onebot",
    "segments_to_message",
    "segments_to_text",
    "segment_from_onebot",
    "segment_to_onebot",
    "segment_to_cq",
    "segment_to_text",
]
