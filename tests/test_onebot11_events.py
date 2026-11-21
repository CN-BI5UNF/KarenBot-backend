"""OneBot 11 事件解析测试。"""

from __future__ import annotations

from adapters.onebot11.events import parse_onebot11_event
from adapters.types import (
    FaceSegment,
    ImageSegment,
    IncomingMessage,
    IncomingNotice,
    IncomingRequest,
    MentionSegment,
    MetaEvent,
    NoticeType,
    ReplySegment,
    RequestType,
    TextSegment,
)


def _message_event(**overrides):
    raw = {
        "post_type": "message",
        "message_type": "group",
        "time": 1700000000,
        "self_id": 10000,
        "message_id": 12345,
        "user_id": 20000,
        "group_id": 30000,
        "raw_message": "hi[CQ:at,qq=10000]",
        "sender": {"card": "群名片", "nickname": "昵称"},
        "message": [
            {"type": "text", "data": {"text": "hi"}},
            {"type": "at", "data": {"qq": "10000"}},
            {"type": "reply", "data": {"id": "999"}},
            {"type": "image", "data": {"file": "a.jpg", "url": "https://x/a.jpg"}},
        ],
    }
    raw.update(overrides)
    return raw


def test_parse_group_message() -> None:
    event = parse_onebot11_event("default", _message_event())
    assert isinstance(event, IncomingMessage)
    assert event.scope == "group"
    assert event.message_id == "12345"
    assert event.user_id == "20000"
    assert event.group_id == "30000"
    assert event.nickname == "群名片"
    assert event.self_id == "10000"
    assert event.reply_to == "999"
    assert event.text == "[引用msg_id=999]hi@我[图片]"
    assert event.self_sent is False
    assert isinstance(event.segments[0], TextSegment)
    assert isinstance(event.segments[1], MentionSegment)
    assert isinstance(event.segments[2], ReplySegment)
    assert isinstance(event.segments[3], ImageSegment)
    assert event.media[0].file_id == "a.jpg"
    assert event.source_target.adapter == "default"
    assert event.source_target.scope == "group"
    assert event.source_target.self_id == "10000"


def test_parse_message_sent() -> None:
    raw = _message_event(post_type="message_sent")
    event = parse_onebot11_event("default", raw)
    assert isinstance(event, IncomingMessage)
    assert event.self_sent is True


def test_parse_string_message_falls_back_to_cq() -> None:
    raw = _message_event(message="[CQ:face,id=178]", raw_message="[CQ:face,id=178]")
    event = parse_onebot11_event("default", raw)
    assert isinstance(event, IncomingMessage)
    assert isinstance(event.segments[0], FaceSegment)
    assert event.text == "[斜眼笑]"


def test_parse_group_upload_notice() -> None:
    event = parse_onebot11_event(
        "default",
        {
            "post_type": "notice",
            "notice_type": "group_upload",
            "time": 1,
            "self_id": 100,
            "group_id": 300,
            "user_id": 200,
            "file": {"id": "f1", "name": "a.zip", "size": 10},
        },
    )
    assert isinstance(event, IncomingNotice)
    assert event.notice_type is NoticeType.GROUP_UPLOAD
    assert event.file["name"] == "a.zip"


def test_parse_group_ban_notice() -> None:
    event = parse_onebot11_event(
        "default",
        {
            "post_type": "notice",
            "notice_type": "group_ban",
            "sub_type": "ban",
            "time": 1,
            "self_id": 100,
            "group_id": 300,
            "user_id": 200,
            "operator_id": 101,
            "duration": "600",
        },
    )
    assert isinstance(event, IncomingNotice)
    assert event.notice_type is NoticeType.GROUP_BAN
    assert event.sub_type == "ban"
    assert event.duration == 600
    assert event.operator_id == "101"


def test_parse_lucky_king_and_honor_notice() -> None:
    lucky = parse_onebot11_event(
        "default",
        {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "lucky_king",
            "time": 1,
            "self_id": 100,
            "group_id": 300,
            "user_id": 200,
            "target_id": 200,
        },
    )
    assert isinstance(lucky, IncomingNotice)
    assert lucky.notice_type is NoticeType.LUCKY_KING
    assert lucky.target_id == "200"

    honor = parse_onebot11_event(
        "default",
        {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "honor",
            "time": 1,
            "self_id": 100,
            "group_id": 300,
            "user_id": 200,
            "honor_type": "talkative",
        },
    )
    assert isinstance(honor, IncomingNotice)
    assert honor.notice_type is NoticeType.HONOR
    assert honor.sub_type == "honor"


def test_parse_friend_request() -> None:
    event = parse_onebot11_event(
        "default",
        {
            "post_type": "request",
            "request_type": "friend",
            "time": 1,
            "self_id": 100,
            "user_id": 200,
            "flag": "abc",
            "comment": "你好",
        },
    )
    assert isinstance(event, IncomingRequest)
    assert event.request_type is RequestType.FRIEND
    assert event.flag == "abc"


def test_parse_meta_event() -> None:
    event = parse_onebot11_event(
        "default",
        {
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "sub_type": "heartbeat",
            "time": 1,
            "self_id": 100,
        },
    )
    assert isinstance(event, MetaEvent)
    assert event.meta_type == "heartbeat"


def test_unknown_event_is_ignored() -> None:
    assert parse_onebot11_event("default", {"post_type": "unknown"}) is None
    assert parse_onebot11_event("default", {"echo": "x", "retcode": 0}) is None


def test_empty_message_array_falls_back_to_raw_cq() -> None:
    raw = _message_event(message=[], raw_message="hi[CQ:at,qq=1]")
    event = parse_onebot11_event("default", raw)
    assert isinstance(event, IncomingMessage)
    assert len(event.segments) == 2
    assert event.text == "hi@QQ1"
