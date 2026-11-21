"""Unified KarenBot distribution updater tests (GitHub Releases contract)."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

import backend.module_updater as updater_module
from backend.module_updater import (
    DISTRIBUTION_REPO,
    MANIFEST_FILENAME,
    ModuleUpdateError,
    ModuleUpdateService,
)


def _tar_files(files: dict[str, str], *, prefix: str = "owner-repo-deadbeef") -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, data in files.items():
            payload = data.encode()
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(payload)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(payload))
    return raw.getvalue()


def _default_backend(tag: str) -> dict[str, str]:
    version = tag.lstrip("vV")
    return {
        "VERSION": version + "\n",
        "backend/__init__.py": f"backend {tag}\n",
    }


def _default_gui(tag: str) -> dict[str, str]:
    version = tag.lstrip("vV")
    return {
        "VERSION": version + "\n",
        "gui/__init__.py": f"gui {tag}\n",
    }


class FakeGitHub:
    def __init__(self) -> None:
        self.releases_by_repo: dict[str, list[dict]] = {}
        self.assets: dict[str, bytes] = {}
        self.manifest_by_url: dict[str, dict] = {}

    def add_release(
        self,
        tag: str,
        *,
        backend: dict[str, str] | None = None,
        gui: dict[str, str] | None = None,
        webui: dict[str, str] | None = None,
        prerelease: bool = False,
        published: str = "",
        manifest_mutator=None,
    ) -> dict:
        backend = backend if backend is not None else _default_backend(tag)
        gui = gui if gui is not None else _default_gui(tag)
        files_by_component = {
            "backend": backend,
            "gui": gui,
            "webui": webui,
        }
        assets = []
        components = {}
        manifest_url = (
            f"https://api.github.com/repos/{DISTRIBUTION_REPO}/releases/assets/"
            f"manifest-{tag}"
        )
        for component, files in files_by_component.items():
            if files is None:
                continue
            payload = _tar_files(
                files, prefix=f"KarenBot-{component}-{tag}"
            )
            name = updater_module.DISTRIBUTION_ASSET_NAMES[component].format(
                tag=tag
            )
            url = (
                f"https://api.github.com/repos/{DISTRIBUTION_REPO}/releases/"
                f"assets/{component}-{tag}"
            )
            self.assets[url] = payload
            assets.append(
                {
                    "id": len(assets) + 1,
                    "name": name,
                    "size": len(payload),
                    "content_type": "application/gzip",
                    "url": url,
                    "browser_download_url": url,
                }
            )
            components[component] = {
                "repo": updater_module.MODULE_REPOS[component],
                "tag": tag,
                "version": tag.lstrip("vV"),
                "asset": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        assets.append(
            {
                "id": 99,
                "name": updater_module.DISTRIBUTION_MANIFEST,
                "size": 128,
                "content_type": "application/json",
                "url": manifest_url,
                "browser_download_url": manifest_url,
            }
        )
        manifest = {
            "schema": 1,
            "release": tag,
            "components": components,
        }
        if manifest_mutator is not None:
            manifest_mutator(manifest)
        self.manifest_by_url[manifest_url] = manifest
        release = {
            "tag": tag,
            "tag_name": tag,
            "name": tag,
            "body": f"notes for {tag}",
            "prerelease": prerelease,
            "published_at": published,
            "html_url": f"https://example.invalid/{tag}",
            "tarball_url": "",
            "assets": assets,
            "id": tag,
        }
        self.releases_by_repo.setdefault(DISTRIBUTION_REPO, []).append(release)
        return release

    def releases(self, repo: str) -> list[dict]:
        assert repo == DISTRIBUTION_REPO
        return list(self.releases_by_repo.get(repo, []))

    def download_asset(self, asset: dict, destination: Path) -> None:
        destination.write_bytes(self.assets[asset["url"]])

    def download_json_asset(self, asset: dict):
        return json.loads(json.dumps(self.manifest_by_url[asset["url"]]))


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "app"
    (root / "backend").mkdir(parents=True)
    (root / "gui").mkdir()
    (root / "backend" / "__init__.py").write_text("old backend\n", encoding="utf-8")
    (root / "gui" / "app.py").write_text("old gui\n", encoding="utf-8")
    (root / MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "schema": 1,
                "frontend": "gui",
                "components": {
                    "backend": {
                        "repo": "CN-BI5UNF/KarenBot-backend",
                        "dir": "backend",
                        "tag": "v0.0.1-dev",
                        "version": "0.0.1-dev",
                    },
                    "gui": {
                        "repo": "CN-BI5UNF/KarenBot-GUI",
                        "dir": "gui",
                        "tag": "v0.0.1-dev",
                        "version": "0.0.1-dev",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return root, tmp_path / "data"


def _service(workspace, fake: FakeGitHub) -> ModuleUpdateService:
    root, data = workspace
    return ModuleUpdateService(root, data, "gui", client=fake)


def test_stable_release_updates_backend_and_frontend(workspace):
    root, data = workspace
    fake = FakeGitHub()
    fake.add_release(
        "v0.0.2",
        backend={
            "VERSION": "0.0.2\n",
            "backend/__init__.py": "new backend\n",
        },
        gui={
            "VERSION": "0.0.2\n",
            "gui/__init__.py": "new gui\n",
        },
        published="2026-11-13T00:00:00Z",
    )

    service = _service(workspace, fake)
    snapshot = asyncio.run(service.check())
    assert snapshot["available"] is True
    assert snapshot["can_apply"] is True
    assert snapshot["distribution"] is True
    assert [item["component"] for item in snapshot["updates"]] == [
        "backend",
        "gui",
    ]
    assert len(snapshot["distribution_updates"]) == 1
    assert snapshot["distribution_updates"][0]["component"] == "distribution"

    result = asyncio.run(
        service.apply(confirmation_token=snapshot["confirmation_token"])
    )
    assert result["backend_updated"] is True
    assert result["frontend_updated"] is True
    assert result["distribution_updated"] is True
    assert result["distribution_tag"] == "v0.0.2"
    assert (root / "backend" / "__init__.py").read_text(encoding="utf-8") == "new backend\n"
    assert (root / "gui" / "__init__.py").read_text(encoding="utf-8") == "new gui\n"
    manifest = json.loads((root / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["components"]["backend"]["tag"] == "v0.0.2"
    assert manifest["components"]["gui"]["tag"] == "v0.0.2"
    assert manifest["distribution"]["tag"] == "v0.0.2"

    with pytest.raises(ModuleUpdateError) as excinfo:
        asyncio.run(
            service.apply(confirmation_token=snapshot["confirmation_token"])
        )
    assert excinfo.value.code == "confirmation_token_invalid"

    # Re-applying an identical release updates metadata but is not reported as
    # a code update and does not require a restart.
    same = asyncio.run(service.check("distribution:v0.0.2"))
    no_op = asyncio.run(
        service.apply(confirmation_token=same["confirmation_token"])
    )
    assert no_op["updated"] == []
    assert no_op["backend_updated"] is False
    assert no_op["frontend_updated"] is False
    assert no_op["restart_required"] is False


def test_component_alias_also_selects_the_whole_train(workspace):
    _root, _data = workspace
    fake = FakeGitHub()
    fake.add_release("v0.0.2", published="2026-11-13T00:00:00Z")
    service = _service(workspace, fake)

    checked = asyncio.run(service.check("backend:v0.0.2"))
    assert checked["can_apply"] is True
    assert [item["component"] for item in checked["updates"]] == [
        "backend",
        "gui",
    ]
    assert checked["target_tag"] == "distribution:v0.0.2"


def test_prerelease_never_auto_notifies_but_can_be_manually_applied(workspace):
    root, _data = workspace
    fake = FakeGitHub()
    fake.add_release(
        "v0.0.2-dev",
        backend={
            "VERSION": "0.0.2-dev\n",
            "backend/__init__.py": "prerelease backend\n",
        },
        gui={
            "VERSION": "0.0.2-dev\n",
            "gui/__init__.py": "prerelease gui\n",
        },
        prerelease=True,
        published="2026-11-13T00:00:00Z",
    )
    service = _service(workspace, fake)

    checked = asyncio.run(service.check())
    assert checked["available"] is False
    assert checked["updates"] == []
    assert checked["confirmation_token"] == ""

    preview = asyncio.run(service.check("backend:v0.0.2-dev"))
    assert preview["can_apply"] is True
    assert preview["updates"][0]["prerelease"] is True
    assert any("Pre-release" in warning for warning in preview["warnings"])
    with pytest.raises(ModuleUpdateError) as excinfo:
        asyncio.run(
            service.apply(confirmation_token=preview["confirmation_token"])
        )
    assert excinfo.value.code == "prerelease_confirmation_required"

    result = asyncio.run(
        service.apply(
            confirmation_token=preview["confirmation_token"],
            acknowledge_prerelease=True,
        )
    )
    assert result["backend_updated"] is True
    assert result["frontend_updated"] is True
    assert (
        root / "backend" / "__init__.py"
    ).read_text(encoding="utf-8") == "prerelease backend\n"
    assert (
        root / "gui" / "__init__.py"
    ).read_text(encoding="utf-8") == "prerelease gui\n"


def test_existing_file_is_backed_up_before_overwrite(workspace):
    root, _data = workspace
    fake = FakeGitHub()
    fake.add_release(
        "v0.0.2",
        backend={
            "VERSION": "0.0.2\n",
            "backend/__init__.py": "new backend\n",
            "backend/extra.py": "new extra\n",
        },
        published="2026-11-13T00:00:00Z",
    )
    service = _service(workspace, fake)

    checked = asyncio.run(service.check())
    assert checked["can_apply"] is True
    assert any("备份" in warning for warning in checked["warnings"])

    result = asyncio.run(
        service.apply(confirmation_token=checked["confirmation_token"])
    )

    # The update is not blocked: the release file is installed and the local
    # version is preserved in the per-file backup.
    assert (
        root / "backend" / "__init__.py"
    ).read_text(encoding="utf-8") == "new backend\n"
    assert (root / "backend" / "extra.py").read_text(
        encoding="utf-8"
    ) == "new extra\n"
    backup_file = (
        Path(result["backup_dir"]) / "files" / "backend" / "__init__.py"
    )
    assert backup_file.read_text(encoding="utf-8") == "old backend\n"
    assert result["partial"] is False
    assert result["files"]["updated_files"] == 1
    assert result["files"]["new_files"] >= 1
    assert "已备份" in result["message"]


def test_program_file_update_is_backed_up(workspace):
    root, _data = workspace
    fake = FakeGitHub()
    fake.add_release(
        "v0.0.2",
        backend={
            "VERSION": "0.0.2\n",
            "backend/__init__.py": "new backend\n",
        },
        published="2026-11-13T00:00:00Z",
    )
    service = _service(workspace, fake)

    checked = asyncio.run(service.check())
    assert checked["can_apply"] is True

    result = asyncio.run(
        service.apply(confirmation_token=checked["confirmation_token"])
    )
    assert result["backend_updated"] is True
    assert result["partial"] is False
    assert (
        root / "backend" / "__init__.py"
    ).read_text(encoding="utf-8") == "new backend\n"
    backup_file = (
        Path(result["backup_dir"]) / "files" / "backend" / "__init__.py"
    )
    assert backup_file.read_text(encoding="utf-8") == "old backend\n"


def test_single_file_release_applies(workspace):
    root, _data = workspace
    fake = FakeGitHub()
    fake.add_release(
        "v0.0.2",
        backend={"backend/__init__.py": "new backend\n"},
        published="2026-11-13T00:00:00Z",
    )
    service = _service(workspace, fake)
    checked = asyncio.run(service.check())

    result = asyncio.run(
        service.apply(confirmation_token=checked["confirmation_token"])
    )

    assert result["backend_updated"] is True
    assert (
        root / "backend" / "__init__.py"
    ).read_text(encoding="utf-8") == "new backend\n"
    backup_file = (
        Path(result["backup_dir"]) / "files" / "backend" / "__init__.py"
    )
    assert backup_file.read_text(encoding="utf-8") == "old backend\n"


def test_frontend_overlay_backs_up_and_keeps_unrelated_files(workspace):
    root, _data = workspace
    (root / "gui" / "legacy.py").write_text("legacy gui\n", encoding="utf-8")
    fake = FakeGitHub()
    fake.add_release(
        "v0.0.2",
        gui={
            "VERSION": "0.0.2\n",
            "gui/__init__.py": "gui 2\n",
            "gui/extra.py": "extra gui\n",
            "gui/app.py": "release gui\n",
        },
        published="2026-11-13T00:00:00Z",
    )
    service = _service(workspace, fake)

    checked = asyncio.run(service.check())
    result = asyncio.run(
        service.apply(confirmation_token=checked["confirmation_token"])
    )

    assert result["frontend_updated"] is True
    assert result["partial"] is False
    assert (root / "gui" / "app.py").read_text(encoding="utf-8") == "release gui\n"
    assert (root / "gui" / "__init__.py").read_text(encoding="utf-8") == "gui 2\n"
    assert (root / "gui" / "extra.py").read_text(encoding="utf-8") == "extra gui\n"
    assert (root / "gui" / "legacy.py").read_text(encoding="utf-8") == "legacy gui\n"
    backup_file = (
        Path(result["backup_dir"]) / "gui" / "files" / "app.py"
    )
    assert backup_file.read_text(encoding="utf-8") == "old gui\n"
    manifest = json.loads((root / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["components"]["gui"]["tag"] == "v0.0.2"


def test_backend_release_never_writes_user_data(workspace):
    root, _data = workspace
    (root / "data").mkdir()
    (root / "personas").mkdir()
    (root / "plugins").mkdir()
    (root / "data" / "user.db").write_text("user-data\n", encoding="utf-8")
    (root / "personas" / "local.md").write_text("persona\n", encoding="utf-8")
    (root / "plugins" / "local.py").write_text("plugin\n", encoding="utf-8")
    (root / "config.yaml").write_text("secret: true\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=keep\n", encoding="utf-8")
    (root / "secrets.enc").write_text("sealed\n", encoding="utf-8")

    fake = FakeGitHub()
    fake.add_release(
        "v0.0.2",
        backend={
            "VERSION": "0.0.2\n",
            "backend/__init__.py": "new backend\n",
            "karen": "#!/bin/sh\necho karen\n",
            "debata": "#!/bin/sh\necho debata\n",
            "data/user.db": "release-data\n",
            "data/new.db": "new-data\n",
            "personas/local.md": "release persona\n",
            "plugins/local.py": "release plugin\n",
            "config.yaml": "release config\n",
            ".env": "TOKEN=release\n",
            "secrets.enc": "release sealed\n",
            "workspace/file.txt": "release workspace\n",
        },
        published="2026-11-13T00:00:00Z",
    )
    service = _service(workspace, fake)
    checked = asyncio.run(service.check())
    assert checked["worktree"]["available"] is False
    assert any(
        "没有可用的 Git 工作树" in warning
        for warning in checked["warnings"]
    )
    result = asyncio.run(
        service.apply(confirmation_token=checked["confirmation_token"])
    )

    assert result["backend_updated"] is True
    assert (root / "backend" / "__init__.py").read_text(
        encoding="utf-8"
    ) == "new backend\n"
    assert (root / "karen").read_text(encoding="utf-8") == "#!/bin/sh\necho karen\n"
    assert (root / "debata").read_text(
        encoding="utf-8"
    ) == "#!/bin/sh\necho debata\n"
    assert (root / "data" / "user.db").read_text(
        encoding="utf-8"
    ) == "user-data\n"
    assert not (root / "data" / "new.db").exists()
    assert (root / "personas" / "local.md").read_text(
        encoding="utf-8"
    ) == "persona\n"
    assert (root / "plugins" / "local.py").read_text(
        encoding="utf-8"
    ) == "plugin\n"
    assert (root / "config.yaml").read_text(
        encoding="utf-8"
    ) == "secret: true\n"
    assert (root / ".env").read_text(encoding="utf-8") == "TOKEN=keep\n"
    assert (root / "secrets.enc").read_text(
        encoding="utf-8"
    ) == "sealed\n"
    assert not (root / "workspace" / "file.txt").exists()


def test_manifest_hash_mismatch_writes_nothing(workspace):
    root, _data = workspace
    fake = FakeGitHub()
    fake.add_release(
        "v0.0.2",
        backend={
            "VERSION": "0.0.2\n",
            "backend/__init__.py": "new backend\n",
        },
        published="2026-11-13T00:00:00Z",
        manifest_mutator=lambda manifest: manifest["components"]["backend"].update(
            {"sha256": "0" * 64}
        ),
    )
    service = _service(workspace, fake)

    checked = asyncio.run(service.check())
    with pytest.raises(ModuleUpdateError) as excinfo:
        asyncio.run(
            service.apply(confirmation_token=checked["confirmation_token"])
        )

    assert excinfo.value.code == "module_checksum_mismatch"
    assert (
        root / "backend" / "__init__.py"
    ).read_text(encoding="utf-8") == "old backend\n"
    manifest = json.loads((root / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["components"]["backend"]["tag"] == "v0.0.1-dev"


def test_invalid_manifest_is_reported_and_not_selectable(workspace):
    _root, _data = workspace
    fake = FakeGitHub()
    fake.add_release(
        "v0.0.2",
        published="2026-11-13T00:00:00Z",
        manifest_mutator=lambda manifest: manifest.pop("components"),
    )
    service = _service(workspace, fake)

    releases = asyncio.run(service.releases())
    assert releases["distribution_releases"] == []
    assert any(
        "module_manifest_invalid" in value
        for value in releases["errors"].values()
    )
    checked = asyncio.run(service.check())
    assert checked["can_apply"] is False


def test_overlay_failure_rolls_back_every_written_file(workspace, monkeypatch):
    root, _data = workspace
    fake = FakeGitHub()
    fake.add_release(
        "v0.0.2",
        backend={
            "VERSION": "0.0.2\n",
            "backend/__init__.py": "new backend\n",
            "backend/z_fail.py": "fail\n",
        },
        published="2026-11-13T00:00:00Z",
    )
    service = _service(workspace, fake)
    checked = asyncio.run(service.check())

    original_copy2 = updater_module.shutil.copy2

    def flaky_copy2(src, dst, *args, **kwargs):
        if "z_fail.py.karen-update" in str(dst):
            raise OSError("disk full")
        return original_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(updater_module.shutil, "copy2", flaky_copy2)
    with pytest.raises(ModuleUpdateError) as excinfo:
        asyncio.run(
            service.apply(confirmation_token=checked["confirmation_token"])
        )

    assert excinfo.value.code == "module_apply_failed"
    assert (
        root / "backend" / "__init__.py"
    ).read_text(encoding="utf-8") == "old backend\n"
    assert not (root / "VERSION").exists()


def test_symlinked_path_components_abort_and_roll_back(workspace):
    root, _data = workspace
    (root / "data" / "core").mkdir(parents=True)
    try:
        (root / "core").symlink_to(
            root / "data" / "core", target_is_directory=True
        )
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    fake = FakeGitHub()
    fake.add_release(
        "v0.0.2",
        backend={
            "VERSION": "0.0.2\n",
            "backend/__init__.py": "new backend\n",
            "core/x.py": "must not escape\n",
        },
        published="2026-11-13T00:00:00Z",
    )
    service = _service(workspace, fake)
    checked = asyncio.run(service.check())

    with pytest.raises(ModuleUpdateError) as excinfo:
        asyncio.run(
            service.apply(confirmation_token=checked["confirmation_token"])
        )

    assert excinfo.value.code == "unsafe_update_target"
    assert not (root / "data" / "core" / "x.py").exists()
    # The whole update is rolled back instead of partially installing files.
    assert (
        root / "backend" / "__init__.py"
    ).read_text(encoding="utf-8") == "old backend\n"
    assert not (root / "VERSION").exists()
    manifest = json.loads((root / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["components"]["backend"]["tag"] == "v0.0.1-dev"

    # After fixing the unsafe path, the same confirmation can be retried and
    # installs the release completely; there is no permanent partial state.
    (root / "core").unlink()
    result = asyncio.run(
        service.apply(confirmation_token=checked["confirmation_token"])
    )
    assert result["backend_updated"] is True
    assert (root / "core" / "x.py").read_text(
        encoding="utf-8"
    ) == "must not escape\n"
    assert (root / "backend" / "__init__.py").read_text(
        encoding="utf-8"
    ) == "new backend\n"
    manifest = json.loads((root / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["components"]["backend"]["tag"] == "v0.0.2"


def test_rollback_failure_is_reported(workspace, monkeypatch):
    root, _data = workspace
    (root / "data" / "core").mkdir(parents=True)
    try:
        (root / "core").symlink_to(
            root / "data" / "core", target_is_directory=True
        )
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    fake = FakeGitHub()
    fake.add_release(
        "v0.0.2",
        backend={
            "VERSION": "0.0.2\n",
            "backend/__init__.py": "new backend\n",
            "core/x.py": "must not escape\n",
        },
        published="2026-11-13T00:00:00Z",
    )
    service = _service(workspace, fake)
    checked = asyncio.run(service.check())

    original_copy2 = updater_module.shutil.copy2

    def failing_restore(src, dst, *args, **kwargs):
        if "/files/" in str(src):
            raise OSError("restore failed")
        return original_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(updater_module.shutil, "copy2", failing_restore)
    with pytest.raises(ModuleUpdateError) as excinfo:
        asyncio.run(
            service.apply(confirmation_token=checked["confirmation_token"])
        )

    assert excinfo.value.code == "unsafe_update_target"
    assert "回滚失败" in excinfo.value.message
    assert "手动恢复" in excinfo.value.message


def test_worktree_summary_counts_clean_and_dirty_files(workspace):
    root, _data = workspace

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    git("config", "commit.template", "")
    git("add", "-A")
    git("commit", "-qm", "initial")
    (root / "backend" / "__init__.py").write_text("changed\n", encoding="utf-8")
    (root / "untracked.txt").write_text("new\n", encoding="utf-8")

    fake = FakeGitHub()
    fake.add_release(
        "v0.0.2",
        backend={
            "VERSION": "0.0.2\n",
            "backend/__init__.py": "updated\n",
        },
        published="2026-11-13T00:00:00Z",
    )
    service = _service(workspace, fake)
    summary = service._worktree_summary()

    assert summary["available"] is True
    assert summary["tracked_count"] == 3
    assert summary["tracked_dirty_count"] == 1
    assert summary["untracked_count"] == 1
    assert summary["dirty_count"] == 2
    assert summary["clean_count"] == 2
    assert "未改动 2 个" in summary["text"]

    checked = asyncio.run(service.check())
    result = asyncio.run(
        service.apply(confirmation_token=checked["confirmation_token"])
    )
    # The snapshot is taken after the overlay, not before it.
    assert result["worktree"]["dirty"] is True
    assert "未改动" in result["worktree_text"]


def test_git_status_failure_does_not_block_update(workspace, monkeypatch):
    fake = FakeGitHub()
    fake.add_release("v0.0.2", published="2026-11-13T00:00:00Z")
    service = _service(workspace, fake)
    monkeypatch.setattr(
        service,
        "_worktree_summary",
        lambda: {
            "available": True,
            "dirty": True,
            "dirty_count": 0,
            "tracked_dirty_count": 0,
            "untracked_count": 0,
            "clean_count": 0,
            "tracked_count": 0,
            "clean_percent": 0.0,
            "error": "git_status_failed",
            "text": "无法读取 Git 工作树状态",
        },
    )

    checked = asyncio.run(service.check())

    assert checked["can_apply"] is True
    assert any("无法读取 Git 工作树状态" in w for w in checked["warnings"])
    assert not any("0 个改动/未跟踪文件" in w for w in checked["warnings"])


def test_webui_service_only_writes_the_active_frontend(tmp_path):
    root = tmp_path / "app"
    (root / "backend").mkdir(parents=True)
    (root / "webui").mkdir()
    (root / "backend" / "__init__.py").write_text(
        "old backend\n", encoding="utf-8"
    )
    (root / "webui" / "app.js").write_text("old webui\n", encoding="utf-8")
    (root / MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "schema": 1,
                "frontend": "webui",
                "components": {
                    "backend": {
                        "repo": "CN-BI5UNF/KarenBot-backend",
                        "dir": "backend",
                        "tag": "v0.0.1-dev",
                        "version": "0.0.1-dev",
                    },
                    "webui": {
                        "repo": "CN-BI5UNF/KarenBot-WebUI",
                        "dir": "webui",
                        "tag": "v0.0.1-dev",
                        "version": "0.0.1-dev",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    fake = FakeGitHub()
    fake.add_release(
        "v0.0.2",
        backend={
            "VERSION": "0.0.2\n",
            "backend/__init__.py": "new backend\n",
        },
        gui=_default_gui("v0.0.2"),
        webui={
            "VERSION": "0.0.2\n",
            "webui/__init__.py": "new webui\n",
        },
        published="2026-11-13T00:00:00Z",
    )
    service = ModuleUpdateService(
        root, tmp_path / "data", "webui", client=fake
    )
    checked = asyncio.run(service.check())
    result = asyncio.run(
        service.apply(confirmation_token=checked["confirmation_token"])
    )
    assert result["backend_updated"] is True
    assert result["frontend_updated"] is True
    assert (root / "backend" / "__init__.py").read_text(
        encoding="utf-8"
    ) == "new backend\n"
    assert (root / "webui" / "__init__.py").read_text(
        encoding="utf-8"
    ) == "new webui\n"
    assert not (root / "gui").exists()
    manifest = json.loads((root / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["frontend"] == "webui"
    assert manifest["components"]["webui"]["tag"] == "v0.0.2"


def test_api_builds_module_service_from_frontend(workspace):
    from types import SimpleNamespace

    from backend.api.update import build_module_service, build_service

    root, data = workspace
    host = SimpleNamespace(
        project_root=root,
        frontend="gui",
        rt=None,
        build_paths=lambda: SimpleNamespace(DATA_DIR=data),
    )
    service = build_module_service(host)
    assert isinstance(service, ModuleUpdateService)
    assert build_module_service(host) is service
    assert build_service(host) is service
