"""Exercise the real async backup and restore workers through HTTP."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi import FastAPI
from test_selective_backup_restore import _paths

from backend.api import backup as api


class Host:
    def __init__(self, root, paths, backup_dir):
        self.project_root = root
        self.paths = paths
        self.runtime = SimpleNamespace(
            project_root=root, paths=paths, secrets=None,
            config=SimpleNamespace(backup=SimpleNamespace(directory=str(backup_dir), include_media=True)),
        )
        self.rt = self.runtime
        self.state = "running"
        self._admin_operation = None
        self.start_calls = 0
        self.fail_starts = 0

    def build_paths(self):
        return self.paths

    async def pause_for_admin(self, operation):
        if self._admin_operation:
            raise RuntimeError("admin busy")
        self._admin_operation = operation
        was_running = self.state == "running"
        await self.stop()
        return was_running

    async def start(self, *, bypass_admin_guard=False):
        assert bypass_admin_guard
        assert self._admin_operation == "backup_restore"
        self.start_calls += 1
        if self.fail_starts:
            self.fail_starts -= 1
            self.state = "error"
            return False
        self.rt = self.runtime
        self.state = "running"
        return True

    async def stop(self):
        self.rt = None
        self.state = "stopped"

    def end_admin_operation(self):
        self._admin_operation = None


@pytest.fixture
def fixture(tmp_path):
    paths = _paths(tmp_path)
    paths.DATA_DIR.mkdir()
    paths.WORKSPACE_DIR.mkdir()
    backup_dir = tmp_path / "backups"
    paths.CONFIG_FILE.write_text(yaml.safe_dump({"backup": {"directory": str(backup_dir)}}))
    (paths.WORKSPACE_DIR / "note.txt").write_text("from backup")
    host = Host(tmp_path, paths, backup_dir)
    app = FastAPI()
    app.state.host = host
    app.include_router(api.router)
    return host, paths, app


async def wait_job(client, job_id, *, restore=False):
    prefix = "/api/backup/restore/jobs/" if restore else "/api/backup/jobs/"
    for _ in range(1000):
        response = await client.get(prefix + job_id)
        assert response.status_code == 200, response.text
        job = response.json()
        if job["state"] in {"done", "partial", "failed", "cancelled"}:
            return job
        await asyncio.sleep(0.005)
    pytest.fail("backup/restore did not finish")


async def create_archive(client):
    response = await client.post("/api/backup", json={"components": ["workspace"]})
    assert response.status_code == 200, response.text
    job = await wait_job(client, response.json()["id"])
    assert job["state"] == "done", job
    return job["filename"]


async def restore_archive(client, filename):
    response = await client.post("/api/backup/restore", json={
        "filename": filename, "components": ["workspace"], "confirmation": "恢复备份",
    })
    assert response.status_code == 200, response.text
    return await wait_job(client, response.json()["id"], restore=True)


async def test_http_restore_progress_and_unselected_files(fixture):
    host, paths, app = fixture
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        filename = await create_archive(client)
        config = paths.CONFIG_FILE.read_bytes()
        (paths.WORKSPACE_DIR / "note.txt").write_text("current")
        (paths.WORKSPACE_DIR / "local-only.txt").write_text("keep")
        job = await restore_archive(client, filename)
        assert job["state"] == "done", job
        assert job["progress"] == len(b"from backup")
        assert job["component_progress"]["workspace"]["bytes"] == len(b"from backup")
        assert job["rollback_filename"] != filename
        assert (paths.WORKSPACE_DIR / "note.txt").read_text() == "from backup"
        assert (paths.WORKSPACE_DIR / "local-only.txt").read_text() == "keep"
        assert paths.CONFIG_FILE.read_bytes() == config
        assert host.start_calls == 1
        assert host._admin_operation is None
        assert (await client.get("/api/backup/status")).json()["active_restore"] is None


async def test_restore_excludes_conflicting_requests_while_runtime_is_stopped(fixture, monkeypatch):
    host, _paths_obj, app = fixture
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        filename = await create_archive(client)
        manager = api.build_manager(host)
        original_wait = manager.wait
        gate = asyncio.Event()

        async def gated_wait(*args, **kwargs):
            await gate.wait()
            return await original_wait(*args, **kwargs)

        monkeypatch.setattr(manager, "wait", gated_wait)
        payload = {"filename": filename, "components": ["workspace"], "confirmation": "恢复备份"}
        response = await client.post("/api/backup/restore", json=payload)
        assert response.status_code == 200, response.text
        assert host.rt is None
        status = await client.get("/api/backup/status")
        assert status.json()["active_restore"]["id"] == response.json()["id"]
        assert (await client.get("/api/backup/components")).status_code == 200
        assert (await client.post("/api/backup", json={"components": ["workspace"]})).status_code == 409
        assert (await client.post("/api/backup/restore", json=payload)).status_code == 409
        gate.set()
        assert (await wait_job(client, response.json()["id"], restore=True))["state"] == "done"


@pytest.mark.parametrize("empty_target", [False, True])
async def test_failed_restore_rolls_back_current_or_empty_category(fixture, monkeypatch, empty_target):
    host, paths, app = fixture
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        filename = await create_archive(client)
        note = paths.WORKSPACE_DIR / "note.txt"
        if empty_target:
            note.unlink()
        else:
            note.write_text("current before restore")
        original_apply = api.apply_restore

        def fail_after_writing(*args, **kwargs):
            result = original_apply(*args, **kwargs)
            if not kwargs.get("rollback"):
                raise OSError("injected restore failure")
            return result

        monkeypatch.setattr(api, "apply_restore", fail_after_writing)
        job = await restore_archive(client, filename)
        assert job["state"] == "failed", job
        assert job["rolled_back"] is True
        if empty_target:
            assert not note.exists()
        else:
            assert note.read_text() == "current before restore"
        assert host.state == "running"
        assert host._admin_operation is None


async def test_restart_failure_rolls_back_before_restarting_original_data(fixture):
    host, paths, app = fixture
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        filename = await create_archive(client)
        (paths.WORKSPACE_DIR / "note.txt").write_text("original current data")
        host.fail_starts = 1
        job = await restore_archive(client, filename)
        assert job["state"] == "failed", job
        assert job["error"]["code"] == "runtime_start_failed"
        assert job["rolled_back"] is True
        assert host.start_calls == 2
        assert host.state == "running"
        assert (paths.WORKSPACE_DIR / "note.txt").read_text() == "original current data"


async def test_stopped_runtime_can_preview_and_restore_without_starting(fixture):
    host, paths, app = fixture
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        filename = await create_archive(client)
        await host.stop()
        host._backup_service_cache = None
        assert (await client.get("/api/backup/status")).status_code == 200
        preview = await client.post("/api/backup/restore/preview", json={
            "filename": filename, "components": ["workspace"],
        })
        assert preview.status_code == 200, preview.text
        (paths.WORKSPACE_DIR / "note.txt").write_text("changed while stopped")
        job = await restore_archive(client, filename)
        assert job["state"] == "done", job
        assert host.state == "stopped"
        assert host.start_calls == 0
        assert (paths.WORKSPACE_DIR / "note.txt").read_text() == "from backup"
