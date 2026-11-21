# Telegram Bot API（计划中）

> 状态：**Beta**。已实现长轮询与 Webhook 两种接入方式、文本/图片/语音/视频/文件、
> 引用回复、表情回应、撤回、topic/thread_id、媒体下载与 UTF-16 实体解析；
> 已通过本地 Mock 测试，尚未在真实 Telegram 环境做长期回归。

## 1. 它是做什么的

Telegram Bot API 是 KarenBot 接入海外用户、自托管社群的最低成本通道：

- 不需要公网 IP（Long Polling 模式）；
- 不需要自建协议端，一个 Bot Token 即可；
- 原生支持语音条（OGG/Opus）、贴纸、引用回复、消息表情回应；
- 适合作为第一个非 QQ 协议，用来验证多协议抽象是否真正「不泄漏 QQ 假设」。

## 2. 前置条件

1. 在 Telegram 里找 [@BotFather](https://t.me/BotFather) 创建机器人，拿到 Bot Token；
2. 把 Token 存进 KarenBot 的 `SecretsManager`，配置里只引用 ID；
3. 群聊使用时，把机器人拉进群，并决定是否关闭 BotFather 的 Privacy Mode
   （关闭后才能读取群内所有消息，否则只能收到 @ 和命令）。

> Webhook 默认只监听 `127.0.0.1`；如果直接监听 `0.0.0.0`，必须配置
> `webhook_secret_token_id`，否则适配器拒绝启动。

## 3. 配置字段

```yaml
adapters:
  telegram:
    type: telegram
    enabled: true
    transport: long_polling        # long_polling | webhook
    bot_token_id: telegram_main_token   # SecretsManager 引用 ID
    api_base: https://api.telegram.org  # 可指向自建 Bot API Server
    proxy: ""                     # 例如 socks5://127.0.0.1:1080

    # webhook 模式（transport=webhook）
    webhook_host: 127.0.0.1
    webhook_port: 8443
    webhook_path: /telegram
    webhook_public_url: ""        # 填写后启动时自动 setWebhook
    webhook_secret_token_id: telegram_webhook_secret

    # 行为
    parse_mode: HTML              # HTML | MarkdownV2 | none
    allowed_updates: []           # 空 = 使用推荐集合
    drop_pending_updates: true

    whitelist:
      mode: verify
      qq_ids: []                  # 通用用户 ID；也可写 user_ids 别名
      group_ids: []               # Telegram chat_id（群为负数）
    blacklist:
      enabled: false
      qq_ids: []
      group_ids: []
```

> 配置项命名遵循「协议前缀 + 语义」：`bot_token_id`、`webhook_secret_token_id`。
> 所有密钥字段都是 **引用 ID**，不是密钥值。

## 4. 协议映射

### 入站

| Telegram Update | 内部模型 |
| :--- | :--- |
| `message`（private） | `IncomingMessage(scope="private", user_id=from.id, target_id=chat.id)` |
| `message`（group/supergroup） | `IncomingMessage(scope="group", group_id=chat.id)` |
| `message.reply_to_message` | `ReplySegment(message_id=...)` |
| `message.entities` / `caption_entities` | `MentionSegment` / `TextSegment` / 链接文本 |
| `photo` / `document` / `voice` / `video` / `sticker` | `ImageSegment` / `FileSegment` / `VoiceSegment` / `VideoSegment` |
| `edited_message` | 可选：覆盖历史或忽略（首版忽略） |
| `callback_query` | `IncomingNotice(notice_type=OTHER, sub_type="callback_query")` |

### 出站

| 内部动作 | Telegram API |
| :--- | :--- |
| 文本 | `sendMessage`（4096 字符分片） |
| 图片 | `sendPhoto`（URL 或 multipart） |
| 语音 | `sendVoice`（OGG/Opus；其他格式需转码） |
| 视频 | `sendVideo` |
| 文件 | `sendDocument` |
| 引用回复 | `reply_parameters.message_id` |
| 撤回 | `deleteMessage` |
| 表情回应 | `setMessageReaction` |
| @ 提及 | 文本实体 `mention` / `text_mention` |

## 5. 能力表

| 能力 | 支持 | 说明 |
| :--- | :---: | :--- |
| 发送图片/视频/文件 | ✅ | |
| 发送语音 | ✅ | 需转成 OGG/Opus，`preferred_voice_format="ogg"` |
| 撤回 | ✅ | 48 小时内 |
| 引用 | ✅ | |
| 表情回应 | ✅ | |
| 戳一戳 | ❌ | Telegram 没有对应语义 |
| 语音转文字 | ❌ | 首版不接外部 ASR |
| 合并转发读取 | ❌ | Telegram 无合并转发 |
| 群历史 | ❌ | Bot API 不允许任意拉取历史 |
| 群管理 | ❌ | 受 Bot 管理员权限限制，首版不做 |
| 主动私聊 | ✅ | 用户必须先 `/start` |
| 主动群聊 | ✅ | 受群权限与频控限制 |

## 6. 已知限制

- **4096 字符上限**：长文本必须分片，且不能破坏 Markdown/HTML 标签。
- **文件下载上限**：Bot API 默认 20 MB（自建 Server 可放宽）。
- **频控**：单聊约 30 msg/s，群聊约 20 msg/s；批量发送需要排队。
- **无法读取历史**：`get_group_history` 永远不可用，相关工具会被能力过滤隐藏。
- **语音格式**：Telegram 语音条只接受 OGG/Opus；本地 mp3/wav 在有 `ffmpeg`
  时会先转码成 OGG/Opus 再发语音条，没有 `ffmpeg` 时自动改用 `sendAudio`
  发送（不会伪装成语音条）。
- **媒体识别**：图片/视频/文件通过 `getFile` 下载到本地临时目录后交给媒体管线；
  临时目录默认保留最近 300 个文件。
- **用户必须先发起会话**：Telegram 不允许机器人主动私聊从未 `/start` 的用户。
- **重启丢消息策略**：`drop_pending_updates: true`（默认）会丢弃离线期间的 Update，
  避免机器人复述旧消息；需要不丢消息时设为 `false`，长轮询 offset 会持久化到
  临时目录，重启后从上次位置继续。
- **代理**：项目已内置 `socksio`，`proxy` 字段可直接填 SOCKS5。

## 7. 前端表单建议

控制台可以用 `GET /api/adapters/protocols` 拿到协议卡片，再按以下分组渲染：

1. **连接**：`transport`、`bot_token_id`（secret_ref）、`api_base`、`proxy`
2. **Webhook**（仅 `transport=webhook`）：`webhook_host`、`webhook_port`、
   `webhook_path`、`webhook_secret_token_id`
3. **行为**：`parse_mode`、`allowed_updates`、`drop_pending_updates`
4. **过滤**：复用黑白名单组件（`user_ids` / `group_ids`）

字段类型与默认值以 `TelegramAdapterConfig` 的 JSON Schema 为准。
