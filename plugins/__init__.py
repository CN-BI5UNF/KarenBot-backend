"""KarenBot 应用插件根目录。

这里放“应用级插件”（前端插件、后端功能插件），与 ``model_plugins/`` 里的
TTS/Embedding 模型插件分层。当前版本只提供协议骨架；后端插件加载器尚未启用。

前端插件清单位于 ``gui/plugin.json`` / ``webui/plugin.json``。
应用插件规范见 ``plugins/PLUGIN_SPEC.md``。
"""

from __future__ import annotations

__all__: list[str] = []
