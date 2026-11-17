"""karen.db 基础 schema、版本与备份契约。"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)

KAREN_DB_SCHEMA_VERSION = 2
KAREN_DB_INITIAL_MIGRATION_ID = "v1_initial_schema"
KAREN_DB_ARCHIVE_HASH_MIGRATION_ID = "v2_archive_record_hash"
DEFAULT_BUSY_TIMEOUT_MS = 30_000

# archive_messages 去重指纹长度。32 位十六进制（128 bit）在本地单库规模下
# 碰撞概率可以忽略，同时索引宽度只有完整 JSON 的几十分之一。
_ARCHIVE_RECORD_HASH_LENGTH = 32


def archive_record_hash(record_json: str) -> str:
    """返回归档记录正文的去重指纹，供写入与迁移回填共用。"""

    return hashlib.sha256(record_json.encode("utf-8")).hexdigest()[
        :_ARCHIVE_RECORD_HASH_LENGTH
    ]


# schema 初始化只在版本检查失败时发生；这把进程内锁避免多个 Store 在
# 首次并发访问时重复跑同一批 DDL。跨进程仍由 SQLite 自己的写锁串行化。
_SCHEMA_INIT_LOCK = threading.Lock()
# 并发连接同时执行 PRAGMA journal_mode=WAL 会偶发 database is locked；
# 用一把进程锁串行化这一次 PRAGMA。每次连接仍会设置，避免原地恢复成
# DELETE 模式的数据库被身份缓存误判。
_WAL_SETUP_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class KarenDBSchemaVersion:
    """SQLite PRAGMA 与内部迁移表的版本读数。"""

    user_version: int
    migration_version: int | None


class KarenDBVersionError(RuntimeError):
    """当前代码不支持打开更高版本的 karen.db。"""


class KarenDB:
    """karen.db 的最小连接、schema 初始化与版本读取入口。"""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._conn: sqlite3.Connection | None = None

    def load(self) -> KarenDB:
        """打开数据库并确保 schema 可用。

        与 :meth:`ensure_ready` 等价；保留该名字供导入器、测试和显式
        管理入口使用，重复调用不会重复执行迁移或回填。
        """

        return self.ensure_ready()

    def ensure_ready(self) -> KarenDB:
        """打开数据库，仅在版本落后或对象缺失时初始化 schema。

        每次调用只做只读的版本与对象完整性检查（``PRAGMA user_version``、
        迁移表最大值、``sqlite_master`` 中的表和索引计数），避免每个
        Store 的每次读写都重跑整套 DDL。数据库被恢复成空文件或部分
        表/索引缺失时，检查会失败并自动补建 schema。
        """

        conn = self.connect()
        if self._is_schema_current(conn):
            return self
        with _SCHEMA_INIT_LOCK:
            if self._is_schema_current(conn):
                return self
            try:
                self.ensure_schema(conn)
            except Exception:
                # 初始化失败的连接不应留给调用方继续使用。
                self.close()
                raise
        return self

    def _is_schema_current(self, conn: sqlite3.Connection) -> bool:
        version = self.schema_version(conn)
        if (
            version.user_version != KAREN_DB_SCHEMA_VERSION
            or version.migration_version != KAREN_DB_SCHEMA_VERSION
        ):
            return False
        return _schema_objects_present(conn)

    def close(self) -> None:
        """关闭当前持有的连接。"""

        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def connect(self) -> sqlite3.Connection:
        """返回带项目约定 PRAGMA 的 sqlite3 连接。"""

        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
            conn.row_factory = sqlite3.Row
            _configure_connection(conn, self.busy_timeout_ms)
            self._conn = conn
        return self._conn

    def ensure_schema(self, conn: sqlite3.Connection | None = None) -> None:
        """把 karen.db 迁移到当前版本，并修复缺失的当前版本对象。"""

        target = conn if conn is not None else self.connect()
        _raise_if_future_schema(self.schema_version(target))
        self._apply_pending_migrations(target)
        _ensure_current_schema(target)

    def _apply_pending_migrations(self, conn: sqlite3.Connection) -> None:
        # 迁移表与被迁移的表必须先存在：user_version 可能被外部工具单独
        # 抬高，或者旧库丢失了某张表。建表语句本身是幂等的。
        with conn:
            conn.execute(_MIGRATIONS_TABLE_SQL)
            for statement in _TABLE_CREATION_STATEMENTS:
                conn.execute(statement)
        version = self.schema_version(conn)
        applied = max(version.user_version, version.migration_version or 0)
        for migration in _SCHEMA_MIGRATIONS:
            if migration.version <= applied:
                continue
            with conn:
                for statement in migration.statements:
                    conn.execute(statement)
                if migration.apply is not None:
                    migration.apply(conn)
                conn.execute(f"PRAGMA user_version = {migration.version}")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO schema_migrations(
                        version, migration_id, applied_at
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        migration.version,
                        migration.migration_id,
                        _utc_timestamp(),
                    ),
                )
            logger.info(
                "karen.db 迁移完成 migration_id=%s version=%s",
                migration.migration_id,
                migration.version,
            )

    def schema_version(self, conn: sqlite3.Connection | None = None) -> KarenDBSchemaVersion:
        """读取 PRAGMA user_version 与内部迁移表中的最高版本。"""

        target = conn if conn is not None else self.connect()
        user_version = int(target.execute("PRAGMA user_version").fetchone()[0])
        try:
            row = target.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        except sqlite3.OperationalError:
            migration_version = None
        else:
            migration_version = None if row is None or row[0] is None else int(row[0])
        return KarenDBSchemaVersion(
            user_version=user_version,
            migration_version=migration_version,
        )


def backup_existing_database(
    db_path: str | Path,
    *,
    schema_version: int = KAREN_DB_SCHEMA_VERSION,
    timestamp: str | datetime | None = None,
) -> Path | None:
    """把已存在的 karen.db 备份到同级 backups/；源库不存在时不创建备份。"""

    source = Path(db_path)
    if not source.exists():
        return None

    backups_dir = source.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = _backup_timestamp(timestamp)
    target, target_stat = _reserve_unique_backup_path(
        backups_dir,
        f"{source.stem}-v{int(schema_version)}-{stamp}",
        source.suffix or ".db",
    )

    source_conn: sqlite3.Connection | None = None
    try:
        source_uri = f"file:{quote(source.resolve().as_posix(), safe='/:')}?mode=ro"
        source_conn = sqlite3.connect(source_uri, uri=True)
        target_conn = sqlite3.connect(target)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
    except Exception:
        _unlink_reserved_backup(target, target_stat)
        raise
    finally:
        if source_conn is not None:
            source_conn.close()
    return target


def _configure_connection(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    with _WAL_SETUP_LOCK:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _backup_timestamp(timestamp: str | datetime | None) -> str:
    if timestamp is None:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if isinstance(timestamp, datetime):
        value = timestamp
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return timestamp


def _raise_if_future_schema(version: KarenDBSchemaVersion) -> None:
    future_parts = []
    if version.user_version > KAREN_DB_SCHEMA_VERSION:
        future_parts.append(f"user_version={version.user_version}")
    if (
        version.migration_version is not None
        and version.migration_version > KAREN_DB_SCHEMA_VERSION
    ):
        future_parts.append(f"migration_version={version.migration_version}")
    if future_parts:
        details = ", ".join(future_parts)
        raise KarenDBVersionError(
            f"karen.db schema version is newer than supported: {details}; "
            f"supported_version={KAREN_DB_SCHEMA_VERSION}"
        )


def _schema_objects_present(conn: sqlite3.Connection) -> bool:
    """确认当前契约里的表和索引都还在，供 ``ensure_ready`` 自愈缺表库。"""

    table_placeholders = ",".join("?" for _ in _CURRENT_TABLE_NAMES)
    index_placeholders = ",".join("?" for _ in _CURRENT_INDEX_NAMES)
    row = conn.execute(
        f"""
        SELECT
            (SELECT COUNT(*) FROM sqlite_master
             WHERE type = 'table' AND name IN ({table_placeholders})),
            (SELECT COUNT(*) FROM sqlite_master
             WHERE type = 'index' AND name IN ({index_placeholders})),
            (SELECT COUNT(*) FROM sqlite_master WHERE name = ?)
        """,
        (
            *_CURRENT_TABLE_NAMES,
            *_CURRENT_INDEX_NAMES,
            _ARCHIVE_RECORD_JSON_INDEX_NAME,
        ),
    ).fetchone()
    if row is None:
        return False
    # 旧 JSON 索引必须已经删除；残留时按不完整处理，让修复流程回收它。
    return (
        int(row[0]) == len(_CURRENT_TABLE_NAMES)
        and int(row[1]) == len(_CURRENT_INDEX_NAMES)
        and int(row[2]) == 0
    )


def _reserve_unique_backup_path(
    directory: Path,
    stem: str,
    suffix: str,
) -> tuple[Path, os.stat_result]:
    counter = 1
    candidate = directory / f"{stem}{suffix}"
    while True:
        try:
            with candidate.open("xb") as reserved:
                reserved_stat = os.fstat(reserved.fileno())
            return candidate, reserved_stat
        except FileExistsError:
            pass
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1


def _unlink_reserved_backup(path: Path, reserved_stat: os.stat_result) -> None:
    try:
        current_stat = path.stat()
    except FileNotFoundError:
        return
    if os.path.samestat(current_stat, reserved_stat):
        path.unlink()


_MIGRATIONS_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        migration_id TEXT UNIQUE NOT NULL,
        applied_at TEXT NOT NULL
    )
    """

_V1_SCHEMA_STATEMENTS = (
    _MIGRATIONS_TABLE_SQL,
    """
    CREATE TABLE IF NOT EXISTS history_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id TEXT NOT NULL,
        history_index INTEGER NOT NULL,
        conversation_id TEXT,
        role TEXT,
        content_hash TEXT,
        content_length INTEGER NOT NULL DEFAULT 0,
        record_json TEXT NOT NULL,
        timestamp TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE(persona_id, history_index)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_history_persona_index ON history_records(persona_id, history_index)",
    "CREATE INDEX IF NOT EXISTS idx_history_conversation ON history_records(persona_id, conversation_id, history_index)",
    "CREATE INDEX IF NOT EXISTS idx_history_role ON history_records(persona_id, role)",
    "CREATE INDEX IF NOT EXISTS idx_history_content_hash ON history_records(content_hash)",
    """
    CREATE TABLE IF NOT EXISTS important_memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id TEXT NOT NULL,
        memory_id TEXT NOT NULL,
        timestamp TEXT,
        scope TEXT,
        pinned INTEGER NOT NULL DEFAULT 0,
        content TEXT,
        item_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL,
        UNIQUE(persona_id, memory_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_important_persona_time ON important_memories(persona_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_important_persona_scope ON important_memories(persona_id, scope)",
    "CREATE INDEX IF NOT EXISTS idx_important_pinned ON important_memories(persona_id, pinned)",
    """
    CREATE TABLE IF NOT EXISTS rolling_summary (
        persona_id TEXT PRIMARY KEY,
        summary_text TEXT NOT NULL DEFAULT '',
        archived_until_json TEXT NOT NULL DEFAULT '{}',
        active_start_index INTEGER,
        summary_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_log (
        persona_id TEXT NOT NULL,
        event_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        event_uuid TEXT NOT NULL,
        conversation_id TEXT,
        session_id TEXT,
        turn_id TEXT,
        source TEXT,
        external_id TEXT,
        tool_call_id TEXT,
        parent_event_id INTEGER,
        idempotency_key TEXT,
        timestamp_unix REAL NOT NULL,
        created_at_unix REAL NOT NULL,
        payload_json TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        PRIMARY KEY(persona_id, event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_projection_state (
        persona_id TEXT NOT NULL,
        name TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY(persona_id, name)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_log_persona_idempotency ON event_log(persona_id, idempotency_key) WHERE idempotency_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_event_log_conversation_event ON event_log(persona_id, conversation_id, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_event_log_type_event ON event_log(persona_id, event_type, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_event_log_session_event ON event_log(persona_id, session_id, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_event_log_external ON event_log(persona_id, source, external_id)",
    "CREATE INDEX IF NOT EXISTS idx_event_log_parent ON event_log(persona_id, parent_event_id)",
    """
    CREATE TABLE IF NOT EXISTS archive_messages (
        persona_id TEXT NOT NULL,
        rowid INTEGER NOT NULL,
        archive_id TEXT NOT NULL,
        timestamp TEXT,
        timestamp_unix INTEGER,
        date_key TEXT,
        month_key TEXT,
        conversation_id TEXT,
        conversation_type TEXT,
        target_id TEXT,
        sender_id TEXT,
        sender_name TEXT,
        sender_role TEXT,
        direction TEXT,
        message_kind TEXT,
        content TEXT,
        content_search TEXT,
        original_msg_id TEXT,
        reply_to_msg_id TEXT,
        metadata_json TEXT,
        record_json TEXT,
        created_at TEXT,
        PRIMARY KEY(persona_id, rowid),
        UNIQUE(persona_id, archive_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS archive_message_media (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL,
        archive_id TEXT NOT NULL,
        media_type TEXT,
        workspace_path TEXT,
        original_name TEXT,
        metadata_json TEXT,
        PRIMARY KEY(persona_id, id),
        FOREIGN KEY(persona_id, archive_id)
            REFERENCES archive_messages(persona_id, archive_id)
            ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_archive_time ON archive_messages(persona_id, timestamp_unix)",
    "CREATE INDEX IF NOT EXISTS idx_archive_conversation_time ON archive_messages(persona_id, conversation_id, timestamp_unix)",
    "CREATE INDEX IF NOT EXISTS idx_archive_sender_time ON archive_messages(persona_id, sender_id, timestamp_unix)",
    "CREATE INDEX IF NOT EXISTS idx_archive_original_msg ON archive_messages(persona_id, original_msg_id)",
    "CREATE INDEX IF NOT EXISTS idx_archive_date ON archive_messages(persona_id, date_key)",
    "CREATE INDEX IF NOT EXISTS idx_archive_record_json ON archive_messages(record_json)",
    "CREATE INDEX IF NOT EXISTS idx_archive_media_archive ON archive_message_media(persona_id, archive_id)",
    """
    CREATE TABLE IF NOT EXISTS usage_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id TEXT,
        ts REAL,
        provider TEXT,
        model TEXT,
        agent TEXT,
        operation TEXT,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        reasoning_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
        total_tokens INTEGER NOT NULL DEFAULT 0,
        record_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_records(ts)",
    "CREATE INDEX IF NOT EXISTS idx_usage_provider_model ON usage_records(provider, model)",
    "CREATE INDEX IF NOT EXISTS idx_usage_agent_operation ON usage_records(agent, operation)",
    "CREATE INDEX IF NOT EXISTS idx_usage_persona_ts ON usage_records(persona_id, ts)",
    """
    CREATE TABLE IF NOT EXISTS persona_schema_version_legacy (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL CHECK (id = 1),
        version INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_state (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL CHECK (id = 1),
        state_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_state_log (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL,
        state_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_update_audits (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL,
        audit_json TEXT NOT NULL,
        "trigger" TEXT,
        conversation_id TEXT,
        user_id TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_effects (
        persona_id TEXT NOT NULL,
        effect_id TEXT NOT NULL,
        effect_json TEXT NOT NULL,
        expires_at TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, effect_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_todos (
        persona_id TEXT NOT NULL,
        todo_id TEXT NOT NULL,
        todo_json TEXT NOT NULL,
        completed INTEGER NOT NULL DEFAULT 0,
        expires_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, todo_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_cues (
        persona_id TEXT NOT NULL,
        cue_id TEXT NOT NULL,
        cue_json TEXT NOT NULL,
        expires_at TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, cue_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_inner_monologues (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL,
        monologue_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_user_profiles (
        persona_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        profile_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_important_state_legacy (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL CHECK (id = 1),
        memories_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_daily_trajectories (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL,
        trajectory_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_arc (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL,
        event_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_sleep_records (
        persona_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        record_json TEXT NOT NULL,
        started_at TEXT,
        ended_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, record_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_eat_records (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL,
        record_id TEXT,
        record_json TEXT NOT NULL,
        ended_at TEXT,
        status TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_persona_effects_expires ON persona_effects(persona_id, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_persona_todos_completed ON persona_todos(persona_id, completed)",
    "CREATE INDEX IF NOT EXISTS idx_persona_cues_expires ON persona_cues(persona_id, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_persona_monologues_recent ON persona_inner_monologues(persona_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_persona_trajectories_recent ON persona_daily_trajectories(persona_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_persona_update_audits_conversation ON persona_update_audits(persona_id, conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_persona_update_audits_user ON persona_update_audits(persona_id, user_id)",
    "CREATE INDEX IF NOT EXISTS idx_persona_sleep_started ON persona_sleep_records(persona_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_persona_eat_record_id ON persona_eat_records(persona_id, record_id)",
)

_V1_TABLE_NAMES = tuple(
    match.group(1)
    for statement in _V1_SCHEMA_STATEMENTS
    if (match := re.search(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", statement))
)
_V1_INDEX_NAMES = tuple(
    match.group(1)
    for statement in _V1_SCHEMA_STATEMENTS
    if (
        match := re.search(
            r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS\s+(\w+)",
            statement,
        )
    )
)
_V1_SCHEMA_NAMES = (*_V1_TABLE_NAMES, *_V1_INDEX_NAMES)

# ---- v2：archive_messages.record_hash ----
#
# v1 用完整 record_json 建索引做去重，索引体积约等于归档表本身。v2 改为
# 32 位指纹的复合窄索引；旧索引在新索引建好后删除。v1 语句保持不变，
# 便于旧库按历史顺序重放迁移。

_ARCHIVE_RECORD_HASH_COLUMN = "record_hash"
_ARCHIVE_RECORD_HASH_INDEX_NAME = "idx_archive_persona_record_hash"
_ARCHIVE_RECORD_HASH_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_archive_persona_record_hash "
    "ON archive_messages(persona_id, record_hash)"
)
_ARCHIVE_RECORD_JSON_INDEX_NAME = "idx_archive_record_json"
_ARCHIVE_HASH_BACKFILL_BATCH = 1000

# 当前版本需要存在的对象：v1 全部对象中去掉旧 JSON 索引，再加新指纹索引。
_CURRENT_SCHEMA_STATEMENTS = tuple(
    statement
    for statement in _V1_SCHEMA_STATEMENTS
    if _ARCHIVE_RECORD_JSON_INDEX_NAME not in statement
)
_TABLE_CREATION_STATEMENTS = tuple(
    statement
    for statement in _CURRENT_SCHEMA_STATEMENTS
    if "CREATE TABLE IF NOT EXISTS" in statement
)
_CURRENT_TABLE_NAMES = _V1_TABLE_NAMES
_CURRENT_INDEX_NAMES = tuple(
    name for name in _V1_INDEX_NAMES if name != _ARCHIVE_RECORD_JSON_INDEX_NAME
) + (_ARCHIVE_RECORD_HASH_INDEX_NAME,)
_CURRENT_SCHEMA_NAMES = (*_CURRENT_TABLE_NAMES, *_CURRENT_INDEX_NAMES)


def _ensure_archive_record_hash_column(conn: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(archive_messages)")
    }
    if _ARCHIVE_RECORD_HASH_COLUMN in columns:
        return
    try:
        with conn:
            conn.execute(
                f"ALTER TABLE archive_messages "
                f"ADD COLUMN {_ARCHIVE_RECORD_HASH_COLUMN} TEXT"
            )
    except sqlite3.OperationalError as exc:
        # 两个进程同时升级时，另一个进程可能刚加完这一列。
        if "duplicate column name" not in str(exc).lower():
            raise


def _backfill_archive_record_hashes(conn: sqlite3.Connection) -> int:
    """为存量行补写 record_hash；按 persona + rowid 分批，可中断续跑。"""

    updated = 0
    personas = conn.execute(
        "SELECT DISTINCT persona_id FROM archive_messages "
        "WHERE record_hash IS NULL AND record_json IS NOT NULL"
    ).fetchall()
    for (persona_id,) in personas:
        # 覆盖所有 SQLite INTEGER 取值；旧导入库的 rowid 理论上可能非正。
        last_rowid = -(2**63)
        while True:
            rows = conn.execute(
                """
                SELECT rowid, record_json FROM archive_messages
                WHERE persona_id = ? AND rowid > ?
                  AND record_hash IS NULL AND record_json IS NOT NULL
                ORDER BY rowid
                LIMIT ?
                """,
                (persona_id, last_rowid, _ARCHIVE_HASH_BACKFILL_BATCH),
            ).fetchall()
            if not rows:
                break
            conn.executemany(
                f"UPDATE archive_messages SET {_ARCHIVE_RECORD_HASH_COLUMN} = ? "
                "WHERE persona_id = ? AND rowid = ?",
                [
                    (archive_record_hash(str(row["record_json"])), persona_id, row["rowid"])
                    for row in rows
                ],
            )
            updated += len(rows)
            last_rowid = int(rows[-1]["rowid"])
            # 分批提交：迁移进程中断后，下次从剩余的 NULL 行继续。
            conn.commit()
            if len(rows) < _ARCHIVE_HASH_BACKFILL_BATCH:
                break
    if updated:
        logger.info("karen.db 回填 archive_messages.record_hash: %s 行", updated)
    return updated


def _apply_v2_archive_record_hash(conn: sqlite3.Connection) -> None:
    _ensure_archive_record_hash_column(conn)
    _backfill_archive_record_hashes(conn)
    conn.execute(_ARCHIVE_RECORD_HASH_INDEX_SQL)
    conn.execute(f"DROP INDEX IF EXISTS {_ARCHIVE_RECORD_JSON_INDEX_NAME}")


def _ensure_current_schema(conn: sqlite3.Connection) -> None:
    """补建当前版本的表/索引，并保证指纹列与历史版本记录完整。

    该入口只在 ``ensure_ready`` 发现版本或对象不完整时调用，正常热路径
    不会执行。所有语句都是幂等的，可重复运行。
    """

    with conn:
        for statement in _CURRENT_SCHEMA_STATEMENTS:
            conn.execute(statement)
    _ensure_archive_record_hash_column(conn)
    _backfill_archive_record_hashes(conn)
    with conn:
        conn.execute(_ARCHIVE_RECORD_HASH_INDEX_SQL)
        conn.execute(f"DROP INDEX IF EXISTS {_ARCHIVE_RECORD_JSON_INDEX_NAME}")
        conn.execute(f"PRAGMA user_version = {KAREN_DB_SCHEMA_VERSION}")
        for version, migration_id in _SCHEMA_MIGRATION_IDS:
            conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(
                    version, migration_id, applied_at
                )
                VALUES (?, ?, ?)
                """,
                (version, migration_id, _utc_timestamp()),
            )


@dataclass(frozen=True, slots=True)
class _SchemaMigration:
    version: int
    migration_id: str
    statements: tuple[str, ...] = ()
    apply: Callable[[sqlite3.Connection], None] | None = None


_SCHEMA_MIGRATIONS = (
    _SchemaMigration(
        version=1,
        migration_id=KAREN_DB_INITIAL_MIGRATION_ID,
        statements=tuple(_V1_SCHEMA_STATEMENTS),
    ),
    _SchemaMigration(
        version=2,
        migration_id=KAREN_DB_ARCHIVE_HASH_MIGRATION_ID,
        apply=_apply_v2_archive_record_hash,
    ),
)
_SCHEMA_MIGRATION_IDS = tuple(
    (migration.version, migration.migration_id) for migration in _SCHEMA_MIGRATIONS
)


__all__ = [
    "KAREN_DB_SCHEMA_VERSION",
    "KarenDB",
    "KarenDBSchemaVersion",
    "KarenDBVersionError",
    "archive_record_hash",
    "backup_existing_database",
]
