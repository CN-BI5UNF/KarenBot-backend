"""登录 Cookie 的 Secure 条件测试。"""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from backend.api.system import router
from backend.auth import TokenAuth


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.auth = TokenAuth("test-token", enabled=True, source="test")
    return app


async def _login(base_url: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
        return await client.post(
            "/api/auth/login", json={"token": "test-token"}
        )


async def test_http_login_cookie_is_not_secure() -> None:
    response = await _login("http://test")
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" not in cookie


async def test_https_login_cookie_is_secure() -> None:
    response = await _login("https://test")
    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]


async def test_env_flag_forces_secure_cookie(monkeypatch) -> None:
    monkeypatch.setenv("KAREN_WEBUI_SECURE_COOKIE", "1")
    response = await _login("http://test")
    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]
