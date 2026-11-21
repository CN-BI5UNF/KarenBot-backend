"""兼容旧路径：NapCatAdapter 已泛化为 OneBot11Adapter。"""

from __future__ import annotations

from adapters.onebot11.adapter import OneBot11Adapter

NapCatAdapter = OneBot11Adapter

__all__ = ["NapCatAdapter", "OneBot11Adapter"]
