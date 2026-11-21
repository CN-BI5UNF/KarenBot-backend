"""v2 -> v3：旧 NapCat 进程托管迁移到 programs。"""

from __future__ import annotations

from app_config.config_migration import migrate_config
from app_config.schema import NapCatAdapterConfig, RootConfig


def _base(adapters: dict | None = None, *, version: int = 2) -> dict:
    return {
        "version": version,
        "agents": {"chat": {"provider": "x", "model": "y"}},
        "providers": {
            "x": {"protocol": "openai_compat", "base_url": "http://localhost:1"}
        },
        "adapters": adapters or {},
    }


def _managed_adapter(**overrides) -> dict:
    adapter = {
        "type": "napcat",
        "enabled": True,
        "transport": "ws_client",
        "host": "127.0.0.1",
        "port": 3001,
        "manage_process": True,
        "process_path": '"C:\\NapCat\\launch.bat"',
        "process_args": ["--fast"],
        "auto_restart": False,
        "process_warmup_seconds": 4.0,
    }
    adapter.update(overrides)
    return adapter


def test_managed_napcat_adapter_becomes_program() -> None:
    migrated, report = migrate_config(_base({"default": _managed_adapter()}))

    assert migrated["version"] == 3
    assert report.changed is True
    program = migrated["programs"]["napcat_default"]
    assert program["executable"] == "C:\\NapCat\\launch.bat"
    assert program["args"] == ["--fast"]
    assert program["probe_host"] == "127.0.0.1"
    assert program["probe_port"] == 3001
    assert program["auto_restart"] is False
    assert program["startup_wait_seconds"] == 4.0
    assert program["stop_with_karen"] is True
    assert "manage_process" not in migrated["adapters"]["default"]
    assert "process_path" not in migrated["adapters"]["default"]
    assert "process_args" not in migrated["adapters"]["default"]
    assert "auto_restart" not in migrated["adapters"]["default"]
    assert "process_warmup_seconds" not in migrated["adapters"]["default"]
    cfg = RootConfig.model_validate(migrated)
    assert cfg.programs["napcat_default"].executable.endswith("launch.bat")


def test_ws_server_adapter_gets_no_probe_port() -> None:
    migrated, _report = migrate_config(
        _base(
            {
                "default": _managed_adapter(
                    transport="ws_server", mode="server", port=6700
                )
            }
        )
    )

    assert migrated["programs"]["napcat_default"]["probe_port"] == 0


def test_disabled_adapter_migrates_to_disabled_program() -> None:
    migrated, _report = migrate_config(
        _base({"default": _managed_adapter(enabled=False)})
    )

    assert migrated["programs"]["napcat_default"]["enabled"] is False


def test_manage_process_false_only_strips_old_keys() -> None:
    migrated, _report = migrate_config(
        _base({"default": _managed_adapter(manage_process=False)})
    )

    assert "programs" not in migrated
    assert "process_path" not in migrated["adapters"]["default"]
    assert "auto_restart" not in migrated["adapters"]["default"]


def test_managed_without_path_warns_and_creates_nothing() -> None:
    migrated, report = migrate_config(
        _base({"default": _managed_adapter(process_path="  ")})
    )

    assert "programs" not in migrated
    assert any("没有进程路径" in warning for warning in report.warnings)


def test_existing_programs_are_preserved_and_id_collisions_suffixed() -> None:
    raw = _base({"default": _managed_adapter()})
    raw["programs"] = {
        "napcat_default": {
            "name": "已有程序",
            "executable": "/already/there",
        }
    }

    migrated, _report = migrate_config(raw)

    assert migrated["programs"]["napcat_default"]["name"] == "已有程序"
    assert migrated["programs"]["napcat_default_2"]["executable"].endswith(
        "launch.bat"
    )


def test_v1_config_migrates_through_v2_to_v3() -> None:
    raw = _base({"default": _managed_adapter()}, version=1)

    migrated, report = migrate_config(raw)

    assert migrated["version"] == 3
    assert "napcat_default" in migrated["programs"]
    assert report.changed is True


def test_telegram_adapter_old_fields_do_not_create_program() -> None:
    adapter = _managed_adapter(type="telegram", bot_token_id="tg_token")

    migrated, _report = migrate_config(_base({"tg": adapter}))

    assert "programs" not in migrated
    assert "manage_process" not in migrated["adapters"]["tg"]
    assert "auto_restart" not in migrated["adapters"]["tg"]


def test_old_process_fields_are_gone_from_adapter_schema() -> None:
    fields = set(NapCatAdapterConfig.model_fields)
    assert not {
        "manage_process",
        "process_path",
        "process_args",
        "auto_restart",
        "process_warmup_seconds",
    } & fields


def test_program_id_is_validated_by_schema() -> None:
    raw = _base()
    raw["version"] = 3
    raw["programs"] = {"bad id": {"executable": "/bin/true"}}

    try:
        RootConfig.model_validate(raw)
    except ValueError as exc:
        assert "程序 ID" in str(exc)
    else:
        raise AssertionError("非法程序 ID 应该被拒绝")
