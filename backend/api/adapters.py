"""渠道/协议接口 —— 给控制台提供协议目录、配置 Schema 与运行状态。

前端接入方式：
    GET /api/adapters/protocols
        → 协议卡片数据 + AdapterConfig 的 JSON Schema（可直接生成表单）
    GET /api/adapters/status
        → 当前主适配器的类型、方言、连接状态、能力表与脱敏配置摘要

新增协议只需在 ``adapters/catalog.py`` 与配置模型里登记，前端无需硬编码。
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import TypeAdapter, ValidationError

from adapters.catalog import protocol_catalog
from app_config import user_error_text
from app_config.schema import AdapterConfig
from backend.config_edit import apply_validated

from .deps import require_runtime

router = APIRouter(prefix="/api/adapters", tags=["adapters"])

_ADAPTER_CONFIG_SCHEMA = TypeAdapter(AdapterConfig).json_schema()
_ADAPTER_CONFIG_ADAPTER = TypeAdapter(AdapterConfig)

# 配置摘要中允许回传的字段（密钥 ID 只返回引用名，绝不返回密钥值）
_SUMMARY_FIELDS = (
    "type",
    "enabled",
    "transport",
    "dialect",
    "message_format",
    "host",
    "port",
    "path",
    "http_api_url",
    "webhook_enabled",
    "webhook_host",
    "webhook_port",
    "webhook_path",
    "access_token_id",
    "webhook_secret_id",
    "bot_token_id",
    "api_base",
    "parse_mode",
    "webhook_public_url",
    "webhook_secret_token_id",
    "polling_timeout_seconds",
)


@router.get("/protocols")
async def list_protocols() -> dict[str, Any]:
    """协议目录 + 配置 JSON Schema，供控制台渲染协议选择与动态表单。"""
    return {
        "ok": True,
        "protocols": protocol_catalog(),
        "config_schema": _ADAPTER_CONFIG_SCHEMA,
        "frontend_doc": "docs/protocols/frontend-integration.md",
    }


@router.get("/config")
async def get_adapter_config(request: Request) -> dict[str, Any]:
    """返回完整适配器配置（含引用 ID，不含密钥值），供动态表单回填。"""
    rt = require_runtime(request)
    adapters = getattr(rt.config, "adapters", {}) or {}
    return {
        "ok": True,
        "adapters": {
            name: cfg.model_dump() for name, cfg in adapters.items()
        },
    }


@router.put("/{name}")
async def put_adapter_config(
    request: Request,
    name: str,
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """整份替换一个已存在适配器的配置，并触发热重载。

    当前版本仍是单主适配器：只允许修改配置里已存在的 key；新增第二个
    适配器会被拒绝，避免前端误以为可以并行运行。
    """
    rt = require_runtime(request)
    host = request.app.state.host
    existing = getattr(rt.config, "adapters", {}) or {}
    if name not in existing:
        raise HTTPException(
            status_code=400,
            detail=(
                "当前版本只支持修改已存在的适配器 key；多适配器并存尚未开放。"
                f"已存在: {sorted(existing)}"
            ),
        )

    data = dict(payload)
    if not data.get("type"):
        # 旧配置默认类型，保持与 config.yaml 加载一致。
        data["type"] = "napcat"
    try:
        adapter_cfg = _ADAPTER_CONFIG_ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"适配器配置不合法：{user_error_text(exc)}",
        ) from exc

    # 先做一次“可构造性”校验：缺 token/secret、非回环无鉴权等错误在保存前
    # 就返回 400，而不是先写盘、再在热重载里失败。
    secrets = getattr(rt, "secrets", None)
    if secrets is not None:
        from adapters.registry import build_adapter

        try:
            build_adapter(
                name,
                str(getattr(adapter_cfg, "type", "onebot11")),
                cfg=adapter_cfg,
                secrets=secrets,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400,
                detail=f"适配器配置无法构造：{exc}",
            ) from exc

    def _apply(draft: Any) -> None:
        draft.adapters[name] = adapter_cfg

    try:
        apply_validated(rt.config, _apply)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"适配器配置不合法：{exc}"
        ) from exc
    await asyncio.to_thread(host.save_config)

    hot_applied = False
    reload_info: dict[str, Any] = {}
    try:
        reload_info = await rt.apply_live_settings([f"adapters.{name}"])
        hot_applied = bool(reload_info.get("hot_applied", True))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"适配器配置已保存，但热重载失败：{exc}",
        ) from exc

    return {
        "ok": True,
        "adapter": adapter_cfg.model_dump(),
        "needs_restart": not hot_applied,
        "hot_applied": hot_applied,
        "reload": reload_info,
        "message": (
            "适配器配置已保存并热重载"
            if hot_applied
            else "适配器配置已保存；热重载失败，请重启 Runtime"
        ),
    }


@router.get("/status")
async def adapter_status(request: Request) -> dict[str, Any]:
    """当前主适配器状态与能力。"""
    rt = require_runtime(request)
    adapter = getattr(rt, "adapter", None)
    config = getattr(rt, "config", None)
    adapters = getattr(config, "adapters", {}) or {}

    config_summary: dict[str, Any] = {}
    for name, cfg in adapters.items():
        config_summary[name] = {
            field: getattr(cfg, field, None) for field in _SUMMARY_FIELDS
        }

    capabilities: dict[str, bool] = {}
    if adapter is not None:
        caps = getattr(adapter, "capabilities", None)
        if is_dataclass(caps):
            capabilities = {
                key: bool(value) for key, value in asdict(caps).items()
            }

    return {
        "ok": True,
        "adapter": None
        if adapter is None
        else {
            "name": getattr(adapter, "name", ""),
            "class": type(adapter).__name__,
            "connected": bool(getattr(adapter, "is_connected", False)),
            "dialect": getattr(getattr(adapter, "dialect", None), "name", None),
            "dialect_display_name": getattr(
                getattr(adapter, "dialect", None), "display_name", None
            ),
            "preferred_voice_format": getattr(
                adapter, "preferred_voice_format", None
            ),
            "capabilities": capabilities,
        },
        "config": config_summary,
    }


__all__ = ["router"]
