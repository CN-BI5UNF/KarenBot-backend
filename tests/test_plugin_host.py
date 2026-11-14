"""应用插件容器：清单发现、校验和前端激活测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.plugin_host import (
    PLUGIN_API_VERSION,
    BackendPlugin,
    PluginContainer,
    PluginContext,
    PluginError,
)


def _write_manifest(
    root: Path, directory: str, **overrides
) -> Path:
    plugin_dir = root / directory
    plugin_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "schema": 1,
        "id": directory,
        "name": f"Test {directory}",
        "kind": "frontend",
        "api_version": PLUGIN_API_VERSION,
        "entry": f"fake_{directory}:Plugin",
        "capabilities": ["test"],
    }
    data.update(overrides)
    path = plugin_dir / "plugin.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_discovers_frontends_and_snapshot(tmp_path):
    _write_manifest(tmp_path, "gui")
    _write_manifest(tmp_path, "webui")
    (tmp_path / "modules.json").write_text(
        json.dumps(
            {
                "frontend": "gui",
                "components": {
                    "gui": {"version": "1.2.3"},
                    "webui": {"version": "4.5.6"},
                },
            }
        ),
        encoding="utf-8",
    )

    container = PluginContainer(tmp_path)
    container.discover()

    assert container.frontend_ids() == ["gui", "webui"]
    assert container.get("gui").manifest.version == "1.2.3"
    assert container.get("webui").manifest.version == "4.5.6"
    snapshot = container.snapshot(active_frontend="gui")
    assert snapshot["api_version"] == PLUGIN_API_VERSION
    assert snapshot["active_frontend"] == "gui"
    by_id = {item["id"]: item for item in snapshot["plugins"]}
    assert by_id["gui"]["active"] is True
    assert by_id["webui"]["active"] is False
    assert by_id["gui"]["status"] == "available"


def test_version_falls_back_to_plugin_version_file(tmp_path):
    _write_manifest(tmp_path, "gui")
    (tmp_path / "gui" / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    container = PluginContainer(tmp_path)
    container.discover()
    assert container.get("gui").manifest.version == "9.9.9"


def test_invalid_manifest_is_reported_not_raised(tmp_path):
    (tmp_path / "gui").mkdir()
    (tmp_path / "gui" / "plugin.json").write_text("{not-json", encoding="utf-8")
    container = PluginContainer(tmp_path)
    container.discover()
    record = container.get("gui")
    assert record is not None
    assert record.status == "invalid"
    assert "JSONDecodeError" in record.error or "Expecting" in record.error
    # 非法规清单仍能出现在快照里，方便 UI 排查。
    assert container.snapshot()["plugins"][0]["status"] == "invalid"


def test_incompatible_api_version_listed_but_not_activatable(tmp_path):
    _write_manifest(tmp_path, "gui", api_version=PLUGIN_API_VERSION + 1)
    container = PluginContainer(tmp_path)
    container.discover()
    record = container.get("gui")
    assert record.status == "incompatible"
    with pytest.raises(PluginError):
        container.activate_frontend("gui", PluginContext(tmp_path))


def test_invalid_entry_rejected(tmp_path):
    _write_manifest(tmp_path, "gui", entry="../../evil:Plugin")
    container = PluginContainer(tmp_path)
    container.discover()
    assert container.get("gui").status == "invalid"


def test_activate_frontend_runs_entry(tmp_path, monkeypatch):
    _write_manifest(tmp_path, "gui", entry="fake_gui_plugin:Plugin")
    (tmp_path / "fake_gui_plugin.py").write_text(
        "from backend.plugin_host import FrontendPlugin\n"
        "class Plugin(FrontendPlugin):\n"
        "    def run(self, context):\n"
        "        return 7\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    container = PluginContainer(tmp_path)
    container.discover()
    code = container.activate_frontend(
        "gui", PluginContext(project_root=tmp_path, frontend="gui")
    )
    assert code == 7
    assert container.get("gui").instance is not None


def test_missing_frontend_raises_with_available_list(tmp_path):
    _write_manifest(tmp_path, "webui")
    container = PluginContainer(tmp_path)
    container.discover()
    with pytest.raises(PluginError) as excinfo:
        container.activate_frontend(
            "nope", PluginContext(project_root=tmp_path)
        )
    assert "webui" in str(excinfo.value)


def test_backend_plugins_are_not_scanned_or_imported(tmp_path):
    # 后端插件协议已定义，但当前版本明确不扫描 plugins/。
    plugin_dir = tmp_path / "plugins" / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "id": "demo",
                "kind": "backend",
                "api_version": PLUGIN_API_VERSION,
                "entry": "should_not_import:Plugin",
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "plugin.py").write_text(
        "raise AssertionError('backend plugin must not be imported')\n",
        encoding="utf-8",
    )
    container = PluginContainer(tmp_path)
    container.discover()
    assert container.get("demo") is None
    assert container.frontend_ids() == []


def test_backend_plugin_protocol_is_defined():
    assert hasattr(BackendPlugin, "setup")
    assert hasattr(BackendPlugin, "startup")
    assert hasattr(BackendPlugin, "shutdown")


def test_version_falls_back_to_package_init(tmp_path):
    _write_manifest(tmp_path, "gui")
    (tmp_path / "gui" / "__init__.py").write_text(
        '__version__ = "7.8.9"\n', encoding="utf-8"
    )
    container = PluginContainer(tmp_path)
    container.discover()
    assert container.get("gui").manifest.version == "7.8.9"
