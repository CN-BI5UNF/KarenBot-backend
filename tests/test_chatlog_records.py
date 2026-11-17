"""对话页合并、发送去重与缓存统计的回归测试。"""

from __future__ import annotations

import asyncio
import time
import weakref
from datetime import datetime

import pytest

from backend.chatlog import records as chat_records
from backend.chatlog.display_items import _parse_timestamp_value
from backend.chatlog.records import (
    _event_store_event_to_record,
    _load_chat_page_records,
    _merge_and_tag_chat_page_records,
    _send_runtime_duplicate_ids,
    chat_page_cache_info,
    clear_chat_page_cache,
)


class _WeakOwner:
    """可被 weakref 引用的占位对象。"""


def _reference_parse_timestamp(value):
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@pytest.mark.parametrize(
    "value",
    [
        0,
        1767252000.5,
        "1767252000",
        "2026-01-01T10:00:00Z",
        "2026-01-01T10:00:00+08:00",
        "2026-01-01T10:00:00.123Z",
        "2026-01-01 10:00:00",
        "2026/01/01 10:00:00",
        "2026-01-01 10:00:00T",
        "not-a-time",
        "",
        "   ",
        None,
    ],
)
def test_parse_timestamp_matches_reference(value) -> None:
    assert _parse_timestamp_value(value) == _reference_parse_timestamp(value)


def test_send_duplicate_ids_ignores_unrelated_records() -> None:
    assert _send_runtime_duplicate_ids(
        {"role": "user", "content": "今天天气不错，没有发送相关的字段"}
    ) == set()


def test_send_duplicate_ids_reads_payload_and_record_fields() -> None:
    record = {
        "role": "tool",
        "content": "ok",
        "send_attempt_id": "a1",
        "metadata": {"event_payload": {"send_id": "p1"}},
    }
    assert _send_runtime_duplicate_ids(record) == {"p1", "a1"}


def test_send_duplicate_ids_reads_tags_and_text() -> None:
    receipt = {
        "role": "system",
        "content": '<send_receipt>{"send_id":"s1","msg_id":"m1"}</send_receipt>',
    }
    assert _send_runtime_duplicate_ids(receipt) == {"s1"}

    status = {
        "role": "system",
        "content": "send_attempt_id=abc, status=queued",
    }
    assert _send_runtime_duplicate_ids(status) == {"abc"}


def test_send_duplicate_ids_reads_json_content() -> None:
    record = {
        "role": "system",
        "content": '{"attempt_id": "j1", "ok": true}',
    }
    assert _send_runtime_duplicate_ids(record) == {"j1"}


def test_send_duplicate_ids_preserves_falsy_but_non_null_ids() -> None:
    record = {"role": "tool", "content": "ok", "send_id": 0}
    assert _send_runtime_duplicate_ids(record) == {"0"}


def test_send_duplicate_ids_decodes_escaped_json_keys() -> None:
    record = {
        "role": "system",
        "content": '{"send\\u005fid": "escaped"}',
    }
    assert _send_runtime_duplicate_ids(record) == {"escaped"}


def test_merge_drops_archive_duplicate_of_event_record() -> None:
    event = _event_store_event_to_record(
        {
            "event_id": 1,
            "event_type": "qq_message_received",
            "conversation_id": "private:1",
            "payload": {
                "content": "hello",
                "msg_id": "m1",
                "user_id": "1",
                "sender_id": "1",
                "sender_name": "A",
                "timestamp": "2026-01-01T10:00:00Z",
            },
        }
    )
    assert event is not None

    archive = [
        {
            "role": "user",
            "content": "hello",
            "conversation_id": "private:1",
            "direction": "inbound",
            "message_id": "m1",
            "timestamp": "2026-01-01T10:00:00Z",
        },
        {
            "role": "user",
            "content": "archive only",
            "conversation_id": "private:1",
            "direction": "inbound",
            "message_id": "m2",
            "timestamp": "2026-01-01T10:01:00Z",
        },
    ]
    history = [{"role": "system", "content": "[系统提示]"}]

    merged = _merge_and_tag_chat_page_records(
        archive_records=archive,
        event_records=[event],
        timeline_records=[],
        history_records=history,
    )

    contents = [record["content"] for record in merged]
    assert contents == ["archive only", "hello", "[系统提示]"]
    assert [record["_sort_layer"] for record in merged] == [
        "archive",
        "event_store",
        "history",
    ]
    assert [record["_record_order"] for record in merged] == [0, 1, 2]


def test_chat_page_cache_info_uses_precomputed_bytes() -> None:
    clear_chat_page_cache()
    owner = _WeakOwner()
    chat_records._chat_page_cache[(1, 2, 3)] = (
        weakref.ref(owner),
        4,
        5.0,
        [{"content": "x"}],
        321,
    )
    try:
        info = chat_page_cache_info()
        assert info == {
            "entries": 1,
            "records": 1,
            "approx_bytes": 321,
            "max_entries": 8,
            "ttl_seconds": 10,
        }
    finally:
        clear_chat_page_cache()


class _FakeHistory:
    def __init__(self) -> None:
        self.length_calls = 0
        self.records_calls = 0

    async def length(self) -> int:
        self.length_calls += 1
        return 0

    async def records(self) -> list[dict]:
        self.records_calls += 1
        await asyncio.sleep(0.02)
        return []


class _FakeArchive:
    def __init__(self) -> None:
        self.records_calls = 0

    async def records(self) -> list[dict]:
        self.records_calls += 1
        await asyncio.sleep(0.02)
        return []


class _FakeEventStore:
    async def iter_events(self, **kwargs) -> list[dict]:
        return []


class _FakeRuntime:
    def __init__(self) -> None:
        self.history = _FakeHistory()
        self.archive = _FakeArchive()
        self.event_store = _FakeEventStore()


@pytest.mark.asyncio
async def test_concurrent_chat_page_loads_share_one_computation() -> None:
    clear_chat_page_cache()
    runtime = _FakeRuntime()

    first, second = await asyncio.gather(
        _load_chat_page_records(runtime),
        _load_chat_page_records(runtime),
    )

    assert first == []
    assert second == []
    assert runtime.history.records_calls == 1
    assert runtime.archive.records_calls == 1


class _FakeHistoryWithLength(_FakeHistory):
    async def length(self) -> int:
        return 1


class _FakeArchiveWithRecord:
    async def records(self) -> list[dict]:
        return [
            {
                "role": "user",
                "content": "hello",
                "conversation_id": "private:1",
                "direction": "inbound",
                "message_id": "m1",
                "timestamp": "2026-01-01T10:00:00Z",
            }
        ]


class _FakeRuntimeWithRecord:
    def __init__(self) -> None:
        self.history = _FakeHistoryWithLength()
        self.archive = _FakeArchiveWithRecord()
        self.event_store = _FakeEventStore()


@pytest.mark.asyncio
async def test_chat_page_cache_rejects_recycled_runtime_id() -> None:
    clear_chat_page_cache()
    runtime = _FakeRuntimeWithRecord()
    stale_owner = _WeakOwner()
    cache_key = (id(runtime), id(runtime.history), id(runtime.event_store))
    chat_records._chat_page_cache[cache_key] = (
        weakref.ref(stale_owner),
        1,
        time.monotonic(),
        [{"content": "stale runtime records"}],
        1,
    )
    try:
        loaded = await _load_chat_page_records(runtime)

        assert loaded and loaded[0]["content"] == "hello"
    finally:
        clear_chat_page_cache()


@pytest.mark.asyncio
async def test_chat_page_cache_info_reads_written_entries() -> None:
    clear_chat_page_cache()
    runtime = _FakeRuntimeWithRecord()
    try:
        loaded = await _load_chat_page_records(runtime)
        info = chat_page_cache_info()

        assert len(loaded) == 1
        assert info["entries"] == 1
        assert info["records"] == len(loaded)
        assert info["approx_bytes"] > 0
    finally:
        clear_chat_page_cache()
