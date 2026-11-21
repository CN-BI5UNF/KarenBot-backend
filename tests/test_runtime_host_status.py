"""RuntimeHost.status 对非 OneBot 配置的兼容性回归。"""

from __future__ import annotations

from types import SimpleNamespace

from app_config.schema import RootConfig, TelegramAdapterConfig
from backend.runtime_host import RuntimeHost


class FakePrograms:
    def __init__(self) -> None:
        self.stopped = False

    def status(self) -> list[dict]:
        return [
            {
                "id": "sidecar",
                "name": "Sidecar",
                "state": "failed",
                "failure_seq": 2,
                "pid": None,
            }
        ]

    async def stop_all(self) -> None:
        self.stopped = True


def test_status_handles_telegram_adapter(tmp_path) -> None:
    cfg = RootConfig.model_validate(
        {
            "agents": {"chat": {"provider": "x", "model": "y"}},
            "providers": {
                "x": {"protocol": "openai_compat", "base_url": "http://localhost:1"}
            },
            "adapters": {
                "tg": TelegramAdapterConfig(bot_token_id="tg_token").model_dump()
            },
        }
    )
    host = RuntimeHost(project_root=tmp_path)
    host.rt = SimpleNamespace(
        config=cfg,
        persona=SimpleNamespace(name="测试人格"),
        adapter=SimpleNamespace(is_connected=True),
    )
    info = host.status()
    assert info["adapter"]["name"] == "tg"
    assert info["adapter"]["type"] == "telegram"
    assert info["adapter_connected"] is True
    assert info["persona"] == "测试人格"
    assert info["programs"] == []


def test_status_reports_external_programs(tmp_path) -> None:
    host = RuntimeHost(project_root=tmp_path)
    host.programs = FakePrograms()

    info = host.status()

    assert info["programs"][0]["id"] == "sidecar"
    assert info["programs"][0]["failure_seq"] == 2


async def test_stop_cleans_programs_even_without_runtime(tmp_path) -> None:
    host = RuntimeHost(project_root=tmp_path)
    host.programs = FakePrograms()

    await host.stop()

    assert host.programs.stopped is True


async def test_stop_stops_programs_before_runtime_shutdown(tmp_path) -> None:
    order: list[str] = []

    class FakeRuntime:
        async def shutdown(self) -> None:
            order.append("runtime")

    class OrderedPrograms(FakePrograms):
        async def stop_all(self) -> None:
            order.append("programs")
            await super().stop_all()

    host = RuntimeHost(project_root=tmp_path)
    host.rt = FakeRuntime()
    host.programs = OrderedPrograms()

    await host.stop()

    assert order == ["programs", "runtime"]
