"""启动横幅的局域网风险提示测试。"""

from __future__ import annotations

from backend.auth import TokenAuth, format_token_banner


def _banner(host: str) -> str:
    return format_token_banner(
        TokenAuth("test-token", enabled=True, source="test"),
        host=host,
        port=8000,
        urls=[f"http://{host}:8000"],
    )


def test_loopback_banner_has_no_lan_warning() -> None:
    assert "已对局域网开放" not in _banner("127.0.0.1")


def test_wildcard_banner_warns_about_lan_exposure() -> None:
    text = _banner("0.0.0.0")
    assert "已对局域网开放" in text
    assert "请确认网络可信" in text
