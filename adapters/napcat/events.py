"""兼容旧路径：事件解析已迁移到 ``adapters.onebot11.events``。"""

from __future__ import annotations

from adapters.onebot11.events import parse_napcat_event, parse_onebot11_event

__all__ = ["parse_napcat_event", "parse_onebot11_event"]
