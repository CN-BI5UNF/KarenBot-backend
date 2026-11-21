# KarenBot 协议支持总览

本文档目录说明 KarenBot 支持（以及计划支持）的聊天协议、各自的用途、
配置方式和前端控制台接入契约。

## 状态矩阵

| 协议 | 状态 | 适用场景 | 接入方式 | 文档 |
| :--- | :--- | :--- | :--- | :--- |
| OneBot 11（NapCat / LLOneBot / Lagrange / go-cqhttp） | **已支持** | QQ 个人号，现有主通道 | WebSocket / HTTP API / HTTP POST | [onebot11.md](onebot11.md) |
| Telegram Bot API | **Beta** | 海外、自托管、轻量部署 | Long Polling / Webhook | [telegram.md](telegram.md) |
| QQ 官方 Bot API v2 | 计划中 | 合规 QQ 频道/官方群，怕封号 | Webhook / Gateway | [qq-official.md](qq-official.md) |
| Satori v1 | 计划中 | 一个适配器桥接 Discord/TG/飞书等 | WS + HTTP | [satori.md](satori.md) |

> Telegram 已进入 Beta：长轮询/Webhook 与消息收发已实现，但尚未做真实环境长期回归。
> 「计划中」表示配置模型与前端契约已在本目录定义，但适配器尚未实现。

## 如何选择

- **只想让 QQ 个人号跑起来**：用 `type: napcat`，接入 NapCat / LLOneBot 等
  协议端。这是目前最成熟的通道。
- **需要严格标准、最小权限**：用 `type: onebot11` + `dialect: generic`。
- **怕 QQ 封号、要合规**：等 QQ 官方 Bot API v2；它使用 OpenID，不需要个人号。
- **想一次覆盖多个海外平台**：等 Satori；但需要你自己部署 Satori/Koishi 服务端。
- **只要 Telegram**：用 `type: telegram`，一个 Bot Token 即可；长轮询模式不需要公网 IP。

## 协议无关的核心概念

所有协议都通过同一套接口向上提供数据：

- `Target`：发送目标。`scope` 为 `private` / `group` / `channel`，
  `target_id` 统一字符串，避免大数 ID 精度问题。
- `MessageSegment`：结构化消息段。文本、@、引用、图片、语音、视频、文件、
  表情、合并转发，以及 `RawSegment` 兜底。
- `AdapterCapabilities`：能力声明。业务层据此隐藏协议端不存在的工具
  （例如纯文本平台不会出现 `send_poke`）。
- `IAdapter`：生命周期、收发消息、联系人查询。非通用能力通过可选方法暴露，
  默认抛 `NotImplementedError`。

## 前端接入

控制台不需要知道每个协议的全部细节：

```http
GET /api/adapters/protocols   # 协议卡片 + AdapterConfig JSON Schema
GET /api/adapters/status      # 当前适配器类型、方言、连接状态、能力表
```

字段含义、JSON 示例和表单生成方式见
[frontend-integration.md](frontend-integration.md)。

## 相关文档

- [OneBot 11 完整接入](onebot11.md)
- [Telegram（计划中）](telegram.md)
- [QQ 官方 Bot API v2（计划中）](qq-official.md)
- [Satori v1（计划中）](satori.md)
- [前端控制台接入契约](frontend-integration.md)
- [后端新增接口报告（前端适配用）](backend-api-report.md)
- [适配器开发指南](../adapter_development.md)

## 当前边界（重要）

- **单主适配器**：一个 Runtime 只启动第一个 `enabled: true` 的适配器；多适配器并存、
  会话命名空间隔离要等后续版本。配置多个会被忽略并记 warning。
- **OneBot 旧 CQ 兼容**：业务发送动作已改为结构化段，但旧 action/模型输出里的
  `[CQ:reply]` / `[CQ:at]` / `[CQ:face]` 只在 `adapters/outbound.py` 边界解析，
  不会进入新的协议适配器。
- **控制台表单**：`/api/settings` 的固定字段表仍以 OneBot 11 为主；
  Telegram 等新协议请使用 `config.yaml` 或 `GET /api/adapters/protocols` 提供的
  JSON Schema，动态表单会在控制台协议模块上线后接入。
- **多账号**：OneBot 反向 WS 支持多账号连接与按 `self_id` 路由普通消息；
  群管理等少数接口仍按最近连接发送，多账号部署请优先用单账号实例。
- **协议切换的历史隔离**：切换 `type` 后，会话键仍是 `scope:id`，不同平台的
  同号用户可能共用历史；需要严格隔离时请使用不同的数据目录/人格。

## 安全默认值

- 反向 WS 与 Webhook 默认只监听 `127.0.0.1`；跨设备/公网监听（`0.0.0.0` 等）
  必须配置 token/secret，否则适配器拒绝启动。
- OneBot Webhook 使用 `X-Signature: sha1=<hmac>` 验签；Telegram Webhook 使用
  `X-Telegram-Bot-Api-Secret-Token`。
- Bot token / access token 只从 `SecretsManager` 引用；配置里填的是引用 ID。
  如果引用的密钥不存在，适配器拒绝启动，而不是静默降级为无鉴权。
- 入站语音只接受公网 http(s) 或适配器提供的受信本地文件，避免把
  `/etc/passwd` 之类的本地路径复制进 workspace。
