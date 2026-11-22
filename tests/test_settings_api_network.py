"""设置页「网络」分组的 API 契约测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI

from app_config.schema import RootConfig
from backend.api.settings import router
from backend.settings_spec import field_map


def _root_config() -> RootConfig:
    return RootConfig.model_validate(
        {
            "agents": {"chat": {"provider": "x", "model": "y"}},
            "providers": {
                "x": {"protocol": "openai_compat", "base_url": "http://localhost:1"}
            },
        }
    )


def _app(frontend: str) -> tuple[FastAPI, Any, list[int]]:
    app = FastAPI()
    app.include_router(router)
    saved: list[int] = []
    rt = SimpleNamespace(
        config=_root_config(),
        paths=SimpleNamespace(
            PROJECT_ROOT="/app",
            DATA_DIR="/data",
            CONFIG_FILE="/data/config.yaml",
            LOGS_DIR="/data/logs",
            MEMORY_DIR="/data/memory",
            PERSONAS_DIR="/data/personas",
            WORKSPACE_DIR="/data/workspace",
            EMOJI_DIR="/data/emoji",
            PROVIDER_PRESETS_DIR="/presets",
            USER_PROVIDER_PRESETS_DIR="/user-presets",
        ),
        secrets=SimpleNamespace(list_ids=lambda: []),
        restart_pending=False,
    )
    host = SimpleNamespace(
        rt=rt,
        state="running",
        last_error="",
        save_config=lambda: saved.append(1),
    )
    app.state.host = host
    app.state.frontend = frontend
    app.state.bind_host = "127.0.0.1"
    app.state.bind_source = "default"
    app.state.bind_port = 8000
    app.state.bind_urls = ["http://127.0.0.1:8000"]
    return app, rt, saved


async def _client(app: FastAPI):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def test_tool_profile_is_hidden_and_hot_applied() -> None:
    spec = field_map()["behavior.tool_schema_profile"]
    assert spec["hidden"] is True
    assert spec["needs_restart"] is False
    assert "不需要重启" in spec["help"]


def test_network_field_is_declared_for_webui() -> None:
    spec = field_map()["webui.allow_lan"]
    assert spec["label"] == "允许暴露在局域网中"
    assert spec["needs_restart"] is True
    assert spec["restart_scope"] == "webui"
    assert tuple(spec["frontends"]) == ("webui",)


async def test_webui_sees_network_group_and_listener_info() -> None:
    app, _rt, _saved = _app("webui")
    async with await _client(app) as client:
        response = await client.get("/api/settings")
        assert response.status_code == 200
        body = response.json()
        keys = [group["key"] for group in body["groups"]]
        assert "network" in keys
        assert body["webui"] == {
            "bind_host": "127.0.0.1",
            "bind_source": "default",
            "bind_port": 8000,
            "urls": ["http://127.0.0.1:8000"],
        }


async def test_gui_does_not_see_network_group_and_cannot_patch_it() -> None:
    app, _rt, _saved = _app("gui")
    async with await _client(app) as client:
        response = await client.get("/api/settings")
        assert response.status_code == 200
        keys = [group["key"] for group in response.json()["groups"]]
        assert "network" not in keys

        patched = await client.patch(
            "/api/settings", json={"path": "webui.allow_lan", "value": True}
        )
        assert patched.status_code == 400
        assert "不适用于当前前端" in patched.json()["detail"]


async def test_patch_tool_profile_hot_applies() -> None:
    app, rt, saved = _app("webui")
    async with await _client(app) as client:
        response = await client.patch(
            "/api/settings",
            json={"path": "behavior.tool_schema_profile", "value": "agent"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["value"] == "agent"
        assert body["needs_restart"] is False
        assert body["hot_applied"] is True
        assert saved == [1]
        assert rt.config.behavior.tool_schema_profile == "agent"


async def test_patch_allow_lan_requires_webui_restart_not_runtime_restart() -> None:
    app, rt, saved = _app("webui")
    async with await _client(app) as client:
        response = await client.patch(
            "/api/settings", json={"path": "webui.allow_lan", "value": True}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["value"] is True
        assert body["needs_restart"] is True
        assert body["restart_scope"] == "webui"
        assert "WebUI 服务" in body["message"]
        assert saved == [1]
        assert rt.config.webui.allow_lan is True
        # 只改 WebUI 监听地址不应把 Runtime 标记为待重启。
        assert rt.restart_pending is False
