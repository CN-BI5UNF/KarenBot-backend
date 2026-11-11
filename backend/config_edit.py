"""配置改动的原子提交 —— 改副本、校验、通过了才搬回原对象。

WebUI 的写配置接口都遵守同一条规则：**校验失败的改动不能留在内存里**。

反面教材（真实事故）：``PATCH /api/settings`` 先把值写进 ``rt.config`` 再校验，
校验失败只回了一个 400，坏值却已经留在内存里；之后任意一个不校验的接口
（比如「模型」页换 Agent 的 provider）落盘一次，就把这份启动时才炸的配置
写进了 config.yaml，Runtime 下次启动直接起不来。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from app_config.schema import RootConfig


def validate_config(cfg: RootConfig) -> None:
    """跑一遍完整校验（含 provider 引用），不合法抛 ``ValidationError``。"""
    RootConfig.model_validate(cfg.model_dump())


def _merge_value(existing: Any, incoming: Any) -> Any:
    """Merge ``incoming`` into an existing model/dict/list when types allow.

    The live Runtime keeps references to nested config sections (pipeline
    holds ``config.behavior``, services hold ``features.*``, agents hold
    ``agents.chat``).  Replacing those sections would silently leave the
    running components on stale objects, which is why so many settings used to
    require a Runtime restart.  Preserving nested identities lets those
    components observe changes immediately.
    """
    if isinstance(existing, BaseModel) and type(existing) is type(incoming):
        _merge_model(existing, incoming)
        return existing
    if isinstance(existing, dict) and isinstance(incoming, dict):
        _merge_dict(existing, incoming)
        return existing
    if isinstance(existing, list) and isinstance(incoming, list):
        _merge_list(existing, incoming)
        return existing
    return incoming


def _merge_model(target: BaseModel, incoming: BaseModel) -> None:
    for field_name in type(target).model_fields:
        old = getattr(target, field_name, None)
        new = getattr(incoming, field_name)
        setattr(target, field_name, _merge_value(old, new))


def _merge_dict(target: dict[Any, Any], incoming: dict[Any, Any]) -> None:
    for key in list(target):
        if key not in incoming:
            del target[key]
    for key, value in incoming.items():
        target[key] = _merge_value(target[key], value) if key in target else value


def _merge_list(target: list[Any], incoming: list[Any]) -> None:
    """Preserve model elements by index; scalar lists are replaced atomically."""
    if all(isinstance(item, (BaseModel, dict)) for item in [*target, *incoming]):
        for index, value in enumerate(incoming):
            if index < len(target):
                target[index] = _merge_value(target[index], value)
            else:
                target.append(value)
        if len(target) > len(incoming):
            del target[len(incoming) :]
        return
    target[:] = incoming


def apply_validated(
    cfg: RootConfig,
    mutate: Callable[[RootConfig], None],
    *,
    in_place: bool = True,
) -> None:
    """在深拷贝上执行 ``mutate``，校验通过后合并回 ``cfg``。

    With ``in_place=True`` (hot settings), nested model/dict identities are
    preserved so live Runtime components see the new values immediately.

    With ``in_place=False`` (fields that still require a restart), top-level
    sections are replaced instead.  Live components keep their old references
    and therefore keep running the old config until the Runtime is restarted,
    avoiding half-applied RAG/persona/container changes.

    Validation always runs on a full deep copy first, so a rejected edit
    leaves ``cfg`` untouched.
    """
    draft = cfg.model_copy(deep=True)
    mutate(draft)
    validate_config(draft)
    if in_place:
        _merge_model(cfg, draft)
    else:
        for field_name in type(cfg).model_fields:
            setattr(cfg, field_name, getattr(draft, field_name))
