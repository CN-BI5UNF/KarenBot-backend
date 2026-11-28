"""用户数据一键备份 API。

备份 job 在后台线程执行；HTTP 只负责启动、轮询和下载。备份包含加密密钥库与
RSA 私钥，所有接口都走 WebUI 鉴权，下载响应禁止缓存。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import tempfile
import time
from functools import partial
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from core.backup import (
    BackupCoordinator,
    BackupError,
    BackupService,
    resolve_backup_directory,
)
from core.restore import RestoreError, verify_backup
from core.selective_restore import (
    CONFIRMATION_PHRASE,
    apply_restore,
    preview_restore,
    remove_files_not_in_rollback,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/backup", tags=["backup"])


class BackupStartPayload(BaseModel):
    include_media: bool | None = None
    components: list[str] | None = None


class BackupVerifyPayload(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    deep_sqlite: bool = True


class BackupRestorePayload(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    components: list[str]
    confirmation: str = Field(min_length=1, max_length=64)


class BackupRestorePreviewPayload(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    components: list[str]


class _OfflineSecrets:
    """Export existing key material without initializing or generating keys."""

    def __init__(self, paths: Any) -> None:
        self.paths = paths

    def export_rsa_private_key(self) -> str:
        from app_config import SecretsManager

        return SecretsManager(self.paths)._load_rsa_private_pem() or ""


def build_manager(host: Any, rt: Any | None = None) -> BackupService:
    runtime = rt if rt is not None else getattr(host, "rt", None)
    cached = getattr(host, "_backup_service_cache", None)
    if runtime is None:
        if cached is not None and getattr(host, "_backup_active_restore", None):
            return cached[1]
        from yaml import safe_load

        paths = host.build_paths()
        try:
            raw = safe_load(Path(paths.CONFIG_FILE).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - offline restore remains available with the default directory
            raw = None
        backup_cfg = raw.get("backup") if isinstance(raw, dict) else None
        backup_cfg = backup_cfg if isinstance(backup_cfg, dict) else {}
        directory = resolve_backup_directory(backup_cfg.get("directory"))
        include_media = backup_cfg.get("include_media") is not False
    else:
        paths = getattr(runtime, "paths", None)
        backup_cfg = getattr(getattr(runtime, "config", None), "backup", None)
        directory = resolve_backup_directory(getattr(backup_cfg, "directory", ""))
        include_media = bool(getattr(backup_cfg, "include_media", True))
    if paths is None:
        raise HTTPException(status_code=503, detail="Runtime 尚未就绪")
    key = (
        str(getattr(runtime, "project_root", host.project_root)),
        str(paths.DATA_DIR),
        str(directory),
        include_media,
        id(runtime),
        id(getattr(runtime, "secrets", None)),
    )
    if cached is not None and cached[0] == key:
        return cached[1]

    root = str(getattr(runtime, "project_root", host.project_root).resolve())
    coordinators = getattr(host, "_backup_coordinators", None)
    if coordinators is None:
        coordinators = {}
        host._backup_coordinators = coordinators
    coord_key = (root, str(paths.DATA_DIR.resolve()))
    coordinator = coordinators.get(coord_key)
    if coordinator is None:
        coordinator = BackupCoordinator(
            getattr(runtime, "project_root", host.project_root), paths
        )
        coordinators[coord_key] = coordinator
    manager = BackupService(
        getattr(runtime, "project_root", host.project_root),
        paths,
        backup_dir=directory,
        include_media=include_media,
        secrets_manager=(getattr(runtime, "secrets", None) if runtime is not None else _OfflineSecrets(paths)),
        coordinator=coordinator,
    )
    host._backup_service_cache = (key, manager)
    return manager


def _manager(request: Request) -> BackupService:
    return build_manager(request.app.state.host)


def _http_error(exc: BackupError) -> HTTPException:
    if exc.code == "not_found":
        status = 404
    elif exc.code in {
        "invalid_name", "invalid_directory", "unsafe_directory",
        "not_cancellable", "invalid_components", "component_not_restorable",
    }:
        status = 400
    else:
        # backup_running / insufficient_space / space_check_failed / lock
        status = 409
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": exc.message},
    )


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    host = request.app.state.host
    payload = await asyncio.to_thread(_manager(request).status)
    active_id = getattr(host, "_backup_active_restore", None)
    payload["active_restore"] = _restore_job_snapshot(host, active_id) if active_id else None
    return payload


@router.post("")
async def start(
    request: Request,
    payload: Annotated[BackupStartPayload | None, Body()] = None,
) -> dict[str, Any]:
    host = request.app.state.host
    async with _action_lock(host):
        manager = _manager(request)
        include_media = payload.include_media if payload else None
        components = payload.components if payload else None
        try:
            _ensure_no_restore(host)
            if getattr(host, "_admin_operation", None):
                raise BackupError("admin_operation_running", "管理操作进行中，暂不能创建备份")
            return await manager.start(include_media=include_media, components=components)
        except BackupError as exc:
            raise _http_error(exc) from exc


@router.get("/jobs")
async def jobs(request: Request) -> dict[str, Any]:
    data = _manager(request).status()
    return {"jobs": data["jobs"], "running": data["running"]}


@router.get("/jobs/{job_id}")
async def job(request: Request, job_id: str) -> dict[str, Any]:
    try:
        return _manager(request).get_job(job_id)
    except BackupError as exc:
        raise _http_error(exc) from exc


@router.post("/jobs/{job_id}/cancel")
async def cancel(request: Request, job_id: str) -> dict[str, Any]:
    try:
        return await _manager(request).cancel(job_id)
    except BackupError as exc:
        raise _http_error(exc) from exc


@router.post("/verify")
async def verify(request: Request, payload: BackupVerifyPayload) -> JSONResponse:
    manager = _manager(request)
    try:
        path = manager.resolve_download(payload.filename)
    except BackupError as exc:
        raise _http_error(exc) from exc
    report = await asyncio.to_thread(
        verify_backup, path, deep_sqlite=payload.deep_sqlite
    )
    return JSONResponse(content=report, headers={"Cache-Control": "no-store"})


@router.get("/components")
async def components(request: Request, include_media: bool | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(_manager(request).component_status, include_media=include_media)


def _ensure_no_restore(host: Any) -> None:
    if getattr(host, "_backup_active_restore", None) is not None:
        raise BackupError("restore_running", "已有恢复任务在进行中")


def _restore_job_snapshot(host: Any, job_id: str) -> dict[str, Any]:
    jobs = getattr(host, "_backup_restore_jobs", {})
    job = jobs.get(job_id)
    if job is None:
        raise BackupError("not_found", "恢复任务不存在")
    return dict(job)


def _restore_update(host: Any, job_id: str, **fields: Any) -> None:
    job = getattr(host, "_backup_restore_jobs", {}).get(job_id)
    if job is None:
        return
    job.update(fields)
    job["updated_at"] = time.time()


def _restore_import_private_key(manager: BackupService, path: Any, *, required: bool) -> bool:
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            pem = archive.read("data/rsa_private.pem").decode("ascii")
    except (OSError, KeyError, UnicodeError, zipfile.BadZipFile):
        if required:
            raise BackupError(
                "credential_restore_failed", "所选凭据包缺少可读取的 RSA 私钥"
            ) from None
        return False
    try:
        from app_config import SecretsManager

        SecretsManager(manager.paths).import_rsa_private_key(pem)
        return True
    except Exception as exc:  # noqa: BLE001
        raise BackupError("credential_restore_failed", f"RSA 私钥导入失败：{exc}") from exc


async def _write_in_thread(callback: Any, *args: Any, **kwargs: Any) -> Any:
    """Finish a filesystem transaction before cancellation can start rollback."""
    task = asyncio.create_task(asyncio.to_thread(callback, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def _run_restore_job(
    *, host: Any, manager: BackupService, was_running: bool,
    job_id: str, source_path: Path, components: list[str], expected_sha256: str,
) -> None:
    rollback_path = None
    source_copy = None
    copied_path = source_path
    writes_started = False
    rollback_failed = False
    loop = asyncio.get_running_loop()
    try:
        _restore_update(host, job_id, state="running", stage="rollback_snapshot", progress=0)
        rollback_job = await manager.start(components=components, include_media=True, _rollback=True)
        rollback_result = await manager.wait(rollback_job["id"], timeout=3600)
        if rollback_result.get("state") not in {"done", "partial"}:
            raise BackupError("rollback_snapshot_failed", "恢复前安全快照失败，已取消恢复")
        rollback_path = manager.resolve_download(str(rollback_result.get("filename") or ""))
        rollback_report = await asyncio.to_thread(verify_backup, rollback_path, deep_sqlite=True)
        # Missing current files are legitimate (including a brand-new category).
        # A snapshot that skipped or failed to read existing data is unsafe.
        credential_files = next(
            (item.get("files", 0) for item in rollback_report.get("components", []) if item["id"] == "credentials"), 0,
        )
        unsafe_warnings = [
            warning for warning in rollback_result.get("warnings", [])
            if credential_files or not warning.startswith("未找到 RSA 私钥")
        ]
        if not rollback_report.get("ok") or unsafe_warnings:
            raise BackupError("rollback_snapshot_failed", "恢复前快照不完整，已取消恢复")
        _restore_update(host, job_id, rollback_filename=rollback_path.name,
                        rollback_size=rollback_result.get("size", 0), stage="verifying")
        source_copy = tempfile.TemporaryDirectory(prefix=".restore-source-", dir=manager.directory)
        copied_path = Path(source_copy.name) / source_path.name
        await _write_in_thread(shutil.copyfile, source_path, copied_path)
        target_summary = await asyncio.to_thread(preview_restore, copied_path, manager.paths, components)
        if target_summary["backup_sha256"] != expected_sha256:
            raise BackupError("archive_changed", "备份文件在预览后发生变化，请重新预览")
        if not all(item.get("restorable") for item in target_summary["components"]):
            raise BackupError("component_not_restorable", "所选内容无法安全恢复")
        progress_state = {item["id"]: {"bytes": 0, "files": 0,
                                      "archive_bytes": item["archive_bytes"]}
                          for item in target_summary["components"]}
        processed = 0

        def report_progress(component_id: str, size: int, files: int) -> None:
            nonlocal processed
            processed += size
            progress_state[component_id]["files"] = files
            progress_state[component_id]["bytes"] += size
            loop.call_soon_threadsafe(partial(
                _restore_update, host, job_id, progress=processed, processed_bytes=processed,
                total_bytes=target_summary["total_archive_bytes"],
                component_progress={key: dict(value) for key, value in progress_state.items()},
            ))

        _restore_update(host, job_id, stage="restoring", component_progress=progress_state)
        writes_started = True
        result = await _write_in_thread(apply_restore, copied_path, manager.paths,
                                        components, progress=report_progress)
        if "credentials" in components:
            await _write_in_thread(_restore_import_private_key, manager, copied_path, required=True)
        _restore_update(host, job_id, stage="restarting", result=result)
        restarted = await host.start(bypass_admin_guard=True) if was_running else False
        if was_running and not restarted:
            raise BackupError("runtime_start_failed", "恢复后的 Runtime 启动失败，将自动回滚")
        _restore_update(host, job_id, state="done", stage="done", finished_at=time.time(),
                        runtime_restarted=bool(restarted), result=result,
                        warnings=target_summary["warnings"])
    except BaseException as exc:  # noqa: BLE001 - rollback also covers cancellation
        code = str(getattr(exc, "code", "restore_failed"))
        message = str(exc)[:500] or type(exc).__name__
        if writes_started and rollback_path is not None:
            try:
                await host.stop()
                await _write_in_thread(remove_files_not_in_rollback, copied_path, rollback_path,
                                        manager.paths, components)
                await _write_in_thread(apply_restore, rollback_path, manager.paths, components, rollback=True)
                if "credentials" in components:
                    await _write_in_thread(_restore_import_private_key, manager, rollback_path, required=False)
                _restore_update(host, job_id, rolled_back=True)
                message += "；已回滚原数据"
            except BaseException as rollback_error:  # noqa: BLE001
                rollback_failed = True
                code = "rollback_failed"
                message += f"；自动回滚失败：{rollback_error}"
                logger.exception("备份恢复与回滚均失败")
        if was_running and not rollback_failed and getattr(host, "state", "") != "running":
            try:
                restarted = await host.start(bypass_admin_guard=True)
                _restore_update(host, job_id, runtime_restarted=bool(restarted))
                if not restarted:
                    message += "；Runtime 未能重新启动"
            except Exception as restart_error:  # noqa: BLE001
                message += f"；Runtime 重启失败：{restart_error}"
        _restore_update(host, job_id, state="failed", stage="failed", finished_at=time.time(),
                        error={"code": code, "message": message[:1000]},
                        rollback_filename=rollback_path.name if rollback_path else None)
    finally:
        if source_copy is not None:
            try:
                source_copy.cleanup()
            except OSError:
                logger.warning("恢复源临时副本清理失败", exc_info=True)
        if not rollback_failed:
            host.end_admin_operation()
        if getattr(host, "_backup_active_restore", None) == job_id:
            host._backup_active_restore = None


@router.post("/restore/preview")
async def restore_preview(request: Request, payload: BackupRestorePreviewPayload) -> JSONResponse:
    manager = _manager(request)
    try:
        path = manager.resolve_download(payload.filename)
        report = await asyncio.to_thread(preview_restore, path, manager.paths, payload.components)
    except BackupError as exc:
        raise _http_error(exc) from exc
    except RestoreError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message}) from exc
    return JSONResponse(content=report, headers={"Cache-Control": "no-store"})


def _action_lock(host: Any) -> asyncio.Lock:
    lock = getattr(host, "_backup_action_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        host._backup_action_lock = lock
    return lock


@router.post("/restore")
async def restore(request: Request, payload: BackupRestorePayload) -> dict[str, Any]:
    if payload.confirmation != CONFIRMATION_PHRASE:
        raise HTTPException(status_code=400, detail={
            "code": "confirmation_required", "message": f"请输入确认短语：{CONFIRMATION_PHRASE}",
        })
    host = request.app.state.host
    async with _action_lock(host):
        manager = _manager(request)
        try:
            _ensure_no_restore(host)
            if (await asyncio.to_thread(manager.status)).get("running"):
                raise BackupError("backup_running", "备份任务运行中，请完成后再恢复")
            source_path = manager.resolve_download(payload.filename)
            preview = await asyncio.to_thread(preview_restore, source_path, manager.paths, payload.components)
            if not all(item.get("restorable") for item in preview["components"]):
                raise BackupError("component_not_restorable", "备份内所选内容无法安全恢复")
            was_running = await host.pause_for_admin("backup_restore")
        except BackupError as exc:
            raise _http_error(exc) from exc
        except RestoreError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message}) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
        jobs = getattr(host, "_backup_restore_jobs", None)
        if jobs is None:
            jobs = {}
            host._backup_restore_jobs = jobs
        while len(jobs) >= 50:
            jobs.pop(next(iter(jobs)))
        job = {
            "id": job_id, "operation": "restore", "state": "queued", "stage": "queued",
            "filename": payload.filename, "components": list(payload.components),
            "progress": 0, "processed_bytes": 0, "total_bytes": preview["total_archive_bytes"],
            "started_at": time.time(), "finished_at": None, "warnings": preview["warnings"],
            "rollback_filename": None, "runtime_was_running": was_running,
            "component_progress": {item["id"]: {"bytes": 0, "files": 0,
                                               "archive_bytes": item["archive_bytes"]}
                                   for item in preview["components"]},
        }
        jobs[job_id] = job
        host._backup_active_restore = job_id
        task = asyncio.create_task(_run_restore_job(
            host=host, manager=manager, was_running=was_running, job_id=job_id,
            source_path=source_path, components=payload.components, expected_sha256=preview["backup_sha256"],
        ), name=f"backup-restore-{job_id}")
        tasks = getattr(host, "_backup_restore_tasks", None)
        if tasks is None:
            tasks = set()
            host._backup_restore_tasks = tasks
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return dict(job)


@router.get("/restore/jobs/{job_id}")
async def restore_job(request: Request, job_id: str) -> dict[str, Any]:
    try:
        return _restore_job_snapshot(request.app.state.host, job_id)
    except BackupError as exc:
        raise _http_error(exc) from exc


@router.get("/files/{name:path}")
async def download(request: Request, name: str) -> FileResponse:
    manager = _manager(request)
    try:
        path = manager.resolve_download(name)
    except BackupError as exc:
        raise _http_error(exc) from exc
    return FileResponse(
        path,
        media_type="application/zip",
        filename=path.name,
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["build_manager", "router"]
