# 外部程序托管

KarenBot 可以在 Runtime 启动后拉起任意外部程序（NapCat、LLOneBot、代理、
旁路服务等），并在关闭时按每个程序的策略决定是否一并停止。

## 配置

顶层 `programs` 是 `dict[id, 配置]`，ID 是稳定标识，`name` 只是显示名称：

```yaml
programs:
  napcat_default:
    name: NapCatQQ
    enabled: true
    executable: C:\NapCat\launch.bat
    args: []
    cwd: ""
    env: {}
    stop_with_karen: true
    probe_host: 127.0.0.1
    probe_port: 3001
    auto_restart: true
    startup_wait_seconds: 2.0
```

| 字段 | 说明 |
| --- | --- |
| `name` | 显示名称，可随时修改，不影响进程身份 |
| `enabled` | 是否在 Runtime 启动时拉起 |
| `executable` | 可执行文件/脚本路径，或 PATH 中的命令名；不经过 shell |
| `args` | 参数列表，不支持管道/重定向 |
| `cwd` | 工作目录；留空用可执行文件目录，再回退项目根目录 |
| `env` | 追加到继承环境之上的 `KEY: VALUE`；明文保存 |
| `stop_with_karen` | 关闭时是否停止**由 Karen 成功拉起且仍存活**的进程 |
| `probe_host` / `probe_port` | 启动前 TCP 探测；端口已监听则跳过启动，且永不接管/停止该实例 |
| `auto_restart` | 异常退出后自动重启；与渠道断线重连无关 |
| `startup_wait_seconds` | 启动后等待多久判定没有快速退出；0 表示不等待 |

## 行为保证

- 启动失败（路径不存在、端口失败、进程立即退出）只写 ERROR 日志并出现在
  `GET /api/status` 的 `programs` 中；Runtime 继续正常运行。
- 只有 `spawn` 成功后才记录进程；关闭时只停止这些进程。启动失败项、探测命中
  的外部实例、从未启动的条目都不会被 `stop()`。
- `stop_with_karen=False` 的进程会跨 Runtime 重启保留，管理器不会重复拉起。
- 保存配置后热应用：新增/启用立即启动，删除/禁用立即停止由 Karen 拉起的进程。
  `stop_with_karen` 只影响 KarenBot 整体关闭，不阻止用户显式删除/禁用。
- 失败提醒由前端按 `failure_seq` 去重：GUI 弹出聚合对话框，WebUI 首次弹窗、
  后续 toast；一次失败只提醒一次。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/settings/programs` | 配置 + 运行状态 + 默认值 |
| PUT | `/api/settings/programs` | 整表保存并热应用 |
| POST | `/api/settings/programs/{id}/start` | 手动启动/重试 |
| POST | `/api/settings/programs/{id}/stop` | 手动停止（只停止 Karen 拉起的进程） |

## 限制

- Windows `.bat/.cmd` 可能只是启动器：启动器退出 0 后 Karen 无法再跟踪其
  子进程，也不会自动重启。需要可靠关闭时请直接填 `.exe`。
- 进程按 Karen 的用户权限运行；`env` 会继承 Karen 的环境变量，不要把密钥
  明文放进 `config.yaml`。
- 极端情况下（进程被系统强杀、事件循环直接取消）托管进程可能残留；需要硬
  保证时请交给 systemd/Windows 服务等外部 supervisor。
- 旧版的 `adapters.*.manage_process` 会在配置 v2→v3 时迁移到本列表。
