"""Telegram Bot API 适配器。

支持两种入站方式：
    - long_polling：getUpdates 长轮询，不需要公网 IP（默认）；
    - webhook：Telegram 把 Update POST 到 KarenBot 的 HTTP 端点。

出站统一走 Bot API；文本按 4096 字符分片，mention 用 HTML 链接或纯文本。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import logging
import re
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from adapters.base import (
    AdapterAPIError,
    AdapterCapabilities,
    IAdapter,
)
from adapters.telegram.client import (
    TelegramAPIClient,
    TelegramAPIError,
    media_upload,
)
from adapters.telegram.events import parse_telegram_update
from adapters.transports.webhook import WebhookServer
from adapters.types import (
    FaceSegment,
    FileSegment,
    FriendInfo,
    GroupInfo,
    GroupMemberInfo,
    ImageSegment,
    IncomingMessage,
    MentionSegment,
    MessageSegment,
    RawSegment,
    ReplySegment,
    Target,
    TextSegment,
    UserInfo,
    VideoSegment,
    VoiceSegment,
)
from app_config.secrets import SecretsManager

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

TELEGRAM_CAPABILITIES = AdapterCapabilities(
    send_image=True,
    send_voice=True,
    send_video=True,
    upload_file=True,
    recall=True,
    reply=True,
    mention=True,
    poke=False,
    emoji_reaction=True,
    voice_stt=False,
    forward_read=False,
    group_history=False,
    get_message=False,
    list_friends=False,
    list_groups=False,
    list_group_members=False,
    friend_request=False,
    group_request=False,
    group_admin=False,
    proactive_private=True,
    proactive_group=True,
)

_MAX_TEXT_LENGTH = 4096
_DEFAULT_ALLOWED_UPDATES = ["message", "channel_post", "callback_query"]


class TelegramAdapter(IAdapter):
    def __init__(
        self,
        name: str,
        client: TelegramAPIClient,
        *,
        transport: Literal["long_polling", "webhook"] = "long_polling",
        parse_mode: Literal["HTML", "MarkdownV2", "none"] = "HTML",
        allowed_updates: list[str] | None = None,
        drop_pending_updates: bool = True,
        polling_timeout_seconds: float = 30.0,
        reconnect_interval_seconds: float = 3.0,
        webhook: WebhookServer | None = None,
        webhook_public_url: str = "",
        webhook_secret: str | None = None,
    ) -> None:
        super().__init__(name)
        self._client = client
        self._transport = transport
        self._parse_mode = parse_mode
        self._allowed_updates = list(allowed_updates or [])
        self._drop_pending_updates = drop_pending_updates
        self._polling_timeout = float(polling_timeout_seconds)
        self._reconnect_interval = float(reconnect_interval_seconds)
        self._webhook = webhook
        self._webhook_public_url = webhook_public_url
        self._webhook_secret = webhook_secret
        self._self_id = ""
        self._self_username = ""
        self._connected = False
        self._stop_event = asyncio.Event()
        self._poll_task: asyncio.Task | None = None
        self._webhook_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._webhook_task: asyncio.Task | None = None
        self._message_chats: dict[str, set[str]] = {}
        if webhook is not None:
            webhook.handler = self._on_webhook_update

    # ============================================================
    # 工厂
    # ============================================================

    @classmethod
    def from_config(
        cls, name: str, cfg: Any, secrets: SecretsManager
    ) -> TelegramAdapter:
        token = _secret_value(secrets, getattr(cfg, "bot_token_id", None))
        if not token:
            raise AdapterAPIError(
                f"Telegram 渠道 {name} 缺少 bot_token_id 或密钥不存在"
            )
        client = TelegramAPIClient(
            str(getattr(cfg, "api_base", "https://api.telegram.org")),
            token,
            timeout=float(getattr(cfg, "api_timeout_seconds", 30.0)),
            proxy=str(getattr(cfg, "proxy", "") or ""),
        )

        transport = str(getattr(cfg, "transport", "long_polling") or "long_polling")
        webhook: WebhookServer | None = None
        secret = _secret_value(
            secrets, getattr(cfg, "webhook_secret_token_id", None)
        )
        if getattr(cfg, "webhook_secret_token_id", None) and not secret:
            raise AdapterAPIError(
                "Telegram webhook_secret_token_id 引用的密钥不存在，"
                "拒绝在无鉴权的情况下启动 Webhook"
            )
        webhook_host = str(getattr(cfg, "webhook_host", "127.0.0.1"))
        if transport == "webhook" and webhook_host not in {"", "localhost", "127.0.0.1", "::1"} and not secret:
            raise AdapterAPIError(
                "Telegram Webhook 监听非回环地址时必须配置 webhook_secret_token_id"
            )
        if transport == "webhook":
            def _verify(_body: bytes, headers: Any) -> bool:
                if not secret:
                    return True
                received = str(
                    headers.get("X-Telegram-Bot-Api-Secret-Token", "") or ""
                )
                import hmac as _hmac

                return bool(
                    received and _hmac.compare_digest(received, secret)
                )

            webhook = WebhookServer(
                handler=_noop_update,
                host=webhook_host,
                port=int(getattr(cfg, "webhook_port", 8443)),
                path=str(getattr(cfg, "webhook_path", "/telegram")),
                secret=None,
                verify_request=_verify,
            )

        return cls(
            name=name,
            client=client,
            transport="webhook" if transport == "webhook" else "long_polling",
            parse_mode=str(getattr(cfg, "parse_mode", "HTML") or "HTML"),
            allowed_updates=list(getattr(cfg, "allowed_updates", []) or []),
            drop_pending_updates=bool(
                getattr(cfg, "drop_pending_updates", True)
            ),
            polling_timeout_seconds=float(
                getattr(cfg, "polling_timeout_seconds", 30.0)
            ),
            reconnect_interval_seconds=float(
                getattr(cfg, "reconnect_interval_seconds", 3.0)
            ),
            webhook=webhook,
            webhook_public_url=str(
                getattr(cfg, "webhook_public_url", "") or ""
            ),
            webhook_secret=secret,
        )

    # ============================================================
    # 生命周期
    # ============================================================

    async def start(self) -> None:
        await self._client.start()
        try:
            me = await self._client.call("getMe")
        except Exception:
            await self._client.stop()
            raise
        self._self_id = str((me or {}).get("id") or "")
        self._self_username = str((me or {}).get("username") or "")
        self._connected = True
        self._stop_event.clear()

        if self._transport == "webhook":
            assert self._webhook is not None
            self._webhook_queue = asyncio.Queue(maxsize=1024)
            self._webhook_task = asyncio.create_task(
                self._webhook_worker(), name="telegram-webhook-events"
            )
            try:
                await self._webhook.start()
            except Exception:
                self._webhook_task.cancel()
                try:
                    await self._webhook_task
                except asyncio.CancelledError:
                    pass
                self._webhook_task = None
                self._webhook_queue = None
                await self._client.stop()
                self._connected = False
                raise
            if self._webhook_public_url:
                params: dict[str, Any] = {
                    "url": self._webhook_public_url,
                    "drop_pending_updates": self._drop_pending_updates,
                    "allowed_updates": self._allowed_updates
                    or _DEFAULT_ALLOWED_UPDATES,
                }
                if self._webhook_secret:
                    params["secret_token"] = self._webhook_secret
                try:
                    await self._client.call("setWebhook", params)
                except Exception:
                    await self._webhook.stop()
                    if self._webhook_task is not None:
                        self._webhook_task.cancel()
                        try:
                            await self._webhook_task
                        except asyncio.CancelledError:
                            pass
                        self._webhook_task = None
                    self._webhook_queue = None
                    await self._client.stop()
                    self._connected = False
                    raise
                logger.info(
                    "Telegram Webhook 已注册: %s",
                    _safe_url(self._webhook_public_url),
                )
            else:
                logger.info(
                    "Telegram Webhook 监听已启动；未配置 webhook_public_url，"
                    "请手动调用 setWebhook 指向 http://本机:%s%s",
                    getattr(self._webhook, "port", "?"),
                    getattr(self._webhook, "path", "/telegram"),
                )
        else:
            # 从 webhook 模式切回长轮询时，Telegram 会因 webhook 仍激活而
            # 返回 409；长轮询模式下该适配器独占 bot，先清除 webhook。
            try:
                await self._client.call(
                    "deleteWebhook",
                    {"drop_pending_updates": self._drop_pending_updates},
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Telegram deleteWebhook 失败（继续长轮询）: %s", exc)
            self._poll_task = asyncio.create_task(
                self._poll_loop(), name="telegram-long-polling"
            )
        logger.info(
            "Telegram 适配器 %s 已启动（transport=%s, bot_id=%s）",
            self.name,
            self._transport,
            self._self_id or "?",
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self._webhook is not None:
            await self._webhook.stop()
        if self._webhook_task is not None:
            self._webhook_task.cancel()
            try:
                await self._webhook_task
            except asyncio.CancelledError:
                pass
            self._webhook_task = None
        self._webhook_queue = None
        await self._client.stop()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        if self._webhook is not None:
            return self._connected and self._webhook.is_connected
        return self._connected

    @property
    def capabilities(self) -> AdapterCapabilities:
        return TELEGRAM_CAPABILITIES

    @property
    def preferred_voice_format(self) -> str:
        return "ogg"

    # ============================================================
    # 轮询 / Webhook 入站
    # ============================================================

    async def _poll_loop(self) -> None:
        offset: int | None = None
        if not self._drop_pending_updates:
            offset = self._load_offset()
        if self._drop_pending_updates:
            try:
                pending = await self._client.call(
                    "getUpdates",
                    {"offset": -1, "limit": 1, "timeout": 0},
                    timeout=10.0,
                )
                if isinstance(pending, list) and pending:
                    last_id = pending[-1].get("update_id")
                    if isinstance(last_id, int):
                        offset = last_id + 1
                        self._save_offset(offset)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Telegram 丢弃积压 Update 失败: %s", exc)

        while not self._stop_event.is_set():
            params: dict[str, Any] = {
                "timeout": int(self._polling_timeout),
                "allowed_updates": self._allowed_updates
                or _DEFAULT_ALLOWED_UPDATES,
            }
            if offset is not None:
                params["offset"] = offset
            try:
                updates = await self._client.call(
                    "getUpdates",
                    params,
                    timeout=self._polling_timeout + 10.0,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if self._stop_event.is_set():
                    break
                logger.warning(
                    "Telegram 长轮询失败，%.1fs 后重试: %s",
                    self._reconnect_interval,
                    exc,
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._reconnect_interval,
                    )
                    break
                except asyncio.TimeoutError:
                    continue

            for update in updates or []:
                if not isinstance(update, dict):
                    continue
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                    self._save_offset(offset)
                await self._handle_update(update)

    async def _on_webhook_update(
        self, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        # 立即返回 2xx，避免 Telegram 因业务处理慢而重投。
        queue = self._webhook_queue
        if queue is None:
            await self._handle_update(payload)
            return None
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("Telegram Webhook 事件队列已满，返回 500 让 Telegram 重试")
            raise RuntimeError("telegram webhook queue full") from None
        return None

    async def _webhook_worker(self) -> None:
        queue = self._webhook_queue
        if queue is None:
            return
        while True:
            payload = await queue.get()
            try:
                await self._handle_update(payload)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Telegram Webhook 事件处理失败")
            finally:
                queue.task_done()

    async def _handle_update(self, update: dict[str, Any]) -> None:
        try:
            event = parse_telegram_update(
                self.name,
                update,
                self_id=self._self_id,
                self_username=self._self_username,
            )
            if event is None:
                return
            if isinstance(event, IncomingMessage):
                self._remember_chat(
                    event.message_id, event.group_id or event.user_id
                )
            await self._emit(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # 单条坏 Update 不能拖垮整个长轮询/Webhook。
            logger.exception("Telegram 事件处理失败 update_id=%s", update.get("update_id"))

    # ============================================================
    # 发送
    # ============================================================

    async def send_message(
        self, target: Target, segments: Sequence[MessageSegment]
    ) -> str | None:
        chat_id = _chat_id(target)
        thread_id = _thread_id(target)
        reply_to = next(
            (
                segment.message_id
                for segment in segments
                if isinstance(segment, ReplySegment) and segment.message_id
            ),
            None,
        )
        pieces: list[str] = []
        last_message_id: str | None = None
        first_message = True
        media_attempted = False

        async def flush_text() -> None:
            nonlocal last_message_id, first_message
            if not pieces:
                return
            chunks = _pack_pieces(pieces, _MAX_TEXT_LENGTH)
            pieces.clear()
            for chunk in chunks:
                if not chunk.strip():
                    continue
                params: dict[str, Any] = {
                    "chat_id": chat_id,
                    "text": chunk,
                }
                if self._parse_mode != "none":
                    params["parse_mode"] = self._parse_mode
                _apply_thread(params, thread_id)
                if first_message and reply_to and reply_to.isdigit():
                    params["reply_parameters"] = {
                        "message_id": int(reply_to),
                        "allow_sending_without_reply": True,
                    }
                result = await self._client.call("sendMessage", params)
                last_message_id = _result_message_id(result)
                self._remember_chat(last_message_id, chat_id)
                first_message = False

        for segment in segments:
            if isinstance(segment, ReplySegment):
                continue
            if isinstance(
                segment, (ImageSegment, VoiceSegment, VideoSegment, FileSegment)
            ):
                media_attempted = True
                await flush_text()
                result = await self._send_media(
                    segment,
                    chat_id,
                    thread_id=thread_id,
                    reply_to=reply_to if first_message else None,
                )
                if result is not None:
                    last_message_id = result
                    self._remember_chat(result, chat_id)
                    first_message = False
                continue
            if isinstance(segment, TextSegment):
                for chunk in _split_raw_text(segment.text, _MAX_TEXT_LENGTH):
                    escaped = self._escape_text(chunk)
                    pieces.extend(
                        _split_escaped_text(
                            escaped,
                            _MAX_TEXT_LENGTH,
                            markdown=self._parse_mode == "MarkdownV2",
                        )
                    )
                continue
            rendered = self._render_text_piece(segment)
            if rendered:
                pieces.append(rendered)

        await flush_text()
        if media_attempted and last_message_id is None:
            raise AdapterAPIError("Telegram 媒体消息全部发送失败")
        return last_message_id

    def _render_text_piece(self, segment: MessageSegment) -> str:
        if isinstance(segment, TextSegment):
            return self._escape_text(segment.text)
        if isinstance(segment, MentionSegment):
            if segment.user_id == "all":
                return "@all"
            name = segment.display_name or segment.user_id
            if self._parse_mode == "HTML" and str(segment.user_id).isdigit():
                return (
                    f'<a href="tg://user?id={html.escape(segment.user_id, quote=True)}">'
                    f"@{html.escape(name, quote=False)}</a>"
                )
            return f"@{name}"
        if isinstance(segment, FaceSegment):
            return self._escape_text(segment.label or f"[face:{segment.face_id}]")
        if isinstance(segment, RawSegment):
            return self._escape_text(
                segment.fallback_text or f"[{segment.platform_type}]"
            )
        return ""

    def _escape_text(self, text: str) -> str:
        if self._parse_mode == "HTML":
            # Telegram HTML 只保证 & < > 和 &quot;；不要生成 &#x27; 之类实体。
            return html.escape(text, quote=False)
        if self._parse_mode == "MarkdownV2":
            return _MDV2_ESCAPE_RE.sub(r"\\\1", text)
        return text

    async def _send_media(
        self,
        segment: MessageSegment,
        chat_id: str,
        *,
        thread_id: int | None = None,
        reply_to: str | None = None,
    ) -> str | None:
        if isinstance(segment, ImageSegment):
            return await self._send_media_item(
                "sendPhoto", "photo", segment, chat_id,
                thread_id=thread_id, reply_to=reply_to,
            )
        if isinstance(segment, VoiceSegment):
            # Telegram 语音条只接受 OGG/Opus。本地 mp3/wav 先尝试 ffmpeg 转码；
            # 没有 ffmpeg 时退回 sendAudio，确保消息能送达。
            source_path = Path(str(segment.path)) if segment.path else None
            converted: Path | None = None
            if source_path is not None and source_path.suffix.lower() not in {
                ".ogg", ".oga", ".opus"
            }:
                converted = await asyncio.to_thread(_try_transcode_ogg, source_path)
                if converted is not None:
                    segment = VoiceSegment(path=str(converted))
            source = str(getattr(segment, "path", None) or getattr(segment, "url", None) or "")
            is_ogg = source.lower().split("?", 1)[0].endswith((".ogg", ".oga", ".opus"))
            method, field = ("sendVoice", "voice") if is_ogg else ("sendAudio", "audio")
            try:
                return await self._send_media_item(
                    method, field, segment, chat_id,
                    thread_id=thread_id, reply_to=reply_to,
                )
            finally:
                if converted is not None:
                    try:
                        converted.unlink()
                    except OSError:
                        pass
        if isinstance(segment, VideoSegment):
            return await self._send_media_item(
                "sendVideo", "video", segment, chat_id,
                thread_id=thread_id, reply_to=reply_to,
            )
        if isinstance(segment, FileSegment):
            return await self._send_media_item(
                "sendDocument", "document", segment, chat_id,
                thread_id=thread_id, reply_to=reply_to,
            )
        return None

    async def _send_media_item(
        self,
        method: str,
        field: str,
        segment: Any,
        chat_id: str,
        *,
        thread_id: int | None = None,
        reply_to: str | None = None,
    ) -> str | None:
        params: dict[str, Any] = {"chat_id": chat_id}
        _apply_thread(params, thread_id)
        if reply_to and reply_to.isdigit():
            params["reply_parameters"] = {
                "message_id": int(reply_to),
                "allow_sending_without_reply": True,
            }
        files: dict[str, Any] | None = None

        path = getattr(segment, "path", None)
        url = getattr(segment, "url", None)
        b64 = getattr(segment, "b64", None)
        file_id = getattr(segment, "file_id", None)

        if path:
            file_path = Path(str(path))
            if not file_path.exists():
                logger.warning("Telegram 媒体文件不存在: %s", file_path.name)
                return None
            files = {field: media_upload(file_path)}
        elif b64:
            try:
                raw = base64.b64decode(
                    str(b64).removeprefix("base64://"), validate=False
                )
            except Exception:  # noqa: BLE001
                logger.warning("Telegram 媒体 base64 解码失败")
                return None
            files = {field: ("upload.bin", raw, "application/octet-stream")}
        elif url:
            params[field] = str(url)
        elif file_id:
            params[field] = str(file_id)
        else:
            logger.warning("Telegram 媒体段没有可发送的来源")
            return None

        try:
            result = await self._client.call(method, params, files=files)
        except TelegramAPIError as exc:
            logger.warning("Telegram %s 失败: %s", method, exc)
            return None
        return _result_message_id(result)

    async def send_text(self, target: Target, content: str) -> str | None:
        return await self.send_message(target, [TextSegment(text=content)])

    async def send_image(
        self,
        target: Target,
        *,
        image_path: Path | None = None,
        image_url: str | None = None,
        image_b64: str | None = None,
    ) -> str | None:
        return await self.send_message(
            target,
            [
                ImageSegment(
                    path=str(image_path) if image_path is not None else None,
                    url=image_url,
                    b64=image_b64,
                )
            ],
        )

    async def send_voice(self, target: Target, audio_path: Path) -> str | None:
        return await self.send_message(
            target, [VoiceSegment(path=str(audio_path))]
        )

    async def upload_file(
        self,
        target: Target,
        file_path: Path,
        *,
        display_name: str | None = None,
    ) -> None:
        await self.send_message(
            target,
            [
                FileSegment(
                    path=str(file_path),
                    name=display_name or file_path.name,
                )
            ],
        )

    async def recall(self, message_id: str) -> bool:
        chats = self._message_chats.get(str(message_id)) or set()
        if not chats:
            logger.warning("Telegram 撤回失败：未知 message_id=%s 所属会话", message_id)
            return False
        if len(chats) > 1:
            # Telegram 的 message_id 只在单个 chat 内唯一；跨群同号时不能猜，
            # 否则可能撤回另一个群的同名消息。
            logger.warning(
                "Telegram 撤回失败：message_id=%s 在多个会话中出现（%s），无法唯一定位",
                message_id,
                ", ".join(sorted(chats)),
            )
            return False
        chat_id = next(iter(chats))
        try:
            await self._client.call(
                "deleteMessage",
                {"chat_id": chat_id, "message_id": int(message_id)},
            )
            return True
        except (TelegramAPIError, ValueError) as exc:
            logger.warning("Telegram 撤回失败: %s", exc)
            return False

    async def set_reaction(
        self,
        target: Target,
        message_id: str,
        emoji_id: str,
        *,
        remove: bool = False,
    ) -> None:
        chat_id = _chat_id(target) or next(iter(self._message_chats.get(str(message_id)) or []), None)
        if not chat_id:
            raise AdapterAPIError("Telegram 表情回应缺少 chat_id")
        reaction = [] if remove else [{"type": "emoji", "emoji": emoji_id}]
        await self._client.call(
            "setMessageReaction",
            {
                "chat_id": chat_id,
                "message_id": int(message_id),
                "reaction": reaction,
            },
        )

    # ============================================================
    # 联系人 / 请求（Bot API 不提供的能力）
    # ============================================================

    async def list_friends(self) -> list[FriendInfo]:
        raise NotImplementedError("Telegram Bot API 不提供好友列表")

    async def list_groups(self) -> list[GroupInfo]:
        raise NotImplementedError("Telegram Bot API 不提供群列表")

    async def list_group_members(self, group_id: str) -> list[GroupMemberInfo]:
        raise NotImplementedError("Telegram Bot API 不提供群成员列表")

    async def get_file_url(self, file_id: str) -> str | None:
        return await self._download_media_to_cache(file_id)

    async def get_image_url(self, file_id: str) -> str | None:
        return await self._download_media_to_cache(file_id)

    async def _download_media_to_cache(self, file_id: str) -> str | None:
        """下载 Telegram 文件到本地临时目录，返回绝对路径。

        不返回带 token 的 Bot API 文件 URL，避免密钥进入日志/模型上下文。
        """
        if not file_id:
            return None
        try:
            info = await self._client.call("getFile", {"file_id": file_id})
        except TelegramAPIError as exc:
            logger.warning("Telegram getFile 失败: %s", exc)
            return None
        file_path = str((info or {}).get("file_path") or "")
        if not file_path:
            return None
        suffix = Path(file_path).suffix.lower() or ".bin"
        cache_dir = Path(tempfile.gettempdir()) / "karenbot-telegram"
        digest = hashlib.sha256(str(file_id).encode("utf-8")).hexdigest()[:32]
        target = cache_dir / f"{digest}{suffix}"
        if target.is_file() and target.stat().st_size > 0:
            return str(target)
        try:
            content = await self._client.download_file(file_path)
        except TelegramAPIError as exc:
            logger.warning("Telegram 文件下载失败: %s", exc)
            return None
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            cache_dir.chmod(0o700)
        except OSError:
            pass
        await asyncio.to_thread(target.write_bytes, content)
        try:
            target.chmod(0o600)
        except OSError:
            pass
        self._trim_media_cache(cache_dir)
        return str(target)

    @staticmethod
    def _trim_media_cache(cache_dir: Path, keep: int = 300) -> None:
        try:
            files = sorted(
                (p for p in cache_dir.iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime,
            )
        except OSError:
            return
        for stale in files[:-keep]:
            try:
                stale.unlink()
            except OSError:
                pass

    async def get_user_info(self, user_id: str) -> UserInfo:
        data = await self._client.call("getChat", {"chat_id": user_id})
        return UserInfo(
            user_id=str(data.get("id", user_id)),
            nickname=str(
                data.get("first_name")
                or data.get("title")
                or data.get("username")
                or ""
            ),
            extra=dict(data) if isinstance(data, dict) else {},
        )

    async def handle_friend_request(
        self, flag: str, approve: bool, remark: str = ""
    ) -> dict[str, Any] | None:
        raise NotImplementedError("Telegram 没有好友请求")

    async def handle_group_request(
        self,
        flag: str,
        sub_type: Literal["add", "invite"],
        approve: bool,
        reason: str = "",
    ) -> None:
        raise NotImplementedError("Telegram 没有加群请求")

    async def call_api(self, action: str, **params: Any) -> Any:
        return await self._client.call(action, params)

    # ============================================================
    # 辅助
    # ============================================================

    def _offset_path(self) -> Path:
        safe_bot_id = "".join(
            char for char in str(self._self_id or "unknown") if char.isalnum()
        ) or "unknown"
        return Path(tempfile.gettempdir()) / "karenbot-telegram" / f"{safe_bot_id}.offset"

    def _load_offset(self) -> int | None:
        try:
            value = int(self._offset_path().read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None
        return value if value >= 0 else None

    def _save_offset(self, offset: int) -> None:
        path = self._offset_path()
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(str(offset), encoding="ascii")
            path.chmod(0o600)
        except OSError:
            logger.debug("Telegram offset 持久化失败", exc_info=True)

    def _remember_chat(self, message_id: str | None, chat_id: str | None) -> None:
        if not message_id or not chat_id:
            return
        chats = self._message_chats.setdefault(str(message_id), set())
        chats.add(str(chat_id))
        if len(self._message_chats) > 2000:
            for key in list(self._message_chats)[:500]:
                self._message_chats.pop(key, None)


_MDV2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _try_transcode_ogg(source: Path) -> Path | None:
    """尽力把本地音频转成 Telegram 语音条要求的 OGG/Opus。"""
    import shutil
    import subprocess

    if not source.is_file() or shutil.which("ffmpeg") is None:
        return None
    digest = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:32]
    target_dir = Path(tempfile.gettempdir()) / "karenbot-telegram"
    target = target_dir / f"{digest}-{uuid.uuid4().hex[:12]}.ogg"
    try:
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(source),
                "-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1",
                str(target),
            ],
            check=True,
            timeout=45,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if target.is_file() and target.stat().st_size > 0:
        try:
            target.chmod(0o600)
        except OSError:
            pass
        return target
    return None


def _safe_url(url: str) -> str:
    """日志用 URL：去掉 query/fragment，避免把 token 等参数写进日志。"""
    from urllib.parse import urlsplit

    parsed = urlsplit(str(url))
    if not parsed.scheme or not parsed.netloc:
        return str(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _thread_id(target: Target) -> int | None:
    raw = str(target.thread_id or "").strip()
    return int(raw) if raw.isdigit() else None


def _apply_thread(params: dict[str, Any], thread_id: int | None) -> None:
    if thread_id is not None:
        params["message_thread_id"] = thread_id


_HTML_TOKEN_RE = re.compile(r"&(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z]+);|.", re.DOTALL)
_MDV2_TOKEN_RE = re.compile(r"\\.|&(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z]+);|.", re.DOTALL)


def _utf16_len(text: str) -> int:
    """Telegram 以 UTF-16 code unit 计算消息长度；emoji 占 2 格。"""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def _split_escaped_text(
    text: str, limit: int, *, markdown: bool = False
) -> list[str]:
    """按上限切分已转义文本，保证不切开 &amp; / MarkdownV2 的反斜杠转义对。"""
    if _utf16_len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    current_units = 0
    token_re = _MDV2_TOKEN_RE if markdown else _HTML_TOKEN_RE
    for token in token_re.findall(text):
        token_units = _utf16_len(token)
        if current_units + token_units > limit:
            if current:
                chunks.append(current)
            current = token
            current_units = token_units
        else:
            current += token
            current_units += token_units
    if current:
        chunks.append(current)
    return chunks


def _split_raw_text(text: str, limit: int) -> list[str]:
    """按 UTF-16 长度切分长文本，保证每段不超过 Telegram 限制。"""
    if _utf16_len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    units = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit:
            chunks.append("".join(current))
            current = []
            units = 0
        current.append(char)
        units += width
    if current:
        chunks.append("".join(current))
    return chunks


def _chat_id(target: Target) -> str:
    if target.scope == "private":
        return str(target.target_id)
    if target.scope in {"group", "channel"}:
        return str(target.target_id)
    raise AdapterAPIError(f"Telegram 不支持 scope={target.scope!r}")


def _result_message_id(result: Any) -> str | None:
    if isinstance(result, dict) and result.get("message_id") is not None:
        return str(result["message_id"])
    return None


def _pack_pieces(pieces: list[str], limit: int) -> list[str]:
    """把原子文本片段打包成不超过 limit（UTF-16）的消息。"""
    chunks: list[str] = []
    current = ""
    current_units = 0
    for piece in pieces:
        piece_units = _utf16_len(piece)
        while piece_units > limit:
            head = _split_raw_text(piece, limit)[0]
            if current:
                chunks.append(current)
                current = ""
                current_units = 0
            chunks.append(head)
            piece = piece[len(head):]
            piece_units = _utf16_len(piece)
        if current_units + piece_units > limit:
            chunks.append(current)
            current = piece
            current_units = piece_units
        else:
            current += piece
            current_units += piece_units
    if current:
        chunks.append(current)
    return chunks


async def _noop_update(_payload: dict[str, Any]) -> dict[str, Any] | None:
    return None


def _secret_value(secrets: SecretsManager, secret_id: str | None) -> str | None:
    if not secret_id:
        return None
    value = secrets.get(secret_id)
    if value is None:
        logger.warning("配置引用的 secret_id=%r 在 secrets 中不存在", secret_id)
    return value


__all__ = ["TelegramAdapter", "TELEGRAM_CAPABILITIES"]
