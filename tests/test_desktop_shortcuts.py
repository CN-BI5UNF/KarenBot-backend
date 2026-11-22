"""更新后修复安装器桌面快捷方式图标的测试。"""

from __future__ import annotations

from pathlib import Path

from backend.module_updater import repair_desktop_shortcut_icons

ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<text x="32" y="45">K</text></svg>\n'
)

OLD_ENTRY = """[Desktop Entry]
Type=Application
Name=KarenBot GUI
Name[zh_CN]=KarenBot 桌面控制台
Comment=KarenBot
Exec=/opt/karenbot/launch-gui.sh
Path=/opt/karenbot
Icon=applications-system
Terminal=false
Categories=Utility;Chat;
"""


def _project(tmp_path: Path, *, with_webui_icon: bool = True) -> Path:
    root = tmp_path / "app"
    (root / "packaging").mkdir(parents=True)
    if with_webui_icon:
        icon_dir = root / "webui" / "static"
        icon_dir.mkdir(parents=True)
        (icon_dir / "favicon.svg").write_text(ICON, encoding="utf-8")
    else:
        (root / "packaging" / "karenbot.svg").write_text(ICON, encoding="utf-8")
    return root


def _home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    applications = home / ".local" / "share" / "applications"
    applications.mkdir(parents=True)
    return home, applications


def test_repairs_old_icon_and_installs_hicolor_icon(tmp_path, monkeypatch) -> None:
    home, applications = _home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    entry = applications / "karenbot-gui.desktop"
    entry.write_text(OLD_ENTRY, encoding="utf-8")

    result = repair_desktop_shortcut_icons(_project(tmp_path))

    text = entry.read_text(encoding="utf-8")
    assert "Icon=karenbot" in text
    assert "applications-system" not in text
    assert result["changed"] == [str(entry)]
    icon = (
        home
        / ".local"
        / "share"
        / "icons"
        / "hicolor"
        / "scalable"
        / "apps"
        / "karenbot.svg"
    )
    assert icon.read_text(encoding="utf-8") == ICON
    assert str(icon) in result["icons"]


def test_repair_is_idempotent(tmp_path, monkeypatch) -> None:
    home, applications = _home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    entry = applications / "karenbot-webui.desktop"
    entry.write_text(OLD_ENTRY.replace("GUI", "WebUI"), encoding="utf-8")
    project = _project(tmp_path)

    first = repair_desktop_shortcut_icons(project)
    second = repair_desktop_shortcut_icons(project)

    assert first["changed"]
    assert second["changed"] == []
    assert second["icons"] == []


def test_leaves_user_custom_icon_alone(tmp_path, monkeypatch) -> None:
    home, applications = _home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    entry = applications / "karenbot-gui.desktop"
    entry.write_text(
        OLD_ENTRY.replace(
            "Icon=applications-system", "Icon=/home/user/Desktop/my-karen.svg"
        ),
        encoding="utf-8",
    )

    result = repair_desktop_shortcut_icons(_project(tmp_path))

    assert result["changed"] == []
    assert result["icons"] == []
    assert "Icon=/home/user/Desktop/my-karen.svg" in entry.read_text(
        encoding="utf-8"
    )


def test_missing_icon_source_skips_repair(tmp_path, monkeypatch) -> None:
    home, applications = _home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    entry = applications / "karenbot-gui.desktop"
    entry.write_text(OLD_ENTRY, encoding="utf-8")
    root = tmp_path / "app"
    root.mkdir()

    result = repair_desktop_shortcut_icons(root)

    assert result["skipped"] == "icon_missing"
    assert result["changed"] == []
    assert "Icon=applications-system" in entry.read_text(encoding="utf-8")


def test_karenbot_icon_entry_gets_missing_icon_file(tmp_path, monkeypatch) -> None:
    home, applications = _home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    entry = applications / "karenbot-gui-safe.desktop"
    entry.write_text(
        OLD_ENTRY.replace("Icon=applications-system", "Icon=karenbot"),
        encoding="utf-8",
    )

    result = repair_desktop_shortcut_icons(_project(tmp_path))

    assert result["changed"] == []
    icon = (
        home
        / ".local"
        / "share"
        / "icons"
        / "hicolor"
        / "scalable"
        / "apps"
        / "karenbot.svg"
    )
    assert icon.is_file()
    assert str(icon) in result["icons"]
