"""兼容旧路径：NapCatApiCaller 已泛化为 OneBot11ApiCaller。"""

from __future__ import annotations

from adapters.onebot11.api_call import NapCatApiCaller, OneBot11ApiCaller

__all__ = ["NapCatApiCaller", "OneBot11ApiCaller"]
