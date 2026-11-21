"""Telegram Update → 内部事件模型。"""

from __future__ import annotations

import logging
from typing import Any

from adapters.types import (
    AnyEvent,
    FileSegment,
    ImageSegment,
    IncomingMessage,
    IncomingNotice,
    MediaSegment,
    MediaType,
    MentionSegment,
    MessageSegment,
    NoticeType,
    RawSegment,
    ReplySegment,
    TextSegment,
    VideoSegment,
    VoiceSegment,
)

logger = logging.getLogger(__name__)


def parse_telegram_update(
    adapter_name: str,
    update: dict[str, Any],
    *,
    self_id: str = "",
    self_username: str = "",
) -> AnyEvent | None:
    """把一条 Telegram Update 转换成统一事件；不认识的类型返回 None。"""
    if not isinstance(update, dict):
        return None

    if "callback_query" in update:
        return _parse_callback_query(adapter_name, update, self_id=self_id)

    # edited_message 默认忽略，避免同一内容被当作新消息再次回复。
    message = update.get("message")
    if isinstance(message, dict):
        notice = _parse_service_message(adapter_name, message, self_id=self_id)
        if notice is not None:
            return notice
    for key in ("message", "channel_post"):
        payload = update.get(key)
        if isinstance(payload, dict):
            return _parse_message(
                adapter_name,
                payload,
                self_id=self_id,
                self_username=self_username,
            )
    return None


def _parse_message(
    adapter_name: str,
    message: dict[str, Any],
    *,
    self_id: str,
    self_username: str = "",
) -> IncomingMessage | None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        logger.debug("Telegram message 缺 chat.id，忽略")
        return None

    chat_type = str(chat.get("type") or "private")
    scope = "private" if chat_type == "private" else "group"
    group_id = None if scope == "private" else str(chat_id)

    sender = message.get("from") or {}
    user_id = str(sender.get("id") or (chat_id if scope == "group" else ""))
    nickname = _display_name(sender) or str(chat.get("title") or chat.get("username") or "未知")

    text = str(message.get("text") or message.get("caption") or "")
    entities = (
        message.get("entities")
        if message.get("text")
        else message.get("caption_entities")
    ) or []

    segments: list[MessageSegment] = []
    reply = message.get("reply_to_message")
    if isinstance(reply, dict) and reply.get("message_id") is not None:
        segments.append(ReplySegment(message_id=str(reply["message_id"])))

    segments.extend(
        _text_segments(
            text,
            entities,
            self_id=self_id,
            self_username=self_username,
        )
    )
    segments.extend(_media_segments(message))

    thread_id = message.get("message_thread_id")
    reply_to = None
    if isinstance(reply, dict) and reply.get("message_id") is not None:
        reply_to = str(reply["message_id"])

    rendered = _render_text(segments, self_id=self_id)

    return IncomingMessage(
        adapter=adapter_name,
        timestamp=float(message.get("date") or 0),
        self_id=str(self_id or ""),
        message_id=str(message.get("message_id") or ""),
        scope=scope,
        user_id=user_id,
        nickname=nickname,
        group_id=group_id,
        thread_id=str(thread_id) if thread_id is not None else None,
        text=rendered,
        raw_message=text,
        segments=segments,
        media=_extract_media(segments),
        reply_to=reply_to,
        raw=message,
    )


def _parse_service_message(
    adapter_name: str, message: dict[str, Any], *, self_id: str
) -> IncomingNotice | None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None or chat.get("type") == "private":
        return None
    sender = message.get("from") or {}
    new_members = message.get("new_chat_members")
    left_member = message.get("left_chat_member")
    if isinstance(new_members, list) and new_members:
        first = new_members[0] if isinstance(new_members[0], dict) else {}
        return IncomingNotice(
            adapter=adapter_name,
            timestamp=float(message.get("date") or 0),
            self_id=str(self_id or ""),
            notice_type=NoticeType.GROUP_INCREASE,
            sub_type="add",
            user_id=str(first.get("id") or "") or None,
            group_id=str(chat_id),
            operator_id=str(sender.get("id") or "") or None,
            target_id=str(first.get("id") or "") or None,
            raw=message,
        )
    if isinstance(left_member, dict):
        return IncomingNotice(
            adapter=adapter_name,
            timestamp=float(message.get("date") or 0),
            self_id=str(self_id or ""),
            notice_type=NoticeType.GROUP_DECREASE,
            sub_type="leave",
            user_id=str(left_member.get("id") or "") or None,
            group_id=str(chat_id),
            operator_id=str(sender.get("id") or "") or None,
            target_id=str(left_member.get("id") or "") or None,
            raw=message,
        )
    return None


def _parse_callback_query(
    adapter_name: str,
    update: dict[str, Any],
    *,
    self_id: str,
) -> IncomingNotice:
    query = update.get("callback_query") or {}
    sender = query.get("from") or {}
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    return IncomingNotice(
        adapter=adapter_name,
        timestamp=float(query.get("date") or 0),
        self_id=str(self_id or ""),
        notice_type=NoticeType.OTHER,
        sub_type="callback_query",
        user_id=str(sender.get("id") or "") or None,
        group_id=(
            str(chat.get("id"))
            if chat.get("type") in {"group", "supergroup", "channel"}
            else None
        ),
        target_id=str(sender.get("id") or "") or None,
        raw=update,
    )


def _text_segments(
    text: str,
    entities: list[Any],
    *,
    self_id: str = "",
    self_username: str = "",
) -> list[MessageSegment]:
    if not text:
        return []
    mentions = _entity_mentions(
        text, entities, self_id=self_id, self_username=self_username
    )
    if not mentions:
        return [TextSegment(text=text)]

    segments: list[MessageSegment] = []
    cursor = 0
    for start, end, user_id, display in mentions:
        if start > cursor:
            segments.append(TextSegment(text=text[cursor:start]))
        segments.append(
            MentionSegment(user_id=user_id, display_name=display or None)
        )
        cursor = end
    if cursor < len(text):
        segments.append(TextSegment(text=text[cursor:]))
    return segments


def _entity_mentions(
    text: str,
    entities: list[Any],
    *,
    self_id: str = "",
    self_username: str = "",
) -> list[tuple[int, int, str, str]]:
    mentions: list[tuple[int, int, str, str]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_type = str(entity.get("type") or "")
        if entity_type not in {"mention", "text_mention"}:
            continue
        try:
            utf16_offset = int(entity.get("offset"))
            utf16_length = int(entity.get("length"))
        except (TypeError, ValueError):
            continue
        if utf16_offset < 0 or utf16_length <= 0:
            continue
        start = _utf16_offset_to_index(text, utf16_offset)
        end = _utf16_offset_to_index(text, utf16_offset + utf16_length)
        if start >= end or end > len(text):
            continue
        fragment = text[start:end]
        user = entity.get("user") if isinstance(entity.get("user"), dict) else {}
        fragment_name = fragment.lstrip("@")
        user_id = str(user.get("id") or fragment_name or "")
        if (
            entity_type == "mention"
            and self_username
            and fragment_name.casefold() == str(self_username).casefold()
        ):
            user_id = str(self_id or user_id)
        display = " ".join(
            part
            for part in (user.get("first_name"), user.get("last_name"))
            if part
        ) or fragment_name
        mentions.append((start, end, user_id, display))
    mentions.sort(key=lambda item: item[0])
    return mentions


def _utf16_offset_to_index(text: str, offset: int) -> int:
    """Telegram 实体偏移按 UTF-16 code unit 计算，转换为 Python 索引。"""
    if offset <= 0:
        return 0
    units = 0
    for index, char in enumerate(text):
        if units >= offset:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
    return len(text)


def _media_segments(message: dict[str, Any]) -> list[MessageSegment]:
    segments: list[MessageSegment] = []

    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        best = photos[-1] if isinstance(photos[-1], dict) else {}
        if best.get("file_id"):
            summary = (
                f"{best.get('width')}x{best.get('height')}"
                if best.get("width") and best.get("height")
                else None
            )
            segments.append(
                ImageSegment(file_id=str(best["file_id"]), summary=summary)
            )

    voice = message.get("voice")
    if isinstance(voice, dict) and voice.get("file_id"):
        segments.append(
            VoiceSegment(
                file_id=str(voice["file_id"]),
                duration=_optional_float(voice.get("duration")),
            )
        )

    video = message.get("video") or message.get("video_note")
    if isinstance(video, dict) and video.get("file_id"):
        segments.append(VideoSegment(file_id=str(video["file_id"])))

    animation = message.get("animation")
    if isinstance(animation, dict) and animation.get("file_id"):
        segments.append(VideoSegment(file_id=str(animation["file_id"])))

    audio = message.get("audio")
    if isinstance(audio, dict) and audio.get("file_id"):
        name = str(audio.get("file_name") or audio.get("title") or "audio")
        segments.append(
            FileSegment(
                file_id=str(audio["file_id"]),
                name=name,
                size=_optional_int(audio.get("file_size")),
            )
        )

    document = message.get("document")
    if isinstance(document, dict) and document.get("file_id"):
        segments.append(
            FileSegment(
                file_id=str(document["file_id"]),
                name=str(document.get("file_name") or "document"),
                size=_optional_int(document.get("file_size")),
            )
        )

    sticker = message.get("sticker")
    if isinstance(sticker, dict):
        segments.append(
            RawSegment(
                platform_type="sticker",
                data={
                    "file_id": str(sticker.get("file_id") or ""),
                    "emoji": sticker.get("emoji"),
                    "is_animated": sticker.get("is_animated"),
                },
                fallback_text=f"[贴纸 {sticker.get('emoji') or ''}]".strip(),
            )
        )

    location = message.get("location")
    if isinstance(location, dict):
        segments.append(
            RawSegment(
                platform_type="location",
                data=dict(location),
                fallback_text="[位置]",
            )
        )

    contact = message.get("contact")
    if isinstance(contact, dict):
        segments.append(
            RawSegment(
                platform_type="contact",
                data=dict(contact),
                fallback_text="[联系人]",
            )
        )

    poll = message.get("poll")
    if isinstance(poll, dict):
        segments.append(
            RawSegment(
                platform_type="poll",
                data=dict(poll),
                fallback_text="[投票]",
            )
        )

    dice = message.get("dice")
    if isinstance(dice, dict):
        segments.append(
            RawSegment(
                platform_type="dice",
                data=dict(dice),
                fallback_text=f"[骰子 {dice.get('value') or ''}]".strip(),
            )
        )

    return segments


def _render_text(segments: list[MessageSegment], *, self_id: str) -> str:
    head: list[str] = []
    body: list[str] = []
    for segment in segments:
        if isinstance(segment, ReplySegment):
            head.append(f"[引用msg_id={segment.message_id}]")
        elif isinstance(segment, TextSegment):
            body.append(segment.text)
        elif isinstance(segment, MentionSegment):
            if self_id and segment.user_id == str(self_id):
                body.append("@我")
            else:
                name = segment.display_name or segment.user_id
                body.append(f"@{name}")
        elif isinstance(segment, ImageSegment):
            body.append("[图片]")
        elif isinstance(segment, VoiceSegment):
            body.append("[语音]")
        elif isinstance(segment, VideoSegment):
            body.append("[视频]")
        elif isinstance(segment, FileSegment):
            body.append(f"[文件 {segment.name}]" if segment.name else "[文件]")
        elif isinstance(segment, RawSegment):
            body.append(segment.fallback_text or f"[{segment.platform_type}]")
    return "".join(head + body)


def _extract_media(segments: list[MessageSegment]) -> list[MediaSegment]:
    out: list[MediaSegment] = []
    for segment in segments:
        if isinstance(segment, ImageSegment):
            out.append(
                MediaSegment(
                    type=MediaType.IMAGE,
                    file_id=segment.file_id,
                )
            )
        elif isinstance(segment, VoiceSegment):
            out.append(
                MediaSegment(
                    type=MediaType.VOICE,
                    file_id=segment.file_id,
                )
            )
        elif isinstance(segment, VideoSegment):
            out.append(
                MediaSegment(
                    type=MediaType.VIDEO,
                    file_id=segment.file_id,
                )
            )
        elif isinstance(segment, FileSegment):
            out.append(
                MediaSegment(
                    type=MediaType.FILE,
                    file_id=segment.file_id,
                    name=segment.name,
                )
            )
    return out


def _display_name(user: dict[str, Any]) -> str:
    if not isinstance(user, dict):
        return ""
    parts = [
        str(user.get(key) or "").strip()
        for key in ("first_name", "last_name")
        if str(user.get(key) or "").strip()
    ]
    if parts:
        return " ".join(parts)
    return str(user.get("username") or "").strip()


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["parse_telegram_update"]
