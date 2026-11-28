# KarenBot 后端协议接口新增报告（前端适配用）

> 适用版本：本轮协议扩展后的 `v0.1.11-alpha` 工作树
> 目标读者：WebUI / 控制台前端
> 相关文档：[前端接入契约](frontend-integration.md) · [OneBot 11](onebot11.md) · [Telegram](telegram.md)

---

## 0. 变更摘要

本轮新增/变更的 HTTP 接口：

| 方法 | 路径 | 类型 | 用途 |
| :--- | :--- | :--- | :--- |
| GET | `/api/adapters/protocols` | **新增** | 协议卡片 + 配置 JSON Schema |
| GET | `/api/adapters/config` | **新增** | 读取完整适配器配置（密钥只返回引用 ID） |
| PUT | `/api/adapters/{name}` | **新增** | 整份保存适配器配置并热重载 |
| GET | `/api/adapters/status` | **新增** | 当前主适配器状态 + 能力表 |
| GET | `/api/settings` | **扩展** | 新增 `transport/dialect/message_format/http/webhook` 字段 |
| PATCH | `/api/settings` | **扩展** | 支持新字段；旧 `mode` 自动同步 `transport` |
| PUT | `/api/settings/secrets` | **扩展** | 保存/删除密钥后，适配器 token 也会触发热重载 |
| GET/PUT | `/api/settings/lists` | **变更** | 黑白名单 ID 改为字符串；支持 Telegram 负数群 ID |
| GET/PUT | `/api/settings/group-triggers` | **变更** | `group_id` 支持负数（Telegram） |
| GET | `/api/status` | **扩展** | `adapter` 对象新增 `type/transport/dialect` |
| POST | `/api/chats/send` | **变更** | 错误文案、字符串 ID、Telegram 负数群 ID |

> 重要：当前仍是**单主适配器**。一个 Runtime 只启动第一个 `enabled: true` 的适配器；
> 配置多个会被忽略并记 warning。前端不要按“多实例并行”设计交互。

---

## 1. 鉴权与通用约定

### 1.1 鉴权

所有 `/api/**`（除 `/api/auth/*`）默认都需要鉴权。三种方式任选：

```http
Authorization: Bearer <webui_token>
# 或
GET /api/status?token=<webui_token>
# 或浏览器登录后的 HttpOnly Cookie: karen_webui_session
```

登录流程：

```http
GET /api/auth/state
→ {"auth_enabled": true, "authenticated": false, "boot_id": "..."}

POST /api/auth/login
Content-Type: application/json

{"token": "启动横幅里打印的令牌"}
→ 200 {"ok": true, "message": "登录成功"}
   Set-Cookie: karen_webui_session=...; HttpOnly; SameSite=Lax; Path=/
```

未授权时 HTTP 返回：

```json
{"detail": "未授权：请在页面上输入访问令牌", "auth_required": true}
```

WebSocket 鉴权认 Cookie 或 `?token=`。

### 1.2 Runtime 未就绪

依赖 Runtime 的接口（`/api/adapters/status`、`/api/adapters/config`、
`PUT /api/adapters/{name}`、`/api/settings`、`/api/chats/send` 等）在 Runtime
未启动时返回 **503**：

```json
{
  "detail": {
    "reason": "setup_required",
    "message": "Runtime 尚未就绪"
  }
}
```

`detail` 可能是字符串，也可能是对象；前端请同时兼容两种形态。

### 1.3 通用错误形态

FastAPI 默认错误：

```json
{"detail": "可直接展示的中文错误"}
```

`400` 通常是配置值不合法、路径不存在、适配器配置无法构造；
`401` 是鉴权失败；`409` 是渠道未连接；`500` 是保存后热重载/发送失败；
`503` 是 Runtime 未就绪。

---

## 2. 新增接口

### 2.1 `GET /api/adapters/protocols`

协议目录 + 配置 JSON Schema。**不依赖 Runtime**，页面初始化时即可调用。

#### 响应

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
    },
    {
      "id": "telegram",
      "name": "Telegram Bot API",
      "status": "beta",
      "summary": "海外与自托管首选……",
      "transports": ["long_polling", "webhook"],
      "dialects": [],
      "docs": "docs/protocols/telegram.md",
      "config_model": "TelegramAdapterConfig"
    }
  ],
  "config_schema": {
    "$defs": {
      "NapCatAdapterConfig": {"type": "object", "properties": {"...": {}}},
      "OneBot11AdapterConfig": {"type": "object", "properties": {"...": {}}},
      "TelegramAdapterConfig": {"type": "object", "properties": {"...": {}}},
      "WhitelistConfig": {"type": "object", "properties": {"...": {}}},
      "BlacklistConfig": {"type": "object", "properties": {"...": {}}}
    },
    "discriminator": {
      "mapping": {
        "napcat": "#/$defs/NapCatAdapterConfig",
        "onebot11": "#/$defs/OneBot11AdapterConfig",
        "telegram": "#/$defs/TelegramAdapterConfig"
      },
      "propertyName": "type"
    },
    "oneOf": [
      {"$ref": "#/$defs/OneBot11AdapterConfig"},
      {"$ref": "#/$defs/NapCatAdapterConfig"},
      {"$ref": "#/$defs/TelegramAdapterConfig"}
    ]
  },
  "frontend_doc": "docs/protocols/frontend-integration.md"
}
```

#### 字段说明

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `protocols[].id` | `string` | 配置里的 `type`：`napcat` / `onebot11` / `telegram` |
| `protocols[].status` | `stable` / `beta` / `planned` | `planned` 只展示，不允许保存 |
| `protocols[].summary` | `string` | 卡片一句话说明 |
| `protocols[].transports` | `string[]` | 该协议支持的接入方式 |
| `protocols[].dialects` | `string[]` | OneBot 11 的方言；其他协议为空 |
| `protocols[].docs` | `string` | 仓库内文档路径，可用于“查看文档”跳转 |
| `protocols[].config_model` | `string` | 对应 `config_schema.$defs` 的 key |
| `config_schema` | JSON Schema | 每个协议的完整配置模型，含 `type` 判别字段 |

### 2.1.1 `GET /api/adapters/docs/{protocol_id}`

读取协议目录登记的介绍文档，供 WebUI 与 GUI 在“渠道与协议”页面内展示。
响应只包含 `docs/protocols/` 下的 Markdown 文件，不接受任意文件路径：

```json
{
  "ok": true,
  "protocol": {"id": "napcat", "name": "OneBot 11 / NapCatQQ"},
  "path": "docs/protocols/onebot11.md",
  "markdown": "# OneBot 11 接入指南\\n..."
}
```

文档缺失、协议不存在或文档路径不在允许目录时返回 `404`。该接口不依赖
Runtime 已启动，但需要当前安装目录保留协议文档。

#### 前端用法

1. 用 `protocols` 渲染协议卡片/选择器；`status: planned` 禁用保存按钮。
2. 用户选中协议后，从 `config_schema.$defs[protocol.config_model]` 取该协议的
   字段定义，用 JSON Schema 表单渲染器生成表单。
3. Pydantic 的 `title` 是英文字段名；需要中文标签时，可复用
   `GET /api/settings` 里的 `label`，或按 `protocol.docs` 的字段表手工映射。
4. `config_schema` 里所有 `*_id` / `*_secret_*` 字段都是**密钥引用 ID**，
   不是密钥值；见 3.3。

---

### 2.2 `GET /api/adapters/config`

读取完整适配器配置，用于动态表单回填。**不返回密钥值**，只返回引用 ID。

```json
{
  "ok": true,
  "adapters": {
    "default": {
      "type": "telegram",
      "enabled": true,
      "whitelist": {"mode": "verify", "qq_ids": [], "group_ids": []},
      "blacklist": {"enabled": false, "qq_ids": [], "group_ids": []},
      "transport": "long_polling",
      "bot_token_id": "telegram_main_token",
      "api_base": "https://api.telegram.org",
      "proxy": "",
      "webhook_host": "127.0.0.1",
      "webhook_port": 8443,
      "webhook_path": "/telegram",
      "webhook_public_url": "",
      "webhook_secret_token_id": null,
      "parse_mode": "HTML",
      "allowed_updates": [],
      "drop_pending_updates": true,
      "polling_timeout_seconds": 30.0,
      "api_timeout_seconds": 30.0,
      "reconnect_interval_seconds": 3.0
    }
  }
}
```

注意：

- 不同 `type` 的字段不同；OneBot 配置会有 `host/port/path/access_token_id/...`，
  Telegram 没有这些字段。前端按 `type` 选择表单。
- `model_dump()` 会包含 `whitelist` / `blacklist`；黑白名单仍建议走
  `/api/settings/lists` 独立保存。
- 密钥字段（`access_token_id`、`webhook_secret_id`、`bot_token_id`、
  `webhook_secret_token_id`）只是引用 ID，真实值在 `SecretsManager`。

---

### 2.3 `PUT /api/adapters/{name}`

整份保存一个**已存在**的适配器配置。前端动态表单的保存入口。

#### 请求

`name` 必须是 `GET /api/adapters/config` 里已存在的 key（通常是 `default`）。
新增第二个适配器当前会被拒绝。

```json
{
  "type": "telegram",
  "enabled": true,
  "transport": "long_polling",
  "bot_token_id": "telegram_main_token",
  "parse_mode": "HTML",
  "whitelist": {"mode": "verify", "qq_ids": [], "group_ids": []},
  "blacklist": {"enabled": false, "qq_ids": [], "group_ids": []}
}
```

请求体就是该协议的完整配置，字段以 `config_schema` 为准。缺失 `type` 时后端按
历史默认值补成 `"napcat"`。

#### 保存前校验

后端会先用这份配置**构造一次适配器**（不启动连接）。以下错误直接返回 400，
不会写盘：

- `type` 不认识 / 字段值非法；
- 引用的 token/secret 在 `SecretsManager` 中不存在；
- OneBot `ws_server` 监听非回环地址但没有 token；
- Webhook 监听非回环地址但没有 secret；
- 引用的密钥看起来像 token 本身而不是引用 ID。

#### 响应

```json
{
  "ok": true,
  "adapter": {"type": "telegram", "enabled": true, "transport": "long_polling", "...": "..."},
  "needs_restart": false,
  "hot_applied": true,
  "reload": {
    "hot_applied": true,
    "rebuilt": ["adapter"],
    "errors": []
  },
  "message": "适配器配置已保存并热重载"
}
```

如果保存成功但热重载失败：

```json
{
  "ok": true,
  "adapter": {"...": "..."},
  "needs_restart": true,
  "hot_applied": false,
  "reload": {
    "hot_applied": false,
    "rebuilt": ["config"],
    "errors": ["适配器：..."],
    "message": "..."
  },
  "message": "适配器配置已保存；热重载失败，请重启 Runtime"
}
```

前端应：

1. `needs_restart === true` 时显示“需要重启 Runtime”；
2. 展示 `reload.errors`（数组）供排查；
3. 重新拉 `GET /api/adapters/status` 刷新连接状态与能力表。

---

### 2.4 `GET /api/adapters/status`

当前主适配器的运行状态、能力表与脱敏配置摘要。依赖 Runtime。

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
      "webhook_host": "127.0.0.1",
      "webhook_port": 5700,
      "webhook_path": "/onebot11",
      "access_token_id": "napcat_default_token",
      "webhook_secret_id": null,
      "bot_token_id": null,
      "api_base": null,
      "parse_mode": null,
      "webhook_public_url": null,
      "webhook_secret_token_id": null,
      "polling_timeout_seconds": null
    }
  }
}
```

说明：

- `adapter === null` 表示当前没有运行中的适配器（全部 `enabled: false`）。
- `config` 是配置摘要，不是完整配置；完整配置用 `GET /api/adapters/config`。
- 不适用的字段返回 `null`。例如 Telegram 配置下 `host/port/path` 为 `null`，
  OneBot 配置下 `bot_token_id/api_base/parse_mode` 为 `null`。
- `capabilities` 用于前端显隐按钮/工具，不要自己按协议类型硬编码。

---

## 3. 变更接口

### 3.1 `GET /api/settings`（新增渠道字段）

原有结构不变：

```json
{
  "groups": [
    {
      "key": "adapter",
      "title": "渠道",
      "desc": "当前支持 OneBot 11……",
      "fields": [
        {
          "path": "adapters.default.transport",
          "label": "接入方式",
          "type": "enum",
          "options": [
            ["ws_client", "WebSocket 客户端（Karen 连协议端）"],
            ["ws_server", "WebSocket 服务端（协议端连 Karen）"],
            ["http", "HTTP API + Webhook"]
          ],
          "default": "ws_client",
          "needs_restart": false,
          "help": "旧配置的 mode=client/server 会自动映射到 ws_client/ws_server。",
          "value": "ws_client"
        }
      ]
    }
  ],
  "paths": {"DATA_DIR": "...", "CONFIG_FILE": "..."},
  "secrets_index": ["napcat_default_token", "telegram_main_token"]
}
```

本轮新增的渠道字段：

| path | type | default | 说明 |
| :--- | :--- | :--- | :--- |
| `adapters.default.transport` | enum | `ws_client` | `ws_client` / `ws_server` / `http` |
| `adapters.default.dialect` | enum | `auto` | `auto/napcat/llonebot/lagrange/gocqhttp/generic` |
| `adapters.default.message_format` | enum | `array` | `array` / `string` |
| `adapters.default.http_api_url` | str | `""` | HTTP API 出站地址 |
| `adapters.default.webhook_enabled` | bool | `false` | HTTP 模式默认启用 |
| `adapters.default.webhook_host` | str | `127.0.0.1` | 非回环必须配 secret |
| `adapters.default.webhook_port` | int | `5700` | |
| `adapters.default.webhook_path` | str | `/onebot11` | |
| `adapters.default.webhook_secret_id` | secret_ref | `""` | HMAC 密钥引用 |
| `adapters.default.access_token_id` | secret_ref | `""` | WS/HTTP token 引用 |

注意：

- 渠道字段的 `needs_restart` 在响应里是 `false`（会热重载适配器）；如果热重载
  失败，`PATCH` 响应会返回 `needs_restart: true`。
- 这张固定字段表仍只覆盖 `adapters.default.*` 的 OneBot 字段。
  **Telegram 请使用 2.2/2.3 的完整适配器配置接口**，不要指望
  `/api/settings` 里出现 `bot_token_id/api_base/parse_mode`。

### 3.2 `PATCH /api/settings`

请求：

```json
{"path": "adapters.default.transport", "value": "http"}
```

响应：

```json
{
  "ok": true,
  "path": "adapters.default.transport",
  "value": "http",
  "needs_restart": false,
  "hot_applied": true,
  "reload": {
    "hot_applied": true,
    "rebuilt": ["config", "adapter"],
    "errors": []
  },
  "message": "已保存并立即生效"
}
```

兼容行为：

- 旧前端 PATCH `adapters.default.mode = "client" | "server"` 时，后端会同时写
  `transport = ws_client | ws_server`，不会出现“界面显示 server、实际仍是 client”。
- `path` 必须在 `field_map` 中；任意 `adapters.<name>.<field>` 不保证可写。
  新协议动态表单请用 `PUT /api/adapters/{name}`。
- 错误：`400` 配置值不合法/路径不存在；`503` Runtime 未就绪。

### 3.3 `PUT /api/settings/secrets`

保存或删除一个密钥；密钥只进 `secrets.enc`，不写 `config.yaml`。

```json
{"key_id": "telegram_main_token", "value": "123456:ABC..."}
```

`value` 留空表示删除：

```json
{"key_id": "telegram_main_token", "value": ""}
```

响应：

```json
{
  "ok": true,
  "deleted": false,
  "hot_applied": true,
  "needs_restart": false,
  "message": "密钥已保存，已应用到运行中的服务"
}
```

本轮扩展：如果该 `key_id` 被适配器的 `access_token_id` / `webhook_secret_id` /
`bot_token_id` / `webhook_secret_token_id` 引用，保存后会自动触发热重载。

前端保存新协议配置的顺序：

1. `PUT /api/settings/secrets` 保存 token/secret，得到 `key_id`；
2. `PUT /api/adapters/{name}` 保存完整配置，字段填 `*_id = key_id`；
3. 根据响应处理 `needs_restart` / `reload.errors`。

### 3.4 `GET/PUT /api/settings/lists`

黑白名单统一入口。

#### GET 响应

```json
{
  "ok": true,
  "hot_applied": true,
  "whitelist": {
    "mode": "verify",
    "qq_ids": [],
    "group_ids": [],
    "active_qq_ids": [],
    "active_group_ids": []
  },
  "blacklist": {
    "enabled": false,
    "qq_ids": [],
    "group_ids": [],
    "active_qq_ids": [],
    "active_group_ids": []
  }
}
```

#### PUT 请求

```json
{
  "whitelist": {
    "mode": "whitelist",
    "qq_ids": ["123456"],
    "group_ids": ["-1001234567890"]
  },
  "blacklist": {
    "enabled": true,
    "qq_ids": [],
    "group_ids": []
  }
}
```

#### 变更点（重要）

- `qq_ids` 仍是字段名，但语义已扩展为**通用用户 ID**；QQ 是数字字符串，
  Telegram 是数字用户 ID。
- 所有 ID 统一字符串：请求可以传 `int` 或 `string`，响应一律字符串。
- 允许负数：Telegram 群/频道 ID 是负数（如 `-1001234567890`）。
- `0` 会被丢弃；输出会排序去重。
- `qq_ids` 的 `user_ids` 别名只适用于 `config.yaml` / 适配器配置 Schema；
  **这个 PUT 请求体目前只接受 `qq_ids`**，不要传 `user_ids`。
- 旧的 `/api/settings/blacklist` GET/PUT 仍保留，但建议新前端统一用
  `/api/settings/lists`。

### 3.5 `GET/PUT /api/settings/group-triggers`

群聊 @ 触发规则。

#### GET 响应

```json
{
  "ok": true,
  "enabled": true,
  "groups": [
    {
      "group_id": -1001234567890,
      "max_messages": 20,
      "window_minutes": 10,
      "cache_messages": 50
    }
  ],
  "active_windows": [],
  "active_llm_groups": [],
  "max_llm_groups": 2,
  "hot_applied": true
}
```

#### PUT 请求

```json
{
  "enabled": true,
  "groups": [
    {
      "group_id": -1001234567890,
      "max_messages": 20,
      "window_minutes": 10,
      "cache_messages": 50
    }
  ]
}
```

变更点：`group_id` 现在允许负数（Telegram 群），仍不允许 `0`。响应为
`{ok, needs_restart:false, message, ...}`。

### 3.6 `GET /api/status`

原有字段不变，`adapter` 对象新增/明确以下字段：

```json
{
  "state": "running",
  "last_error": "",
  "warnings": [],
  "config_file": "/path/config.yaml",
  "data_dir": "/path/data",
  "uptime_seconds": 123.4,
  "adapter_connected": true,
  "adapter": {
    "name": "default",
    "type": "napcat",
    "transport": "ws_client",
    "dialect": "auto",
    "mode": "client",
    "host": "127.0.0.1",
    "port": 3001,
    "path": "/",
    "enabled": true
  },
  "persona": "大肥鱼",
  "persona_id": "大肥鱼",
  "setup_required": false
}
```

Telegram 配置下 `mode/host/port/path` 可能为 `null`，`dialect` 为 `null`；
前端顶栏只应展示 `type` + `transport` + `adapter_connected`，不要强行读 OneBot 字段。

### 3.7 `POST /api/chats/send`

直接用机器人身份发一条纯文本（不过模型），用于连通性验证。

```json
{
  "target_id": "-1001234567890",
  "text": "测试消息",
  "scope": "group"
}
```

响应：

```json
{"ok": true, "message_id": "123456"}
```

错误：

| 状态码 | 场景 |
| :--- | :--- |
| 503 | Runtime 未就绪 |
| 409 | 渠道未连接（文案已改为“渠道未连接，无法发送”） |
| 500 | 发送异常，`detail` 为“发送失败：...” |

`target_id` 为字符串；Telegram 群/频道填负数 chat_id，私聊填用户 ID。

---

## 4. 前端适配建议

### 4.1 协议选择卡片

1. `GET /api/adapters/protocols`。
2. 按 `status` 渲染徽标：`stable` / `beta` / `planned`。
3. `planned` 卡片只展示说明和“即将支持”，禁用保存。
4. 卡片文案直接用 `summary`，不要在前端硬编码协议说明。

### 4.2 动态配置表单

1. 选择协议后，取 `config_schema.$defs[protocol.config_model]`。
2. 用 JSON Schema 渲染表单；`type` 字段固定为当前协议，不要给用户编辑。
3. `*_id` / `*_secret_*` 字段渲染成“密钥选择/新建”，不要当普通输入框。
4. 保存前先确保密钥存在：`PUT /api/settings/secrets`。
5. 保存用 `PUT /api/adapters/{name}`，不要用 `PATCH /api/settings` 拼路径。

### 4.3 能力驱动 UI

用 `GET /api/adapters/status` 的 `capabilities`：

| 能力 | 前端显隐 |
| :--- | :--- |
| `send_image` | 图片按钮/表情包选择 |
| `send_voice` | 语音按钮/TTS 发语音 |
| `upload_file` | 文件上传 |
| `recall` | 撤回按钮 |
| `poke` | 戳一戳 |
| `emoji_reaction` | 表情回应 |
| `voice_stt` | 语音转文字开关 |
| `forward_read` | 合并转发展开 |
| `group_history` | 拉取群历史 |
| `get_message` | 消息详情 |
| `group_admin` | 群管理页 |
| `friend_request` / `group_request` | 审批面板 |
| `proactive_private` / `proactive_group` | 主动消息开关 |

不要用 `type === "napcat"` 来判断按钮是否显示。

### 4.4 状态轮询

- 连接状态：轮询 `GET /api/adapters/status`（建议 5–10 秒）。
- Runtime 状态：轮询 `GET /api/status`，用 `adapter_connected` + `state`。
- 保存配置后立即重新拉一次状态，不要只依赖本地表单状态。

### 4.5 保存失败处理

- `400`：展示 `detail`，保留用户表单内容。
- `200 + needs_restart: true`：提示“已保存，但需要重启 Runtime”，并展示
  `reload.errors`。
- `500`：提示“已保存但热重载失败”，展示 `detail`，引导检查日志/重启。
- `503`：跳首次配置页或提示 Runtime 未就绪。

---

## 5. TypeScript 类型参考

```ts
export type AdapterType = "napcat" | "onebot11" | "telegram";
export type ProtocolStatus = "stable" | "beta" | "planned";

export interface ProtocolCard {
  id: AdapterType | string;
  name: string;
  status: ProtocolStatus;
  summary: string;
  transports: string[];
  dialects: string[];
  docs: string;
  config_model: string;
}

export interface ProtocolListResponse {
  ok: true;
  protocols: ProtocolCard[];
  config_schema: Record<string, unknown>;
  frontend_doc: string;
}

export interface AdapterCapabilities {
  send_image: boolean;
  send_voice: boolean;
  send_video: boolean;
  upload_file: boolean;
  recall: boolean;
  reply: boolean;
  mention: boolean;
  poke: boolean;
  emoji_reaction: boolean;
  voice_stt: boolean;
  forward_read: boolean;
  group_history: boolean;
  get_message: boolean;
  list_friends: boolean;
  list_groups: boolean;
  list_group_members: boolean;
  friend_request: boolean;
  group_request: boolean;
  group_admin: boolean;
  proactive_private: boolean;
  proactive_group: boolean;
}

export interface AdapterRuntimeStatus {
  name: string;
  class: string;
  connected: boolean;
  dialect: string | null;
  dialect_display_name: string | null;
  preferred_voice_format: string | null;
  capabilities: AdapterCapabilities;
}

export interface AdapterConfigSummary {
  type: AdapterType;
  enabled: boolean;
  transport?: string | null;
  dialect?: string | null;
  message_format?: string | null;
  host?: string | null;
  port?: number | null;
  path?: string | null;
  http_api_url?: string | null;
  webhook_enabled?: boolean | null;
  webhook_host?: string | null;
  webhook_port?: number | null;
  webhook_path?: string | null;
  access_token_id?: string | null;
  webhook_secret_id?: string | null;
  bot_token_id?: string | null;
  api_base?: string | null;
  parse_mode?: string | null;
  webhook_public_url?: string | null;
  webhook_secret_token_id?: string | null;
  polling_timeout_seconds?: number | null;
}

export interface AdapterStatusResponse {
  ok: true;
  adapter: AdapterRuntimeStatus | null;
  config: Record<string, AdapterConfigSummary>;
}

export interface SettingField {
  path: string;
  label: string;
  type: "bool" | "int" | "float" | "str" | "enum" | "secret_ref" | "list_int" | "list_str";
  default: unknown;
  value: unknown;
  needs_restart: boolean;
  options?: [string, string][];
  help?: string;
  min?: number;
  max?: number;
  secret_id?: string;
}

export interface SettingsResponse {
  groups: Array<{
    key: string;
    title: string;
    desc: string;
    fields: SettingField[];
  }>;
  paths: Record<string, string>;
  secrets_index: string[];
}

export interface SettingPatchResponse {
  ok: true;
  path: string;
  value: unknown;
  needs_restart: boolean;
  hot_applied: boolean;
  reload: {
    hot_applied: boolean;
    rebuilt: string[];
    errors: string[];
    message?: string;
  };
  message: string;
}

export interface ListsResponse {
  ok: true;
  hot_applied?: boolean;
  needs_restart?: boolean;
  message?: string;
  whitelist: {
    mode: "open" | "verify" | "whitelist";
    qq_ids: string[];
    group_ids: string[];
    active_qq_ids: string[];
    active_group_ids: string[];
  };
  blacklist: {
    enabled: boolean;
    qq_ids: string[];
    group_ids: string[];
    active_qq_ids: string[];
    active_group_ids: string[];
  };
}

export interface GroupTriggerRule {
  group_id: number;
  max_messages: number;
  window_minutes: number;
  cache_messages: number;
}

export interface GroupTriggersResponse {
  ok: true;
  enabled: boolean;
  groups: GroupTriggerRule[];
  active_windows: unknown[];
  active_llm_groups: number[];
  max_llm_groups: number;
  hot_applied?: boolean;
  needs_restart?: boolean;
  message?: string;
}

export interface ChatSendResponse {
  ok: true;
  message_id: string;
}
```

---

## 6. 兼容性与破坏性变更

1. **`ws_server` 监听地址**
   - `host: 127.0.0.1` 现在只监听本机，不再自动改 `0.0.0.0`。
   - 跨设备反向 WS 必须显式 `host: 0.0.0.0` 且配置 `access_token_id`。
2. **Webhook 监听地址**
   - 默认 `127.0.0.1`；`0.0.0.0` 等非回环地址必须配置 secret，否则 400/启动失败。
3. **密钥引用**
   - `access_token_id` / `webhook_secret_id` / `bot_token_id` /
     `webhook_secret_token_id` 引用的密钥不存在时拒绝启动，不再静默无鉴权。
4. **ID 类型**
   - 黑白名单、Telegram 用户/群 ID 全部按字符串处理；群里负数 ID 合法。
5. **单适配器**
   - `PUT /api/adapters/{name}` 只允许已存在的 key；不要把多协议并行的 UI 做出来。
6. **计划中协议**
   - `qq_official`、`satori` 目前只是目录项；`PUT /api/adapters/default` 传它们的
     `type` 会返回 400。

---

## 7. 相关文件

- 后端实现：`backend/api/adapters.py`、`backend/api/settings.py`
- 协议目录：`adapters/catalog.py`
- 配置模型：`app_config/schema.py`
- 前端契约：`docs/protocols/frontend-integration.md`
- OneBot 11：`docs/protocols/onebot11.md`
- Telegram：`docs/protocols/telegram.md`
