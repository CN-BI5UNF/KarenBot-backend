# Karen Desktop ↔ WebUI 控制台对齐清单

- 更新日期：2026-11-13
- 对齐基准：`v1.0.1-alpha.1` 的 WebUI（`webui/static/index.html`、`app.css`、`js/pages/*`、`providers.js`、`releases.js`、`destructive_admin.js`、`container_setup.js`、`settings_spec.py`）
- 原生实现：GTK4 + libadwaita，目录 `~/tf/KarenBot-GUI`
- 用途：把 WebUI 控制台的全部页面、控件、操作习惯和设置字段集中记录；每完成一项，把 `[ ]` 改成 `[x]`。暂时无法完全一致或有意保留差异的项用 `[~]`，并写清原因。
- 状态图例：
  - `[x]` 已完成并对齐
  - `[~]` 已实现但仍有交互/布局/性能差异
  - `[ ]` 尚未实现
  - “原生有意差异”表示平台原因不照搬浏览器行为，但保留同等能力。

## 验证方式

```bash
cd ~/tf/KarenBot-GUI
.venv/bin/python -m py_compile gui/app.py gui/widgets.py gui/pages/*.py
KAREN_DISABLE_TREND_RESEARCH=1 KAREN_DISABLE_DEPENDENCY_CHECK=1 \
  .venv/bin/python -m pytest -q --ignore=tests/test_kv_cache_real.py
DISPLAY=:1.0 .venv/bin/python scripts/gui_smoke.py \
  --data-dir /tmp/karen-gui-hotdata --theme light
DISPLAY=:1.0 .venv/bin/python scripts/gui_smoke.py \
  --data-dir /tmp/karen-gui-hotdata --theme dark
```

最近一次（2026-11-12）：独立 Xvfb 下浅色与深色 15 页 smoke 均为 `0 errors`；全量测试 **2831 passed in 446.82s**（`KAREN_DISABLE_TREND_RESEARCH=1`、`KAREN_DISABLE_DEPENDENCY_CHECK=1`，忽略真实 KV cache 压测）。额外 Xvfb 探针覆盖全部向导步骤、模型提供商/Agent 对话框、更新页渲染、存储分类选择保持、黑白名单实时状态，以及 **GUI 本地后端停止→重启→页面重建→状态恢复**。Ruff、`compileall`、`git diff --check` 通过。


## 2026-11-13 复核与修复记录

本轮以当前 WebUI 源码重新逐页审计，不再沿用“清单打勾即完成”的假设。已修复：

- 全新数据目录、黑白名单/群聊触发读取失败时，禁止用空表单覆盖服务端配置；
- 首次配置门禁无法被 Ctrl+数字/深链绕过；步骤区内部滚动、底部按钮固定；
- 人格后台唤醒后轮询不再永久停止，标签页失败不再每 3 秒自动重试；
- 思考强度按“模型 → 预设 → 兜底”解析，打开 Agent 对话框不会再静默清空已有档位；
- 容器文件浏览缓存竞态、重建/本机策略防重入、C 存储尺寸不一致优先级；
- 存储分类介质/数量、上下文预算结构化展示、清理轮询失败重试、压缩结果判断；
- 人格/形象/表情包模态父窗口、上传逐张进度、重复提交防重入、大图预览；
- 表情包纹理缓存与拖放上传；知识共享开关单飞、来源渠道、归属兜底；
- 设置备份/预算面板未加载时禁用动作，备份文件不再只显示前 20 个；
- 更新页 timeout 提升到 330 秒，依赖变更说明与审计路径回退；
- 打包版默认数据目录移到用户数据目录，避免更新器 `data_dir_inside_project` 永久拒绝。

仍需实机确认或保留的差异：对话/日志使用轮询而非 WebSocket；聊天正文中的表情包
仍是块状而非真正的文字流内联；Windows/macOS GTK 运行库与安装器打包尚未实机验证。

---

# 1. 全局外壳与通用交互

## 1.1 布局

- [x] 暖米色浅色主题：背景 `#F8F5EE`、卡片 `#FCFAF4`、悬停 `#F0E8D6`、边框 `#E8E1CE`
- [x] 深色主题：背景 `#1A1817`、卡片 `#232120`、悬停 `#2E2B28`、边框 `#3A3633`
- [x] 青瓷青主色 `#6FA39A`，成功苔绿、警告落霞黄、错误朱砂红
- [x] 200px 侧栏；折叠后 64px
- [x] 56px 顶栏
- [x] 内容区最大宽度 1100px
- [x] 内容区 32px 内边距（窄窗口由 `Adw.Clamp` 自动收紧）
- [x] 卡片 8px 圆角、20px 内边距、1px 边框
- [x] 页面标题思源宋体 20px/500；卡片标题黑体 16px/600
- [x] 表格表头 12px 次要色、行分隔线、行悬停
- [x] 实心状态徽标（成功/警告/错误）；未连接为描边徽标
- [x] 主按钮青瓷青、危险按钮朱砂红、次按钮卡片底
- [x] 输入框 4px 圆角、青瓷青焦点环
- [x] 细滚动条
- [x] 页面切换淡入（160ms）
- [x] 设置页左侧分组导航可拖动分隔条调整占比，默认 156px（不再固定 184px）
- [x] 主侧栏固定 200px，内容页最小宽度变化不会再撑宽侧栏
- [x] 全屏按钮使用 GTK4 `fullscreen()/unfullscreen()`，图标随状态切换；支持 F11
- [x] 不同 `--data-dir` 的 GUI 使用不同 GApplication ID，可与默认实例并存
- [x] 后端拆分为独立 `backend/` 包；`webui/` 只保留静态前端与静态挂载启动器
- [x] `gui/` 只依赖 `backend/`，不导入 `webui/`
- [x] `backend/` 不导入 `webui/` 或 `gui/`
- [x] WebUI 与 GUI 共用 `backend.app.create_control_app()`，API/Runtime 生命周期/鉴权/更新检查同一实现

## 1.2 侧栏

- [x] 品牌名 `KarenBot`，青瓷青
- [x] 品牌行内折叠按钮
- [x] 折叠状态持久化到 `~/.config/karen-gui/sidebar`
- [x] 折叠后只保留图标，分类文字隐藏
- [x] 无分组标题，导航顺序和名称与 WebUI 一致：
  - [x] 总览
  - [x] 对话
  - [x] 记忆
  - [x] 人格后台
  - [x] 日志
  - [x] 人格
  - [x] 黑话
  - [x] 知识
  - [x] 模型管理
  - [x] 表情包
  - [x] 容器
  - [x] 存储与数据
  - [x] 黑白名单
  - [x] 群聊触发
  - [x] 设置
- [x] 导航项 40px 高、选中态浅青填充 + 左侧 3px 竖条
- [x] 侧栏底部连接状态：运行中/未运行/启动失败 + 运行时长
- [x] 版本标记（原生放在侧栏底部；WebUI 在右下角，位置有意不同）
- [~] WebUI 折叠按钮位于品牌行右侧；原生相同，但图标使用系统 `sidebar-show-symbolic`

## 1.3 顶栏

- [x] 左侧渠道徽标：已就位/未连接/未配置渠道/未运行/启动失败
- [x] 当前人格标签
- [x] 重启 Runtime 按钮（带确认）
- [x] 主题按钮：单击循环“跟随系统 → 浅色 → 深色”
- [x] 主题按钮图标随当前模式变化
- [x] 主题偏好持久化到 `~/.config/karen-gui/theme`
- [x] 全屏按钮（原生替代 WebUI 的“退出登录”，因为本地后端无登录页）
- [x] 不显示居中窗口标题
- [x] 原生窗口按钮保留
- [x] 顶栏与内容区同底色，仅下边框分隔
- [~] WebUI 主题切换会同步 `app.theme` 配置；原生只改本地外观，避免误改服务端配置。能力不受影响

## 1.4 通用交互

- [x] 后台 HTTP 请求线程池，不阻塞 GTK 主循环
- [x] 回调经队列 + 25ms GLib 定时器回主线程
- [x] Toast 提示
- [x] 确认对话框（危险操作）
- [x] 关闭窗口时停止本地后端
- [x] F5 / Ctrl+R 刷新当前页
- [x] Ctrl+Q 退出
- [x] Ctrl+1…9 快速切换前九个页面
- [~] 浏览器 WebSocket 实时事件 → 原生使用定向轮询（总览 2s、人格后台 3s、对话 5s），避免为单一本地客户端维护 WS。运行效率优先，事件延迟略有增加
- [x] 未保存修改提醒：黑白名单、群聊触发在离开页面或关闭窗口时弹出“取消 / 放弃修改”确认；设置页字段是逐项自动保存，无需离开提醒
- [x] 本地后端只监听 127.0.0.1，随机端口与随机 Bearer token
- [x] 原生有意差异：无 WebUI 登录遮罩/访问令牌输入
- [x] 原生有意差异：无浏览器 URL hash；设置深链通过原生 `show_page(..., section=...)` 实现

---

# 2. 总览

- [x] 页面标题“总览”，说明“运行状态、模型健康与用量。数据每 2 秒自动刷新。”
- [x] 每 2 秒自动刷新
- [x] 页面标题右侧“发现新版本”按钮（有更新时出现，点击进入版本与更新）
- [x] 页面标题右侧“重启 Runtime”按钮
- [x] 更新检查每 60 秒一次，独立于 2 秒总览刷新，避免放大网络开销
- [x] 离开页面后两个定时器自动停止
- [x] “主模型状态”卡片：状态徽标（空闲/思考中/调用工具中/出错了）
- [x] “主模型状态”卡片：Agent · 模型
- [x] “主模型状态”卡片：活动文字
- [x] “渠道状态”卡片：已就位/未连接/未运行/启动失败
- [x] “渠道状态”卡片：渠道名 · 模式 · 地址:端口(/路径)
- [x] “渠道状态”卡片：运行时长
- [x] “模型健康”卡片：表头 Provider / 状态 / 延迟 / 说明
- [x] 模型健康状态映射（正常/较慢/出错了/未检测/未配置）
- [x] 模型健康空状态“还没有配置任何模型提供商”
- [x] “用量统计”卡片：今天 / 7 天 / 30 天 / 全部
- [x] 用量区间按钮保持选中态
- [x] 12 项统计：全历史请求数、总 token、V2 命中率、输出 token
- [x] 12 项统计：V2 输入、未命中输入、命中缓存、写入缓存
- [x] 12 项统计：推理 token、V2 估算误差、估算样本、估算输入
- [x] V2 样本提示；无新口径数据时使用旧口径说明
- [x] 旧口径记录提示（数量、输入、总 token）
- [x] 未知未来口径记录提示
- [x] 缓存字段异常提示
- [x] “按用途（V2 口径）”表：用途 / 请求 / V2 输入 token / V2 总 token
- [x] 用量不可用空状态
- [x] 读取失败时“不可用”状态，不把整个页面变空
- [x] 用量字段为 `None` 时显示“—”，不误显示为 0
- [x] 切换今天/7 天/30 天/全部时丢弃旧区间响应并重新拉取
- [x] 页面激活时重新读取更新徽标，提醒抑制后不残留旧徽标
- [~] WebUI 的绿色圆形更新浮标 → 原生使用带图标和文字的“发现新版本”按钮，可读性更好
- [~] WebUI 由状态事件触发即时刷新；原生仍以 2 秒轮询兜底

---

# 3. 对话

- [x] 页面标题“对话”与说明
- [x] 会话搜索框
- [~] WebUI 会话搜索 200ms 防抖；原生使用 `Gtk.SearchEntry` 原生延迟
- [x] 会话列表：名称、记录数、预览、选中高亮
- [x] 首次进入自动选择第一个非 `system:` 会话
- [x] 记录搜索框
- [x] 聊天 / 系统 / 工具 / 思考 过滤开关
- [x] 只看图片/文件过滤
- [x] 实时刷新开关
- [x] 记录窗口 300 条
- [x] “加载更早”按 300 条递增
- [x] “显示全部”
- [x] 还有更早记录提示
- [x] 保留滚动位置；在底部时自动跟随
- [x] 空会话、空过滤结果、读取失败状态
- [x] 工具结果过长截断并提示
- [x] 注入测试消息：消息正文 / 发送者 QQ / 昵称 / 私聊或群聊 / 群号
- [~] WebUI 搜索框位于消息流标题下；原生搜索框在页面工具栏
- [x] 独立“默认展开：思考/系统/工具调用/工具返回”开关
- [x] 每条思考/系统/工具记录可点击折叠；单条手动状态优先于默认开关
- [x] 底部常驻试跑输入框（回车发送），并保留高级群聊注入对话框
- [~] WebUI 记录流实时追加；原生 5 秒轮询
- [x] 记录面板标题显示“已显示 / 总数”
- [x] 工具调用与工具结果成对嵌套展示；工具调用、每个工具返回可独立折叠
- [x] 聊天正文中的 `[表情包: 名字]` 渲染为内联缩略图；名称索引缓存，缺失时保留原标签
- [x] 注入测试消息、手动刷新和筛选切换都会刷新右侧记录，不被“已加载”状态挡住

---

# 4. 记忆

- [x] 页面标题“记忆”与说明
- [x] 显示当前长期记忆模式徽标
- [x] “重要记忆 / RAG 历史索引”两个标签页
- [x] 切换标签页时按需加载对应数据
- [x] 新增记忆卡常驻两个标签页
- [x] 多行输入
- [x] “记下来”保存；重复内容由后端去重
- [x] 置顶复选框，立即保存
- [x] 删除，带确认
- [x] 范围、时间、内容展示
- [x] RAG 未启用提示
- [x] RAG 条目数、向量服务就绪状态
- [x] RAG 条目内容、会话 ID、时间
- [x] RAG 空状态
- [~] WebUI 使用表格（内容/范围/时间/置顶/操作）；原生使用卡片行
- [~] WebUI 的“刷新”是独立按钮；原生使用页面标题栏刷新图标
- [x] 原生补充：新增框清空逻辑与失败提示

---

# 5. 人格后台

- [x] 页面标题“人格后台”与说明
- [x] `唤醒`按钮，仅睡眠/昏睡可用
- [x] 唤醒留言（可选）与本地回复说明
- [x] 唤醒进行中防重入
- [x] 每 3 秒刷新状态卡
- [x] 当前心情、社交需求、当前动作、年龄
- [x] 能量/饱食度按生理机制显示
- [x] 上次心跳
- [x] 最近一次内心独白
- [x] 人格后台未启用提示
- [x] 短期影响、线索、待办、用户画像、内心独白、整理轨迹、状态日志、睡眠记录、进食记录、人格弧光
- [x] 10 类轨迹数据按需加载
- [x] 只读展示
- [x] 10 个横向标签页（放在横向滚动容器中，窄窗口可滚动）
- [x] 每个标签页按需加载，动态表格最多 6 列
- [x] 唤醒使用模态对话框（留言 + 本地回复）
- [x] 标签页不可用/读取失败不标记为已加载，显示原因并可重试
- [x] 表头跨全部记录取并集（最多 6 列）；缺少动作时显示“空闲”
- [~] 原生额外显示生效年龄、上次 tick、生理机制门槛等信息
- [~] 轨迹字段中文表头：WebUI 本身也显示英文键名，原生保持一致，暂不翻译字段名

---

# 6. 日志

- [x] 页面标题“日志”与说明
- [x] 等级过滤，默认 INFO
- [x] 模块前缀过滤
- [x] 正文/模块关键字过滤
- [x] 自动刷新开关
- [x] 清屏（只清当前视图）
- [x] 显示当前行数
- [x] 状态色：DEBUG/INFO/WARNING/ERROR
- [x] 异常堆栈换行显示
- [x] 读取失败提示
- [x] 自动刷新时保持滚动位置；已在底部则跟随
- [x] “自动刷新”与“暂停滚动”开关；暂停时停止自动追加，手动刷新/筛选仍可加载
- [x] “重新加载”按钮和标题栏刷新都会丢弃清屏/本地视图并重新拉取历史
- [x] 筛选请求在读取中排队，切换等级/模块/关键字不会丢失
- [~] WebUI 为 WebSocket 追加；原生 2 秒轮询并按 seq 增量追加，延迟略高但本地开销低
- [x] 日志缓冲上限 2000 行的本地环形缓冲；按 seq 增量合并，清屏只清当前视图

---

# 7. 人格

## 7.1 列表与头部

- [x] 页面标题“人格”与说明
- [x] 新建、直接写提示词、导入 zip
- [x] 列表缩略图与形象数量徽标
- [x] 当前使用、内置状态
- [x] 设为当前（非当前人格）
- [x] 切换后需要重启时提供确认
- [x] 导出 zip
- [x] AI 改写
- [x] 直接编辑
- [x] 复制
- [x] 删除人格定义（非内置、非当前）
- [x] 导入 zip
- [~] WebUI 人格列表是表格（人格/说明/状态/操作）；原生是卡片列表
- [~] 删除按钮位于“更多”菜单，WebUI 直接放在行内
- [x] “查看人设”只读源码窗口（900×760，可复制源码）
- [x] “清理运行数据”入口：预览影响、确认短语、可选备份、后台任务进度

## 7.2 AI 新建人格

- [x] 人格目录名 / 显示名
- [x] 8 段简短描述
- [x] 生成、再生成、采用并保存
- [x] 生成中状态
- [x] 名称校验
- [x] 重名覆盖确认与自动备份
- [x] 预览生成结果
- [x] 从首次配置带入人格描述
- [x] “不用 AI，直接写提示词”快捷入口
- [~] 原生预览解析为结构化段落，而不是浏览器里的原始 XML 折叠
- [x] 关闭对话框不保存草稿

## 7.3 AI 改写

- [x] 输入修改方向
- [x] 多轮修改稿
- [x] 修改历史
- [x] 采用并覆盖前确认
- [x] 覆盖前自动备份
- [~] WebUI 每次生成后清空反馈框并累计历史；原生行为相近，但预览排版不同

## 7.4 直接写提示词 / 直接编辑

- [x] 目录名、显示名、性别、年龄
- [x] 管理员 QQ / 管理员称呼
- [x] 20000 字符上限与计数
- [x] 编辑时目录名只读
- [x] 保存前备份
- [x] 重名覆盖确认
- [x] 空提示词校验
- [x] 直接编辑器展示一个管理员；保存时按原配置保留其余管理员，避免多管理员人设被覆盖

## 7.5 角色形象

- [x] 多图上传
- [x] 图片格式与大小限制
- [x] 上传进度/失败提示
- [x] 图库缩略图
- [x] 当前预览图标记
- [x] 设为预览图
- [x] 删除单张
- [x] 删除全部
- [x] 上传后刷新图库而不关闭窗口
- [~] WebUI 支持拖放；原生使用文件选择器（桌面平台更可靠）
- [~] WebUI 图库选中态和预览区更大；原生使用平铺卡片

---

# 8. 群聊黑话

- [x] 页面标题“群聊黑话”、说明
- [x] “学习设置”入口（跳到设置 → 功能）
- [x] 群聊会话输入
- [x] 已知群聊下拉选择
- [x] 搜索词条/释义/用法/示例/风险
- [x] 状态过滤（全部/待确认/已确认/已拒绝）
- [x] “从当前缓存学习”
- [x] 统计：总数、匹配数、共享、待确认、已确认、已拒绝
- [x] 当前群缓存条数与自动提取阈值提示
- [x] 只读徽标（读取失败时）
- [x] 手动添加：词条、含义、使用场景、示例、风险提示
- [x] 手动词条默认已确认
- [x] 词条列表：词条、含义、群聊、来源人格、次数、状态、方式、操作
- [x] 编辑词条
- [x] 保存、保存并确认、拒绝、删除
- [x] 确认前必须有释义
- [x] 重复词条自动拒绝原因展示
- [x] 依据/证据查看
- [x] 空状态、读取失败提示
- [~] WebUI 会话/名单使用 datalist 和 chip；原生使用输入框 + 下拉，未做 chip 交互
- [~] WebUI 支持从下拉直接定位群；原生有“全部群聊”下拉，但缺少完整的已有会话 datalist
- [~] WebUI 统计使用多个徽标；原生使用文本摘要
- [~] WebUI 列表是表格；原生是卡片
- [~] 窄屏下表格式：多数表格已用弹性列，极窄窗口仍需手动横向滚动；完整响应式布局待后续

---

# 9. 知识 / 热梗

- [x] 页面标题“知识 / 热梗”、说明
- [x] “考究设置”入口（跳到设置 → 功能）
- [x] “手动添加”按钮
- [x] “获取当前热梗”按钮（带费用确认）
- [x] 搜索标题/内容/标签
- [x] 类型过滤（全部/梗/资料/笔记）
- [x] 状态过滤（全部/启用/归档）
- [x] 统计：总数、梗、资料、笔记、共享
- [x] 查询上限/字符上限、后台考究间隔提示
- [x] 上次热梗考究时间
- [x] 只读徽标
- [x] 联网考究卡：主题输入、整理类型、执行考究
- [x] 考究进行中状态与结果/跳过数量
- [x] 笔记列表：类型、标题/内容、标签、来源、共享、更新时间、操作
- [x] 共享开关（立即保存）
- [x] 非本人条目只读
- [x] 编辑、删除
- [x] 添加/编辑：类型、置信度、标题、状态、共享、内容、使用语境、标签、来源 URL
- [x] 标题/内容必填校验
- [x] 空状态、读取失败提示
- [~] WebUI 是表格；原生为卡片列表
- [x] 共享开关使用原生 GTK Switch，保存失败回滚选中态
- [~] WebUI 搜索即时过滤；原生 300ms 防抖后服务端搜索
- [x] 原生修复：创建后补写置信度

---

# 10. 模型管理

- [x] 页面标题“模型管理”与说明
- [x] 重新扫描本地模型
- [x] 模型配置保存后热重建 Provider/Agent；失败时才提示重启 Runtime
- [x] 模型提供商表格/列表
- [x] 提供商 ID、名称、Base URL、密钥状态、健康状态、操作
- [x] 测试连接（不保存）
- [x] 编辑提供商
- [x] 删除提供商（带确认与占用提示）
- [x] 添加提供商
- [x] 提供商预设（DeepSeek 等）
- [x] 自定义 OpenAI / Anthropic 兼容端点
- [x] 上游模型厂商选择
- [x] 协议选择（预设时锁定）
- [x] 提供商 ID 校验；编辑时不可改 ID
- [x] Base URL 自动补 `/v1`
- [x] API Key 显示/隐藏、留空不修改
- [x] 思考参数风格、思考强度传递
- [x] 超时
- [x] 模型 ID + 建议列表
- [x] 获取模型列表
- [x] 测试未保存配置
- [x] 4 个 Agent 卡片：主聊天、主动思考、滚动摘要、人格生成
- [x] Agent 禁用态
- [x] 提供商、模型 ID、温度、top_p、最大输出 token
- [x] 启用思考模式
- [x] 思考强度
- [x] 首 token 超时
- [x] 每个 Agent 独立保存
- [x] 本地模型插件：模型/类型/状态/依赖
- [x] 本地模型详情：状态、类型、目标目录、下载页、错误
- [x] 安装指引
- [x] 上传模型 zip
- [x] 安装依赖
- [~] WebUI 提供商和 Agent 是表格/双列卡片；原生提供商为列表卡片，Agent 为卡片，信息一致
- [~] WebUI 获取模型列表使用 datalist；原生使用建议菜单
- [~] 原生补充了首 token 超时字段（WebUI 当前未显示，属于能力补齐）
- [x] 编辑预设提供商时同步预设协议/地址/思考方式，避免把 anthropic 预设静默写成 openai_compat
- [x] 已不存在的 provider 不自动落回列表第一个，保存前明确报错
- [x] 思考强度选项随 provider/模型动态刷新，包含 minimal；禁用思考时降低提示
- [x] 禁用 Agent 显示“未启用”卡片，而不是直接省略
- [x] 本地模型显示类型、下载页、错误、缺失依赖名称，并可单独重试扫描/上传/安装
- [x] 提供商/预设/本地模型分区读取，单项失败不影响其他分区
- [x] 删除提供商确认框列出 used_by；测试连接失败保留状态并刷新健康信息
- [x] top_p=0 等零值不被 1.0 默认值覆盖
- [x] 原生有意差异：本地模型扫盘/依赖安装仍通过系统 Python/现有 API，未引入浏览器下载行为

---

# 11. 表情包

- [x] 页面标题“表情包”与说明
- [x] 按名称筛选
- [x] 支持格式与单文件大小说明
- [x] 表情包网格
- [x] 添加表情包（多选文件）
- [x] 每个文件可单独改名
- [x] 预览缩略图
- [x] 跳过不支持格式/超大文件并报告
- [x] 重名自动改名提示
- [x] 删除表情包（带确认）
- [x] 空状态、搜索无结果、读取失败
- [~] WebUI 支持拖放；原生使用文件选择器
- [~] WebUI 每个待上传文件有移除按钮；原生在多选器内移除（桌面习惯）
- [x] 上传暂存区与底部状态只保留一份；上传统计包含跳过与重名另存数量
- [x] 文件读取与 multipart 上传在工作线程执行，不阻塞 GTK 主循环

---

# 12. 容器

## 12.1 页面与操作

- [x] 页面标题“受控容器”、安全边界说明
- [x] “刷新”
- [x] “一键准备 / 启动”
- [x] “初始化 / 重新挂载 C 存储”（只在需要时出现）
- [x] “重建容器”
- [x] “打开容器设置”
- [x] 功能 / 就绪 / Docker / 存储 四张状态卡
- [x] 问题卡：功能未启用
- [x] 问题卡：Docker 不可用
- [x] 问题卡：镜像缺失
- [x] 问题卡：C 存储尺寸不一致（采用实际大小 / 修改设置 / 重启）
- [x] 问题卡：C 存储未初始化/未挂载
- [x] 问题卡：配置变化警告
- [x] 容器测试前提示首次拉取镜像
- [x] 容器测试失败保留错误与 stderr/stdout 输出尾部，刷新后仍在问题卡显示
- [x] 重启 Runtime 前确认，避免误中断在途请求

## 12.2 Agent 模式

- [x] Agent 模式表：模式 / 状态 / 版本 / 依赖 / 操作
- [x] 模式说明
- [x] 安装/重装
- [x] 激活
- [x] 停用
- [x] 卸载（带确认，保留 /workspace）
- [x] 使用中/已安装/未安装徽标
- [x] 内置回退依赖展示
- [x] 安装说明（写入 `/workspace/.karen-agents`）
- [x] 本机 Claude Code 执行方式：沙箱 / host
- [x] 沙箱联网开关
- [x] 保存本机设置
- [x] 本机设置风险说明
- [x] Agent 状态与容器状态并行读取
- [x] 安装/激活/停用/卸载期间禁用全部 Agent 操作按钮，快速二次点击不会静默无效
- [~] Agent 模式安装进度/日志：WebUI 也只有 toast，原生保持 toast 并在行内更新状态

## 12.3 工作区文件管理

- [x] 本机工作区 / 容器工作区两个切换按钮
- [x] 只读说明
- [x] 路径输入、打开路径、上一级、刷新
- [x] 面包屑导航
- [x] 当前目录筛选
- [x] 15 秒目录缓存
- [x] 名称 / 类型 / 大小 / 修改时间 / 权限 / 操作 表
- [x] 目录“打开”
- [x] 文件“查看”/“查看图片”
- [x] 符号链接不跟随
- [x] 文本预览、二进制提示、截断提示
- [x] 常见图片预览
- [x] 容器未运行提示
- [x] 两个工作区各自记住路径和筛选
- [x] 重建容器后清空缓存并重置浏览状态
- [~] WebUI 使用模态预览层；原生使用独立 GTK 窗口
- [~] WebUI 行内面包屑更紧凑；原生面包屑按钮尺寸略大

## 12.4 诊断与存储

- [x] 运行诊断表：Docker Server、镜像、容器名/状态、内存/CPU、pids/单文件、网络、exec 超时、存储、平台
- [x] 存储与安装表：挂载点、镜像文件、初始化方式、安装命令
- [x] C 存储初始化/挂载对话框
- [x] sudo 密码只用于本次命令、不保存/不写日志/不进命令行
- [x] Windows UAC 初始化提示
- [x] 无提权方式时显示手动命令
- [x] 初始化成功/失败状态；失败时显示 output_tail
- [x] 关闭初始化窗口时清空 sudo 密码并重新提示手动命令
- [x] 尺寸不一致时“采用实际大小”
- [x] 不自动降级到 B 模式
- [~] WebUI 在初始化时显示 HTTPS 警告；原生本地进程无网络传输密码，故省略
- [x] 原生始终不会触碰容器内已有文件

---

# 13. 存储与数据

- [x] 页面标题“存储与数据”与说明
- [x] 磁盘可用空间、已用比例、总量
- [x] 可回收缓存
- [x] 受保护数据
- [x] 历史分页缓存（页数/占用/记录数）
- [x] 磁盘低水位预警
- [x] 维护模式、临时文件保留时间、自动清理状态
- [x] 占用分类表：类别/状态/介质/占用/数量/说明
- [x] 可清理类别多选；刷新后保留用户勾选，只首次默认勾选
- [x] 安全清理预览（影响、警告、候选数量、预览过期时间）；确认绑定当前预览，避免执行另一份预览
- [x] 执行清理、创建后台任务、idempotency
- [x] 任务轮询与完成/部分成功/失败提示
- [x] 上下文预算只读快照：有效窗口、输出预留、安全余量、净输入预算
- [x] 窗口/模型限制、阈值、固定开销、历史条数、活跃起点
- [x] 手动压缩上下文（执行前确认并防重复点击）
- [x] 清理任务列表：创建时间、状态、类别、已释放、缓存条目、跳过、操作
- [x] 清理任务取消
- [x] 空状态与读取失败
- [x] 四张统计卡：磁盘、可回收、受保护、历史分页缓存；维护状态单独一行
- [~] WebUI 的任务表有更多阶段字段；原生保留核心字段
- [x] “人格运行数据”清理入口（跳到人格页逐个人格执行）
- [x] “卸载 KarenBot”入口、影响预览、目标/保留项、确认短语与可选备份
- [x] 最近危险操作任务表：时间/操作/对象/状态/阶段/结果/取消

---

# 14. 黑白名单

- [x] 页面标题“黑白名单”与说明
- [x] 页面标题右侧“保存并立即生效”
- [x] 入站策略：open / verify / whitelist
- [x] 白名单 QQ / 群
- [x] 启用黑名单
- [x] 黑名单 QQ / 群
- [x] 黑名单优先说明
- [x] 白名单为空且 whitelist 模式的警告
- [x] 保存后立即生效，无需重启
- [x] 读取失败提示
- [x] 可增删的数字 chip，接受空格/逗号/中文逗号/顿号/分号
- [x] 未保存修改离开提醒（取消 / 放弃修改），关闭窗口同样拦截
- [x] 空名单逐项“名单为空”视觉提示（ChipList）
- [x] 编辑时实时重算策略/名单状态和空白名单警告
- [x] 页面重新激活时刷新；有未保存修改时不自动覆盖，标题栏刷新也先确认
- [x] whitelist 且白名单为空时保存前二次确认

---

# 15. 群聊触发

- [x] 页面标题“群聊触发”与说明
- [x] 页面标题右侧“保存并立即生效”
- [x] 启用按群 @ 触发
- [x] 群号、连续消息数、时间窗口、过滤缓存条数
- [x] 添加群规则
- [x] 群号/重复规则校验
- [x] 规则行内编辑数值
- [x] 移除规则
- [x] 每个群规则说明
- [x] 当前窗口：群号、来源、剩余消息、剩余时间、状态
- [x] LLM 临时放行说明
- [x] 空状态、读取失败
- [x] 原生默认过滤缓存 50，与 WebUI 当前默认一致
- [x] 添加规则为内联可编辑行，与 WebUI 一致
- [x] 空群号行、非正整数和重复群号阻止保存并聚焦到问题行
- [~] WebUI 活动窗口与规则同页刷新；原生在页面激活/保存后刷新，无独立轮询
- [x] 未保存修改离开提醒（取消 / 放弃修改），关闭窗口同样拦截
- [x] 行内“未保存”徽标

---

# 16. 设置

## 16.1 布局与通用能力

- [x] 页面标题“设置”与说明
- [x] 左侧分组导航 + 右侧字段区
- [x] 分组：渠道、渠道高级、软件行为、功能、容器、记忆、主动思考预算、人格与生理、Token 预算、上下文推算规则、工具循环提醒、存储与缓存、更新、备份、日志与诊断、路径与诊断
- [x] 分组切换不刷新整个页面
- [x] 影响运行时的字段显示“需重启”徽标
- [x] 重启提示卡 + 立即重启 Runtime
- [x] 布尔字段即时保存
- [x] 枚举字段即时保存
- [x] 枚举选项帮助文字（例如工具 schema 档位）
- [x] 密钥字段写入加密密钥库，不落 config.yaml
- [x] 密钥留空不修改；清空需确认
- [x] 列表字段：逗号/换行分隔
- [x] 多行列表：失焦保存、Ctrl+Enter 保存
- [x] JSON 字段：失焦保存、Ctrl+Enter 保存、后端行号错误提示
- [x] 整数上下限 SpinButton
- [x] 文本/数字：失焦或 Enter 保存
- [x] 保存失败回滚控件
- [x] 无变化时不重复写盘
- [x] 159/179 个字段保存后热应用（WebUI 与 GUI 共用后端返回值）
- [x] 配置递归合并保留嵌套对象引用，运行中的 Pipeline/服务立即看到新值
- [x] 渠道开关/地址、行为参数、存储缓存、视觉、搜索、天气、TTS、容器普通限制、工具 schema 档位热应用
- [x] 提供商与 Agent 修改热重建 Provider/Agent，失败才提示重启
- [x] 20 个结构性字段仍明确要求重启（容器存储/工作区大小、RAG/Embedding 模式、人格后台启停等）
- [x] 保存重启型字段后设置“待重启”，后续修改不再半热应用，避免运行中配置与组件引用不一致
- [x] 并行读取设置、提供商、预设
- [x] 兼容门禁前自动补齐缺失的派生 maintenance.sqlite3，修复已有实例永久卡在 maintenance v0→v1；真正空实例仍走初始化门禁
- [x] 设置读取失败时显示原因、重试和“打开路径与诊断”，不再只剩空白分组
- [x] 深链到指定分组
- [x] 路径与诊断页：数据目录、工作区、容器目录、密钥文件
- [x] 启动 / 停止 / 重启 Runtime
- [x] 特殊面板：视觉、搜索、备份、更新、Token 预算
- [x] 更新面板提供“打开版本与更新”入口，以及“重启后端”按钮
- [x] 视觉面板检测已有的 base_url/api_key_id 覆盖并默认保留，避免保存时静默清空
- [x] 备份面板支持 partial（部分完成）终止状态、警告与状态徽标
- [~] WebUI 设置字段按条件显示/隐藏（如 Embedding 类型、视觉启用）；原生通用表单会保留相关字段，专用面板已做条件布局，普通分组仍是全量显示
- [x] WebUI 提供商/Agent 配置在模型管理页，设置页只放运行参数；原生与之一致
- [~] WebUI 备份目录使用路径选择器；原生已有选择器，但更新 release 列表仍在独立页面
- [~] WebUI 的“恢复默认值”/撤销单字段：当前 WebUI 版本没有必须项，原生保持逐项保存

## 16.2 设置字段逐项清单

说明：以下字段来自 `backend/settings_spec.SETTINGS_GROUPS`，共 15 组、179 个字段。所有字段已由原生声明式表单或专用面板渲染；标记 `[x]` 表示可见、可读、可写。特殊交互差异在组标题下注明。`hidden` 字段由专用面板接管。

### 渠道（`adapter`，10 项）

密钥字段写入加密密钥库；敏感值不回显。修改后显示重启徽标。

- [x] `adapters.default.enabled` · 启用渠道（`bool`）
- [x] `adapters.default.mode` · 连接模式（`enum`）
- [x] `adapters.default.host` · 地址（`str`）
- [x] `adapters.default.port` · 端口（`int`）
- [x] `adapters.default.path` · 路径（`str`）
- [x] `adapters.default.access_token_id` · Access Token（`secret_ref`）
- [x] `adapters.default.manage_process` · 由 Karen 拉起 NapCat（`bool`）
- [x] `adapters.default.process_path` · NapCat 启动路径（`str`）
- [x] `adapters.default.api_timeout_seconds` · API 超时（秒）（`float`）
- [x] `adapters.default.ping_interval_seconds` · 心跳间隔（秒）（`float`）

### 渠道高级（`adapter_advanced`，14 项）

重连节奏与进程管理字段全部可写；帮助文字来自 settings_spec。

- [x] `adapters.default.auto_restart` · 断线自动重连（`bool`）
- [x] `adapters.default.process_args` · 启动参数（`list_str`）
- [x] `adapters.default.startup_connect_timeout_seconds` · 进程启动后等待连接（秒）（`float`）
- [x] `adapters.default.initial_connect_timeout_seconds` · 首次连接超时（秒）（`float`）
- [x] `adapters.default.api_wait_connected_timeout_seconds` · 等连接再发 API 的上限（秒）（`float`）
- [x] `adapters.default.fast_reconnect_attempts` · 快速重试次数（`int`）
- [x] `adapters.default.fast_reconnect_interval_seconds` · 快速重试间隔（秒）（`float`）
- [x] `adapters.default.reconnect_interval_seconds` · 常规重连间隔（秒）（`float`）
- [x] `adapters.default.reconnect_backoff_max_seconds` · 退避上限（秒）（`float`）
- [x] `adapters.default.reconnect_jitter_seconds` · 重连抖动（秒）（`float`）
- [x] `adapters.default.max_reconnect_attempts` · 最大重连次数（`int`）
- [x] `adapters.default.ping_timeout_seconds` · 心跳超时（秒）（`float`）
- [x] `adapters.default.process_warmup_seconds` · 托管进程预热（秒）（`float`）
- [x] `adapters.default.voice_fetch_delay_seconds` · 语音转写等待（秒）（`float`）

### 软件行为（`behavior`，12 项）

工具 schema 档位显示四项选项解释；原生外观由本地主题按钮独立管理，不自动改写 app.theme。

- [x] `behavior.merge_window_seconds` · 消息合并窗口（秒）（`float`）
- [x] `behavior.recall_merge_window_seconds` · 撤回合并窗口（秒）（`float`）
- [x] `behavior.default_history_fetch_count` · 默认拉取历史条数（`int`）
- [x] `behavior.proactive_think_interval_seconds` · 主动思考间隔（秒）（`float`）
- [x] `behavior.proactive_context_token_budget` · 主动思考上下文预算（`int`）
- [x] `behavior.tool_schema_profile` · 工具 schema 档位（`enum`）
- [x] `behavior.pending_request_timeout_seconds` · 待处理请求超时（秒）（`float`）
- [x] `behavior.rate_limit.enabled` · 启用陌生人限流（`bool`）
- [x] `behavior.rate_limit.window_seconds` · 限流窗口（秒）（`int`）
- [x] `behavior.rate_limit.max_messages` · 窗口内最大条数（`int`）
- [x] `app.theme` · 主题（`enum`）
- [x] `app.log_level` · 日志级别（`enum`）

### 功能（`features`，42 项）

视觉与搜索由专用面板接管；天气、TTS、黑话、知识字段使用声明式表单。视觉/搜索专用面板已做条件显隐；普通分组仍展示全部字段，避免隐藏后无法发现。

- [x] `features.web_search.enabled` · 联网搜索（`bool`） — 由搜索专用面板提供
- [x] `features.web_search.free_enabled` · 免费搜索（`bool`） — 由搜索专用面板提供
- [x] `features.web_search.provider` · 免费搜索后端（`enum`） — 由搜索专用面板提供
- [x] `features.web_search.searxng_base_url` · SearXNG 地址（`str`） — 由搜索专用面板提供
- [x] `features.web_search.max_results` · 最多结果数（`int`） — 由搜索专用面板提供
- [x] `features.web_search.timeout_seconds` · 免费搜索超时（秒）（`float`） — 由搜索专用面板提供
- [x] `features.web_search.official.enabled` · 官方搜索（`bool`） — 由搜索专用面板提供
- [x] `features.web_search.official.provider` · 官方搜索 provider（`str`） — 由搜索专用面板提供
- [x] `features.web_search.official.model` · 官方搜索模型（`str`） — 由搜索专用面板提供
- [x] `features.web_search.official.timeout_seconds` · 官方搜索超时（秒）（`float`） — 由搜索专用面板提供
- [x] `features.slang.enabled` · 群聊黑话学习（`bool`）
- [x] `features.slang.auto_learn` · 自动提取黑话候选（`bool`）
- [x] `features.slang.extract_min_messages` · 黑话提取阈值（条）（`int`）
- [x] `features.slang.extract_cooldown_seconds` · 黑话提取冷却（秒）（`int`）
- [x] `features.slang.inject_max` · 黑话查询返回上限（条）（`int`）
- [x] `features.slang.max_prompt_chars` · 黑话查询字符上限（`int`）
- [x] `features.knowledge.enabled` · 联网考究知识库（`bool`）
- [x] `features.knowledge.inject_max` · 知识查询返回上限（条）（`int`）
- [x] `features.knowledge.max_prompt_chars` · 知识查询字符上限（`int`）
- [x] `features.knowledge.auto_research_enabled` · 后台自动考究热梗（`bool`）
- [x] `features.knowledge.research_interval_hours` · 热梗考究间隔（小时）（`int`）
- [x] `features.knowledge.research_search_provider` · 热梗考究搜索后端（`enum`）
- [x] `features.knowledge.research_max_notes` · 单次考究保存上限（条）（`int`）
- [x] `features.knowledge.research_queries` · 热梗考究搜索词（`list_str`）
- [x] `features.vision.main_model_images` · 主模型原生图片输入（`enum`） — 由视觉专用面板提供
- [x] `features.vision.enabled` · 独立视觉识别（describe_image）（`bool`） — 由视觉专用面板提供
- [x] `features.vision.provider` · 视觉 provider（`str`） — 由视觉专用面板提供
- [x] `features.vision.model` · 视觉模型（`str`） — 由视觉专用面板提供
- [x] `features.vision.base_url` · 视觉独立 base_url（`str`） — 由视觉专用面板提供
- [x] `features.vision.api_key_id` · 视觉独立 API Key（`secret_ref`） — 由视觉专用面板提供
- [x] `features.vision.gif_mode` · GIF 处理方式（`enum`） — 由视觉专用面板提供
- [x] `features.vision.main_model_videos` · 主模型原生视频输入（`enum`） — 由视觉专用面板提供
- [x] `features.vision.video_token_reserve` · 每段视频 token 预留（`int`） — 由视觉专用面板提供
- [x] `features.vision.video_limits.max_videos_per_request` · 单请求最大视频数（`int`） — 由视觉专用面板提供
- [x] `features.vision.image_token_reserve` · 每图 token 预留（`int`） — 由视觉专用面板提供
- [x] `features.vision.image_limits.max_images_per_request` · 单请求最大图片数（`int`） — 由视觉专用面板提供
- [x] `features.weather.enabled` · 天气查询（`bool`）
- [x] `features.weather.host` · 和风天气 API Host（`str`）
- [x] `features.weather.api_key_id` · 和风天气 Key（`secret_ref`）
- [x] `features.tts.enabled` · 语音合成（`bool`）
- [x] `features.tts.type` · TTS 类型（`enum`）
- [x] `features.tts.provider` · 云端厂商（`enum`）

### 容器（`container`，18 项）

字段本身全部可写；启用容器或切换到 C 固定大小后，会刷新容器存储状态并在需要时自动打开管理员初始化对话框。

- [x] `features.container.enabled` · 启用容器工具（`bool`）
- [x] `features.container.storage_mode` · 存储模式（`enum`）；切换 C 后自动提示初始化；运行中的旧模式会提示先重启
- [x] `features.container.image` · 容器镜像（`str`）
- [x] `features.container.container_name` · 容器名（`str`）
- [x] `features.container.volume_name` · B 命名卷（`str`）
- [x] `features.container.workspace_size_mb` · 存储大小 (MiB)（`int`）
- [x] `features.container.memory_mb` · 内存上限 (MiB)（`int`）
- [x] `features.container.cpus` · CPU 配额（`float`）
- [x] `features.container.pids_limit` · pids 上限（`int`）
- [x] `features.container.file_size_mb` · 单文件上限 (MiB)（`int`）
- [x] `features.container.exec_timeout_seconds` · exec 超时 (秒)（`int`）
- [x] `features.container.max_output_bytes` · 输出上限 (字节)（`int`）
- [x] `features.container.network` · 网络（`enum`）
- [x] `features.container.idle_timeout_minutes` · 空闲停止 (分钟)（`int`）
- [x] `features.container.auto_pull_image` · 自动拉取镜像（`bool`）
- [x] `features.container.image_mirror` · Docker 镜像加速前缀（`str`）
- [x] `features.container.image_fixed_mountpoint` · C 挂载点（`str`）
- [x] `features.container.image_fixed_image_path` · C 镜像文件（`str`）

### 记忆（`memory`，6 项）

Embedding 相关字段在通用分组中全部可见；设置页可选地按模式隐藏，但为避免用户找不到字段，原生保留全量显示。

- [x] `features.long_term_memory.mode` · 长期记忆模式（`enum`）
- [x] `features.embedding.enabled` · 启用向量检索（`bool`）
- [x] `features.embedding.type` · Embedding 类型（`enum`）
- [x] `features.embedding.provider` · Embedding provider（`str`）
- [x] `features.embedding.api_model` · Embedding 模型（`str`）
- [x] `features.embedding.local_quality` · 本地模型档位（`enum`）

### 主动思考预算（`proactive_router`，5 项）

主动思考上下文预算字段。

- [x] `behavior.proactive_router_text_limit_tokens` · 文本片段上限 token（`int`）
- [x] `behavior.proactive_router_tool_result_inline_tokens` · 工具结果内联 token（`int`）
- [x] `behavior.proactive_router_tool_result_hard_cap_tokens` · 工具结果硬上限 token（`int`）
- [x] `behavior.proactive_router_summary_limit_tokens` · 摘要片段上限 token（`int`）
- [x] `behavior.proactive_router_history_token_budget` · 历史对话总预算 token（`int`）

### 人格与生理（`persona_physiology`，17 项）

人格生成、能量、饱食度、工作上限字段；生理机制的启用条件由运行时校验。

- [x] `persona_management.enabled` · 启用人格后台（`bool`）
- [x] `persona_management.age.default_age` · 默认年龄（`int_or_none`）
- [x] `persona_management.persona_agent.provider` · 人格生成 provider（`str`）
- [x] `persona_management.persona_agent.model` · 人格生成模型（`str`）
- [x] `persona_management.persona_agent.temperature` · 人格生成温度（`float`）
- [x] `persona_management.persona_agent.max_tokens` · 人格生成最大输出 token（`int`）
- [x] `persona_management.persona_agent.timer_interval_minutes` · 人格生成间隔（分钟）（`int`）
- [x] `persona_management.persona_agent.min_interval_seconds` · 人格生成最短间隔（秒）（`int`）
- [x] `persona_management.physiology.energy.mode` · 能量机制（`enum`）
- [x] `persona_management.physiology.energy.decay_per_hour` · 能量每小时衰减（`float`）
- [x] `persona_management.physiology.energy.recovery_per_hour_sleep` · 睡眠每小时恢复（`float`）
- [x] `persona_management.physiology.energy.long_sleep_threshold_minutes` · 长睡眠阈值（分钟）（`int`）
- [x] `persona_management.physiology.energy.max_sleep_minutes` · 单次睡眠上限（分钟）（`int`）
- [x] `persona_management.physiology.satiety.mode` · 饱食度机制（`enum`）
- [x] `persona_management.physiology.satiety.decay_per_hour` · 饱食度每小时衰减（`float`）
- [x] `persona_management.physiology.satiety.recovery_per_minute` · 进食每分钟恢复（`float`）
- [x] `persona_management.physiology.work.max_minutes` · 单次工作上限（分钟，0=禁用）（`int`）

### Token 预算（`token_budget`，28 项）

专用 Token 预算面板提供结构化快照和手动压缩，同时保留全部字段编辑。

- [x] `behavior.context.max_context_tokens` · 最大上下文 token（`int_or_none`）
- [x] `behavior.context.reserve_output_tokens` · 预留输出 token（`int`）
- [x] `behavior.context.budget_policy_version` · 预算策略版本（`int`）
- [x] `behavior.context.safety_margin_percent` · 安全余量（%）（`int`）
- [x] `behavior.context.safety_margin_min_tokens` · 安全余量最小值 token（`int`）
- [x] `behavior.context.prompt_overhead_estimate_tokens` · 提示词开销估算（`int`）
- [x] `behavior.context.memory_token_budget` · 记忆 token 预算（`int`）
- [x] `behavior.context.summary_token_budget` · 摘要 token 预算（`int`）
- [x] `behavior.context.tool_result_default_budget_tokens` · 工具结果默认预算（`int`）
- [x] `behavior.context.tool_result_default_hard_cap_tokens` · 工具结果硬上限（`int`）
- [x] `behavior.summarize.background_enabled` · 允许后台自动压缩（`bool`）
- [x] `behavior.summarize.forecast_enabled` · 启用增长预测（`bool`）
- [x] `behavior.summarize.cold_growth_tokens` · 冷启动增长预算 token（`int`）
- [x] `behavior.summarize.cooldown_seconds` · 压缩冷却（秒）（`int`）
- [x] `behavior.summarize.min_new_tokens` · 再次压缩所需新增 token（`int`）
- [x] `behavior.summarize.trigger_at_context_percent` · 触发摘要的上下文占比（%）（`int`）
- [x] `behavior.summarize.target_after_context_percent` · 摘要后压到（%）（`int`）
- [x] `behavior.summarize.retry_target_after_context_percent` · 重试时压到（%）（`int`）
- [x] `behavior.summarize.urgent_at_input_percent` · 紧急压缩线（%）（`int`）
- [x] `behavior.kv_prompt_diagnostics` · KV 诊断级别（`enum`）
- [x] `behavior.auto_finish_after_send` · 发送成功后自动收尾（`bool`）
- [x] `behavior.context.model_view.enabled` · 启用模型视图裁剪（`bool`）
- [x] `behavior.context.model_view.task_context_keep` · 保留最近 task_context 数量（`int`）
- [x] `behavior.context.model_view.send_receipt_keep` · 保留最近发送回执数量（`int`）
- [x] `behavior.context.model_view.send_status_keep` · 保留最近发送完成快照数量（`int`）
- [x] `behavior.context.model_view.no_action_keep` · 保留最近 no_action 组数（`int`）
- [x] `behavior.context.model_view.max_record_chars` · 单条记录最大字符数（`int`）
- [x] `behavior.context.model_view.project_tool_results` · 精简发送类工具结果（`bool`）

### 上下文推算规则（`context_rules`，7 项）

JSON 字段支持失焦保存和 Ctrl+Enter；后端返回带行号的 JSON 错误。

- [x] `behavior.context.recommended_context_budget.context_length_scale_percent` · 按模型上下文长度的百分比（%）（`int`）
- [x] `behavior.context.recommended_context_budget.min_scaled_budget_tokens` · 缩放后的下限 token（`int`）
- [x] `behavior.context.recommended_context_budget.fallback_budget_tokens` · 查不到模型时的兜底 token（`int`）
- [x] `behavior.context.recommended_context_budget.model_name_budget_tokens` · 按模型名的特例表（`json`）
- [x] `behavior.context.recommended_context_budget.context_length_rules` · 按上下文长度的规则表（`json`）
- [x] `behavior.context.tool_result_budgets` · 逐工具结果预算（`json`）
- [x] `behavior.context.tool_result_soft_overrides` · 逐工具软上限覆盖（`json`）

### 工具循环提醒（`agent_loops`，6 项）

工具循环上限、提醒间隔、收尾阈值与输出上限。

- [x] `agents.chat.max_loops` · 工具循环上限（`int`）
- [x] `agents.chat.tool_loop_reminder_interval` · 每隔几轮提醒一次（`int`）
- [x] `agents.chat.tool_loop_final_warning_count` · 收尾警告阈值（剩几轮）（`int`）
- [x] `agents.chat.tool_loop_final_grace_loops` · 收尾宽限轮数（`int`）
- [x] `agents.chat.tool_loop_final_max_tokens` · 收尾输出上限 token（`int`）
- [x] `agents.chat.refocus_interval` · 重新聚焦间隔（轮）（`int`）

### 存储与缓存（`storage`，8 项）

清理与时序字段；危险清理动作在存储与数据页，不在设置页。

- [x] `storage.maintenance.temp_hours` · 临时文件保留（小时）（`int`）
- [x] `storage.maintenance.derived_cache_days` · 派生缓存保留（天）（`int`）
- [x] `storage.retention.inbound_media_hours` · 入站图片/视频保留（小时）（`int`）
- [x] `storage.maintenance.min_free_bytes` · 磁盘低水位（字节）（`int`）
- [x] `storage.maintenance.min_free_percent` · 磁盘低水位（%）（`int`）
- [x] `storage.maintenance.history_cache_max_bytes` · 历史分页缓存上限（字节）（`int`）
- [x] `storage.maintenance.history_cache_page_size` · 历史分页大小（条）（`int`）
- [x] `storage.maintenance.history_cache_max_pages` · 历史分页缓存最多页数（`int`）

### 更新（`update`，4 项）

镜像字段由专用面板接管；完整的 Release 浏览、兼容检查、更新/回退/切换和 GUI 本地后端重启在独立“版本与更新”页。

- [x] `update.mirror` · GitHub 镜像/加速源（`str`） — 由更新专用面板提供
- [x] `update.check_timeout_seconds` · 检查超时（秒）（`int`）
- [x] `update.check_interval_days` · 自动检查间隔（天）（`int`）
- [x] `update.auto_install_dependencies` · 更新时自动安装依赖（`bool`）

### 备份（`backup`，2 项）

备份目录字段带路径选择器；备份任务、进度、取消、校验、下载由专用面板提供。

- [x] `backup.directory` · 备份目录（`str`）
- [x] `backup.include_media` · 包含入站媒体/文件（`bool`）

### 日志与诊断（`diagnostics`，0 项）

无声明式字段；“路径与诊断”页提供数据目录、密钥文件和 Runtime 启停诊断。

- [x] 该分组无声明式字段；诊断能力见“路径与诊断”。

> 字段合计：179 项。

---

# 17. 首次配置向导

- [x] 首次运行检测与独立数据目录
- [x] 读取 `/api/setup/options` 预置项
- [x] 提供商预设、模型建议
- [x] API Key
- [x] 自定义 Base URL
- [x] 渠道模式（client/server）、地址、端口、路径
- [x] WebSocket Access Token
- [x] 由 Karen 拉起 NapCat、进程路径、自动重连
- [x] 入站策略
- [x] 白名单 QQ / 群
- [x] 写入配置并启动 Runtime
- [x] 原生使用后端 flow 驱动的分步向导
- [x] “开始之前”推荐/自定义路径选择
- [x] 独立“其它模型”步骤（主动思考/滚动摘要/人格生成）
- [x] “选些可选的本领”步骤（图片、视觉、搜索、天气、TTS）
- [x] “记忆方式”步骤（重要记忆文件/向量检索）
- [x] “赋予一个角色”步骤（内置人格/生成新人格）
- [x] “塑造你的角色”分支（描述暂存，配置后到人格页继续生成）
- [x] “就快好了”汇总表
- [x] 跳过可选步骤；服务端 flow 决定下一步
- [x] 步骤进度条与返回；进度响应带当前步骤校验，避免旧响应覆盖
- [x] 从向导填写人格描述，配置完成后可在人格页继续生成
- [x] 主模型原生图片策略始终可见，与独立视觉开关分开
- [x] 每图 token 预留默认 8192、允许 256–65536；单请求最大图片数 1–20
- [x] 思考强度档位跟随当前预设/模型，包含 minimal；关闭思考时隐藏
- [x] NapCat 进程路径/自动重连按 manage_process 显示；白名单字段仅在 whitelist 模式显示
- [x] 进度读取失败时禁用下一步并自动重试，避免使用旧的 is_last 提前提交
- [~] 向导提供商/Agent 字段覆盖日常配置；厂商细分、思考风格等高级项仍在设置/模型页调整

---

# 18. 版本与更新

- [x] 版本、分支、工作树、远端信息
- [x] GitHub 镜像/加速源预设与自定义
- [x] 镜像测速
- [x] 更新检查间隔/超时/自动安装依赖字段
- [x] 更新提醒开关（`suppress_badge`）
- [x] 远端 Release 列表：标签、名称、提交、时间
- [x] 关系徽标：当前版本/可更新/可回退/另一条历史
- [x] 兼容性徽标与不可应用原因
- [x] 更新日志展开/收起
- [x] 自动加载远端 Release；镜像测速结果可一键“使用”并保存
- [x] 更新检查展示 warning、依赖文件变更、提交列表、兼容原因
- [x] 更新/回退/切换：兼容门禁、确认令牌、目标提交校验、分支切换与回退确认
- [x] 依赖文件变更按后端自动安装/回滚策略执行；失败原因直接显示
- [x] 检查目标版本（指定 tag、关系、当前 HEAD、工作树）
- [x] 确认更新/回退/切换：列出警告、依赖变更与提交，用户确认后才操作源码树
- [x] 工作树脏、数据兼容失败、依赖需手动安装等阻止更新并显示原因
- [x] 更新后提示重启 GUI 本地后端；重启会重新加载磁盘上的 Python 代码
- [x] 更新提醒抑制写入（`suppress_badge`）
- [x] GUI 界面代码若也更新，文档明确要求重新打开桌面应用
- [~] 原生通过“打开版本与更新”进入独立页，而不是设置页内嵌；能力等价

---

# 19. 危险管理

- [x] 人格运行数据清理预览（聊天/记忆数据库、RAG、私有知识、预计释放）
- [x] 保留项清单
- [x] 可选“清理前完整备份”
- [x] 强制勾选不可撤销确认
- [x] 输入确认短语
- [x] 后台任务进度与 Runtime 自动恢复
- [x] 审计记录
- [x] 本地完全卸载影响预览
- [x] 卸载目标路径、受保护目标
- [x] 可选完整备份
- [x] 卸载确认短语
- [x] 独立助手在 WebUI 退出后继续删除
- [x] 卸载进度页与审计路径

> 危险操作验证范围：已覆盖服务端预览、确认短语/复选框、对话框渲染、任务列表读取；**未在验证中实际执行删除或卸载**。执行入口要求 `X-Karen-Admin: 1`、确认短语、不可撤销勾选，可选备份默认关闭；卸载会退出本地控制服务。

---

# 20. 性能与兼容性约束

- [x] 原生 GTK 进程内复用 FastAPI 控制面，不启动浏览器、不加载 WebUI 静态资源
- [x] HTTP 调用全部在工作线程
- [x] 回调队列泵送，避免 GLib 跨线程警告
- [x] 总览 2 秒轮询；更新检查独立 60 秒
- [x] 页面离开后停止总览/日志/人格后台轮询
- [x] 请求防重入（刷新、加载、Agent 操作）
- [x] 文件管理目录缓存 15 秒
- [x] 设置页并行请求
- [x] 容器状态/存储/Agent 并行请求
- [x] 聊天列表与记录加载串行防竞态
- [x] GTK 资源在窗口关闭时释放
- [x] 本地后端随机端口 + token，不暴露局域网
- [x] Windows 代码路径兼容（继承自 v1.0.1-alpha.1 的修复）
- [x] Linux/aarch64 实机 smoke 通过
- [~] 大量表格仍采用普通 Gtk.Box 行，适配小屏时列宽会压缩；后续可针对窄窗口做列折叠
- [~] 部分页面使用轮询而非事件流，CPU 占用低但实时性不如 WebSocket
- [ ] Windows 实机回归（需要 Windows 环境）
- [ ] macOS 实机回归（需要 macOS 环境）

---

# 21. 前后端模块边界

- [x] `backend/`：共享控制面（`backend/app.py`、`runtime_host.py`、`api/`、`auth.py`、`events.py`、`logbuffer.py`、`settings_spec.py`、`wizard/`、`chatlog/` 等）
- [x] `webui/`：只保留 `server.py`、`__init__.py`、`theme.py`、`static/`
- [x] `gui/`：只保留原生前端和 `LocalControlServer` 适配器
- [x] WebUI 服务器只负责静态挂载、令牌横幅、端口预检和 uvicorn 启动
- [x] GUI 只负责起本机控制服务、原生控件和 GTK 生命周期
- [x] 两个前端共用 `create_control_app()`，路由、鉴权、Runtime 启停、更新 watchdog 只有一份
- [x] `backend/` 无任何 `webui` / `gui` / GTK / 静态资源导入
- [x] 后端独立测试与前端页面测试仍可分别运行
- [x] 后续一键安装器可以只安装 `backend` + 选择 WebUI 或 GUI，而不再把两者绑在一起

---

# 22. 下一步优先级

1. [x] 人格后台：横向标签页、动态表格、唤醒模态框
2. [x] 对话：底部常驻试跑输入框、默认展开开关、记录总数
3. [x] 黑白名单/群聊触发：chip 编辑、未保存提醒
4. [x] 设置：条件字段显示、热应用与完整更新面板
5. [x] 首次配置：分步向导
6. [x] 危险管理页（预览 → 确认短语 → 可选备份 → 后台任务）
7. [x] 深色主题复测与截图基线
8. [ ] 窄窗口表格/表单自适应

主要功能已齐；后续主要是窄窗口响应式布局、角色形象拖拽上传、Windows/macOS 实机回归，以及用干净工作树对源码更新/回退做端到端演练。
