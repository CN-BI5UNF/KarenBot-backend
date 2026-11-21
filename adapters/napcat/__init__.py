"""NapCat 兼容层（已迁移到 ``adapters.onebot11``）。

保留旧 import 路径 1-2 个版本：
    from adapters.napcat.adapter import NapCatAdapter
    from adapters.napcat.connection import ReverseWSConnection
"""

from __future__ import annotations

# 触发 onebot11 的类型注册
import adapters.onebot11  # noqa: F401
from adapters.onebot11 import (
    ForwardWSConnection,
    NapCatAdapter,
    NapCatApiCaller,
    NapCatConnection,
    ReverseWSConnection,
    parse_napcat_event,
)

__all__ = [
    "NapCatAdapter",
    "NapCatApiCaller",
    "NapCatConnection",
    "ReverseWSConnection",
    "ForwardWSConnection",
    "parse_napcat_event",
]
