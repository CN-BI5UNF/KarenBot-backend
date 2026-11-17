"""karen.db VACUUM 维护入口：跳过阈值、空间检查与回收统计。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core import storage_maintenance as maintenance_module
from core.storage_maintenance import (
    StorageMaintenanceError,
    StorageMaintenanceService,
    _freelist_bytes,
)
from memory.karen_db import KarenDB


class _Paths:
    def __init__(self, root) -> None:
        self.DATA_DIR = root
        self.MAINTENANCE_DB_FILE = root / "maintenance.sqlite3"

    def memory_dir_for(self, name: str):
        return self.DATA_DIR / "memory" / name


class _Persona:
    name = "p"


class _Runtime:
    def __init__(self, root) -> None:
        self.paths = _Paths(root)
        self.persona = _Persona()


def _service(tmp_path) -> tuple[StorageMaintenanceService, Path]:
    service = StorageMaintenanceService(_Runtime(tmp_path))
    db_path = tmp_path / "memory" / "p" / "p.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = KarenDB(db_path)
    db.ensure_ready()
    db.close()
    return service, db_path


def _create_freelist(db_path, rows: int = 2000, payload: int = 2000) -> int:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE vacuum_probe(x BLOB)")
        conn.executemany(
            "INSERT INTO vacuum_probe VALUES (?)",
            [(b"x" * payload,) for _ in range(rows)],
        )
        conn.commit()
        conn.execute("DROP TABLE vacuum_probe")
        conn.commit()
        return _freelist_bytes(conn)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_vacuum_reclaims_free_pages(tmp_path) -> None:
    service, db_path = _service(tmp_path)
    freelist = _create_freelist(db_path)
    before = db_path.stat().st_size
    assert freelist > 0

    result = await service.vacuum_karen_db(min_reclaim_bytes=0)

    assert result["ok"] is True
    assert result["skipped"] is False
    assert result["freelist_before_bytes"] == freelist
    assert result["freelist_after_bytes"] == 0
    assert result["after_bytes"] < before
    assert result["reclaimed_bytes"] == before - result["after_bytes"]


@pytest.mark.asyncio
async def test_vacuum_skips_below_threshold_by_default(tmp_path) -> None:
    service, db_path = _service(tmp_path)
    _create_freelist(db_path, rows=10, payload=16)

    result = await service.vacuum_karen_db(min_reclaim_bytes=1024 * 1024)

    assert result["skipped"] is True
    assert result["reason"] == "below_threshold"
    assert result["reclaimed_bytes"] == 0


@pytest.mark.asyncio
async def test_vacuum_force_ignores_threshold(tmp_path) -> None:
    service, db_path = _service(tmp_path)
    _create_freelist(db_path, rows=10, payload=16)

    result = await service.vacuum_karen_db(
        min_reclaim_bytes=1024 * 1024,
        force=True,
    )

    assert result["skipped"] is False


@pytest.mark.asyncio
async def test_vacuum_rejects_when_free_space_too_low(
    tmp_path, monkeypatch
) -> None:
    service, db_path = _service(tmp_path)
    _create_freelist(db_path)
    monkeypatch.setattr(
        maintenance_module.shutil,
        "disk_usage",
        lambda path: type(
            "Usage", (), {"total": 1, "used": 1, "free": 0}
        )(),
    )

    with pytest.raises(StorageMaintenanceError) as excinfo:
        await service.vacuum_karen_db(min_reclaim_bytes=0)

    assert excinfo.value.code == "vacuum_no_space"


@pytest.mark.asyncio
async def test_vacuum_reports_missing_database(tmp_path) -> None:
    service = StorageMaintenanceService(_Runtime(tmp_path))

    with pytest.raises(StorageMaintenanceError) as excinfo:
        await service.vacuum_karen_db()

    assert excinfo.value.code == "database_missing"
