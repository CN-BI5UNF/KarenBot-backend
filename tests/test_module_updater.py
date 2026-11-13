"""Split backend/frontend module updater tests (GitHub Releases contract)."""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from pathlib import Path

import pytest

from backend.module_updater import (
    MANIFEST_FILENAME,
    ModuleUpdateError,
    ModuleUpdateService,
)


def _tar(module: str, version: str, content: str = "hello\n") -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, data in (
            ("VERSION", version + "\n"),
            (f"{module}/__init__.py", content),
        ):
            payload = data.encode()
            info = tarfile.TarInfo(f"owner-repo-deadbeef/{name}")
            info.size = len(payload)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(payload))
    return raw.getvalue()


class FakeGitHub:
    def __init__(self) -> None:
        self.releases_by_repo: dict[str, list[dict]] = {}
        self.tarballs: dict[tuple[str, str], bytes] = {}

    def releases(self, repo: str) -> list[dict]:
        return list(self.releases_by_repo.get(repo, []))

    def download_tarball(self, repo: str, tag: str, destination: Path) -> None:
        destination.write_bytes(self.tarballs[(repo, tag)])


def _release(tag: str, *, prerelease: bool = False, published: str = "") -> dict:
    return {
        "tag": tag,
        "tag_name": tag,
        "name": tag,
        "body": f"notes for {tag}",
        "prerelease": prerelease,
        "published_at": published,
        "html_url": f"https://example.invalid/{tag}",
        "tarball_url": "",
        "id": tag,
    }


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


def test_backend_only_stable_update_is_available(workspace):
    root, data = workspace
    fake = FakeGitHub()
    fake.releases_by_repo["CN-BI5UNF/KarenBot-backend"] = [
        _release("v0.0.2", published="2026-11-13T00:00:00Z")
    ]
    fake.releases_by_repo["CN-BI5UNF/KarenBot-GUI"] = [
        _release("v0.0.1-dev", published="2026-11-10T00:00:00Z")
    ]
    fake.tarballs[
        ("CN-BI5UNF/KarenBot-backend", "v0.0.2")
    ] = _tar("backend", "0.0.2", "new backend\n")

    service = _service(workspace, fake)
    snapshot = asyncio.run(service.check())
    assert snapshot["available"] is True
    assert [item["component"] for item in snapshot["updates"]] == ["backend"]
    assert snapshot["can_apply"] is True
    assert "backend" in snapshot["confirmation_token"] or snapshot["confirmation_token"]

    result = asyncio.run(
        service.apply(confirmation_token=snapshot["confirmation_token"])
    )
    assert result["backend_updated"] is True
    assert result["frontend_updated"] is False
    assert (root / "backend" / "__init__.py").read_text(encoding="utf-8") == "new backend\n"
    assert (root / "gui" / "app.py").read_text(encoding="utf-8") == "old gui\n"
    manifest = json.loads((root / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["components"]["backend"]["tag"] == "v0.0.2"


def test_prerelease_never_auto_notifies_but_can_be_manually_applied(workspace):
    root, data = workspace
    fake = FakeGitHub()
    fake.releases_by_repo["CN-BI5UNF/KarenBot-backend"] = [
        _release("v0.0.2-dev", prerelease=True, published="2026-11-13T00:00:00Z")
    ]
    fake.releases_by_repo["CN-BI5UNF/KarenBot-GUI"] = [
        _release("v0.0.1-dev", published="2026-11-10T00:00:00Z")
    ]
    fake.tarballs[
        ("CN-BI5UNF/KarenBot-backend", "v0.0.2-dev")
    ] = _tar("backend", "0.0.2-dev", "prerelease backend\n")
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
    assert (
        root / "backend" / "__init__.py"
    ).read_text(encoding="utf-8") == "prerelease backend\n"


def test_both_components_update_and_manifest(workspace):
    root, data = workspace
    fake = FakeGitHub()
    fake.releases_by_repo["CN-BI5UNF/KarenBot-backend"] = [
        _release("v0.0.2", published="2026-11-13T00:00:00Z")
    ]
    fake.releases_by_repo["CN-BI5UNF/KarenBot-GUI"] = [
        _release("v0.0.2", published="2026-11-13T00:00:00Z")
    ]
    fake.tarballs[("CN-BI5UNF/KarenBot-backend", "v0.0.2")] = _tar(
        "backend", "0.0.2", "backend 2\n"
    )
    fake.tarballs[("CN-BI5UNF/KarenBot-GUI", "v0.0.2")] = _tar(
        "gui", "0.0.2", "gui 2\n"
    )
    service = _service(workspace, fake)
    checked = asyncio.run(service.check())
    result = asyncio.run(
        service.apply(confirmation_token=checked["confirmation_token"])
    )
    assert result["backend_updated"] is True
    assert result["frontend_updated"] is True
    assert result["requires_app_restart"] is True
    assert (root / "backend" / "__init__.py").read_text(encoding="utf-8") == "backend 2\n"
    assert (root / "gui" / "__init__.py").read_text(encoding="utf-8") == "gui 2\n"
    assert (root / "backend" / "VERSION").read_text(encoding="utf-8").strip() == "0.0.2"


def test_dirty_development_tree_blocks_apply(workspace, monkeypatch):
    root, data = workspace
    fake = FakeGitHub()
    fake.releases_by_repo["CN-BI5UNF/KarenBot-backend"] = [
        _release("v0.0.2", published="2026-11-13T00:00:00Z")
    ]
    fake.releases_by_repo["CN-BI5UNF/KarenBot-GUI"] = []
    fake.tarballs[("CN-BI5UNF/KarenBot-backend", "v0.0.2")] = _tar(
        "backend", "0.0.2"
    )
    service = _service(workspace, fake)
    monkeypatch.setattr(service, "_development_dirty", lambda: True)
    checked = asyncio.run(service.check())
    assert checked["can_apply"] is False
    assert checked["reason"] == "worktree_dirty"
    with pytest.raises(ModuleUpdateError) as excinfo:
        asyncio.run(service.apply(confirmation_token=checked["confirmation_token"]))
    assert excinfo.value.code == "worktree_dirty"


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
