"""ExternalProgramManager 的隔离、停止策略与平台命令形状。"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from app_config.schema import ExternalProgramConfig
from core import programs as programs_module
from core.programs import ExternalProgramManager


def _sleep_cfg(name: str = "good", **overrides) -> ExternalProgramConfig:
    data = {
        "name": name,
        "executable": sys.executable,
        "args": ["-c", "import time; time.sleep(30)"],
        "startup_wait_seconds": 0.2,
    }
    data.update(overrides)
    return ExternalProgramConfig.model_validate(data)


async def _wait_state(manager: ExternalProgramManager, program_id: str, state: str, timeout: float = 3.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        item = next(
            (entry for entry in manager.status() if entry["id"] == program_id),
            None,
        )
        if item is not None and item["state"] == state:
            return item
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"{program_id} did not reach {state}: {item}")
        await asyncio.sleep(0.02)


async def test_start_failure_is_isolated_and_never_stopped() -> None:
    manager = ExternalProgramManager(".", graceful_shutdown_timeout=1.0)
    good = _sleep_cfg()
    bad = ExternalProgramConfig(
        name="bad",
        executable=str(Path("/definitely/missing/karenbot-program")),
        startup_wait_seconds=0.05,
    )

    result = await manager.start_all({"good": good, "bad": bad})
    await _wait_state(manager, "good", "running")

    status = {item["id"]: item for item in manager.status()}
    assert status["bad"]["state"] == "failed"
    assert status["bad"]["failure_seq"] == 1
    assert status["bad"]["managed"] is False
    assert result["errors"].get("bad")
    assert manager.status()[0]["state"] == "running"

    good_process = manager._runtimes["good"].process
    assert good_process is not None
    await manager.stop_all()
    assert good_process.returncode is not None


async def test_stop_with_karen_false_leaves_process_running() -> None:
    manager = ExternalProgramManager(".", graceful_shutdown_timeout=1.0)
    cfg = _sleep_cfg(stop_with_karen=False)
    await manager.start_all({"good": cfg})
    await _wait_state(manager, "good", "running")
    try:
        await manager.stop_all()
        status = manager.status()[0]
        assert status["state"] == "running"
        assert status["managed"] is True
    finally:
        await manager.stop_one("good", manual=True)


async def test_probe_skips_launch_and_never_stops_external() -> None:
    def _close(
        _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer.close()

    server = await asyncio.start_server(_close, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    manager = ExternalProgramManager(".", graceful_shutdown_timeout=1.0)
    cfg = ExternalProgramConfig(
        name="external",
        executable=str(Path("/definitely/missing/karenbot-program")),
        probe_port=port,
        startup_wait_seconds=0.05,
    )
    try:
        await manager.start_all({"external": cfg})
        status = manager.status()[0]
        assert status["state"] == "external"
        assert status["managed"] is False
        assert status["failure_seq"] == 0

        await manager.stop_all()
        assert manager.status()[0]["state"] == "external"
        assert server.is_serving()
    finally:
        server.close()
        await server.wait_closed()


async def test_quick_exit_marks_failed_with_one_episode() -> None:
    manager = ExternalProgramManager(".", graceful_shutdown_timeout=1.0)
    cfg = ExternalProgramConfig(
        name="exit-fast",
        executable=sys.executable,
        args=["-c", "import sys; sys.exit(3)"],
        startup_wait_seconds=0.2,
    )

    await manager.start_all({"fast": cfg})
    item = await _wait_state(manager, "fast", "failed")
    assert item["failure_seq"] == 1
    assert "立即退出" in item["error"]
    await manager.stop_all()


async def test_auto_restart_retries_and_keeps_one_failure_episode() -> None:
    manager = ExternalProgramManager(
        ".", graceful_shutdown_timeout=1.0, restart_delay=0.05
    )
    cfg = ExternalProgramConfig(
        name="flaky",
        executable=sys.executable,
        args=["-c", "import sys; sys.exit(4)"],
        startup_wait_seconds=0.05,
        auto_restart=True,
    )

    await manager.start_all({"flaky": cfg})
    deadline = asyncio.get_running_loop().time() + 3.0
    while True:
        item = manager.status()[0]
        if item["restarts"] >= 2:
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(item)
        await asyncio.sleep(0.02)
    assert item["failure_seq"] == 1
    await manager.stop_all()


async def test_reconcile_disables_and_removes_managed_program() -> None:
    manager = ExternalProgramManager(".", graceful_shutdown_timeout=1.0)
    cfg = _sleep_cfg()
    await manager.start_all({"good": cfg})
    await _wait_state(manager, "good", "running")

    disabled = cfg.model_copy(update={"enabled": False})
    result = await manager.reconcile({"good": disabled})
    assert "good" in result["stopped"]
    assert manager.status()[0]["state"] == "disabled"

    await manager.reconcile({"good": cfg})
    await _wait_state(manager, "good", "running")

    result = await manager.reconcile({})
    assert "good" in result["stopped"]
    assert manager.status() == []


async def test_reconcile_name_change_does_not_restart() -> None:
    manager = ExternalProgramManager(".", graceful_shutdown_timeout=1.0)
    cfg = _sleep_cfg()
    await manager.start_all({"good": cfg})
    first = await _wait_state(manager, "good", "running")

    renamed = cfg.model_copy(update={"name": "renamed"})
    await manager.reconcile({"good": renamed})
    second = await _wait_state(manager, "good", "running")

    assert first["pid"] == second["pid"]
    assert second["name"] == "renamed"
    await manager.stop_all()


async def test_reconcile_launch_change_restarts() -> None:
    manager = ExternalProgramManager(".", graceful_shutdown_timeout=1.0)
    cfg = _sleep_cfg()
    await manager.start_all({"good": cfg})
    first = await _wait_state(manager, "good", "running")

    changed = cfg.model_copy(
        update={"args": ["-c", "import time; time.sleep(31)"]}
    )
    await manager.reconcile({"good": changed})
    deadline = asyncio.get_running_loop().time() + 3.0
    while True:
        item = await _wait_state(manager, "good", "running")
        if item["pid"] != first["pid"]:
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(item)
        await asyncio.sleep(0.02)
    await manager.stop_all()


async def test_windows_taskkill_command_shape(monkeypatch) -> None:
    manager = ExternalProgramManager(".", graceful_shutdown_timeout=1.0)
    monkeypatch.setattr(programs_module, "_is_windows", lambda: True)
    calls: list[list[str]] = []

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", None

    async def fake_exec(*argv, **_kwargs):
        calls.append(list(argv))
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runtime = programs_module._ProgramRuntime(
        program_id="x", config=_sleep_cfg()
    )

    await manager._taskkill(123, force=False, runtime=runtime)
    await manager._taskkill(123, force=True, runtime=runtime)

    assert calls[0] == ["taskkill", "/PID", "123", "/T"]
    assert calls[1] == ["taskkill", "/F", "/PID", "123", "/T"]


async def test_windows_bat_uses_comspec(monkeypatch, tmp_path) -> None:
    manager = ExternalProgramManager(".", graceful_shutdown_timeout=1.0)
    monkeypatch.setattr(programs_module, "_is_windows", lambda: True)
    bat = tmp_path / "launch.bat"
    bat.write_text("@echo off\n", encoding="utf-8")
    captured: list[list[str]] = []

    class FakeProcess:
        pid = 9
        stdout = None
        returncode = None

    async def fake_exec(*argv, **_kwargs):
        captured.append(list(argv))
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runtime = programs_module._ProgramRuntime(
        program_id="bat",
        config=ExternalProgramConfig(
            name="bat", executable=str(bat), args=["--fast"]
        ),
    )

    await manager._spawn(runtime)

    assert captured
    assert captured[0][0].lower().endswith("cmd.exe")
    assert captured[0][1] == "/c"
    assert captured[0][2] == str(bat)
    assert captured[0][3] == "--fast"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX signal semantics")
async def test_posix_terminate_uses_process_group(monkeypatch) -> None:
    manager = ExternalProgramManager(".", graceful_shutdown_timeout=1.0)
    signals: list[int] = []

    class FakeProcess:
        pid = 4321
        returncode = None

        async def wait(self):
            return 0

    def fake_killpg(pid, sig):
        assert pid == 4321
        signals.append(sig)

    monkeypatch.setattr(os, "killpg", fake_killpg)
    runtime = programs_module._ProgramRuntime(
        program_id="x", config=_sleep_cfg()
    )

    assert await manager._terminate(runtime, FakeProcess()) is True
    assert signals == [signal.SIGTERM]
