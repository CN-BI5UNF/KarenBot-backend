# Changelog

## v0.0.1-dev — 2026-11-11

首个私有开发版。

- 从 KarenBot 主项目拆出共享后端控制面 `backend/`。
- 提供 `create_control_app()`、`RuntimeHost`、API 路由、鉴权、事件、日志环形缓冲、
  设置声明、首次配置向导和聊天显示管线。
- 设置保存支持 159/179 字段热应用；Provider / Agent 支持热重建。
- 与 `KarenBot-WebUI`、`KarenBot-GUI` 的 v0.0.1-dev 配套。
