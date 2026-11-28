"""Regression tests for per-component inventory, backup selection and restore."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.backup import (
    BACKUP_COMPONENT_IDS,
    BACKUP_FORMAT,
    BACKUP_TYPE_USER_EXPORT,
    BackupError,
    BackupService,
    _backup_component_for_path,
    _component_totals,
    _validate_component_selection,
)
from core.restore import RestoreError, verify_backup
from core.selective_restore import (
    apply_restore,
    preview_restore,
    transactional_restore,
)


def _paths(root: Path):
    data = root / "data"
    personas = root / "personas"
    names = {
        "PROJECT_ROOT": root,
        "DATA_DIR": data,
        "CONFIG_FILE": data / "config.yaml",
        "SECRETS_FILE": data / "secrets.enc",
        "SECRETS_META_FILE": data / "secrets.meta",
        "RSA_PUBLIC_KEY_FILE": data / "rsa_public.pem",
        "RSA_PRIVATE_KEY_FILE": data / "rsa_private.pem",
        "TOKEN_CALIBRATION_FILE": data / "token_calibration.json",
        "MEMORY_DIR": data / "memory",
        "VECTOR_DIR": data / "vector",
        "EMOJI_DIR": data / "emoji",
        "WORKSPACE_DIR": data / "workspace",
        "USER_PROVIDER_PRESETS_DIR": data / "providers" / "presets",
        "PERSONAS_DIR": personas,
        "LOGS_DIR": data / "logs",
        "CACHE_DIR": data / "cache",
        "TMP_DIR": data / "tmp",
        "MODELS_DIR": data / "models",
        "MAINTENANCE_DIR": data / "maintenance",
        "MAINTENANCE_DB_FILE": data / "maintenance.sqlite3",
        "instance_name": "default",
    }
    return SimpleNamespace(**names)


def _archive(path: Path, files: dict[str, bytes], *, components=None) -> Path:
    entries = []
    for name, content in files.items():
        component = _backup_component_for_path(name)
        if components is not None and component not in components:
            continue
        entries.append({
            "path": name,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "kind": "file",
            "consistent": True,
            "component": component,
        })
    manifest = {
        "format": BACKUP_FORMAT,
        "format_version": BACKUP_FORMAT,
        "backup_type": BACKUP_TYPE_USER_EXPORT,
        "files": entries,
        "partial": False,
        "includes_secrets": False,
        "includes_keys": False,
        "credential_restorable": False,
        "capabilities": {"chat_restorable": False},
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README_RESTORE.txt", "restore fixture")
        for name, content in files.items():
            if components is None or _backup_component_for_path(name) in components:
                archive.writestr(name, content)
        archive.writestr("manifest.json", json.dumps(manifest))
    return path


def _valid_user_archive(path: Path, files: dict[str, bytes], *, components=None) -> Path:
    selected = components or set(BACKUP_COMPONENT_IDS)
    entries = []
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README_RESTORE.txt", "restore fixture")
        for name, content in files.items():
            component = _backup_component_for_path(name)
            if component not in selected:
                continue
            archive.writestr(name, content)
            entries.append({
                "path": name, "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "kind": "file", "consistent": True, "component": component,
            })
        manifest = {
            "format": BACKUP_FORMAT, "format_version": BACKUP_FORMAT,
            "backup_type": BACKUP_TYPE_USER_EXPORT, "files": entries,
            "partial": False, "includes_secrets": False, "includes_keys": False,
            "credential_restorable": False,
            "capabilities": {"chat_restorable": False},
        }
        archive.writestr("manifest.json", json.dumps(manifest))
    return path


def test_component_selection_legacy_and_invalid_ids():
    assert _validate_component_selection(None) == set(BACKUP_COMPONENT_IDS)
    assert _validate_component_selection(["config", "chats"]) == {"config", "chats"}
    for value in ([], ["unknown"], "config", [1]):
        with pytest.raises(BackupError):
            _validate_component_selection(value)


def test_path_mapping_and_actual_manifest_component_totals():
    assert _backup_component_for_path("data/secrets.enc") == "credentials"
    assert _backup_component_for_path("data/memory/alice/chat.db") == "chats"
    assert _backup_component_for_path("personas/alice/persona.py") == "personas"
    totals = _component_totals([
        {"path": "data/config.yaml", "size": 10},
        {"path": "data/config.yaml.bak", "size": 4, "component": "config"},
        {"path": "data/memory/a.db", "size": 20},
    ])
    by_id = {entry["id"]: entry for entry in totals}
    assert by_id["config"]["bytes"] == 14
    assert by_id["chats"]["bytes"] == 20


def test_component_inventory_reports_media_toggle_sizes(tmp_path: Path):
    paths = _paths(tmp_path)
    paths.CONFIG_FILE.parent.mkdir(parents=True)
    paths.CONFIG_FILE.write_bytes(b"config")
    (paths.WORKSPACE_DIR / "incoming").mkdir(parents=True)
    (paths.WORKSPACE_DIR / "incoming" / "media.bin").write_bytes(b"m" * 123)
    (paths.WORKSPACE_DIR / "notes.txt").write_bytes(b"n" * 7)
    service = BackupService(tmp_path, paths, backup_dir=tmp_path / "backups")
    with_media = service.component_status(include_media=True)
    without_media = service.component_status(include_media=False)
    work_yes = next(item for item in with_media["components"] if item["id"] == "workspace")
    work_no = next(item for item in without_media["components"] if item["id"] == "workspace")
    assert work_yes["bytes"] == 130
    assert work_yes["bytes_with_media"] == 130
    assert work_yes["bytes_without_media"] == 7
    assert work_yes["media_bytes"] == 123
    assert work_no["bytes"] == 7
    assert next(item for item in with_media["components"] if item["id"] == "config")["bytes"] == 6


def test_selective_archive_includes_only_selected_categories(tmp_path: Path):
    paths = _paths(tmp_path)
    paths.CONFIG_FILE.parent.mkdir(parents=True)
    paths.CONFIG_FILE.write_text("config", encoding="utf-8")
    (paths.MEMORY_DIR / "alice").mkdir(parents=True)
    (paths.MEMORY_DIR / "alice" / "chat.db").write_bytes(b"chat")
    service = BackupService(tmp_path, paths, backup_dir=tmp_path / "backups")
    warnings = []
    sources, _private, _key_source = service._collect_sources(
        warnings, True, components={"config"},
    )
    assert [source.arcname for source in sources] == ["data/config.yaml"]
    assert "chat.db" not in " ".join(source.arcname for source in sources)


def test_selective_overlay_preview_and_transactional_rollback(tmp_path: Path, monkeypatch):
    paths = _paths(tmp_path)
    paths.DATA_DIR.mkdir()
    paths.PERSONAS_DIR.mkdir()
    paths.CONFIG_FILE.write_bytes(b"old-config")
    (paths.WORKSPACE_DIR / "old.txt").parent.mkdir(parents=True)
    (paths.WORKSPACE_DIR / "old.txt").write_bytes(b"keep-existing")
    selected = {"config", "workspace"}
    source = _valid_user_archive(
        tmp_path / "source.zip",
        {"data/config.yaml": b"new-config", "data/workspace/new.txt": b"new-file"},
        components=selected,
    )
    rollback = _valid_user_archive(
        tmp_path / "rollback.zip",
        {
            "data/config.yaml": b"old-config",
            "data/workspace/old.txt": b"keep-existing",
        },
        components=selected,
    )
    plan = preview_restore(source, paths, ["config", "workspace"])
    assert plan["confirmation_phrase"] == "恢复备份"
    by_id = {item["id"]: item for item in plan["components"]}
    assert by_id["config"]["archive_bytes"] == len(b"new-config")
    assert by_id["workspace"]["archive_bytes"] == len(b"new-file")
    result = apply_restore(source, paths, ["config", "workspace"])
    assert result["components"]["workspace"]["files"] == 1
    assert paths.CONFIG_FILE.read_bytes() == b"new-config"
    assert (paths.WORKSPACE_DIR / "old.txt").read_bytes() == b"keep-existing"
    assert (paths.WORKSPACE_DIR / "new.txt").read_bytes() == b"new-file"

    import core.selective_restore as restore_module
    real_install = restore_module._install_entry
    calls = {"count": 0}

    def fail_after_first(archive, entry, target, roots):
        calls["count"] += 1
        value = real_install(archive, entry, target, roots)
        if calls["count"] == 2:
            raise OSError("injected write failure")
        return value

    monkeypatch.setattr(restore_module, "_install_entry", fail_after_first)
    with pytest.raises(OSError):
        transactional_restore(source, rollback, paths, ["config", "workspace"])
    assert paths.CONFIG_FILE.read_bytes() == b"old-config"
    assert (paths.WORKSPACE_DIR / "old.txt").read_bytes() == b"keep-existing"
    assert not (paths.WORKSPACE_DIR / "new.txt").exists()


def test_tampered_component_claim_cannot_restore_credentials_as_chats(tmp_path: Path):
    paths = _paths(tmp_path)
    source = _valid_user_archive(
        tmp_path / "tampered-component.zip",
        {"data/secrets.enc": b"secret"},
        components={"credentials"},
    )
    with zipfile.ZipFile(source, "r") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        files = {name: archive.read(name) for name in archive.namelist() if name not in {"manifest.json", "README_RESTORE.txt"}}
    manifest["files"][0]["component"] = "chats"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("README_RESTORE.txt", "restore fixture")
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr("manifest.json", json.dumps(manifest))
    report = verify_backup(source, deep_sqlite=False)
    assert not report["ok"]
    assert any(item["code"] == "manifest_component_mismatch" for item in report["errors"])
    with pytest.raises(RestoreError, match="组件声明"):
        preview_restore(source, paths, ["chats"])


def test_restore_rejects_symlink_ancestor_before_writing(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    real_data = tmp_path / "real-data"
    real_data.mkdir()
    link = tmp_path / "linked-data"
    link.symlink_to(outside, target_is_directory=True)
    paths = _paths(tmp_path)
    paths.DATA_DIR = link
    paths.CONFIG_FILE = link / "config.yaml"
    source = _valid_user_archive(
        tmp_path / "config.zip", {"data/config.yaml": b"must-not-write"}, components={"config"},
    )
    with pytest.raises(RestoreError, match="符号链接"):
        preview_restore(source, paths, ["config"])
    assert not (outside / "config.yaml").exists()
