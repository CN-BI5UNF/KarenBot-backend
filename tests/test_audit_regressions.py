from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
import zipfile
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from agents.persona_import import (
    PersonaImportError,
    _validated_zip_entries,
    copy_persona_dir,
    import_persona_zip,
)
from agents.persona_loader import load_persona
from app_config.paths import AppPaths
from backend.api import chats, personas
from backend.provider_admin import ProviderAdminError, ProviderPayload, resolve_probe_target
from core import config_transaction
from core.chat_pagination import ChatPageRequest, ChatPaginationManager
from core.scoped_deletion import (
    ScopedDeletionError,
    ScopedDeletionManager,
    _get_secret_key,
)
from features.coding_agent.backends import _APPLY_PATCH_SCRIPT
from features.container.docker_cli import DockerCLI
from features.container.errors import ContainerError
from features.container.service import _EXPORT_FILE_SCRIPT, ContainerService
from providers.protocols.anthropic_proto import AnthropicProvider
from providers.safe_http import UnsafeProviderTarget, send_provider_request


def test_persona_loader_accepts_only_literal_exports(tmp_path) -> None:
    paths = AppPaths(project_root=tmp_path)
    persona_dir = paths.PERSONAS_DIR / "safe"
    persona_dir.mkdir(parents=True)
    (persona_dir / "persona_prompt.py").write_text(
        '"""Static persona data."""\n'
        'PERSONA_PROMPT = "hello"\n'
        'PERSONA_VARS = {"name": "Safe"}\n',
        encoding="utf-8",
    )

    persona = load_persona(paths, "safe")
    assert persona.prompt == "hello"
    assert persona.vars == {"name": "Safe"}


def test_persona_loader_never_executes_module_code(tmp_path) -> None:
    paths = AppPaths(project_root=tmp_path)
    persona_dir = paths.PERSONAS_DIR / "unsafe"
    persona_dir.mkdir(parents=True)
    marker = tmp_path / "executed"
    (persona_dir / "persona_prompt.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran')\n"
        "PERSONA_PROMPT = 'hello'\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="静态"):
        load_persona(paths, "unsafe")
    assert not marker.exists()


def test_persona_api_rejects_traversal_and_symlink_directories(tmp_path) -> None:
    root = tmp_path / "personas"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    rt = SimpleNamespace(paths=SimpleNamespace(PERSONAS_DIR=root))

    with pytest.raises(HTTPException) as traversal:
        personas._persona_directory(rt, "..")
    assert traversal.value.status_code == 400
    with pytest.raises(HTTPException) as linked:
        personas._persona_directory(rt, "linked")
    assert linked.value.status_code == 400


def test_persona_zip_limits_uncompressed_file_size(tmp_path, monkeypatch) -> None:
    archive = tmp_path / "large.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("persona/persona_prompt.py", "0123456789")
    monkeypatch.setattr("agents.persona_import.MAX_PERSONA_FILE_BYTES", 4)

    with zipfile.ZipFile(archive) as zf, pytest.raises(
        PersonaImportError, match="大小上限"
    ):
        _validated_zip_entries(zf)


def test_anthropic_parallel_tool_results_share_one_user_message() -> None:
    _system, converted, _break_at = AnthropicProvider._convert_messages(
        [
            {"role": "tool", "tool_call_id": "call-1", "content": "first"},
            {"role": "tool", "tool_call_id": "call-2", "content": "second"},
        ]
    )
    assert len(converted) == 1
    assert [block["tool_use_id"] for block in converted[0]["content"]] == [
        "call-1",
        "call-2",
    ]


def test_host_patch_rejects_partial_file_deletion(tmp_path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("first\nsecond\n", encoding="utf-8")
    patch = "--- a/notes.txt\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-first\n"
    result = subprocess.run(
        [sys.executable, "-c", _APPLY_PATCH_SCRIPT],
        input=json.dumps({"patch": patch}),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode != 0
    assert target.read_text(encoding="utf-8") == "first\nsecond\n"


def test_host_patch_checks_symlink_before_reading_target(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    secret = tmp_path / "outside.txt"
    secret.write_text("sensitive-outside-content\n", encoding="utf-8")
    (root / "linked.txt").symlink_to(secret)
    patch = (
        "--- a/linked.txt\n+++ b/linked.txt\n@@ -1,1 +1,1 @@\n"
        "-sensitive-outside-content\n+replacement\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", _APPLY_PATCH_SCRIPT],
        input=json.dumps({"patch": patch}),
        text=True,
        capture_output=True,
        cwd=root,
        check=False,
    )
    assert result.returncode != 0
    assert "sensitive-outside-content" not in result.stdout + result.stderr
    assert secret.read_text(encoding="utf-8") == "sensitive-outside-content\n"


def test_probe_does_not_send_stored_key_to_changed_url() -> None:
    stored = SimpleNamespace(
        protocol="openai_compat",
        base_url="https://8.8.4.4/v1",
        api_key_id="provider-key",
        preset=None,
        model_vendor=None,
    )
    cfg = SimpleNamespace(
        providers={"saved": stored},
        agents=SimpleNamespace(),
    )

    class Secrets:
        calls: list[str] = []

        def get(self, key_id: str) -> str:
            self.calls.append(key_id)
            return "secret"

    secrets = Secrets()
    payload = ProviderPayload(
        base_url="https://8.8.8.8/v1",
        model="test-model",
    )
    with pytest.raises(ProviderAdminError, match="已保存的密钥"):
        resolve_probe_target(
            cfg, secrets, {}, payload, provider_id="saved"
        )
    assert secrets.calls == []


def test_probe_does_not_treat_request_preset_as_saved_target() -> None:
    stored = SimpleNamespace(
        protocol=None,
        base_url=None,
        api_key_id="provider-key",
        preset="saved-local",
        model_vendor=None,
    )
    cfg = SimpleNamespace(
        providers={"saved": stored},
        agents=SimpleNamespace(),
    )
    presets = {
        "saved-local": SimpleNamespace(
            protocol="openai_compat",
            base_url="http://127.0.0.1:11434/v1",
            models=[],
        ),
        "request-preset": SimpleNamespace(
            protocol="openai_compat",
            base_url="https://8.8.8.8/v1",
            models=[],
        ),
    }

    class Secrets:
        calls: list[str] = []

        def get(self, key_id: str) -> str:
            self.calls.append(key_id)
            return "secret"

    secrets = Secrets()
    with pytest.raises(ProviderAdminError, match="已保存的密钥"):
        resolve_probe_target(
            cfg,
            secrets,
            presets,
            ProviderPayload(preset="request-preset", model="test-model"),
            provider_id="saved",
        )
    assert secrets.calls == []


def test_probe_allows_saved_private_provider_url() -> None:
    stored = SimpleNamespace(
        protocol="openai_compat",
        base_url="http://127.0.0.1:11434/v1",
        api_key_id="provider-key",
        preset=None,
        model_vendor=None,
    )
    cfg = SimpleNamespace(
        providers={"local": stored},
        agents=SimpleNamespace(),
    )

    class Secrets:
        def get(self, key_id: str) -> str:
            assert key_id == "provider-key"
            return "local-key"

    target = resolve_probe_target(
        cfg,
        Secrets(),
        {},
        ProviderPayload(model="local-model"),
        provider_id="local",
    )
    assert target.api_key == "local-key"
    assert target.base_url.startswith("http://127.0.0.1:11434")
    assert not target.public_only


def test_probe_rejects_unsaved_private_provider_url() -> None:
    cfg = SimpleNamespace(providers={}, agents=SimpleNamespace())
    with pytest.raises(ProviderAdminError, match="不能探测内网"):
        resolve_probe_target(
            cfg,
            SimpleNamespace(get=lambda _key: None),
            {},
            ProviderPayload(
                protocol="openai_compat",
                base_url="http://127.0.0.1:11434/v1",
                api_key="local-key",
                model="local-model",
            ),
        )


def test_probe_rejects_invalid_target_port() -> None:
    cfg = SimpleNamespace(providers={}, agents=SimpleNamespace())
    with pytest.raises(ProviderAdminError, match="端口无效"):
        resolve_probe_target(
            cfg,
            SimpleNamespace(get=lambda _key: None),
            {},
            ProviderPayload(
                protocol="openai_compat",
                base_url="https://8.8.8.8:invalid/v1",
                api_key="local-key",
                model="local-model",
            ),
        )


def test_config_merge_preserves_protected_keys_and_rejects_future_csrf(
    tmp_path, monkeypatch
) -> None:
    merged = config_transaction.safe_merge_config_draft(
        {"backup": {"enabled": True}, "webui": {"port": 8000}},
        {"backup": {"enabled": False}, "webui": {"port": 9000}},
    )
    assert merged["backup"] == {"enabled": True}
    assert merged["webui"]["port"] == 9000

    monkeypatch.setenv("KAREN_CSRF_SECRET_FILE", str(tmp_path / "csrf.key"))
    monkeypatch.delenv("KAREN_CSRF_SECRET", raising=False)
    monkeypatch.setattr(config_transaction, "_CSRF_SECRET", None)
    session_id = "session"
    timestamp = str(int(time.time()) + 3600)
    payload = f"{session_id}:{timestamp}".encode()
    signature = hmac.new(
        config_transaction._csrf_secret(), payload, hashlib.sha256
    ).hexdigest()[:16]
    assert not config_transaction.validate_csrf_token(
        session_id, f"{timestamp}:{signature}"
    )


def test_csrf_secret_is_persisted_with_private_permissions(tmp_path, monkeypatch) -> None:
    key_path = tmp_path / "csrf.key"
    monkeypatch.setenv("KAREN_CSRF_SECRET_FILE", str(key_path))
    monkeypatch.delenv("KAREN_CSRF_SECRET", raising=False)
    monkeypatch.setattr(config_transaction, "_CSRF_SECRET", None)
    generated = config_transaction._csrf_secret()

    monkeypatch.setattr(config_transaction, "_CSRF_SECRET", None)
    reloaded = config_transaction._csrf_secret()
    assert len(generated) == 32
    assert reloaded == generated
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_origin_allowlist_matches_effective_port() -> None:
    allowed = ["localhost:8000"]
    assert not config_transaction.validate_origin_header(
        "http://localhost", None, allowed
    )
    assert config_transaction.validate_origin_header(
        "http://localhost:8000", None, allowed
    )


def test_scoped_deletion_uses_persisted_private_key_and_validates_ids(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("KAREN_SCOPE_DELETION_KEY", raising=False)
    monkeypatch.delenv("DEBATA_SCOPE_DELETION_KEY", raising=False)
    quarantine = tmp_path / "quarantine"
    conn = sqlite3.connect(":memory:")
    first = ScopedDeletionManager(conn, quarantine)
    second = ScopedDeletionManager(conn, quarantine)

    key_file = quarantine / ".scope-deletion.key"
    assert len(first._secret_key) == 32
    assert second._secret_key == first._secret_key
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    with pytest.raises(ScopedDeletionError, match="标识格式"):
        first._quarantine_path("../../outside", kind="manifest")
    assert conn.execute("SELECT 1").fetchone() == (1,)
    conn.close()


def test_scoped_deletion_keeps_caller_owned_karen_db_connection_open(tmp_path) -> None:
    from memory.karen_db import KarenDB

    database = KarenDB(tmp_path / "memory.db")
    database.ensure_ready()
    manager = ScopedDeletionManager(database, tmp_path / "quarantine")
    assert not manager.is_tombstoned("persona", "record")
    assert not manager.is_tombstoned("persona", "record")
    assert database.connect().execute("SELECT 1").fetchone()[0] == 1
    database.close()


def test_persisted_secret_files_reload_without_os_fchmod(tmp_path, monkeypatch) -> None:
    csrf_path = tmp_path / "csrf.key"
    scope_path = tmp_path / "quarantine"
    monkeypatch.setenv("KAREN_CSRF_SECRET_FILE", str(csrf_path))
    monkeypatch.delenv("KAREN_CSRF_SECRET", raising=False)
    monkeypatch.delenv("KAREN_SCOPE_DELETION_KEY", raising=False)
    monkeypatch.delenv("DEBATA_SCOPE_DELETION_KEY", raising=False)
    monkeypatch.setattr(config_transaction, "_CSRF_SECRET", None)
    csrf_key = config_transaction._csrf_secret()
    scope_path.mkdir()
    scope_key = _get_secret_key(scope_path)

    monkeypatch.delattr(os, "fchmod")
    monkeypatch.setattr(config_transaction, "_CSRF_SECRET", None)
    assert config_transaction._csrf_secret() == csrf_key
    assert _get_secret_key(scope_path) == scope_key


def test_chat_pagination_keeps_caller_owned_karen_db_open(tmp_path) -> None:
    from memory.karen_db import KarenDB

    database = KarenDB(tmp_path / "memory.db")
    database.ensure_ready()
    manager = ChatPaginationManager(database)
    request = ChatPageRequest(persona_id="persona")

    manager.fetch_page(request)
    manager.fetch_page(request)

    assert database.connect().execute("SELECT 1").fetchone()[0] == 1
    database.close()


def test_persona_import_rejects_symlinks_and_invalid_prompt_before_publish(tmp_path) -> None:
    source = tmp_path / "source-persona"
    source.mkdir()
    (source / "persona_prompt.py").write_text(
        'PERSONA_PROMPT = "safe"\n', encoding="utf-8"
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    (source / "linked.txt").symlink_to(outside)
    destination = tmp_path / "personas"

    with pytest.raises(PersonaImportError, match="符号链接"):
        copy_persona_dir(source, destination)
    assert not (destination / source.name).exists()

    (source / "linked.txt").unlink()
    (source / "persona_prompt.py").write_text(
        "from pathlib import Path\nPERSONA_PROMPT = 'unsafe'\n",
        encoding="utf-8",
    )
    with pytest.raises(PersonaImportError, match="静态"):
        copy_persona_dir(source, destination)
    assert not (destination / source.name).exists()

    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(
            "unsafe/persona_prompt.py",
            "import os\nPERSONA_PROMPT = 'unsafe'\n",
        )
    with pytest.raises(PersonaImportError, match="静态"):
        import_persona_zip(archive, destination)
    assert not (destination / "unsafe").exists()
    assert not list(destination.glob(".persona-import-*"))


def test_copy_persona_dir_enforces_entry_and_total_byte_limits(tmp_path, monkeypatch) -> None:
    from agents import persona_import

    source = tmp_path / "bounded-persona"
    source.mkdir()
    prompt = source / "persona_prompt.py"
    prompt.write_text('PERSONA_PROMPT = "ok"\n', encoding="utf-8")
    destination = tmp_path / "personas"

    monkeypatch.setattr(persona_import, "MAX_PERSONA_ZIP_ENTRIES", 1)
    (source / "asset.txt").write_text("asset", encoding="utf-8")
    with pytest.raises(PersonaImportError, match="文件数量"):
        copy_persona_dir(source, destination)
    assert not (destination / source.name).exists()

    (source / "asset.txt").unlink()
    monkeypatch.setattr(persona_import, "MAX_PERSONA_ZIP_ENTRIES", 512)
    monkeypatch.setattr(persona_import, "MAX_PERSONA_TOTAL_BYTES", 4)
    with pytest.raises(PersonaImportError, match="总大小"):
        copy_persona_dir(source, destination)
    assert not (destination / source.name).exists()
    assert not list(destination.glob(".persona-import-*"))


@pytest.mark.asyncio
async def test_persona_export_reports_nested_symlink_as_bad_request(tmp_path, monkeypatch) -> None:
    root = tmp_path / "personas"
    persona_dir = root / "linked"
    persona_dir.mkdir(parents=True)
    (persona_dir / "persona_prompt.py").write_text(
        'PERSONA_PROMPT = "safe"\n', encoding="utf-8"
    )
    (persona_dir / "linked.txt").symlink_to(tmp_path / "outside.txt")
    runtime = SimpleNamespace(paths=SimpleNamespace(PERSONAS_DIR=root))
    monkeypatch.setattr(personas, "require_runtime", lambda _request: runtime)

    with pytest.raises(HTTPException) as error:
        await personas.export_persona(object(), "linked")
    assert error.value.status_code == 400


def test_public_provider_dns_answers_are_pinned_and_restricted(monkeypatch) -> None:
    import socket

    from providers import safe_http

    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ],
    )
    pinned, authority, hostname = safe_http.pin_public_url(
        "https://api.example.test/v1"
    )
    assert pinned == "https://8.8.8.8/v1"
    assert authority == "api.example.test"
    assert hostname == "api.example.test"

    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(UnsafeProviderTarget, match="不能探测内网"):
        safe_http.pin_public_url("https://api.example.test/v1")


@pytest.mark.asyncio
async def test_public_provider_request_does_not_follow_redirects_or_use_dns_again(
    monkeypatch,
) -> None:
    import socket

    from providers import safe_http
    from providers.health import _client_kwargs

    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ],
    )
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await send_provider_request(
            client,
            "GET",
            "https://api.example.test/models",
            headers={"Authorization": "Bearer secret", "host": "127.0.0.1"},
            public_only=True,
        )

    assert response.status_code == 302
    assert len(seen) == 1
    assert seen[0].url.host == "8.8.8.8"
    assert seen[0].headers["host"] == "api.example.test"
    assert seen[0].extensions["sni_hostname"] == "api.example.test"
    assert _client_kwargs(1, public_only=True)["trust_env"] is False


@pytest.mark.asyncio
async def test_public_provider_request_tries_each_validated_public_ip(monkeypatch) -> None:
    import socket

    from providers import safe_http

    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
        ],
    )
    attempted: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        attempted.append(request.url.host)
        if request.url.host == "8.8.8.8":
            raise httpx.ConnectError("first address is unavailable", request=request)
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await send_provider_request(
            client,
            "GET",
            "https://api.example.test/models",
            headers={"Authorization": "Bearer secret"},
            public_only=True,
        )

    assert response.status_code == 200
    assert attempted == ["8.8.8.8", "1.1.1.1"]


@pytest.mark.asyncio
async def test_docker_cli_streams_stdout_with_a_hard_limit(tmp_path) -> None:
    output_path = tmp_path / "export.bin"
    with output_path.open("wb") as output:
        result = await DockerCLI(sys.executable).run(
            ["-c", "import sys; sys.stdout.buffer.write(b'abcdef')"],
            timeout=5,
            max_output_bytes=1024,
            stdout_file=output,
            stdout_limit=4,
        )

    assert output_path.read_bytes() == b"abcd"
    assert result.truncated


@pytest.mark.asyncio
async def test_container_export_removes_partial_temp_file_on_size_error(
    tmp_path, monkeypatch
) -> None:
    from features.container.docker_cli import CommandResult

    service = ContainerService(
        SimpleNamespace(enabled=True, file_size_mb=1, storage_mode="volume_soft"),
        AppPaths(project_root=tmp_path),
    )

    async def ensure_running() -> None:
        return None

    monkeypatch.setattr(service, "ensure_running", ensure_running)
    commands: list[list[str]] = []

    async def run(args, **_kwargs):
        commands.append(args)
        return CommandResult(returncode=3, stdout="", stderr="")

    monkeypatch.setattr(service, "_run", run)
    with pytest.raises(ContainerError) as error:
        await service.export_file(
            "/workspace/data.bin", "export.bin", workspace_dir=tmp_path
        )
    assert getattr(error.value, "code", "") == "container_file_too_large"
    assert not (tmp_path / "export.bin").exists()
    assert not list(tmp_path.glob(".export.bin.*.tmp"))
    assert len(commands) == 1
    assert "python3" in commands[0]


def test_container_export_reader_rejects_symlink_paths_and_oversize_files(
    tmp_path,
) -> None:
    content = tmp_path / "content.bin"
    content.write_bytes(b"data")
    result = subprocess.run(
        [sys.executable, "-I", "-c", _EXPORT_FILE_SCRIPT, str(content), "8"],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == b"data"

    too_small = subprocess.run(
        [sys.executable, "-I", "-c", _EXPORT_FILE_SCRIPT, str(content), "2"],
        capture_output=True,
        check=False,
    )
    assert too_small.returncode == 3
    assert not too_small.stdout

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    via_link = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _EXPORT_FILE_SCRIPT,
            str(tmp_path / "linked" / "secret.txt"),
            "128",
        ],
        capture_output=True,
        check=False,
    )
    assert via_link.returncode != 0
    assert not via_link.stdout


@pytest.mark.asyncio
async def test_remote_chat_html_and_svg_are_attachments(monkeypatch) -> None:
    monkeypatch.setattr(chats, "safe_remote_media_url", lambda _url: "https://qq.test/media")

    async def download(_url, **_kwargs):
        return b"<svg></svg>"

    monkeypatch.setattr(chats, "download_remote_media_bytes", download)
    monkeypatch.setattr(chats, "download_remote_image_bytes", download)
    for name, kind in (("payload.html", "file"), ("payload.svg", "image")):
        response = await chats.remote_attachment(
            url="https://qq.test/source",
            name=name,
            kind=kind,
            inline=True,
        )
        assert response.headers["content-disposition"].startswith("attachment;")


@pytest.mark.asyncio
async def test_chat_page_does_not_create_missing_persona_database(
    tmp_path, monkeypatch
) -> None:
    paths = SimpleNamespace(
        memory_dir_for=lambda persona: tmp_path / "memory" / persona
    )
    runtime = SimpleNamespace(paths=paths, persona=SimpleNamespace(name="test"))
    monkeypatch.setattr(chats, "require_runtime", lambda _request: runtime)

    result = await chats.chat_page(object())

    assert result["items"] == []
    assert not (tmp_path / "memory" / "test" / "test.db").exists()
