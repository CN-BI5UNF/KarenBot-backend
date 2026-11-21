"""Temporary group-chat receive filter controlled by the model.

This is intentionally group-only: private messages always reach the main model.
In ``pause`` mode all ordinary group messages are dropped while @-mentions still
pass.  In ``selected`` mode only the chosen groups pass, but @-mentions from any
group still pass as well.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

GroupSelectionMode = Literal["all", "pause", "selected"]
MAX_SELECTION_MINUTES = 1440


def _positive_ids(
    values: list[int | str] | tuple[int | str, ...] | set[int | str] | None,
) -> tuple[str, ...]:
    """规范化群 ID 列表。

    统一使用字符串，兼容 QQ 数字群号与未来非数字频道 ID。旧的整数调用方
    仍然可以传 int，这里会无损转成字符串。
    """
    result: list[str] = []
    for value in values or []:
        text = str(value).strip()
        if not text:
            continue
        if text.lstrip("-").isdigit() and int(text) == 0:
            continue
        if text not in result:
            result.append(text)
    return tuple(result)


@dataclass(slots=True)
class GroupChatSelection:
    """Current temporary group receive policy."""

    mode: GroupSelectionMode = "all"
    group_ids: tuple[str, ...] = ()
    until: float | None = None
    reason: str = ""
    updated_at: float = field(default_factory=time.time)

    def is_active(self, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        return (
            self.mode in {"pause", "selected"}
            and self.until is not None
            and self.until > current
        )

    def allows(self, group_id: int | str, is_mention: bool, *, now: float | None = None) -> bool:
        """Return whether one group message should reach the model."""
        if not self.is_active(now) or is_mention:
            return True
        if self.mode == "selected":
            return str(group_id) in self.group_ids
        return self.mode != "pause"

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        current = time.time() if now is None else float(now)
        active = self.is_active(current)
        remaining = 0
        if active and self.until is not None:
            remaining = max(0, int((self.until - current + 59) // 60))
        return {
            "mode": self.mode if active else "all",
            "active": active,
            "group_ids": list(self.group_ids),
            "until": self.until if active else None,
            "remaining_minutes": remaining,
            "reason": self.reason if active else "",
        }


def build_group_chat_selection(
    *,
    mode: str,
    group_ids: list[int | str] | None = None,
    duration_minutes: int = 60,
    reason: str = "",
    now: float | None = None,
) -> GroupChatSelection:
    """Validate tool input and build the runtime selection."""
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"all", "pause", "selected"}:
        raise ValueError("mode 必须是 all、pause 或 selected")
    normalized_ids = _positive_ids(group_ids)
    if normalized_mode == "selected" and not normalized_ids:
        raise ValueError("selected 模式至少需要一个有效群号")
    if normalized_mode == "all":
        return GroupChatSelection(mode="all", reason="", updated_at=time.time())
    try:
        minutes = int(duration_minutes)
    except (TypeError, ValueError):
        raise ValueError("duration_minutes 必须是整数") from None
    if minutes < 1 or minutes > MAX_SELECTION_MINUTES:
        raise ValueError(f"duration_minutes 必须在 1-{MAX_SELECTION_MINUTES} 之间")
    current = time.time() if now is None else float(now)
    return GroupChatSelection(
        mode=normalized_mode,
        group_ids=normalized_ids if normalized_mode == "selected" else (),
        until=current + minutes * 60.0,
        reason=str(reason or "").strip(),
        updated_at=current,
    )


__all__ = [
    "GroupChatSelection",
    "GroupSelectionMode",
    "MAX_SELECTION_MINUTES",
    "build_group_chat_selection",
]
