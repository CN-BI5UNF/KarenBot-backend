"""工具提示词与 Schema 优化回归测试。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel

from agents.runner import _tool_schema_mode_counts, _tools_diagnostics
from app_config.schema import RootConfig
from tools import (
    COMPACT_SCHEMA_TOOLS,
    ToolContext,
    ToolRegistry,
    ToolSpec,
    build_default_registry,
    get_default_specs,
)
from tools.base import (
    ToolSchemaMode,
    _smart_truncate_text,
    is_stub_schema_parameters,
)
from tools.schemas import ToolSearchArgs
from tools.tool_search_tools import _discover, tool_search


def _config(profile: str = "compact") -> RootConfig:
    cfg = RootConfig.model_construct()
    cfg.behavior = cfg.behavior.model_copy(update={"tool_schema_profile": profile})
    return cfg


@pytest.fixture(scope="module")
def compact_registry() -> ToolRegistry:
    return build_default_registry(_config("compact"))


def test_group_trigger_window_stays_compact_and_directly_callable(
    compact_registry: ToolRegistry,
) -> None:
    assert "group_trigger_window" in COMPACT_SCHEMA_TOOLS
    spec = compact_registry.get_spec("group_trigger_window")
    assert spec is not None
    assert spec.schema_mode == ToolSchemaMode.COMPACT
    schema = spec.to_openai_schema()
    properties = schema["function"]["parameters"]["properties"]
    assert {"action", "group_ids", "duration_minutes"} <= set(properties)


@pytest.mark.parametrize(
    "tool_name",
    ["send_private_messages", "send_group_message"],
)
def test_compact_send_schema_keeps_reply_and_review_fields(
    compact_registry: ToolRegistry,
    tool_name: str,
) -> None:
    spec = compact_registry.get_spec(tool_name)
    assert spec is not None
    properties = spec.to_openai_schema()["function"]["parameters"]["properties"]
    assert "reply_to_message_id" in properties
    assert "reviewed_until_seq" in properties
    # 低频策略覆写字段仍可由 tool_search detail=full 查看。
    assert "review_policy" not in properties


def test_compact_commit_keeps_review_and_reply_fields(
    compact_registry: ToolRegistry,
) -> None:
    spec = compact_registry.get_spec("commit_send_attempt")
    assert spec is not None
    properties = spec.to_openai_schema()["function"]["parameters"]["properties"]
    assert "reply_to_message_id" in properties
    assert "reviewed_until_seq" in properties
    assert "delivery_interrupt_policy" not in properties


def test_stub_schema_has_no_fake_parameter(compact_registry: ToolRegistry) -> None:
    spec = compact_registry.get_spec("run_python")
    assert spec is not None
    assert spec.schema_mode == ToolSchemaMode.STUB
    schema = spec.to_openai_schema()
    function = schema["function"]
    assert function["parameters"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    }
    assert "_tool_search_required" not in json.dumps(schema, ensure_ascii=False)
    assert "[需先tool_search]" in function["description"]


def test_compact_scope_description_keeps_forbidden_value_in_first_sentence(
    compact_registry: ToolRegistry,
) -> None:
    spec = compact_registry.get_spec("save_important_memory")
    assert spec is not None
    properties = spec.to_openai_schema()["function"]["parameters"]["properties"]
    scope = properties["scope"]["description"]
    assert "private:QQ" in scope
    assert len(scope) <= 90
    # 不能被硬截断成残句。
    assert not scope.endswith("…")


def _iter_descriptions(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                yield value
            else:
                yield from _iter_descriptions(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_descriptions(item)


@pytest.mark.parametrize("profile", ["compact", "full", "routed", "agent"])
def test_schemas_have_no_hard_truncated_descriptions(profile: str) -> None:
    registry = build_default_registry(_config(profile))
    truncated = [
        (spec.name, description)
        for spec in registry._specs.values()
        for description in _iter_descriptions(spec.to_openai_schema())
        if description.endswith("…")
    ]
    assert truncated == []


def test_smart_truncate_prefers_complete_sentence() -> None:
    text = "核心规则必须完整保留；后面是补充说明，不应该被截进来"
    truncated = _smart_truncate_text(text, 15)
    assert truncated == "核心规则必须完整保留；"
    assert not truncated.endswith("…")


def test_smart_truncate_falls_back_to_ellipsis_without_sentence_boundary() -> None:
    truncated = _smart_truncate_text("a" * 100, 20)
    assert truncated == "a" * 20 + "…"


def test_stub_diagnostics_recognize_new_stub_shape(
    compact_registry: ToolRegistry,
) -> None:
    schemas = compact_registry.get_schemas()
    counts = _tool_schema_mode_counts(schemas)
    assert counts["stub"] > 0
    assert counts["full"] + counts["stub"] == len(schemas)
    diagnostics = _tools_diagnostics(schemas, None)
    assert diagnostics["stub"] == counts["stub"]
    assert diagnostics["full"] == counts["full"]


def test_stub_classifier_does_not_flag_no_arg_or_full_tools(
    compact_registry: ToolRegistry,
) -> None:
    no_action = compact_registry.get_spec("no_action")
    assert no_action is not None
    parameters = no_action.to_openai_schema()["function"]["parameters"]
    assert parameters["properties"] == {}
    assert is_stub_schema_parameters(parameters) is False
    assert _tool_schema_mode_counts(
        [no_action.to_openai_schema()]
    ) == {"full": 1, "stub": 0}

    # 空 properties 但没有 additionalProperties=true 也不是 stub。
    assert is_stub_schema_parameters({"type": "object", "properties": {}}) is False
    # 有真实字段的更不是 stub。
    assert (
        is_stub_schema_parameters(
            {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "additionalProperties": True,
            }
        )
        is False
    )
    # 真正的 stub 形态。
    assert (
        is_stub_schema_parameters(
            {"type": "object", "properties": {}, "additionalProperties": True}
        )
        is True
    )


def test_every_registered_tool_has_search_tags() -> None:
    missing = [spec.name for spec in get_default_specs() if not spec.search_tags]
    assert missing == []


def test_complex_tools_have_examples() -> None:
    specs = {spec.name: spec for spec in get_default_specs()}
    expected = {
        "start_agent_task",
        "filter_archive_records",
        "schedule_wakeup",
        "save_important_memory",
        "get_forward_msg",
        "summarize_conversation",
        "summarize_chat_history",
        "group_trigger_window",
    }
    assert {
        name for name in expected if not specs[name].examples
    } == set()


@pytest.mark.parametrize("profile", ["compact", "routed", "agent"])
def test_registry_fills_search_tags_when_tool_has_none(
    profile: str,
    monkeypatch,
) -> None:
    import dataclasses

    import tools as tools_module

    original = next(
        spec for spec in get_default_specs() if spec.name == "read_file"
    )
    untagged = dataclasses.replace(original, search_tags=[])
    monkeypatch.setattr(tools_module, "get_default_specs", lambda: [untagged])

    registry = build_default_registry(_config(profile))
    spec = registry.get_spec("read_file")
    assert spec is not None
    assert spec.search_tags
    assert set(spec.search_tags) & {"workspace", "read_file"}


def test_tool_search_prefers_exact_tag_over_description_substring(
    compact_registry: ToolRegistry,
) -> None:
    candidates = _discover(compact_registry, "定时")
    assert candidates[0]["name"] == "schedule_wakeup"
    assert _discover(compact_registry, "撤回")[0]["name"] == "recall_message"
    assert "set_group_ban" in {
        item["name"] for item in _discover(compact_registry, "禁言")
    }


@pytest.mark.asyncio
async def test_tool_search_returns_examples_and_tags(compact_registry: ToolRegistry) -> None:
    ctx = ToolContext(extras={"tool_registry": compact_registry})
    result = await tool_search(
        ToolSearchArgs(tool_name="start_agent_task"),
        ctx,
    )
    assert result["ok"] is True
    assert result["examples"]
    assert result["search_tags"]


def test_routed_read_file_description_mentions_document_support() -> None:
    registry = build_default_registry(_config("routed"))
    spec = registry.get_spec("read_file")
    assert spec is not None
    assert "PDF" in (spec.short_description or "")
    assert "agent_read" in (spec.short_description or "")


def test_read_only_tools_are_marked_concurrency_safe() -> None:
    specs = {spec.name: spec for spec in get_default_specs()}
    expected = {
        "web_search",
        "web_fetch",
        "get_weather",
        "describe_image",
        "read_file",
        "list_files",
        "lookup_slang",
        "list_knowledge_notes",
        "tool_search",
        "agent_read",
        "agent_glob",
        "agent_grep",
        "agent_ls",
    }
    assert expected <= {name for name, spec in specs.items() if spec.concurrency_safe}
    assert not specs["send_group_message"].concurrency_safe
    assert not specs["write_file"].concurrency_safe


class _EmptyArgs(BaseModel):
    pass


class _ProbeTool:
    def __init__(self) -> None:
        self.seen_ids: list[str | None] = []

    async def __call__(self, args: Any, ctx: ToolContext) -> dict[str, Any]:
        self.seen_ids.append(ctx.tool_call_id)
        if ctx.tool_call_id == "slow":
            await asyncio.sleep(0.05)
        return {
            "ok": True,
            "tool_call_id": ctx.tool_call_id,
            "extras_has_tool_call_id": "tool_call_id" in ctx.extras,
        }


def test_executor_isolates_tool_call_id_per_invocation() -> None:
    probe = _ProbeTool()
    spec = ToolSpec(
        name="probe",
        description="probe",
        args_model=_EmptyArgs,
        func=probe,
        concurrency_safe=True,
    )
    registry = ToolRegistry([spec])
    ctx = ToolContext()
    executor = registry.get_executor(ctx)

    async def run_both() -> list[dict[str, Any]]:
        return await asyncio.gather(
            executor("probe", {}, tool_call_id="slow"),
            executor("probe", {}, tool_call_id="fast"),
        )

    results = asyncio.run(run_both())
    assert {result["tool_call_id"] for result in results} == {"slow", "fast"}
    assert all(result["extras_has_tool_call_id"] is False for result in results)
    assert ctx.tool_call_id is None
    assert "tool_call_id" not in ctx.extras


def test_executor_exposes_concurrency_safe_names() -> None:
    def _spec(name: str, safe: bool) -> ToolSpec:
        return ToolSpec(
            name=name,
            description=name,
            args_model=_EmptyArgs,
            func=_ProbeTool(),
            concurrency_safe=safe,
        )

    registry = ToolRegistry([_spec("read_a", True), _spec("write_b", False)])
    executor = registry.get_executor(ToolContext())
    assert executor.concurrency_safe_names == frozenset({"read_a"})  # type: ignore[attr-defined]
