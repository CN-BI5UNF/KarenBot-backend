"""发送 action → MessageSegment 结构化转换测试。"""

from __future__ import annotations

import pytest

from adapters.outbound import action_to_segments, legacy_text_to_segments
from adapters.types import (
    FaceSegment,
    ImageSegment,
    MentionSegment,
    ReplySegment,
    TextSegment,
)


def test_legacy_markers_are_converted() -> None:
    segments = legacy_text_to_segments(
        "[CQ:reply,id=9]你好[CQ:at,qq=123][CQ:face,id=178]"
    )
    assert segments == [
        ReplySegment(message_id="9"),
        TextSegment(text="你好"),
        MentionSegment(user_id="123"),
        FaceSegment(face_id="178"),
    ]


def test_unknown_cq_marker_stays_text() -> None:
    segments = legacy_text_to_segments("a[CQ:json,data=x]b")
    assert segments == [TextSegment(text="a[CQ:json,data=x]b")]


def test_reply_to_is_prepended_without_duplicate() -> None:
    segments = action_to_segments({"kind": "text", "content": "hi", "reply_to": "9"})
    assert segments == [ReplySegment(message_id="9"), TextSegment(text="hi")]

    already = action_to_segments(
        {"kind": "text", "content": "[CQ:reply,id=9]hi", "reply_to": "9"}
    )
    assert sum(isinstance(item, ReplySegment) for item in already) == 1


def test_mentions_are_prepended_and_deduped() -> None:
    segments = action_to_segments(
        {
            "kind": "text",
            "content": "hi",
            "mentions": ["1", "2", "1"],
        }
    )
    assert segments[0] == MentionSegment(user_id="1")
    assert segments[1] == TextSegment(text=" ")
    assert segments[2] == MentionSegment(user_id="2")
    assert segments[3] == TextSegment(text=" ")
    assert segments[4] == TextSegment(text="hi")


def test_face_action_uses_face_segment() -> None:
    segments = action_to_segments(
        {"kind": "face", "face_id": "178", "content": "", "label": "[斜眼笑]"}
    )
    assert segments == [FaceSegment(face_id="178")]


def test_image_action_uses_image_segment() -> None:
    segments = action_to_segments(
        {"kind": "image", "image_path": "/tmp/a.png", "image_url": ""}
    )
    assert segments == [ImageSegment(path="/tmp/a.png", url=None)]


def test_voice_action_must_be_handled_by_send_voice() -> None:
    with pytest.raises(ValueError):
        action_to_segments({"kind": "voice", "audio_path": "/tmp/a.mp3"})
