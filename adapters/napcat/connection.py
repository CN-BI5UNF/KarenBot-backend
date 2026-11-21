"""兼容旧路径：WS 连接已迁移到 ``adapters.transports.ws``。"""

from __future__ import annotations

from adapters.transports.ws import (
    ConnectionLostCallback,
    ConnectionState,
    ConnectionStatus,
    ForwardWSConnection,
    MessageCallback,
    NapCatConnection,
    ReverseWSConnection,
    WSClientConnection,
    WSConnection,
    WSServerConnection,
)

__all__ = [
    "ConnectionState",
    "ConnectionStatus",
    "MessageCallback",
    "ConnectionLostCallback",
    "WSConnection",
    "WSClientConnection",
    "WSServerConnection",
    "NapCatConnection",
    "ReverseWSConnection",
    "ForwardWSConnection",
]
