"""协议目录 —— 提供给前端控制台的协议元数据。

这份目录是「前端如何知道有哪些协议、每个协议支持什么、该渲染哪些配置项」
的唯一数据源。新增协议时在这里加一条，控制台无需硬编码协议知识。

字段约定：
    id          配置里 ``adapters.<name>.type`` 的取值
    name        展示名称
    status      stable / beta / planned
    summary     一句话用途（控制台卡片/settings 分组说明直接显示）
    transports  支持的接入方式（与配置字段 transport 的取值一致）
    dialects    协议方言/实现端（仅 OneBot 11 使用）
    docs        仓库内文档路径
    config_model Pydantic 配置模型名，前端可用 JSON Schema 生成表单
"""

from __future__ import annotations

from typing import Any

PROTOCOL_CATALOG: list[dict[str, Any]] = [
    {
        "id": "napcat",
        "name": "OneBot 11 / NapCatQQ",
        "status": "stable",
        "summary": (
            "QQ 个人号的现有主通道。通过 NapCat / LLOneBot / Lagrange / go-cqhttp "
            "等 OneBot 11 协议端接入，支持私聊、群聊、图片、语音、文件、群管理。"
        ),
        "transports": ["ws_client", "ws_server", "http"],
        "dialects": ["auto", "napcat", "llonebot", "lagrange", "gocqhttp", "generic"],
        "docs": "docs/protocols/onebot11.md",
        "config_model": "NapCatAdapterConfig",
    },
    {
        "id": "onebot11",
        "name": "OneBot 11（标准）",
        "status": "stable",
        "summary": (
            "严格按 OneBot 11 标准接入任意实现端，默认只调用标准公开 API。"
            "适合非 NapCat 的协议端或需要最小权限的部署。"
        ),
        "transports": ["ws_client", "ws_server", "http"],
        "dialects": ["auto", "napcat", "llonebot", "lagrange", "gocqhttp", "generic"],
        "docs": "docs/protocols/onebot11.md",
        "config_model": "OneBot11AdapterConfig",
    },
    {
        "id": "telegram",
        "name": "Telegram Bot API",
        "status": "beta",
        "summary": (
            "海外与自托管首选。一个 Bot Token 即可接入私聊、群组和 Topic，"
            "原生支持语音条、贴纸与引用回复。"
        ),
        "transports": ["long_polling", "webhook"],
        "dialects": [],
        "docs": "docs/protocols/telegram.md",
        "config_model": "TelegramAdapterConfig",
    },
    {
        "id": "qq_official",
        "name": "QQ 官方 Bot API v2",
        "status": "planned",
        "summary": (
            "合规、无第三方协议端封号风险的 QQ 官方通道；支持频道、官方群与单聊，"
            "但需要公网回调或 Gateway，主动消息受配额限制。"
        ),
        "transports": ["webhook", "gateway"],
        "dialects": [],
        "docs": "docs/protocols/qq-official.md",
        "config_model": "QQOfficialAdapterConfig",
    },
    {
        "id": "satori",
        "name": "Satori v1",
        "status": "planned",
        "summary": (
            "通用聊天协议。接入一个 Satori 服务端即可桥接 Discord、Telegram、飞书等"
            "十余个平台；适合不想逐个维护原生适配器的部署。"
        ),
        "transports": ["ws", "http"],
        "dialects": [],
        "docs": "docs/protocols/satori.md",
        "config_model": "SatoriAdapterConfig",
    },
]


def protocol_catalog() -> list[dict[str, Any]]:
    """返回副本，避免调用方原地修改模块常量。"""
    return [dict(item) for item in PROTOCOL_CATALOG]


__all__ = ["PROTOCOL_CATALOG", "protocol_catalog"]
