"""OneBot 11 WebSocket 连接管理（通用传输层）。

两种连接模式（在配置中通过 mode 字段选择，命名从 KarenBot 视角）：

    client（推荐）：
        程序作为 WS 客户端，主动连 OneBot11 的 WS 服务（如 ws://127.0.0.1:3001）。
        OneBot11 端配「正向 WS」服务监听。
        优势：便于程序托管 OneBot11 进程；无需开放本地端口。

    server：
        程序作为 WS 服务端监听端口，OneBot11 作为客户端反向连入。
        OneBot11 端配「反向 WS」客户端，目标地址填本程序的监听地址。
        旧 NoneBot 用户的默认行为。

通用特性：
    - 心跳检测（websockets 自带 ping/pong）
    - 断线自动重连（指数退避，封顶 60 秒）
    - 收发消息回调式投递，非阻塞
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import websockets
from websockets.asyncio.client import ClientConnection as _WSClient
from websockets.asyncio.server import ServerConnection as _WSServer

logger = logging.getLogger(__name__)


# OneBot11 投递过来的原始 JSON 字典
MessageCallback = Callable[[dict], Awaitable[None]]
ConnectionLostCallback = Callable[[], None]
ConnectionState = Literal[
    "idle",
    "connecting",
    "connected",
    "disconnected",
    "stopping",
    "error",
]
_KEEP = object()


@dataclass(slots=True)
class ConnectionStatus:
    state: ConnectionState
    connected: bool
    attempt: int
    last_connected_at: float | None
    last_disconnected_at: float | None
    last_error: str | None
    endpoint: str


class WSConnection(ABC):
    """WebSocket 连接抽象基类。"""

    _DISPATCH_QUEUE_WARN_SIZE = 1024
    _DISPATCH_DRAIN_TIMEOUT_SECONDS = 2.0
    _DISPATCH_QUEUE_WARNING_INTERVAL_SECONDS = 5.0

    def __init__(self) -> None:
        self._callback: MessageCallback | None = None
        self._lost_callbacks: list[ConnectionLostCallback] = []
        self._dispatch_queue: asyncio.Queue[dict] | None = None
        self._dispatch_task: asyncio.Task | None = None
        self._dispatch_queue_warn_size = self._DISPATCH_QUEUE_WARN_SIZE
        self._dispatch_drain_timeout_seconds = self._DISPATCH_DRAIN_TIMEOUT_SECONDS
        self._dispatch_queue_warning_interval_seconds = (
            self._DISPATCH_QUEUE_WARNING_INTERVAL_SECONDS
        )
        self._last_dispatch_queue_warning_at = 0.0

    def on_message(self, callback: MessageCallback) -> None:
        """注册接收到 OneBot11 消息时的回调。"""
        self._callback = callback

    def on_connection_lost(self, callback: ConnectionLostCallback) -> None:
        """注册连接断开回调。用于取消等待中的 API 调用。"""
        self._lost_callbacks.append(callback)

    def _notify_connection_lost(self) -> None:
        for callback in list(self._lost_callbacks):
            try:
                callback()
            except Exception as e:
                logger.exception(f"OneBot11 断线回调失败: {type(e).__name__}: {e}")

    async def _dispatch(self, data: dict) -> None:
        if self._callback is None:
            return
        try:
            await self._callback(data)
        except Exception as e:
            logger.exception(f"OneBot11 消息回调失败: {type(e).__name__}: {e}")

    def _ensure_dispatch_worker(self) -> None:
        task = self._dispatch_task
        if task is not None and not task.done():
            return
        self._dispatch_queue = asyncio.Queue()
        self._dispatch_task = asyncio.create_task(
            self._dispatch_worker(), name="napcat-message-dispatch"
        )

    async def _dispatch_worker(self) -> None:
        queue = self._dispatch_queue
        if queue is None:
            return
        try:
            while True:
                data = await queue.get()
                try:
                    await self._dispatch(data)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            raise

    async def _dispatch_received(self, data: dict) -> None:
        if not isinstance(data, dict):
            logger.warning("OneBot11 WS 忽略非对象 JSON 帧: %s", type(data).__name__)
            return
        if data.get("echo"):
            await self._dispatch(data)
            return

        self._ensure_dispatch_worker()
        queue = self._dispatch_queue
        if queue is None:
            return
        queue.put_nowait(data)
        self._warn_if_dispatch_queue_high(queue)

    def _warn_if_dispatch_queue_high(self, queue: asyncio.Queue[dict]) -> None:
        size = queue.qsize()
        if size < self._dispatch_queue_warn_size:
            return
        now = time.monotonic()
        if (
            now - self._last_dispatch_queue_warning_at
            < self._dispatch_queue_warning_interval_seconds
        ):
            return
        self._last_dispatch_queue_warning_at = now
        logger.warning(
            "OneBot11 消息分发队列积压较多（size=%s warn_size=%s），继续收包并保留 echo 直通",
            size,
            self._dispatch_queue_warn_size,
        )

    async def _stop_dispatch_worker(self, *, drain: bool) -> None:
        task = self._dispatch_task
        queue = self._dispatch_queue
        if task is None:
            self._dispatch_queue = None
            return
        if task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                self._dispatch_task = None
                self._dispatch_queue = None
            return
        if drain and queue is not None:
            try:
                await asyncio.wait_for(
                    queue.join(), timeout=self._dispatch_drain_timeout_seconds
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "OneBot11 消息分发队列 drain 超时（%.1fs），取消消费者；队列中仍有 %s 条未处理上报",
                    self._dispatch_drain_timeout_seconds,
                    queue.qsize(),
                )
        elif queue is not None and queue.qsize() > 0:
            logger.warning(
                "OneBot11 消息分发消费者停止，队列中仍有 %s 条未处理上报",
                queue.qsize(),
            )
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._dispatch_task = None
        self._dispatch_queue = None

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, data: dict, self_id: str | None = None) -> None: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    @property
    @abstractmethod
    def status(self) -> ConnectionStatus: ...

    @abstractmethod
    async def wait_connected(self, timeout: float | None = None) -> bool: ...


class WSClientConnection(WSConnection):
    """程序作为客户端连接 OneBot11（推荐模式）。"""

    def __init__(
        self,
        ws_url: str,
        access_token: str | None = None,
        *,
        reconnect_interval: float = 3.0,
        max_reconnect_attempts: int = -1,
        reconnect_backoff_max: float = 60.0,
        ping_interval: float = 20.0,
        ping_timeout: float = 20.0,
        initial_connect_timeout: float = 10.0,
        fast_reconnect_attempts: int = 5,
        fast_reconnect_interval: float = 0.3,
        reconnect_jitter: float = 0.2,
    ) -> None:
        super().__init__()
        self.ws_url = ws_url
        self.access_token = access_token
        self.reconnect_interval = reconnect_interval
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_backoff_max = reconnect_backoff_max
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.initial_connect_timeout = initial_connect_timeout
        self.fast_reconnect_attempts = max(0, fast_reconnect_attempts)
        self.fast_reconnect_interval = max(0.0, fast_reconnect_interval)
        self.reconnect_jitter = max(0.0, reconnect_jitter)

        self._ws: _WSClient | None = None
        self._loop_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._state: ConnectionStatus = ConnectionStatus(
            state="idle",
            connected=False,
            attempt=0,
            last_connected_at=None,
            last_disconnected_at=None,
            last_error=None,
            endpoint=ws_url,
        )

    @property
    def is_connected(self) -> bool:
        return self._ws is not None

    @property
    def status(self) -> ConnectionStatus:
        return self._state

    async def wait_connected(self, timeout: float | None = None) -> bool:
        if self.is_connected:
            return True
        if timeout is not None and timeout <= 0:
            return self.is_connected
        try:
            if timeout is None:
                await self._connected_event.wait()
            else:
                await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return self.is_connected
        return self.is_connected

    async def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            logger.info("OneBot11 WS 重连循环已在运行: %s", self.ws_url)
            if self.initial_connect_timeout <= 0:
                return
            try:
                await asyncio.wait_for(
                    self._connected_event.wait(), timeout=self.initial_connect_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"OneBot11 初次连接 {self.initial_connect_timeout}s 内未成功，"
                    f"重连循环已在后台运行"
                )
            return

        self._stop_event.clear()
        self._connected_event.clear()
        self._loop_task = asyncio.create_task(self._run_forever(), name="onebot11-ws-client")

        # 等待首次连接成功（不强求，超时也继续——会持续重连）
        if self.initial_connect_timeout <= 0:
            return
        try:
            await asyncio.wait_for(
                self._connected_event.wait(), timeout=self.initial_connect_timeout
            )
            logger.info(f"OneBot11 初次连接成功: {self.ws_url}")
        except asyncio.TimeoutError:
            logger.warning(
                f"OneBot11 初次连接 {self.initial_connect_timeout}s 内未成功，"
                f"重连循环已在后台运行"
            )

    async def stop(self) -> None:
        self._set_state("stopping")
        self._stop_event.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        await self._stop_dispatch_worker(drain=False)
        self._set_state("idle")

    async def send(self, data: dict, self_id: str | None = None) -> None:
        if self._ws is None:
            raise RuntimeError("OneBot11 连接未建立")
        await self._ws.send(json.dumps(data, ensure_ascii=False))

    async def _run_forever(self) -> None:
        attempts = 0
        failure_count = 0

        while not self._stop_event.is_set():
            attempts += 1
            self._set_state("connecting", attempt=attempts, last_error=None)
            try:
                logger.info(f"连接 OneBot11（第 {attempts} 次尝试）: {self.ws_url}")
                headers: dict[str, str] = {}
                if self.access_token:
                    headers["Authorization"] = f"Bearer {self.access_token}"

                async with websockets.connect(
                    self.ws_url,
                    additional_headers=headers if headers else None,
                    ping_interval=self.ping_interval,
                    ping_timeout=self.ping_timeout,
                    max_size=2**24,  # 16 MB，支持大消息（如长群历史）
                    proxy=None,
                ) as ws:
                    self._ws = ws
                    self._connected_event.set()
                    failure_count = 0
                    self._set_state(
                        "connected",
                        connected=True,
                        attempt=attempts,
                        last_connected_at=time.time(),
                        last_error=None,
                    )
                    attempts = 0
                    logger.info(f"OneBot11 WS 已连接: {self.ws_url}")

                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError as e:
                            logger.warning(f"OneBot11 消息 JSON 解析失败: {e}")
                            continue
                        if not isinstance(data, dict):
                            logger.warning("OneBot11 WS 忽略非对象 JSON 帧: %s", type(data).__name__)
                            continue
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(
                                "OneBot11 WS 收到上报 post_type=%s message_type=%s notice_type=%s "
                                "request_type=%s meta_event_type=%s self_id=%s user_id=%s group_id=%s "
                                "message_id=%s raw_bytes=%s keys=%s",
                                data.get("post_type"),
                                data.get("message_type"),
                                data.get("notice_type"),
                                data.get("request_type"),
                                data.get("meta_event_type"),
                                data.get("self_id"),
                                data.get("user_id"),
                                data.get("group_id"),
                                data.get("message_id"),
                                len(raw) if isinstance(raw, (str, bytes)) else None,
                                sorted(str(key) for key in data.keys()),
                            )
                        await self._dispatch_received(data)

                    logger.info("OneBot11 WS 连接已关闭")

            except asyncio.CancelledError:
                raise
            except (
                websockets.ConnectionClosed,
                ConnectionRefusedError,
                OSError,
            ) as e:
                failure_count += 1
                self._set_state(
                    "error",
                    connected=False,
                    attempt=attempts,
                    last_error=f"{type(e).__name__}: {e}",
                )
                logger.warning(f"OneBot11 连接失败: {type(e).__name__}: {e}")
            except Exception as e:
                failure_count += 1
                self._set_state(
                    "error",
                    connected=False,
                    attempt=attempts,
                    last_error=f"{type(e).__name__}: {e}",
                )
                logger.exception(f"OneBot11 连接异常: {e}")
            finally:
                was_connected = self._ws is not None
                self._ws = None
                self._connected_event.clear()
                if was_connected:
                    self._set_state(
                        "disconnected",
                        connected=False,
                        attempt=attempts,
                        last_disconnected_at=time.time(),
                    )
                    self._notify_connection_lost()
                    await self._stop_dispatch_worker(drain=not self._stop_event.is_set())

            if self._stop_event.is_set():
                break

            if 0 < self.max_reconnect_attempts <= attempts:
                logger.error(f"已达最大重连次数 {self.max_reconnect_attempts}，放弃")
                break

            backoff = self._next_delay(failure_count)
            logger.info(f"{backoff:.1f}s 后重试 OneBot11 连接")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                break  # stop 信号到达
            except asyncio.TimeoutError:
                pass

    def _next_delay(self, failure_count: int) -> float:
        if failure_count <= self.fast_reconnect_attempts:
            return self.fast_reconnect_interval
        slow_index = max(1, failure_count - self.fast_reconnect_attempts)
        delay = self.reconnect_interval * (2 ** (slow_index - 1))
        delay = min(delay, self.reconnect_backoff_max)
        if self.reconnect_jitter:
            delay += random.uniform(0, self.reconnect_jitter)
        return delay

    def _set_state(
        self,
        state: ConnectionState,
        *,
        connected: bool | None = None,
        attempt: int | None = None,
        last_connected_at: float | None = None,
        last_disconnected_at: float | None = None,
        last_error: str | None | object = _KEEP,
    ) -> None:
        self._state = ConnectionStatus(
            state=state,
            connected=self.is_connected if connected is None else connected,
            attempt=self._state.attempt if attempt is None else attempt,
            last_connected_at=(
                self._state.last_connected_at
                if last_connected_at is None
                else last_connected_at
            ),
            last_disconnected_at=(
                self._state.last_disconnected_at
                if last_disconnected_at is None
                else last_disconnected_at
            ),
            last_error=(
                self._state.last_error if last_error is _KEEP else last_error
            ),
            endpoint=self.ws_url,
        )


class WSServerConnection(WSConnection):
    """程序作为 WS 服务端监听 OneBot 11 反向连入。

    与旧实现的区别：
      - 不再限制单连接：同一账号允许 API + Event 双连接，多账号可用
        ``X-Self-ID`` 区分；
      - 校验 ``X-Client-Role``（Universal / API / Event）；
      - 鉴权同时接受 ``Authorization: Bearer`` 与 ``?access_token=``；
      - 路径匹配忽略 query string。
    """

    _ROLE_API = "API"
    _ROLE_EVENT = "EVENT"
    _ROLE_UNIVERSAL = "UNIVERSAL"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        path: str = "/onebot/v11/ws",
        access_token: str | None = None,
        *,
        ping_interval: float = 20.0,
        ping_timeout: float = 20.0,
    ) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.path = path
        self.access_token = access_token
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout

        self._server = None
        self._clients: dict[tuple[str, str], _WSServer] = {}
        self._last_connected_key: tuple[str, str] | None = None
        self._stopping = False
        self._connected_event = asyncio.Event()
        self._state: ConnectionStatus = ConnectionStatus(
            state="idle",
            connected=False,
            attempt=0,
            last_connected_at=None,
            last_disconnected_at=None,
            last_error=None,
            endpoint=f"ws://{host}:{port}{path}",
        )

    @property
    def is_connected(self) -> bool:
        return bool(self._clients)

    @property
    def status(self) -> ConnectionStatus:
        return self._state

    async def wait_connected(self, timeout: float | None = None) -> bool:
        if self.is_connected:
            return True
        if timeout is not None and timeout <= 0:
            return self.is_connected
        try:
            if timeout is None:
                await self._connected_event.wait()
            else:
                await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return self.is_connected
        return self.is_connected

    async def start(self) -> None:
        self._stopping = False
        self._set_state("connecting", last_error=None)
        self._server = await websockets.serve(
            self._handle_client,
            self.host,
            self.port,
            ping_interval=self.ping_interval,
            ping_timeout=self.ping_timeout,
            max_size=2**24,
        )
        logger.info(
            f"OneBot11 反向 WS 服务监听: ws://{self.host}:{self.port}{self.path}"
        )
        self._set_state("disconnected")

    async def stop(self) -> None:
        self._stopping = True
        self._set_state("stopping")
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for ws in list(self._clients.values()):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
        self._last_connected_key = None
        self._connected_event.clear()
        await self._stop_dispatch_worker(drain=False)
        self._set_state("idle")

    async def send(self, data: dict, self_id: str | None = None) -> None:
        ws = self._select_send_client(self_id)
        if ws is None:
            raise RuntimeError("OneBot11 尚未连入")
        await ws.send(json.dumps(data, ensure_ascii=False))

    def _select_send_client(self, self_id: str | None = None) -> _WSServer | None:
        """选择承载出站 API 的连接：优先指定账号，其次最近的 API/Universal 连接。"""
        if self_id:
            for role in (self._ROLE_API, self._ROLE_UNIVERSAL):
                ws = self._clients.get((str(self_id), role))
                if ws is not None:
                    return ws
            # 协议端没发 X-Self-ID 时，连接键是 ("", role)，但事件仍带 self_id。
            # 只有在无法区分多账号时才回退，避免把消息发到错误账号。
            api_clients = [
                (sid, ws)
                for (sid, role), ws in self._clients.items()
                if role in {self._ROLE_API, self._ROLE_UNIVERSAL}
            ]
            known_ids = {sid for sid, _ws in api_clients if sid}
            if known_ids and str(self_id) not in known_ids:
                return None
            if len(known_ids) <= 1:
                return api_clients[-1][1] if api_clients else None
            return None
        if self._last_connected_key is not None:
            key = self._last_connected_key
            if key[1] in {self._ROLE_API, self._ROLE_UNIVERSAL}:
                ws = self._clients.get(key)
                if ws is not None:
                    return ws
        for (_sid, role), ws in reversed(list(self._clients.items())):
            if role in {self._ROLE_API, self._ROLE_UNIVERSAL}:
                return ws
        return None

    @staticmethod
    def _request_parts(ws: _WSServer) -> tuple[str, dict[str, str]]:
        from urllib.parse import parse_qs, urlparse

        raw_path = str(getattr(ws.request, "path", "/") or "/")
        parsed = urlparse(raw_path)
        query = {
            key: values[0]
            for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
            if values
        }
        return parsed.path or "/", query

    def _authorized(self, ws: _WSServer, query: dict[str, str]) -> bool:
        if not self.access_token:
            return True
        auth = str(ws.request.headers.get("Authorization", "") or "")
        expected = f"Bearer {self.access_token}"
        if auth and hmac.compare_digest(auth, expected):
            return True
        query_token = query.get("access_token")
        return bool(
            query_token and hmac.compare_digest(query_token, self.access_token)
        )

    async def _handle_client(self, ws: _WSServer) -> None:
        request_path, query = self._request_parts(ws)
        if request_path != self.path:
            logger.warning(
                "OneBot11 连入路径不匹配: %s（期望 %s）", request_path, self.path
            )
            await ws.close(code=4004, reason="path mismatch")
            return

        if not self._authorized(ws, query):
            auth = str(ws.request.headers.get("Authorization", "") or "")
            if not auth and not query.get("access_token"):
                logger.warning(
                    "OneBot11 连入被拒：未带 Authorization 或 access_token。"
                    "程序配置了 access_token 但 OneBot11 那边没填 token / 填错了。"
                )
            else:
                logger.warning(
                    "OneBot11 连入被拒：鉴权信息与程序配置的 token 不一致。"
                )
            await ws.close(code=4001, reason="unauthorized")
            return

        headers = ws.request.headers
        self_id = str(headers.get("X-Self-ID", "") or "").strip()
        role = str(headers.get("X-Client-Role", "") or self._ROLE_UNIVERSAL).strip().upper()
        if role not in {self._ROLE_API, self._ROLE_EVENT, self._ROLE_UNIVERSAL}:
            logger.warning("OneBot11 连入携带未知 X-Client-Role=%r，按 Universal 处理", role)
            role = self._ROLE_UNIVERSAL

        key = (self_id, role)
        old = self._clients.get(key)
        if old is not None and old is not ws:
            logger.info(
                "OneBot11 同账号同角色连接重复（self_id=%r role=%s），关闭旧连接",
                self_id,
                role,
            )
            try:
                await old.close(code=4002, reason="replaced by new connection")
            except Exception:
                pass

        self._clients[key] = ws
        self._last_connected_key = key
        self._connected_event.set()
        self._set_state(
            "connected",
            connected=True,
            last_connected_at=time.time(),
            last_error=None,
        )
        logger.info(
            "OneBot11 已连入: role=%s self_id=%r remote=%s",
            role,
            self_id or "-",
            ws.remote_address,
        )

        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("OneBot11 消息 JSON 解析失败，跳过")
                    continue
                if not isinstance(data, dict):
                    logger.warning("OneBot11 WS 忽略非对象 JSON 帧: %s", type(data).__name__)
                    continue
                await self._dispatch_received(data)
        finally:
            if self._clients.get(key) is ws:
                self._clients.pop(key, None)
            if self._last_connected_key == key:
                self._last_connected_key = next(reversed(self._clients), None)
            if not self._clients:
                self._connected_event.clear()
                self._notify_connection_lost()
                self._set_state(
                    "disconnected",
                    connected=False,
                    last_disconnected_at=time.time(),
                )
            elif not self._has_api_channel():
                # Event 连接还在，但所有可发出站请求的通道都断了：让挂起的 API 立刻失败。
                self._notify_connection_lost()
            # 只有最后一个连接断开时才停共享分发 worker；否则会丢掉其他连接
            # 仍在队列里的事件。
            if not self._clients:
                await self._stop_dispatch_worker(drain=not self._stopping)
            logger.info("OneBot11 连接已断开: role=%s self_id=%r", role, self_id or "-")

    def _has_api_channel(self) -> bool:
        return any(
            role in {self._ROLE_API, self._ROLE_UNIVERSAL}
            for (_sid, role) in self._clients
        )

    def _set_state(
        self,
        state: ConnectionState,
        *,
        connected: bool | None = None,
        last_connected_at: float | None = None,
        last_disconnected_at: float | None = None,
        last_error: str | None | object = _KEEP,
    ) -> None:
        self._state = ConnectionStatus(
            state=state,
            connected=self.is_connected if connected is None else connected,
            attempt=self._state.attempt,
            last_connected_at=(
                self._state.last_connected_at
                if last_connected_at is None
                else last_connected_at
            ),
            last_disconnected_at=(
                self._state.last_disconnected_at
                if last_disconnected_at is None
                else last_disconnected_at
            ),
            last_error=(
                self._state.last_error if last_error is _KEEP else last_error
            ),
            endpoint=f"ws://{self.host}:{self.port}{self.path}",
        )


# ============================================================
# 旧名称兼容（adapters.napcat.connection 的调用方）
# ============================================================

NapCatConnection = WSConnection
ReverseWSConnection = WSClientConnection
ForwardWSConnection = WSServerConnection

__all__ = [
    "ConnectionState",
    "ConnectionStatus",
    "MessageCallback",
    "ConnectionLostCallback",
    "WSConnection",
    "WSClientConnection",
    "WSServerConnection",
    # 旧名称
    "NapCatConnection",
    "ReverseWSConnection",
    "ForwardWSConnection",
]
