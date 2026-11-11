# KarenBot-backend

KarenBot 的**共享后端控制面**模块。仓库当前为私有开发仓库，版本 **v0.0.1-dev**，后续计划公开。

> 这是模块源码仓库，不是可独立启动的完整 KarenBot 应用。它需要配合 KarenBot 宿主运行时
> （`core/`、`agents/`、`providers/`、`app_config/`、`features/`、`memory/` 等）使用。

## 这是什么

`backend/` 把 WebUI 和原生 GUI 共用的控制面集中在一个包里：

- FastAPI 应用工厂：`backend.app.create_control_app()`
- Runtime 生命周期宿主：`backend.runtime_host.RuntimeHost`
- 页面 API 与 WebSocket：`backend/api/`、`backend/events.py`
- 浏览器/桌面共用的设置声明、配置提交、向导、聊天归一化、Provider/人格管理
- 令牌鉴权、日志环形缓冲、更新检查等运行时基础设施

WebUI 只负责静态资源和 HTTP 入口，原生 GUI 只负责本机回环服务和 GTK4 控件；
两边不再复制后端逻辑。

## 兼容性

| 项目 | 版本 |
|---|---|
| 本模块 release | `v0.0.1-dev`（预发布） |
| 兼容的 KarenBot 宿主 | `1.0.1-alpha.1` 拆分版本及后续兼容版本 |
| 配套 WebUI 模块 | `KarenBot-WebUI` `v0.0.1-dev` |
| 配套 GUI 模块 | `KarenBot-GUI` `v0.0.1-dev` |

仓库根目录的 `VERSION` 是模块发布版本；`backend/__init__.py` 中的代码版本沿用宿主兼容版本
`1.0.1-alpha.1`。两者用途不同，不冲突。

## 目录

```text
backend/
├── app.py              create_control_app()：FastAPI 装配 + lifespan + 鉴权 + /ws/events
├── runtime_host.py     Runtime 生命周期、配置落盘、首次配置
├── auth.py             令牌与会话
├── events.py           log / chat / status 事件集线器
├── logbuffer.py        日志环形缓冲
├── settings_spec.py    设置页字段声明与热应用分类
├── config_edit.py      校验后的递归配置合并
├── api/                HTTP / WebSocket 路由
├── chatlog/            对话显示项归一化、分组、缓存
├── wizard/             首次配置步骤、流转与文案
├── persona_admin.py    人格生成、改写、落盘
├── provider_admin.py   Provider 增删改
├── emoji_store.py      表情包文件校验与读写
├── model_guide.py      本地模型安装指引
└── copy.py             用户可见中文文案
```

## 集成方式

把本仓库的 `backend/` 放到 KarenBot 宿主项目根目录（与 `core/`、`webui/`、`gui/` 平级）：

```bash
# 在 KarenBot 根目录
cp -a /path/to/KarenBot-backend/backend ./backend
```

宿主项目仍需要自己的 `pyproject.toml` 包含：

```toml
[tool.setuptools.packages.find]
include = [
    "backend*",
    # ... 其它宿主包
]
```

安装宿主依赖后，WebUI 可以用共享工厂装配应用：

```python
from backend.app import create_control_app
from backend.auth import TokenAuth
from backend.runtime_host import RuntimeHost

host = RuntimeHost(project_root=PROJECT_ROOT)
auth = TokenAuth(token_value, enabled=True, source="app")
app = create_control_app(
    host,
    auth,
    title="KarenBot",
    docs=False,
)
```

原生 GUI 使用同一个工厂，但通过 `127.0.0.1` 回环 HTTP 连接，不挂载浏览器静态资源。

## 热设置

设置保存策略由 `backend/settings_spec.py` 决定。当前 179 个设置字段中：

- **159 个**保存后通过递归配置合并热应用；
- **20 个**结构性字段（容器存储模式/大小、RAG/Embedding、人格后台启停等）明确提示重启；
- 保存重启型字段后，后续修改会排队等待同一次重启，避免半热应用；
- Provider / Agent 修改会热重建，失败时才要求重启。

## 安全边界

- 本仓库只包含源码，不包含 `config.yaml`、`secrets.enc`、密钥、聊天记录、日志或数据目录。
- 访问令牌 / API Key / RSA 私钥不要提交到仓库；`.gitignore` 已覆盖常见敏感文件。
- 后端 API 默认需要令牌；部署时不要把控制面直接暴露到不可信网络。

## 版本与发布

- 版本标签格式：`vMAJOR.MINOR.PATCH[-dev]`
- 当前为 `v0.0.1-dev`，是**预发布开发版**，接口可能变化。
- GitHub Releases 会自动提供源码归档；每个 release 同时附带模块源码 zip。
- 不兼容变更必须升 `MAJOR` 并在 release notes 中写明宿主兼容范围。

## 许可证与来源

本项目沿用 KarenBot 的 **Apache-2.0** 许可证，并保留上游 Debata_Agent 的版权声明，
详见 `LICENSE` 与 `NOTICE`。

相关仓库：

- [KarenBot-WebUI](https://github.com/CN-BI5UNF/KarenBot-WebUI)
- [KarenBot-GUI](https://github.com/CN-BI5UNF/KarenBot-GUI)
