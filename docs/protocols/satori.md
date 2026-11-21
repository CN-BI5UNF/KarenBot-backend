# Satori v1（计划中）

> 状态：**计划中**。本文定义用途、配置契约与协议映射，供前端控制台提前接入；
> 适配器实现尚未提交。

## 1. 它是做什么的

Satori 是一个通用聊天机器人协议。你运行一个 Satori 服务端（例如 Koishi 的
`@koishijs/plugin-satori` 或 onebots），它负责连接 Discord、Telegram、飞书、
WhatsApp 等平台；KarenBot 只实现一个 Satori 适配器，就能同时接入这些平台。

**优点**：一个适配器覆盖十余个平台，协议稳定，社区维护。

**代价**：你必须自己部署并维护 Satori 服务端；平台特有的交互（Discord
Slash Command、Telegram 贴纸包等）在 Satori 层会被抹平或丢失。

适合：同时运营多个海外平台、愿意自建桥接服务的部署。

## 2. 前置条件

1. 部署 Satori 服务端（Koishi + satori 插件，或 onebots）；
2. 在 Satori 侧配置好各平台账号；
3. 拿到 Satori 的 WebSocket 地址、HTTP API 地址和访问令牌；
4. 把令牌存进 `SecretsManager`。

## 3. 计划配置字段

```yaml
adapters:
  satori:
    type: satori
    enabled: true
    api_base: http://127.0.0.1:5140     # Satori HTTP API
    ws_url: ws://127.0.0.1:5140/v1/events
    token_id: satori_main_token         # SecretsManager 引用 ID
    platform: discord                   # 可选：只接收某个平台的账号事件
    self_id: ""                         # 可选：只接收某个机器人账号
    reconnect_interval_seconds: 3.0
    max_reconnect_attempts: -1
    api_timeout_seconds: 30.0

    whitelist:
      mode: verify
      user_ids: []                      # Satori user.id
      group_ids: []                     # Satori channel.id / guild.id
    blacklist:
      enabled: false
      user_ids: []
      group_ids: []
```

## 4. 协议映射

### 连接与事件

- WebSocket：`GET ws_url`，事件是 JSON，带递增 `sn` 序列号；断线后可用
  `sn` 做增量恢复（首版可全量重连）。
- HTTP API：`POST {api_base}/{method}`，`Authorization: Bearer <token>`，
  body 为 JSON 参数，返回 `{code, data}`。
- 事件类型：`message-created`、`message-deleted`、`message-updated`、
  `guild-added`、`guild-removed`、`guild-member-added` 等。

### 消息内容

Satori 的消息是 **XML 元素字符串**，例如：

```xml
<at id="123"/> 你好 <img src="https://..."/> <quote id="456"/>
```

映射到内部 IR：

| Satori 元素 | 内部 IR |
| :--- | :--- |
| 纯文本 | `TextSegment` |
| `<at id="..."/>` | `MentionSegment` |
| `<quote id="..."/>` | `ReplySegment` |
| `<img src="..."/>` | `ImageSegment` |
| `<audio src="..."/>` | `VoiceSegment` |
| `<video src="..."/>` | `VideoSegment` |
| `<file src="..."/>` | `FileSegment` |
| `<face id="..."/>` | `FaceSegment` |
| `<br/>` | `TextSegment("\n")` |
| 其他 | `RawSegment` + 可读占位 |

出站时反向编码；文本必须先做 XML 转义（`& < > " '`），否则会被解析成标签。

### 会话映射

| Satori 字段 | 内部字段 |
| :--- | :--- |
| `channel.id` | `Target.target_id` |
| `guild.id` | `Target.guild_id` |
| `channel.type == DIRECT` | `scope="private"` |
| `channel.type == TEXT/VOICE` | `scope="group"` |
| `user.id` | `IncomingMessage.user_id` |
| `message.id` | `message_id` |
| `message.content` | `raw_message` + `segments` |

## 5. 能力表（预计）

| 能力 | 支持 | 说明 |
| :--- | :---: | :--- |
| 发送文本/图片 | ✅ | 取决于底层平台 |
| 发送语音/视频/文件 | ⚠️ | 取决于底层平台资源上传能力 |
| 撤回 | ✅ | `message.delete` |
| 引用 | ✅ | `<quote>` |
| @ 提及 | ✅ | `<at>` |
| 表情回应 | ✅ | `reaction.create/delete` |
| 戳一戳 | ❌ | 协议无此语义 |
| 语音转文字 | ❌ | |
| 合并转发 | ❌ | |
| 群历史 | ❌ | Satori 不提供历史拉取 |
| 群管理 | ❌ | 首版不做 |
| 好友/群列表 | ⚠️ | `guild.list` 可用；好友列表视平台而定 |
| 主动私聊/群聊 | ✅ | 受底层平台限制 |

## 6. 已知限制

- **XML 转义**：用户输入里的 `<`、`&` 必须转义，否则会被当成标签。
- **能力不透明**：Satori 不会告诉你底层平台支持什么；首版按保守能力运行，
  失败时降级。
- **平台特性丢失**：Discord Embed、Telegram 贴纸、飞书卡片等只能走
  `RawSegment` 透传。
- **运维成本**：Satori 服务端需要单独部署、升级、排障。
- **多账号**：一个 Satori 服务端可能挂多个平台账号；用 `platform` + `self_id`
  过滤，避免串线。

## 7. 前端表单建议

1. **连接**：`api_base`、`ws_url`、`token_id`（secret_ref）
2. **账号过滤**：`platform`、`self_id`
3. **重连**：`reconnect_interval_seconds`、`max_reconnect_attempts`、
   `api_timeout_seconds`
4. **过滤**：黑白名单（字符串 ID）

卡片文案建议明确写出：「需要你自行部署 Satori/Koishi 服务端；KarenBot
只负责协议客户端」。
