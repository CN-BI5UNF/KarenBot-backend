# Changelog

## v2.0.1-alpha — 2026-11-22

WebUI 默认收紧到本机并新增局域网开关；更新时修复旧安装器的快捷方式图标；工具档位移到容器 Agent 模式。

### WebUI 安全默认值

- `main.py` / `webui_server.py` 默认只监听 `127.0.0.1`；新增 `webui.allow_lan` 配置，可在 WebUI 设置页「网络」开启局域网访问，修改后需重启 WebUI 服务。
- 启动命令显式传入的 `--host` 仍优先于配置；非回环监听时启动横幅打印风险提示。
- 登录 Cookie 在 HTTPS 或 `KAREN_WEBUI_SECURE_COOKIE=1` 时带 `Secure`；`/api/auth/*` 响应带 `Cache-Control: no-store`。
- 更新 WebUI/GUI 后自动修复旧安装器创建的桌面快捷方式图标（`applications-system` → `karenbot`），并把 SVG 写入 hicolor 图标目录。
- `behavior.tool_schema_profile` 的控件移到容器页 Agent 模式；字段声明保留并从设置页隐藏，保存后热重建工具注册表。

## v2.0.0-alpha.1 — 2026-11-21

修复统一发行版只能整体更新、WebUI 无法单独更新前端的问题。

- `POST /api/update/check` 新增可选 `components` 数组，支持 `backend` 和当前前端（`gui` 或 `webui`）。
- 不传 `components` 时仍默认同时更新后端与当前前端；单独选择时允许只更新一个组件，并提示两端版本可能不一致。
- 允许对当前版本重新应用所选组件（用于热修复/刷新资产）；只选前端时不会重启后端。
- `module_updater` 只下载、校验、写入所选组件，未选组件的文件保持原样；返回 `selected_components`、`updated_components`。
- 更新计划与状态中的 `updates` 仍保留全部组件信息，避免另一个前端版本显示丢失。

## v2.0.0-alpha — 2026-11-21

KarenBot 统一发行版首个 v2 alpha。后端、GUI、WebUI 版本统一为 `v2.0.0-alpha`，正式安装/更新入口为 `CN-BI5UNF/KarenBot` 主仓库。

- 三组件并入 KarenBot 主仓库的 `modules/` submodule；KarenBot Release 统一提供 `modules.json` + 三个组件资产。
- 更新器从 KarenBot 统一 Release 读取并校验资产，后端与当前前端必须一起更新。
- 从 0.1.x 升级必须先安装过渡版：后端 `v0.1.11-alpha.1`、GUI/WebUI `v0.1.1-alpha.2`。
- 组件仓库的 v2 Release 仅供源码回溯；旧 1.0.x 绑定版本不再支持。

## v0.1.11-alpha.1 — 2026-11-21

过渡版本：后端模块 `0.1.11-alpha.1`，应用代码 `1.0.11-alpha.1`。升级后更新源切换为 `CN-BI5UNF/KarenBot` 的**统一发行版**；请在本版本更新页手动选择 `v2.0.0-alpha`。

- 更新器改为读取 KarenBot 主仓库 Release：`modules.json` 描述后端/GUI/WebUI 三个组件资产，下载后逐资产校验 SHA256；任何入口都同时更新后端与当前前端，避免半套更新。
- 保留旧 GUI/WebUI 可读的组件别名；过渡期间即使前端尚未升级，也能看到并应用同一发行版。
- 新增 `distribution` 状态、`distribution_releases`/`distribution_updates` 字段；Pre-release 仍需手动选择并显式确认。
- 适配器层拆为 `adapters/transports/`（WS 客户端/服务端、HTTP API、HTTP POST Webhook）与 `adapters/onebot11/`（标准协议 + NapCat/LLOneBot/Lagrange/go-cqhttp 方言）；`adapters/napcat/` 保留为兼容薄壳。
- OneBot 11 支持正向/反向 WS、HTTP API、HTTP POST 三种接入方式；反向 WS 支持 API/Event 双连接、多账号 `X-Self-ID`、Header/Query 鉴权；HTTP POST 支持 HMAC-SHA1 验签。
- 新增结构化 `MessageSegment` IR（文本/@/引用/图片/语音/视频/文件/表情/合并转发/Raw）、数组/CQ 双向编解码、全量标准事件与方言能力表。
- 发送链路改为结构化段：reply/@/face 不再由业务层拼 CQ；旧 action 的 CQ 标记只在 `adapters/outbound.py` 边界兼容。
- 配置改为判别联合 `AdapterConfig`，旧 `type: napcat` 与 `mode: client/server` 零修改兼容；黑白名单 ID 字符串化，支持 `user_ids` 别名和 Telegram 负数群 ID。
- 工具按 `AdapterCapabilities` 动态过滤；`get_msg`/`send_poke`/表情回应/群管改用适配器语义方法。
- 新增 Telegram Bot API 适配器（长轮询/Webhook、文本分片、图片/语音/视频/文件、引用、表情回应、撤回、topic/thread_id、媒体下载、UTF-16 实体偏移、429 重试、offset 持久化）。
- 新增 `GET /api/adapters/protocols`（协议目录 + 配置 JSON Schema）、`GET/PUT /api/adapters/config`（完整配置读写/热重载）与 `GET /api/adapters/status`（连接状态/能力表）。
- 安全收紧：反向 WS/Webhook 默认只监听回环；非回环监听强制要求 token/secret；Webhook 流式限长；HTTPS 客户端日志抑制 token 泄漏；语音下载限制为公网 URL；热重载失败回滚旧适配器。
- ⚠️ 升级提示：`ws_server` 的 `host: 127.0.0.1` 现在只监听本机（不再自动改 0.0.0.0）；跨设备反向 WS 请显式设 `host: 0.0.0.0` 并配置 `access_token_id`。
- ⚠️ 升级提示：`access_token_id` / `webhook_secret_id` / 注入的 Telegram token 引用不存在时，适配器现在拒绝启动，不再静默降级为无鉴权。
- 新增 60+ 协议/发送/配置/安全回归测试；全量 269 项测试通过，ruff 通过。
- 新增通用「外部程序」托管：`programs` 配置 + `ExternalProgramManager`，可在 Runtime 启动后拉起多个自定义程序；启动失败只记 ERROR/状态提醒，不影响主流程；关闭时只停止由 Karen 成功拉起且仍存活、`stop_with_karen=True` 的进程；支持端口探测跳过、自动重启、自定义环境变量/工作目录。
- 配置版本升至 v3：`adapters.*.manage_process/process_path/process_args/auto_restart/process_warmup_seconds` 自动迁移为 `programs.napcat_*`，旧字段删除；兼容门禁允许 v2 配置先通过再迁移。
- 新增 `GET/PUT /api/settings/programs` 与单程序 start/stop 接口；`/api/status` 增加结构化 `programs` 状态，EventHub 状态指纹包含程序状态/失败序号。
- GUI/WebUI 设置页新增「外部程序」面板；失败按 `failure_seq` 去重弹窗提醒。

## v0.1.11-alpha — 2026-11-18

后端模块版本 `v0.1.11-alpha`，应用代码版本 `1.0.11-alpha`。基于 `v0.1.9-alpha`，包含其全部改动。

- 模块更新器改为完整后端的文件级叠加：只写白名单程序文件，`data/`、`personas/`、`plugins/`、`config.yaml` 等用户数据保持不动且不删除任何文件；工作树有本地改动不再拒绝更新，覆盖前逐个备份到 `module_backups/`；状态接口显示工作树未改动/已改动/未跟踪文件数。
- 更新失败（包括符号链接/Windows junction 或目录冲突）会整次回滚；符号链接路径不会被跟随；部分文件不会造成版本清单前进；无变更重装不再误报需要重启；确认令牌只能使用一次。
- 新增更新器回归测试：用户数据白名单、逐文件备份、失败回滚、符号链接、目录冲突、无变更重装与工作树统计。

## v0.1.9-alpha — 2026-11-17

后端模块版本 `v0.1.9-alpha`，应用代码版本 `1.0.9-alpha`。基于 `v0.1.3-alpha`。

- karen.db schema 升至 v2：`archive_messages` 新增 `record_hash` 并分批回填，去重索引从完整 JSON 换成 `(persona_id, record_hash)` 窄索引；迁移可中断续跑，兼容门禁允许 v1 启动后自动迁移。设备实测索引 2.47 MiB → 52 KiB，1026 行迁移约 194ms，20k 行合成库约 1.85s。
- 新增 `KarenDB.ensure_ready()`：基于版本与对象完整性的 schema 自检，替代多个 Store 每次建连重跑整套 DDL；空库、缺表、缺索引、缺迁移记录和外部切走 WAL 均可自愈。
- `KarenEventStore` 连接补齐 `WAL` / `synchronous=NORMAL` / `foreign_keys`；并发建连的 `journal_mode` 设置改为进程内串行，避免 `database is locked`。
- 对话页合并热点优化：发送去重增加廉价前缀守卫（只对 JSON `\u` 转义回退完整解析）、ISO 时间戳快路径、缓存字节 O(1) 统计、每 runtime 并发加载锁；设备实测合并 1438ms → 678ms，缓存统计 429ms → 0.02ms，合并输出与旧实现逐字节一致。
- 归档与事件改为增量读取缓存：新增 `KarenArchiveStore.records_since()` 与 `KarenEventStore.event_id_bounds()`，刷新只读取新增行并在删除/窗口滑动时自动重载；设备实测追加后刷新 715ms（全量 1520ms）。
- 对话页缓存复用会话分组结果，缓存命中时省掉约 250ms 的重复分组；设备实测命中 0.67ms。
- 新增 `POST /api/storage/vacuum` 显式 VACUUM 入口：默认低于 1 MiB 空闲页跳过，检查磁盘空间，返回前后体积与回收字节；设备实测回收 2.95MB / 907ms。
- 兼容门禁 `compatibility.json` 新增可选 `min_local_version`，`persona_db` 升到 v2 并允许 v1 自动迁移；旧版本 manifest 仍会拒绝 v2 库。
- 新增 schema 自愈、v2 迁移、半迁移续跑、并发升级、归档去重、增量缓存、分组复用、VACUUM 与兼容区间测试；本地与设备全量测试通过。

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
