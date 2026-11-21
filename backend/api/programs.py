"""外部程序配置与运行状态接口。

前端用 ``GET/PUT /api/settings/programs`` 做整表编辑；保存后由
``RuntimeHost.programs`` 热应用（新增/启用启动、删除/禁用停止）。
启动失败是运行事件而不是 HTTP 错误：``POST .../start`` 仍返回 200，
前端通过返回的 ``program.state/error`` 与全局状态弹窗提醒用户。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import ValidationError

from app_config import user_error_text
from app_config.schema import PROGRAM_ID_RE, ExternalProgramConfig
from backend.config_edit import apply_validated

from .deps import get_host, require_runtime

router = APIRouter(prefix="/api/settings/programs", tags=["programs"])

_DEFAULTS = ExternalProgramConfig().model_dump()


def _manager(request: Request) -> Any:
    manager = getattr(get_host(request), "programs", None)
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail="外部程序管理器尚未就绪；请重启 Runtime 后再试",
        )
    return manager


def _program_list(programs: dict[str, ExternalProgramConfig]) -> list[dict[str, Any]]:
    return [
        {"id": program_id, **cfg.model_dump()}
        for program_id, cfg in programs.items()
    ]


def _program_status(request: Request) -> list[dict[str, Any]]:
    manager = getattr(get_host(request), "programs", None)
    return manager.status() if manager is not None else []


def _find_status(request: Request, program_id: str) -> dict[str, Any] | None:
    for item in _program_status(request):
        if item.get("id") == program_id:
            return item
    return None


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def _parse_programs(payload: dict[str, Any]) -> dict[str, ExternalProgramConfig]:
    raw = payload.get("programs")
    if raw is None:
        raise _bad_request("缺少 programs 字段")

    entries: list[tuple[str, dict[str, Any]]] = []
    if isinstance(raw, dict):
        for program_id, item in raw.items():
            if not isinstance(item, dict):
                raise _bad_request(f"程序 {program_id!r} 的配置必须是对象")
            entries.append((str(program_id), dict(item)))
    elif isinstance(raw, list):
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise _bad_request(f"第 {index + 1} 个程序配置必须是对象")
            data = dict(item)
            program_id = str(data.pop("id", "") or "").strip()
            if not program_id:
                program_id = f"program_{uuid.uuid4().hex[:12]}"
            entries.append((program_id, data))
    else:
        raise _bad_request("programs 必须是数组或对象")

    programs: dict[str, ExternalProgramConfig] = {}
    errors: list[str] = []
    for program_id, data in entries:
        if not PROGRAM_ID_RE.fullmatch(program_id):
            errors.append(
                f"程序 ID {program_id!r} 不合法；只能使用字母、数字、下划线和"
                "短横线，且不能以符号开头"
            )
            continue
        if program_id in programs:
            errors.append(f"程序 ID 重复：{program_id}")
            continue
        try:
            cfg = ExternalProgramConfig.model_validate(data)
        except ValidationError as exc:
            errors.append(f"程序 {program_id} 配置不合法：{user_error_text(exc)}")
            continue
        if cfg.enabled and not cfg.executable.strip():
            errors.append(f"程序 {program_id} 已启用，但可执行文件为空")
            continue
        programs[program_id] = cfg

    if errors:
        raise _bad_request("\n".join(errors))
    return programs


@router.get("")
async def get_programs(request: Request) -> dict[str, Any]:
    rt = require_runtime(request)
    programs = getattr(rt.config, "programs", {}) or {}
    return {
        "ok": True,
        "programs": _program_list(programs),
        "status": _program_status(request),
        "defaults": _DEFAULTS,
    }


@router.put("")
async def put_programs(
    request: Request,
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    rt = require_runtime(request)
    host = get_host(request)
    programs = _parse_programs(payload)

    def _apply(draft: Any) -> None:
        draft.programs = programs

    apply_validated(rt.config, _apply)
    await asyncio.to_thread(host.save_config)

    manager = getattr(host, "programs", None)
    if manager is None:
        return {
            "ok": True,
            "programs": _program_list(rt.config.programs),
            "status": [],
            "started": [],
            "stopped": [],
            "errors": {},
            "needs_restart": True,
            "hot_applied": False,
            "message": "配置已保存；外部程序管理器未就绪，重启 Runtime 后生效",
        }

    try:
        result = await manager.reconcile(rt.config.programs)
    except Exception as exc:  # noqa: BLE001 - config is saved; don't 500
        return {
            "ok": True,
            "programs": _program_list(rt.config.programs),
            "status": _program_status(request),
            "started": [],
            "stopped": [],
            "errors": {"_manager": str(exc)},
            "needs_restart": True,
            "hot_applied": False,
            "message": f"配置已保存，但热应用失败：{exc}；请重启 Runtime",
        }
    errors = dict(result.get("errors") or {})
    message = "已保存并立即生效"
    if errors:
        message = f"已保存；{len(errors)} 个程序启动/停止失败，详情见状态"
    return {
        "ok": True,
        "programs": _program_list(rt.config.programs),
        "status": _program_status(request),
        "started": list(result.get("started") or []),
        "stopped": list(result.get("stopped") or []),
        "errors": errors,
        "needs_restart": False,
        "hot_applied": True,
        "message": message,
    }


@router.post("/{program_id}/start")
async def start_program(program_id: str, request: Request) -> dict[str, Any]:
    rt = require_runtime(request)
    cfg = (getattr(rt.config, "programs", {}) or {}).get(program_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"未知外部程序：{program_id}")
    if not cfg.enabled:
        raise _bad_request(f"程序 {program_id} 未启用，请先启用再启动")

    manager = _manager(request)
    item = await manager.start_one(program_id, cfg)
    started = item.get("state") in {"starting", "running", "external"}
    message = (
        f"程序 {program_id} 已启动"
        if started
        else f"程序 {program_id} 启动失败：{item.get('error') or '未知错误'}"
    )
    return {
        "ok": True,
        "started": started,
        "needs_restart": False,
        "program": item,
        "message": message,
    }


@router.post("/{program_id}/stop")
async def stop_program(program_id: str, request: Request) -> dict[str, Any]:
    rt = require_runtime(request)
    configured = program_id in (getattr(rt.config, "programs", {}) or {})
    if not configured and _find_status(request, program_id) is None:
        raise HTTPException(status_code=404, detail=f"未知外部程序：{program_id}")
    manager = _manager(request)
    result = await manager.stop_one(program_id, manual=True)
    item = _find_status(request, program_id) or result
    return {
        "ok": True,
        "stopped": bool(result.get("stopped")),
        "needs_restart": False,
        "program": item,
        "message": (
            f"程序 {program_id} 已停止"
            if result.get("stopped")
            else f"程序 {program_id} 当前没有由 KarenBot 拉起且存活的进程"
        ),
    }
