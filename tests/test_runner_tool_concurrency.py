"""AgentRunner 只读工具有序并发测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents.runner import AgentRunner
from app_config.schema import AgentConfig
from providers.base import ToolCall


def _runner() -> AgentRunner:
    return AgentRunner(
        provider=object(),  # type: ignore[arg-type]
        agent_cfg=AgentConfig(provider="test", model="test"),
    )


def _call(call_id: str, name: str, arguments: str = "{}") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _executor(
    events: list[tuple[str, str]],
    safe_names: set[str],
    *,
    delay: float = 0.05,
):
    state = {"active": 0, "max_active": 0}

    async def execute(
        name: str,
        args: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        events.append(("start", name))
        await asyncio.sleep(delay)
        events.append(("end", name))
        state["active"] -= 1
        return {"ok": True, "name": name, "id": tool_call_id}

    execute.concurrency_safe_names = frozenset(safe_names)  # type: ignore[attr-defined]
    execute.state = state  # type: ignore[attr-defined]
    return execute


@pytest.mark.asyncio
async def test_consecutive_safe_tools_run_concurrently_and_keep_result_order() -> None:
    events: list[tuple[str, str]] = []
    executor = _executor(events, {"read_a", "read_b"})
    calls = [
        _call("1", "read_a"),
        _call("2", "read_b"),
    ]

    results = await _runner()._execute_tools(calls, executor)

    assert executor.state["max_active"] == 2
    assert [item["name"] for item in results] == ["read_a", "read_b"]
    assert [item["id"] for item in results] == ["1", "2"]


@pytest.mark.asyncio
async def test_stateful_tool_is_a_barrier_between_safe_batches() -> None:
    events: list[tuple[str, str]] = []
    executor = _executor(events, {"read_a", "read_b"}, delay=0.01)
    calls = [
        _call("1", "read_a"),
        _call("2", "write_x"),
        _call("3", "read_b"),
    ]

    await _runner()._execute_tools(calls, executor)

    assert events == [
        ("start", "read_a"),
        ("end", "read_a"),
        ("start", "write_x"),
        ("end", "write_x"),
        ("start", "read_b"),
        ("end", "read_b"),
    ]
    assert executor.state["max_active"] == 1


@pytest.mark.asyncio
async def test_result_and_event_order_for_parallel_batch() -> None:
    events: list[tuple[str, str]] = []
    runtime_events: list[tuple[str, str]] = []
    executor = _executor(events, {"read_a", "read_b"}, delay=0.01)

    async def record(event: dict[str, Any]) -> None:
        payload = event["payload"]
        runtime_events.append(
            (event["event_type"], str(payload.get("tool_name") or ""))
        )

    calls = [_call("1", "read_a"), _call("2", "read_b")]
    await _runner()._execute_tools(
        calls,
        executor,
        runtime_event_callback=record,
        loop=1,
    )

    assert runtime_events == [
        ("tool_call_started", "read_a"),
        ("tool_call_started", "read_b"),
        ("tool_result_received", "read_a"),
        ("tool_result_received", "read_b"),
    ]


@pytest.mark.asyncio
async def test_parse_error_does_not_block_next_safe_batch() -> None:
    events: list[tuple[str, str]] = []
    executor = _executor(events, {"read_a"}, delay=0.0)
    calls = [
        _call("1", "read_a", "{bad json"),
        _call("2", "read_a"),
    ]

    results = await _runner()._execute_tools(calls, executor)

    assert [item["name"] for item in results] == ["read_a", "read_a"]
    assert results[0]["result"]["ok"] is False
    assert "参数解析失败" in results[0]["result"]["error"]
    assert results[1]["result"]["ok"] is True
