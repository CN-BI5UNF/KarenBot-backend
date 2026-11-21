"""用户自定义外部程序的进程托管。

设计约束（与产品需求一一对应）：

- 任何一个程序启动失败都只写 ERROR 日志并更新状态，不能让 Runtime 启动失败。
- 只有 ``create_subprocess_exec`` 成功返回后才记录进程，关闭时也只停止
  KarenBot 真正拉起且仍存活的进程；失败项、探测命中项、外部已运行项永不停。
- ``stop_with_karen`` 只决定 KarenBot 关闭时的行为；用户显式删除/禁用某个
  程序时，由 KarenBot 拉起的进程会被立即停止。
- 进程输出统一转发到标准 logger，带 ``[program:<name>]`` 前缀。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app_config.schema import ExternalProgramConfig

logger = logging.getLogger(__name__)

STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_RESTARTING = "restarting"
STATE_EXTERNAL = "external"
STATE_FAILED = "failed"
STATE_EXITED = "exited"
STATE_STOPPING = "stopping"
STATE_DISABLED = "disabled"
STATE_LAUNCHER_EXITED = "launcher_exited"


@dataclass
class _ProgramRuntime:
    program_id: str
    config: ExternalProgramConfig
    process: asyncio.subprocess.Process | None = None
    supervisor_task: asyncio.Task | None = None
    log_task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    state: str = STATE_STOPPED
    started_by_karen: bool = False
    external: bool = False
    started_at: float = 0.0
    last_exit_code: int | None = None
    last_error: str = ""
    failure_seq: int = 0
    failure_active: bool = False
    restarts: int = 0
    manual_stopped: bool = False
    launch_signature: tuple[Any, ...] | None = None


def _is_alive(process: asyncio.subprocess.Process | None) -> bool:
    return process is not None and process.returncode is None


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _is_windows_command_launcher(cfg: ExternalProgramConfig) -> bool:
    return _is_windows() and Path(cfg.executable).suffix.lower() in {
        ".bat",
        ".cmd",
    }


def _clean_executable(value: str) -> str:
    text = os.path.expandvars(os.path.expanduser(str(value).strip()))
    while len(text) >= 2 and (
        (text[0] == text[-1] and text[0] in {'"', "'"})
        or (text[0] in {"“", "‘"} and text[-1] in {"”", "’"})
    ):
        text = text[1:-1].strip()
    return text


def _launch_signature(cfg: ExternalProgramConfig) -> tuple[Any, ...]:
    return (
        cfg.executable,
        tuple(cfg.args),
        cfg.cwd,
        tuple(sorted(cfg.env.items())),
        cfg.startup_wait_seconds,
    )


class ExternalProgramManager:
    """Own multiple user-defined external programs for one RuntimeHost."""

    def __init__(
        self,
        project_root: Path,
        *,
        graceful_shutdown_timeout: float = 5.0,
        restart_delay: float = 3.0,
    ) -> None:
        self.project_root = Path(project_root)
        self.graceful_shutdown_timeout = max(1.0, float(graceful_shutdown_timeout))
        self.restart_delay = max(0.0, float(restart_delay))
        self._configs: dict[str, ExternalProgramConfig] = {}
        self._runtimes: dict[str, _ProgramRuntime] = {}
        self._failure_seq = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def start_all(
        self, configs: Mapping[str, ExternalProgramConfig]
    ) -> dict[str, Any]:
        """按配置启动所有程序；任何单项失败都不会抛出。"""
        try:
            return await self.reconcile(configs, start_missing=True)
        except Exception:  # noqa: BLE001 - must never break Runtime startup
            logger.exception("外部程序批量启动失败")
            return {"started": [], "stopped": [], "errors": {}}

    async def reconcile(
        self,
        configs: Mapping[str, ExternalProgramConfig],
        *,
        start_missing: bool = False,
    ) -> dict[str, Any]:
        """Hot-apply a new program list without restarting Runtime."""
        normalized, errors = self._normalize_configs(configs)
        old_configs = dict(self._configs)
        old_ids = set(old_configs)
        new_ids = set(normalized)
        started: list[str] = []
        stopped: list[str] = []
        self._configs = normalized

        for program_id in sorted(old_ids - new_ids):
            runtime = self._runtimes.get(program_id)
            if runtime is None:
                continue
            try:
                result = await self.stop_one(program_id, manual=True)
                if result.get("stopped"):
                    stopped.append(program_id)
                if result.get("error"):
                    errors[program_id] = str(result["error"])
            except Exception as exc:  # noqa: BLE001
                errors[program_id] = str(exc)
                continue
            if result.get("state") in {STATE_STOPPED, STATE_EXTERNAL}:
                self._runtimes.pop(program_id, None)

        for program_id, cfg in normalized.items():
            runtime = self._runtimes.get(program_id)
            if runtime is None:
                runtime = _ProgramRuntime(program_id=program_id, config=cfg)
                self._runtimes[program_id] = runtime
            old_cfg = old_configs.get(program_id)
            runtime.config = cfg

            if not cfg.enabled:
                if runtime.started_by_karen and _is_alive(runtime.process):
                    try:
                        result = await self.stop_one(program_id, manual=True)
                        if result.get("stopped"):
                            stopped.append(program_id)
                        if result.get("error"):
                            errors[program_id] = str(result["error"])
                            continue
                    except Exception as exc:  # noqa: BLE001
                        errors[program_id] = str(exc)
                        continue
                runtime.state = STATE_DISABLED
                runtime.last_error = ""
                runtime.launch_signature = _launch_signature(cfg)
                continue

            signature = _launch_signature(cfg)
            signature_changed = (
                runtime.launch_signature is not None
                and runtime.launch_signature != signature
            )
            alive = runtime.started_by_karen and _is_alive(runtime.process)

            if alive and signature_changed:
                try:
                    result = await self.stop_one(program_id, manual=True)
                    if result.get("stopped"):
                        stopped.append(program_id)
                    if result.get("error"):
                        errors[program_id] = str(result["error"])
                        continue
                except Exception as exc:  # noqa: BLE001
                    errors[program_id] = str(exc)
                    continue
                alive = False

            if alive:
                # 只更新名称/关闭策略/自动重启等非启动字段。
                continue

            if runtime.external:
                # 端口上已有外部实例；不接管、不停止。完整启动时重新探测，
                # 外部实例已经不在了才改由 KarenBot 拉起。
                if start_missing and cfg.probe_port and not await self._probe(
                    cfg.probe_host, cfg.probe_port
                ):
                    runtime.external = False
                    runtime.state = STATE_STOPPED
                else:
                    continue

            newly_enabled = old_cfg is not None and not old_cfg.enabled
            is_new = old_cfg is None
            # 失败/手动停止的旧条目不在每次保存时自动重试；只有完整启动
            # （start_missing）、新增、重新启用或启动配置变化才重启。
            should_start = (
                start_missing or is_new or newly_enabled or signature_changed
            )

            if not should_start:
                continue

            runtime.launch_signature = signature
            status = await self.start_one(program_id, cfg)
            if status.get("state") in {
                STATE_STARTING,
                STATE_RUNNING,
                STATE_EXTERNAL,
            }:
                started.append(program_id)
            elif status.get("error"):
                errors[program_id] = str(status["error"])

        return {"started": started, "stopped": stopped, "errors": errors}

    async def start_one(
        self,
        program_id: str,
        cfg: ExternalProgramConfig | None = None,
    ) -> dict[str, Any]:
        """Start one program; startup failures are returned, never raised."""
        cfg = cfg or self._configs.get(program_id)
        if cfg is None:
            return {"id": program_id, "state": STATE_FAILED, "error": "程序未配置"}
        runtime = self._runtimes.get(program_id)
        if runtime is None:
            runtime = _ProgramRuntime(program_id=program_id, config=cfg)
            self._runtimes[program_id] = runtime
        runtime.config = cfg
        runtime.launch_signature = _launch_signature(cfg)

        async with runtime.lock:
            if not cfg.enabled:
                runtime.state = STATE_DISABLED
                return self._status_item(program_id)
            if runtime.started_by_karen and _is_alive(runtime.process):
                return self._status_item(program_id)
            runtime.manual_stopped = False
            runtime.external = False
            runtime.stop_event.clear()
            runtime.last_exit_code = None
            runtime.last_error = ""
            runtime.state = STATE_STARTING

            if cfg.probe_port and await self._probe(
                cfg.probe_host, cfg.probe_port
            ):
                runtime.state = STATE_EXTERNAL
                runtime.external = True
                runtime.started_by_karen = False
                runtime.process = None
                logger.info(
                    "外部程序 %s 的探测端口 %s:%s 已有服务，跳过启动",
                    cfg.name or program_id,
                    cfg.probe_host,
                    cfg.probe_port,
                )
                return self._status_item(program_id)

            try:
                process = await self._spawn(runtime)
            except Exception as exc:  # noqa: BLE001 - per-program isolation
                self._record_failure(runtime, f"启动失败：{exc}")
                return self._status_item(program_id)

            runtime.process = process
            runtime.started_by_karen = True
            runtime.started_at = time.time()
            runtime.state = STATE_STARTING
            if process.stdout is not None:
                runtime.log_task = asyncio.create_task(
                    self._pipe_logs(runtime, process.stdout),
                    name=f"external-program-log-{program_id}",
                )
            runtime.supervisor_task = asyncio.create_task(
                self._supervise(runtime, process),
                name=f"external-program-supervisor-{program_id}",
            )
            logger.info(
                "外部程序 %s 已启动 pid=%s",
                cfg.name or program_id,
                process.pid,
            )
            return self._status_item(program_id)

    async def stop_one(
        self,
        program_id: str,
        *,
        manual: bool = True,
    ) -> dict[str, Any]:
        """Stop one program if KarenBot started it; never raises."""
        runtime = self._runtimes.get(program_id)
        if runtime is None:
            return {"id": program_id, "state": STATE_STOPPED, "stopped": False}
        async with runtime.lock:
            if manual:
                runtime.manual_stopped = True
            runtime.stop_event.set()
            process = runtime.process
            if not runtime.started_by_karen or not _is_alive(process):
                await self._cancel_tasks(runtime)
                runtime.process = None
                runtime.started_by_karen = False
                runtime.state = (
                    STATE_STOPPED if not runtime.external else STATE_EXTERNAL
                )
                return {
                    "id": program_id,
                    "state": runtime.state,
                    "stopped": False,
                }

            runtime.state = STATE_STOPPING
            logger.info(
                "正在停止外部程序 %s pid=%s",
                runtime.config.name or program_id,
                process.pid,
            )
            try:
                ok = await self._terminate(runtime, process)
            except Exception as exc:  # noqa: BLE001 - stop must not explode
                logger.exception(
                    "停止外部程序 %s 时异常",
                    runtime.config.name or program_id,
                )
                runtime.state = STATE_FAILED
                runtime.last_error = f"停止失败：{exc}"
                return {
                    "id": program_id,
                    "state": runtime.state,
                    "stopped": False,
                    "error": runtime.last_error,
                }
            if not ok:
                runtime.state = STATE_FAILED
                runtime.last_error = "进程未能在超时内退出"
                logger.error(
                    "外部程序 %s 停止失败 pid=%s",
                    runtime.config.name or program_id,
                    process.pid,
                )
                return {
                    "id": program_id,
                    "state": runtime.state,
                    "stopped": False,
                    "error": runtime.last_error,
                }

            runtime.process = None
            runtime.started_by_karen = False
            runtime.state = STATE_STOPPED
            await self._cancel_tasks(runtime)
            logger.info(
                "外部程序 %s 已停止", runtime.config.name or program_id
            )
            return {"id": program_id, "state": runtime.state, "stopped": True}

    async def stop_all(self) -> None:
        """Stop only process this manager started and policy says to stop."""
        targets = [
            runtime
            for runtime in list(self._runtimes.values())
            if runtime.config.stop_with_karen
            and (runtime.started_by_karen or runtime.supervisor_task is not None)
        ]
        if not targets:
            return
        results = await asyncio.gather(
            *(self.stop_one(runtime.program_id, manual=True) for runtime in targets),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("停止外部程序时出现异常: %s", result)

    def status(self) -> list[dict[str, Any]]:
        """Pure in-memory status; safe to call from the 2s status poll."""
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for program_id in self._configs:
            seen.add(program_id)
            items.append(self._status_item(program_id))
        for program_id in self._runtimes:
            if program_id not in seen:
                items.append(self._status_item(program_id))
        return items

    def fingerprint(self) -> str:
        parts = []
        for item in self.status():
            parts.append(
                f"{item.get('id')}:{item.get('state')}:"
                f"{item.get('failure_seq')}:{item.get('pid')}"
            )
        return ";".join(parts)

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def _normalize_configs(
        self, configs: Mapping[str, ExternalProgramConfig]
    ) -> tuple[dict[str, ExternalProgramConfig], dict[str, str]]:
        normalized: dict[str, ExternalProgramConfig] = {}
        errors: dict[str, str] = {}
        for program_id, cfg in dict(configs or {}).items():
            try:
                normalized[str(program_id)] = (
                    cfg
                    if isinstance(cfg, ExternalProgramConfig)
                    else ExternalProgramConfig.model_validate(cfg)
                )
            except Exception as exc:  # noqa: BLE001
                errors[str(program_id)] = str(exc)
        return normalized, errors

    def _status_item(self, program_id: str) -> dict[str, Any]:
        cfg = self._configs.get(program_id)
        runtime = self._runtimes.get(program_id)
        if runtime is not None:
            cfg = cfg or runtime.config
        if cfg is None and runtime is not None:
            cfg = runtime.config
        if cfg is None:
            cfg = ExternalProgramConfig()
        alive = bool(
            runtime and runtime.started_by_karen and _is_alive(runtime.process)
        )
        state = runtime.state if runtime is not None else (
            STATE_DISABLED if not cfg.enabled else STATE_STOPPED
        )
        return {
            "id": program_id,
            "name": cfg.name or program_id,
            "enabled": bool(cfg.enabled),
            "configured": program_id in self._configs,
            "state": state,
            "pid": (
                runtime.process.pid
                if alive and runtime is not None and runtime.process is not None
                else None
            ),
            "managed": alive,
            "started_at": runtime.started_at if runtime is not None else 0.0,
            "exit_code": runtime.last_exit_code if runtime is not None else None,
            "error": runtime.last_error if runtime is not None else "",
            "failure_seq": runtime.failure_seq if runtime is not None else 0,
            "restarts": runtime.restarts if runtime is not None else 0,
            "stop_with_karen": bool(cfg.stop_with_karen),
            "auto_restart": bool(cfg.auto_restart),
            "probe_port": int(cfg.probe_port),
        }

    def _record_failure(
        self, runtime: _ProgramRuntime, message: str
    ) -> None:
        runtime.state = STATE_FAILED
        runtime.last_error = message
        if not runtime.failure_active:
            runtime.failure_active = True
            self._failure_seq += 1
            runtime.failure_seq = self._failure_seq
        logger.error(
            "外部程序 %s 启动/运行失败：%s",
            runtime.config.name or runtime.program_id,
            message,
        )

    async def _spawn(
        self, runtime: _ProgramRuntime
    ) -> asyncio.subprocess.Process:
        cfg = runtime.config
        executable = _clean_executable(cfg.executable)
        if not executable:
            raise RuntimeError("未配置可执行文件")

        resolved = self._resolve_executable(executable)
        cwd = self._resolve_cwd(cfg, resolved)
        env = os.environ.copy()
        for key, value in cfg.env.items():
            env[str(key)] = os.path.expandvars(str(value))

        argv = [str(resolved), *cfg.args]
        if _is_windows_command_launcher(cfg):
            comspec = os.environ.get("COMSPEC") or "cmd.exe"
            argv = [comspec, "/c", str(resolved), *cfg.args]

        kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "env": env,
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
        }
        if _is_windows():
            creationflags = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
            if creationflags:
                kwargs["creationflags"] = creationflags
        else:
            kwargs["start_new_session"] = True

        logger.info(
            "启动外部程序 %s: %s (cwd=%s)",
            cfg.name or runtime.program_id,
            argv,
            cwd,
        )
        return await asyncio.create_subprocess_exec(*argv, **kwargs)

    def _resolve_executable(self, executable: str) -> Path:
        candidate = Path(executable)
        has_separator = (
            os.sep in executable
            or (os.altsep is not None and os.altsep in executable)
            or "/" in executable
            or "\\" in executable
        )
        if has_separator:
            if not candidate.is_absolute():
                candidate = self.project_root / candidate
            if not candidate.is_file():
                raise FileNotFoundError(f"可执行文件不存在: {candidate}")
            return candidate
        found = shutil.which(executable)
        if not found:
            raise FileNotFoundError(f"找不到命令: {executable}")
        return Path(found)

    def _resolve_cwd(
        self, cfg: ExternalProgramConfig, executable: Path
    ) -> Path:
        if cfg.cwd:
            cwd = Path(
                os.path.expandvars(os.path.expanduser(cfg.cwd.strip()))
            )
            if not cwd.is_absolute():
                cwd = self.project_root / cwd
            if not cwd.is_dir():
                raise NotADirectoryError(f"工作目录不存在: {cwd}")
            return cwd
        if executable.parent != Path("."):
            return executable.parent
        return self.project_root

    async def _probe(self, host: str, port: int) -> bool:
        if host in {"0.0.0.0", "::", "[::]"}:
            host = "127.0.0.1"
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, int(port)), timeout=0.8
            )
        except (OSError, asyncio.TimeoutError, ValueError):
            return False
        writer.close()
        with contextlib.suppress(OSError, asyncio.TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
        return True

    async def _pipe_logs(
        self,
        runtime: _ProgramRuntime,
        stream: asyncio.StreamReader,
    ) -> None:
        label = runtime.config.name or runtime.program_id
        while True:
            try:
                line = await stream.readline()
            except (ValueError, asyncio.CancelledError):
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.info("[program:%s] %s", label, text)

    async def _supervise(
        self,
        runtime: _ProgramRuntime,
        process: asyncio.subprocess.Process,
    ) -> None:
        try:
            first = True
            while True:
                wait = runtime.config.startup_wait_seconds if first else 0.0
                code, exited_early = await self._wait_for_exit(
                    runtime, process, wait=wait
                )
                first = False

                if runtime.stop_event.is_set() or runtime.manual_stopped:
                    runtime.state = STATE_STOPPED
                    break

                if exited_early and code == 0 and _is_windows_command_launcher(
                    runtime.config
                ):
                    runtime.state = STATE_LAUNCHER_EXITED
                    runtime.last_exit_code = code
                    logger.warning(
                        "外部程序 %s 是一次性启动器（.bat/.cmd），"
                        "启动器已退出；KarenBot 无法再跟踪其子进程",
                        runtime.config.name or runtime.program_id,
                    )
                    break

                runtime.last_exit_code = code
                if exited_early:
                    self._record_failure(
                        runtime, f"进程启动后立即退出（code {code}）"
                    )
                elif code == 0:
                    runtime.state = STATE_EXITED
                    logger.warning(
                        "外部程序 %s 已退出（code 0）",
                        runtime.config.name or runtime.program_id,
                    )
                else:
                    self._record_failure(
                        runtime, f"进程异常退出（code {code}）"
                    )

                if not runtime.config.auto_restart or runtime.manual_stopped:
                    break

                # 自动重启：反复 spawn 失败时按 restart_delay 重试，但不再
                # 去 wait 已经退出的旧进程。
                restarted = False
                while not restarted:
                    if runtime.stop_event.is_set() or runtime.manual_stopped:
                        runtime.state = STATE_STOPPED
                        return
                    if not runtime.config.auto_restart:
                        runtime.state = STATE_FAILED
                        return
                    runtime.state = STATE_RESTARTING
                    runtime.restarts += 1
                    if await self._sleep_or_stop(runtime, self.restart_delay):
                        runtime.state = STATE_STOPPED
                        return
                    try:
                        process = await self._spawn(runtime)
                    except Exception as exc:  # noqa: BLE001
                        self._record_failure(runtime, f"自动重启失败：{exc}")
                        continue
                    runtime.process = process
                    runtime.started_by_karen = True
                    runtime.started_at = time.time()
                    runtime.last_exit_code = None
                    if process.stdout is not None:
                        runtime.log_task = asyncio.create_task(
                            self._pipe_logs(runtime, process.stdout),
                            name=f"external-program-log-{runtime.program_id}",
                        )
                    logger.info(
                        "外部程序 %s 已自动重启 pid=%s",
                        runtime.config.name or runtime.program_id,
                        process.pid,
                    )
                    restarted = True
                    first = True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - supervisor must not die silently
            logger.exception(
                "外部程序 %s 监控循环异常",
                runtime.config.name or runtime.program_id,
            )
        finally:
            runtime.supervisor_task = None
            if runtime.process is process and process.returncode is not None:
                runtime.process = None

    async def _wait_for_exit(
        self,
        runtime: _ProgramRuntime,
        process: asyncio.subprocess.Process,
        *,
        wait: float,
    ) -> tuple[int, bool]:
        if wait > 0:
            try:
                code = await asyncio.wait_for(process.wait(), timeout=wait)
                return int(code or 0), True
            except asyncio.TimeoutError:
                pass
        if runtime.state == STATE_STARTING:
            runtime.state = STATE_RUNNING
            runtime.failure_active = False
            runtime.last_error = ""
            logger.info(
                "外部程序 %s 已就绪 pid=%s",
                runtime.config.name or runtime.program_id,
                process.pid,
            )
        code = await process.wait()
        return int(code or 0), False

    async def _sleep_or_stop(
        self, runtime: _ProgramRuntime, delay: float
    ) -> bool:
        if delay <= 0:
            return runtime.stop_event.is_set()
        try:
            await asyncio.wait_for(runtime.stop_event.wait(), timeout=delay)
            return True
        except asyncio.TimeoutError:
            return False

    async def _terminate(
        self,
        runtime: _ProgramRuntime,
        process: asyncio.subprocess.Process,
    ) -> bool:
        if _is_windows():
            await self._taskkill(process.pid, force=False, runtime=runtime)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)

        try:
            await asyncio.wait_for(
                process.wait(), timeout=self.graceful_shutdown_timeout
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "外部程序 %s 未在 %.1fs 内退出，强制结束",
                runtime.config.name or runtime.program_id,
                self.graceful_shutdown_timeout,
            )

        if _is_windows():
            await self._taskkill(process.pid, force=True, runtime=runtime)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
            return True
        except asyncio.TimeoutError:
            return False

    async def _taskkill(
        self,
        pid: int,
        *,
        force: bool,
        runtime: _ProgramRuntime,
    ) -> None:
        command = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            command.insert(1, "/F")
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            text = out.decode("utf-8", errors="replace").strip()
            if proc.returncode not in (0, 128):
                logger.warning(
                    "taskkill 外部程序 %s 失败 rc=%s: %s",
                    runtime.config.name or runtime.program_id,
                    proc.returncode,
                    text,
                )
        except OSError as exc:
            logger.warning("无法执行 taskkill，退回 terminate(): %s", exc)
            if runtime.process is not None:
                with contextlib.suppress(ProcessLookupError):
                    runtime.process.terminate()

    async def _cancel_tasks(self, runtime: _ProgramRuntime) -> None:
        current = asyncio.current_task()
        for attr in ("supervisor_task", "log_task"):
            task = getattr(runtime, attr)
            if task is None or task is current:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            setattr(runtime, attr, None)


__all__ = ["ExternalProgramManager"]
