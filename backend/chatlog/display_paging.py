"""DisplayItem 分页：一次归一化，按稳定 item id 向前切片。

对话页不再适合用 ``/api/chats/page`` 那种原始记录 Keyset 分页：
展示项经过工具配对、发送回执去重、跨来源合并和过滤，和原始记录不是
一一对应。本模块保留完整归一化语义，但把结果按对话缓存起来，后续
“加载更早”只做列表切片，不再重复跑整条 DisplayItem 管线。

缓存按“记录前缀”校验：新消息只会追加到末尾，旧前缀不变时缓存继续
有效；历史被压缩、删除或改写导致前缀变化时自动重建。分页游标只记录
“下一页从哪个 item 之前开始”，不暴露内部索引。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .display_items import _text_has_media_or_file
from .display_pipeline import build_display_items, display_items_json
from .media_refs import extract_media_attachments
from .models import DisplayItem
from .records import _load_chat_page_records_and_groups

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 300
MAX_PAGE_SIZE = 1000
_MAX_SNAPSHOTS = 4
_SNAPSHOT_TTL_SECONDS = 30 * 60.0
_CURSOR_VERSION = 1
# 每个进程独立生成签名密钥。进程重启后旧游标会被拒绝，前端回到最新页
# 重新开始，这比接受一个无法对应任何快照的旧游标更安全。
_CURSOR_KEY = secrets.token_bytes(32)


class DisplayPageError(RuntimeError):
    """分页失败；``reset_required`` 表示前端应丢弃已加载的旧页。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        reset_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reset_required = reset_required


@dataclass
class _Snapshot:
    items: list[DisplayItem]
    prefix_hash: str
    prefix_len: int
    total_records: int
    filters_hash: str
    created_at: float
    last_used_at: float
    generation: str


class _DisplayPageCache:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.snapshots: OrderedDict[str, _Snapshot] = OrderedDict()

    def get(self, key: str) -> _Snapshot | None:
        snapshot = self.snapshots.get(key)
        if snapshot is None:
            return None
        if time.monotonic() - snapshot.last_used_at > _SNAPSHOT_TTL_SECONDS:
            self.snapshots.pop(key, None)
            return None
        snapshot.last_used_at = time.monotonic()
        self.snapshots.move_to_end(key)
        return snapshot

    def put(self, key: str, snapshot: _Snapshot) -> None:
        self.snapshots[key] = snapshot
        self.snapshots.move_to_end(key)
        while len(self.snapshots) > _MAX_SNAPSHOTS:
            self.snapshots.popitem(last=False)


_CACHE_BY_RUNTIME: weakref.WeakKeyDictionary[Any, _DisplayPageCache] = (
    weakref.WeakKeyDictionary()
)


def _cache_for_runtime(runtime: Any) -> _DisplayPageCache:
    cache = _CACHE_BY_RUNTIME.get(runtime)
    if cache is None:
        cache = _DisplayPageCache()
        _CACHE_BY_RUNTIME[runtime] = cache
    return cache


def _runtime_persona_name(runtime: Any) -> str:
    persona = getattr(runtime, "persona", None)
    return str(getattr(persona, "name", "") or "")


def _filters_signature(
    *,
    persona_name: str,
    search_text: str,
    show_chat: bool,
    show_system: bool,
    show_tools: bool,
    show_reasoning: bool,
    media_only: bool,
) -> tuple[Any, ...]:
    return (
        persona_name,
        str(search_text or "").strip(),
        bool(show_chat),
        bool(show_system),
        bool(show_tools),
        bool(show_reasoning),
        bool(media_only),
    )


def _filters_hash(filters: tuple[Any, ...]) -> str:
    raw = json.dumps(filters, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _snapshot_key(conversation_id: str, filters_hash: str) -> str:
    return f"{conversation_id}\x00{filters_hash}"


def _record_token(record: dict[str, Any]) -> str:
    """参与前缀校验的轻量指纹；避免每次分页都序列化整条记录。"""
    identity = (
        record.get("message_id")
        or record.get("msg_id")
        or record.get("original_msg_id")
        or record.get("archive_id")
        or record.get("tool_call_id")
        or record.get("id")
        or ""
    )
    return "\x1f".join(
        (
            str(record.get("_sort_layer") or record.get("_source") or ""),
            str(
                record.get("timestamp")
                or record.get("time")
                or record.get("created_at")
                or ""
            ),
            str(identity),
            str(record.get("role") or ""),
            str(len(str(record.get("content") or ""))),
        )
    )


def _prefix_hash(records: list[dict[str, Any]], length: int) -> str:
    digest = hashlib.sha256()
    for record in records[:length]:
        digest.update(_record_token(record).encode("utf-8", "replace"))
        digest.update(b"\n")
    return digest.hexdigest()


def _snapshot_matches(snapshot: _Snapshot, records: list[dict[str, Any]]) -> bool:
    if len(records) < snapshot.prefix_len:
        return False
    return _prefix_hash(records, snapshot.prefix_len) == snapshot.prefix_hash


def _encode_cursor(
    *,
    conversation_id: str,
    filters_hash: str,
    before_id: str,
    generation: str,
) -> str:
    payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "c": conversation_id,
            "f": filters_hash,
            "b": before_id,
            "g": generation,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(_CURSOR_KEY, payload, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(payload + b"." + signature).decode("ascii")


def _decode_cursor(token: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        if len(raw) <= 17 or raw[-17:-16] != b".":
            raise ValueError("bad cursor framing")
        payload, signature = raw[:-17], raw[-16:]
        expected = hmac.new(_CURSOR_KEY, payload, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(signature, expected):
            raise ValueError("bad cursor signature")
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict) or int(data.get("v", 0)) != _CURSOR_VERSION:
            raise ValueError("unsupported cursor version")
        if not str(data.get("c") or "") or not str(data.get("b") or ""):
            raise ValueError("incomplete cursor")
        return data
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise DisplayPageError(
            "cursor_invalid",
            "分页游标已失效，请重新从最新记录开始加载。",
            reset_required=True,
        ) from exc


def _find_item_index(items: list[DisplayItem], item_id: str) -> int | None:
    for index in range(len(items) - 1, -1, -1):
        if items[index].item_id == item_id:
            return index
    return None


def _build_items(
    records: list[dict[str, Any]],
    *,
    persona_name: str,
    search_text: str,
    show_chat: bool,
    show_system: bool,
    show_tools: bool,
    show_reasoning: bool,
    media_only: bool,
) -> list[DisplayItem]:
    return build_display_items(
        records,
        persona_name=persona_name,
        search_text=search_text,
        show_chat=show_chat,
        show_system=show_system,
        show_tools=show_tools,
        show_reasoning=show_reasoning,
        media_only=media_only,
    )


async def get_display_page(
    runtime: Any,
    *,
    conversation_id: str,
    before_id: str | None = None,
    cursor: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    search_text: str = "",
    show_chat: bool = True,
    show_system: bool = True,
    show_tools: bool = True,
    show_reasoning: bool = True,
    media_only: bool = False,
) -> dict[str, Any]:
    """返回从 ``before_id`` 之前开始的更早一页显示项。

    ``before_id`` 为空时返回最新一页；``cursor`` 优先于 ``before_id``。
    """
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        raise DisplayPageError("conversation_missing", "缺少会话 ID。")

    size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    persona_name = _runtime_persona_name(runtime)
    filters = _filters_signature(
        persona_name=persona_name,
        search_text=search_text,
        show_chat=show_chat,
        show_system=show_system,
        show_tools=show_tools,
        show_reasoning=show_reasoning,
        media_only=media_only,
    )
    filters_hash = _filters_hash(filters)

    if cursor:
        payload = _decode_cursor(cursor)
        if str(payload.get("c") or "") != conversation_id:
            raise DisplayPageError(
                "cursor_conversation_mismatch",
                "分页游标属于另一个会话，请重新加载。",
                reset_required=True,
            )
        if str(payload.get("f") or "") != filters_hash:
            raise DisplayPageError(
                "cursor_filter_mismatch",
                "筛选条件已变化，请重新从最新记录开始加载。",
                reset_required=True,
            )
        before_id = str(payload.get("b") or "")
    before_id = str(before_id).strip() if before_id else None

    cache = _cache_for_runtime(runtime)
    async with cache.lock:
        try:
            all_records, grouped = await _load_chat_page_records_and_groups(runtime)
        except Exception as exc:  # noqa: BLE001
            logger.exception("读取会话记录失败")
            raise DisplayPageError(
                "records_unavailable", f"读取会话记录失败：{exc}"
            ) from exc

        target = next(
            (item for item in grouped if item.get("key") == conversation_id),
            None,
        )
        records = list((target or {}).get("records") or [])
        total_records = len(records)
        key = _snapshot_key(conversation_id, filters_hash)
        snapshot = cache.get(key)

        # 不带 before_id 时是在要“最新一页”，缓存必须覆盖当前全部记录；
        # 带 before_id 时新消息只追加在尾部，不影响更早页，可继续复用。
        if snapshot is not None and before_id is None:
            if not _snapshot_matches(snapshot, records) or len(records) != snapshot.prefix_len:
                snapshot = None
        elif snapshot is not None and not _snapshot_matches(snapshot, records):
            snapshot = None

        if snapshot is None:
            items = await asyncio.to_thread(
                _build_items,
                records,
                persona_name=persona_name,
                search_text=search_text,
                show_chat=show_chat,
                show_system=show_system,
                show_tools=show_tools,
                show_reasoning=show_reasoning,
                media_only=media_only,
            )
            snapshot = _Snapshot(
                items=items,
                prefix_hash=_prefix_hash(records, len(records)),
                prefix_len=len(records),
                total_records=total_records,
                filters_hash=filters_hash,
                created_at=time.monotonic(),
                last_used_at=time.monotonic(),
                generation=secrets.token_hex(8),
            )
            cache.put(key, snapshot)

        end = len(snapshot.items) if before_id is None else _find_item_index(
            snapshot.items, before_id
        )
        if end is None:
            # 快照可能来自旧前缀、尾部又追加了新记录，或锚点已被压缩/删除；
            # 前缀变化或尾部增长时都重建一次再找，只有真正的缺锚才要求重置。
            matches_prefix = _snapshot_matches(snapshot, records)
            if matches_prefix and len(records) <= snapshot.prefix_len:
                raise DisplayPageError(
                    "cursor_anchor_missing",
                    "要加载的位置已经不存在，请重新从最新记录开始。",
                    reset_required=True,
                )
            items = await asyncio.to_thread(
                _build_items,
                records,
                persona_name=persona_name,
                search_text=search_text,
                show_chat=show_chat,
                show_system=show_system,
                show_tools=show_tools,
                show_reasoning=show_reasoning,
                media_only=media_only,
            )
            snapshot = _Snapshot(
                items=items,
                prefix_hash=_prefix_hash(records, len(records)),
                prefix_len=len(records),
                total_records=total_records,
                filters_hash=filters_hash,
                created_at=time.monotonic(),
                last_used_at=time.monotonic(),
                generation=secrets.token_hex(8),
            )
            cache.put(key, snapshot)
            end = _find_item_index(snapshot.items, before_id)
            if end is None:
                raise DisplayPageError(
                    "cursor_anchor_missing",
                    "要加载的位置已经不存在，请重新从最新记录开始。",
                    reset_required=True,
                )

        start = max(0, end - size)
        page_items = snapshot.items[start:end]
        has_more = start > 0
        next_cursor = None
        if has_more and page_items:
            next_cursor = _encode_cursor(
                conversation_id=conversation_id,
                filters_hash=filters_hash,
                before_id=page_items[0].item_id,
                generation=snapshot.generation,
            )

        return {
            "items": display_items_json(page_items),
            "page_size": len(page_items),
            "has_more": has_more,
            "next_cursor": next_cursor,
            "total": total_records,
            "filtered_total": len(snapshot.items),
            "snapshot_records": snapshot.prefix_len,
            "stale_tail": len(records) > snapshot.prefix_len,
            "reset_required": False,
        }


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "DisplayPageError",
    "get_display_page",
    "get_indexed_display_page",
    "warm_display_index",
]


# ============================================================
# 持久化 Display 索引（第二期）
# ============================================================

_INDEX_FILENAME = "chat_display_index.sqlite3"
_INDEX_SCHEMA_VERSION = 3

_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_display_items (
    persona_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    parent_item_id TEXT,
    is_top INTEGER NOT NULL,
    page_order INTEGER NOT NULL,
    child_order INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL,
    timestamp TEXT,
    speaker_label TEXT,
    speaker_id TEXT,
    role_label TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'normal',
    collapsed_by_default INTEGER NOT NULL DEFAULT 0,
    related_tool_call_id TEXT,
    related_message_id TEXT,
    search_text_folded TEXT NOT NULL DEFAULT '',
    media_flag INTEGER NOT NULL DEFAULT 0,
    attachments_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (persona_id, conversation_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_chat_display_page_order
    ON chat_display_items(persona_id, conversation_id, is_top, page_order);
CREATE INDEX IF NOT EXISTS idx_chat_display_parent_order
    ON chat_display_items(persona_id, conversation_id, parent_item_id, child_order);
CREATE TABLE IF NOT EXISTS chat_display_index_meta (
    persona_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    raw_count INTEGER NOT NULL DEFAULT 0,
    top_count INTEGER NOT NULL DEFAULT 0,
    history_length INTEGER NOT NULL DEFAULT -1,
    indexed_at REAL NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 3,
    PRIMARY KEY (persona_id, conversation_id)
);
"""

_INDEX_LOCKS: weakref.WeakKeyDictionary[Any, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _index_lock(runtime: Any) -> asyncio.Lock:
    lock = _INDEX_LOCKS.get(runtime)
    if lock is None:
        lock = asyncio.Lock()
        _INDEX_LOCKS[runtime] = lock
    return lock


def _index_db_path(runtime: Any) -> Path | None:
    paths = getattr(runtime, "paths", None)
    data_dir = getattr(paths, "DATA_DIR", None) or getattr(paths, "data_dir", None)
    if not data_dir:
        return None
    return Path(data_dir) / _INDEX_FILENAME


def _open_index(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(_INDEX_SCHEMA)
    meta_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(chat_display_index_meta)")
    }
    if "history_length" not in meta_columns:
        conn.execute(
            "ALTER TABLE chat_display_index_meta "
            "ADD COLUMN history_length INTEGER NOT NULL DEFAULT -1"
        )
        conn.commit()
    item_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(chat_display_items)")
    }
    if "attachments_json" not in item_columns:
        conn.execute(
            "ALTER TABLE chat_display_items "
            "ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'"
        )
        # 旧行没有附件字段，清空后由后台预热重建。
        conn.execute("DELETE FROM chat_display_items")
        conn.execute("DELETE FROM chat_display_index_meta")
        conn.commit()
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass
    return conn


def _index_item_flags(
    item: DisplayItem,
    cache: dict[int, tuple[str, bool]],
) -> tuple[str, bool]:
    """一次序列化算出搜索文本与媒体标记，避免父项/子项重复 JSON dumps。"""
    cached = cache.get(id(item))
    if cached is not None:
        return cached
    raw_json = json.dumps(item.raw, ensure_ascii=False, default=str)
    parts = [
        item.kind,
        item.speaker_label or "",
        item.speaker_id or "",
        item.role_label,
        item.text,
        item.summary,
        item.related_tool_call_id or "",
        item.related_message_id or "",
        raw_json,
    ]
    media = (
        _text_has_media_or_file(item.text)
        or _text_has_media_or_file(item.summary)
        or _text_has_media_or_file(raw_json)
    )
    for child in item.tool_results:
        child_search, child_media = _index_item_flags(child, cache)
        parts.append(child_search)
        media = media or child_media
    result = ("\n".join(parts).casefold(), media)
    cache[id(item)] = result
    return result


def _item_index_row(
    item: DisplayItem,
    *,
    persona_id: str,
    conversation_id: str,
    parent_item_id: str | None,
    page_order: int,
    child_order: int,
    flags: tuple[str, bool] | None = None,
) -> tuple[Any, ...]:
    search_text, media = flags or _index_item_flags(item, {})
    return (
        persona_id,
        conversation_id,
        item.item_id,
        parent_item_id,
        0 if parent_item_id else 1,
        page_order,
        child_order,
        item.kind,
        item.timestamp,
        item.speaker_label,
        item.speaker_id,
        item.role_label,
        item.text,
        item.summary,
        item.severity,
        1 if item.collapsed_by_default else 0,
        item.related_tool_call_id,
        item.related_message_id,
        search_text,
        1 if media else 0,
        json.dumps(extract_media_attachments(item), ensure_ascii=False),
    )


_INDEX_INSERT_SQL = """
INSERT INTO chat_display_items (
    persona_id, conversation_id, item_id, parent_item_id, is_top,
    page_order, child_order, kind, timestamp, speaker_label, speaker_id,
    role_label, text, summary, severity, collapsed_by_default,
    related_tool_call_id, related_message_id, search_text_folded, media_flag,
    attachments_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _build_display_index_sync(
    db_path: Path,
    *,
    persona_id: str,
    conversation_id: str,
    records: list[dict[str, Any]],
    persona_name: str,
    history_length: int = -1,
) -> int:
    """从完整显示项重建一个会话的索引，返回顶层显示项数量。"""
    items = build_display_items(records, persona_name=persona_name)
    rows: list[tuple[Any, ...]] = []
    flags_cache: dict[int, tuple[str, bool]] = {}
    for page_order, item in enumerate(items):
        rows.append(
            _item_index_row(
                item,
                persona_id=persona_id,
                conversation_id=conversation_id,
                parent_item_id=None,
                page_order=page_order,
                child_order=0,
                flags=_index_item_flags(item, flags_cache),
            )
        )
        for child_order, child in enumerate(item.tool_results):
            rows.append(
                _item_index_row(
                    child,
                    persona_id=persona_id,
                    conversation_id=conversation_id,
                    parent_item_id=item.item_id,
                    page_order=page_order,
                    child_order=child_order,
                    flags=_index_item_flags(child, flags_cache),
                )
            )
    conn = _open_index(db_path)
    try:
        with conn:
            conn.execute(
                "DELETE FROM chat_display_items WHERE persona_id = ? AND conversation_id = ?",
                (persona_id, conversation_id),
            )
            conn.execute(
                "DELETE FROM chat_display_index_meta WHERE persona_id = ? AND conversation_id = ?",
                (persona_id, conversation_id),
            )
            if rows:
                conn.executemany(_INDEX_INSERT_SQL, rows)
            conn.execute(
                """
                INSERT INTO chat_display_index_meta (
                    persona_id, conversation_id, raw_count, top_count,
                    history_length, indexed_at, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    persona_id,
                    conversation_id,
                    len(records),
                    len(items),
                    int(history_length),
                    time.time(),
                    _INDEX_SCHEMA_VERSION,
                ),
            )
    finally:
        conn.close()
    return len(items)


def _allowed_kinds(
    *,
    show_chat: bool,
    show_system: bool,
    show_tools: bool,
    show_reasoning: bool,
) -> list[str]:
    kinds: list[str] = []
    if show_chat:
        kinds.extend(("inbound_message", "outbound_message"))
    if show_system:
        kinds.extend(("system_event", "runtime_receipt", "assistant_note"))
    if show_tools:
        kinds.extend(("tool_call", "tool_result"))
    if show_reasoning:
        kinds.append("reasoning")
    return kinds


def _row_to_display_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["item_id"],
        "kind": row["kind"],
        "timestamp": row["timestamp"],
        "speaker_label": row["speaker_label"],
        "speaker_id": row["speaker_id"],
        "role_label": row["role_label"],
        "text": row["text"],
        "summary": row["summary"],
        "severity": row["severity"],
        "collapsed_by_default": bool(row["collapsed_by_default"]),
        "related_tool_call_id": row["related_tool_call_id"],
        "related_message_id": row["related_message_id"],
        "attachments": _parse_attachments(row),
        "tool_results": [],
    }


def _parse_attachments(row: sqlite3.Row) -> list[dict[str, Any]]:
    try:
        value = json.loads(str(row["attachments_json"] or "[]"))
    except (KeyError, TypeError, ValueError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _query_display_index_sync(
    db_path: Path,
    *,
    persona_id: str,
    conversation_id: str,
    before_id: str,
    page_size: int,
    show_chat: bool,
    show_system: bool,
    show_tools: bool,
    show_reasoning: bool,
    media_only: bool,
    search_text: str,
    filters_hash: str,
) -> dict[str, Any] | None:
    """按 item id 取上一页；索引不存在或锚点缺失时返回 None。"""
    if not db_path.is_file():
        return None
    try:
        conn = _open_index(db_path)
    except sqlite3.DatabaseError:
        return None
    try:
        anchor = conn.execute(
            """
            SELECT page_order FROM chat_display_items
            WHERE persona_id = ? AND conversation_id = ?
              AND item_id = ? AND is_top = 1
            """,
            (persona_id, conversation_id, before_id),
        ).fetchone()
        if anchor is None:
            return None
        anchor_order = int(anchor["page_order"])
        meta = conn.execute(
            """
            SELECT raw_count, top_count FROM chat_display_index_meta
            WHERE persona_id = ? AND conversation_id = ?
            """,
            (persona_id, conversation_id),
        ).fetchone()
        raw_count = int(meta["raw_count"]) if meta is not None else 0

        kinds = _allowed_kinds(
            show_chat=show_chat,
            show_system=show_system,
            show_tools=show_tools,
            show_reasoning=show_reasoning,
        )
        if not kinds:
            return {
                "items": [],
                "page_size": 0,
                "has_more": False,
                "next_cursor": None,
                "total": raw_count,
                "filtered_total": 0,
                "snapshot_records": raw_count,
                "stale_tail": False,
                "reset_required": False,
            }

        params: list[Any] = [persona_id, conversation_id]
        placeholders = ",".join("?" for _ in kinds)
        filters = [
            "persona_id = ?",
            "conversation_id = ?",
            "is_top = 1",
            f"kind IN ({placeholders})",
        ]
        params.extend(kinds)
        if media_only:
            filters.append("media_flag = 1")
        query = str(search_text or "").strip().casefold()
        if query:
            filters.append("instr(search_text_folded, ?) > 0")
            params.append(query)

        count_sql = (
            "SELECT COUNT(*) FROM chat_display_items WHERE "
            + " AND ".join(filters)
        )
        filtered_total = int(conn.execute(count_sql, params).fetchone()[0])

        page_filters = [*filters, "page_order < ?"]
        page_params = [*params, anchor_order]
        rows = conn.execute(
            "SELECT * FROM chat_display_items WHERE "
            + " AND ".join(page_filters)
            + " ORDER BY page_order DESC LIMIT ?",
            [*page_params, page_size + 1],
        ).fetchall()
        has_more = len(rows) > page_size
        page_rows = rows[:page_size]
        page_rows.reverse()
        if not page_rows:
            return {
                "items": [],
                "page_size": 0,
                "has_more": False,
                "next_cursor": None,
                "total": raw_count,
                "filtered_total": filtered_total,
                "snapshot_records": raw_count,
                "stale_tail": False,
                "reset_required": False,
            }

        parent_ids = [str(row["item_id"]) for row in page_rows]
        child_rows: list[sqlite3.Row] = []
        if parent_ids:
            child_placeholders = ",".join("?" for _ in parent_ids)
            child_rows = conn.execute(
                "SELECT * FROM chat_display_items WHERE "
                "persona_id = ? AND conversation_id = ? "
                f"AND parent_item_id IN ({child_placeholders}) "
                "ORDER BY page_order ASC, child_order ASC",
                [persona_id, conversation_id, *parent_ids],
            ).fetchall()
        children: dict[str, list[dict[str, Any]]] = {}
        for row in child_rows:
            children.setdefault(str(row["parent_item_id"]), []).append(
                _row_to_display_json(row)
            )

        items: list[dict[str, Any]] = []
        for row in page_rows:
            item = _row_to_display_json(row)
            item["tool_results"] = children.get(str(row["item_id"]), [])
            items.append(item)

        next_cursor = None
        if has_more:
            next_cursor = _encode_cursor(
                conversation_id=conversation_id,
                filters_hash=filters_hash,
                before_id=str(page_rows[0]["item_id"]),
                generation="index",
            )
        return {
            "items": items,
            "page_size": len(items),
            "has_more": has_more,
            "next_cursor": next_cursor,
            "total": raw_count,
            "filtered_total": filtered_total,
            "snapshot_records": raw_count,
            "stale_tail": False,
            "reset_required": False,
        }
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()


def _index_meta_sync(
    db_path: Path,
    *,
    persona_id: str,
    conversation_id: str,
) -> int | None:
    """返回建索引时的 history 长度；没有索引或无法读取时返回 None。"""
    if not db_path.is_file():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        row = conn.execute(
            """
            SELECT history_length FROM chat_display_index_meta
            WHERE persona_id = ? AND conversation_id = ?
            """,
            (persona_id, conversation_id),
        ).fetchone()
        if row is None:
            return None
        return int(row[0])
    except sqlite3.DatabaseError:
        return None
    finally:
        if conn is not None:
            conn.close()


async def _runtime_history_length(runtime: Any) -> int | None:
    history = getattr(runtime, "history", None)
    length = getattr(history, "length", None)
    if not callable(length):
        return None
    try:
        return int(await length())
    except Exception:  # noqa: BLE001
        return None


async def warm_display_index(
    runtime: Any,
    conversation_id: str,
    *,
    force: bool = False,
) -> None:
    """后台预热/重建一个会话的索引；失败不影响实时 /display。"""
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        return
    db_path = _index_db_path(runtime)
    if db_path is None:
        return
    persona_name = _runtime_persona_name(runtime)
    persona_id = persona_name or "default"

    async def _still_fresh() -> bool:
        if force:
            return False
        stored = await asyncio.to_thread(
            _index_meta_sync,
            db_path,
            persona_id=persona_id,
            conversation_id=conversation_id,
        )
        if stored is None or stored < 0:
            return False
        current = await _runtime_history_length(runtime)
        # 新消息会让 current > stored，但旧页仍然有效；只有历史缩短
        # （压缩/删除）才必须重建。
        return current is None or current >= stored

    try:
        if await _still_fresh():
            return
        lock = _index_lock(runtime)
        if lock.locked():
            return
        async with lock:
            if await _still_fresh():
                return
            all_records, grouped = await _load_chat_page_records_and_groups(runtime)
            target = next(
                (item for item in grouped if item.get("key") == conversation_id),
                None,
            )
            if target is None:
                return
            history_length = await _runtime_history_length(runtime)
            await asyncio.to_thread(
                _build_display_index_sync,
                db_path,
                persona_id=persona_id,
                conversation_id=conversation_id,
                records=list(target.get("records") or []),
                persona_name=persona_name,
                history_length=-1 if history_length is None else history_length,
            )
    except Exception:  # noqa: BLE001
        logger.debug("后台预热对话显示索引失败: %s", conversation_id, exc_info=True)


async def get_indexed_display_page(
    runtime: Any,
    *,
    conversation_id: str,
    before_id: str | None = None,
    cursor: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    search_text: str = "",
    show_chat: bool = True,
    show_system: bool = True,
    show_tools: bool = True,
    show_reasoning: bool = True,
    media_only: bool = False,
) -> dict[str, Any] | None:
    """优先从持久化索引取更早的一页；索引不可用或没有 before_id 时返回 None。"""
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        raise DisplayPageError("conversation_missing", "缺少会话 ID。")
    size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    persona_name = _runtime_persona_name(runtime)
    persona_id = persona_name or "default"
    filters = _filters_signature(
        persona_name=persona_name,
        search_text=search_text,
        show_chat=show_chat,
        show_system=show_system,
        show_tools=show_tools,
        show_reasoning=show_reasoning,
        media_only=media_only,
    )
    filters_hash = _filters_hash(filters)

    if cursor:
        payload = _decode_cursor(cursor)
        if str(payload.get("c") or "") != conversation_id:
            raise DisplayPageError(
                "cursor_conversation_mismatch",
                "分页游标属于另一个会话，请重新加载。",
                reset_required=True,
            )
        if str(payload.get("f") or "") != filters_hash:
            raise DisplayPageError(
                "cursor_filter_mismatch",
                "筛选条件已变化，请重新从最新记录开始加载。",
                reset_required=True,
            )
        before_id = str(payload.get("b") or "")
    before_id = str(before_id).strip() if before_id else ""
    # 最新一页继续走实时 /display，索引只负责“更早”。
    if not before_id:
        return None

    db_path = _index_db_path(runtime)
    if db_path is None:
        return None

    lock = _index_lock(runtime)
    # 后台正在建索引时先用快照分页顶一下，避免第一下点击卡两三秒。
    if lock.locked():
        return None
    async with lock:
        try:
            result = await asyncio.to_thread(
                _query_display_index_sync,
                db_path,
                persona_id=persona_id,
                conversation_id=conversation_id,
                before_id=before_id,
                page_size=size,
                show_chat=show_chat,
                show_system=show_system,
                show_tools=show_tools,
                show_reasoning=show_reasoning,
                media_only=media_only,
                search_text=search_text,
                filters_hash=filters_hash,
            )
        except Exception:  # noqa: BLE001
            logger.warning("读取对话显示索引失败，回退快照分页", exc_info=True)
            result = None
        if result is not None:
            return result

        # 索引缺失、尾部落后或锚点已失效：后台重建，本次先用快照分页，
        # 不让用户为 2-3 秒的建索引等待买单。
        meta_length = await asyncio.to_thread(
            _index_meta_sync,
            db_path,
            persona_id=persona_id,
            conversation_id=conversation_id,
        )
        try:
            asyncio.get_running_loop().create_task(
                warm_display_index(
                    runtime,
                    conversation_id,
                    force=meta_length is not None,
                )
            )
        except RuntimeError:
            pass
        return None
