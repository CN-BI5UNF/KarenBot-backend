"""Karen 各 Store 的 schema 门控与连接配置。"""

from __future__ import annotations

import sqlite3

import pytest

from memory.karen_db import KarenDB
from memory.karen_stores import (
    KarenArchiveStore,
    KarenEventStore,
    KarenHistoryStore,
    KarenImportantStore,
    KarenPersonaDB,
    KarenRollingSummaryStore,
    KarenUsageStatsStore,
)


@pytest.mark.asyncio
async def test_stores_do_not_rerun_schema_init(tmp_path, monkeypatch) -> None:
    path = tmp_path / "karen.db"
    db = KarenDB(path)
    db.ensure_ready()
    db.close()

    def fail_if_called(*args, **kwargs):
        raise AssertionError("schema should not be re-initialized")

    monkeypatch.setattr(KarenDB, "ensure_schema", fail_if_called)

    history = KarenHistoryStore(path, "persona")
    assert await history.length() == 0

    archive = KarenArchiveStore(path, "persona")
    assert await archive.records() == []

    important = KarenImportantStore(path, "persona")
    assert await important.read(default=[]) == []

    persona = KarenPersonaDB(path, "persona")
    assert await persona.get_state(default=None) is None

    rolling = KarenRollingSummaryStore(path, "persona")
    await rolling.load()

    usage = KarenUsageStatsStore(path, "persona")
    await usage.load()

    event_store = KarenEventStore(path, "persona")
    stats = await event_store.stats()
    assert stats["closed"] is False


def test_event_store_connection_uses_shared_pragmas(tmp_path) -> None:
    path = tmp_path / "karen.db"
    store = KarenEventStore(path, "persona")
    store._ensure_schema_sync()

    conn = store._connect()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert int(conn.execute("PRAGMA synchronous").fetchone()[0]) == 1
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        assert int(conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 30000
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'event_log'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_event_store_writes_after_schema_is_ready(tmp_path) -> None:
    path = tmp_path / "karen.db"
    store = KarenEventStore(path, "persona")

    event_id = await store.append_event(
        event_type="qq_message_received",
        payload={"content": "hello"},
    )
    assert event_id == 1

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM event_log WHERE persona_id = ?",
            ("persona",),
        ).fetchone()
        assert int(row[0]) == 1
    finally:
        conn.close()
