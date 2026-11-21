"""业务层发送动作 → 结构化 MessageSegment。

发送队列里的 action 是纯字典（便于持久化/回执），本模块把它转换成
协议无关的 ``MessageSegment`` 列表，再交给 ``IAdapter.send_message``。

历史兼容：
    - 旧 action 可能没有 ``reply_to`` / ``mentions`` 字段，而是在 content
      里写 ``[CQ:reply]`` / ``[CQ:at]`` / ``[CQ:face]``。这里只在边界处
      解析这三种旧标记；新代码应使用结构化字段。
    - 其他 CQ 标记一律按普通文本保留，不再由业务层解释。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .types import (
    FaceSegment,
    ImageSegment,
    MentionSegment,
    MessageSegment,
    ReplySegment,
    TextSegment,
)

_LEGACY_MARKER_RE = re.compile(r"\[CQ:(?P<kind>at|reply|face),(?P<body>[^\]]*)\]")
_PARAM_SPLIT_RE = re.compile(r",(?=\w+=)")


def legacy_text_to_segments(text: str) -> list[MessageSegment]:
    """把旧提示词格式解析成 IR；未识别的 CQ 标记按文本保留。"""
    if not text:
        return []

    out: list[MessageSegment] = []
    buffer: list[str] = []
    index = 0

    def flush() -> None:
        if buffer:
            out.append(TextSegment(text="".join(buffer)))
            buffer.clear()

    while index < len(text):
        match = _LEGACY_MARKER_RE.search(text, index)
        if match is None:
            buffer.append(text[index:])
            break
        if match.start() > index:
            buffer.append(text[index : match.start()])
        kind = match.group("kind")
        params = _parse_params(match.group("body"))
        if kind == "at":
            flush()
            out.append(MentionSegment(user_id=params.get("qq", "")))
        elif kind == "reply":
            flush()
            out.append(ReplySegment(message_id=params.get("id", "")))
        elif kind == "face":
            flush()
            out.append(FaceSegment(face_id=params.get("id", "")))
        else:  # pragma: no cover - 正则已限定
            buffer.append(match.group(0))
        index = match.end()

    flush()
    return out


def action_to_segments(action: Mapping[str, Any]) -> list[MessageSegment]:
    """把发送队列 action 转成结构化消息段。

    支持的 kind：text / face / emoji / image。voice 由发送管线单独处理。
    """
    kind = str(action.get("kind") or "text")
    if kind == "face":
        face_id = str(action.get("face_id") or "")
        segments: list[MessageSegment] = (
            [FaceSegment(face_id=face_id)]
            if face_id
            else legacy_text_to_segments(str(action.get("content") or ""))
        )
    elif kind in {"emoji", "image"}:
        image_path = str(action.get("image_path") or "") or None
        image_url = str(action.get("image_url") or "") or None
        if not image_path and not image_url:
            raise ValueError("image action 缺少 image_path / image_url")
        segments = [ImageSegment(path=image_path, url=image_url)]
    elif kind == "voice":
        raise ValueError("voice action 必须由 send_voice 处理")
    else:
        segments = legacy_text_to_segments(str(action.get("content") or ""))

    if not segments:
        segments = [TextSegment(text="")]

    # 结构化 @ 列表：放在正文前，避免模型在正文里手写 @。
    mentioned: set[str] = {
        segment.user_id
        for segment in segments
        if isinstance(segment, MentionSegment)
    }
    prefix: list[MessageSegment] = []
    for raw_user_id in action.get("mentions") or []:
        user_id = str(raw_user_id).strip()
        if not user_id or user_id in mentioned:
            continue
        mentioned.add(user_id)
        prefix.extend([MentionSegment(user_id=user_id), TextSegment(text=" ")])
    if prefix:
        segments = prefix + segments

    # 引用回复：只在没有显式 reply 段时插入到最前面。
    reply_to = str(action.get("reply_to") or "").strip()
    if reply_to and not any(isinstance(seg, ReplySegment) for seg in segments):
        segments = [ReplySegment(message_id=reply_to), *segments]

    return segments


def _parse_params(body: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for part in _PARAM_SPLIT_RE.split(body or ""):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        params[key.strip()] = value.strip()
    return params


__all__ = ["action_to_segments", "legacy_text_to_segments"]
