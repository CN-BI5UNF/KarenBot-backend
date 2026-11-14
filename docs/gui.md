# Karen Desktop（原生 GUI）

这是 KarenBot 的独立桌面版本，位于本仓库的 `gui/`。它不是 WebUI 的浏览器壳，
也不把 `webui/static` 放进 WebView；界面使用 **GTK4 + libadwaita** 原生控件。
GUI 通过 `gui/plugin.json` 注册为前端插件，可由 `python main.py --frontend gui`
统一启动；`python -m gui` 仍保留为直接入口。
当前 GUI 版本：`1.0.1-alpha.1-gui.6`。

## 设计

项目拆成共享后端和两个独立前端：

```text
backend/   共享 FastAPI 控制面 + RuntimeHost + 数据层
   │
   ├── webui/  浏览器前端（static + 静态挂载/uvicorn 启动）
   └── gui/    原生 GTK4 前端（本机回环控制服务 + GTK4 界面）

GTK4 界面
   │  HTTP（只绑定 127.0.0.1，随机端口 + 随机 Bearer token）
   ▼
backend.app.create_control_app()
   │
   ▼
RuntimeHost / core.Runtime 与现有 KarenBot 数据层
```

- GUI 进程自己启动一个**只监听回环地址**的控制 API，端口随机。
- WebUI 与 GUI 共用 `backend/`，聊天、角色、记忆、设置、容器、存储和日志等行为
  只有一套后端实现，不会出现“WebUI 有、GUI 没有”的分叉。
- `backend/` 不导入 `webui/` 或 `gui/`，便于后续一键安装器按前端分别安装。
- API 不对外监听；GUI 使用每次进程随机生成的 Bearer 令牌，不会启动浏览器。
- 控制 API 的静态 WebUI 路由没有挂载，GUI 只读取 JSON API。
- 使用不同 `--data-dir` 的 GUI 实例会得到不同的 GApplication ID，可以并存；
  同一个数据目录仍然只允许一个 GUI/Runtime 实例。
- 开发检出把数据放在仓库内时，更新页会显示警告；要验证更新功能，请用
  `--data-dir` 指向仓库外目录，或使用打包安装版。

## 运行要求

- Linux（主要目标；当前在 Armbian / Allwinner H618 上开发和验证）
- Python 3.10+（建议与项目一致的 3.12）
- 系统提供 GTK4、libadwaita 和 PyGObject 绑定。Debian/Ubuntu 系的包名是
  `python3-gi`、`gir1.2-gtk-4.0`、`gir1.2-adw-1`，请用系统的包管理器安装。
- 其余 Python 依赖与主项目相同。

## 安装与运行

在本目录（`~/tf/KarenBot-GUI`）：

```bash
uv venv --python 3.12 --system-site-packages .venv
uv pip install -e ".[dev]"
./run-gui.sh
```

`--system-site-packages` 很重要：PyGObject 是由系统包提供的，普通 venv
看不到 `gi.repository`。

也可以直接运行：

```bash
.venv/bin/python -m gui
# 或使用安装后的入口
karen-gui
```

安装到桌面应用菜单：

```bash
./scripts/install-desktop.sh
```

`--version` 查看 GUI 版本，`--data-dir` 指定数据目录，`--theme` 控制外观：

```bash
./run-gui.sh --data-dir ~/.local/share/KarenBot
./run-gui.sh --theme dark      # 强制深色
./run-gui.sh --theme light     # 强制浅色
```

默认的 `--theme auto` 跟随系统/桌面环境；检测不到系统偏好时使用浅色。

### 数据目录

开发检出（目录里存在 `.git`）默认使用仓库下的 `data/`，与系统上正在运行的
KarenBot 服务完全隔离；这样可以在不影响现有机器人数据的情况下开发、试用 GUI。
打包安装版（发布归档不含 `.git`）默认使用平台用户数据目录（Linux 为
`~/.local/share/KarenBot/`），因此数据在更新器管理范围之外，应用内更新不会被
`data_dir_inside_project` 拒绝。可用 `KAREN_GUI_DATA_DIR` 或 `--data-dir`
覆盖。

如果要管理已有实例：

1. 先停止现有 `karenbot.service`（SQLite 与 NapCat 连接不能同时被两个进程持有）。
2. 备份数据目录。
3. 用 `--data-dir` 指向该实例的数据根，例如
   `--data-dir ~/.local/share/KarenBot`。`AppPaths` 会自动解析
   `instances/default`；同一个数据目录只能被一个 Runtime 使用。

首次使用一个空数据目录时，GUI 会走与 WebUI 相同的分步向导：推荐/自定义路径、
主模型、子 Agent（自定义路径）、可选功能、记忆方式、NapCat 渠道、人格与汇总。
步骤流转由后端 `backend/wizard/flow.py` 决定，原生只渲染当前步骤；支持上一步、
跳过可选步骤和进度条。写入配置后，GUI 会为这个全新的数据根补齐兼容状态，使
Runtime 可以正常初始化数据库。已有数据没有兼容状态时不会被擅自伪造；遇到
放行失败请先按项目迁移文档处理。

## 页面

侧栏不再按分组加标题，而是复刻 WebUI 的顺序与名称：

`总览 · 对话 · 记忆 · 人格后台 · 日志 · 人格 · 黑话 · 知识 · 模型管理 ·
表情包 · 容器 · 存储与数据 · 黑白名单 · 群聊触发 · 设置`

详细到每个控件、设置字段和剩余差异的清单见
[`docs/gui_parity_checklist.md`](gui_parity_checklist.md)。每完成一项就在那里打勾。

功能覆盖：

- 外壳：暖米色/深色 WebUI 调色板、200px 可折叠侧栏、56px 顶栏、1100px 内容栏、
  渠道徽标/当前人格/重启 Runtime/主题循环/全屏、侧栏底部连接状态
- 总览：主模型状态、渠道状态、模型健康表、V2 用量统计与四个时间范围、
  更新提示、重启 Runtime；2 秒刷新，更新检查独立 60 秒
- 对话：会话列表、会话搜索、结构化显示记录、聊天/系统/工具/思考/只看媒体过滤、
  5 秒实时刷新、DisplayItem 持久化索引分页（加载更早/显示全部，只查增量）、
  记录总数、底部常驻试跑输入框、
  工具调用/返回独立折叠、四个“默认展开”开关、注入测试消息、
  `[表情包: 名字]` 内联缩略图、图片附件缩略图/大图预览、
  文件附件用默认应用打开或在文件夹中显示；已被存储清理的附件显示清理原因，
  并提示可在 WebUI 中尝试从 QQ 原链接恢复
- 日志：等级/模块/关键字过滤、自动刷新、暂停滚动、重新加载、本地 2000 行
  环形缓冲、按序号增量追加、筛选请求串行排队
- 人格：切换、AI 按简述生成人格、AI 反复改写、直接编辑提示词、只读查看人设源码、
  清理人格运行数据（预览/确认短语/可选备份/后台任务）、导出 zip、
  角色形象画廊（多图上传/预览图标记/删除全部）、导入/复制/删除
- 人格后台：3 秒实时状态、10 个横向标签页按需加载、动态表格、有效年龄与生理状态、
  管理员唤醒模态框
- 记忆：重要记忆增删改置顶、RAG 索引浏览
- 知识：实时搜索、增删改、共享、置信度、来源、只读共享条目保护、联网考究、
  一键刷新当前热梗
- 黑话：群聊选择器、实时搜索、按群/状态筛选、增删改、确认状态、来源/证据/
  自动拒绝原因、从缓存学习
- 表情包：缩略图、实时筛选、批量上传并为每个文件改名、上传结果/跳过/重名提示
- 容器：WebUI 风格状态卡与 Agent 模式表、C 存储初始化/重新挂载/采用实际大小、
  Agent 安装/激活/重装/卸载（操作期间统一禁用按钮）、host 执行策略、
  重启 Runtime 确认、测试失败输出尾部诊断、两个工作区只读文件管理（面包屑、
  双模式切换、表格、15 秒缓存、文本与图片预览）
- 模型管理：完整提供商表单（预设/协议/厂商/思考风格/超时/模型）、未保存配置
  测试连接、拉取模型列表、Agent 绑定与动态思考强度/首包超时、本地模型扫描/
  安装指引/依赖安装/上传；分区读取，单项失败不拖垮整页
- 存储与数据：磁盘/缓存盘点、安全清理预览与执行、任务结果与轮询、上下文压缩、
  人格运行数据清理与本地卸载的影响预览/确认短语/可选备份/任务审计
- 版本与更新：版本状态、远端版本与更新日志、自动加载 Release、镜像测速与
  一键使用、兼容性门禁、更新/回退/切换确认、依赖变更警告、目标版本提交列表、
  更新提醒开关，以及 GUI 本地后端重启
- 设置：按 `backend/settings_spec.py` 声明式生成全部 179 个字段；视觉与联网搜索
  专用面板、备份任务/校验/下载、Token 预算快照、路径诊断、密钥引用修复、
  运行时操作；可从容器页等位置深链到对应设置分组
- 首次配置：分步向导、路径选择、主模型（推荐/自定义）、子 Agent、可选功能、
  Embedding、NapCat、人格与汇总，支持跳过、返回与服务端进度

## 设置热应用

设置保存后不再一律要求重启 Runtime：

- 179 个字段中 **159 个保存即热应用**，只有 20 个结构性字段仍要求重启
  （容器存储模式/大小/固定镜像路径、长期记忆与 Embedding 模式/模型、人格后台启停等）。
- 配置改动在深拷贝上校验，通过后**递归合并回运行中的配置对象**，因此
  Pipeline、记忆、主动思考、限流器、搜索、视觉、天气、TTS、容器服务和工具注册表
  持有的是同一份新配置，无需重构 Runtime。
- 渠道地址/开关会重建适配器并重连；提供商与 Agent 修改会热重建 Provider/Agent，
  失败才回退为“需要重启”；工具 schema 档位会热重建工具注册表。
- 视觉/搜索/天气/TTS 的启用状态会重建对应服务和 Pipeline 引用。
- 容器普通资源限制会更新服务配置，已有容器的内存/CPU 等限制在下一次重建后完全生效。
- 结构性字段保存后会标记“待重启”：后续修改不会半热应用，避免运行中的组件看到
  一半新、一半旧的配置；重启 Runtime 后统一生效。
- WebUI 与 GUI 使用同一个后端返回值，保存提示会显示“已保存并立即生效”、
  “需要重启 Runtime”或“已有待重启的配置”。

## 数据兼容与维护门禁

- GUI 默认使用仓库内的隔离数据目录；也可用 `--data-dir` 指向已有实例。启动时
  Runtime 会先做维护/数据兼容检查。
- `maintenance.sqlite3` 只保存清理计划、任务和资源盘点等派生运维元数据。已有实例
  如果缺少该文件，GUI 会在兼容检查前按当前 schema 安全补建，避免永久停在上
  “maintenance 需要从 v0 迁移到 v1”；真正空实例仍走首次初始化流程。
- 其他版本的数据库迁移仍由维护流程负责；兼容门禁失败时设置页会显示具体原因和
  重试/路径诊断入口，而不是展示空设置。

## 已知边界

- 更新页会在用户确认后操作当前代码仓库，并提供兼容、工作树、依赖与目标提交检查；
  工作树有未提交改动时后端会拒绝。更新完成后的“重启后端”会重启 GUI 自己的
  进程内控制后端并重新加载 Python 代码；如果目标版本也修改了 `gui/` 界面代码，
  需要重新打开桌面应用。更新前请先提交或备份本地开发改动。
- 危险数据操作已接入 WebUI 的服务端预览 → 确认短语 → 后台任务契约；可选备份
  默认关闭。卸载会退出本地控制服务，删除由后端独立助手继续。验证覆盖预览、
  对话框和任务列表，**没有在测试中实际执行删除或卸载**。
- 首次配置向导覆盖日常字段；厂商细分、思考风格、官方搜索通道等高级项仍在
  「设置 / 模型管理」中调整。向导页较长时需滚动，尚未做固定底栏。
- 当前主要面向 Linux/X11；GTK4 在其他平台需要自行准备 GTK 运行库。
- 外观默认跟随系统；顶栏主题按钮单击一次循环“跟随系统 → 浅色 → 深色”，
  与 WebUI 的主题切换习惯一致。选择保存在 `~/.config/karen-gui/theme`，也可用
  `--theme` 覆盖。侧栏折叠状态单独保存在 `~/.config/karen-gui/sidebar`。
- 冷启动需要加载 KarenBot 的完整 Runtime 与提供方 SDK，ARM 设备上约 20–30 秒。
  这是进程导入与 Runtime 初始化的成本，不是每个页面请求的延迟。
- 控制 API 只监听回环地址，并使用每个 GUI 进程随机生成的 Bearer 令牌；
  不要把监听地址改成 `0.0.0.0` 对外提供。令牌不会显示在界面上，也不写入磁盘。

## 开发与验证

- 纯客户端/API 测试：`pytest -q tests/test_gui_api.py`
- 有 X11 或 Xvfb 时可运行 GUI 冒烟脚本逐页加载并截图：

  ```bash
  DISPLAY=:1.0 .venv/bin/python scripts/gui_smoke.py
  DISPLAY=:1.0 .venv/bin/python scripts/gui_smoke.py --screenshot-dir /tmp/karen-shots
  ```

- GUI 代码风格与主项目一致：`ruff check gui tests/test_gui_api.py scripts/gui_smoke.py`。


## 渲染回退、字体与主题

- 启动入口现在是 `gui.launcher`：在导入 `gi` 之前设置渲染器环境变量。默认先走
  GTK 自动选择；子进程提前崩溃时自动回退 `GSK_RENDERER=cairo` +
  `LIBGL_ALWAYS_SOFTWARE=1`，必要时再试 `GDK_BACKEND=x11/win32`。
  可用 `KAREN_GUI_RENDERER=auto|gl|cairo` 覆盖，日志在
  `DATA_DIR/logs/gui-launcher.log`。
- 字体使用回退栈：Noto Sans CJK SC / Noto Sans SC / Microsoft YaHei UI / Segoe UI；
  衬线和等宽也有对应回退。安装器可把 Noto 字体下载到 `DATA_DIR/fonts` 并生成
  `fonts.conf`，启动器通过 `FONTCONFIG_FILE` 让 GTK 看到它们。
- 设置页新增“外观主题”：内置 Karen 暖色和系统 Adwaita。用户也可以把 GTK4 主题
  放到 `~/.local/share/themes/<name>/gtk-4.0/gtk.css`，在设置页刷新后选择；
  外部主题会叠加在 Karen 样式之上，可能与自带样式冲突。

## 多轮迭代建议

WebUI 控制台的主要页面、设置字段、热应用、分步向导和危险操作预览已完成；
剩余差异以 [`docs/gui_parity_checklist.md`](gui_parity_checklist.md) 为准：

1. 窄窗口下的响应式表格/表单与向导固定底栏。
2. 角色形象拖拽上传。
3. Windows/macOS 的 GTK 打包、实机回归与桌面集成。
4. 更新/回退已接入，但依赖真实干净工作树与远端 Release 做端到端演练；当前本地
   开发树通常有未提交改动，因此只验证了检查、确认、阻止与 GUI 后端重启流程。

## 分模块更新（backend + GUI）

GUI 的「设置 → 更新」现在按**独立模块仓库**工作，而不是更新一个合并源码仓库：

| 组件 | 仓库 |
|---|---|
| 后端 | `https://github.com/CN-BI5UNF/KarenBot-backend` |
| GUI 前端 | `https://github.com/CN-BI5UNF/KarenBot-GUI` |

更新只替换应用根目录下的 `backend/` 与 `gui/`，不会改动 `core/` 等宿主运行时，也不会覆盖用户数据。更新前会把当前模块备份到数据目录的 `module_backups/<时间戳>/`。

### Latest 与 Pre-release

- GitHub Release 勾选 **Latest**（非 Pre-release）时，自动检查和总览徽标会提示更新。
- 勾选 **Pre-release** 的版本**不会自动提示**，但会出现在「远端版本」列表中；用户可以手动选择更新，确认框会明确提示“极不稳定”，并要求显式确认。
- Pre-release 是每个组件独立判断的：后端有 Pre-release、GUI 没有时，只会提示后端的 Pre-release。
- 后端单独发布修复版、GUI 未变化时，只显示后端更新；反之亦然。

### 私有仓库凭据

三个模块仓库当前是私有仓库，更新检查需要 GitHub Releases API 权限。任选一种方式：

1. `gh auth login` 登录有 `repo` 权限的账号；
2. 设置 `KAREN_GITHUB_TOKEN`（或 `GH_TOKEN` / `GITHUB_TOKEN`）；
3. 把 token 写入 `~/.local/share/KarenBot/instances/default/github_token.txt`（权限 0600），GUI 不会把 token 展示在界面或日志里。

目标 token 至少需要读取这些仓库的 Releases/源码（classic PAT 的 `repo` scope，或 fine-grained token 的 Contents read + Metadata read）。

### 应用更新后的重启规则

- 仅后端更新：刷新/重启 GUI 本地后端即可加载新的 Python 代码。
- GUI 前端更新：必须完全退出并重新打开 GUI；仅重启后端不会替换已经加载的界面代码。
- 当前是带未提交改动的开发检出时，更新会被拒绝。测试时可用
  `KAREN_ALLOW_MODULE_UPDATE_DIRTY=1` 显式允许覆盖（危险，仅用于测试）。
