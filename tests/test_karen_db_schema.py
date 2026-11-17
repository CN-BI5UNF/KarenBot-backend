"""karen.db schema 快速自检与恢复行为。"""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from memory.karen_db import (
    KAREN_DB_SCHEMA_VERSION,
    KarenDB,
    KarenDBVersionError,
)


def _table_names(path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()


def _ensure_ready(path) -> None:
    db = KarenDB(path)
    try:
        db.ensure_ready()
    finally:
        db.close()


def test_all_v1_schema_statements_are_tracked() -> None:
    from memory.karen_db import _V1_SCHEMA_NAMES, _V1_SCHEMA_STATEMENTS

    assert len(_V1_SCHEMA_NAMES) == len(_V1_SCHEMA_STATEMENTS)
    assert len(set(_V1_SCHEMA_NAMES)) == len(_V1_SCHEMA_NAMES)


def test_current_schema_names_replace_json_index() -> None:
    from memory.karen_db import (
        _CURRENT_SCHEMA_NAMES,
        _CURRENT_SCHEMA_STATEMENTS,
        _V1_SCHEMA_STATEMENTS,
    )

    assert len(set(_CURRENT_SCHEMA_NAMES)) == len(_CURRENT_SCHEMA_NAMES)
    # 当前对象 = 所有建表/建索引语句（去掉旧 JSON 索引） + 专门的指纹索引。
    assert len(_CURRENT_SCHEMA_NAMES) == len(_CURRENT_SCHEMA_STATEMENTS) + 1
    assert any(
        "idx_archive_record_json" in statement
        for statement in _V1_SCHEMA_STATEMENTS
    )
    assert "idx_archive_record_json" not in _CURRENT_SCHEMA_NAMES
    assert "idx_archive_persona_record_hash" in _CURRENT_SCHEMA_NAMES


def test_ensure_ready_initializes_fresh_database(tmp_path) -> None:
    path = tmp_path / "karen.db"

    _ensure_ready(path)

    assert "history_records" in _table_names(path)
    assert "archive_messages" in _table_names(path)
    conn = sqlite3.connect(path)
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == (
            KAREN_DB_SCHEMA_VERSION
        )
        row = conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        assert int(row[0]) == KAREN_DB_SCHEMA_VERSION
    finally:
        conn.close()


def test_ensure_ready_skips_ddl_when_schema_is_current(tmp_path) -> None:
    path = tmp_path / "karen.db"
    _ensure_ready(path)

    with patch.object(
        KarenDB,
        "ensure_schema",
        side_effect=AssertionError("schema should not be re-initialized"),
    ):
        _ensure_ready(path)


def test_ensure_ready_rebuilds_replaced_database(tmp_path) -> None:
    path = tmp_path / "karen.db"
    _ensure_ready(path)

    for suffix in ("", "-wal", "-shm"):
        candidate = path.with_name(path.name + suffix)
        candidate.unlink(missing_ok=True)
    sqlite3.connect(path).close()

    _ensure_ready(path)

    assert "history_records" in _table_names(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_ensure_ready_repairs_missing_table(tmp_path) -> None:
    path = tmp_path / "karen.db"
    _ensure_ready(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE archive_messages")
        conn.commit()
    finally:
        conn.close()

    _ensure_ready(path)

    assert "archive_messages" in _table_names(path)


def test_ensure_ready_repairs_missing_index(tmp_path) -> None:
    path = tmp_path / "karen.db"
    _ensure_ready(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP INDEX idx_archive_conversation_time")
        conn.commit()
    finally:
        conn.close()

    _ensure_ready(path)

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("idx_archive_conversation_time",),
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_ensure_ready_restores_wal_after_external_switch(tmp_path) -> None:
    path = tmp_path / "karen.db"
    _ensure_ready(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()

    db = KarenDB(path)
    try:
        db.ensure_ready()
        connection = db.connect()
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        db.close()


def test_ensure_ready_repairs_missing_migration_row(tmp_path) -> None:
    path = tmp_path / "karen.db"
    _ensure_ready(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM schema_migrations")
        conn.commit()
    finally:
        conn.close()

    _ensure_ready(path)

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        assert int(row[0]) == KAREN_DB_SCHEMA_VERSION
    finally:
        conn.close()


def test_load_is_idempotent_and_skips_migrations(tmp_path) -> None:
    path = tmp_path / "karen.db"
    db = KarenDB(path)
    db.load()
    db.close()

    with patch.object(
        KarenDB,
        "ensure_schema",
        side_effect=AssertionError("load() should not re-run migrations"),
    ):
        second = KarenDB(path)
        second.load()
        second.close()


def test_ensure_ready_reopens_after_close(tmp_path) -> None:
    path = tmp_path / "karen.db"
    db = KarenDB(path)
    db.ensure_ready()
    first = db._conn
    assert first is not None
    db.ensure_ready()
    assert db._conn is first

    db.close()
    db.ensure_ready()
    assert db._conn is not None
    db.close()


def test_ensure_ready_rejects_future_schema_version(tmp_path) -> None:
    path = tmp_path / "karen.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA user_version = {KAREN_DB_SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(KarenDBVersionError):
        _ensure_ready(path)


def test_ensure_ready_is_safe_under_concurrent_first_use(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "karen.db"
    calls = 0
    calls_lock = threading.Lock()
    original = KarenDB.ensure_schema

    def counting_ensure_schema(self, conn=None):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.02)
        return original(self, conn)

    monkeypatch.setattr(KarenDB, "ensure_schema", counting_ensure_schema)

    def open_once(_: int) -> None:
        db = KarenDB(path)
        db.ensure_ready()
        db.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(open_once, range(16)))

    assert calls == 1
    assert "history_records" in _table_names(path)
