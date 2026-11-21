# QQ 官方 Bot API v2（计划中）

> 状态：**计划中**。本文定义用途、配置契约与消息映射，供前端控制台提前接入；
> 适配器实现尚未提交。

## 1. 它是做什么的

QQ 官方 Bot API v2 是腾讯开放的合规机器人通道，覆盖：

- QQ 频道（guild / channel）
- 官方群聊（group）
- 官方单聊（C2C）

与 NapCat 等第三方协议端相比，它**没有个人号封号风险**，但接入门槛更高：
需要 QQ 开放平台账号、机器人审核、公网回调或 Gateway，并且主动消息有配额。

适合：怕封号的长期部署、企业/社群官方运营、需要合规审计的场景。

## 2. 前置条件

1. 在 [QQ 开放平台](https://q.qq.com/) 创建机器人，拿到 `AppID`、`AppSecret`；
2. 配置回调地址（Webhook 模式）或启用 Gateway（WebSocket 模式）；
3. 在开放平台申请所需权限/事件订阅（intents）；
4. 把 `AppSecret` 存进 `SecretsManager`，配置里只引用 ID。

## 3. 两种事件通道

| 模式 | 说明 | 要求 |
| :--- | :--- | :--- |
| `webhook` | 腾讯把事件 POST 到你的 HTTPS 地址 | 公网可达、有效证书、Ed25519 验签 |
| `gateway` | Karen 主动连腾讯 WebSocket 网关 | 出站网络即可；需处理 identify / heartbeat / resume / shard |

首版建议先做 `webhook`，部署简单；Gateway 适合无法暴露公网端口的环境。

## 4. 计划配置字段

```yaml
adapters:
  qq_official:
    type: qq_official
    enabled: true
    transport: webhook              # webhook | gateway
    app_id: "102xxxxx"
    app_secret_id: qq_official_secret   # SecretsManager 引用 ID
    sandbox: false                  # true = 沙箱环境

    # webhook
    webhook_host: 0.0.0.0
    webhook_port: 8443
    webhook_path: /qq-official
    webhook_public_url: https://bot.example.com/qq-official
    signature_key_id: qq_official_signature  # Ed25519 验签密钥引用

    # gateway
    intents: 0                      # 0 = 使用推荐集合
    shard: [0, 1]                   # [shard_id, shard_count]

    # 主动消息
    proactive_enabled: false        # 受官方配额限制，默认关闭
    proactive_private_daily_limit: 0
    proactive_group_daily_limit: 0

    whitelist:
      mode: verify
      user_ids: []                  # OpenID，字符串
      group_ids: []                 # group_openid / channel_id
    blacklist:
      enabled: false
      user_ids: []
      group_ids: []
```

> 官方 API 全链路使用字符串 OpenID，不是 QQ 号。黑白名单、会话键、记忆库
> 都必须按字符串处理；这也是 Phase 0 把内部 ID 全部字符串化的原因。

## 5. 鉴权与验签

1. **获取 access token**：`POST https://bots.qq.com/app/getAppAccessToken`，
   body `{appId, clientSecret}`，返回 `access_token`（有有效期，需缓存并提前刷新）。
2. **调用 API**：`Authorization: QQBot {access_token}`。
3. **Webhook 验签**：请求头 `X-Signature-Ed25519` + `X-Signature-Timestamp`，
   用机器人密钥对 `timestamp + raw_body` 做 Ed25519 校验。
4. **回调地址验证**：开放平台会发送 `op=13` 的验证事件，需要原样返回
   `plain_token` 与签名。

## 6. 协议映射

### 入站

| 官方事件 | 内部模型 |
| :--- | :--- |
| `C2C_MESSAGE_CREATE` | `IncomingMessage(scope="private", user_id=author.user_openid)` |
| `GROUP_AT_MESSAGE_CREATE` | `IncomingMessage(scope="group", group_id=group_openid)` |
| `AT_MESSAGE_CREATE`（频道） | `IncomingMessage(scope="channel", group_id=channel_id, guild_id=guild_id)` |
| `MESSAGE_REACTION_ADD` | `IncomingNotice(notice_type=OTHER, sub_type="reaction_add")` |
| `GROUP_ADD_ROBOT` / `GROUP_DEL_ROBOT` | `IncomingNotice(GROUP_INCREASE/DECREASE)` |
| `FRIEND_ADD` / `FRIEND_DEL` | `IncomingNotice(FRIEND_ADD)` |

### 出站

| 内部动作 | 官方 API |
| :--- | :--- |
| 文本 | `POST /v2/users/{openid}/messages` 或 `/v2/groups/{group_openid}/messages` |
| 图片/语音/视频/文件 | 先 `POST /v2/.../files` 上传，拿到 `file_info` 再发 `msg_type=7` 富媒体 |
| 引用 | `message_reference` + `msg_id` |
| 撤回 | `DELETE /v2/.../messages/{message_id}` |
| 表情回应 | `PUT /v2/.../reactions/{type}/{id}` |

## 7. 能力表（预计）

| 能力 | 支持 | 说明 |
| :--- | :---: | :--- |
| 发送图片/语音/视频/文件 | ✅ | 富媒体需先上传 |
| 撤回 | ✅ | |
| 引用 | ✅ | |
| 表情回应 | ✅ | |
| 戳一戳 | ❌ | |
| 语音转文字 | ❌ | |
| 合并转发 | ❌ | |
| 群历史 | ❌ | 官方 API 不提供 |
| 群管理 | ❌ | 官方不开放踢人/禁言 |
| 好友/群列表 | ⚠️ | 仅能拿到机器人已加入的群，不能枚举好友 |
| 主动私聊 | ⚠️ | 受配额限制，且用户需先与机器人交互 |
| 主动群聊 | ⚠️ | 受配额限制 |

## 8. 关键限制

- **主动消息配额**：官方对主动推送有严格频控；`ProactiveLoop` 必须按
  `proactive_private_daily_limit` / `proactive_group_daily_limit` 降级。
- **被动回复窗口**：收到消息后须在限定时间内回复，且次数有限。
- **OpenID 不通用**：同一用户在不同机器人下的 OpenID 不同，不能跨机器人关联。
- **审核与权限**：部分事件/接口需要单独申请，未开通时只能降级。
- **不允许任意加群**：机器人只能被邀请进群，不能主动搜索/加入。

## 9. 前端表单建议

1. **凭证**：`app_id`、`app_secret_id`（secret_ref）、`sandbox`
2. **通道**：`transport`（webhook/gateway）
3. **Webhook**：`webhook_host`、`webhook_port`、`webhook_path`、
   `webhook_public_url`、`signature_key_id`
4. **Gateway**：`intents`、`shard`
5. **主动消息**：`proactive_enabled`、两个日限额
6. **过滤**：黑白名单（OpenID 字符串）

控制台应在卡片上明确提示「需要公网 HTTPS / 官方审核 / 主动消息配额」，
避免用户把它当成 NapCat 的零门槛替代品。
