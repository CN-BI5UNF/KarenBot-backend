"""OneBot 11 事件解析 —— JSON → 统一事件类型。

覆盖规范中的四类事件：
    - message（含 message_sent 自身发送回显）
    - notice（群文件、禁言、运气王、群荣誉、戳一戳等）
    - request（加好友 / 加群 / 邀请）
    - meta_event（lifecycle / heartbeat）

消息正文优先从结构化 ``message`` 数组解析；只有字符串时才回退到 CQ 码。
"""

from __future__ import annotations

import logging
from typing import Any

from adapters.types import (
    AnyEvent,
    IncomingMessage,
    IncomingNotice,
    IncomingRequest,
    MetaEvent,
    NoticeType,
    ReplySegment,
    RequestType,
)

from .segments import extract_media, segments_from_onebot, segments_to_text

logger = logging.getLogger(__name__)


def parse_onebot11_event(adapter_name: str, raw: dict[str, Any]) -> AnyEvent | None:
    """主入口：把 OneBot 11 上报的 JSON 转成统一事件类型。"""
    post_type = raw.get("post_type")
    if post_type == "message":
        return _parse_message(adapter_name, raw)
    if post_type == "message_sent":
        return _parse_message(adapter_name, raw, self_sent=True)
    if post_type == "notice":
        return _parse_notice(adapter_name, raw)
    if post_type == "request":
        return _parse_request(adapter_name, raw)
    if post_type == "meta_event":
        return _parse_meta(adapter_name, raw)
    # 没有 post_type：通常是 API 响应（有 echo 字段），不应到达这里
    if "echo" in raw or "retcode" in raw:
        return None
    logger.debug(
        "未识别的 OneBot11 事件 post_type=%s keys=%s",
        post_type,
        sorted(str(key) for key in raw.keys()),
    )
    return None


# 旧名称兼容
parse_napcat_event = parse_onebot11_event


# ============================================================
# Message
# ============================================================


def _parse_message(
    adapter_name: str,
    raw: dict[str, Any],
    *,
    self_sent: bool = False,
) -> IncomingMessage | None:
    try:
        msg_type = raw["message_type"]
        message_id = str(raw["message_id"])
        user_id = str(raw["user_id"])
        self_id = str(raw.get("self_id", ""))
        group_id = str(raw["group_id"]) if msg_type == "group" else None
    except KeyError as e:
        logger.warning("消息事件缺字段: %s", e)
        return None

    if msg_type not in {"private", "group"}:
        logger.debug("忽略未知 message_type=%r", msg_type)
        return None

    sender = raw.get("sender") or {}
    nickname = (
        sender.get("card") or sender.get("nickname") or "未知"
        if msg_type == "group"
        else sender.get("nickname") or "未知"
    )
    raw_message_str = raw.get("raw_message", "") or ""
    message_payload = raw.get("message", []) or []
    segments = segments_from_onebot(message_payload, raw_message_str)
    text = segments_to_text(segments, self_id=self_id)

    reply_to: str | None = None
    for segment in segments:
        if isinstance(segment, ReplySegment) and segment.message_id:
            reply_to = segment.message_id
            break

    return IncomingMessage(
        adapter=adapter_name,
        timestamp=_timestamp(raw),
        self_id=self_id,
        message_id=message_id,
        scope=msg_type,
        user_id=user_id,
        nickname=nickname,
        group_id=group_id,
        text=text,
        raw_message=raw_message_str,
        segments=segments,
        media=extract_media(segments),
        reply_to=reply_to,
        self_sent=self_sent,
        raw=raw,
    )


# ============================================================
# Notice
# ============================================================


_NOTICE_TYPE_MAP: dict[str, NoticeType] = {
    "group_recall": NoticeType.GROUP_RECALL,
    "friend_recall": NoticeType.FRIEND_RECALL,
    "group_increase": NoticeType.GROUP_INCREASE,
    "group_decrease": NoticeType.GROUP_DECREASE,
    "group_admin": NoticeType.GROUP_ADMIN,
    "group_upload": NoticeType.GROUP_UPLOAD,
    "group_ban": NoticeType.GROUP_BAN,
    "friend_add": NoticeType.FRIEND_ADD,
}


def _parse_notice(adapter_name: str, raw: dict[str, Any]) -> IncomingNotice | None:
    nt_raw = str(raw.get("notice_type", "") or "")
    sub_type = str(raw.get("sub_type", "") or "")

    if nt_raw == "notify":
        nt = {
            "poke": NoticeType.POKE,
            "lucky_king": NoticeType.LUCKY_KING,
            "honor": NoticeType.HONOR,
        }.get(sub_type, NoticeType.OTHER)
    else:
        nt = _NOTICE_TYPE_MAP.get(nt_raw, NoticeType.OTHER)

    file_info: dict[str, Any] = {}
    if nt is NoticeType.GROUP_UPLOAD:
        file_payload = raw.get("file")
        if isinstance(file_payload, dict):
            file_info = {str(key): value for key, value in file_payload.items()}

    target_id = raw.get("target_id")
    if target_id is None:
        target_id = raw.get("user_id") if nt in {
            NoticeType.LUCKY_KING,
            NoticeType.HONOR,
            NoticeType.GROUP_BAN,
        } else None

    return IncomingNotice(
        adapter=adapter_name,
        timestamp=_timestamp(raw),
        self_id=str(raw.get("self_id", "")),
        notice_type=nt,
        user_id=_opt_str(raw.get("user_id")),
        group_id=_opt_str(raw.get("group_id")),
        operator_id=_opt_str(raw.get("operator_id")),
        message_id=_opt_str(raw.get("message_id")),
        sub_type=sub_type,
        target_id=_opt_str(target_id),
        duration=_opt_int(raw.get("duration")),
        file=file_info,
        raw=raw,
    )


# ============================================================
# Request
# ============================================================


def _parse_request(adapter_name: str, raw: dict[str, Any]) -> IncomingRequest | None:
    req_type = raw.get("request_type", "")
    if req_type == "friend":
        rt = RequestType.FRIEND
    elif req_type == "group":
        sub = raw.get("sub_type", "")
        rt = RequestType.GROUP_ADD if sub == "add" else RequestType.GROUP_INVITE
    else:
        logger.debug("未识别请求 request_type=%s", req_type)
        return None

    flag = str(raw.get("flag", ""))
    if not flag:
        logger.warning("请求事件缺 flag: %s", raw)
        return None

    return IncomingRequest(
        adapter=adapter_name,
        timestamp=_timestamp(raw),
        self_id=str(raw.get("self_id", "")),
        request_type=rt,
        flag=flag,
        user_id=str(raw.get("user_id", "")),
        group_id=_opt_str(raw.get("group_id")),
        comment=raw.get("comment", "") or "",
        raw=raw,
    )


# ============================================================
# Meta
# ============================================================


def _parse_meta(adapter_name: str, raw: dict[str, Any]) -> MetaEvent:
    return MetaEvent(
        adapter=adapter_name,
        timestamp=_timestamp(raw),
        self_id=str(raw.get("self_id", "")),
        meta_type=raw.get("meta_event_type", ""),
        sub_type=raw.get("sub_type", ""),
        raw=raw,
    )


# ============================================================
# 辅助
# ============================================================


def _timestamp(raw: dict[str, Any]) -> float:
    try:
        return float(raw.get("time", 0))
    except (TypeError, ValueError):
        return 0.0


def _opt_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v)
    return s if s else None


def _opt_int(v: Any) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


__all__ = ["parse_onebot11_event", "parse_napcat_event"]
