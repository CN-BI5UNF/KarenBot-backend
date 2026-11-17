"""archive_messages.record_hash 的 v2 迁移与归档去重。"""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from memory import karen_db as karen_db_module
from memory import karen_stores as karen_stores_module
from memory.archive_sqlite import SqliteArchiveStore
from memory.karen_db import (
    _V1_SCHEMA_STATEMENTS,
    KAREN_DB_ARCHIVE_HASH_MIGRATION_ID,
    KAREN_DB_INITIAL_MIGRATION_ID,
    KAREN_DB_SCHEMA_VERSION,
    KarenDB,
    archive_record_hash,
)
from memory.karen_importers import (
    _insert_archive_message_row,
    _LegacyArchiveMessageRow,
    import_legacy_memory_files,
)
from memory.karen_stores import KarenArchiveStore


def _ensure_ready(path) -> None:
    db = KarenDB(path)
    try:
        db.ensure_ready()
    finally:
        db.close()


def _build_v1_database(
    path,
    rows: list[tuple[str, int, str, str | None]],
) -> None:
    conn = sqlite3.connect(path)
    try:
        for statement in _V1_SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "INSERT INTO schema_migrations(version, migration_id, applied_at) "
            "VALUES (1, ?, '2026-01-01T00:00:00Z')",
            (KAREN_DB_INITIAL_MIGRATION_ID,),
        )
        conn.executemany(
            "INSERT INTO archive_messages(persona_id, rowid, archive_id, record_json) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _migration_versions(path) -> list[int]:
    conn = sqlite3.connect(path)
    try:
        return [
            int(row[0])
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
    finally:
        conn.close()


def _schema_snapshot(path) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(path)
    try:
        return sorted(
            conn.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
        )
    finally:
        conn.close()


def _index_names(path, table: str = "archive_messages") -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {
            str(row[1])
            for row in conn.execute(f"PRAGMA index_list({table})")
        }
    finally:
        conn.close()


def _archive_rows(path, persona_id: str = "p") -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT rowid, record_json, record_hash FROM archive_messages "
            "WHERE persona_id = ? ORDER BY rowid",
            (persona_id,),
        ).fetchall()
    finally:
        conn.close()


def test_fresh_database_creates_hash_schema(tmp_path) -> None:
    path = tmp_path / "karen.db"
    _ensure_ready(path)

    conn = sqlite3.connect(path)
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == (
            KAREN_DB_SCHEMA_VERSION
        )
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(archive_messages)")
        }
        assert "record_hash" in columns
    finally:
        conn.close()

    indexes = _index_names(path)
    assert "idx_archive_persona_record_hash" in indexes
    assert "idx_archive_record_json" not in indexes
    assert _migration_versions(path) == [1, 2]


def test_v1_database_is_migrated_and_backfilled(tmp_path) -> None:
    path = tmp_path / "karen.db"
    rows = [
        ("p", 1, "a1", '{"content": "one"}'),
        ("p", 2, "a2", '{"content": "two"}'),
        ("p", 3, "a3", None),
        ("q", 1, "b1", '{"content": "other"}'),
    ]
    _build_v1_database(path, rows)

    _ensure_ready(path)

    migrated = [*_archive_rows(path, "p"), *_archive_rows(path, "q")]
    assert len(migrated) == 4
    for row in migrated:
        if row["record_json"] is None:
            assert row["record_hash"] is None
        else:
            assert row["record_hash"] == archive_record_hash(row["record_json"])

    indexes = _index_names(path)
    assert "idx_archive_persona_record_hash" in indexes
    assert "idx_archive_record_json" not in indexes
    assert _migration_versions(path) == [1, 2]

    conn = sqlite3.connect(path)
    try:
        max_version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        assert int(max_version) == KAREN_DB_SCHEMA_VERSION
        null_hashes = conn.execute(
            "SELECT COUNT(*) FROM archive_messages "
            "WHERE record_json IS NOT NULL AND record_hash IS NULL"
        ).fetchone()[0]
        assert int(null_hashes) == 0
    finally:
        conn.close()


def test_v1_database_missing_archive_table_is_recreated(tmp_path) -> None:
    path = tmp_path / "karen.db"
    _build_v1_database(path, [("p", 1, "a1", '{"content": "one"}')])
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE archive_messages")
        conn.commit()
    finally:
        conn.close()

    _ensure_ready(path)

    conn = sqlite3.connect(path)
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == (
            KAREN_DB_SCHEMA_VERSION
        )
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(archive_messages)")
        }
        assert "record_hash" in columns
    finally:
        conn.close()
    assert "idx_archive_persona_record_hash" in _index_names(path)


def test_concurrent_first_upgrade_is_safe(tmp_path) -> None:
    path = tmp_path / "karen.db"
    _build_v1_database(
        path,
        [
            ("p", index, f"a{index}", f'{{"content": "row {index}"}}')
            for index in range(1, 301)
        ],
    )
    errors: list[str] = []

    def upgrade(_: int) -> None:
        try:
            _ensure_ready(path)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(upgrade, range(16)))

    assert errors == []
    rows = _archive_rows(path)
    assert len(rows) == 300
    assert all(row["record_hash"] is not None for row in rows)
    assert _migration_versions(path) == [1, 2]


def test_migrated_schema_matches_fresh_schema(tmp_path) -> None:
    migrated = tmp_path / "migrated.db"
    _build_v1_database(migrated, [("p", 1, "a1", '{"content": "one"}')])
    _ensure_ready(migrated)

    fresh = tmp_path / "fresh.db"
    _ensure_ready(fresh)

    assert _schema_snapshot(migrated) == _schema_snapshot(fresh)


def test_v1_database_with_existing_hash_column_resumes_backfill(tmp_path) -> None:
    path = tmp_path / "karen.db"
    _build_v1_database(
        path,
        [("p", 1, "a1", '{"content": "one"}')],
    )
    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE archive_messages ADD COLUMN record_hash TEXT")
        conn.commit()
    finally:
        conn.close()

    _ensure_ready(path)

    rows = _archive_rows(path)
    assert rows[0]["record_hash"] == archive_record_hash(rows[0]["record_json"])
    assert _migration_versions(path) == [1, 2]


def test_backfill_crosses_batch_boundary(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(karen_db_module, "_ARCHIVE_HASH_BACKFILL_BATCH", 7)
    path = tmp_path / "karen.db"
    rows = [
        ("p", index, f"a{index}", f'{{"content": "row {index}"}}')
        for index in range(1, 20)
    ]
    _build_v1_database(path, rows)

    _ensure_ready(path)

    migrated = _archive_rows(path)
    assert len(migrated) == 19
    assert all(
        row["record_hash"] == archive_record_hash(row["record_json"])
        for row in migrated
    )


def test_current_database_skips_backfill(tmp_path, monkeypatch) -> None:
    path = tmp_path / "karen.db"
    _ensure_ready(path)

    def fail_if_called(conn):
        raise AssertionError("backfill should not run on a current database")

    monkeypatch.setattr(
        karen_db_module,
        "_backfill_archive_record_hashes",
        fail_if_called,
    )
    monkeypatch.setattr(
        KarenDB,
        "ensure_schema",
        lambda self, conn=None: (_ for _ in ()).throw(
            AssertionError("schema should not be repaired")
        ),
    )

    _ensure_ready(path)


def test_lingering_old_json_index_is_removed(tmp_path) -> None:
    path = tmp_path / "karen.db"
    _ensure_ready(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE INDEX idx_archive_record_json ON archive_messages(record_json)"
        )
        conn.commit()
    finally:
        conn.close()

    _ensure_ready(path)

    assert "idx_archive_record_json" not in _index_names(path)


def test_missing_hash_index_is_repaired(tmp_path) -> None:
    path = tmp_path / "karen.db"
    _ensure_ready(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP INDEX idx_archive_persona_record_hash")
        conn.commit()
    finally:
        conn.close()

    _ensure_ready(path)

    assert "idx_archive_persona_record_hash" in _index_names(path)


def _real_chat_record(index: int) -> dict:
    return {
        "role": "user",
        "content": f"归档消息 {index}",
        "conversation_id": "group:100",
        "direction": "inbound",
        "sender_id": str(200 + index),
        "sender_name": f"user{index}",
        "message_id": f"msg-{index}",
        "qq_visible": True,
        "timestamp": f"2026-01-01T10:{index % 60:02d}:00Z",
        "metadata": {"scope": "group", "group_id": "100"},
    }


@pytest.mark.asyncio
async def test_archive_store_writes_hash_and_dedupes(tmp_path) -> None:
    path = tmp_path / "karen.db"
    _ensure_ready(path)
    store = KarenArchiveStore(path, "p")
    record = _real_chat_record(1)

    await store.append_many([record])
    rows = _archive_rows(path)
    assert len(rows) == 1
    assert rows[0]["record_hash"] == archive_record_hash(rows[0]["record_json"])

    await store.append_many([record])
    assert len(_archive_rows(path)) == 1


@pytest.mark.asyncio
async def test_archive_dedupe_chunks_more_than_500_hashes(tmp_path) -> None:
    path = tmp_path / "karen.db"
    _ensure_ready(path)
    store = KarenArchiveStore(path, "p")
    records = [_real_chat_record(index) for index in range(600)]

    await store.append_many(records)
    assert len(_archive_rows(path)) == 600

    await store.append_many(records)
    assert len(_archive_rows(path)) == 600


@pytest.mark.asyncio
async def test_archive_dedupe_tolerates_hash_collisions(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "karen.db"
    _ensure_ready(path)
    monkeypatch.setattr(
        karen_stores_module,
        "archive_record_hash",
        lambda record_json: "collision",
    )
    store = KarenArchiveStore(path, "p")

    await store.append_many([_real_chat_record(1)])
    await store.append_many([_real_chat_record(2)])

    rows = _archive_rows(path)
    assert len(rows) == 2
    assert rows[0]["record_hash"] == rows[1]["record_hash"] == "collision"


def test_importer_writes_record_hash(tmp_path) -> None:
    path = tmp_path / "karen.db"
    _ensure_ready(path)
    conn = sqlite3.connect(path)
    try:
        row = _LegacyArchiveMessageRow(
            rowid=1,
            archive_id="a1",
            timestamp="2026-01-01T00:00:00Z",
            timestamp_unix=1,
            date_key="2026-01-01",
            month_key="2026-01",
            conversation_id="private:1",
            conversation_type="private",
            target_id="1",
            sender_id="1",
            sender_name="A",
            sender_role="user",
            direction="inbound",
            message_kind="text",
            content="hello",
            content_search="hello",
            original_msg_id="m1",
            reply_to_msg_id=None,
            metadata_json="{}",
            record_json='{"content": "hello"}',
            created_at="2026-01-01T00:00:00Z",
        )
        _insert_archive_message_row(conn, "p", row)
        conn.commit()

        stored = conn.execute(
            "SELECT record_hash FROM archive_messages "
            "WHERE persona_id = 'p' AND rowid = 1"
        ).fetchone()[0]
        assert stored == archive_record_hash('{"content": "hello"}')
    finally:
        conn.close()


def test_importer_keeps_null_hash_for_null_record_json(tmp_path) -> None:
    path = tmp_path / "karen.db"
    _ensure_ready(path)
    conn = sqlite3.connect(path)
    try:
        row = _LegacyArchiveMessageRow(
            rowid=1,
            archive_id="a1",
            timestamp=None,
            timestamp_unix=None,
            date_key=None,
            month_key=None,
            conversation_id=None,
            conversation_type=None,
            target_id=None,
            sender_id=None,
            sender_name=None,
            sender_role=None,
            direction=None,
            message_kind=None,
            content=None,
            content_search=None,
            original_msg_id=None,
            reply_to_msg_id=None,
            metadata_json=None,
            record_json=None,
            created_at=None,
        )
        _insert_archive_message_row(conn, "p", row)
        conn.commit()

        stored = conn.execute(
            "SELECT record_hash FROM archive_messages "
            "WHERE persona_id = 'p' AND rowid = 1"
        ).fetchone()[0]
        assert stored is None
    finally:
        conn.close()


def test_legacy_import_writes_record_hash(tmp_path) -> None:
    legacy_store = SqliteArchiveStore(tmp_path / "archive.sqlite3")
    asyncio.run(
        legacy_store.append_many(
            [
                {
                    "role": "user",
                    "content": "legacy hello",
                    "conversation_id": "group:1",
                    "direction": "inbound",
                    "sender_id": "1",
                    "sender_name": "A",
                    "message_id": "m1",
                    "qq_visible": True,
                    "timestamp": "2026-01-01T10:00:00Z",
                    "metadata": {"scope": "group", "group_id": "1"},
                }
            ]
        )
    )

    target = tmp_path / "karen.db"
    _build_v1_database(target, [])
    result = import_legacy_memory_files(
        target,
        tmp_path,
        "p",
        backup=False,
        usage_source_path=None,
    )

    assert result.archive.imported >= 1
    conn = sqlite3.connect(target)
    try:
        rows = conn.execute(
            "SELECT record_json, record_hash FROM archive_messages "
            "WHERE persona_id = 'p'"
        ).fetchall()
    finally:
        conn.close()
    assert rows
    assert all(
        record_hash == archive_record_hash(record_json)
        for record_json, record_hash in rows
    )


def test_migration_ids_are_unique_and_ordered() -> None:
    from memory.karen_db import _SCHEMA_MIGRATIONS

    versions = [migration.version for migration in _SCHEMA_MIGRATIONS]
    assert versions == sorted(versions)
    assert versions == list(range(1, KAREN_DB_SCHEMA_VERSION + 1))
    assert KAREN_DB_ARCHIVE_HASH_MIGRATION_ID in {
        migration.migration_id for migration in _SCHEMA_MIGRATIONS
    }
