"""零渠道模式：没有启用的 adapter 时 Runtime 仍可启动。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app_config.schema import BehaviorConfig, RootConfig
from core.proactive_loop import ProactiveLoop
from core.runtime import Runtime


def _config(adapters: dict) -> RootConfig:
    return RootConfig.model_validate(
        {
            "agents": {"chat": {"provider": "x", "model": "y"}},
            "providers": {
                "x": {"protocol": "openai_compat", "base_url": "http://localhost:1"}
            },
            "adapters": adapters,
        }
    )


def test_setup_adapter_allows_disabled_adapter(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path)
    runtime.config = _config({"default": {"type": "napcat", "enabled": False}})
    runtime.secrets = None

    name, cfg = runtime._setup_adapter()

    assert name == "none"
    assert cfg is None
    assert runtime.adapter is None
    assert runtime.adapter_registry is not None


def test_setup_adapter_allows_empty_adapter_map(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path)
    runtime.config = _config({})
    runtime.secrets = None

    name, cfg = runtime._setup_adapter()

    assert name == "none"
    assert cfg is None
    assert runtime.adapter is None


async def test_proactive_loop_skips_without_adapter() -> None:
    class FailingRouter:
        async def should_act(self, *_args, **_kwargs):
            raise AssertionError("没有渠道时不应调用主动路由模型")

    loop = ProactiveLoop(
        pipeline=SimpleNamespace(adapter=None),
        proactive_agent=FailingRouter(),
        behavior_cfg=BehaviorConfig(),
    )
    await loop._maybe_act()
