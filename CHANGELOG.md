# Changelog

## v0.1.3-alpha — 2026-11-16

后端模块版本 `v0.1.3-alpha`，应用代码版本 `1.0.3-alpha`。基于 `v0.1.1-alpha.1`；界面模块仍由各自仓库独立发布。

- `group_trigger_window` 在 compact 模式改为常驻紧凑 schema，task_context 不再要求先 `tool_search`，省掉一轮无效调用。
- `reply_to_message_id` / `reviewed_until_seq` 不再从 compact schema 隐藏；`needs_review` 重发和引用回复可直接使用。
- 精简发送目标、图片识图、记忆 scope 等高频字段描述；Schema 截断改为优先按句/分号收尾；stub schema 移除 `_tool_search_required` 假参数。
- 合并 file/RAG 记忆提示词，修正 `delete_important_memory` 的关键词矛盾；裁剪 `<other_tools>` 中的重复工具说明。
- 只读工具支持连续批次并发执行；`tool_call_id` 改为独立上下文字段，消除共享 `extras` 竞态。
- 修复动态/守卫/账本/子 Agent 执行器包装层丢失并发标记的问题，确保并发优化在真实聊天路径生效。
- RAG 向量库入库/加载时预归一化，支持可选 NumPy 矩阵检索；SQLite 连接改为复用并在 shutdown 关闭。
- RAG 关闭时等待被取消但仍在执行的 SQLite 线程，避免关闭连接与在途写入竞态；空/异维向量按 0 分参与排序。
- RAG 取消时让 DB 写入与内存索引更新作为一个不可中断操作完成；`top_k` 线程结束前保持索引锁，避免孤儿线程与后台索引竞态。
- `PersonaDB` 复用 SQLite 长连接，并在关闭时释放。
- Anthropic 协议自动注入 Prompt Caching 断点（tools / system / 稳定历史末尾），上游拒绝 `cache_control` 时安全降级。
- 为全部工具补齐 `search_tags`，为复杂工具补齐 `examples`；`tool_search` 优先精确标签命中。
- 清理 `backend/chatlog_tmp/` 与 `tools/base.py` 死代码；CI 安装项目依赖并运行完整测试矩阵。

## Unreleased — 核心程序并入

- 新增 `KarenDB.ensure_ready()`：基于版本与对象完整性的 schema 自检，替代 5 个 Store 每次建连重跑整套 DDL；空库、缺表、缺索引、缺迁移记录和外部切走 WAL 均可自愈。
- `KarenEventStore` 连接补齐 `WAL` / `synchronous=NORMAL` / `foreign_keys`；并发建连的 `journal_mode` 设置改为进程内串行，避免 `database is locked`。
- 对话页合并热点优化：发送去重增加廉价前缀守卫、ISO 时间戳快路径、缓存字节 O(1) 统计、每 runtime 并发加载锁；设备实测合并 1438ms → 678ms，缓存统计 429ms → 0.02ms，合并输出与旧实现逐字节一致。
- karen.db schema 升至 v2：`archive_messages` 新增 `record_hash` 并回填，去重索引从完整 JSON 换成 `(persona_id, record_hash)` 窄索引；设备实测索引 2.47 MiB → 52 KiB，迁移耗时约 192ms（1026 行），可中断续跑。
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
