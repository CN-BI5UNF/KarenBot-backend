# KarenBot-backend

KarenBot 的**核心程序 + 后端**仓库。当前仓库已从“纯 `backend/` 模块”升级为可组合的核心基础包。

> 这里包含启动程序、核心运行时和后端控制面，但**不包含 GUI/WebUI 界面模块**。
> 使用时需要把 `KarenBot-GUI/gui/` 或 `KarenBot-WebUI/webui/` 放到仓库根目录，
> 再安装依赖并启动。

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
adapters/               NapCat 等适配器
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
.venv/bin/python main.py --frontend gui
```

也可以直接运行 `python main.py`，默认启动 WebUI。

## 安装器如何组合

安装器的在线安装会把本仓库作为“核心程序 + 后端”基础包，再下载用户选择的
`KarenBot-GUI` 或 `KarenBot-WebUI` 源码包，把 `gui/` 或 `webui/` 合并到应用根目录。
这样不需要额外的第四个仓库，也不会在仓库里复制前端代码。

## 版本

| 项目 | 版本 |
|---|---|
| 本仓库发布版本 | `v0.1.1-alpha.1` |
| 应用代码版本 | `1.0.1-alpha.1` |
| 配套界面模块 | `v0.1.1-alpha.1` |

`VERSION` 是模块发布版本，`backend/__init__.py` 中的代码版本沿用应用兼容版本。
核心程序合并进入 `main` 后，发布 tag/Release 会在验证通过后同步更新。

## 安全边界

- 不提交 `config.yaml`、`secrets.enc`、访问令牌、API Key、RSA 私钥、聊天记录或日志。
- 用户人格、表情包、模型权重和 `data/` 属于用户数据，不进入仓库。
- 后端 API 默认需要令牌；不要把控制面直接暴露到不可信网络。
- 更新器只替换 `backend/`；核心程序更新通过安装器或新的核心基础包完成。

## 许可证

Apache-2.0，详见 `LICENSE` 与 `NOTICE`。
