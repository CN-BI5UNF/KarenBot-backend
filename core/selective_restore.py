"""Verified, component-scoped overlay restore for user-export archives.

The archive is always deeply verified before planning or writing. Restore is an
overlay: it replaces archived paths and adds missing paths, but leaves current
paths absent from the archive untouched (important for partial/media-omitted
backups). A caller must create a rollback user-export before applying a plan.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from core.backup import (
    BACKUP_COMPONENTS,
    COMPONENT_CONFIG_FILES,
    _backup_component_for_path,
    _format_bytes,
    _validate_component_selection,
)
from core.restore import RestoreError, verify_backup

CONFIRMATION_PHRASE = "恢复备份"
CHUNK_SIZE = 1024 * 1024


def _target_for_member(name: str, paths: Any) -> Path:
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise RestoreError("unsafe_member", f"归档路径不安全：{name}")
    if name.startswith("personas/"):
        suffix = PurePosixPath(name).parts[1:]
        if not suffix:
            raise RestoreError("unsafe_member", f"人格路径无效：{name}")
        return Path(paths.PERSONAS_DIR).joinpath(*suffix)
    if not name.startswith("data/"):
        raise RestoreError("unsafe_member", f"归档路径不在数据目录内：{name}")
    suffix = PurePosixPath(name).parts[1:]
    if not suffix:
        raise RestoreError("unsafe_member", f"数据路径无效：{name}")
    component = _backup_component_for_path(name)
    permitted = {
        "config": len(suffix) == 1 and suffix[0] in {
            "config.yaml", "token_calibration.json", "persona_brief.json",
            "slang.json", "knowledge.json", "knowledge_state.json",
        },
        "credentials": len(suffix) == 1 and suffix[0] in {
            "secrets.enc", "secrets.meta", "rsa_public.pem", "rsa_private.pem",
        },
        "chats": suffix[0] == "memory" and len(suffix) > 1,
        "rag": suffix[0] == "vector" and len(suffix) > 1,
        "workspace": suffix[0] == "workspace" and len(suffix) > 1,
        "emoji": suffix[0] == "emoji" and len(suffix) > 1,
        "providers": suffix[:2] == ("providers", "presets") and len(suffix) > 2,
    }.get(component, False)
    if not permitted:
        raise RestoreError("unsafe_member", f"归档成员不在可恢复组件内：{name}")
    if suffix[0] == "config.yaml":
        if len(suffix) != 1:
            raise RestoreError("unsafe_member", f"主配置路径无效：{name}")
        return Path(paths.CONFIG_FILE)
    return Path(paths.DATA_DIR).joinpath(*suffix)


def _component_label_map() -> dict[str, str]:
    return dict(BACKUP_COMPONENTS)


def _selected_entries(archive: zipfile.ZipFile, manifest: dict, selected: set[str]) -> list[dict]:
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise RestoreError("manifest_invalid", "manifest.files 不是列表")
    output = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RestoreError("manifest_invalid", "manifest 条目无效")
        name = entry.get("path")
        if not isinstance(name, str) or name not in archive.namelist():
            raise RestoreError("manifest_invalid", "manifest 引用的归档成员不存在")
        component = _backup_component_for_path(name)
        declared = entry.get("component")
        if declared is not None and declared != component:
            raise RestoreError(
                "manifest_component_mismatch",
                f"归档成员组件声明与路径不匹配：{name}",
            )
        if component in selected:
            output.append({**entry, "component": component})
    return output


def _reject_symlink_ancestors(target: Path, allowed_roots: list[Path]) -> None:
    absolute = target.absolute()
    root = next((candidate.absolute() for candidate in allowed_roots if absolute == candidate.absolute() or candidate.absolute() in absolute.parents), None)
    if root is None:
        raise RestoreError("unsafe_destination", f"恢复目标超出受信目录：{target}")
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise RestoreError("unsafe_destination", f"恢复根路径经过符号链接：{current}")
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise RestoreError("unsafe_destination", f"恢复目标超出受信目录：{target}") from exc
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise RestoreError("unsafe_destination", f"恢复路径经过符号链接：{current}")
    if target.is_symlink():
        raise RestoreError("unsafe_destination", f"恢复目标是符号链接：{target}")


def _tree_stats(path: Path) -> tuple[int, int]:
    if not path.exists():
        return (0, 0)
    if path.is_symlink():
        raise RestoreError("unsafe_destination", f"恢复目标目录是符号链接：{path}")
    if path.is_file():
        return (path.stat().st_size, 1)
    total = count = 0
    for current, dirs, files in os.walk(path, followlinks=False):
        base = Path(current)
        if base.is_symlink():
            raise RestoreError("unsafe_destination", f"恢复路径经过符号链接：{base}")
        dirs[:] = [name for name in dirs if not (base / name).is_symlink()]
        for name in files:
            item = base / name
            if item.is_symlink():
                raise RestoreError("unsafe_destination", f"恢复目标含符号链接：{item}")
            if item.is_file():
                total += item.stat().st_size
                count += 1
    return total, count


def preview_restore(
    path: Path, paths: Any, components: Any = None, *, rollback: bool = False,
) -> dict[str, Any]:
    selected = _validate_component_selection(components)
    verification = verify_backup(Path(path), deep_sqlite=True)
    if not verification.get("ok"):
        errors = verification.get("errors") or []
        candidate = errors[0] if errors else None
        first = candidate if isinstance(candidate, dict) else {}
        code = str(first.get("code") or "backup_verify_failed")
        message = str(first.get("message") or "备份完整性验证未通过")
        raise RestoreError(code, message)
    if verification.get("backup_type") != "user_export":
        raise RestoreError("restore_type_unsupported", "仅支持恢复用户数据导出包")
    if not rollback and selected & {"chats", "rag"}:
        from core.compat import read_local_state

        local = read_local_state(Path(paths.DATA_DIR), config_path=Path(paths.CONFIG_FILE))
        if local.data_contract_epoch or local.migration_set_id or verification.get("ledger_detected"):
            raise RestoreError(
                "managed_restore_requires_controller",
                "带迁移或删除账本的数据需要维护控制器执行恢复，不能直接覆盖；请取消聊天/RAG类别。",
            )
    allowed_roots = [Path(paths.DATA_DIR), Path(paths.PERSONAS_DIR)]
    if Path(paths.CONFIG_FILE).absolute() not in Path(paths.DATA_DIR).absolute().parents:
        allowed_roots.append(Path(paths.CONFIG_FILE).parent)
    labels = _component_label_map()
    component_data = {
        component_id: {
            "id": component_id, "label": labels[component_id],
            "archive_bytes": 0, "files": 0,
            "target_bytes": 0, "target_files": 0,
            "overwrite_files": 0, "new_files": 0, "delete_files": 0,
            "available": False, "restorable": True,
        }
        for component_id in sorted(selected)
    }
    with zipfile.ZipFile(path, "r") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        entries = _selected_entries(archive, manifest, selected)
        for entry in entries:
            component_id = entry["component"]
            item = component_data[component_id]
            target = _target_for_member(entry["path"], paths)
            _reject_symlink_ancestors(target, allowed_roots)
            size = max(0, int(entry.get("size") or 0))
            item["archive_bytes"] += size
            item["files"] += 1
            if target.is_file():
                item["target_bytes"] += target.stat().st_size
                item["target_files"] += 1
                item["overwrite_files"] += 1
            else:
                item["new_files"] += 1
        # Report current size even where the archive is partial/omits files.
        targets = {
            "config": [
                Path(paths.CONFIG_FILE) if name == "config.yaml" else Path(paths.DATA_DIR) / name
                for name in COMPONENT_CONFIG_FILES
            ],
            "credentials": [Path(paths.SECRETS_FILE), Path(paths.SECRETS_META_FILE), Path(paths.RSA_PUBLIC_KEY_FILE), Path(paths.RSA_PRIVATE_KEY_FILE)],
            "chats": [Path(paths.MEMORY_DIR)],
            "rag": [Path(paths.VECTOR_DIR)],
            "workspace": [Path(paths.WORKSPACE_DIR)],
            "emoji": [Path(paths.EMOJI_DIR)],
            "providers": [Path(paths.USER_PROVIDER_PRESETS_DIR)],
            "personas": [Path(paths.PERSONAS_DIR)],
        }
        for component_id, roots in targets.items():
            if component_id not in component_data:
                continue
            item = component_data[component_id]
            archive_names = {entry["path"] for entry in entries if entry["component"] == component_id}
            target_bytes = 0
            target_files = 0
            for root in roots:
                size, count = _tree_stats(root)
                target_bytes += size
                target_files += count
            item["target_bytes"] = target_bytes
            item["target_files"] = target_files
            item["overwrite_files"] = sum(
                1 for entry in entries
                if entry["component"] == component_id
                and _target_for_member(entry["path"], paths).is_file()
            )
            item["new_files"] = max(0, item["files"] - item["overwrite_files"])
            item["available"] = bool(archive_names)
            item["restorable"] = bool(archive_names)
            if component_id == "credentials":
                item["restorable"] = bool(
                    archive_names
                    and verification.get("credential_restorable")
                    and {"data/secrets.enc", "data/secrets.meta", "data/rsa_public.pem", "data/rsa_private.pem"}.issubset(archive_names)
                )
            if component_id in {"chats", "rag"} and item["files"]:
                item["restorable"] = all(
                    bool(entry.get("consistent", True))
                    for entry in entries if entry["component"] == component_id
                )
    if not rollback and not any(item["available"] for item in component_data.values()):
        raise RestoreError("no_selected_data", "所选组件在备份中没有可恢复内容")
    unavailable = [item["label"] for item in component_data.values() if not item["available"]]
    if not rollback and unavailable:
        raise RestoreError(
            "component_unavailable",
            "所选内容未包含在该备份中：" + "、".join(unavailable),
        )
    warnings = list(verification.get("warnings") or [])
    if verification.get("partial"):
        warnings.append("该备份在创建时被标记为部分备份；当前按归档成员覆盖/新增，不会删除未收录文件。")
    if "credentials" in selected and not verification.get("credential_restorable"):
        warnings.append("该备份凭据密钥材料不可验证；选择凭据可能导致原密钥无法解密。")
    output = list(component_data.values())
    total_archive = sum(item["archive_bytes"] for item in output)
    total_target = sum(item["target_bytes"] for item in output)
    return {
        "ok": True, "filename": Path(path).name,
        "confirmation_phrase": CONFIRMATION_PHRASE,
        "components": [
            item | {
                "formatted_archive_bytes": _format_bytes(item["archive_bytes"]),
                "formatted_target_bytes": _format_bytes(item["target_bytes"]),
            }
            for item in output
        ],
        "total_archive_bytes": total_archive,
        "total_target_bytes": total_target,
        "warnings": warnings,
        "backup_partial": bool(verification.get("partial")),
        "backup_sha256": verification.get("sha256", ""),
    }


def _install_entry(archive: zipfile.ZipFile, entry: dict[str, Any], target: Path, allowed_roots: list[Path]) -> bool:
    """Atomically install one verified regular-file member; return whether new."""
    _reject_symlink_ancestors(target, allowed_roots)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink_ancestors(target, allowed_roots)
    existing = target.is_file()
    if target.exists() and not existing:
        raise RestoreError("unsafe_destination", f"目标不是普通文件：{target}")
    previous_mode = stat.S_IMODE(target.stat().st_mode) if existing else None
    archive_mode = stat.S_IMODE(archive.getinfo(entry["path"]).external_attr >> 16)
    private = _backup_component_for_path(entry["path"]) == "credentials"
    mode = 0o600 if private else (previous_mode if previous_mode is not None else (archive_mode & 0o666 or 0o600))
    fd, tmp_name = tempfile.mkstemp(prefix=".karen-restore-", dir=target.parent)
    tmp = Path(tmp_name)
    digest = hashlib.sha256()
    written = 0
    try:
        with os.fdopen(fd, "wb") as out, archive.open(entry["path"], "r") as src:
            while True:
                chunk = src.read(CHUNK_SIZE)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                written += len(chunk)
            out.flush()
            os.fsync(out.fileno())
        if written != int(entry["size"]) or digest.hexdigest() != entry["sha256"]:
            raise RestoreError("member_changed", f"归档成员校验不一致：{entry['path']}")
        if entry.get("kind") == "sqlite":
            if existing:
                try:
                    connection = sqlite3.connect(target, timeout=0)
                    try:
                        busy, *_rest = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                        if busy:
                            raise RestoreError("database_busy", f"数据库仍被占用：{target.name}")
                    finally:
                        connection.close()
                except sqlite3.Error as exc:
                    raise RestoreError("database_busy", f"数据库无法安全停止：{target.name}") from exc
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = Path(str(target) + suffix)
                _reject_symlink_ancestors(sidecar, allowed_roots)
                sidecar.unlink(missing_ok=True)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
        try:
            dfd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
        return not existing
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def apply_restore(
    path: Path,
    paths: Any,
    components: Any = None,
    *,
    progress: Callable[[str, int, int], None] | None = None,
    rollback: bool = False,
) -> dict[str, Any]:
    """Apply a previewed selection. Caller creates rollback and stops Runtime."""
    plan = preview_restore(path, paths, components, rollback=rollback)
    if not rollback and not all(item["restorable"] for item in plan["components"]):
        raise RestoreError("component_not_restorable", "所选内容未通过安全恢复检查")
    selected = _validate_component_selection(components)
    allowed_roots = [Path(paths.DATA_DIR), Path(paths.PERSONAS_DIR)]
    if Path(paths.CONFIG_FILE).absolute() not in Path(paths.DATA_DIR).absolute().parents:
        allowed_roots.append(Path(paths.CONFIG_FILE).parent)
    installed_new: list[Path] = []
    restored = {component_id: {"files": 0, "bytes": 0} for component_id in selected}
    try:
        with zipfile.ZipFile(path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json"))
            for entry in _selected_entries(archive, manifest, selected):
                target = _target_for_member(entry["path"], paths)
                was_new = _install_entry(archive, entry, target, allowed_roots)
                if was_new:
                    installed_new.append(target)
                restored[entry["component"]]["files"] += 1
                restored[entry["component"]]["bytes"] += int(entry["size"])
                if progress is not None:
                    progress(entry["component"], int(entry["size"]), restored[entry["component"]]["files"])
    except BaseException:
        # The caller's verified pre-restore archive puts overwritten files back;
        # these are the only paths absent from that rollback archive.
        for target in reversed(installed_new):
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return {"filename": Path(path).name, "components": restored, "warnings": plan["warnings"]}


def remove_files_not_in_rollback(
    source_path: Path,
    rollback_path: Path,
    paths: Any,
    components: Any,
) -> list[str]:
    """Remove source entries absent before restore; rollback archive owns old files."""
    selected = _validate_component_selection(components)
    with zipfile.ZipFile(source_path) as source_zip, zipfile.ZipFile(rollback_path) as rollback_zip:
        source_manifest = json.loads(source_zip.read("manifest.json"))
        rollback_manifest = json.loads(rollback_zip.read("manifest.json"))
        source_entries = _selected_entries(source_zip, source_manifest, selected)
        rollback_entries = _selected_entries(rollback_zip, rollback_manifest, selected)
        old_names = {entry["path"] for entry in rollback_entries}
    roots = [Path(paths.DATA_DIR), Path(paths.PERSONAS_DIR)]
    if Path(paths.CONFIG_FILE).absolute() not in Path(paths.DATA_DIR).absolute().parents:
        roots.append(Path(paths.CONFIG_FILE).parent)
    removed: list[str] = []
    for entry in source_entries:
        if entry["path"] in old_names:
            continue
        target = _target_for_member(entry["path"], paths)
        _reject_symlink_ancestors(target, roots)
        try:
            target.unlink(missing_ok=True)
            removed.append(entry["path"])
        except OSError as exc:
            raise RestoreError("rollback_cleanup_failed", f"回滚新增文件失败：{entry['path']}: {exc}") from exc
    return removed


def transactional_restore(
    source_path: Path,
    rollback_path: Path,
    paths: Any,
    components: Any,
    *,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Apply an archive and restore selected current data if any write fails."""
    selected = _validate_component_selection(components)
    try:
        return apply_restore(source_path, paths, selected, progress=progress)
    except BaseException as restore_error:
        try:
            remove_files_not_in_rollback(source_path, rollback_path, paths, selected)
            apply_restore(rollback_path, paths, selected, rollback=True)
        except BaseException as rollback_error:
            raise RestoreError(
                "rollback_failed",
                f"恢复失败，且自动回滚失败：{type(rollback_error).__name__}: {rollback_error}",
            ) from restore_error
        raise
