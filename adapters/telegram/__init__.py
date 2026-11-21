"""Telegram Bot API 适配器。"""

from __future__ import annotations

from adapters.registry import known_adapter_types, register_adapter_type

from .adapter import TELEGRAM_CAPABILITIES, TelegramAdapter
from .client import TelegramAPIClient, TelegramAPIError, media_upload
from .events import parse_telegram_update

if "telegram" not in known_adapter_types():
    register_adapter_type("telegram", TelegramAdapter.from_config)

__all__ = [
    "TelegramAdapter",
    "TELEGRAM_CAPABILITIES",
    "TelegramAPIClient",
    "TelegramAPIError",
    "parse_telegram_update",
    "media_upload",
]
