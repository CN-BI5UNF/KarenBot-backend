"""外部程序配置 API 的契约测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI

from app_config.schema import ExternalProgramConfig, RootConfig
from backend.api.programs import router


class FakeManager:
    def __init__(self) -> None:
        self.reconciled: list[dict[str, ExternalProgramConfig]] = []
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.start_result: dict[str, Any] = {
            "id": "sidecar",
            "name": "Sidecar",
            "state": "running",
            "pid": 123,
            "error": "",
            "failure_seq": 0,
        }

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "id": program_id,
                "name": cfg.name or program_id,
                "state": "running",
                "pid": 1,
                "managed": True,
                "error": "",
                "failure_seq": 0,
            }
            for program_id, cfg in self._programs.items()
        ]

    async def reconcile(self, programs):
        self._programs = dict(programs)
        self.reconciled.append(dict(programs))
        return {"started": list(programs), "stopped": [], "errors": {}}

    async def start_one(self, program_id, cfg=None):
        self.started.append(program_id)
        return dict(self.start_result, id=program_id)

    async def stop_one(self, program_id, *, manual=True):
        self.stopped.append(program_id)
        return {"id": program_id, "state": "stopped", "stopped": True}


def _root_config(programs: dict[str, Any] | None = None) -> RootConfig:
    return RootConfig.model_validate(
        {
            "agents": {"chat": {"provider": "x", "model": "y"}},
            "providers": {
                "x": {"protocol": "openai_compat", "base_url": "http://localhost:1"}
            },
            "programs": programs or {},
        }
    )


def _program(**overrides) -> dict[str, Any]:
    data = {
        "name": "Sidecar",
        "enabled": True,
        "executable": "/bin/true",
        "args": [],
        "cwd": "",
        "env": {},
        "stop_with_karen": True,
        "probe_port": 0,
        "auto_restart": False,
        "startup_wait_seconds": 1.0,
    }
    data.update(overrides)
    return data


def _app(*, with_manager: bool = True, state: str = "running"):
    app = FastAPI()
    app.include_router(router)
    saved: list[int] = []
    rt = SimpleNamespace(config=_root_config())
    host = SimpleNamespace(
        rt=rt,
        state=state,
        last_error="",
        save_config=lambda: saved.append(1),
        programs=FakeManager() if with_manager else None,
    )
    if host.programs is not None:
        host.programs._programs = {}
    app.state.host = host
    return app, rt, saved


async def _client(app: FastAPI):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_get_put_and_hot_apply() -> None:
    app, rt, saved = _app()
    async with await _client(app) as client:
        got = await client.get("/api/settings/programs")
        assert got.status_code == 200
        assert got.json()["programs"] == []
        assert "executable" in got.json()["defaults"]

        put = await client.put(
            "/api/settings/programs",
            json={"programs": [{"id": "sidecar", **_program()}]},
        )
        assert put.status_code == 200, put.text
        body = put.json()
        assert body["ok"] is True
        assert body["needs_restart"] is False
        assert body["hot_applied"] is True
        assert body["started"] == ["sidecar"]
        assert rt.config.programs["sidecar"].name == "Sidecar"
        assert saved
        assert app.state.host.programs.reconciled

        status = await client.get("/api/settings/programs")
        assert status.json()["status"][0]["id"] == "sidecar"


async def test_put_accepts_dict_form_and_generates_missing_ids() -> None:
    app, rt, _saved = _app()
    async with await _client(app) as client:
        put = await client.put(
            "/api/settings/programs",
            json={"programs": {"named": _program()}},
        )
        assert put.status_code == 200, put.text
        assert rt.config.programs["named"].executable == "/bin/true"

        generated = await client.put(
            "/api/settings/programs",
            json={"programs": [_program(name="New")]},
        )
        assert generated.status_code == 200, generated.text
        assert len(generated.json()["programs"]) == 1
        assert generated.json()["programs"][0]["id"].startswith("program_")


async def test_put_rejects_bad_ids_duplicates_and_blank_enabled_path() -> None:
    app, _rt, saved = _app()
    async with await _client(app) as client:
        bad_id = await client.put(
            "/api/settings/programs",
            json={"programs": [{"id": "bad id", **_program()}]},
        )
        assert bad_id.status_code == 400
        assert "不合法" in bad_id.text

        duplicate = await client.put(
            "/api/settings/programs",
            json={
                "programs": [
                    {"id": "x", **_program()},
                    {"id": "x", **_program()},
                ]
            },
        )
        assert duplicate.status_code == 400
        assert "重复" in duplicate.text

        blank = await client.put(
            "/api/settings/programs",
            json={"programs": [{"id": "x", **_program(executable="  ")}]},
        )
        assert blank.status_code == 400
        assert "可执行文件为空" in blank.text
        assert not saved

        disabled_blank = await client.put(
            "/api/settings/programs",
            json={
                "programs": [
                    {"id": "x", **_program(executable="", enabled=False)}
                ]
            },
        )
        assert disabled_blank.status_code == 200, disabled_blank.text


async def test_put_requires_programs_field_and_running_runtime() -> None:
    app, _rt, _saved = _app()
    async with await _client(app) as client:
        missing = await client.put("/api/settings/programs", json={})
        assert missing.status_code == 400

    stopped_app, _rt2, _saved2 = _app(state="stopped")
    async with await _client(stopped_app) as client:
        stopped = await client.get("/api/settings/programs")
        assert stopped.status_code == 503


async def test_put_without_manager_reports_needs_restart() -> None:
    app, rt, saved = _app(with_manager=False)
    async with await _client(app) as client:
        put = await client.put(
            "/api/settings/programs",
            json={"programs": [{"id": "sidecar", **_program()}]},
        )
        assert put.status_code == 200, put.text
        body = put.json()
        assert body["hot_applied"] is False
        assert body["needs_restart"] is True
        assert saved
        assert "sidecar" in rt.config.programs


async def test_start_and_stop_endpoints() -> None:
    app, rt, _saved = _app()
    manager = app.state.host.programs
    rt.config.programs["sidecar"] = ExternalProgramConfig(
        name="Sidecar", executable="/bin/true"
    )
    manager._programs = dict(rt.config.programs)
    async with await _client(app) as client:
        started = await client.post("/api/settings/programs/sidecar/start")
        assert started.status_code == 200, started.text
        assert started.json()["started"] is True
        assert manager.started == ["sidecar"]

        manager.start_result = {
            "id": "sidecar",
            "name": "Sidecar",
            "state": "failed",
            "pid": None,
            "error": "找不到命令",
            "failure_seq": 1,
        }
        failed = await client.post("/api/settings/programs/sidecar/start")
        assert failed.status_code == 200, failed.text
        assert failed.json()["started"] is False
        assert "找不到命令" in failed.json()["message"]

        stopped = await client.post("/api/settings/programs/sidecar/stop")
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["stopped"] is True
        assert manager.stopped == ["sidecar"]

        unknown = await client.post("/api/settings/programs/nope/start")
        assert unknown.status_code == 404
        unknown_stop = await client.post("/api/settings/programs/nope/stop")
        assert unknown_stop.status_code == 404


async def test_start_rejects_disabled_program() -> None:
    app, rt, _saved = _app()
    rt.config.programs["sidecar"] = ExternalProgramConfig(
        name="Sidecar", executable="/bin/true", enabled=False
    )
    async with await _client(app) as client:
        response = await client.post("/api/settings/programs/sidecar/start")
        assert response.status_code == 400
        assert "未启用" in response.text
