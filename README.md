# KarenBot-backend

[English](README_EN.md) · [KarenBot 主仓库](https://github.com/CN-BI5UNF/KarenBot) · [更新日志](CHANGELOG.md)

KarenBot 的**核心程序 + 后端**仓库。当前仓库已从“纯 `backend/` 模块”升级为可组合的核心基础包。

> 这里包含启动程序、核心运行时和后端控制面，但**不包含 GUI/WebUI 界面模块**。
> 使用时需要把 `KarenBot-GUI/gui/` 或 `KarenBot-WebUI/webui/` 放到仓库根目录，
> 再安装依赖并启动。

当前统一发行版为 [v2.1.3-alpha](https://github.com/CN-BI5UNF/KarenBot/releases/tag/v2.1.3-alpha)，
发布为 GitHub **Latest 普通发行版（非预发布）**；tag 保留 `alpha` 后缀。
正式安装推荐使用 [KarenBot 安装器](https://github.com/CN-BI5UNF/KarenBot-Installer)。

## 仓库边界

| 仓库 | 内容 |
|---|---|
| **KarenBot-backend** | 核心程序 + 后端控制面 |
| [KarenBot-GUI](https://github.com/CN-BI5UNF/KarenBot-GUI) | 原生 GTK4 界面模块 `gui/` |
| [KarenBot-WebUI](https://github.com/CN-BI5UNF/KarenBot-WebUI) | 浏览器界面模块 `webui/` |

核心程序负责启动、配置、Runtime、适配器、记忆、工具、模型插件和插件加载；
`backend/` 负责 FastAPI 控制面、WebSocket、鉴权和页面 API。界面模块只负责各自的
展示与交互。

## 目录

```text
main.py                 统一入口：--frontend gui|webui、--list-frontends
main_cli.py             headless / 配置向导入口
core/                   Runtime、事件、媒体管线、聊天时间线等
features/               TTS、视觉、知识、绘画等能力
model_plugins/          TTS / Embedding 模型插件
plugins/                应用插件根目录
app_config/             配置 schema、加载、迁移、密钥
adapters/               OneBot 11 / NapCat、Telegram 等适配器
agents/                 人格代理与工具循环
memory/                 对话历史、归档、RAG
mind/                   人格后台状态
providers/              LLM Provider 协议与预设
tools/                  工具注册表与内置工具
utils/                  token、图片、路径等通用工具
packaging/ deploy/       打包、部署与服务示例
backend/                FastAPI 控制面、页面 API、插件协议
docs/                   架构、配置、部署与开发文档
```

仓库不包含：`.venv/`、`data/`、用户人格目录、`config.yaml`、`secrets.enc`、日志、
模型权重、聊天记录和构建产物。

## 快速启动

先放入一个前端模块：

```bash
# WebUI
cp -a /path/to/KarenBot-WebUI/webui ./webui

# 或者 GUI
cp -a /path/to/KarenBot-GUI/gui ./gui
```

创建环境并安装：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e .
.venv/bin/python main.py --list-frontends
.venv/bin/python main.py --frontend webui
```

原生 GUI 在 Linux 上还需要系统 GTK4/libadwaita/PyGObject：

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -e .
.venv/bin/python main.py --frontend gui
```

GUI 的虚拟环境需要能导入系统 `gi` 包。已有环境可检查 `.venv/pyvenv.cfg` 中的
`include-system-site-packages`。

也可以直接运行 `python main.py`，默认启动 WebUI；WebUI 默认只监听
`127.0.0.1`。需要局域网访问时，在设置页「网络」打开
“允许暴露在局域网中”，或在 `config.yaml` 设置 `webui.allow_lan: true`，
或启动时显式加 `--host 0.0.0.0`（命令行优先于配置）。

首次使用时，先配置并测试主模型；暂时没有 NapCat 或其它协议端时，可在向导中
选择“暂不连接渠道”，完成后在「模型」页测试模型连接。之后在「设置 → 渠道」添加或
启用渠道，再配置黑白名单。局域网访问、备份目录等设置不会因重新提交向导而重置。
运行中的 Runtime 需要先停止，才能再次提交配置向导。

## 安装器如何组合

v2 起安装器从 `CN-BI5UNF/KarenBot` 的统一 Release 下载 `modules.json`、本仓库资产
和用户选择的 GUI/WebUI 资产，按 manifest 校验后把 `gui/` 或 `webui/` 合并到应用根目录。
本仓库仍可作为开发时的核心基础包单独拉取，但正式安装/更新以 KarenBot 统一发行为准。

## 备份与选择性恢复

GUI 与 WebUI 的「设置 → 备份」共用后端备份 API，可按类别选择备份和恢复内容：

| 类别 | 主要内容 |
|---|---|
| 配置与偏好 | `config.yaml`、校准、人格简述、知识与黑话文件 |
| 加密凭据与密钥 | `secrets.enc`、`secrets.meta`、RSA 公私钥（含 keyring 导出） |
| 聊天与记忆 / 向量记忆 | `memory/` / `vector/` |
| 工作区 | `workspace/`；入站媒体与附件可单独排除 |
| 表情 / 提供商 / 人格 | `emoji/`、自定义 Provider 预设、`personas/` |

创建前显示每类源数据大小，执行中显示分类处理量和实际归档数据量；大小是未压缩内容统计，
ZIP 文件的最终体积会因压缩率而不同。恢复前校验归档并显示每类备份大小、当前目标大小和
覆盖/新增文件数；勾选类别、预览并输入确认短语 `恢复备份` 后即可启动异步恢复。

恢复会暂时停止 Runtime，先生成当前所选类别的完整回滚备份，再逐文件覆盖归档内的同路径
文件并增加缺失文件；目标中未收录的文件会保留，未选类别不写入。写入失败会尝试回滚，
原先运行的 Runtime 会尝试重新启动；结果和回滚备份名可在任务进度中查看。
选择凭据时要求归档内密钥材料可验证，恢复时同步 RSA 私钥到当前密钥存储。

各 SQLite 库分别取得一致性快照，用户导出不承诺跨库全局事务，也不等同于严格迁移快照。
ZIP 本身未加密；凭据包包含可能解密 API Key 的材料，请按密码保管。
详细限制、隔离演练和 API 见 [备份文档](docs/backup.md)。

## 版本

| 项目 | 版本 |
|---|---|
| v2 统一发行版 | `v2.1.3-alpha` |
| 应用代码版本 | `2.1.3-alpha` |
| 过渡版 | 后端 `v0.1.11-alpha.1` / 前端 `v0.1.1-alpha.2` |

`VERSION`、`backend/__init__.py` 与 `pyproject.toml` 统一标记当前版本；`uv.lock` 使用
PEP 440 规范化版本 `2.1.3a0`。
过渡版本发布后，更新源切换为 KarenBot 主仓库的统一发行版；v2 组件 tag/Release
只用于源码回溯和 submodule 固定。

## 安全边界

- 不提交 `config.yaml`、`secrets.enc`、访问令牌、API Key、RSA 私钥、聊天记录或日志。
- 用户人格、表情包、模型权重和 `data/` 属于用户数据，不进入仓库。
- 后端 API 默认需要令牌；不要把控制面直接暴露到不可信网络。
- 模块更新器按白名单叠加完整后端程序（`backend/`、`core/`、`memory/` 等），只写程序文件；`data/`、`personas/`、`plugins/`、`config.yaml` 等用户数据不变，被替换的文件会先备份。

## 开发与文档

Python 3.10+；CI 测试 Python 3.11 / 3.12。安装开发依赖后运行：

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q tests -k 'not api_builds_module_service'
.venv/bin/python -m ruff check agents core memory mind providers tools tests
git diff --check
```

- [架构](docs/architecture.md)、[功能](docs/features.md)、[后台运行](docs/background_running.md)
- [协议与前端 API](docs/protocols/README.md)、[接入契约](docs/protocols/frontend-integration.md)
- [数据目录](docs/data_root_contract.md)、[存储维护](docs/storage_maintenance.md)、[危险操作](docs/destructive_admin.md)
- [人格编写](docs/persona_writing_guide.md)、[Provider 开发](docs/provider_development.md)

## 许可证

Apache-2.0，详见 `LICENSE` 与 `NOTICE`。
