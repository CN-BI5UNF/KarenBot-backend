"""Setup resubmission must preserve settings and reject invalid writes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app_config.loader import load_config, save_config
from app_config.schema import RootConfig
from backend.api import settings, system
from backend.runtime_host import RuntimeHost
from backend.wizard.flow import next_step, prev_step, progress
from backend.wizard.steps import StepId


def _config() -> RootConfig:
    return RootConfig.model_validate({
        "providers": {"main": {"protocol": "openai_compat", "base_url": "http://localhost:1", "extra_headers": {"X-Test": "keep"}}},
        "agents": {"chat": {"provider": "main", "model": "original", "tool_loop_reminder_interval": 12, "reasoning": {"enabled": False, "budget": "high"}}},
        "adapters": {
            "default": {"type": "napcat", "mode": "server", "access_token_id": "keep_token", "http_api_url": "http://localhost:2", "whitelist": {"mode": "whitelist", "qq_ids": ["123"]}},
            "telegram": {"type": "telegram", "bot_token_id": "tg_token"},
        },
        "app": {"theme": "dark"},
        "webui": {"allow_lan": True},
        "backup": {"directory": "/my/backups"},
        "update": {"check_interval_days": 30},
        "storage": {"retention": {"inbound_media_hours": 72}},
        "behavior": {"rate_limit": {"max_messages": 17}, "context": {"max_context_tokens": 64000, "reserve_output_tokens": 4096}},
    })


def _host(tmp_path, monkeypatch, existing=None):
    config_file = tmp_path / "config.yaml"
    if existing is not None:
        config_file.write_text("existing configuration", encoding="utf-8")
    paths = SimpleNamespace(CONFIG_FILE=config_file, DATA_DIR=tmp_path)
    host = RuntimeHost(tmp_path)
    monkeypatch.setattr(host, "build_paths", lambda: paths)
    secrets = SimpleNamespace(has=lambda key: False, set=Mock())
    monkeypatch.setattr(host, "secrets", lambda: secrets)
    monkeypatch.setattr("app_config.loader.load_config", lambda *args, **kwargs: existing)
    saved = Mock()
    monkeypatch.setattr("app_config.loader.save_config", saved)
    return host, saved, secrets


def test_setup_resubmission_preserves_unshown_configuration(tmp_path, monkeypatch):
    existing = _config()
    before = existing.model_dump()
    host, saved, _secrets = _host(tmp_path, monkeypatch, existing)
    host.write_initial_config({
        "providers": [{"id": "main"}],
        "chat": {"model": "replacement", "temperature": 0.0},
        "adapter": {"enabled": False, "mode": "client", "access_token": ""},
        "behavior": {"max_context_tokens": 128000},
    })
    actual = saved.call_args.args[1]
    for section in ("app", "webui", "backup", "update", "storage", "persona_management"):
        assert getattr(actual, section) == getattr(existing, section)
    assert actual.behavior.rate_limit.max_messages == 17
    assert actual.behavior.context.max_context_tokens == 128000
    assert actual.behavior.context.reserve_output_tokens == 4096
    assert actual.providers["main"].extra_headers == {"X-Test": "keep"}
    assert actual.agents.chat.model == "replacement"
    assert actual.agents.chat.temperature == 0.0
    assert actual.agents.chat.tool_loop_reminder_interval == 12
    assert actual.agents.chat.reasoning.enabled is False
    assert actual.agents.chat.reasoning.budget == "high"
    assert actual.agents.proactive is None
    assert actual.adapters["telegram"] == existing.adapters["telegram"]
    assert actual.adapters["default"].enabled is False
    assert actual.adapters["default"].transport == "ws_client"
    assert actual.adapters["default"].access_token_id == "keep_token"
    assert actual.adapters["default"].http_api_url == "http://localhost:2"
    assert actual.adapters["default"].whitelist.qq_ids == ["123"]
    assert existing.model_dump() == before


def test_setup_resubmission_without_adapter_preserves_all_channels(tmp_path, monkeypatch):
    existing = _config()
    host, saved, _secrets = _host(tmp_path, monkeypatch, existing)
    host.write_initial_config({"providers": [{"id": "main"}]})
    actual = saved.call_args.args[1]
    assert actual.adapters == existing.adapters
    assert actual.agents == existing.agents


def test_explicit_provider_reset_rebinds_omitted_agents_on_disk(tmp_path, monkeypatch):
    host = RuntimeHost(tmp_path)
    monkeypatch.setattr(host, "secrets", lambda: SimpleNamespace(has=lambda key: False, set=Mock()))
    existing = _config()
    existing.agents.proactive = existing.agents.chat.model_copy(deep=True)
    save_config(host.build_paths(), existing)

    host.write_initial_config({
        "replace_providers": True,
        "providers": [{"id": "replacement", "protocol": "openai_compat", "base_url": "http://localhost:2"}],
        "chat": {"provider": "replacement", "model": "new-model"},
    })

    persisted = load_config(host.build_paths(), set_global=False)
    assert set(persisted.providers) == {"replacement"}
    for name in ("proactive", "summary", "persona_gen"):
        agent = getattr(persisted.agents, name)
        assert agent.provider == "replacement"
        assert agent.model == "new-model"
    assert persisted.webui.allow_lan is True
    assert persisted.backup.directory == "/my/backups"


def test_first_setup_can_skip_channel_and_preserves_zero_temperature(tmp_path, monkeypatch):
    host, saved, secrets = _host(tmp_path, monkeypatch)
    result = host.write_initial_config({
        "providers": [{"id": "main", "protocol": "openai_compat", "base_url": "http://localhost:1", "api_key": "test-secret"}],
        "chat": {"provider": "main", "model": "model", "temperature": 0.0},
        "adapter": {"enabled": False},
    })
    actual = saved.call_args.args[1]
    assert result["ok"] is True
    assert actual.adapters["default"].enabled is False
    assert actual.agents.chat.temperature == 0.0
    assert actual.agents.summary is not None
    secrets.set.assert_called_once_with("main_key", "test-secret")


def test_invalid_setup_does_not_write_configuration_or_secrets(tmp_path, monkeypatch):
    host, saved, secrets = _host(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="配置校验"):
        host.write_initial_config({
            "providers": [{"id": "main", "protocol": "openai_compat", "base_url": "http://localhost:1", "api_key": "test-secret"}],
            "chat": {"provider": "main", "model": "model"},
            "agents": {"summary": {"provider": "missing-provider"}},
        })
    saved.assert_not_called()
    secrets.set.assert_not_called()


@pytest.mark.parametrize("path", ["recommended", "custom"])
@pytest.mark.parametrize("source", ["builtin", "create", "import"])
def test_wizard_steps_reverse_and_progress_remains_contiguous(path, source):
    context = {"persona_source": source}
    current = StepId.WELCOME
    route = []
    while current is not None:
        route.append(current)
        following = next_step(current, path, context)
        if following is not None:
            assert prev_step(following, path, context) == current
        current = following
    assert route[-1] == StepId.SUMMARY
    assert (StepId.PERSONA_CREATE in route) == (source == "create")
    assert [progress(step, path, context) for step in route] == [
        (index + 1, len(route)) for index in range(len(route))
    ]


@pytest.mark.parametrize(("path", "value"), [
    ("behavior.rate_limit.enabled", "ture"),
    ("behavior.rate_limit.enabled", None),
    ("behavior.rate_limit.enabled", []),
    ("app.theme", []),
    ("app.theme", {}),
    ("behavior.rate_limit.window_seconds", 1.5),
    ("behavior.rate_limit.window_seconds", True),
    ("features.knowledge.research_queries", 42),
])
async def test_invalid_setting_values_are_400_without_mutation_or_save(path, value):
    cfg = _config()
    before = cfg.model_dump()
    save = Mock()
    app = FastAPI()
    app.include_router(settings.router)
    app.state.host = SimpleNamespace(rt=SimpleNamespace(config=cfg), state="running", last_error="", save_config=save)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        response = await client.patch("/api/settings", json={"path": path, "value": value})
    assert response.status_code == 400, response.text
    assert response.json()["detail"]
    assert cfg.model_dump() == before
    save.assert_not_called()


@pytest.mark.parametrize(("value", "expected"), [(True, True), (False, False), (1, True), (0, False), ("YES", True), ("off", False)])
def test_compatible_boolean_values(value, expected):
    assert settings._coerce(value, {"type": "bool"}) is expected


@pytest.mark.parametrize(("value", "spec"), [
    ("nan", {"type": "float"}),
    ("inf", {"type": "float"}),
    (True, {"type": "float"}),
    ([1.5], {"type": "list_int"}),
    (1, {"type": "list_int"}),
    (9, {"type": "int", "min": 10}),
    (11.0, {"type": "float", "max": 10}),
])
def test_numeric_and_list_errors_are_explained(value, spec):
    with pytest.raises(HTTPException) as error:
        settings._coerce(value, {"label": "测试设置", **spec})
    assert error.value.status_code == 400
    assert error.value.detail.startswith("测试设置：")


@pytest.mark.parametrize(("state", "runtime"), [("running", object()), ("running", None), ("starting", None), ("error", object())])
async def test_setup_rejects_active_runtime_before_writing(tmp_path, state, runtime):
    host = RuntimeHost(tmp_path)
    host.state, host.rt = state, runtime
    host.write_initial_config, host.start = Mock(), AsyncMock()
    app = FastAPI()
    app.include_router(system.router)
    app.state.host = host
    app.state.hub = SimpleNamespace(attach_timeline=Mock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/setup/apply", json={})
    assert response.status_code == 409
    host.write_initial_config.assert_not_called()
    host.start.assert_not_called()


async def test_concurrent_setup_submissions_do_not_overwrite_running_config(tmp_path):
    host = RuntimeHost(tmp_path)
    host.write_initial_config = Mock(return_value={"ok": True})
    entered_start, finish_start = asyncio.Event(), asyncio.Event()

    async def start():
        entered_start.set()
        await finish_start.wait()
        host.state, host.rt = "running", object()
        return True

    host.start = start
    app = FastAPI()
    app.include_router(system.router)
    app.state.host = host
    app.state.hub = SimpleNamespace(attach_timeline=Mock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = asyncio.create_task(client.post("/api/setup/apply", json={}))
        await asyncio.wait_for(entered_start.wait(), 2)
        second = asyncio.create_task(client.post("/api/setup/apply", json={}))
        finish_start.set()
        first_response, second_response = await asyncio.gather(first, second)
    assert first_response.status_code == 200
    assert second_response.status_code == 409
    host.write_initial_config.assert_called_once()


async def test_setup_first_start_reports_start_failure_without_false_success(tmp_path, monkeypatch):
    host, saved, _secrets = _host(tmp_path, monkeypatch)
    host.state = "setup_required"
    host.last_error = "测试启动失败"
    host.start = AsyncMock(return_value=False)
    app = FastAPI()
    app.include_router(system.router)
    app.state.host = host
    app.state.hub = SimpleNamespace(attach_timeline=Mock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/setup/apply", json={
            "providers": [{"id": "main", "protocol": "openai_compat", "base_url": "http://localhost:1"}],
            "chat": {"provider": "main", "model": "model"},
            "adapter": {"enabled": False},
        })
    assert response.status_code == 200
    assert response.json()["started"] is False
    assert response.json()["message"] == "测试启动失败"
    saved.assert_called_once()
