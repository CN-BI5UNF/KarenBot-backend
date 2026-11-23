"""Anthropic Prompt Caching 断点注入测试。"""

from __future__ import annotations

import asyncio
from typing import Any

from providers.base import CompletionResult
from providers.protocols.anthropic_proto import (
    AnthropicProvider,
    _apply_cache_breakpoint,
    _is_cache_control_rejection,
    _stable_prefix_end,
    _strip_cache_control,
)


def _provider() -> AnthropicProvider:
    return AnthropicProvider(
        "anthropic_test",
        api_key="test-key",
        base_url="https://example.invalid",
    )


def test_stable_prefix_end_reads_structured_marker() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "user", "content": "tail", "_prompt_section": "volatile_tail"},
    ]
    assert _stable_prefix_end(messages) == 2
    assert _stable_prefix_end([{"role": "user", "content": "u"}]) is None


def test_convert_messages_marks_last_stable_assistant() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "stable user"},
        {"role": "assistant", "content": "stable assistant"},
        {"role": "user", "content": "volatile", "_prompt_section": "volatile_tail"},
    ]
    system_text, converted, break_at = AnthropicProvider._convert_messages(
        messages,
        stable_prefix_end=3,
    )
    assert system_text == "system"
    assert break_at == 2
    _apply_cache_breakpoint(converted, break_at)
    assert converted[1]["role"] == "assistant"
    assert converted[1]["content"] == [
        {
            "type": "text",
            "text": "stable assistant",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert "cache_control" not in converted[2]["content"]


def test_convert_messages_marks_stable_tool_result() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
        {"role": "user", "content": "volatile", "_prompt_section": "volatile_tail"},
    ]
    _, converted, break_at = AnthropicProvider._convert_messages(
        messages,
        stable_prefix_end=3,
    )
    # 首条是 assistant，协议层会在最前面插入 [continue]，断点随之后移一位。
    assert break_at == 3
    _apply_cache_breakpoint(converted, break_at)
    tool_result = converted[2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in converted[3]["content"]


def test_convert_messages_shifts_breakpoint_for_inserted_continue() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "stable assistant"},
        {"role": "user", "content": "volatile", "_prompt_section": "volatile_tail"},
    ]
    _, converted, break_at = AnthropicProvider._convert_messages(
        messages,
        stable_prefix_end=2,
    )
    assert converted[0] == {"role": "user", "content": "[continue]"}
    assert break_at == 2
    _apply_cache_breakpoint(converted, break_at)
    assert converted[1]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_thinking_only_message_does_not_crash_breakpoint() -> None:
    converted = [
        {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "..."}],
        }
    ]
    _apply_cache_breakpoint(converted, 1)
    assert "cache_control" not in converted[0]["content"][0]


def test_empty_text_block_is_skipped_for_cache_control() -> None:
    converted = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "real content"},
                {"type": "text", "text": ""},
            ],
        }
    ]
    _apply_cache_breakpoint(converted, 1)
    assert "cache_control" in converted[0]["content"][0]
    assert "cache_control" not in converted[0]["content"][1]

    empty_only = [{"role": "user", "content": [{"type": "text", "text": ""}]}]
    _apply_cache_breakpoint(empty_only, 1)
    assert "cache_control" not in empty_only[0]["content"][0]


def test_chat_completion_injects_system_tools_and_message_breakpoints() -> None:
    provider = _provider()
    captured: dict[str, Any] = {}

    async def fake_stream(
        kwargs: dict[str, Any], first_token_timeout: float | None, timeout: float
    ):
        captured.update(kwargs)
        captured["timeout"] = timeout
        return CompletionResult(content="ok")

    provider._stream_call = fake_stream  # type: ignore[method-assign]

    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "stable user"},
        {"role": "assistant", "content": "stable assistant"},
        {"role": "user", "content": "volatile", "_prompt_section": "volatile_tail"},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "probe",
                "description": "probe",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    async def run() -> CompletionResult:
        result = await provider.chat_completion(
            messages,
            model="claude-test",
            tools=tools,
            stream=True,
        )
        await provider._client.close()
        return result

    result = asyncio.run(run())

    assert result.content == "ok"
    assert captured["system"] == [
        {
            "type": "text",
            "text": "system prompt",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert captured["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert captured["messages"][1]["content"][0]["cache_control"] == {
        "type": "ephemeral"
    }
    assert "cache_control" not in captured["messages"][2]["content"]
    assert captured["timeout"] == 120.0


def test_chat_completion_without_tail_marker_only_caches_tools_and_system() -> None:
    provider = _provider()
    captured: dict[str, Any] = {}

    async def fake_stream(
        kwargs: dict[str, Any], first_token_timeout: float | None, timeout: float
    ):
        captured.update(kwargs)
        captured["timeout"] = timeout
        return CompletionResult(content="ok")

    provider._stream_call = fake_stream  # type: ignore[method-assign]
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "just one message"},
    ]
    async def run() -> None:
        await provider.chat_completion(
            messages,
            model="claude-test",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "probe",
                        "description": "probe",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            stream=True,
        )
        await provider._client.close()

    asyncio.run(run())

    assert isinstance(captured["system"], list)
    assert "cache_control" in captured["tools"][-1]
    assert "cache_control" not in captured["messages"][0]["content"]
    assert captured["timeout"] == 120.0


def test_cache_control_rejection_detection_and_strip() -> None:
    class FakeBadRequest(Exception):
        status_code = 400

    error = FakeBadRequest("cache_control is not supported")
    assert _is_cache_control_rejection(error)

    class FakeOtherBadRequest(Exception):
        status_code = 400

    assert not _is_cache_control_rejection(FakeOtherBadRequest("bad request"))

    payload = {
        "system": [
            {"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}
        ],
        "tools": [{"name": "t", "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "u", "cache_control": {"type": "ephemeral"}}
                ],
            }
        ],
    }
    stripped = _strip_cache_control(payload)
    assert "cache_control" not in repr(stripped)
    # 原始 payload 不被就地修改。
    assert "cache_control" in repr(payload)
