"""OneBot 11 HTTP API 传输（出站）。

对应 OneBot 11 的「HTTP API」通信方式：
    POST {http_api_url}/{action}
    Body: JSON 参数
    鉴权: Authorization: Bearer <token>，兼容 ?access_token=<token>

本传输只负责「调用动作」，入站事件由 Webhook 传输负责。两者可以单独使用，
也可以组合成「HTTP 收事件 + HTTP 调 API」的完整 HTTP 部署。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx

from adapters.base import AdapterAPIError, AdapterNotConnectedError

logger = logging.getLogger(__name__)

# httpx 在 INFO/DEBUG 级别会打印完整请求 URL；OneBot/Telegram 的 URL 含 token，
# 因此收紧到 WARNING，避免密钥进入日志。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class HTTPAPITransport:
    """基于 httpx.AsyncClient 的 OneBot 11 HTTP API 调用器。"""

    def __init__(
        self,
        base_url: str,
        access_token: str | None = None,
        *,
        timeout: float = 30.0,
        verify_tls: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.access_token = access_token
        self.timeout = float(timeout)
        self.verify_tls = bool(verify_tls)
        self._client: httpx.AsyncClient | None = client
        self._owns_client = client is None

    @property
    def is_connected(self) -> bool:
        """HTTP 是无状态传输：客户端就绪即可认为可用。"""
        return self._client is not None

    async def wait_connected(self, timeout: float | None = None) -> bool:
        return self.is_connected

    async def start(self) -> None:
        if not self.base_url:
            raise AdapterAPIError("HTTP API 模式缺少 http_api_url")
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                verify=self.verify_tls,
                follow_redirects=False,
            )
            self._owns_client = True
        logger.info("OneBot11 HTTP API 传输已就绪: %s", self.base_url)

    async def stop(self) -> None:
        client = self._client
        self._client = None
        if client is not None and self._owns_client:
            await client.aclose()

    async def call(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        self_id: str | None = None,
    ) -> Any:
        client = self._client
        if client is None:
            raise AdapterNotConnectedError("OneBot11 HTTP API 尚未启动")

        url = f"{self.base_url}/{quote(str(action), safe='')}"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        query: dict[str, str] = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
            # 部分实现端只认 query 鉴权，双写以提升兼容性。
            query["access_token"] = self.access_token

        try:
            response = await client.post(
                url,
                params=query or None,
                json=params or {},
                headers=headers,
                timeout=timeout if timeout is not None else self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise asyncio.TimeoutError(f"OneBot11 HTTP API 超时: {action}") from exc
        except httpx.HTTPError as exc:
            raise AdapterAPIError(f"OneBot11 HTTP API 请求失败: {action}: {exc}") from exc

        if response.status_code >= 400:
            raise AdapterAPIError(
                f"OneBot11 HTTP API {action} 返回 HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise AdapterAPIError(
                f"OneBot11 HTTP API {action} 返回非 JSON: {response.text[:300]}"
            ) from exc

        if not isinstance(payload, dict):
            raise AdapterAPIError(f"OneBot11 HTTP API {action} 返回格式非法: {payload!r}")

        status = payload.get("status", "")
        retcode = payload.get("retcode", -1)
        if status != "ok" or retcode != 0:
            message = payload.get("message") or payload.get("wording") or str(payload)
            raise AdapterAPIError(
                f"OneBot11 HTTP API {action} 失败 (retcode={retcode}): {message}"
            )
        data = payload.get("data")
        # data 可能是对象、数组，个别实现直接返回布尔/字符串。
        if isinstance(data, (dict, list)):
            return data
        if data is None:
            return {}
        return {"value": data}
