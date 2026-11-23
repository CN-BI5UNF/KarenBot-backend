"""OneBot 11 适配器 —— 标准协议实现 + 方言扩展。

支持三种传输：
    - ws_client：Karen 作为 WS 客户端连接协议端（原 NapCat client 模式）；
    - ws_server：Karen 作为 WS 服务端等协议端反向连入（原 server 模式）；
    - http：HTTP API 出站 + HTTP POST Webhook 入站。

``type: napcat`` 与 ``type: onebot11`` 共用本类，区别只在配置默认值与方言。
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from adapters.base import (
    AdapterAPIError,
    AdapterCapabilities,
    IAdapter,
)
from adapters.transports.http import HTTPAPITransport
from adapters.transports.webhook import WebhookServer
from adapters.transports.ws import (
    WSClientConnection,
    WSConnection,
    WSServerConnection,
)
from adapters.types import (
    FriendInfo,
    GroupInfo,
    GroupMemberInfo,
    ImageSegment,
    IncomingMessage,
    IncomingRequest,
    MessageSegment,
    RequestType,
    Target,
    UserInfo,
)
from app_config.secrets import SecretsManager

from .api_call import OneBot11ApiCaller
from .dialects import DialectSpec, resolve_dialect
from .events import parse_onebot11_event
from .segments import (
    cq_to_segments,
    segments_to_cq,
    segments_to_message,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


_LOOPBACK_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


def _server_bind_host(host: str) -> str:
    """server 模式监听地址。

    尊重用户配置：默认只监听回环地址。跨设备部署需要显式写 0.0.0.0，
    并且必须配置 access_token，避免局域网内任意主机注入事件。
    """
    raw = (host or "").strip()
    if raw.lower() in {"", "localhost"}:
        return "127.0.0.1"
    return raw


def _is_friend_request_missing_error(message: str) -> bool:
    text = message.lower()
    return any(
        marker in text
        for marker in (
            "请求不存在",
            "请求已不存在",
            "好友请求不存在",
            "request not exist",
            "request does not exist",
            "request not found",
        )
    )


def _wire_id(value: str) -> int | str:
    """QQ 数字 ID 保持 int（兼容旧协议端），非数字 ID 原样传字符串。"""
    text = str(value)
    if text.lstrip("-").isdigit():
        return int(text)
    return text


class OneBot11Adapter(IAdapter):
    """OneBot 11 协议适配器（标准 + 方言）。"""

    def __init__(
        self,
        name: str,
        connection: WSConnection | None,
        *,
        http_api: HTTPAPITransport | None = None,
        webhook: WebhookServer | None = None,
        dialect: DialectSpec | None = None,
        message_format: Literal["array", "string"] = "array",
        api_timeout_seconds: float = 30.0,
        api_wait_connected_timeout_seconds: float = 3.0,
        voice_fetch_delay_seconds: float = 1.0,
    ) -> None:
        super().__init__(name)
        self._connection = connection
        self._http_api = http_api
        self._webhook = webhook
        self._dialect = dialect or resolve_dialect("generic", "onebot11")
        self._api = OneBot11ApiCaller(
            connection,
            http_transport=http_api,
            default_timeout=api_timeout_seconds,
            wait_connected_timeout=api_wait_connected_timeout_seconds,
        )
        self._voice_fetch_delay_seconds = voice_fetch_delay_seconds
        self.message_format = message_format
        self._friend_request_users_by_flag: dict[str, str] = {}
        self._capabilities_override: AdapterCapabilities | None = None
        self._webhook_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._webhook_task: asyncio.Task | None = None

        if connection is not None:
            connection.on_message(self._on_transport_message)
            connection.on_connection_lost(self._api.discard_pending)
        if webhook is not None:
            webhook.handler = self._on_webhook_payload

    # ============================================================
    # 工厂方法
    # ============================================================

    @classmethod
    def from_config(
        cls,
        name: str,
        cfg: Any,
        secrets: SecretsManager,
    ) -> OneBot11Adapter:
        """从配置构造。所有 timeout / heartbeat / 重连参数均来自 cfg。"""
        access_token = _secret_value(secrets, getattr(cfg, "access_token_id", None))
        if getattr(cfg, "access_token_id", None) and not access_token:
            raise ValueError(
                f"OneBot11 渠道 {name} 的 access_token_id 引用的密钥不存在；"
                "拒绝在无鉴权的情况下启动渠道"
            )
        adapter_type = str(getattr(cfg, "type", "onebot11") or "onebot11")
        dialect = resolve_dialect(getattr(cfg, "dialect", "auto"), adapter_type)

        transport = str(getattr(cfg, "transport", "") or "").strip()
        if not transport:
            mode = str(getattr(cfg, "mode", "client") or "client").strip().lower()
            transport = "ws_server" if mode == "server" else "ws_client"

        connection: WSConnection | None = None
        http_api: HTTPAPITransport | None = None
        webhook: WebhookServer | None = None

        if transport == "ws_client":
            ws_url = f"ws://{cfg.host}:{cfg.port}{cfg.path}"
            logger.info("OneBot11 配置：ws_client 模式，连接 %s", ws_url)
            connection = WSClientConnection(
                ws_url=ws_url,
                access_token=access_token,
                reconnect_interval=getattr(cfg, "reconnect_interval_seconds", 3.0),
                max_reconnect_attempts=getattr(cfg, "max_reconnect_attempts", -1),
                reconnect_backoff_max=getattr(
                    cfg, "reconnect_backoff_max_seconds", 60.0
                ),
                ping_interval=getattr(cfg, "ping_interval_seconds", 20.0),
                ping_timeout=getattr(cfg, "ping_timeout_seconds", 20.0),
                initial_connect_timeout=getattr(
                    cfg, "startup_connect_timeout_seconds", 1.5
                ),
                fast_reconnect_attempts=getattr(cfg, "fast_reconnect_attempts", 5),
                fast_reconnect_interval=getattr(
                    cfg, "fast_reconnect_interval_seconds", 0.3
                ),
                reconnect_jitter=getattr(cfg, "reconnect_jitter_seconds", 0.2),
            )
        elif transport == "ws_server":
            bind_host = _server_bind_host(str(getattr(cfg, "host", "127.0.0.1")))
            if bind_host != getattr(cfg, "host", "127.0.0.1"):
                logger.info(
                    "OneBot11 server 模式监听地址已归一化: %r → %s",
                    getattr(cfg, "host", ""),
                    bind_host,
                )
            if bind_host in _LOOPBACK_HOSTS:
                logger.info(
                    "OneBot11 反向 WS 只监听本机 %s；跨设备请把 host 设为 0.0.0.0 并配置 token。",
                    bind_host,
                )
            if bind_host not in _LOOPBACK_HOSTS and not access_token:
                raise ValueError(
                    f"OneBot11 渠道 {name} 的 ws_server 监听非回环地址 {bind_host}，"
                    "但没有配置 access_token_id。请配置 token，或把 host 改成 127.0.0.1。"
                )
            logger.info(
                "OneBot11 配置：ws_server 模式，监听 ws://%s:%s%s",
                bind_host,
                getattr(cfg, "port", 3001),
                getattr(cfg, "path", "/"),
            )
            connection = WSServerConnection(
                host=bind_host,
                port=int(getattr(cfg, "port", 3001)),
                path=str(getattr(cfg, "path", "/") or "/"),
                access_token=access_token,
                ping_interval=getattr(cfg, "ping_interval_seconds", 20.0),
                ping_timeout=getattr(cfg, "ping_timeout_seconds", 20.0),
            )
        elif transport == "http":
            base_url = str(getattr(cfg, "http_api_url", "") or "").strip()
            if base_url:
                http_api = HTTPAPITransport(
                    base_url,
                    access_token=access_token,
                    timeout=getattr(cfg, "api_timeout_seconds", 30.0),
                )
            logger.info(
                "OneBot11 配置：http 模式，API=%s webhook=%s",
                base_url or "(未配置)",
                getattr(cfg, "webhook_path", ""),
            )
        else:
            raise ValueError(f"未知 OneBot11 transport: {transport!r}")

        # 混合部署：WS 收事件 + HTTP 调 API，或 HTTP 模式互补。
        if http_api is None and transport != "http":
            base_url = str(getattr(cfg, "http_api_url", "") or "").strip()
            if base_url:
                http_api = HTTPAPITransport(
                    base_url,
                    access_token=access_token,
                    timeout=getattr(cfg, "api_timeout_seconds", 30.0),
                )
                logger.info("OneBot11 出站 API 改用 HTTP: %s", base_url)

        webhook_enabled = bool(getattr(cfg, "webhook_enabled", False)) or (
            transport == "http"
        )
        webhook_path = str(getattr(cfg, "webhook_path", "") or "").strip()
        if webhook_enabled and webhook_path:
            webhook_host = str(getattr(cfg, "webhook_host", "127.0.0.1"))
            secret = _secret_value(secrets, getattr(cfg, "webhook_secret_id", None))
            if getattr(cfg, "webhook_secret_id", None) and not secret:
                raise ValueError(
                    f"OneBot11 渠道 {name} 的 webhook_secret_id 引用的密钥不存在，"
                    "拒绝在无鉴权的情况下启动 Webhook"
                )
            if webhook_host not in _LOOPBACK_HOSTS and not secret:
                raise ValueError(
                    f"OneBot11 渠道 {name} 的 Webhook 监听非回环地址 {webhook_host}，"
                    "但没有配置 webhook_secret_id。请配置密钥，或把 webhook_host 改成 127.0.0.1。"
                )
            webhook = WebhookServer(
                handler=_noop_webhook,  # 构造后由 __init__ 重新绑定
                host=webhook_host,
                port=int(getattr(cfg, "webhook_port", 5700)),
                path=webhook_path,
                secret=secret,
            )

        if connection is None and http_api is None and webhook is None:
            raise ValueError(
                f"OneBot11 渠道 {name} 没有任何可用传输：请检查 transport、"
                "http_api_url、webhook_path 配置"
            )

        return cls(
            name=name,
            connection=connection,
            http_api=http_api,
            webhook=webhook,
            dialect=dialect,
            message_format=str(getattr(cfg, "message_format", "array") or "array"),
            api_timeout_seconds=getattr(cfg, "api_timeout_seconds", 30.0),
            api_wait_connected_timeout_seconds=getattr(
                cfg, "api_wait_connected_timeout_seconds", 3.0
            ),
            voice_fetch_delay_seconds=getattr(cfg, "voice_fetch_delay_seconds", 1.0),
        )

    # ============================================================
    # 生命周期
    # ============================================================

    async def start(self) -> None:
        if self._connection is not None:
            await self._connection.start()
        if self._http_api is not None:
            await self._http_api.start()
        if self._webhook is not None:
            self._webhook_queue = asyncio.Queue(maxsize=1024)
            self._webhook_task = asyncio.create_task(
                self._webhook_worker(), name="onebot11-webhook-events"
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
                raise
        logger.info(
            "OneBot11 适配器 %s 已启动（dialect=%s）",
            self.name,
            self._dialect.display_name,
        )

    async def stop(self) -> None:
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
        if self._http_api is not None:
            await self._http_api.stop()
        if self._connection is not None:
            await self._connection.stop()
        self._api.discard_pending()

    @property
    def is_connected(self) -> bool:
        # 入站通道优先：混合部署（WS 收事件 + HTTP 调 API）时，WS 断了
        # 不能因为 HTTP 客户端就绪就报告「已连接」。
        if self._connection is not None:
            return self._connection.is_connected
        if self._webhook is not None:
            return self._webhook.is_connected
        if self._http_api is not None:
            return self._http_api.is_connected
        return False

    @property
    def capabilities(self) -> AdapterCapabilities:
        return self._capabilities_override or self._dialect.capabilities

    @property
    def preferred_voice_format(self) -> str:
        return "mp3"

    @property
    def dialect(self) -> DialectSpec:
        return self._dialect

    # ============================================================
    # 入站消息分发
    # ============================================================

    async def _on_transport_message(self, data: dict[str, Any]) -> None:
        if self._api.handle_response(data):
            return
        await self._on_event_payload(data)

    async def _on_webhook_payload(
        self, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        # Webhook 必须尽快返回 204；媒体下载/ASR 等业务处理放到后台队列，
        # 否则协议端会因响应超时而重投同一事件。
        if self._api.handle_response(data):
            return None
        queue = self._webhook_queue
        if queue is None:
            await self._on_event_payload(data)
            return None
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("OneBot11 Webhook 事件队列已满，返回 500 让协议端重试")
            raise RuntimeError("onebot11 webhook queue full") from None
        return None

    async def _webhook_worker(self) -> None:
        queue = self._webhook_queue
        if queue is None:
            return
        while True:
            data = await queue.get()
            try:
                await self._on_event_payload(data)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("OneBot11 Webhook 事件处理失败")
            finally:
                queue.task_done()

    async def _on_event_payload(self, data: dict[str, Any]) -> None:
        event = parse_onebot11_event(self.name, data)
        if event is None:
            return
        # 自身发送回显不进入业务管线，避免机器人回复自己的消息形成循环。
        if isinstance(event, IncomingMessage) and event.self_sent:
            logger.debug("忽略 message_sent 自身发送回显 message_id=%s", event.message_id)
            return
        if (
            isinstance(event, IncomingRequest)
            and event.request_type == RequestType.FRIEND
        ):
            self._friend_request_users_by_flag[event.flag] = event.user_id
            try:
                if await self._is_friend(event.user_id, timeout_seconds=3.0):
                    self._friend_request_users_by_flag.pop(event.flag, None)
                    await self._emit_friend_confirmed(event.user_id)
                    logger.info(
                        "收到好友请求但对方已在好友列表中，跳过审批事件 flag=%s user_id=%s",
                        event.flag,
                        event.user_id,
                    )
                    return
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "好友请求预检查好友列表失败，保留审批事件 flag=%s user_id=%s: %s",
                    event.flag,
                    event.user_id,
                    e,
                )
        await self._emit(event)

    # ============================================================
    # 消息发送
    # ============================================================

    async def send_message(
        self, target: Target, segments: Sequence[MessageSegment]
    ) -> str | None:
        items = list(segments)
        if not items:
            return None
        message = segments_to_message(items, message_format=self.message_format)
        return await self._send_payload(target, message)

    async def send_text(self, target: Target, content: str) -> str | None:
        """发送文本；content 中的 CQ 码会在适配器层转换为标准段。"""
        segments = cq_to_segments(content)
        if not segments:
            return None
        if self.message_format == "string":
            # 重新编码一遍，确保普通文本里的 & [ ] 按 CQ 标准转义。
            return await self._send_payload(target, segments_to_cq(segments))
        return await self.send_message(target, segments)

    async def send_image(
        self,
        target: Target,
        *,
        image_path: Path | None = None,
        image_url: str | None = None,
        image_b64: str | None = None,
    ) -> str | None:
        if image_path is not None:
            raw = await asyncio.to_thread(image_path.read_bytes)
            image_b64 = base64.b64encode(raw).decode("ascii")
        if not any((image_b64, image_url)):
            raise ValueError("必须提供 image_path / image_url / image_b64 之一")
        return await self.send_message(
            target,
            [
                ImageSegment(
                    b64=image_b64,
                    url=image_url,
                )
            ],
        )

    async def _send_payload(
        self, target: Target, message: Any
    ) -> str | None:
        params = self._build_send_params(target, message)
        result = await self._api.call("send_msg", params, self_id=target.self_id)
        mid = result.get("message_id")
        return str(mid) if mid is not None else None

    async def send_voice(self, target: Target, audio_path: Path) -> str | None:
        file_uri = "file:///" + str(audio_path.absolute()).replace("\\", "/")
        message = [{"type": "record", "data": {"file": file_uri}}]
        try:
            return await self._send_payload(target, message)
        except AdapterAPIError as e:
            msg = str(e)
            if "retcode=1200" in msg and "Timeout" in msg:
                logger.warning(
                    "协议端 send_voice 回包超时，消息可能已实际发出；按疑似成功处理: %s",
                    e,
                )
                return None
            raise

    async def recall(self, message_id: str) -> bool:
        try:
            await self._api.call("delete_msg", {"message_id": _wire_id(message_id)})
            return True
        except (AdapterAPIError, ValueError) as e:
            logger.warning("撤回失败: %s", e)
            return False

    # ============================================================
    # 联系人查询
    # ============================================================

    async def list_friends(self) -> list[FriendInfo]:
        return await self._list_friends()

    async def _list_friends(
        self, timeout_seconds: float | None = None
    ) -> list[FriendInfo]:
        data = await self._api.call("get_friend_list", {}, timeout=timeout_seconds)
        items = data if isinstance(data, list) else data.get("friends", [])
        return [
            FriendInfo(
                user_id=str(f.get("user_id", "")),
                nickname=f.get("nickname", "") or "",
                remark=f.get("remark", "") or "",
            )
            for f in items
        ]

    async def list_groups(self) -> list[GroupInfo]:
        data = await self._api.call("get_group_list", {})
        items = data if isinstance(data, list) else data.get("groups", [])
        return [
            GroupInfo(
                group_id=str(g.get("group_id", "")),
                group_name=g.get("group_name", "") or "",
                member_count=int(g.get("member_count", 0) or 0),
            )
            for g in items
        ]

    async def list_group_members(self, group_id: str) -> list[GroupMemberInfo]:
        data = await self._api.call(
            "get_group_member_list", {"group_id": _wire_id(group_id)}
        )
        items = data if isinstance(data, list) else data.get("members", [])
        out: list[GroupMemberInfo] = []
        for member in items:
            role = member.get("role", "member")
            if role not in ("owner", "admin", "member"):
                role = "member"
            out.append(
                GroupMemberInfo(
                    user_id=str(member.get("user_id", "")),
                    nickname=member.get("nickname", "") or "",
                    card=member.get("card", "") or "",
                    role=role,
                )
            )
        return out

    async def get_user_info(self, user_id: str) -> UserInfo:
        data = await self._api.call(
            "get_stranger_info", {"user_id": _wire_id(user_id)}
        )
        return UserInfo(
            user_id=str(data.get("user_id", user_id)),
            nickname=data.get("nickname", "") or "",
            sex=data.get("sex", "unknown") or "unknown",
            age=int(data.get("age", 0) or 0),
            extra={
                key: value
                for key, value in data.items()
                if key not in ("user_id", "nickname", "sex", "age")
            },
        )

    # ============================================================
    # 请求处理
    # ============================================================

    async def handle_friend_request(
        self, flag: str, approve: bool, remark: str = ""
    ) -> dict[str, Any] | None:
        user_id = self._friend_request_users_by_flag.get(flag)
        try:
            await self._api.call(
                "set_friend_add_request",
                {"flag": flag, "approve": approve, "remark": remark},
            )
        except AdapterAPIError as e:
            if not _is_friend_request_missing_error(str(e)) or not user_id:
                raise
            try:
                if await self._is_friend(user_id):
                    self._friend_request_users_by_flag.pop(flag, None)
                    await self._emit_friend_confirmed(user_id)
                    return {
                        "ok": True,
                        "status": "already_friend",
                        "already_handled": True,
                        "flag": flag,
                        "approve": approve,
                        "remark": remark,
                        "user_id": user_id,
                    }
            except Exception as check_error:  # noqa: BLE001
                logger.warning(
                    "好友请求不存在后复查好友列表失败 flag=%s user_id=%s: %s",
                    flag,
                    user_id,
                    check_error,
                )
            raise

        self._friend_request_users_by_flag.pop(flag, None)
        if approve and user_id:
            await self._emit_friend_confirmed(user_id)
        return {
            "ok": True,
            "status": "done",
            "flag": flag,
            "approve": approve,
            "remark": remark,
            "user_id": user_id,
        }

    async def handle_group_request(
        self,
        flag: str,
        sub_type: Literal["add", "invite"],
        approve: bool,
        reason: str = "",
    ) -> None:
        await self._api.call(
            "set_group_add_request",
            {
                "flag": flag,
                "sub_type": sub_type,
                "approve": approve,
                "reason": reason,
            },
        )

    async def _is_friend(
        self, user_id: str, *, timeout_seconds: float | None = None
    ) -> bool:
        friends = await self._list_friends(timeout_seconds)
        return any(str(friend.user_id) == str(user_id) for friend in friends)

    # ============================================================
    # 媒体辅助（方言驱动）
    # ============================================================

    async def fetch_voice_text(self, message_id: str) -> str:
        action = self._dialect.action("voice_stt")
        if action is None:
            raise NotImplementedError(f"{self.name} 方言不支持语音转文字")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 8.0
        await asyncio.sleep(min(max(self._voice_fetch_delay_seconds, 0.0), 8.0))
        last_error: Exception | None = None
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                data = await self._api.call(
                    action,
                    {"message_id": _wire_id(message_id)},
                    timeout=max(0.1, min(3.0, remaining)),
                )
                return data.get("text", "") or ""
            except (AdapterAPIError, ValueError) as e:
                last_error = e
                msg = str(e)
                retryable = "获取语音转文字结果失败" in msg or (
                    "retcode=1200" in msg and action in msg
                )
                remaining = deadline - loop.time()
                if not retryable or remaining <= 0:
                    break
                await asyncio.sleep(min(1.5, max(0.1, remaining)))
            except asyncio.TimeoutError as e:
                last_error = e
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(1.0, max(0.1, remaining)))
        if last_error is not None:
            logger.warning("语音转文字失败 msg_id=%s: %s", message_id, last_error)
        return ""

    async def get_file_url(self, file_id: str) -> str | None:
        candidate = Path(file_id)
        if file_id and candidate.is_absolute() and candidate.is_file():
            return file_id
        action = self._dialect.action("get_file")
        if action is None:
            raise NotImplementedError(f"{self.name} 方言不支持 get_file")
        last_error: Exception | None = None
        for params in (
            {"file_id": file_id, "type": "url"},
            {"file_id": file_id},
            {"file": file_id},
        ):
            try:
                data = await self._api.call(action, params)
                value = (
                    data.get("url")
                    or data.get("file")
                    or data.get("file_path")
                    or data.get("path")
                    or None
                )
                if value:
                    return str(value)
            except AdapterAPIError as e:
                last_error = e
                msg = str(e).lower()
                if "no such file" in msg or "not found" in msg or "不存在" in msg:
                    break
                continue
            except asyncio.TimeoutError as e:
                last_error = e
                break
        if last_error is not None:
            logger.warning("获取文件 URL 失败 file_id=%s: %s", file_id, last_error)
        return None

    async def get_image_url(self, file_id: str) -> str | None:
        candidate = Path(file_id)
        if file_id and candidate.is_absolute() and candidate.is_file():
            return file_id
        action = self._dialect.action("get_image")
        if action is None:
            raise NotImplementedError(f"{self.name} 方言不支持 get_image")
        last_error: Exception | None = None
        for params in ({"file": file_id}, {"file_id": file_id}):
            try:
                data = await self._api.call(action, params)
                value = (
                    data.get("file")
                    or data.get("file_path")
                    or data.get("path")
                    or data.get("url")
                    or None
                )
                if value:
                    return str(value)
            except AdapterAPIError as e:
                last_error = e
                msg = str(e).lower()
                if "no such file" in msg or "not found" in msg or "不存在" in msg:
                    break
                continue
            except asyncio.TimeoutError as e:
                last_error = e
                break
        if last_error is not None:
            logger.warning("获取图片文件失败 file_id=%s: %s", file_id, last_error)
        return None

    async def upload_file(
        self,
        target: Target,
        file_path: Path,
        *,
        display_name: str | None = None,
    ) -> None:
        action = (
            self._dialect.action("upload_private_file")
            if target.scope == "private"
            else self._dialect.action("upload_group_file")
        )
        if action is None:
            raise NotImplementedError(f"{self.name} 方言不支持文件上传")
        params: dict[str, Any] = {
            "file": str(file_path),
            "name": display_name or file_path.name,
        }
        if target.scope == "private":
            params["user_id"] = _wire_id(target.target_id)
        elif target.scope == "group":
            params["group_id"] = _wire_id(target.target_id)
        else:
            raise ValueError(f"未知 scope: {target.scope}")
        await self._api.call(action, params, self_id=target.self_id)

    async def get_forward_msg(self, forward_id: str) -> list[dict[str, Any]]:
        data = await self._api.call("get_forward_msg", {"id": forward_id})
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        messages = data.get("messages", [])
        return messages if isinstance(messages, list) else []

    async def get_group_history(
        self, group_id: str, count: int = 100
    ) -> list[dict[str, Any]]:
        action = self._dialect.action("group_history")
        if action is None:
            raise NotImplementedError(f"{self.name} 方言不支持群历史拉取")
        data = await self._api.call(
            action, {"group_id": _wire_id(group_id), "count": count}
        )
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        messages = data.get("messages", [])
        return messages if isinstance(messages, list) else []

    # ============================================================
    # 高频可选动作
    # ============================================================

    async def get_message(self, message_id: str) -> dict[str, Any]:
        action = self._dialect.action("get_message")
        if action is None:
            raise NotImplementedError(f"{self.name} 方言不支持消息详情读取")
        return await self._api.call(action, {"message_id": _wire_id(message_id)})

    async def send_poke(self, target: Target, user_id: str) -> None:
        action = self._dialect.action("send_poke")
        if action is None:
            raise NotImplementedError(f"{self.name} 方言不支持戳一戳")
        params: dict[str, Any] = {"user_id": _wire_id(user_id)}
        if target.scope == "group":
            params["group_id"] = _wire_id(target.target_id)
        await self._api.call(action, params, self_id=target.self_id)

    async def set_reaction(
        self,
        target: Target,
        message_id: str,
        emoji_id: str,
        *,
        remove: bool = False,
    ) -> None:
        action = self._dialect.action("set_reaction")
        if action is None:
            raise NotImplementedError(f"{self.name} 方言不支持消息表情回应")
        await self._api.call(
            action,
            {
                "message_id": _wire_id(message_id),
                "emoji_id": str(emoji_id),
                "set": not remove,
            },
            self_id=target.self_id,
        )

    async def group_kick(
        self, group_id: str, user_id: str, *, reject_add_request: bool = False
    ) -> None:
        await self._api.call(
            "set_group_kick",
            {
                "group_id": _wire_id(group_id),
                "user_id": _wire_id(user_id),
                "reject_add_request": bool(reject_add_request),
            },
        )

    async def group_ban(
        self, group_id: str, user_id: str, duration_seconds: int
    ) -> None:
        await self._api.call(
            "set_group_ban",
            {
                "group_id": _wire_id(group_id),
                "user_id": _wire_id(user_id),
                "duration": int(duration_seconds),
            },
        )

    async def group_whole_ban(self, group_id: str, enable: bool) -> None:
        await self._api.call(
            "set_group_whole_ban",
            {"group_id": _wire_id(group_id), "enable": bool(enable)},
        )

    async def group_leave(self, group_id: str, *, dismiss: bool = False) -> None:
        await self._api.call(
            "set_group_leave",
            {"group_id": _wire_id(group_id), "is_dismiss": bool(dismiss)},
        )

    # ============================================================
    # 标准查询 / 运维接口（不注册为 LLM 工具）
    # ============================================================

    async def get_login_info(self) -> dict[str, Any]:
        return await self._api.call("get_login_info", {})

    async def get_status(self) -> dict[str, Any]:
        return await self._api.call("get_status", {})

    async def get_version_info(self) -> dict[str, Any]:
        return await self._api.call("get_version_info", {})

    async def can_send_image(self) -> bool:
        data = await self._api.call("can_send_image", {})
        return _yes(data)

    async def can_send_record(self) -> bool:
        data = await self._api.call("can_send_record", {})
        return _yes(data)

    async def set_group_card(
        self, group_id: str, user_id: str, card: str
    ) -> None:
        await self._api.call(
            "set_group_card",
            {
                "group_id": _wire_id(group_id),
                "user_id": _wire_id(user_id),
                "card": str(card),
            },
        )

    async def set_group_name(self, group_id: str, name: str) -> None:
        await self._api.call(
            "set_group_name",
            {"group_id": _wire_id(group_id), "group_name": str(name)},
        )

    async def set_group_admin(
        self, group_id: str, user_id: str, enable: bool = True
    ) -> None:
        await self._api.call(
            "set_group_admin",
            {
                "group_id": _wire_id(group_id),
                "user_id": _wire_id(user_id),
                "enable": bool(enable),
            },
        )

    async def send_like(self, user_id: str, times: int = 1) -> None:
        await self._api.call(
            "send_like", {"user_id": _wire_id(user_id), "times": int(times)}
        )

    async def probe_capabilities(self) -> AdapterCapabilities:
        """启动后探测实现端实际能力。

        建议在连接建立后调用；结果只影响 ``capabilities`` 返回值。
        工具注册表需要重建才能看到变化（Runtime 下一次重载时生效）。
        """
        from dataclasses import replace

        capabilities = self._dialect.capabilities
        try:
            if not await self.can_send_image():
                capabilities = replace(capabilities, send_image=False)
        except Exception:  # noqa: BLE001
            logger.debug("探测 can_send_image 失败，保留方言默认值", exc_info=True)
        try:
            if not await self.can_send_record():
                capabilities = replace(capabilities, send_voice=False)
        except Exception:  # noqa: BLE001
            logger.debug("探测 can_send_record 失败，保留方言默认值", exc_info=True)
        self._capabilities_override = capabilities
        return capabilities

    # ============================================================
    # 通用 API 通道
    # ============================================================

    async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
        return await self._api.call(action, params)

    # ============================================================
    # 内部辅助
    # ============================================================

    def _build_send_params(self, target: Target, message: Any) -> dict[str, Any]:
        if target.scope == "private":
            return {
                "message_type": "private",
                "user_id": _wire_id(target.target_id),
                "message": message,
            }
        if target.scope == "group":
            return {
                "message_type": "group",
                "group_id": _wire_id(target.target_id),
                "message": message,
            }
        raise ValueError(f"OneBot 11 不支持 scope={target.scope!r}")


async def _noop_webhook(_payload: dict[str, Any]) -> dict[str, Any] | None:
    return None


def _yes(data: Any) -> bool:
    """兼容 ``{"yes": true}`` / ``true`` / ``"true"`` 等 OneBot 返回。"""
    if isinstance(data, dict):
        if "yes" in data:
            return bool(data["yes"])
        if "value" in data:
            return bool(data["value"])
        return bool(data.get("ok", False))
    if isinstance(data, str):
        return data.strip().lower() in {"true", "yes", "1", "ok"}
    return bool(data)


def _secret_value(secrets: SecretsManager, secret_id: str | None) -> str | None:
    if not secret_id:
        return None
    value = secrets.get(secret_id)
    if value is None:
        logger.warning("配置引用的 secret_id=%r 在 secrets 中不存在", secret_id)
    return value


__all__ = ["OneBot11Adapter"]
