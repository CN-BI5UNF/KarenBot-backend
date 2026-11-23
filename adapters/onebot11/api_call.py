"""OneBot 11 API 调用层。

支持两种出站通道：
    1. WebSocket（正向/反向 WS 均可）：用 echo 字段配对请求与响应；
    2. HTTP API：直接 POST ``{http_api_url}/{action}``。

两者可同时存在。配置了 ``http_api_url`` 时优先走 HTTP（便于「WS 收事件 +
HTTP 调 API」的混合部署），否则走 WS echo。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from adapters.base import AdapterAPIError, AdapterNotConnectedError
from adapters.transports.http import HTTPAPITransport
from adapters.transports.ws import WSConnection

logger = logging.getLogger(__name__)


class OneBot11ApiCaller:
    """OneBot 11 API 调用与响应分发。"""

    def __init__(
        self,
        connection: WSConnection | None,
        *,
        http_transport: HTTPAPITransport | None = None,
        default_timeout: float = 30.0,
        wait_connected_timeout: float = 3.0,
    ) -> None:
        self._conn = connection
        self._http = http_transport
        self.default_timeout = default_timeout
        self.wait_connected_timeout = wait_connected_timeout
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    @property
    def has_channel(self) -> bool:
        return self._http is not None or self._conn is not None

    def handle_response(self, data: dict[str, Any]) -> bool:
        """处理一条 WS 消息：如果是 API 响应（含 echo），唤醒对应 Future。"""
        echo = data.get("echo")
        if not echo:
            return False

        future = self._pending.pop(echo, None)
        if future is None:
            logger.warning("收到无主响应（echo=%s）", echo)
            return True

        if not future.done():
            future.set_result(data)
        return True

    async def call(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        self_id: str | None = None,
    ) -> Any:
        """调用一次 OneBot 11 API，返回响应的 data 字段（对象或数组）。"""
        if self._http is not None:
            return await self._http.call(
                action, params, timeout=timeout, self_id=self_id
            )

        conn = self._conn
        if conn is None:
            raise AdapterNotConnectedError(f"OneBot11 没有可用的 API 通道，无法调用 {action}")
        if not conn.is_connected:
            if self.wait_connected_timeout > 0:
                ok = await conn.wait_connected(self.wait_connected_timeout)
                if not ok:
                    raise AdapterNotConnectedError(
                        f"OneBot11 未连接，等待 {self.wait_connected_timeout:.1f}s 后"
                        f"仍无法调用 {action}"
                    )
            else:
                raise AdapterNotConnectedError(f"OneBot11 未连接，无法调用 {action}")

        echo = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[echo] = future

        payload = {"action": action, "params": params or {}, "echo": echo}

        try:
            try:
                await conn.send(payload, self_id=self_id)
            except Exception as e:
                raise AdapterAPIError(f"发送 API 请求失败: {e}") from e

            try:
                response = await asyncio.wait_for(
                    future,
                    timeout=timeout if timeout is not None else self.default_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("API 调用超时: %s (echo=%s)", action, echo)
                raise
        finally:
            self._pending.pop(echo, None)

        status = response.get("status", "")
        retcode = response.get("retcode", -1)
        if status != "ok" or retcode != 0:
            message = response.get("message") or response.get("wording") or str(response)
            raise AdapterAPIError(
                f"API {action} 失败 (retcode={retcode}): {message}"
            )

        data = response.get("data")
        # OneBot 11 的 data 可能是对象、数组，个别实现也会直接返回布尔/字符串。
        if isinstance(data, (dict, list)):
            return data
        if data is None:
            return {}
        return {"value": data}

    def discard_pending(self) -> None:
        """清空所有等待中的 Future（断线时调用，让调用方早点失败）。"""
        for _echo, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(
                    AdapterNotConnectedError("OneBot11 连接已断开，请求被取消")
                )
        self._pending.clear()


# 旧名称兼容
NapCatApiCaller = OneBot11ApiCaller

__all__ = ["OneBot11ApiCaller", "NapCatApiCaller"]
