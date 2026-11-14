# plugins/ —— 应用插件

这个目录用于 KarenBot 的**应用级插件**，与 `model_plugins/` 分开：

- `plugins/`：前端插件、后端功能插件（后续）
- `model_plugins/`：TTS / Embedding 等本地模型插件

当前版本只完成了**前端插件清单 + 统一入口**和**后端插件协议骨架**；
`plugins/` 下暂时不扫描、不加载任何后端插件。未来后端插件会使用
`plugins/<id>/plugin.json`，规范见 `PLUGIN_SPEC.md`。

内置前端插件不在这里，而在各自的模块目录：

- `gui/plugin.json` + `gui/plugin.py`
- `webui/plugin.json` + `webui/plugin.py`
