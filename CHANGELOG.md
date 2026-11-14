# Changelog

## Unreleased — 核心程序并入

- `main.py`、`main_cli.py`、`service_cli.py`、`core/`、`features/`、`model_plugins/`、`plugins/`、`app_config/`、`adapters/`、`agents/`、`memory/`、`mind/`、`providers/`、`tools/`、`utils/`、`packaging/`、`deploy/`、`docs/` 等核心程序并入本仓库。
- 本仓库现在提供“核心程序 + 后端”基础包；`gui/`、`webui/` 仍由各自模块仓库维护。
- 在线安装可以直接下载本仓库源码包，再叠加所选界面模块，不需要额外的第四个仓库。
- 不包含用户数据、配置、密钥、日志、模型权重和虚拟环境。
- 修复 `app_config/secrets.py` 被过宽的 `secrets.*` 忽略规则排除，导致在线安装启动失败的问题。

## v0.1.1-alpha.1 — 2026-11-15

- 与 `KarenBot-GUI`、`KarenBot-WebUI` 的 v0.1.1-alpha.1 配套。
- 新增应用插件协议骨架：`backend/plugin_host.py`（清单校验、前端发现/激活、`BackendPlugin` 协议）和 `GET /api/plugins`。
- 前端插件清单支持 `gui/plugin.json`、`webui/plugin.json`，统一入口可发现可用前端。
- 修复 WebUI 控制面未传 `frontend="webui"`，导致模块更新器身份为空、走 legacy 更新的问题。
- 同步最新聊天显示分页/持久索引、媒体附件、工作区文件下载/打开等后端能力。
- 修复附件原文件被存储清理后只剩 `[图片 workspace=...]` 裸文本：保留 `missing` 状态与原因，并从事件记录附带 QQ `remote_url` 作为远程恢复线索。
- 新增 `GET /api/chats/attachments/remote`：仅允许 QQ HTTPS 媒体域名，沿用 SSRF/重定向/大小限制，供已清理附件的回退下载。
- 更新页数据按组件返回；后端与前端 Release 仍由各自仓库发布。

## v0.1.1-alpha — 2026-11-13

首个公开发布版本。

- 与 `KarenBot-backend`、`KarenBot-GUI`、`KarenBot-WebUI` 的 v0.1.1-alpha 配套。
- 后端新增分模块更新器：同时检查 backend 与前端仓库，Latest 自动提示，Pre-release 仅允许手动选择并强制警告。
- 修复设置、聊天、容器、存储与人格页面的多处数据覆盖、竞态和交互问题。
- GUI 与 WebUI 的更新页支持组件化版本、Pre-release 标识和按组件重启提示。

## v0.0.1-dev — 2026-11-11

首个私有开发版。

- 从 KarenBot 主项目拆出共享后端控制面 `backend/`。
- 提供 `create_control_app()`、`RuntimeHost`、API 路由、鉴权、事件、日志环形缓冲、
  设置声明、首次配置向导和聊天显示管线。
- 设置保存支持 159/179 字段热应用；Provider / Agent 支持热重建。
- 与 `KarenBot-WebUI`、`KarenBot-GUI` 的 v0.0.1-dev 配套。
