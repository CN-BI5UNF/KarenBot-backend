"""Telegram Bot API HTTP 客户端。

只依赖项目已有的 ``httpx``；支持 SOCKS/HTTP 代理（项目已内置 socksio）。
Token 只出现在请求 URL 中，日志绝不打印完整 URL。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# httpx 在 INFO/DEBUG 级别会打印完整请求 URL；OneBot/Telegram 的 URL 含 token，
# 因此收紧到 WARNING，避免密钥进入日志。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class TelegramAPIError(Exception):
    """Telegram Bot API 返回 ok=false 或 HTTP 错误。"""

    def __init__(self, message: str, *, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class TelegramAPIClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        proxy: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or "https://api.telegram.org").rstrip("/")
        self.token = token
        self.timeout = float(timeout)
        self.proxy = (proxy or "").strip()
        self._client = client
        self._owns_client = client is None

    @property
    def is_ready(self) -> bool:
        return self._client is not None

    async def start(self) -> None:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(self.timeout),
                "follow_redirects": False,
            }
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self._client = httpx.AsyncClient(**kwargs)
            self._owns_client = True
        logger.info("Telegram API 客户端已就绪: %s", self.base_url)

    async def stop(self) -> None:
        client = self._client
        self._client = None
        if client is not None and self._owns_client:
            await client.aclose()

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        files: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        client = self._client
        if client is None:
            raise TelegramAPIError("Telegram 客户端尚未启动")
        url = f"{self.base_url}/bot{self.token}/{method}"
        request_timeout = timeout if timeout is not None else self.timeout

        # 只对 429 rate limit 做有限重试；网络超时不重试，避免重复发送。
        for attempt in range(3):
            try:
                if files:
                    data = {
                        key: _form_value(value)
                        for key, value in (params or {}).items()
                        if value is not None
                    }
                    response = await client.post(
                        url, data=data, files=files, timeout=request_timeout
                    )
                else:
                    response = await client.post(
                        url, json=params or {}, timeout=request_timeout
                    )
            except httpx.TimeoutException as exc:
                raise asyncio.TimeoutError(f"Telegram API 超时: {method}") from exc
            except httpx.HTTPError as exc:
                # 不要把 URL（含 token）写进异常/日志。
                raise TelegramAPIError(
                    f"Telegram API 网络错误: {method}: {type(exc).__name__}"
                ) from exc

            try:
                payload = response.json()
            except ValueError as exc:
                raise TelegramAPIError(
                    f"Telegram API {method} 返回非 JSON (HTTP {response.status_code})"
                ) from exc
            if not isinstance(payload, dict):
                raise TelegramAPIError(f"Telegram API {method} 返回格式非法")

            error_code = payload.get("error_code") or response.status_code
            if error_code == 429 and attempt < 2:
                retry_after = 1
                parameters = payload.get("parameters")
                if isinstance(parameters, dict):
                    try:
                        retry_after = int(parameters.get("retry_after") or 1)
                    except (TypeError, ValueError):
                        retry_after = 1
                logger.warning(
                    "Telegram API %s 触发频控，%ss 后重试（%s/3）",
                    method,
                    retry_after,
                    attempt + 1,
                )
                await asyncio.sleep(min(max(retry_after, 1), 10))
                continue

            if response.status_code >= 400 or not payload.get("ok"):
                description = str(payload.get("description") or response.text[:200])
                raise TelegramAPIError(
                    f"Telegram API {method} 失败: {description}",
                    error_code=error_code,
                )
            return payload.get("result")

        raise TelegramAPIError(f"Telegram API {method} 连续触发频控")

    async def download_file(self, file_path: str) -> bytes:
        """按 getFile 返回的 file_path 下载文件。"""
        client = self._client
        if client is None:
            raise TelegramAPIError("Telegram 客户端尚未启动")
        url = f"{self.base_url}/file/bot{self.token}/{file_path}"
        try:
            response = await client.get(url, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise TelegramAPIError(
                f"Telegram 文件下载失败: {type(exc).__name__}"
            ) from exc
        if response.status_code >= 400:
            raise TelegramAPIError(
                f"Telegram 文件下载失败: HTTP {response.status_code}"
            )
        return response.content


def media_upload(path: Path) -> tuple[str, bytes, str]:
    """读取本地文件并推断 multipart 内容类型。"""
    content = path.read_bytes()
    suffix = path.suffix.lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".opus": "audio/ogg",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".pdf": "application/pdf",
        ".zip": "application/zip",
    }.get(suffix, "application/octet-stream")
    return path.name, content, mime


def _form_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


__all__ = ["TelegramAPIClient", "TelegramAPIError", "media_upload"]
