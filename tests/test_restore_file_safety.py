"""Restore data integrity checks against real files and SQLite journals."""

from __future__ import annotations

import json
import shutil
import sqlite3
import stat
import zipfile

import pytest
from test_selective_backup_restore import _paths, _valid_user_archive

from core.restore import RestoreError
from core.selective_restore import _install_entry, apply_restore, preview_restore, transactional_restore


def test_rollback_restores_a_category_that_was_empty(tmp_path, monkeypatch):
    import core.selective_restore as module

    paths = _paths(tmp_path)
    paths.DATA_DIR.mkdir()
    paths.CONFIG_FILE.write_bytes(b"old")
    source = _valid_user_archive(tmp_path / "source.zip", {
        "data/config.yaml": b"new", "data/workspace/new.txt": b"new workspace",
    })
    rollback = _valid_user_archive(tmp_path / "rollback.zip", {"data/config.yaml": b"old"})
    install = module._install_entry
    failed = False

    def fail_once(archive, entry, target, roots):
        nonlocal failed
        result = install(archive, entry, target, roots)
        if entry["component"] == "workspace" and not failed:
            failed = True
            raise OSError("write interrupted")
        return result

    monkeypatch.setattr(module, "_install_entry", fail_once)
    with pytest.raises(OSError, match="write interrupted"):
        transactional_restore(source, rollback, paths, ["config", "workspace"])
    assert paths.CONFIG_FILE.read_bytes() == b"old"
    assert not (paths.WORKSPACE_DIR / "new.txt").exists()


def test_config_target_size_includes_preferences_not_in_the_archive(tmp_path):
    paths = _paths(tmp_path)
    paths.DATA_DIR.mkdir()
    paths.CONFIG_FILE.write_bytes(b"old")
    (paths.DATA_DIR / "knowledge.json").write_bytes(b"preferences")
    source = _valid_user_archive(tmp_path / "source.zip", {"data/config.yaml": b"new"})
    plan = preview_restore(source, paths, ["config"])
    assert plan["total_target_bytes"] == len(b"oldpreferences")


def test_existing_credential_files_are_restored_with_private_permissions(tmp_path):
    paths = _paths(tmp_path)
    paths.DATA_DIR.mkdir()
    paths.SECRETS_FILE.write_bytes(b"old")
    paths.SECRETS_FILE.chmod(0o644)
    source = _valid_user_archive(tmp_path / "source.zip", {"data/secrets.enc": b"new"})
    with zipfile.ZipFile(source) as archive:
        entry = json.loads(archive.read("manifest.json"))["files"][0]
        _install_entry(archive, entry, paths.SECRETS_FILE, [paths.DATA_DIR])
    assert stat.S_IMODE(paths.SECRETS_FILE.stat().st_mode) == 0o600


def test_restore_clears_stale_sqlite_wal_before_installing_backup(tmp_path):
    paths = _paths(tmp_path)
    target = paths.MEMORY_DIR / "alice.db"
    target.parent.mkdir(parents=True)
    live = tmp_path / "live.db"
    connection = sqlite3.connect(live)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE messages (body TEXT)")
    connection.execute("INSERT INTO messages VALUES ('later WAL data')")
    connection.commit()
    for suffix in ("", "-wal", "-shm"):
        shutil.copyfile(str(live) + suffix, str(target) + suffix)
    connection.close()
    backup_db = tmp_path / "backup.db"
    with sqlite3.connect(backup_db) as backup:
        backup.execute("CREATE TABLE messages (body TEXT)")
        backup.execute("INSERT INTO messages VALUES ('restored')")
    source = _valid_user_archive(tmp_path / "source.zip", {"data/memory/alice.db": backup_db.read_bytes()})
    with zipfile.ZipFile(source) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(members["manifest.json"])
    manifest["files"][0]["kind"] = "sqlite"
    members["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(source, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    apply_restore(source, paths, ["chats"])
    assert not (target.parent / "alice.db-wal").exists()
    with sqlite3.connect(target) as restored:
        assert restored.execute("SELECT body FROM messages").fetchall() == [("restored",)]


def test_ledger_managed_current_data_requires_a_controller(tmp_path):
    paths = _paths(tmp_path)
    paths.DATA_DIR.mkdir()
    (paths.DATA_DIR / "compatibility_state.json").write_text(json.dumps({
        "state_format": 1, "data_contract_epoch": 1, "migration_set_id": "M001",
        "generation": 1, "phase": "ready", "planes": {}, "features": [],
    }))
    source = _valid_user_archive(tmp_path / "source.zip", {"data/memory/chat.txt": b"old"})
    with pytest.raises(RestoreError, match="维护控制器"):
        preview_restore(source, paths, ["chats"])
