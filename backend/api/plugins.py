"""应用插件状态 API。

当前只返回前端插件（``gui``/``webui``）；后端插件加载器尚未启用，
``plugins/`` 下的清单不会出现在这里。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ..plugin_host import PLUGIN_API_VERSION, PluginContainer

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


@router.get("")
async def list_plugins(request: Request) -> dict[str, Any]:
    container = getattr(request.app.state, "plugins", None)
    active = str(getattr(request.app.state, "frontend", "") or "")
    if not isinstance(container, PluginContainer):
        return {
            "api_version": PLUGIN_API_VERSION,
            "active_frontend": active,
            "plugins": [],
        }
    return container.snapshot(active_frontend=active)


__all__ = ["router"]
