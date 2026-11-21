"""状态指纹与新连接快照：外部程序失败必须能推送。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.events import EventHub, status_fingerprint


def test_fingerprint_changes_with_program_failure() -> None:
    base = {
        "state": "running",
        "adapter_connected": True,
        "persona": "p",
        "last_error": "",
        "programs": [
            {"id": "a", "state": "running", "failure_seq": 0, "pid": 1},
        ],
    }
    same = {**base, "programs": [dict(base["programs"][0])]}
    failed = {
        **base,
        "programs": [
            {
                "id": "a",
                "state": "failed",
                "failure_seq": 1,
                "pid": None,
            }
        ],
    }

    assert status_fingerprint(base) == status_fingerprint(same)
    assert status_fingerprint(base) != status_fingerprint(failed)


async def test_register_sends_immediate_status_snapshot() -> None:
    host = SimpleNamespace(
        status=lambda: {"state": "running", "programs": [{"id": "a"}]}
    )
    hub = EventHub(host, log_buffer=None)
    queue: asyncio.Queue = asyncio.Queue(maxsize=4)

    hub.register(queue)

    event = queue.get_nowait()
    assert event["type"] == "status"
    assert event["data"]["programs"][0]["id"] == "a"
