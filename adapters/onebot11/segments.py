"""OneBot 11 消息段编解码。

职责：
    - 数组格式：``[{"type": "text", "data": {...}}, ...]`` ↔ ``MessageSegment`` IR
    - 字符串格式：CQ 码 ↔ ``MessageSegment`` IR
    - 严格按 OneBot 11 转义表处理 ``&`` / ``[`` / ``]`` / ``,``

业务层只应接触 ``MessageSegment``；CQ 字符串只是 OneBot 11 的传输方言，
不应泄漏到 ``core``。本模块是唯一允许拼/解 CQ 码的地方。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from adapters.types import (
    FaceSegment,
    FileSegment,
    ForwardSegment,
    ImageSegment,
    MediaSegment,
    MediaType,
    MentionSegment,
    MessageSegment,
    RawSegment,
    ReplySegment,
    TextSegment,
    VideoSegment,
    VoiceSegment,
)
from utils.qq_faces import face_label

_CQ_SEGMENT_RE = re.compile(r"\[CQ:(?P<body>[^\]]+)\]")
_PARAM_SPLIT_RE = re.compile(r",(?=\w+=)")


# ============================================================
# CQ 转义
# ============================================================


def escape_cq_text(value: str) -> str:
    """转义 CQ 文本内容：& [ ]。"""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
    )


def escape_cq_param(value: str) -> str:
    """转义 CQ 参数值：& [ ] ,。"""
    return escape_cq_text(value).replace(",", "&#44;")


def unescape_cq(value: str) -> str:
    """还原 CQ 转义；按标准只处理四个实体。"""
    text = str(value)
    text = text.replace("&#44;", ",")
    text = text.replace("&#91;", "[")
    text = text.replace("&#93;", "]")
    text = text.replace("&amp;", "&")
    return text


def parse_cq_params(params_str: str) -> dict[str, str]:
    if not params_str:
        return {}
    params: dict[str, str] = {}
    for part in _PARAM_SPLIT_RE.split(params_str):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        params[key] = unescape_cq(value)
    return params


# ============================================================
# OneBot 11 段 → IR
# ============================================================


def segment_from_onebot(segment: Any) -> MessageSegment:
    """把一个 OneBot 11 段字典转换成 IR；未知类型落到 RawSegment。"""
    if not isinstance(segment, dict):
        return TextSegment(text=str(segment))

    seg_type = str(segment.get("type") or "")
    data = segment.get("data")
    if not isinstance(data, dict):
        data = {}

    if seg_type == "text":
        return TextSegment(text=str(data.get("text") or ""))

    if seg_type == "at":
        return MentionSegment(
            user_id=str(data.get("qq") or ""),
            display_name=_optional_str(data.get("name")),
        )

    if seg_type == "reply":
        return ReplySegment(message_id=str(data.get("id") or ""))

    if seg_type == "image":
        return ImageSegment(
            file_id=_optional_str(data.get("file")),
            url=_optional_str(data.get("url")),
            path=_optional_str(data.get("path") or data.get("file_path")),
            summary=_optional_str(data.get("summary") or data.get("sub_type")),
        )

    if seg_type in {"record", "voice"}:
        return VoiceSegment(
            file_id=_optional_str(data.get("file")),
            url=_optional_str(data.get("url")),
            path=_optional_str(data.get("path") or data.get("file_path")),
            duration=_optional_float(data.get("duration")),
        )

    if seg_type == "video":
        return VideoSegment(
            file_id=_optional_str(data.get("file")),
            url=_optional_str(data.get("url")),
            path=_optional_str(data.get("path") or data.get("file_path")),
        )

    if seg_type == "file":
        url = _optional_str(data.get("url") or data.get("path") or data.get("file_path"))
        file_id = _optional_str(data.get("file_id") or data.get("file"))
        name = _optional_str(data.get("file_name") or data.get("name")) or _basename(
            url or file_id
        )
        size = _optional_int(data.get("file_size") or data.get("size"))
        return FileSegment(file_id=file_id, url=url, name=name, size=size)

    if seg_type == "face":
        face_id = str(data.get("id") or "")
        return FaceSegment(face_id=face_id, label=face_label(face_id))

    if seg_type == "forward":
        return ForwardSegment(
            forward_id=str(data.get("id") or ""),
            extra=_forward_extra(data),
        )

    return RawSegment(
        platform_type=seg_type or "unknown",
        data=dict(data),
        fallback_text=_raw_fallback_text(seg_type, data),
    )


def segments_from_onebot(
    message: Any,
    raw_message: str = "",
) -> list[MessageSegment]:
    """把 OneBot 11 的 message（数组或 CQ 字符串）转换成 IR 列表。"""
    if isinstance(message, dict):
        return [segment_from_onebot(message)]
    if isinstance(message, list) and message:
        return [segment_from_onebot(item) for item in message]
    if isinstance(message, str) and message:
        return cq_to_segments(message)
    if raw_message:
        return cq_to_segments(raw_message)
    return []


# ============================================================
# IR → OneBot 11 段 / CQ
# ============================================================


def segment_to_onebot(segment: MessageSegment) -> dict[str, Any]:
    """把 IR 段转换成 OneBot 11 数组格式的段字典。"""
    if isinstance(segment, TextSegment):
        return {"type": "text", "data": {"text": segment.text}}

    if isinstance(segment, MentionSegment):
        data: dict[str, Any] = {"qq": segment.user_id}
        if segment.display_name:
            data["name"] = segment.display_name
        return {"type": "at", "data": data}

    if isinstance(segment, ReplySegment):
        return {"type": "reply", "data": {"id": segment.message_id}}

    if isinstance(segment, ImageSegment):
        file_value = _media_source(
            file_id=segment.file_id,
            url=segment.url,
            path=segment.path,
            b64=segment.b64,
        )
        data = {"file": file_value}
        if segment.url and not segment.url.startswith("base64://"):
            data["url"] = segment.url
        if segment.summary:
            data["summary"] = segment.summary
        return {"type": "image", "data": data}

    if isinstance(segment, VoiceSegment):
        return {
            "type": "record",
            "data": {
                "file": _media_source(
                    file_id=segment.file_id, url=segment.url, path=segment.path
                )
            },
        }

    if isinstance(segment, VideoSegment):
        return {
            "type": "video",
            "data": {
                "file": _media_source(
                    file_id=segment.file_id, url=segment.url, path=segment.path
                )
            },
        }

    if isinstance(segment, FileSegment):
        data = {
            "file": _media_source(
                file_id=segment.file_id, url=segment.url, path=segment.path
            )
        }
        if segment.name:
            data["name"] = segment.name
        return {"type": "file", "data": data}

    if isinstance(segment, FaceSegment):
        return {"type": "face", "data": {"id": segment.face_id}}

    if isinstance(segment, ForwardSegment):
        return {"type": "forward", "data": {"id": segment.forward_id}}

    if isinstance(segment, RawSegment):
        if not segment.platform_type:
            return {"type": "text", "data": {"text": segment.fallback_text}}
        return {"type": segment.platform_type, "data": dict(segment.data)}

    raise TypeError(f"未知消息段类型: {type(segment).__name__}")


def segments_to_onebot(segments: list[MessageSegment]) -> list[dict[str, Any]]:
    return [segment_to_onebot(item) for item in segments]


def segment_to_cq(segment: MessageSegment) -> str:
    """把 IR 段转换成 CQ 码字符串。"""
    if isinstance(segment, TextSegment):
        return escape_cq_text(segment.text)

    if isinstance(segment, MentionSegment):
        return f"[CQ:at,qq={escape_cq_param(segment.user_id)}]"

    if isinstance(segment, ReplySegment):
        return f"[CQ:reply,id={escape_cq_param(segment.message_id)}]"

    if isinstance(segment, ImageSegment):
        file_value = _media_source(
            file_id=segment.file_id,
            url=segment.url,
            path=segment.path,
            b64=segment.b64,
        )
        return f"[CQ:image,file={escape_cq_param(file_value)}]"

    if isinstance(segment, VoiceSegment):
        file_value = _media_source(
            file_id=segment.file_id, url=segment.url, path=segment.path
        )
        return f"[CQ:record,file={escape_cq_param(file_value)}]"

    if isinstance(segment, VideoSegment):
        file_value = _media_source(
            file_id=segment.file_id, url=segment.url, path=segment.path
        )
        return f"[CQ:video,file={escape_cq_param(file_value)}]"

    if isinstance(segment, FileSegment):
        file_value = _media_source(
            file_id=segment.file_id, url=segment.url, path=segment.path
        )
        params = [f"file={escape_cq_param(file_value)}"]
        if segment.name:
            params.append(f"name={escape_cq_param(segment.name)}")
        return f"[CQ:file,{','.join(params)}]"

    if isinstance(segment, FaceSegment):
        return f"[CQ:face,id={escape_cq_param(segment.face_id)}]"

    if isinstance(segment, ForwardSegment):
        return f"[CQ:forward,id={escape_cq_param(segment.forward_id)}]"

    if isinstance(segment, RawSegment):
        if not segment.platform_type:
            return escape_cq_text(segment.fallback_text)
        if not segment.data:
            return escape_cq_text(segment.fallback_text)
        params = ",".join(
            f"{key}={escape_cq_param(str(value))}"
            for key, value in segment.data.items()
        )
        return f"[CQ:{segment.platform_type},{params}]"

    raise TypeError(f"未知消息段类型: {type(segment).__name__}")


def segments_to_cq(segments: list[MessageSegment]) -> str:
    return "".join(segment_to_cq(item) for item in segments)


def segments_to_message(
    segments: list[MessageSegment], *, message_format: str = "array"
) -> list[dict[str, Any]] | str:
    """按 OneBot 11 配置输出数组或字符串格式。"""
    if message_format == "string":
        return segments_to_cq(segments)
    return segments_to_onebot(segments)


# ============================================================
# CQ 字符串 → IR
# ============================================================


def cq_to_segments(raw: str) -> list[MessageSegment]:
    """解析 CQ 码字符串；未闭合/非法片段按普通文本保留。"""
    if not raw:
        return []

    out: list[MessageSegment] = []
    text_buffer: list[str] = []

    def flush_text() -> None:
        if text_buffer:
            out.append(TextSegment(text=unescape_cq("".join(text_buffer))))
            text_buffer.clear()

    index = 0
    length = len(raw)
    while index < length:
        match = _CQ_SEGMENT_RE.search(raw, index)
        if match is None:
            text_buffer.append(raw[index:])
            break
        if match.start() > index:
            text_buffer.append(raw[index : match.start()])
        body = match.group("body")
        if "," in body:
            cq_type, params_str = body.split(",", 1)
        else:
            cq_type, params_str = body, ""
        if not cq_type:
            text_buffer.append(match.group(0))
            index = match.end()
            continue
        flush_text()
        out.append(_cq_body_to_segment(cq_type, parse_cq_params(params_str), body))
        index = match.end()

    flush_text()
    return out


def _cq_body_to_segment(
    cq_type: str, params: dict[str, str], raw_body: str
) -> MessageSegment:
    if cq_type == "text":
        return TextSegment(text=unescape_cq(params.get("text", "")))
    if cq_type == "at":
        return MentionSegment(
            user_id=params.get("qq", ""), display_name=params.get("name")
        )
    if cq_type == "reply":
        return ReplySegment(message_id=params.get("id", ""))
    if cq_type == "image":
        return ImageSegment(
            file_id=params.get("file"),
            url=params.get("url"),
            path=params.get("path") or params.get("file_path"),
            summary=params.get("summary"),
        )
    if cq_type in {"record", "voice"}:
        return VoiceSegment(
            file_id=params.get("file"),
            url=params.get("url"),
            path=params.get("path") or params.get("file_path"),
            duration=_optional_float(params.get("duration")),
        )
    if cq_type == "video":
        return VideoSegment(
            file_id=params.get("file"),
            url=params.get("url"),
            path=params.get("path") or params.get("file_path"),
        )
    if cq_type == "file":
        url = params.get("url") or params.get("path") or params.get("file_path")
        file_id = params.get("file_id") or params.get("file")
        return FileSegment(
            file_id=file_id,
            url=url,
            name=params.get("file_name") or params.get("name") or _basename(url or file_id),
            size=_optional_int(params.get("file_size") or params.get("size")),
        )
    if cq_type == "face":
        face_id = params.get("id", "")
        return FaceSegment(face_id=face_id, label=face_label(face_id))
    if cq_type == "forward":
        return ForwardSegment(
            forward_id=params.get("id", ""), extra=_forward_extra(params)
        )
    return RawSegment(
        platform_type=cq_type,
        data=dict(params),
        fallback_text=_raw_fallback_text(cq_type, params),
    )


# ============================================================
# 可读文本与媒体提取
# ============================================================


def segment_to_text(segment: MessageSegment, *, self_id: str | None = None) -> str:
    """把单个段渲染为给模型看的可读文本。"""
    if isinstance(segment, TextSegment):
        return segment.text
    if isinstance(segment, MentionSegment):
        if segment.user_id == "all":
            return "@全体成员"
        if self_id and segment.user_id == str(self_id):
            return "@我"
        return f"@QQ{segment.user_id}"
    if isinstance(segment, ReplySegment):
        return f"[引用msg_id={segment.message_id}]"
    if isinstance(segment, ImageSegment):
        return "[图片]"
    if isinstance(segment, VoiceSegment):
        return "[语音]"
    if isinstance(segment, VideoSegment):
        return "[视频]"
    if isinstance(segment, FileSegment):
        return f"[文件 {segment.name}]" if segment.name else "[文件]"
    if isinstance(segment, FaceSegment):
        return segment.label or face_label(segment.face_id)
    if isinstance(segment, ForwardSegment):
        title = segment.extra.get("title") or segment.extra.get("summary")
        if title:
            return f"[合并转发 id={segment.forward_id} title={title}]"
        return f"[合并转发 id={segment.forward_id}]"
    return segment.fallback_text or f"[{segment.platform_type}]"


def segments_to_text(
    segments: list[MessageSegment], *, self_id: str | None = None
) -> str:
    """渲染可读文本；引用段前置，与旧 parse_raw_cq 行为保持一致。"""
    head: list[str] = []
    body: list[str] = []
    for segment in segments:
        text = segment_to_text(segment, self_id=self_id)
        if not text:
            continue
        if isinstance(segment, ReplySegment):
            head.append(text)
        else:
            body.append(text)
    return "".join(head + body)


def extract_media(segments: list[MessageSegment]) -> list[MediaSegment]:
    """从 IR 中提取旧的 MediaSegment 视图，供现有媒体管线平滑消费。"""
    out: list[MediaSegment] = []
    for segment in segments:
        if isinstance(segment, ImageSegment):
            out.append(
                MediaSegment(
                    type=MediaType.IMAGE,
                    file_id=segment.file_id,
                    url=segment.url or segment.file_id,
                )
            )
        elif isinstance(segment, VoiceSegment):
            out.append(
                MediaSegment(
                    type=MediaType.VOICE,
                    file_id=segment.file_id,
                    url=segment.url,
                )
            )
        elif isinstance(segment, VideoSegment):
            out.append(
                MediaSegment(
                    type=MediaType.VIDEO,
                    file_id=segment.file_id,
                    url=segment.url,
                )
            )
        elif isinstance(segment, FileSegment):
            out.append(
                MediaSegment(
                    type=MediaType.FILE,
                    file_id=segment.file_id,
                    name=segment.name,
                    url=segment.url,
                )
            )
        elif isinstance(segment, ForwardSegment):
            out.append(
                MediaSegment(
                    type=MediaType.FORWARD,
                    file_id=segment.forward_id,
                    extra=dict(segment.extra),
                )
            )
    return out


# ============================================================
# 辅助
# ============================================================


def _media_source(
    *,
    file_id: str | None = None,
    url: str | None = None,
    path: str | None = None,
    b64: str | None = None,
) -> str:
    if b64:
        return b64 if b64.startswith("base64://") else f"base64://{b64}"
    if path:
        absolute = str(Path(path).absolute()).replace("\\", "/").lstrip("/")
        return f"file:///{absolute}"
    if file_id:
        return str(file_id)
    if url:
        return str(url)
    return ""


def _raw_fallback_text(seg_type: str, data: dict[str, Any]) -> str:
    labels = {
        "rps": "[猜拳]",
        "dice": "[骰子]",
        "shake": "[窗口抖动]",
        "poke": "[戳一戳]",
        "anonymous": "[匿名消息]",
        "share": "[分享链接]",
        "contact": "[推荐联系人]",
        "location": "[位置]",
        "music": "[音乐分享]",
        "xml": "[XML 卡片]",
        "json": "[JSON 卡片]",
        "node": "[合并转发节点]",
    }
    if seg_type in labels:
        return labels[seg_type]
    title = data.get("title") or data.get("name") or data.get("content")
    if title:
        return f"[{seg_type}: {title}]"
    return f"[{seg_type}]"


def _forward_extra(data: dict[str, Any]) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    for key in ("title", "summary", "name", "content"):
        value = data.get(key)
        if value:
            extra[key] = value
    return extra


def _basename(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).replace("\\", "/")
    name = text.rsplit("/", 1)[-1]
    return name or None


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


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


__all__ = [
    "escape_cq_text",
    "escape_cq_param",
    "unescape_cq",
    "parse_cq_params",
    "segment_from_onebot",
    "segments_from_onebot",
    "segment_to_onebot",
    "segments_to_onebot",
    "segment_to_cq",
    "segments_to_cq",
    "segments_to_message",
    "cq_to_segments",
    "segment_to_text",
    "segments_to_text",
    "extract_media",
]
