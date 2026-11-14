# KarenBot 应用插件规范（Plugin Spec）

本目录用于**应用级插件**，与 `model_plugins/`（TTS / Embedding 模型插件）分层：

| 目录 | 用途 | 当前状态 |
|---|---|---|
| `gui/`、`webui/` | 内置前端插件 | 已接入统一入口 |
| `plugins/` | 应用插件根目录（前端/后端） | 本次只提供协议骨架 |
| `model_plugins/` | TTS / Embedding 模型插件 | 已存在，由 PluginManager 管理 |

当前版本**不会扫描或加载** `plugins/<id>/plugin.json` 的后端插件；这里先固定
清单和生命周期协议，后续加载器直接按此实现，不再改插件作者接口。

## 清单

每个插件目录包含 `plugin.json`：

```json
{
  "schema": 1,
  "id": "example",
  "name": "示例插件",
  "kind": "backend",
  "api_version": 1,
  "entry": "plugins.example.plugin:ExamplePlugin",
  "description": "一句话说明",
  "capabilities": ["api"]
}
```

字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema` | 否 | 清单格式版本，当前只支持 1 |
| `id` | 是 | 小写字母开头，可含数字、`_`、`.`、`-`；必须与目录名一致 |
| `name` | 否 | UI 显示名，默认 id |
| `kind` | 是 | `frontend` 或 `backend` |
| `api_version` | 是 | 宿主插件 API 版本，当前为 1 |
| `entry` | 是 | `module:attr`，attr 是类或实例；不要放文件路径 |
| `version` | 否 | 省略时依次读取插件目录/父目录 `VERSION`、`modules.json` |
| `description` | 否 | UI 描述 |
| `capabilities` | 否 | 字符串数组，供未来 UI/权限筛选 |

`entry` 模块必须在 Python 导入路径上；插件目录如果是包，需有
`__init__.py`（例如 `plugins/example/__init__.py`）。

## 前端插件

内置前端清单：

- `gui/plugin.json` → `gui.plugin:GuiFrontendPlugin`
- `webui/plugin.json` → `webui.plugin:WebUIFrontendPlugin`

契约：

```python
class FrontendPlugin:
    def run(self, context: PluginContext) -> int:
        """启动前端；返回退出码，0 表示正常结束。"""
```

`PluginContext` 提供 `project_root`、`config_file`、`host`、`port`、`token`、
`no_auth`、`argv`、`frontend`。GUI 插件的 `run` 会把 `argv` 透传给
`gui.launcher`；WebUI 插件会把 host/port/token 透传给 `webui.server.run_webui`。

统一入口：

```bash
python main.py --list-frontends
python main.py --frontend webui
python main.py --frontend gui --theme dark
```

`python -m gui` / `python -m webui.server` 仍保留为直接入口。

## 后端插件（协议骨架）

后端插件未来由后端容器加载；当前版本不扫描、不导入、不执行：

```python
class BackendPlugin:
    def setup(self, app, context: PluginContext) -> None:
        """构建 FastAPI 应用时调用；可注册路由/后台任务。"""

    async def startup(self) -> None:
        """应用启动后调用（可选）。"""

    async def shutdown(self) -> None:
        """应用关闭前调用（可选）。"""
```

未来加载器会：

1. 扫描 `plugins/*/plugin.json`；
2. 校验 `api_version`、id、entry；
3. 在核心路由之后调用 `setup(app, context)`；
4. 在 FastAPI lifespan 中调用 `startup`/`shutdown`；
5. 单个插件失败只记录错误，不影响核心服务。

在这些实现完成前，`/api/plugins` 只返回前端插件，不返回 `plugins/` 下的
后端插件。

## 版本与安全

- `api_version` 是宿主与插件之间的硬兼容边界；不匹配的插件列出但不激活。
- 插件代码在宿主进程内运行，拥有完整权限；只安装可信插件。
- 清单只读取 JSON，不执行代码；`entry` 必须符合 `module:attr`，不指向文件路径。
- 清单路径必须位于项目根内；拒绝越界/符号链接逃逸。
- 当前不提供远程安装、远程启停或权限沙箱；这些是后续独立设计。
