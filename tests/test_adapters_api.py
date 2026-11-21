"""新增 /api/adapters/config 与 PUT /api/adapters/{name} 的契约测试。"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from app_config.schema import NapCatAdapterConfig, RootConfig, TelegramAdapterConfig
from backend.api.adapters import router


def _root_config() -> RootConfig:
    return RootConfig.model_validate(
        {
            "agents": {"chat": {"provider": "x", "model": "y"}},
            "providers": {
                "x": {"protocol": "openai_compat", "base_url": "http://localhost:1"}
            },
            "adapters": {"default": NapCatAdapterConfig().model_dump()},
        }
    )


def _app():
    app = FastAPI()
    app.include_router(router)
    saved: list[int] = []

    async def apply_live_settings(paths):
        return {"hot_applied": True, "rebuilt": ["adapter"], "errors": []}

    rt = SimpleNamespace(
        config=_root_config(),
        apply_live_settings=apply_live_settings,
    )
    host = SimpleNamespace(
        rt=rt,
        state="running",
        save_config=lambda: saved.append(1),
    )
    app.state.host = host
    return app, rt, saved


async def test_get_and_put_adapter_config() -> None:
    app, rt, saved = _app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        got = await client.get("/api/adapters/config")
        assert got.status_code == 200
        body = got.json()
        assert body["adapters"]["default"]["type"] == "napcat"

        payload = TelegramAdapterConfig(bot_token_id="tg_token").model_dump()
        put = await client.put("/api/adapters/default", json=payload)
        assert put.status_code == 200, put.text
        result = put.json()
        assert result["ok"] is True
        assert result["hot_applied"] is True
        assert rt.config.adapters["default"].type == "telegram"
        assert saved


async def test_put_adapter_config_rejects_unknown_name_and_bad_type() -> None:
    app, _rt, _saved = _app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        unknown = await client.put(
            "/api/adapters/other",
            json=TelegramAdapterConfig(bot_token_id="tg").model_dump(),
        )
        assert unknown.status_code == 400

        bad = await client.put(
            "/api/adapters/default",
            json={"type": "telegram", "transport": "nope"},
        )
        assert bad.status_code == 400


async def test_put_adapter_config_rejects_missing_secret_before_save() -> None:
    app, rt, saved = _app()
    rt.secrets = SimpleNamespace(get=lambda _key: None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        payload = TelegramAdapterConfig(bot_token_id="tg_missing").model_dump()
        response = await client.put("/api/adapters/default", json=payload)
    assert response.status_code == 400
    assert not saved
