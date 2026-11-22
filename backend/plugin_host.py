"""应用插件宿主：前端插件发现/激活 + 后端插件协议骨架。

设计边界：

* 前端插件（``gui/``、``webui/``）通过 ``plugin.json`` 被发现；
  ``main.py --frontend <id>`` 激活对应插件。
* 后端插件（未来的 ``plugins/<id>/plugin.json``）这里只定义协议，不扫描、
  不导入、不执行。加载器是后续独立阶段。
* ``backend`` 绝不静态导入 ``gui``/``webui``；插件入口是 manifest 里的字符串。

清单和 entry 校验只读取 JSON，不执行插件代码；只有 ``activate_frontend``
才会 import。
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLUGIN_API_VERSION = 1
FRONTEND_DIRS = ("gui", "webui")
_ALLOWED_KINDS = frozenset({"frontend", "backend"})
_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_ENTRY_RE = re.compile(
    r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$"
)


class PluginError(RuntimeError):
    """插件发现、校验或激活失败。"""


class PluginActivationError(PluginError):
    """插件入口导入/实例化/run 失败。"""


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """插件清单。``entry`` 格式为 ``module:attr``。"""

    plugin_id: str
    name: str
    kind: str
    api_version: int
    entry: str
    version: str = ""
    description: str = ""
    capabilities: tuple[str, ...] = ()
    path: Path | None = None


@dataclass(slots=True)
class PluginRecord:
    """发现阶段的一条记录；invalid/incompatible 也能在 API 中看到。"""

    plugin_id: str
    manifest: PluginManifest | None
    status: str = "available"
    error: str = ""
    instance: Any = None


@dataclass(slots=True)
class PluginContext:
    """传给前端插件 ``run`` / 后端插件 ``setup`` 的上下文。"""

    project_root: Path
    config_file: Path | None = None
    host: str | None = None
    """显式监听地址；None 表示由前端按 config.yaml 的 webui.allow_lan 解析。"""
    port: int = 8000
    token: str | None = None
    no_auth: bool = False
    argv: tuple[str, ...] = ()
    frontend: str = ""
    runtime_host: Any = None
    app: Any = None


class FrontendPlugin:
    """前端插件基类。

    子类实现 ``run(context)``，返回进程退出码；默认 0 表示正常结束。
    前端可以运行在进程内（如 WebUI）或自己启动子进程（如 GUI launcher）。
    """

    manifest: PluginManifest | None = None

    def run(self, context: PluginContext) -> int:
        raise NotImplementedError


class BackendPlugin:
    """后端插件协议骨架（当前版本不加载）。

    未来加载器会在 FastAPI 应用构建期调用 ``setup(app, context)``，并在
    lifespan 中调用可选的 ``startup``/``shutdown``。插件可以在这里注册路由、
    后台任务和配置项。当前版本只保留接口，避免后续返工。
    """

    manifest: PluginManifest | None = None

    def setup(self, app: Any, context: PluginContext) -> None:
        raise NotImplementedError

    async def startup(self) -> None:
        """可选：应用启动后的初始化。"""

    async def shutdown(self) -> None:
        """可选：应用关闭前的清理。"""


class PluginContainer:
    """应用插件发现与前端激活。

    当前只发现内置前端模块（``gui/plugin.json``、``webui/plugin.json``）；
    ``plugins/`` 下的后端插件清单暂不扫描，见模块说明。
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self._records: dict[str, PluginRecord] = {}

    # ------------------------------------------------------------------
    # 发现
    # ------------------------------------------------------------------
    def discover(self) -> None:
        """扫描前端插件清单；不导入任何插件代码。"""
        self._records.clear()
        for directory in FRONTEND_DIRS:
            manifest_path = self.project_root / directory / "plugin.json"
            if manifest_path.is_file():
                self._load_manifest(manifest_path, expected_dir=directory)

    def _load_manifest(self, manifest_path: Path, *, expected_dir: str) -> None:
        fallback_id = expected_dir
        try:
            resolved = manifest_path.resolve()
            if not resolved.is_relative_to(self.project_root):
                raise PluginError("plugin.json 位于项目根之外")
            data = json.loads(resolved.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise PluginError("plugin.json 必须是对象")
            if int(data.get("schema", 1) or 1) != 1:
                raise PluginError("不支持的 plugin schema")
            plugin_id = str(data.get("id") or "").strip()
            if not plugin_id or not _ID_RE.match(plugin_id):
                raise PluginError("plugin id 不合法")
            if plugin_id != expected_dir:
                raise PluginError(
                    f"plugin id 必须与目录名一致（{expected_dir}）"
                )
            kind = str(data.get("kind") or "").strip().lower()
            if kind not in _ALLOWED_KINDS:
                raise PluginError(f"未知插件类型：{kind or '空'}")
            if kind != "frontend":
                raise PluginError(f"{expected_dir}/plugin.json 必须是 frontend 插件")
            api_version = data.get("api_version")
            if not isinstance(api_version, int):
                raise PluginError("api_version 必须是整数")
            entry = str(data.get("entry") or "").strip()
            if not _ENTRY_RE.match(entry):
                raise PluginError("entry 必须是 module:attr 格式")
            capabilities_raw = data.get("capabilities") or []
            if not isinstance(capabilities_raw, list) or not all(
                isinstance(item, str) for item in capabilities_raw
            ):
                raise PluginError("capabilities 必须是字符串数组")
            manifest = PluginManifest(
                plugin_id=plugin_id,
                name=str(data.get("name") or plugin_id),
                kind=kind,
                api_version=api_version,
                entry=entry,
                version=str(data.get("version") or "").strip()
                or self._resolve_version(plugin_id, resolved.parent),
                description=str(data.get("description") or ""),
                capabilities=tuple(capabilities_raw),
                path=resolved,
            )
            status = (
                "available"
                if api_version == PLUGIN_API_VERSION
                else "incompatible"
            )
            error = (
                ""
                if status == "available"
                else f"需要 plugin API {PLUGIN_API_VERSION}，清单声明 {api_version}"
            )
            record = PluginRecord(
                plugin_id=plugin_id,
                manifest=manifest,
                status=status,
                error=error,
            )
        except (OSError, ValueError, TypeError, PluginError) as exc:
            logger.warning("插件清单无效：%s（%s）", manifest_path, exc)
            record = PluginRecord(
                plugin_id=fallback_id,
                manifest=None,
                status="invalid",
                error=f"{type(exc).__name__}: {exc}",
            )
        existing = self._records.get(record.plugin_id)
        if existing is not None:
            logger.warning(
                "忽略重复插件 id=%s：%s", record.plugin_id, manifest_path
            )
            return
        self._records[record.plugin_id] = record

    def _resolve_version(self, plugin_id: str, plugin_dir: Path) -> str:
        for candidate in (
            plugin_dir / "VERSION",
            plugin_dir.parent / "VERSION",
        ):
            try:
                value = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if value:
                return value
        for candidate in (
            plugin_dir / "__init__.py",
            plugin_dir.parent / "__init__.py",
        ):
            try:
                text = candidate.read_text(encoding="utf-8")
            except OSError:
                continue
            match = re.search(
                r"^__version__\s*=\s*[\"']([^\"']+)[\"']",
                text,
                re.MULTILINE,
            )
            if match:
                return match.group(1)
        manifest = self.project_root / "modules.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        components = data.get("components") if isinstance(data, dict) else None
        entry = components.get(plugin_id) if isinstance(components, dict) else None
        if isinstance(entry, dict):
            return str(entry.get("version") or "").strip()
        return ""

    # ------------------------------------------------------------------
    # 查询 / 快照
    # ------------------------------------------------------------------
    def get(self, plugin_id: str) -> PluginRecord | None:
        return self._records.get(plugin_id)

    def frontends(self) -> list[PluginRecord]:
        return [
            record
            for record in self._records.values()
            if (
                record.manifest is not None
                and record.manifest.kind == "frontend"
            )
            or record.status == "invalid"
        ]

    def frontend_ids(self) -> list[str]:
        return sorted(record.plugin_id for record in self.frontends())

    def snapshot(self, *, active_frontend: str = "") -> dict[str, Any]:
        plugins: list[dict[str, Any]] = []
        for record in sorted(
            self.frontends(), key=lambda item: item.plugin_id
        ):
            manifest = record.manifest
            plugins.append(
                {
                    "id": record.plugin_id,
                    "name": manifest.name if manifest else record.plugin_id,
                    "kind": manifest.kind if manifest else "frontend",
                    "version": manifest.version if manifest else "",
                    "description": manifest.description if manifest else "",
                    "capabilities": (
                        list(manifest.capabilities) if manifest else []
                    ),
                    "status": record.status,
                    "active": record.plugin_id == active_frontend,
                    "error": record.error,
                }
            )
        return {
            "api_version": PLUGIN_API_VERSION,
            "active_frontend": active_frontend,
            "plugins": plugins,
        }

    # ------------------------------------------------------------------
    # 前端激活
    # ------------------------------------------------------------------
    def activate_frontend(
        self, plugin_id: str, context: PluginContext
    ) -> int:
        record = self._records.get(plugin_id)
        if record is None:
            available = ", ".join(self.frontend_ids()) or "无"
            raise PluginError(
                f"前端插件不存在：{plugin_id}（可用：{available}）"
            )
        manifest = record.manifest
        if manifest is None or manifest.kind != "frontend":
            raise PluginError(f"{plugin_id} 不是可用的前端插件")
        if record.status == "incompatible":
            raise PluginError(
                f"前端插件 {plugin_id} 不兼容：{record.error}"
            )
        if record.status == "invalid":
            raise PluginError(f"前端插件 {plugin_id} 清单无效：{record.error}")

        try:
            module_name, attr_name = manifest.entry.split(":", 1)
            module = importlib.import_module(module_name)
            target = getattr(module, attr_name)
            if inspect.isclass(target):
                plugin = target()
            else:
                plugin = target
            run = getattr(plugin, "run", None)
            if not callable(run):
                raise PluginActivationError("插件入口没有可调用的 run(context)")
            record.instance = plugin
            result = run(context)
        except PluginError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PluginActivationError(
                f"激活前端插件 {plugin_id} 失败：{type(exc).__name__}: {exc}"
            ) from exc
        if result is None:
            return 0
        return int(result)


__all__ = [
    "BackendPlugin",
    "FrontendPlugin",
    "PLUGIN_API_VERSION",
    "PluginActivationError",
    "PluginContainer",
    "PluginContext",
    "PluginError",
    "PluginManifest",
    "PluginRecord",
]
