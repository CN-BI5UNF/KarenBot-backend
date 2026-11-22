"""WebUI 监听地址解析。

WebUI 默认只监听本机回环地址；需要局域网访问时由 ``webui.allow_lan``
配置项打开。启动命令里显式传入的 ``--host`` 仍然优先，方便终端用户临时
覆盖配置。

这里只做一次轻量 YAML 读取，不构造完整 Runtime：WebUI 启动入口需要在
端口预检之前知道绑定地址，而完整配置校验会留给 RuntimeHost。
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

LOOPBACK_HOST = "127.0.0.1"
LAN_HOST = "0.0.0.0"

SOURCE_CLI = "cli"
SOURCE_CONFIG = "config"
SOURCE_DEFAULT = "default"


def configured_allow_lan(config_file: Path | None) -> bool:
    """读取 ``webui.allow_lan``；配置缺失或损坏时按关闭处理。"""
    if config_file is None:
        return False
    try:
        raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("读取 WebUI 监听配置失败，按仅本机处理：%s", exc)
        return False
    if not isinstance(raw, dict):
        return False
    section = raw.get("webui")
    if not isinstance(section, dict):
        return False
    return bool(section.get("allow_lan", False))


def resolve_webui_host(
    cli_host: str | None,
    config_file: Path | None,
) -> tuple[str, str]:
    """返回 ``(监听地址, 来源)``。

    优先级：显式 ``--host`` > ``webui.allow_lan`` > 默认回环地址。
    来源用于设置页提示“当前监听由启动参数覆盖”，取值为 ``cli`` /
    ``config`` / ``default``。
    """
    explicit = str(cli_host or "").strip()
    if explicit:
        return explicit, SOURCE_CLI
    if configured_allow_lan(config_file):
        return LAN_HOST, SOURCE_CONFIG
    return LOOPBACK_HOST, SOURCE_DEFAULT


__all__ = [
    "LAN_HOST",
    "LOOPBACK_HOST",
    "SOURCE_CLI",
    "SOURCE_CONFIG",
    "SOURCE_DEFAULT",
    "configured_allow_lan",
    "resolve_webui_host",
]
