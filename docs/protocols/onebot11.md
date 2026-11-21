# OneBot 11 接入指南

> 状态：**已支持**
> 适配器类型：`napcat`（NapCat 方言） / `onebot11`（严格标准）
> 代码位置：`adapters/onebot11/`、`adapters/transports/`

## 1. 它是做什么的

OneBot 11 是 QQ 机器人社区的事实标准协议。KarenBot 通过它连接
NapCatQQ、LLOneBot、Lagrange.OneBot、go-cqhttp 等「协议端」，从而以机器人
账号收发 QQ 私聊/群聊消息。

`type: napcat` 与 `type: onebot11` 使用同一套实现，区别只有默认方言：

- `napcat`：默认方言 `napcat`，自动启用语音转文字、表情回应、群历史、
  文件上传等 NapCat 扩展能力。存量用户零配置升级。
- `onebot11`：默认方言 `generic`，只调用 OneBot 11 标准公开 API。
  任何严格实现 OneBot 11 的协议端都能开箱即用。

## 2. 四种通信方式

| `transport` | Karen 角色 | 协议端配置 | 说明 |
| :--- | :--- | :--- | :--- |
| `ws_client` | WebSocket 客户端 | 「正向 WS」监听端口 | Karen 主动连 `ws://host:port/path`，最常用 |
| `ws_server` | WebSocket 服务端 | 「反向 WS」客户端 | Karen 监听端口，协议端连入；支持 API/Event 双连接与多账号。默认只监听 127.0.0.1；跨设备需 host=0.0.0.0 且必须配置 token |
| `http` | HTTP 客户端 + Webhook 服务端 | 「HTTP API」+「HTTP POST」 | 出站 POST 到 `http_api_url/action`，入站由 Karen 的 Webhook 接收 |

混合部署也支持：`transport: ws_client` 同时填写 `http_api_url` 时，事件走 WS、
出站 API 走 HTTP。

### 鉴权

- WebSocket：`Authorization: Bearer <token>`；反向 WS 同时接受
  `?access_token=<token>`。反向 WS 默认只监听 `127.0.0.1`；监听非回环地址时
  必须配置 `access_token_id`，否则拒绝启动。
- HTTP API：请求头 Bearer + query 双写，兼容只认 query 的实现端。
- HTTP POST：配置 `webhook_secret_id` 后校验
  `X-Signature: sha1=<hmac_sha1_hex>`，签名原文是原始请求体字节，
  使用 `hmac.compare_digest` 防时序攻击。Webhook 默认只监听 `127.0.0.1`；
  监听 `0.0.0.0` 或其他非回环地址时**必须**配置 `webhook_secret_id`，否则拒绝启动。
- 所有 token/secret 都通过 `SecretsManager` 引用：配置里只填
  `access_token_id` / `webhook_secret_id`，不要填密钥本身。

## 3. 配置字段

```yaml
adapters:
  default:
    type: napcat                # napcat | onebot11
    enabled: true
    transport: ws_client        # ws_client | ws_server | http
    dialect: auto               # auto | napcat | llonebot | lagrange | gocqhttp | generic
    message_format: array       # array | string

    # WS 端点（transport=ws_client / ws_server）
    host: 127.0.0.1
    port: 3001
    path: /
    access_token_id: napcat_default_token   # SecretsManager 引用 ID，不是 token

    # HTTP（transport=http 或混合部署）
    http_api_url: ""            # 例如 http://127.0.0.1:3000
    webhook_enabled: false
    webhook_host: 127.0.0.1
    webhook_port: 5700
    webhook_path: /onebot11
    webhook_secret_id: null

    # 重连与超时
    reconnect_interval_seconds: 3.0
    max_reconnect_attempts: -1        # -1 = 无限
    reconnect_backoff_max_seconds: 60.0
    ping_interval_seconds: 20.0
    ping_timeout_seconds: 20.0
    startup_connect_timeout_seconds: 1.5
    api_timeout_seconds: 30.0
    api_wait_connected_timeout_seconds: 3.0
    fast_reconnect_attempts: 5
    fast_reconnect_interval_seconds: 0.3
    reconnect_jitter_seconds: 0.2
    voice_fetch_delay_seconds: 1.0

    # 入站过滤（ID 统一字符串）
    whitelist:
      mode: verify              # open | verify | whitelist
      qq_ids: []
      group_ids: []
    blacklist:
      enabled: false
      qq_ids: []
      group_ids: []
```

### 旧配置兼容

旧配置里的 `mode: client | server` 会自动映射：

```yaml
mode: client   →   transport: ws_client
mode: server   →   transport: ws_server
```

`type: "napcat"` 保持原样，不需要改配置文件。`mode` 字段仍然保留为只读兼容项，
新配置建议直接写 `transport`。

### 协议端进程托管

旧版 `adapters.*.manage_process` / `process_path` / `process_args` /
`process_warmup_seconds` 已移除；配置升级时会自动迁移到顶层 `programs`
（见 `docs/external_programs.md`）。新配置用 `programs` 管理 NapCat 或其他
外部程序的启动/关闭，不再把进程细节塞进渠道配置。

## 4. 方言与能力

方言决定哪些可选动作可用。前端可以用
`GET /api/adapters/status` 读取当前能力表，据此隐藏工具/按钮。

| 能力 | napcat | llonebot | lagrange | gocqhttp | generic |
| :--- | :---: | :---: | :---: | :---: | :---: |
| 发送图片/语音/视频 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 撤回 / 引用 / @ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 群管理（踢/禁言/全员禁言/退群） | ✅ | ✅ | ✅ | ✅ | ✅ |
| 读取单条消息 `get_msg` | ✅ | ✅ | ✅ | ✅ | ✅ |
| 合并转发读取 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 文件上传 | ✅ | ✅ | ❌ | ✅ | ❌ |
| 群历史消息 | ✅ | ✅ | ✅ | ✅ | ❌ |
| 戳一戳 | ✅ | ✅ | ❌ | ✅ | ❌ |
| 消息表情回应 | ✅ | ❌ | ❌ | ❌ | ❌ |
| 语音转文字 | ✅ | ❌ | ❌ | ❌ | ❌ |

> `generic` 不调用任何扩展 API；当实现端是未知方言时，KarenBot 不会因为
> 一个不存在的 action 而报错，相关工具会直接从模型工具列表中消失。

## 5. 消息段映射

内部 IR 只保留 10 类语义段。OneBot 11 的数组格式与 CQ 字符串都在这层转换，
业务层（`core/`、`tools/`）不直接拼 CQ 码。

| OneBot 11 | 内部 IR | 可读文本占位 |
| :--- | :--- | :--- |
| `text` | `TextSegment` | 原文 |
| `at` | `MentionSegment` | `@QQ123` / `@我` / `@全体成员` |
| `reply` | `ReplySegment` | `[引用msg_id=...]` |
| `image` | `ImageSegment` | `[图片]` |
| `record` / `voice` | `VoiceSegment` | `[语音]` |
| `video` | `VideoSegment` | `[视频]` |
| `file` | `FileSegment` | `[文件 name]` |
| `face` | `FaceSegment` | 表情名 |
| `forward` | `ForwardSegment` | `[合并转发 id=...]` |
| `rps` / `dice` / `shake` / `json` / `xml` / `music` / `location` 等 | `RawSegment` | `[猜拳]` / `[JSON 卡片]` 等 |

CQ 转义严格按标准处理：

- 文本：`&` → `&amp;`，`[` → `&#91;`，`]` → `&#93;`
- 参数值额外：`,` → `&#44;`

出站默认 `message_format: array`，避免 base64 图片拼进 CQ 串导致的转义问题。
只有极旧的实现端才需要 `string`。

业务层发送动作（reply/@/face）已改为结构化 `MessageSegment`；CQ 只在
`adapters/onebot11/segments.py` 与 `adapters/outbound.py` 的旧动作兼容层出现。

## 6. 事件覆盖

- `message`：私聊（好友/群临时会话）、群聊（普通/匿名/系统提示）
- `message_sent`：自身账号发送回显，`IncomingMessage.self_sent=True`
- `notice`：群撤回、好友撤回、入群、退群、管理员变更、群文件上传、
  群禁言/解禁、好友添加，以及 `notify` 下的戳一戳、运气王、群荣誉
- `request`：加好友、加群、邀请入群
- `meta_event`：lifecycle、heartbeat（用于连接健康指示）

`IncomingNotice` 已扩展 `sub_type`、`target_id`、`duration`、`file` 字段。

## 7. API 覆盖

适配器直接调用并封装的标准动作：

- 消息：`send_msg`、`delete_msg`、`get_msg`、`get_forward_msg`
- 联系人：`get_friend_list`、`get_group_list`、`get_group_member_list`、
  `get_stranger_info`
- 请求：`set_friend_add_request`、`set_group_add_request`
- 群管理：`set_group_kick`、`set_group_ban`、`set_group_whole_ban`、
  `set_group_leave`
- 方言扩展：`send_poke`、`set_msg_emoji_like`、`fetch_ptt_text`、
  `get_group_msg_history`、`upload_group_file` / `upload_private_file`、
  `get_file`、`get_image`
- 查询/运维（不注册为 LLM 工具）：`get_login_info`、`get_status`、
  `get_version_info`、`can_send_image`、`can_send_record`、`send_like`、
  `set_group_card`、`set_group_name`、`set_group_admin`

`OneBot11Adapter.probe_capabilities()` 会在连接后调用
`can_send_image` / `can_send_record` 细化能力表；工具注册表需要在下一次
重载时才会看到变化，因此默认仍以方言能力表为准。

### 敏感 API 边界

`get_cookies`、`get_csrf_token`、`get_credentials`、`set_restart`、
`clean_cache` 这类系统/凭证接口**不会**注册成 LLM 工具，也不会出现在
`IAdapter` 的语义方法里。需要时只能由运维代码通过 `call_api()` 显式调用。
这样可以避免 prompt 注入后模型直接拿到登录凭证或重启进程。

## 8. 故障排查

| 现象 | 检查 |
| :--- | :--- |
| `ws_client` 一直重连 | 协议端是否开启「正向 WS」；host/port/path 是否一致；token 是否相同 |
| `ws_server` 没有连入 | 协议端「反向 WS」地址是否填了 Karen 的局域网 IP；防火墙端口 |
| HTTP 模式收不到事件 | `webhook_enabled=true`；协议端 HTTP POST 地址是否为 `http://IP:5700/onebot11`；secret 是否一致 |
| 模型调用的戳一戳/表情工具不见了 | 当前方言能力为 false；换 `dialect: napcat` 或实现端对应的方言 |
| 语音转文字为空 | 仅 `napcat` 方言支持；确认 `voice_fetch_delay_seconds` 足够 |
| 图片/语音发送失败 | `message_format` 是否被改成 `string` 且 base64 过大；改用 `array` |

## 9. 测试

```bash
uv run pytest tests/test_onebot11_segments.py tests/test_onebot11_events.py \
  tests/test_onebot11_http.py tests/test_onebot11_webhook.py -q
```

不需要真实 NapCat：HTTP API 用 `httpx.MockTransport`，Webhook 用
`httpx.ASGITransport`，WS 用假连接对象。
