"""Telegram Update 解析测试。"""

from __future__ import annotations

from adapters.telegram.events import parse_telegram_update
from adapters.types import (
    FileSegment,
    ImageSegment,
    IncomingMessage,
    IncomingNotice,
    MentionSegment,
    NoticeType,
    ReplySegment,
    TextSegment,
    VoiceSegment,
)


def _private_update(**overrides):
    message = {
        "message_id": 10,
        "date": 1700000000,
        "chat": {"id": 42, "type": "private", "first_name": "Alice"},
        "from": {"id": 42, "first_name": "Alice", "username": "alice"},
        "text": "hello",
    }
    message.update(overrides)
    return {"update_id": 1, "message": message}


def test_parse_private_text() -> None:
    event = parse_telegram_update("tg", _private_update(), self_id="999")
    assert isinstance(event, IncomingMessage)
    assert event.scope == "private"
    assert event.user_id == "42"
    assert event.nickname == "Alice"
    assert event.self_id == "999"
    assert event.message_id == "10"
    assert event.text == "hello"
    assert event.segments == [TextSegment(text="hello")]
    assert event.source_target.target_id == "42"
    assert event.source_target.scope == "private"


def test_parse_group_reply_and_photo() -> None:
    update = _private_update(
        chat={"id": -100, "type": "supergroup", "title": "群"},
        message_id=11,
        text="看这个",
        reply_to_message={"message_id": 9},
        photo=[
            {"file_id": "small", "width": 10, "height": 10},
            {"file_id": "big", "width": 800, "height": 600},
        ],
        message_thread_id=77,
    )
    event = parse_telegram_update("tg", update, self_id="999")
    assert isinstance(event, IncomingMessage)
    assert event.scope == "group"
    assert event.group_id == "-100"
    assert event.thread_id == "77"
    assert event.reply_to == "9"
    assert event.text == "[引用msg_id=9]看这个[图片]"
    assert isinstance(event.segments[0], ReplySegment)
    assert isinstance(event.segments[-1], ImageSegment)
    assert event.segments[-1].file_id == "big"
    assert event.media[0].file_id == "big"


def test_parse_text_mention() -> None:
    update = _private_update(
        text="@Alice hi",
        entities=[{"type": "text_mention", "offset": 0, "length": 6,
                   "user": {"id": 42, "first_name": "Alice"}}],
    )
    event = parse_telegram_update("tg", update, self_id="999")
    assert isinstance(event, IncomingMessage)
    assert isinstance(event.segments[0], MentionSegment)
    assert event.segments[0].user_id == "42"
    assert event.text == "@Alice hi"


def test_parse_voice_and_document() -> None:
    update = _private_update(
        text=None,
        caption="文件来了",
        voice={"file_id": "v1", "duration": 3},
        document={"file_id": "d1", "file_name": "a.pdf", "file_size": 100},
    )
    event = parse_telegram_update("tg", update, self_id="999")
    assert isinstance(event, IncomingMessage)
    assert any(isinstance(seg, VoiceSegment) and seg.file_id == "v1" for seg in event.segments)
    assert any(isinstance(seg, FileSegment) and seg.name == "a.pdf" for seg in event.segments)
    assert event.text == "文件来了[语音][文件 a.pdf]"


def test_parse_callback_query() -> None:
    update = {
        "update_id": 3,
        "callback_query": {
            "id": "cb",
            "from": {"id": 42, "first_name": "Alice"},
            "message": {"message_id": 10, "chat": {"id": 42, "type": "private"}},
            "data": "btn",
        },
    }
    event = parse_telegram_update("tg", update, self_id="999")
    assert isinstance(event, IncomingNotice)
    assert event.notice_type is NoticeType.OTHER
    assert event.sub_type == "callback_query"
    assert event.user_id == "42"


def test_parse_channel_post_as_group() -> None:
    update = {
        "update_id": 4,
        "channel_post": {
            "message_id": 12,
            "date": 1,
            "chat": {"id": -200, "type": "channel", "title": "频道"},
            "text": "公告",
        },
    }
    event = parse_telegram_update("tg", update, self_id="999")
    assert isinstance(event, IncomingMessage)
    assert event.scope == "group"
    assert event.group_id == "-200"
    assert event.text == "公告"


def test_utf16_entity_offsets_with_emoji() -> None:
    update = _private_update(
        text="😀 @Alice hi",
        entities=[{"type": "text_mention", "offset": 3, "length": 6,
                   "user": {"id": 42, "first_name": "Alice"}}],
    )
    event = parse_telegram_update("tg", update, self_id="999")
    assert isinstance(event, IncomingMessage)
    assert isinstance(event.segments[1], MentionSegment)
    assert event.segments[1].user_id == "42"
    assert event.text == "😀 @Alice hi"


def test_new_chat_members_become_increase_notice() -> None:
    update = _private_update(
        chat={"id": -100, "type": "supergroup", "title": "群"},
        text=None,
        new_chat_members=[{"id": 42, "first_name": "Alice"}],
    )
    event = parse_telegram_update("tg", update, self_id="999")
    assert isinstance(event, IncomingNotice)
    assert event.notice_type is NoticeType.GROUP_INCREASE
    assert event.user_id == "42"
    assert event.group_id == "-100"


def test_bot_username_mention_renders_as_me() -> None:
    update = _private_update(
        text="@karen_bot hi",
        entities=[{"type": "mention", "offset": 0, "length": 10}],
    )
    event = parse_telegram_update(
        "tg", update, self_id="999", self_username="karen_bot"
    )
    assert isinstance(event, IncomingMessage)
    assert isinstance(event.segments[0], MentionSegment)
    assert event.segments[0].user_id == "999"
    assert event.text == "@我 hi"
