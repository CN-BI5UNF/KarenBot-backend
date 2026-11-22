"""WebUI 监听地址解析测试。"""

from __future__ import annotations

from pathlib import Path

from app_config.schema import RootConfig
from backend.webui_access import resolve_webui_host


def _write_config(path: Path, *, allow_lan: bool | None) -> Path:
    lines = ["version: 3", "agents:", "  chat:", "    provider: x", "    model: y"]
    lines += [
        "providers:",
        "  x:",
        "    protocol: openai_compat",
        "    base_url: http://localhost:1",
    ]
    if allow_lan is not None:
        lines += ["webui:", f"  allow_lan: {'true' if allow_lan else 'false'}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_root_config_defaults_to_loopback_only() -> None:
    cfg = RootConfig.model_validate(
        {
            "agents": {"chat": {"provider": "x", "model": "y"}},
            "providers": {
                "x": {"protocol": "openai_compat", "base_url": "http://localhost:1"}
            },
        }
    )
    assert cfg.webui.allow_lan is False


def test_missing_config_defaults_to_loopback(tmp_path: Path) -> None:
    host, source = resolve_webui_host(None, tmp_path / "missing.yaml")
    assert host == "127.0.0.1"
    assert source == "default"


def test_config_can_enable_lan(tmp_path: Path) -> None:
    config = _write_config(tmp_path / "config.yaml", allow_lan=True)
    host, source = resolve_webui_host(None, config)
    assert host == "0.0.0.0"
    assert source == "config"


def test_config_false_keeps_loopback(tmp_path: Path) -> None:
    config = _write_config(tmp_path / "config.yaml", allow_lan=False)
    host, source = resolve_webui_host(None, config)
    assert host == "127.0.0.1"
    assert source == "default"


def test_cli_host_overrides_config(tmp_path: Path) -> None:
    config = _write_config(tmp_path / "config.yaml", allow_lan=True)
    host, source = resolve_webui_host("192.168.1.50", config)
    assert host == "192.168.1.50"
    assert source == "cli"


def test_invalid_yaml_falls_back_to_loopback(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("webui: [unterminated\n", encoding="utf-8")
    host, source = resolve_webui_host(None, config)
    assert host == "127.0.0.1"
    assert source == "default"
