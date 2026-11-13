"""Shared FastAPI control-plane factory.

Both frontends use this module:

* ``webui/server.py`` adds static-file routes around it for the browser console.
* ``gui/local_server.py`` runs it on loopback for the native GTK client.

The backend deliberately contains no WebUI/GTK imports.  A JSON API added here
is available to both clients automatically, which keeps feature parity from
drifting between the browser and desktop versions.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .api import ALL_ROUTERS
from .auth import TokenAuth
from .events import EventHub
from .logbuffer import install_log_buffer
from .runtime_host import RuntimeHost
from .update_watch import run_update_watchdog

logger = logging.getLogger(__name__)

DEFAULT_PUBLIC_PREFIXES = ("/api/auth/",)


def create_control_app(
    host: RuntimeHost,
    auth: TokenAuth,
    *,
    title: str = "KarenBot Control API",
    description: str = "KarenBot 的共享后端控制面，供 WebUI 与原生 GUI 使用。",
    version: str = "",
    public_prefixes: Iterable[str] = DEFAULT_PUBLIC_PREFIXES,
    docs: bool = False,
    auth_error_message: str = "未授权：请在页面上输入访问令牌",
    enable_websocket: bool = True,
    frontend: str = "",
) -> FastAPI:
    """Build the shared FastAPI application.

    ``public_prefixes`` only affects token authentication.  Frontends that
    serve assets should pass their own prefixes (for example ``/static/`` and
    ``/docs``) and install those routes afterwards.
    """
    log_buffer = install_log_buffer()
    hub = EventHub(host, log_buffer)
    public_prefixes = tuple(str(prefix) for prefix in public_prefixes)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()
        log_buffer.bind_loop(loop)
        hub.bind_loop(loop)
        await hub.start()

        # Runtime 启动可能要几秒（探测密钥、拉起插件），放后台跑，
        # 让 HTTP 服务先起来，前端能立刻显示「启动中」。
        boot_task = asyncio.create_task(
            _boot(host, hub), name="control-boot-runtime"
        )
        # 版本自动检查与 HTTP 请求解耦；只读 update_state.json，到期才联网。
        update_task = asyncio.create_task(
            run_update_watchdog(host), name="control-update-watchdog"
        )
        try:
            yield
        finally:
            update_task.cancel()
            try:
                await update_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            boot_task.cancel()
            try:
                await boot_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            await hub.stop()
            await host.stop()

    app = FastAPI(
        title=title,
        description=description,
        version=version,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.host = host
    app.state.auth = auth
    app.state.hub = hub
    app.state.log_buffer = log_buffer
    app.state.frontend = str(frontend or "")
    if frontend:
        # The update API and its watchdog distinguish GUI and WebUI installs.
        host.frontend = str(frontend)

    for router in ALL_ROUTERS:
        app.include_router(router)

    _install_config_error_handler(app)
    _install_auth_middleware(
        app,
        auth,
        public_prefixes=public_prefixes,
        auth_error_message=auth_error_message,
    )
    if enable_websocket:
        _install_websocket_events(app)
    return app


async def _boot(host: RuntimeHost, hub: EventHub) -> None:
    """后台启动 Runtime。"""
    try:
        ok = await host.start()
        if ok:
            hub.attach_timeline()
    except Exception:  # noqa: BLE001
        logger.exception("Runtime 后台启动失败")


def _install_config_error_handler(app: FastAPI) -> None:
    """配置写入被拒 → 400 带原文，而不是 500「Internal Server Error」。"""
    from app_config import ConfigError

    @app.exception_handler(ConfigError)
    async def _handle(request: Request, exc: ConfigError) -> JSONResponse:
        logger.warning("配置操作被拒绝 %s: %s", request.url.path, exc)
        return JSONResponse(status_code=400, content={"detail": str(exc)})


def _install_auth_middleware(
    app: FastAPI,
    auth: TokenAuth,
    *,
    public_prefixes: tuple[str, ...],
    auth_error_message: str,
) -> None:
    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if (
            not auth.enabled
            or path == "/"
            or path.startswith(public_prefixes)
        ):
            return await call_next(request)
        if auth.request_authorized(request):
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={"detail": auth_error_message, "auth_required": True},
        )


def _install_websocket_events(app: FastAPI) -> None:
    @app.websocket("/ws/events")
    async def ws_events(websocket: WebSocket) -> None:
        """一条连接同时收 log / chat / status 三类事件。"""
        auth: TokenAuth = websocket.app.state.auth
        if not auth.websocket_authorized(websocket):
            await websocket.close(code=4401, reason="unauthorized")
            return

        await websocket.accept()
        hub: EventHub = websocket.app.state.hub
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1000)
        hub.register(queue)
        pump = asyncio.create_task(
            hub.pump_logs(queue), name="control-ws-log-pump"
        )
        try:
            while True:
                payload = await queue.get()
                await websocket.send_json(payload)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            logger.debug("WebSocket 连接异常结束", exc_info=True)
        finally:
            pump.cancel()
            hub.unregister(queue)


__all__ = ["DEFAULT_PUBLIC_PREFIXES", "create_control_app"]
