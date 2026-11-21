"""OneBot 11 HTTP POST 事件上报（入站 Webhook）。

协议要点（botuniverse/onebot-11 communication/http-post）：
    - 协议端把事件 JSON POST 到配置的 URL；
    - 若配置了 secret，请求头带 ``X-Signature: sha1=<hmac_sha1_hex>``，
      签名原文是 **原始请求体字节**；
    - 接收方返回 ``204 No Content`` 表示不执行快速操作；返回 JSON 则作为
      同步快速操作结果。

实现上使用项目已有的 Starlette + uvicorn（FastAPI 的传递依赖），不新增依赖。
测试时可直接用 ``httpx.ASGITransport`` 驱动 ``WebhookServer.app``，无需监听端口。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

WebhookHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
WebhookVerifier = Callable[[bytes, Any], bool]


def verify_signature(secret: str | None, body: bytes, signature: str | None) -> bool:
    """校验 OneBot 11 HMAC-SHA1 签名；未配置 secret 时直接放行。"""
    if not secret:
        return True
    if not signature:
        return False
    algorithm, _, digest = signature.partition("=")
    if algorithm.strip().lower() != "sha1" or not digest:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(expected, digest.strip())


class _QuietUvicornServer(uvicorn.Server):
    """不接管进程信号的 uvicorn Server。

    主程序可能已经运行在自己的 asyncio 事件循环里，Webhook 只是附属监听端口；
    如果让 uvicorn 安装 SIGINT/SIGTERM 处理器，会抢走主程序的退出控制。
    """

    def install_signal_handlers(self) -> None:  # pragma: no cover - 防御性覆盖
        return None

    def capture_signals(self):  # pragma: no cover - 兼容新版 uvicorn
        return nullcontext()


class WebhookServer:
    """独立的 OneBot 11 HTTP POST 监听器。"""

    def __init__(
        self,
        handler: WebhookHandler,
        *,
        host: str = "127.0.0.1",
        port: int = 5700,
        path: str = "/onebot11",
        secret: str | None = None,
        verify_request: WebhookVerifier | None = None,
        max_body_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self.handler = handler
        self.host = host
        self.port = port
        self.path = path if path.startswith("/") else f"/{path}"
        self.secret = secret
        self.verify_request = verify_request
        self.max_body_bytes = max_body_bytes
        self._server: _QuietUvicornServer | None = None
        self._task: asyncio.Task | None = None
        self.app = Starlette(routes=[Route(self.path, self._handle, methods=["POST"])])

    async def _handle(self, request: Request) -> Response:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.max_body_bytes:
                    return JSONResponse(
                        {"ok": False, "error": "body too large"}, status_code=413
                    )
            except ValueError:
                return JSONResponse(
                    {"ok": False, "error": "invalid content-length"}, status_code=400
                )

        # 流式读取并累计上限，避免 chunked 大包先整体进内存。
        body_bytes = bytearray()
        async for chunk in request.stream():
            body_bytes.extend(chunk)
            if len(body_bytes) > self.max_body_bytes:
                return JSONResponse(
                    {"ok": False, "error": "body too large"}, status_code=413
                )
        body = bytes(body_bytes)

        if self.verify_request is not None:
            authorized = self.verify_request(body, request.headers)
        else:
            signature = request.headers.get("X-Signature")
            authorized = verify_signature(self.secret, body, signature)
        if not authorized:
            logger.warning("Webhook 鉴权失败，拒绝请求 path=%s", self.path)
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            logger.warning("OneBot11 Webhook 收到非法 JSON")
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

        items = payload if isinstance(payload, list) else [payload]
        quick_result: dict[str, Any] | None = None
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                result = await self.handler(item)
            except Exception:  # noqa: BLE001
                logger.exception("OneBot11 Webhook 事件处理失败")
                return JSONResponse({"ok": False, "error": "handler failed"}, status_code=500)
            if isinstance(result, dict) and quick_result is None:
                quick_result = result

        if quick_result is None:
            return Response(status_code=204)
        return JSONResponse(quick_result)

    @property
    def is_connected(self) -> bool:
        return bool(self._server is not None and self._server.started)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            log_config=None,  # 不要重置宿主程序的 logging 配置
            access_log=False,
            lifespan="off",
        )
        self._server = _QuietUvicornServer(config)
        self._task = asyncio.create_task(
            self._server.serve(), name="onebot11-webhook"
        )
        # 等待监听真正就绪；端口冲突等错误会在这里冒出来。
        for _ in range(250):
            if self._server.started:
                logger.info(
                    "OneBot11 Webhook 监听: http://%s:%s%s",
                    self.host,
                    self.port,
                    self.path,
                )
                return
            if self._task.done():
                exc = self._task.exception()
                raise RuntimeError(f"OneBot11 Webhook 启动失败: {exc}") from exc
            await asyncio.sleep(0.02)
        raise RuntimeError("OneBot11 Webhook 启动超时")

    async def stop(self) -> None:
        server = self._server
        task = self._task
        self._server = None
        self._task = None
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            except (asyncio.CancelledError, Exception):
                pass
