"""对话页增量读取缓存：与全量读取逐条等价，并正确响应删除/窗口滑动。"""

from __future__ import annotations

import pytest

from backend.chatlog import records as chat_records
from backend.chatlog.records import (
    _converted_event_records,
    _load_archive_records_cached,
    _load_event_records_cached,
    clear_chat_page_cache,
)


def test_event_window_matches_store_limit_clamp() -> None:
    from memory.event_store import _clamp_limit

    assert (
        _clamp_limit(chat_records._EVENT_PAGE_EVENT_LIMIT)
        == chat_records._EVENT_PAGE_EVENT_LIMIT
    )


class _MemoryArchive:
    """只实现增量接口的内存归档，用来验证缓存一致性。"""

    def __init__(self) -> None:
        self.rows: list[tuple[int, dict, bool]] = []
        self.next_rowid = 1
        self.records_calls = 0

    def add(self, records: list[dict], *, real: bool = True) -> list[int]:
        rowids = []
        for record in records:
            rowid = self.next_rowid
            self.next_rowid += 1
            self.rows.append((rowid, dict(record), real))
            rowids.append(rowid)
        return rowids

    def delete(self, rowid: int) -> None:
        self.rows = [row for row in self.rows if row[0] != rowid]

    async def records(self) -> list[dict]:
        self.records_calls += 1
        return [dict(record) for _, record, real in self.rows if real]

    async def records_since(
        self, rowid: int
    ) -> tuple[list[dict], int, int, int]:
        new_rows = [row for row in self.rows if row[0] > rowid]
        return (
            [dict(record) for _, record, real in new_rows if real],
            len(self.rows),
            max((row[0] for row in self.rows), default=0),
            len(new_rows),
        )


def _real_archive_record(index: int) -> dict:
    return {
        "role": "user",
        "content": f"archive {index}",
        "conversation_id": "group:1",
        "direction": "inbound",
        "message_id": f"m{index}",
    }


@pytest.mark.asyncio
async def test_archive_cache_tracks_appends_without_full_reads() -> None:
    clear_chat_page_cache()
    archive = _MemoryArchive()
    archive.add([_real_archive_record(index) for index in range(5)])

    first = await _load_archive_records_cached(archive)
    assert [record["content"] for record in first] == [
        f"archive {index}" for index in range(5)
    ]

    archive.add([_real_archive_record(index) for index in range(5, 8)])
    second = await _load_archive_records_cached(archive)
    assert [record["content"] for record in second] == [
        f"archive {index}" for index in range(8)
    ]
    assert archive.records_calls == 0


@pytest.mark.asyncio
async def test_archive_cache_reloads_after_delete() -> None:
    clear_chat_page_cache()
    archive = _MemoryArchive()
    rowids = archive.add([_real_archive_record(index) for index in range(5)])
    await _load_archive_records_cached(archive)

    archive.delete(rowids[2])
    after_delete = await _load_archive_records_cached(archive)

    assert [record["content"] for record in after_delete] == [
        "archive 0",
        "archive 1",
        "archive 3",
        "archive 4",
    ]
    assert archive.records_calls == 0


@pytest.mark.asyncio
async def test_archive_cache_ignores_non_chat_rows_but_counts_them() -> None:
    clear_chat_page_cache()
    archive = _MemoryArchive()
    archive.add([_real_archive_record(0)])
    await _load_archive_records_cached(archive)

    archive.add([{"role": "system", "content": "runtime note"}], real=False)
    archive.add([_real_archive_record(1)])
    records = await _load_archive_records_cached(archive)

    assert [record["content"] for record in records] == ["archive 0", "archive 1"]
    assert archive.records_calls == 0


class _MemoryEventStore:
    """支持 event_id_bounds/iter_events 的内存事件库。"""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.next_event_id = 1
        self.iter_calls = 0

    def add(self, event_type: str, count: int = 1) -> list[int]:
        event_ids = []
        for _ in range(count):
            event_id = self.next_event_id
            self.next_event_id += 1
            event_ids.append(event_id)
            event = {
                "event_id": event_id,
                "event_type": event_type,
                "conversation_id": "private:1",
                "payload": {
                    "content": f"event {event_id}",
                    "msg_id": f"m{event_id}",
                    "user_id": "1",
                    "sender_id": "1",
                    "sender_name": "A",
                    "timestamp": f"2026-01-01T10:{event_id % 60:02d}:00Z",
                },
                "timestamp_unix": 1767252000 + event_id,
            }
            self.events.append(event)
        return event_ids

    async def event_id_bounds(self, limit: int) -> tuple[int, int | None]:
        if not self.events:
            return 0, None
        ordered = sorted(
            self.events, key=lambda event: event["event_id"], reverse=True
        )
        return (
            int(ordered[0]["event_id"]),
            int(ordered[limit - 1]["event_id"]) if len(ordered) >= limit else None,
        )

    async def iter_events(
        self,
        *,
        limit: int = 100,
        after_event_id: int | None = None,
        before_event_id: int | None = None,
        order: str = "asc",
    ) -> list[dict]:
        self.iter_calls += 1
        items = [
            event
            for event in self.events
            if after_event_id is None or event["event_id"] > after_event_id
        ]
        items.sort(
            key=lambda event: event["event_id"], reverse=order == "desc"
        )
        return [dict(event) for event in items[:limit]]


async def _reference_event_records(store: _MemoryEventStore) -> list[dict]:
    page = await store.iter_events(
        limit=chat_records._EVENT_PAGE_EVENT_LIMIT,
        order="desc",
    )
    return _converted_event_records(page)


def _event_ids(records: list[dict]) -> list[int | None]:
    return [record.get("event_id") for record in records]


@pytest.mark.asyncio
async def test_event_cache_matches_full_read_across_appends() -> None:
    clear_chat_page_cache()
    store = _MemoryEventStore()
    store.add("qq_message_received", 100)

    first = await _load_event_records_cached(store)
    assert _event_ids(first) == _event_ids(await _reference_event_records(store))

    store.add("history_record_appended", 1500)
    store.add("qq_message_received", 20)
    second = await _load_event_records_cached(store)
    assert _event_ids(second) == _event_ids(
        await _reference_event_records(store)
    )


@pytest.mark.asyncio
async def test_event_cache_trims_window_when_invisible_events_flood() -> None:
    clear_chat_page_cache()
    store = _MemoryEventStore()
    store.add("qq_message_received", 100)
    await _load_event_records_cached(store)

    store.add("history_record_appended", 2000)
    store.add("qq_message_received", 50)

    cached = await _load_event_records_cached(store)
    reference = await _reference_event_records(store)

    assert _event_ids(cached) == _event_ids(reference)
    assert len(cached) == 50


@pytest.mark.asyncio
async def test_event_cache_reloads_when_event_ids_shrink() -> None:
    clear_chat_page_cache()
    store = _MemoryEventStore()
    store.add("qq_message_received", 50)
    await _load_event_records_cached(store)

    store.events = [
        event for event in store.events if event["event_id"] <= 10
    ]
    cached = await _load_event_records_cached(store)

    assert _event_ids(cached) == _event_ids(await _reference_event_records(store))


@pytest.mark.asyncio
async def test_clear_chat_page_cache_clears_incremental_caches() -> None:
    clear_chat_page_cache()
    archive = _MemoryArchive()
    archive.add([_real_archive_record(0)])
    store = _MemoryEventStore()
    store.add("qq_message_received", 1)
    await _load_archive_records_cached(archive)
    await _load_event_records_cached(store)

    assert len(chat_records._ARCHIVE_READ_CACHES) == 1
    assert len(chat_records._EVENT_READ_CACHES) == 1

    clear_chat_page_cache()

    assert len(chat_records._ARCHIVE_READ_CACHES) == 0
    assert len(chat_records._EVENT_READ_CACHES) == 0
