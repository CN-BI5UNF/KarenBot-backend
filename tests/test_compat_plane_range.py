"""persona_db v1→v2 自动迁移的兼容门禁区间语义。"""

from __future__ import annotations

import copy
import json

import pytest

from core.compat import (
    CompatError,
    DatabaseProbe,
    LocalDataState,
    evaluate,
    parse_manifest,
)

_BASE_MANIFEST = {
    "manifest_format": 1,
    "minimum_launcher_protocol": 1,
    "data_contract_epoch": 0,
    "migration_set_id": "",
    "irreversible": False,
    "planes": {
        "root_config": {
            "supported_read": 2,
            "supported_write": 2,
            "target_version": 2,
        },
        "persona_db": {
            "supported_read": 2,
            "supported_write": 2,
            "target_version": 2,
            "min_local_version": 1,
        },
        "maintenance": {
            "supported_read": 1,
            "supported_write": 1,
            "target_version": 1,
        },
        "rag": {
            "supported_read": 0,
            "supported_write": 0,
            "target_version": 0,
        },
        "projection": {
            "supported_read": 0,
            "supported_write": 0,
            "target_version": 0,
        },
    },
    "required_features": [],
    "optional_features": [],
}


def _local(version: int) -> LocalDataState:
    return LocalDataState(
        planes={
            "root_config": 2,
            "persona_db": version,
            "maintenance": 1,
            "rag": 0,
            "projection": 0,
        },
        databases=(
            DatabaseProbe(
                plane="persona_db",
                relative_path="memory/p/p.db",
                user_version=version,
                schema_version=None,
            ),
        ),
    )


def _manifest(**plane_overrides):
    data = copy.deepcopy(_BASE_MANIFEST)
    data["planes"]["persona_db"].update(plane_overrides)
    return parse_manifest(json.dumps(data))


def test_v1_and_v2_local_databases_are_accepted() -> None:
    manifest = _manifest()

    assert evaluate(manifest, _local(1))["code"] == "compatible"
    assert evaluate(manifest, _local(2))["code"] == "compatible"


def test_newer_local_database_is_rejected() -> None:
    manifest = _manifest()

    result = evaluate(manifest, _local(3))

    assert result["compatible"] is False
    assert result["code"] == "unsupported_data_contract"


def test_legacy_manifest_without_range_still_requires_exact_match() -> None:
    data = copy.deepcopy(_BASE_MANIFEST)
    data["planes"]["persona_db"].pop("min_local_version")
    manifest = parse_manifest(json.dumps(data))

    assert evaluate(manifest, _local(1))["code"] == "migration_required"
    assert evaluate(manifest, _local(2))["code"] == "compatible"


def test_min_local_version_cannot_exceed_target() -> None:
    with pytest.raises(CompatError):
        _manifest(min_local_version=3, target_version=2)


def test_launcher_accepts_range_manifest() -> None:
    from deploy.trusted_launcher import _validate_manifest

    raw, error = _validate_manifest(copy.deepcopy(_BASE_MANIFEST))

    assert error is None
    assert raw is not None
    assert raw["planes"]["persona_db"]["min_local_version"] == 1


def test_launcher_rejects_invalid_min_local_version() -> None:
    from deploy.trusted_launcher import _validate_manifest

    data = copy.deepcopy(_BASE_MANIFEST)
    data["planes"]["persona_db"]["min_local_version"] = 3

    _raw, error = _validate_manifest(data)

    assert error is not None
    assert "min_local_version" in error
