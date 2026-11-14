# 应用插件架构（前端插件 + 后端插件骨架）

## 为什么做

KarenBot 已经拆成 `backend/`、`gui/`、`webui/` 三个模块。为了让前端不只是
“靠入口文件名区分”，并给后续后端功能插件打基础，引入一层应用插件协议：

```text
main.py / karen
  │
  ├─ backend.plugin_host.PluginContainer
  │    ├─ gui/plugin.json    → gui.plugin:GuiFrontendPlugin
  │    └─ webui/plugin.json  → webui.plugin:WebUIFrontendPlugin
  │
  └─ FastAPI 控制面（backend.app.create_control_app）
       └─ /api/plugins        # 只读前端插件状态
```

`backend/` 不静态导入 `gui/` 或 `webui/`；插件入口是清单中的字符串，只有
真正激活前端时才 import。

## 分层

| 层 | 目录 | 说明 |
|---|---|---|
| 应用插件 | `plugins/` | 前端/后端功能插件。当前只定义协议，后端加载器未启用 |
| 内置前端 | `gui/`、`webui/` | 各自带 `plugin.json` + `plugin.py` |
| 模型插件 | `model_plugins/` | 原 `plugins/` 的 TTS/Embedding 模型插件 |
| 后端控制面 | `backend/` | FastAPI、RuntimeHost、插件发现/前端状态 API |

模型插件和应用插件是两套不同生命周期：

- 模型插件由 `core.runtime` 的 `PluginManager` 扫描 `model_plugins/`，按
  TTS/Embedding 配置 build，负责模型下载、预热和释放。
- 应用插件描述“应用层能力”（前端、未来后端路由/后台任务），不负责模型文件。

## 当前实现范围

已完成：

- `backend/plugin_host.py`：manifest 校验、前端发现、`run(context)` 激活、
  `PluginContainer.snapshot()`。
- `gui/plugin.json`、`gui/plugin.py`、`webui/plugin.json`、`webui/plugin.py`。
- `main.py --frontend` / `--list-frontends` 统一入口。
- `GET /api/plugins` 返回前端插件状态和 active frontend。
- WebUI 现在会把 `frontend="webui"` 传给控制面，修复模块更新器身份为空。

尚未实现（有意保持骨架）：

- 扫描/加载 `plugins/<id>/plugin.json` 的后端插件。
- 后端插件 setup/startup/shutdown 生命周期。
- 插件安装、启停、权限、配置 schema、市场。
- GUI 改为连接外部后端进程；当前 GUI 仍由 `LocalControlServer` 自管回环后端。

## 插件生命周期（未来）

```text
discover（只读清单）
  → validate（id/kind/api_version/entry）
  → setup(app, context)       # FastAPI 构建期
  → startup()                 # lifespan 启动后
  → shutdown()                # lifespan 关闭前
```

失败隔离原则：单个插件的清单错误、导入错误、setup/startup 错误只影响该插件，
核心 API 和 Runtime 继续工作。

## 目录迁移

原 `plugins/` 下的 TTS/Embedding 模型插件已移动到 `model_plugins/`：

- Python 导入：`plugins.*` → `model_plugins.*`
- Runtime 扫描路径：`project_root/model_plugins`
- 模型文件仍放在 `data/models/`，不随代码移动

`plugins/` 现在只放应用插件；详细清单格式见
[`plugins/PLUGIN_SPEC.md`](../plugins/PLUGIN_SPEC.md)。
