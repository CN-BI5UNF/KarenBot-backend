"""执行器包装层必须保留并发安全标记。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from agents.runner import AgentRunner
from app_config.schema import AgentConfig, RootConfig
from core.message_pipeline import MessagePipeline
from core.pipeline_agent_tasks import _wrap_agent_task_executor
from core.pipeline_turns import _wrap_turn_executor
from providers.base import ToolCall
from tools import ToolContext, ToolRegistry, ToolSpec, build_default_registry
from tools.base import copy_executor_metadata


class _EmptyArgs(BaseModel):
    pass


def _read_func(name: str, state: dict[str, int]):
    async def func(args: Any, ctx: ToolContext) -> dict[str, Any]:
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(0.05)
        state["active"] -= 1
        return {"ok": True, "tool": name}

    return func


def _make_registry(state: dict[str, int]) -> ToolRegistry:
    specs = [
        ToolSpec(
            name="read_a",
            description="read_a",
            args_model=_EmptyArgs,
            func=_read_func("read_a", state),
            concurrency_safe=True,
        ),
        ToolSpec(
            name="read_b",
            description="read_b",
            args_model=_EmptyArgs,
            func=_read_func("read_b", state),
            concurrency_safe=True,
        ),
    ]
    return ToolRegistry(specs)


def _config() -> RootConfig:
    cfg = RootConfig.model_construct()
    cfg.behavior = cfg.behavior.model_copy(update={"tool_schema_profile": "agent"})
    return cfg


def test_registry_exposes_concurrency_safe_names() -> None:
    registry = build_default_registry(_config())
    names = registry.concurrency_safe_names()
    assert "read_file" in names
    assert "agent_read" in names
    assert "send_group_message" not in names


def test_copy_executor_metadata_preserves_attribute() -> None:
    async def inner(name: str, args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    async def wrapper(name: str, args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return await inner(name, args, **kwargs)

    inner.concurrency_safe_names = frozenset({"read_file"})  # type: ignore[attr-defined]
    wrapped = copy_executor_metadata(wrapper, inner)
    assert wrapped is wrapper
    assert wrapper.concurrency_safe_names == frozenset({"read_file"})  # type: ignore[attr-defined]


def test_dynamic_executor_resolves_safe_names_live() -> None:
    first = build_default_registry(_config())
    second = ToolRegistry([])

    class _Pipeline:
        def __init__(self) -> None:
            self.tool_registry = first
            self._tool_profile_grace_registry = None

    pipeline = _Pipeline()
    wrapper = MessagePipeline._dynamic_tool_executor(pipeline, ToolContext())
    assert "read_file" in wrapper.concurrency_safe_names  # type: ignore[attr-defined]

    pipeline.tool_registry = second
    assert "read_file" not in wrapper.concurrency_safe_names  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_turn_executor_wrappers_preserve_metadata_and_deny() -> None:
    calls: list[tuple[str, str | None]] = []

    async def base(
        name: str,
        args: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        calls.append((name, tool_call_id))
        return {"ok": True, "name": name}

    base.concurrency_safe_names = frozenset({"read_a"})  # type: ignore[attr-defined]
    wrapped = _wrap_turn_executor(
        base,
        denied_tools={"write_x"},
        ledger=None,
        persona_id="persona",
        current_turn_id="turn",
    )

    assert wrapped.concurrency_safe_names == frozenset({"read_a"})  # type: ignore[attr-defined]
    denied = await wrapped("write_x", {})
    assert denied["status"] == "denied"
    allowed = await wrapped("read_a", {}, tool_call_id="call_1")
    assert allowed["ok"] is True
    assert calls == [("read_a", "call_1")]


@pytest.mark.asyncio
async def test_turn_executor_without_denylist_preserves_metadata() -> None:
    async def base(
        name: str,
        args: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        return {"ok": True}

    base.concurrency_safe_names = frozenset({"read_a"})  # type: ignore[attr-defined]
    wrapped = _wrap_turn_executor(
        base,
        denied_tools=set(),
        ledger=None,
        persona_id="persona",
        current_turn_id="turn",
    )
    assert wrapped.concurrency_safe_names == frozenset({"read_a"})  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_agent_task_executor_preserves_metadata_and_stop_marker() -> None:
    async def base(
        name: str,
        args: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        return {"ok": True, "path": "out/result.md"}

    base.concurrency_safe_names = frozenset({"read_a"})  # type: ignore[attr-defined]
    wrapped = _wrap_agent_task_executor(
        base,
        output_rel="out/result.md",
        same_workspace_path=lambda left, right: left == right,
    )
    assert wrapped.concurrency_safe_names == frozenset({"read_a"})  # type: ignore[attr-defined]
    result = await wrapped("write_file", {})
    assert result["stop_after_tool"] is True


@pytest.mark.asyncio
async def test_dynamic_executor_and_runner_run_reads_concurrently() -> None:
    state = {"active": 0, "max_active": 0}
    registry = _make_registry(state)

    class _Pipeline:
        def __init__(self) -> None:
            self.tool_registry = registry
            self._tool_profile_grace_registry = None

    ctx = ToolContext()
    wrapper = MessagePipeline._dynamic_tool_executor(_Pipeline(), ctx)
    runner = AgentRunner(
        provider=object(),  # type: ignore[arg-type]
        agent_cfg=AgentConfig(provider="test", model="test"),
    )
    calls = [
        ToolCall(id="1", name="read_a", arguments="{}"),
        ToolCall(id="2", name="read_b", arguments="{}"),
    ]

    results = await runner._execute_tools(calls, wrapper)

    assert state["max_active"] == 2
    assert [item["name"] for item in results] == ["read_a", "read_b"]
