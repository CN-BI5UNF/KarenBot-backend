"""OneBot 11 消息段编解码测试。"""

from __future__ import annotations

from adapters.onebot11.segments import (
    cq_to_segments,
    escape_cq_param,
    escape_cq_text,
    extract_media,
    segment_from_onebot,
    segment_to_onebot,
    segments_from_onebot,
    segments_to_cq,
    segments_to_message,
    segments_to_text,
    unescape_cq,
)
from adapters.types import (
    FaceSegment,
    ForwardSegment,
    ImageSegment,
    MentionSegment,
    RawSegment,
    ReplySegment,
    TextSegment,
    VoiceSegment,
)


def test_cq_escape_round_trip() -> None:
    value = "a&b[c]d,e"
    assert unescape_cq(escape_cq_text(value)) == "a&b[c]d,e"
    assert unescape_cq(escape_cq_param(value)) == value
    assert escape_cq_param(value) == "a&amp;b&#91;c&#93;d&#44;e"


def test_parse_cq_string_with_special_characters() -> None:
    segments = cq_to_segments(
        "你好[CQ:at,qq=123] [CQ:reply,id=456]"
        "[CQ:image,file=a.jpg,url=https://example.com/a.jpg]"
    )
    assert segments[0] == TextSegment(text="你好")
    assert segments[1] == MentionSegment(user_id="123")
    assert segments[2] == TextSegment(text=" ")
    assert segments[3] == ReplySegment(message_id="456")
    assert isinstance(segments[4], ImageSegment)
    assert segments[4].file_id == "a.jpg"
    assert segments[4].url == "https://example.com/a.jpg"


def test_parse_cq_text_entities_are_unescaped() -> None:
    segments = cq_to_segments("a&amp;b&#91;x&#93;&#44;")
    assert segments == [TextSegment(text="a&b[x],")]


def test_array_segments_decode_and_encode() -> None:
    payload = [
        {"type": "text", "data": {"text": "hello"}},
        {"type": "at", "data": {"qq": "all"}},
        {"type": "image", "data": {"file": "a.jpg", "url": "https://x/a.jpg"}},
        {"type": "record", "data": {"file": "b.mp3"}},
        {"type": "face", "data": {"id": "178"}},
        {"type": "forward", "data": {"id": "f1", "title": "聊天记录"}},
        {"type": "json", "data": {"data": "{}"}},
    ]
    segments = segments_from_onebot(payload)
    assert segments[0] == TextSegment(text="hello")
    assert segments[1] == MentionSegment(user_id="all")
    assert isinstance(segments[2], ImageSegment)
    assert isinstance(segments[3], VoiceSegment)
    assert isinstance(segments[4], FaceSegment)
    assert isinstance(segments[5], ForwardSegment)
    assert isinstance(segments[6], RawSegment)

    encoded = [segment_to_onebot(item) for item in segments]
    assert encoded[0] == {"type": "text", "data": {"text": "hello"}}
    assert encoded[1] == {"type": "at", "data": {"qq": "all"}}


def test_reply_is_rendered_first() -> None:
    segments = cq_to_segments("正文[CQ:reply,id=9]")
    assert segments_to_text(segments) == "[引用msg_id=9]正文"


def test_mention_self_and_all_rendering() -> None:
    segments = [
        MentionSegment(user_id="123"),
        TextSegment(text=" "),
        MentionSegment(user_id="all"),
    ]
    assert segments_to_text(segments, self_id="123") == "@我 @全体成员"


def test_raw_segment_fallback_text() -> None:
    segment = segment_from_onebot({"type": "dice", "data": {}})
    assert isinstance(segment, RawSegment)
    assert segment.fallback_text == "[骰子]"


def test_extract_media_keeps_legacy_view() -> None:
    segments = segments_from_onebot(
        [
            {"type": "image", "data": {"file": "a.jpg"}},
            {"type": "file", "data": {"file": "report.pdf", "name": "report.pdf"}},
        ]
    )
    media = extract_media(segments)
    assert [item.type.value for item in media] == ["image", "file"]


def test_message_format_output() -> None:
    segments = cq_to_segments("hi[CQ:at,qq=1]")
    array = segments_to_message(segments, message_format="array")
    assert array == [
        {"type": "text", "data": {"text": "hi"}},
        {"type": "at", "data": {"qq": "1"}},
    ]
    string = segments_to_message(segments, message_format="string")
    assert string == "hi[CQ:at,qq=1]"


def test_cq_param_escaping_round_trip() -> None:
    segments = cq_to_segments("[CQ:at,qq=123&#44;456]")
    assert segments == [MentionSegment(user_id="123,456")]
    assert segments_to_cq(segments) == "[CQ:at,qq=123&#44;456]"
