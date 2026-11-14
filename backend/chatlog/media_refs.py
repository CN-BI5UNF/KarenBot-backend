"""从 DisplayItem 文本/元数据里提取可预览或下载的本地媒体引用。

媒体管线会把 QQ 图片/视频/文件落下到 workspace，并在正文里留下
``[图片 workspace=incoming/...]`` 一类占位。这里只认 workspace 内的相对
路径；远程 URL 不直接下发，避免前端误把任意地址当成可访问资源。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from .models import DisplayItem

_PLACEHOLDER_RE = re.compile(
    r"\[(?P<label>[^\[\]\n]{0,240}?)"
    r"workspace=(?P<path>(?:incoming|workspace|outgoing)[/\\][^\s\]]+)"
    r"(?P<extra>[^\]]*)\]",
    re.IGNORECASE,
)

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".amr": "audio/amr",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".zip": "application/zip",
}


def _safe_path(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/").lstrip("/")
    # 兼容 workspace=workspace/incoming/... 的旧写法。
    while text.startswith("workspace/"):
        text = text[len("workspace/") :]
    parts = [part for part in text.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _asset_index(item: DisplayItem) -> dict[str, dict[str, Any]]:
    raw = item.raw if isinstance(item.raw, dict) else {}
    record = raw.get("record") if isinstance(raw.get("record"), dict) else raw
    metadata = record.get("metadata") if isinstance(record, dict) else None
    if not isinstance(metadata, dict):
        return {}
    assets: dict[str, dict[str, Any]] = {}
    for key in ("image_assets", "video_assets"):
        values = metadata.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            path = _safe_path(str(value.get("path") or ""))
            if not path:
                continue
            assets[path] = dict(value)
    return assets


def _kind_from_label(label: str, mime: str) -> str:
    lowered = label.casefold()
    if "图片" in lowered or "image" in lowered:
        return "image"
    if "视频" in lowered or "video" in lowered:
        return "video"
    if "音频" in lowered or "语音" in lowered or mime.startswith("audio/"):
        return "audio"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    return "file"


def _mime_for(name: str, asset: dict[str, Any] | None) -> str:
    if asset:
        mime = str(asset.get("mime") or "").strip()
        if mime:
            return mime
    return _MIME_BY_EXT.get(PurePosixPath(name).suffix.casefold(), "")


def _attachment(
    *,
    path: str,
    name: str,
    kind: str,
    asset: dict[str, Any] | None,
) -> dict[str, Any]:
    mime = _mime_for(name, asset)
    size = None
    width = None
    height = None
    if asset:
        for key, target in (
            ("byte_size", "size"),
            ("width", "width"),
            ("height", "height"),
        ):
            value = asset.get(key)
            if isinstance(value, int) and value >= 0:
                if target == "size":
                    size = value
                elif target == "width":
                    width = value
                else:
                    height = value
    return {
        "kind": kind,
        "name": name[:240],
        "path": path,
        "mime": mime,
        "size": size,
        "width": width,
        "height": height,
    }


def extract_media_attachments(item: DisplayItem) -> list[dict[str, Any]]:
    """返回 item 里落到 workspace 的附件；没有则返回空列表。"""
    text = str(item.text or "")
    assets = _asset_index(item)
    attachments: list[dict[str, Any]] = []
    seen: set[str] = set()

    for match in _PLACEHOLDER_RE.finditer(text):
        path = _safe_path(match.group("path"))
        if not path or path in seen:
            continue
        name = PurePosixPath(path).name
        asset = assets.get(path)
        kind = _kind_from_label(match.group("label"), _mime_for(name, asset))
        attachments.append(
            _attachment(path=path, name=name, kind=kind, asset=asset)
        )
        seen.add(path)

    # 图片/视频可能被 Pillow 转码后只在 metadata 里出现，正文占位仍是旧路径。
    # 但 legacy header 拆出来的多条消息共享同一份 metadata，不能把整条记录的
    # 所有图片都挂到每一条拆分项上。
    raw = item.raw if isinstance(item.raw, dict) else {}
    if raw.get("legacy_header"):
        return attachments
    for path, asset in assets.items():
        if path in seen:
            continue
        name = PurePosixPath(path).name
        mime = _mime_for(name, asset)
        kind = "image" if mime.startswith("image/") else "video"
        attachments.append(
            _attachment(path=path, name=name, kind=kind, asset=asset)
        )
        seen.add(path)
    return attachments


__all__ = ["extract_media_attachments"]
