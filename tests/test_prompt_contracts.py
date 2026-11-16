"""系统提示词与 task_context 提示的契约测试。"""

from __future__ import annotations

from types import SimpleNamespace

from agents.behavior_prompt import build_tool_use_protocol
from core.pipeline_task_context import PipelineTaskContextMixin


class _DummyManager:
    enabled = True

    def __init__(self, snapshot: dict) -> None:
        self._snapshot = snapshot

    def context_snapshot(self, group_id: int) -> dict:
        return self._snapshot


class _DummyRegistry:
    def __init__(
        self,
        *,
        profile: str,
        schema_mode: str = "compact",
        routed_names: list[str] | None = None,
    ) -> None:
        self.schema_profile = profile
        self._schema_mode = schema_mode
        self._spec = SimpleNamespace(schema_mode=schema_mode)
        self._routed_names = (
            ["group_trigger_window"] if routed_names is None else routed_names
        )

    def get_spec(self, name: str):
        return self._spec if name == "group_trigger_window" else None

    def routed_tool_names(self, session=None) -> list[str]:
        return list(self._routed_names)


class _DummyPipeline(PipelineTaskContextMixin):
    def __init__(self, *, registry, snapshot: dict, loaded_tools=None) -> None:
        self.group_trigger = _DummyManager(snapshot)
        self.tool_registry = registry
        self._toolset_sessions = {
            "group:123": SimpleNamespace(loaded_tools=set(loaded_tools or ()))
        }


def _closed_snapshot() -> dict:
    return {"active": False, "closed_after": True, "cached_messages": 2}


def _active_snapshot() -> dict:
    return {
        "active": True,
        "llm_owned": True,
        "remaining_messages": 1,
        "near_limit": True,
        "cached_messages": 3,
        "cache_messages": 10,
    }


def test_compact_mode_prompts_direct_group_trigger_call() -> None:
    pipeline = _DummyPipeline(
        registry=_DummyRegistry(profile="compact"),
        snapshot=_closed_snapshot(),
    )
    hint = pipeline._group_trigger_context_hint("group:123")
    assert "调用 group_trigger_window action=open" in hint
    assert "tool_search" not in hint


def test_active_window_hint_uses_direct_extend_and_status() -> None:
    pipeline = _DummyPipeline(
        registry=_DummyRegistry(profile="compact"),
        snapshot=_active_snapshot(),
    )
    hint = pipeline._group_trigger_context_hint("group:123")
    assert "group_trigger_window action=extend" in hint
    assert "group_trigger_window action=status" in hint
    assert "tool_search" not in hint


def test_stub_mode_still_asks_for_tool_search() -> None:
    pipeline = _DummyPipeline(
        registry=_DummyRegistry(profile="compact", schema_mode="stub"),
        snapshot=_closed_snapshot(),
    )
    hint = pipeline._group_trigger_context_hint("group:123")
    assert "先 tool_search 查询参数" in hint


def test_agent_mode_asks_to_load_schema_first() -> None:
    pipeline = _DummyPipeline(
        registry=_DummyRegistry(profile="agent"),
        snapshot=_closed_snapshot(),
        loaded_tools=set(),
    )
    hint = pipeline._group_trigger_context_hint("group:123")
    assert 'tool_search select=["group_trigger_window"]' in hint


def test_agent_mode_after_load_prompts_direct_call() -> None:
    pipeline = _DummyPipeline(
        registry=_DummyRegistry(profile="agent"),
        snapshot=_closed_snapshot(),
        loaded_tools={"group_trigger_window"},
    )
    hint = pipeline._group_trigger_context_hint("group:123")
    assert "调用 group_trigger_window action=open" in hint
    assert "tool_search" not in hint


def test_routed_mode_prompts_direct_call() -> None:
    pipeline = _DummyPipeline(
        registry=_DummyRegistry(profile="routed"),
        snapshot=_closed_snapshot(),
    )
    hint = pipeline._group_trigger_context_hint("group:123")
    assert "调用 group_trigger_window action=open" in hint
    assert "tool_search" not in hint


def test_routed_mode_fallback_uses_load_toolset() -> None:
    pipeline = _DummyPipeline(
        registry=_DummyRegistry(profile="routed", routed_names=[]),
        snapshot=_closed_snapshot(),
    )
    hint = pipeline._group_trigger_context_hint("group:123")
    assert "先 load_toolset" in hint
    assert "tool_search select" not in hint


def test_file_memory_prompt_uses_memory_id_for_delete() -> None:
    protocol = build_tool_use_protocol("file")
    assert "delete_important_memory" in protocol
    assert "memory_id" in protocol
    assert "关键词模糊匹配" not in protocol
    assert "keyword 兼容参数" in protocol


def test_rag_prompt_adds_retrieval_note_without_losing_common_rules() -> None:
    file_protocol = build_tool_use_protocol("file")
    rag_protocol = build_tool_use_protocol("rag")
    assert "rag_retrieval_note" not in file_protocol
    assert "rag_retrieval_note" in rag_protocol
    assert "pinned=true" in rag_protocol
    assert "密钥" in rag_protocol
    assert "delete_important_memory" in rag_protocol


def test_other_tools_block_is_trimmed() -> None:
    protocol = build_tool_use_protocol("file")
    assert "web_search：联网搜索实时信息" not in protocol
    assert "get_weather：查天气" not in protocol
    assert "schedule_wakeup" in protocol
    assert "send_voice_message" in protocol
    assert "_tool_search_required" not in protocol
    assert "[需先tool_search]" in protocol
