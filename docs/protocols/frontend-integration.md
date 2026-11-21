# 前端控制台接入契约

> 本轮新增接口的逐项请求/响应说明见
> [backend-api-report.md](backend-api-report.md)。

本文说明控制台（WebUI / GUI / 未来的协议管理页）如何读取协议信息、
渲染配置表单、展示连接状态与能力。

所有接口都在现有认证体系内，路径前缀与 WebUI 其他 API 一致。

## 1. 协议目录

```http
GET /api/adapters/protocols
```

响应：

```json
{
  "ok": true,
  "protocols": [
    {
      "id": "napcat",
      "name": "OneBot 11 / NapCatQQ",
      "status": "stable",
      "summary": "QQ 个人号的现有主通道……",
      "transports": ["ws_client", "ws_server", "http"],
      "dialects": ["auto", "napcat", "llonebot", "lagrange", "gocqhttp", "generic"],
      "docs": "docs/protocols/onebot11.md",
      "config_model": "NapCatAdapterConfig"
    }
  ],
  "config_schema": { "$defs": {}, "discriminator": {}, "oneOf": [] },
  "frontend_doc": "docs/protocols/frontend-integration.md"
}
```

- `protocols` 用于渲染协议卡片/选择器；`status` 为 `stable` / `beta` / `planned`。
- `config_schema` 是 `AdapterConfig` 的 Pydantic JSON Schema，含
  `discriminator.propertyName = "type"`，当前包含 `napcat` / `onebot11` /
  `telegram` 三种配置模型。前端可以直接用
  [JSON Forms](https://jsonforms.io/) / 自研渲染器生成表单。
- `status: beta`（当前 Telegram）可以允许保存，但应在卡片上标注「Beta」。
- `status: planned` 的协议只展示说明与「即将支持」，不要允许保存配置。

## 2. 适配器完整配置（动态表单用）

```http
GET /api/adapters/config
PUT /api/adapters/{name}
```

`GET` 返回每个适配器的完整配置（包含 `type` 与密钥引用 ID，不包含密钥值）：

```json
{
  "ok": true,
  "adapters": {
    "default": {
      "type": "telegram",
      "enabled": true,
      "transport": "long_polling",
      "bot_token_id": "telegram_main_token",
      "parse_mode": "HTML"
    }
  }
}
```

`PUT` 用整份配置替换已存在的适配器，并在保存前做可构造性校验（缺 token、
非回环无鉴权等直接返回 400），成功后触发热重载：

```json
{
  "ok": true,
  "adapter": {"type": "telegram", "enabled": true, "...": "..."},
  "needs_restart": false,
  "hot_applied": true,
  "reload": {"hot_applied": true, "rebuilt": ["adapter"], "errors": []},
  "message": "适配器配置已保存并热重载"
}
```

当前仍是单主适配器：`name` 必须是配置里已存在的 key（通常是 `default`）。

## 3. 当前适配器状态

```http
GET /api/adapters/status
```

响应：

```json
{
  "ok": true,
  "adapter": {
    "name": "default",
    "class": "OneBot11Adapter",
    "connected": true,
    "dialect": "napcat",
    "dialect_display_name": "NapCatQQ",
    "preferred_voice_format": "mp3",
    "capabilities": {
      "send_image": true,
      "send_voice": true,
      "send_video": true,
      "upload_file": true,
      "recall": true,
      "reply": true,
      "mention": true,
      "poke": true,
      "emoji_reaction": true,
      "voice_stt": true,
      "forward_read": true,
      "group_history": true,
      "get_message": true,
      "list_friends": true,
      "list_groups": true,
      "list_group_members": true,
      "friend_request": true,
      "group_request": true,
      "group_admin": true,
      "proactive_private": true,
      "proactive_group": true
    }
  },
  "config": {
    "default": {
      "type": "napcat",
      "enabled": true,
      "transport": "ws_client",
      "dialect": "auto",
      "message_format": "array",
      "host": "127.0.0.1",
      "port": 3001,
      "path": "/",
      "http_api_url": "",
      "webhook_enabled": false,
      "webhook_host": "0.0.0.0",
      "webhook_port": 5700,
      "webhook_path": "/onebot11",
      "access_token_id": "napcat_default_token",
      "webhook_secret_id": null
    }
  }
}
```

`config` 已脱敏：只返回密钥引用 ID，不返回密钥值。

## 4. 能力 → 前端显隐

控制台不需要自己判断协议差异，直接用 `capabilities`：

| 能力字段 | 控制的 UI/功能 |
| :--- | :--- |
| `send_image` | 图片发送按钮、表情包选择器 |
| `send_voice` | 语音发送按钮、TTS「发语音」开关 |
| `send_video` | 视频发送 |
| `upload_file` | 文件上传 |
| `recall` | 撤回按钮 |
| `poke` | 戳一戳按钮/工具说明 |
| `emoji_reaction` | 表情回应 |
| `voice_stt` | 语音转文字开关 |
| `forward_read` | 合并转发展开 |
| `group_history` | 「拉取群历史」按钮 |
| `get_message` | 消息详情 |
| `group_admin` | 群管理页（踢/禁言/退群） |
| `friend_request` / `group_request` | 审批面板 |
| `proactive_private` / `proactive_group` | 主动消息开关 |

后端 `build_default_registry` 已经按能力过滤工具；前端隐藏对应按钮即可，
二者保持一致。

## 5. 配置读写（固定字段表）

### 读

```http
GET /api/settings
```

返回 `groups[].fields[]`，每项包含：

```json
{
  "path": "adapters.default.transport",
  "label": "接入方式",
  "type": "enum",
  "options": [["ws_client", "WebSocket 客户端（Karen 连协议端）"]],
  "default": "ws_client",
  "needs_restart": false,
  "help": "旧配置的 mode=client/server 会自动映射……",
  "value": "ws_client"
}
```

> 注意：当前 `/api/settings` 的声明式字段表仍以 OneBot 11 为主。
> Telegram 等新协议请使用 `GET /api/adapters/config` 读取完整配置，再用
> `PUT /api/adapters/{name}` 保存；字段定义见 `config_schema`。

### 写单个字段

```http
PATCH /api/settings
Content-Type: application/json

{"path": "adapters.default.transport", "value": "http"}
```

- `needs_restart=true`：保存后提示用户重启 Runtime。
- `needs_restart=false`：后端尝试热应用。
- 配置校验失败返回 `400`，`detail` 是可直接展示的中文错误。

### 外部程序

```http
GET  /api/settings/programs
PUT  /api/settings/programs
POST /api/settings/programs/{id}/start
POST /api/settings/programs/{id}/stop
```

`GET` 返回 `{ok, programs: [...], status: [...], defaults}`；`programs` 是配置，
`status` 是运行状态（`state/pid/managed/error/failure_seq/...`）。`PUT` 接受
`{"programs": [...]}`（每项带稳定 `id`；缺省时后端生成），保存后立即热应用：
新增/启用启动，删除/禁用停止由 Karen 拉起的进程。

`POST .../start` 的启动失败是运行事件，仍返回 HTTP 200：
`{"ok": true, "started": false, "program": {"state": "failed", "error": "..."}}`。
前端应结合 `/api/status` 的 `programs[].failure_seq` 做一次性提醒；不要把
外部程序失败写进 `last_error`/`warnings`。

### 黑白名单

```http
GET  /api/settings/lists
PUT  /api/settings/lists
```

ID 统一字符串，`qq_ids` / `group_ids` 同时接受数字与字符串输入（后端会规范化）。

## 6. 配置文件形状

```json
{
  "adapters": {
    "default": {
      "type": "napcat",
      "enabled": true,
      "transport": "ws_client",
      "dialect": "auto",
      "message_format": "array",
      "host": "127.0.0.1",
      "port": 3001,
      "path": "/",
      "access_token_id": "napcat_default_token",
      "whitelist": {"mode": "verify", "qq_ids": [], "group_ids": []},
      "blacklist": {"enabled": false, "qq_ids": [], "group_ids": []}
    }
  }
}
```

`type` 是判别字段，当前允许：

- `napcat`
- `onebot11`
- `telegram`

未来加入 `qq_official`、`satori` 后，同一位置扩展即可，
不需要改 `adapters` 的字典结构。

## 7. 兼容规则

| 旧写法 | 新写法 | 处理 |
| :--- | :--- | :--- |
| `type: napcat` + `mode: client` | `type: napcat` + `transport: ws_client` | 加载时自动映射 |
| `type: napcat` + `mode: server` | `type: napcat` + `transport: ws_server` | 加载时自动映射 |
| `whitelist.qq_ids: [123]` | `qq_ids: ["123"]` | 校验器自动转字符串 |
| 只读 `adapters.default.*` | 仍是 `default` 键 | 单主适配器，键名不变 |

前端如果暂时不区分协议，继续读写 `adapters.default.*` 即可；
新增协议时再按 `type` 切换表单。

## 8. 错误与状态码

| 场景 | 状态码 | 前端处理 |
| :--- | :--- | :--- |
| 协议未实现（`status: planned`） | 前端阻止保存 | 显示「即将支持」 |
| 配置值非法 | `400` | 展示 `detail` |
| Runtime 未就绪 | `503` | 提示稍后重试 |
| 渠道未连接 | `409`（发送接口） | 提示检查协议端 |
| 适配器启动失败 | Runtime 日志 + `connected=false` | 展示排障文档链接 |

## 9. 新增协议的落地清单

后端：

1. 在 `app_config/schema.py` 新增 `XxxAdapterConfig`，加入 `AdapterConfig` 联合体；
2. 在 `adapters/xxx/` 实现 `IAdapter`，在 `__init__.py` 调
   `register_adapter_type("xxx", XxxAdapter.__init__ 的 from_config)`；
3. 在 `adapters/catalog.py` 登记协议卡片；
4. 在 `docs/protocols/xxx.md` 写清用途、配置、映射与限制；
5. 补契约测试与单测。

前端：

1. 用 `GET /api/adapters/protocols` 自动出现新卡片；
2. 用 `config_schema` 自动生成表单；
3. 用 `capabilities` 控制按钮显隐；
4. 不需要为新协议写专属页面，除非需要特殊引导（如 QQ 官方的公网回调说明）。
