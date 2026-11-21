"""通用异步网络传输原语。

这些类不包含任何 QQ / OneBot 业务语义，协议适配器按需组合：
    - ``WSClientConnection``  / ``WSServerConnection``：OneBot 11 正向/反向 WS
    - ``HTTPAPITransport``：HTTP API 出站
    - ``WebhookServer``：HTTP POST 入站
"""

from .http import HTTPAPITransport
from .webhook import WebhookServer, verify_signature
from .ws import (
    ConnectionLostCallback,
    ConnectionState,
    ConnectionStatus,
    MessageCallback,
    WSClientConnection,
    WSConnection,
    WSServerConnection,
)

__all__ = [
    "WSConnection",
    "WSClientConnection",
    "WSServerConnection",
    "ConnectionStatus",
    "ConnectionState",
    "MessageCallback",
    "ConnectionLostCallback",
    "HTTPAPITransport",
    "WebhookServer",
    "verify_signature",
]
