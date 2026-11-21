"""Distribution updater for the KarenBot backend + GUI/WebUI release train.

The legacy :mod:`core.updater` updates one combined git checkout.  Since v2,
KarenBot publishes one unified release from the ``CN-BI5UNF/KarenBot``
repository:

* ``modules.json`` describes the release and the three component archives;
* ``KarenBot-backend-<tag>.tar.gz`` provides the complete backend program;
* ``KarenBot-GUI-<tag>.tar.gz`` provides ``gui/``;
* ``KarenBot-WebUI-<tag>.tar.gz`` provides ``webui/``.

Updates are applied as a file-level overlay: program files are written, but
nothing is deleted.  User data (``data/``, ``personas/``, ``plugins/``,
``config.yaml``, ...) is never part of the update whitelist.  Uncommitted
local changes no longer block the update, but every overwritten file is backed
up first so local edits can be recovered.

GitHub Releases are the source of truth so the updater can distinguish
``Latest`` from ``Pre-release``.  Automatic notifications only use Latest
releases; Pre-releases remain selectable manually after a strong warning.
Every release train updates the backend and the active frontend together;
selecting an individual component never applies half a release.

Authentication for repositories is resolved from ``KAREN_GITHUB_TOKEN`` /
``GH_TOKEN`` / ``GITHUB_TOKEN`` or ``gh auth token``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
DISTRIBUTION_REPO = "CN-BI5UNF/KarenBot"
DISTRIBUTION_MANIFEST = "modules.json"
DISTRIBUTION_TAG_PREFIX = "distribution:"
DISTRIBUTION_ASSET_NAMES = {
    "backend": "KarenBot-backend-{tag}.tar.gz",
    "gui": "KarenBot-GUI-{tag}.tar.gz",
    "webui": "KarenBot-WebUI-{tag}.tar.gz",
}
DISTRIBUTION_LABEL = "统一发行版"
MODULE_REPOS = {
    "backend": "CN-BI5UNF/KarenBot-backend",
    "gui": "CN-BI5UNF/KarenBot-GUI",
    "webui": "CN-BI5UNF/KarenBot-WebUI",
}
MODULE_DIRS = {"backend": "backend", "gui": "gui", "webui": "webui"}
MODULE_LABELS = {"backend": "后端", "gui": "GUI 前端", "webui": "WebUI 前端"}

# KarenBot-backend 发布包包含完整后端程序。更新时只覆盖这些程序条目，
# 其余顶层条目一律保留（用户数据、人格、配置、插件、备份等）。
BACKEND_UPDATE_ENTRIES = (
    "backend",
    "core",
    "memory",
    "mind",
    "agents",
    "providers",
    "tools",
    "utils",
    "features",
    "adapters",
    "app_config",
    "model_plugins",
    "deploy",
    "docs",
    "scripts",
    "packaging",
    "tests",
    "main.py",
    "main_cli.py",
    "service_cli.py",
    "webui_server.py",
    "karen",
    "debata",
    "compatibility.json",
    "VERSION",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "README_EN.md",
    "CHANGELOG.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "NOTICE",
)
# 兼容/展示用：这些顶层条目即使出现在发布包里也不会被写入。
BACKEND_PRESERVED_ENTRIES = (
    "data",
    "logs",
    "personas",
    "plugins",
    "workspace",
    "config.yaml",
    "config_backups",
    "secrets.enc",
    "secrets.key",
    ".env",
    ".venv",
    "venv",
    ".git",
    ".github",
    ".gitignore",
    "modules.json",
    "release",
    "releases",
    "backups",
    "cache",
    "tmp",
    ".karen-run",
    ".debata-run",
)
FRONTEND_COMPONENT = {"gui": "gui", "webui": "webui"}
FRONTEND_LABELS = {"gui": "GUI 版本", "webui": "WebUI 版本"}
DEFAULT_INTERVAL_DAYS = 9
RETRY_INTERVAL_SECONDS = 6 * 60 * 60
CONFIRM_TTL_SECONDS = 300
RELEASE_CACHE_SECONDS = 300
MAX_RELEASES_PER_COMPONENT = 40
RELEASE_NOTES_MAX_CHARS = 20_000
MAX_TARBALL_BYTES = 200 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MANIFEST_FILENAME = "modules.json"
_STATE_FILENAME = "module_update_state.json"
_TOKEN_ENV_VARS = ("KAREN_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class ModuleUpdateError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _safe_tag(tag: str) -> bool:
    return bool(
        _TAG_RE.match(tag)
        and ".." not in tag
        and not tag.endswith("/")
        and not tag.endswith(".lock")
        and "\\" not in tag
    )


def _is_link_like(path: Path) -> bool:
    """Detect symlinks and Windows reparse points (junctions) portably.

    ``Path.is_junction`` only exists on Python 3.12+, so Windows junctions are
    detected via ``lstat().st_file_attributes`` instead.
    """

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        # If the type cannot be determined, treat it as unsafe.
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    if os.name == "nt":
        reparse_flag = getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        )
        if getattr(info, "st_file_attributes", 0) & reparse_flag:
            return True
    return False


def _parse_git_status_payload(payload: bytes) -> list[tuple[str, str]]:
    """Parse ``git status --porcelain=v1 -z`` into ``(status, path)`` pairs.

    NUL-separated output avoids quoting problems with spaces and non-ASCII
    filenames.  Rename/copy entries carry the original path as an extra token
    which must be skipped.
    """

    tokens = payload.split(b"\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4:
            continue
        status = token[:2].decode("ascii", errors="replace")
        path = token[3:].decode("utf-8", errors="surrogateescape")
        path = path.replace("\\", "/").strip("/")
        if not path:
            continue
        entries.append((status, path))
        if "R" in status or "C" in status:
            index += 1
    return entries


def _version_key(value: str | None):
    """Return a comparable version, tolerating tags such as ``v0.0.1-dev``."""
    text = str(value or "").strip().lstrip("vV")
    if not text:
        return None
    try:
        from packaging.version import Version

        return Version(text)
    except Exception:  # noqa: BLE001
        parts = re.findall(r"\d+|[A-Za-z]+", text)
        numbers: list[int] = []
        pre = ""
        for part in parts:
            if part.isdigit():
                numbers.append(int(part))
            else:
                pre = part.lower()
                break
        while len(numbers) < 4:
            numbers.append(0)
        # Stable sorts above a pre-release with the same numeric core.
        return (tuple(numbers[:4]), 0 if pre else 1, pre)


def _installed_manifest(project_root: Path, frontend: str) -> dict[str, Any]:
    path = project_root / MANIFEST_FILENAME
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                data = value
        except (OSError, ValueError):
            logger.warning("读取 %s 失败，按未记录模块版本处理", path, exc_info=True)
    data.setdefault("schema", 1)
    data.setdefault("frontend", frontend)
    components = data.setdefault("components", {})
    if not isinstance(components, dict):
        components = {}
        data["components"] = components
    wanted = ["backend", FRONTEND_COMPONENT[frontend]]
    for component in wanted:
        entry = components.get(component)
        if not isinstance(entry, dict):
            entry = {}
            components[component] = entry
        entry.setdefault("repo", MODULE_REPOS[component])
        entry.setdefault("dir", MODULE_DIRS[component])
        entry.setdefault("tag", "")
        entry.setdefault("version", "")
    return data


def _write_manifest_atomic(project_root: Path, manifest: dict[str, Any]) -> None:
    path = project_root / MANIFEST_FILENAME
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_module_version(project_root: Path, component: str) -> str:
    if component == "backend":
        # 完整后端发布包把 VERSION 放在仓库根目录；旧布局的
        # backend/VERSION 作为后备。
        candidates = [
            project_root / "VERSION",
            project_root / MODULE_DIRS[component] / "VERSION",
        ]
    else:
        candidates = [project_root / MODULE_DIRS[component] / "VERSION"]
    for path in candidates:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def _release_body(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > RELEASE_NOTES_MAX_CHARS:
        text = text[:RELEASE_NOTES_MAX_CHARS] + "\n\n…（更新日志过长，已截断）"
    return text


def _published_key(value: Any) -> str:
    return str(value or "")


def _distribution_version(tag: str) -> str:
    return str(tag or "").strip().lstrip("vV")


def _expected_asset_name(component: str, tag: str) -> str:
    template = DISTRIBUTION_ASSET_NAMES[component]
    return template.format(tag=tag)


def _find_asset(release: dict[str, Any], name: str) -> dict[str, Any] | None:
    for asset in release.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        if str(asset.get("name") or "") == name:
            return asset
    return None


def _manifest_asset(release: dict[str, Any]) -> dict[str, Any] | None:
    return _find_asset(release, DISTRIBUTION_MANIFEST)


def _validate_distribution_manifest(
    release: dict[str, Any],
    raw: Any,
    frontend: str,
) -> dict[str, Any]:
    """Validate the release manifest and return normalised component metadata.

    The manifest is downloaded from GitHub, but it is still treated as
    untrusted data: every asset name, tag, version and hash is checked before
    anything is staged or written.
    """

    tag = str(release.get("tag") or "").strip()
    if not _safe_tag(tag):
        raise ModuleUpdateError("module_manifest_invalid", "发行版 tag 不合法")
    if not isinstance(raw, dict):
        raise ModuleUpdateError("module_manifest_invalid", "modules.json 不是对象")
    if raw.get("schema") != 1:
        raise ModuleUpdateError(
            "module_manifest_invalid",
            f"modules.json schema 不受支持：{raw.get('schema')!r}",
        )
    if str(raw.get("release") or "").strip() != tag:
        raise ModuleUpdateError(
            "module_manifest_invalid",
            "modules.json 的 release 与 GitHub Release tag 不一致",
        )
    components = raw.get("components")
    if not isinstance(components, dict):
        raise ModuleUpdateError("module_manifest_invalid", "modules.json 缺少 components")
    wanted = ("backend", FRONTEND_COMPONENT[frontend])
    normalised: dict[str, dict[str, Any]] = {}
    for component in wanted:
        entry = components.get(component)
        if not isinstance(entry, dict):
            raise ModuleUpdateError(
                "module_manifest_invalid",
                f"modules.json 缺少 {component} 组件",
            )
        component_tag = str(entry.get("tag") or "").strip()
        version = str(entry.get("version") or "").strip()
        asset_name = str(entry.get("asset") or "").strip()
        digest = str(entry.get("sha256") or "").strip().lower()
        size = entry.get("size")
        if not _safe_tag(component_tag):
            raise ModuleUpdateError(
                "module_manifest_invalid",
                f"{component} 的 tag 不合法",
            )
        if not version or version != component_tag.lstrip("vV"):
            raise ModuleUpdateError(
                "module_manifest_invalid",
                f"{component} 的 version 与 tag 不一致",
            )
        expected_name = _expected_asset_name(component, tag)
        if asset_name != expected_name or "/" in asset_name or "\\" in asset_name:
            raise ModuleUpdateError(
                "module_manifest_invalid",
                f"{component} 的资产名不符合契约：{asset_name!r}",
            )
        if not _SHA256_RE.match(digest):
            raise ModuleUpdateError(
                "module_manifest_invalid",
                f"{component} 的 SHA256 不合法",
            )
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ModuleUpdateError(
                "module_manifest_invalid",
                f"{component} 的 size 不合法",
            )
        if size > MAX_TARBALL_BYTES:
            raise ModuleUpdateError(
                "module_too_large",
                f"{component} 组件包超过 200 MiB",
            )
        asset = _find_asset(release, asset_name)
        if asset is None:
            raise ModuleUpdateError(
                "module_asset_missing",
                f"Release 缺少资产 {asset_name}",
            )
        if asset.get("size") not in (None, size):
            raise ModuleUpdateError(
                "module_manifest_invalid",
                f"{component} 资产大小与 GitHub 记录不一致",
            )
        normalised[component] = {
            "repo": MODULE_REPOS[component],
            "tag": component_tag,
            "version": version,
            "asset": asset_name,
            "sha256": digest,
            "size": size,
            "url": str(asset.get("url") or ""),
            "browser_download_url": str(asset.get("browser_download_url") or ""),
        }
    return normalised


class _AuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep the bearer token while GitHub redirects API tarball requests."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        new_request = super().redirect_request(
            req, fp, code, msg, headers, newurl
        )
        if new_request is None:
            return None
        auth = req.get_header("Authorization")
        host = urllib.parse.urlsplit(newurl).hostname or ""
        if auth and (
            host == "api.github.com"
            or host == "github.com"
            or host.endswith(".github.com")
            or host.endswith(".githubusercontent.com")
        ):
            new_request.add_header("Authorization", auth)
        return new_request


class GitHubClient:
    """Small synchronous GitHub REST client (run it in a worker thread)."""

    def __init__(
        self,
        *,
        token: str | None = None,
        api_base: str = GITHUB_API_BASE,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.token = str(token or "").strip() or self._discover_token()
        self.api_base = api_base.rstrip("/")
        self.timeout_seconds = max(5.0, float(timeout_seconds))
        self._opener = urllib.request.build_opener(_AuthRedirectHandler())

    @staticmethod
    def _discover_token() -> str:
        for name in _TOKEN_ENV_VARS:
            value = os.environ.get(name, "").strip()
            if value:
                return value
        candidates = []
        found = shutil.which("gh")
        if found:
            candidates.append(found)
        home = Path.home()
        candidates.extend(
            str(path)
            for path in (
                home / ".local" / "bin" / "gh",
                Path("/usr/bin/gh"),
                Path("/usr/local/bin/gh"),
            )
            if path.is_file()
        )
        seen: set[str] = set()
        for gh in candidates:
            if gh in seen:
                continue
            seen.add(gh)
            try:
                result = subprocess.run(
                    [gh, "auth", "token"],
                    capture_output=True,
                    text=True,
                    timeout=6,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            token = result.stdout.strip()
            if result.returncode == 0 and token:
                return token
        return ""

    def _headers(self, *, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": "KarenBot-Module-Updater/1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _get_json(self, path: str) -> Any:
        url = self.api_base + path
        request = urllib.request.Request(url, headers=self._headers())
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code == 401:
                raise ModuleUpdateError(
                    "github_auth_required",
                    "GitHub 拒绝了凭据；请重新执行 gh auth login 或设置 KAREN_GITHUB_TOKEN",
                ) from None
            if exc.code == 403:
                raise ModuleUpdateError(
                    "github_rate_limited",
                    "GitHub API 暂时不可用（可能触发限流）；请稍后重试",
                ) from None
            if exc.code == 404:
                raise ModuleUpdateError(
                    "github_repo_unavailable",
                    "GitHub 仓库不存在或当前凭据无权访问；私有仓库请设置 KAREN_GITHUB_TOKEN 或执行 gh auth login",
                ) from None
            raise ModuleUpdateError(
                "github_http_error",
                f"GitHub API 返回 HTTP {exc.code}：{body or '无响应体'}",
            ) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ModuleUpdateError(
                "github_network_error",
                f"无法连接 GitHub API：{getattr(exc, 'reason', exc)}",
            ) from None
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModuleUpdateError(
                "github_invalid_response", f"GitHub API 返回了无效 JSON：{exc}"
            ) from None

    def releases(self, repo: str) -> list[dict[str, Any]]:
        data = self._get_json(
            f"/repos/{repo}/releases?per_page=100&page=1"
        )
        if not isinstance(data, list):
            raise ModuleUpdateError("github_invalid_response", "GitHub Releases 返回格式不正确")
        result = []
        for item in data:
            if not isinstance(item, dict) or item.get("draft"):
                continue
            tag = str(item.get("tag_name") or "").strip()
            if not tag or not _safe_tag(tag):
                logger.warning("忽略不安全的模块 tag：%r", tag)
                continue
            assets = []
            for asset in item.get("assets") or []:
                if not isinstance(asset, dict):
                    continue
                name = str(asset.get("name") or "").strip()
                if not name or "/" in name or "\\" in name:
                    continue
                assets.append(
                    {
                        "id": asset.get("id"),
                        "name": name,
                        "size": asset.get("size"),
                        "content_type": str(asset.get("content_type") or ""),
                        "url": str(asset.get("url") or ""),
                        "browser_download_url": str(
                            asset.get("browser_download_url") or ""
                        ),
                    }
                )
            result.append(
                {
                    "tag": tag,
                    "name": str(item.get("name") or tag),
                    "body": _release_body(item.get("body")),
                    "prerelease": bool(item.get("prerelease")),
                    "published_at": str(item.get("published_at") or ""),
                    "html_url": str(item.get("html_url") or ""),
                    "tarball_url": str(item.get("tarball_url") or ""),
                    "zipball_url": str(item.get("zipball_url") or ""),
                    "assets": assets,
                    "id": item.get("id"),
                }
            )
        result.sort(
            key=lambda item: (
                _published_key(item.get("published_at")),
                str(item.get("id") or ""),
            ),
            reverse=True,
        )
        return result

    def download_tarball(self, repo: str, tag: str, destination: Path) -> None:
        if not _safe_tag(tag):
            raise ModuleUpdateError("release_invalid", "版本 tag 不合法")
        url = (
            f"{self.api_base}/repos/{repo}/tarball/"
            + urllib.parse.quote(tag, safe="")
        )
        request = urllib.request.Request(
            url,
            headers=self._headers(accept="application/vnd.github+json"),
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                total = 0
                with destination.open("wb") as handle:
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_TARBALL_BYTES:
                            raise ModuleUpdateError(
                                "module_too_large",
                                "模块源码包超过 200 MiB，已拒绝下载",
                            )
                        handle.write(chunk)
        except urllib.error.HTTPError as exc:
            raise ModuleUpdateError(
                "module_download_failed",
                f"下载模块 {repo}@{tag} 失败：HTTP {exc.code}",
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModuleUpdateError(
                "module_download_failed",
                f"下载模块 {repo}@{tag} 失败：{exc}",
            ) from None

    def _download_url(
        self,
        url: str,
        destination: Path,
        *,
        limit: int,
        label: str,
    ) -> None:
        if not url:
            raise ModuleUpdateError("module_asset_missing", f"{label} 没有下载地址")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not (
            parsed.hostname == "api.github.com"
            or parsed.hostname == "github.com"
            or (parsed.hostname or "").endswith(".github.com")
        ):
            raise ModuleUpdateError(
                "module_asset_missing",
                f"{label} 的下载地址不是 GitHub HTTPS 地址",
            )
        request = urllib.request.Request(
            url,
            headers=self._headers(accept="application/octet-stream"),
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                total = 0
                with destination.open("wb") as handle:
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > limit:
                            raise ModuleUpdateError(
                                "module_too_large",
                                f"{label} 超过允许大小，已拒绝下载",
                            )
                        handle.write(chunk)
        except urllib.error.HTTPError as exc:
            raise ModuleUpdateError(
                "module_download_failed",
                f"下载 {label} 失败：HTTP {exc.code}",
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModuleUpdateError(
                "module_download_failed",
                f"下载 {label} 失败：{exc}",
            ) from None

    def download_asset(self, asset: dict[str, Any], destination: Path) -> None:
        name = str(asset.get("name") or "组件包")
        url = str(asset.get("url") or "").strip()
        if not url:
            url = str(asset.get("browser_download_url") or "").strip()
        self._download_url(
            url,
            destination,
            limit=MAX_TARBALL_BYTES,
            label=f"组件资产 {name}",
        )

    def download_json_asset(self, asset: dict[str, Any]) -> Any:
        name = str(asset.get("name") or DISTRIBUTION_MANIFEST)
        url = str(asset.get("url") or "").strip()
        if not url:
            url = str(asset.get("browser_download_url") or "").strip()
        with tempfile.TemporaryDirectory(prefix="karen-manifest-") as temp_dir:
            target = Path(temp_dir) / "manifest.json"
            self._download_url(
                url,
                target,
                limit=MAX_MANIFEST_BYTES,
                label=f"发行版清单 {name}",
            )
            try:
                return json.loads(target.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ModuleUpdateError(
                    "module_manifest_invalid",
                    f"读取 {name} 失败：{exc}",
                ) from None


@dataclass
class _Selection:
    component: str
    tag: str
    public: dict[str, Any]
    components: tuple[str, ...] = field(default_factory=tuple)


class ModuleUpdateService:
    """Update the backend and active frontend from KarenBot release trains."""

    def __init__(
        self,
        project_root: Path,
        data_dir: Path,
        frontend: str,
        *,
        client: GitHubClient | None = None,
        timeout_seconds: int = 60,
        check_interval_days: int = DEFAULT_INTERVAL_DAYS,
        restart_callback: Callable[[], Any] | None = None,
    ) -> None:
        if frontend not in FRONTEND_COMPONENT:
            raise ModuleUpdateError("invalid_frontend", "未知的前端类型")
        self.project_root = Path(project_root).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.frontend = frontend
        if client is None:
            token = ""
            token_file = self.data_dir / "github_token.txt"
            try:
                token = token_file.read_text(encoding="utf-8").strip()
            except OSError:
                token = ""
            client = GitHubClient(
                token=token or None, timeout_seconds=timeout_seconds
            )
        self.client = client
        self.timeout_seconds = max(10, min(300, int(timeout_seconds)))
        self.check_interval_days = max(1, min(365, int(check_interval_days)))
        self.restart_callback = restart_callback
        self._lock = asyncio.Lock()
        self._plan: dict[str, Any] | None = None
        self._catalog_cache: tuple[float, dict[str, Any]] | None = None
        self._state_path = self.data_dir / _STATE_FILENAME

    # ------------------------------------------------------------------
    # Manifest / state
    # ------------------------------------------------------------------
    def _manifest(self) -> dict[str, Any]:
        return _installed_manifest(self.project_root, self.frontend)

    def _installed(self, component: str) -> dict[str, Any]:
        entry = self._manifest()["components"].get(component) or {}
        tag = str(entry.get("tag") or "")
        version = str(entry.get("version") or "")
        if not version and tag:
            # A recorded tag is authoritative.  Do not let a partially
            # installed VERSION file hide a still-pending update.
            version = tag.lstrip("vV")
        if not version:
            version = _read_module_version(self.project_root, component)
        return {
            "tag": tag,
            "version": version,
            "known": bool(tag or version),
            "dir": str(entry.get("dir") or MODULE_DIRS[component]),
            "repo": str(entry.get("repo") or MODULE_REPOS[component]),
        }

    def _read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_state(self, state: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_name(self._state_path.name + ".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._state_path)

    # ------------------------------------------------------------------
    # Release catalog
    # ------------------------------------------------------------------
    def _release_relation(self, version: str, installed: dict[str, Any]) -> str:
        if (
            (installed.get("tag") and installed["tag"] == f"v{version}")
            or (installed.get("version") and installed["version"] == version)
        ):
            return "current"
        installed_key = _version_key(
            installed.get("version") or installed.get("tag")
        )
        release_key = _version_key(version)
        if installed_key is None or release_key is None:
            return "newer"
        return "newer" if release_key > installed_key else "older"

    def _component_release(
        self,
        release: dict[str, Any],
        component: str,
        metadata: dict[str, Any],
        installed: dict[str, Any],
    ) -> dict[str, Any]:
        release_tag = str(release.get("tag") or "")
        version = str(metadata.get("version") or "")
        relation = self._release_relation(version, installed)
        return {
            "id": f"{component}:{release_tag}",
            "tag": f"{component}:{release_tag}",
            "release_tag": release_tag,
            "component_tag": str(metadata.get("tag") or ""),
            "version": version,
            "component": component,
            "component_label": MODULE_LABELS[component],
            "name": str(release.get("name") or release_tag),
            "notes": str(release.get("body") or ""),
            "prerelease": bool(release.get("prerelease")),
            "is_latest_stable": False,
            "is_current": relation == "current",
            "relation": relation,
            "published_at": str(release.get("published_at") or ""),
            "html_url": str(release.get("html_url") or ""),
            "tarball_url": str(release.get("tarball_url") or ""),
            "repo": MODULE_REPOS[component],
            "distribution": True,
            "distribution_tag": release_tag,
            "distribution_repo": DISTRIBUTION_REPO,
            "asset": str(metadata.get("asset") or ""),
            "sha256": str(metadata.get("sha256") or ""),
            "size": metadata.get("size"),
            "compat": {"compatible": True, "message": ""},
        }

    def _distribution_release(
        self,
        release: dict[str, Any],
        components: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        release_tag = str(release.get("tag") or "")
        frontend_component = FRONTEND_COMPONENT[self.frontend]
        installed_backend = self._installed("backend")
        installed_frontend = self._installed(frontend_component)
        backend_relation = self._release_relation(
            components["backend"]["version"], installed_backend
        )
        frontend_relation = self._release_relation(
            components[frontend_component]["version"], installed_frontend
        )
        if backend_relation == "current" and frontend_relation == "current":
            relation = "current"
        elif "newer" in {backend_relation, frontend_relation}:
            relation = "newer"
        else:
            relation = "older"
        component_releases = {
            "backend": self._component_release(
                release, "backend", components["backend"], installed_backend
            ),
            frontend_component: self._component_release(
                release,
                frontend_component,
                components[frontend_component],
                installed_frontend,
            ),
        }
        return {
            "id": f"{DISTRIBUTION_TAG_PREFIX}{release_tag}",
            "tag": f"{DISTRIBUTION_TAG_PREFIX}{release_tag}",
            "release_tag": release_tag,
            "version": _distribution_version(release_tag),
            "component": "distribution",
            "component_label": DISTRIBUTION_LABEL,
            "name": str(release.get("name") or release_tag),
            "notes": str(release.get("body") or ""),
            "prerelease": bool(release.get("prerelease")),
            "is_latest_stable": False,
            "is_current": relation == "current",
            "relation": relation,
            "published_at": str(release.get("published_at") or ""),
            "html_url": str(release.get("html_url") or ""),
            "tarball_url": str(release.get("tarball_url") or ""),
            "repo": DISTRIBUTION_REPO,
            "distribution": True,
            "components": {
                component: {
                    "repo": item.get("repo"),
                    "tag": item.get("tag"),
                    "version": item.get("version"),
                    "asset": item.get("asset"),
                    "sha256": item.get("sha256"),
                    "size": item.get("size"),
                }
                for component, item in components.items()
            },
            "component_releases": component_releases,
            "compat": {"compatible": True, "message": ""},
        }

    async def _fetch_distribution_release(
        self, release: dict[str, Any]
    ) -> dict[str, Any]:
        manifest_asset = _manifest_asset(release)
        if manifest_asset is None:
            raise ModuleUpdateError(
                "module_manifest_invalid",
                "Release 没有 modules.json",
            )
        raw = await asyncio.to_thread(
            self.client.download_json_asset, manifest_asset
        )
        components = _validate_distribution_manifest(
            release, raw, self.frontend
        )
        return self._distribution_release(release, components)

    async def _fetch_catalog(self, *, force: bool = False) -> dict[str, Any]:
        now = time.time()
        cached = self._catalog_cache
        if not force and cached is not None and cached[0] > now:
            return cached[1]
        catalog: dict[str, Any] = {
            "distribution": True,
            "components": {},
            "distribution_releases": [],
            "releases": [],
            "errors": {},
        }
        try:
            releases = await asyncio.to_thread(
                self.client.releases, DISTRIBUTION_REPO
            )
        except ModuleUpdateError as exc:
            releases = []
            catalog["errors"]["distribution"] = f"{exc.code}: {exc.message}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 KarenBot 发行版失败", exc_info=True)
            releases = []
            catalog["errors"]["distribution"] = f"github_error: {exc}"

        distribution_releases: list[dict[str, Any]] = []
        for release in releases:
            if _manifest_asset(release) is None:
                continue
            if len(distribution_releases) >= MAX_RELEASES_PER_COMPONENT:
                break
            try:
                entry = await self._fetch_distribution_release(release)
            except ModuleUpdateError as exc:
                tag = str(release.get("tag") or "?")
                catalog["errors"][f"distribution:{tag}"] = (
                    f"{exc.code}: {exc.message}"
                )
                continue
            distribution_releases.append(entry)

        frontend_component = FRONTEND_COMPONENT[self.frontend]
        for component in ("backend", frontend_component):
            installed = self._installed(component)
            aliases = [
                entry["component_releases"][component]
                for entry in distribution_releases
            ]
            latest_stable = next(
                (item for item in aliases if not item["prerelease"]),
                None,
            )
            latest_prerelease = next(
                (item for item in aliases if item["prerelease"]),
                None,
            )
            if latest_stable is not None:
                latest_stable["is_latest_stable"] = True
            catalog["components"][component] = {
                "component": component,
                "label": MODULE_LABELS[component],
                "repo": MODULE_REPOS[component],
                "installed": installed,
                "latest_stable": latest_stable,
                "latest_prerelease": latest_prerelease,
                "update_available": self._is_newer(latest_stable, installed),
                "prerelease_available": self._is_newer(
                    latest_prerelease, installed
                ),
            }
        for entry in distribution_releases:
            for alias in entry["component_releases"].values():
                alias["is_latest_stable"] = any(
                    alias is component.get("latest_stable")
                    for component in catalog["components"].values()
                )
                catalog["releases"].append(alias)
        catalog["distribution_releases"] = distribution_releases
        catalog["checked_at"] = now
        self._catalog_cache = (now + RELEASE_CACHE_SECONDS, catalog)
        return catalog

    @staticmethod
    def _is_newer(
        release: dict[str, Any] | None, installed: dict[str, Any]
    ) -> bool:
        if release is None:
            return False
        if release.get("is_current"):
            return False
        installed_key = _version_key(
            installed.get("version") or installed.get("tag")
        )
        release_key = _version_key(release.get("version") or release.get("release_tag"))
        if not installed.get("known") or installed_key is None:
            return True
        if release_key is None:
            return True
        return release_key > installed_key

    def _component_summary(self, component: dict[str, Any]) -> dict[str, Any]:
        return {
            "component": component["component"],
            "label": component["label"],
            "repo": component["repo"],
            "installed": component["installed"],
            "latest_stable": component["latest_stable"],
            "latest_prerelease": component["latest_prerelease"],
            "update_available": component["update_available"],
            "prerelease_available": component["prerelease_available"],
        }

    # ------------------------------------------------------------------
    # Public read-only API
    # ------------------------------------------------------------------
    def _version_label(self) -> str:
        backend = self._installed("backend")
        frontend = self._installed(FRONTEND_COMPONENT[self.frontend])
        backend_text = backend.get("version") or backend.get("tag") or "未记录"
        frontend_text = frontend.get("version") or frontend.get("tag") or "未记录"
        return (
            f"{FRONTEND_LABELS[self.frontend]} · 后端 {backend_text} · "
            f"前端 {frontend_text}"
        )

    def _worktree_summary(self) -> dict[str, Any]:
        """统计工作树改动/未改动文件数；非 Git 安装返回 available=False。

        ``dirty_count`` 是已改动+未跟踪文件总数，``tracked_dirty_count``
        只统计已跟踪文件的改动，``clean_count`` 是受控文件中未改动的数量。
        """

        if not (self.project_root / ".git").exists():
            return {
                "available": False,
                "dirty": False,
                "dirty_count": 0,
                "tracked_dirty_count": 0,
                "untracked_count": 0,
                "clean_count": 0,
                "tracked_count": 0,
                "clean_percent": 0.0,
                "text": "非 Git 安装，无法统计工作树文件",
            }
        try:
            status = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.project_root),
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=normal",
                ],
                capture_output=True,
                timeout=15,
                check=False,
            )
            tracked = subprocess.run(
                ["git", "-C", str(self.project_root), "ls-files", "-z"],
                capture_output=True,
                timeout=15,
                check=False,
            )
            if status.returncode != 0 or tracked.returncode != 0:
                raise OSError("git status/ls-files failed")
            entries = _parse_git_status_payload(status.stdout)
            tracked_count = len(
                [path for path in tracked.stdout.split(b"\0") if path]
            )
        except (OSError, subprocess.SubprocessError):
            return {
                "available": True,
                "dirty": True,
                "dirty_count": 0,
                "tracked_dirty_count": 0,
                "untracked_count": 0,
                "clean_count": 0,
                "tracked_count": 0,
                "clean_percent": 0.0,
                "error": "git_status_failed",
                "text": "无法读取 Git 工作树状态",
            }
        untracked = sum(1 for status_text, _path in entries if status_text == "??")
        changed_tracked = len(entries) - untracked
        clean = max(0, tracked_count - changed_tracked)
        percent = round(clean / tracked_count * 100, 2) if tracked_count else 0.0
        dirty_count = len(entries)
        return {
            "available": True,
            "dirty": bool(entries),
            "dirty_count": dirty_count,
            "tracked_dirty_count": changed_tracked,
            "untracked_count": untracked,
            "clean_count": clean,
            "tracked_count": tracked_count,
            "clean_percent": percent,
            "text": (
                f"工作树共 {tracked_count} 个受控文件："
                f"未改动 {clean} 个、已改动 {changed_tracked} 个、"
                f"未跟踪 {untracked} 个"
                "（相对本地 Git HEAD，可能包含以前更新写入但未提交的文件）"
            ),
        }

    @staticmethod
    def _worktree_text(worktree: dict[str, Any]) -> str:
        if worktree.get("text"):
            return str(worktree["text"])
        return (
            f"工作树共 {worktree.get('tracked_count', 0)} 个受控文件："
            f"未改动 {worktree.get('clean_count', 0)} 个、"
            f"已改动 {worktree.get('tracked_dirty_count', 0)} 个、"
            f"未跟踪 {worktree.get('untracked_count', 0)} 个"
        )

    async def version_info(self) -> dict[str, Any]:
        return {
            "mode": "module",
            "distribution": True,
            "frontend": self.frontend,
            "version_label": self._version_label(),
            "repo": DISTRIBUTION_REPO,
            "target_ref": "distribution-releases",
            "commit": "",
            "commit_short": "",
            "components": {
                component: self._installed(component)
                for component in ("backend", FRONTEND_COMPONENT[self.frontend])
            },
        }

    async def status(self) -> dict[str, Any]:
        manifest = self._manifest()
        components = {
            component: self._component_summary(
                {
                    "component": component,
                    "label": MODULE_LABELS[component],
                    "repo": MODULE_REPOS[component],
                    "installed": self._installed(component),
                    "latest_stable": None,
                    "latest_prerelease": None,
                    "update_available": False,
                    "prerelease_available": False,
                }
            )
            for component in ("backend", FRONTEND_COMPONENT[self.frontend])
        }
        state = self._read_state()
        worktree = await asyncio.to_thread(self._worktree_summary)
        return {
            "mode": "module",
            "distribution": True,
            "frontend": self.frontend,
            "frontend_label": FRONTEND_LABELS[self.frontend],
            "repos": {
                "distribution": DISTRIBUTION_REPO,
                "backend": MODULE_REPOS["backend"],
                "frontend": MODULE_REPOS[FRONTEND_COMPONENT[self.frontend]],
            },
            "repo": DISTRIBUTION_REPO,
            "target_ref": "distribution-releases",
            "target_branch": "统一发行版",
            "current_branch": f"{self.frontend} release train",
            "current_commit": "",
            "current_commit_short": "",
            "worktree_dirty": worktree.get("dirty", False),
            "worktree": worktree,
            "worktree_text": self._worktree_text(worktree),
            "update_entries": list(BACKEND_UPDATE_ENTRIES),
            "preserved_entries": list(BACKEND_PRESERVED_ENTRIES),
            "data_dir": str(self.data_dir),
            "data_dir_inside_project": self._dir_inside_project(),
            "restart_supported": self.restart_callback is not None,
            "check_interval_days": self.check_interval_days,
            "version_label": self._version_label(),
            "components": components,
            "manifest": manifest,
            "mirror": "",
            "effective_remote": "",
            "mirror_error": "",
            "mirror_presets": [],
            "last_check_at": state.get("last_check_at"),
            "next_check_at": state.get("next_check_at"),
            "last_error": state.get("last_error") or "",
            "suppressed": bool(state.get("suppress_badge")),
        }

    async def available_snapshot(self, *, include_commit: bool = True) -> dict[str, Any]:
        state = self._read_state()
        updates = state.get("updates") or []
        distribution_updates = state.get("distribution_updates") or []
        suppressed = bool(state.get("suppress_badge"))
        return {
            "mode": "module",
            "distribution": True,
            "frontend": self.frontend,
            "version_label": self._version_label(),
            "available": bool(updates) and not suppressed,
            "release": distribution_updates[0] if distribution_updates else (
                updates[0] if updates else None
            ),
            "updates": updates,
            "distribution_updates": distribution_updates,
            "components": state.get("components") or {},
            "suppressed": suppressed,
            "last_attempt_at": state.get("last_attempt_at"),
            "last_check_at": state.get("last_check_at"),
            "next_check_at": state.get("next_check_at"),
            "last_error": state.get("last_error") or "",
            "interval_days": self.check_interval_days,
            "current_commit": "",
            "current_commit_short": "",
        }

    async def set_suppress_badge(self, suppress: bool) -> dict[str, Any]:
        state = self._read_state()
        state["suppress_badge"] = bool(suppress)
        state["suppressed_at"] = time.time() if suppress else None
        self._write_state(state)
        return await self.available_snapshot()

    async def releases(self, *, force: bool = False) -> dict[str, Any]:
        catalog = await self._fetch_catalog(force=force)
        merged = list(catalog.get("releases") or [])
        merged.sort(
            key=lambda item: (item.get("published_at") or "", item.get("tag") or ""),
            reverse=True,
        )
        distribution_releases = list(
            catalog.get("distribution_releases") or []
        )
        distribution_releases.sort(
            key=lambda item: (item.get("published_at") or "", item.get("tag") or ""),
            reverse=True,
        )
        components = {
            key: self._component_summary(value)
            for key, value in catalog["components"].items()
        }
        worktree = await asyncio.to_thread(self._worktree_summary)
        return {
            "mode": "module",
            "distribution": True,
            "frontend": self.frontend,
            "repo": DISTRIBUTION_REPO,
            "target_ref": "distribution-releases",
            "current_commit": "",
            "current_commit_short": "",
            "current_branch": f"{self.frontend} release train",
            "worktree_dirty": worktree.get("dirty", False),
            "worktree": worktree,
            "worktree_text": self._worktree_text(worktree),
            "preserved_entries": list(BACKEND_PRESERVED_ENTRIES),
            "mirror": "",
            "version_label": self._version_label(),
            "interval_days": self.check_interval_days,
            "releases": merged,
            "distribution_releases": distribution_releases,
            "components": components,
            "errors": catalog.get("errors") or {},
        }

    # ------------------------------------------------------------------
    # Check / apply
    # ------------------------------------------------------------------
    async def check(self, target_tag: str | None = None) -> dict[str, Any]:
        catalog = await self._fetch_catalog(force=True)
        distribution_releases = list(
            catalog.get("distribution_releases") or []
        )
        selected_release: dict[str, Any] | None = None
        if target_tag:
            raw = str(target_tag).strip()
            component, sep, tag = raw.partition(":")
            if not sep:
                tag = raw
                component = ""
            if component and component not in {
                "distribution",
                "backend",
                FRONTEND_COMPONENT[self.frontend],
            }:
                raise ModuleUpdateError(
                    "release_not_found",
                    f"远端找不到模块版本 {target_tag}",
                )
            match = next(
                (
                    item
                    for item in distribution_releases
                    if item["release_tag"] == tag
                ),
                None,
            )
            if match is None:
                raise ModuleUpdateError(
                    "release_not_found",
                    f"远端找不到发行版 {target_tag}",
                )
            selected_release = match
        else:
            selected_release = next(
                (
                    item
                    for item in distribution_releases
                    if not item["prerelease"]
                    and item["relation"] == "newer"
                ),
                None,
            )

        selected: list[_Selection] = []
        if selected_release is not None:
            selected.append(
                _Selection(
                    "distribution",
                    selected_release["release_tag"],
                    selected_release,
                    tuple(selected_release["component_releases"].keys()),
                )
            )
        worktree = await asyncio.to_thread(self._worktree_summary)
        warnings: list[str] = []
        if worktree.get("available"):
            warnings.append(
                f"{self._worktree_text(worktree)}。"
                "更新采用文件级叠加：只写入程序文件，data/、personas/、"
                "config.yaml、plugins/ 与备份等用户数据不会改动，也不会删除任何文件。"
            )
        if selected and not worktree.get("available"):
            warnings.append(
                "当前安装没有可用的 Git 工作树，无法统计工作树改动；"
                "更新会先备份再覆盖程序文件，用户数据仍不会改动。"
            )
        elif selected and worktree.get("error"):
            warnings.append(
                "无法读取 Git 工作树状态；更新不会因此被拒绝，"
                "覆盖前会逐个备份被替换的程序文件。"
            )
        elif selected and worktree.get("dirty"):
            warnings.append(
                f"工作树有 {worktree.get('dirty_count', 0)} 个改动/未跟踪文件，"
                "不会阻止更新；每个被发布版替换的程序文件都会先备份到数据目录的 "
                "module_backups/，可从中恢复。"
            )
        if selected_release is not None:
            warnings.append(
                "这是 KarenBot 统一发行版：会同时更新后端与当前前端，"
                "不能只更新其中一个组件。"
            )
            if selected_release.get("prerelease"):
                warnings.append(
                    "这是 GitHub Pre-release 版本，极不稳定，可能包含未完成的接口或数据迁移；"
                    "请先备份数据，并接受它可能无法回滚。"
                )
            if self.frontend == "gui":
                warnings.append(
                    "GUI 前端代码更新后需要完全退出并重新打开应用，仅重启后端不会加载新界面。"
                )
            else:
                warnings.append("WebUI 前端更新后需要刷新浏览器页面。")
        if selected_release is not None:
            token = secrets.token_urlsafe(32)
            self._plan = {
                "token": token,
                "expires": time.time() + CONFIRM_TTL_SECONDS,
                "distribution": True,
                "release_tag": selected_release["release_tag"],
                "prerelease": bool(selected_release.get("prerelease")),
                "components": selected_release.get("components") or {},
                "warnings": warnings,
            }
        else:
            token = ""
            self._plan = None
        component_updates = (
            list(selected_release["component_releases"].values())
            if selected_release is not None
            else []
        )
        distribution_updates = (
            [selected_release] if selected_release is not None else []
        )
        target_name = (
            str(selected_release.get("name") or selected_release["release_tag"])
            if selected_release is not None
            else ""
        )
        result = {
            "mode": "module",
            "distribution": True,
            "ok": True,
            "frontend": self.frontend,
            "checked_at": time.time(),
            "can_apply": bool(selected),
            "reason": "" if selected else "up_to_date",
            "target_tag": (
                selected_release["tag"] if selected_release is not None else ""
            ),
            "target_name": target_name,
            "target_commit": "",
            "target_sha": "",
            "target_label": target_name,
            "target_ref": "distribution-releases",
            "target_relation": (
                selected_release.get("relation")
                if selected_release is not None
                else ""
            ),
            "confirmation_token": token,
            "is_rollback": False,
            "is_diverged": False,
            "warnings": warnings,
            "updates": component_updates,
            "distribution_updates": distribution_updates,
            "components": {
                key: self._component_summary(value)
                for key, value in catalog["components"].items()
            },
            "available_release": selected_release,
            "available": any(
                value["update_available"]
                for value in catalog["components"].values()
            ),
            "errors": catalog.get("errors") or {},
            "worktree_dirty": worktree.get("dirty", False),
            "worktree": worktree,
            "worktree_text": self._worktree_text(worktree),
            "local_changes_policy": "backup_then_overwrite",
            "update_entries": list(BACKEND_UPDATE_ENTRIES),
            "preserved_entries": list(BACKEND_PRESERVED_ENTRIES),
            "version_label": self._version_label(),
        }
        state = self._read_state()
        now = time.time()
        stable_distribution = [
            self._public_summary(item)
            for item in distribution_updates
            if not item.get("prerelease")
        ]
        stable_components = [
            self._public_summary(item)
            for item in component_updates
            if not item.get("prerelease")
        ]
        state.update(
            {
                "last_attempt_at": now,
                "last_check_at": now,
                "last_error": "; ".join(result["errors"].values())[:500],
                "next_check_at": now + self.check_interval_days * 86400,
                "updates": stable_components,
                "distribution_updates": stable_distribution,
                "components": result["components"],
            }
        )
        self._write_state(state)
        return result

    @staticmethod
    def _public_summary(release: dict[str, Any]) -> dict[str, Any]:
        return {
            key: release.get(key)
            for key in (
                "id",
                "tag",
                "release_tag",
                "version",
                "component",
                "component_label",
                "name",
                "notes",
                "prerelease",
                "published_at",
                "html_url",
                "repo",
                "distribution",
                "components",
            )
        }

    async def apply(
        self,
        *,
        confirmation_token: str,
        acknowledge_prerelease: bool = False,
        defer_restart: bool = False,
    ) -> dict[str, Any]:
        plan = self._plan
        if not plan or plan.get("token") != confirmation_token:
            raise ModuleUpdateError(
                "confirmation_token_invalid", "确认令牌无效，请重新检查更新"
            )
        if float(plan.get("expires") or 0) < time.time():
            raise ModuleUpdateError("check_expired", "更新检查已过期，请重新检查")
        if not plan.get("release_tag"):
            raise ModuleUpdateError("no_check", "没有可应用的发行版更新")
        if plan.get("prerelease") and not acknowledge_prerelease:
            raise ModuleUpdateError(
                "prerelease_confirmation_required",
                "目标包含 Pre-release，必须显式确认其极不稳定后才能更新",
            )
        async with self._lock:
            if self._plan is not plan:
                raise ModuleUpdateError(
                    "confirmation_token_invalid",
                    "确认令牌已失效（可能已执行过更新），请重新检查更新",
                )
            result = await asyncio.to_thread(self._apply_sync, plan)
            # Single-use token: a second concurrent apply must not run again.
            self._plan = None
            self._catalog_cache = None
        worktree = await asyncio.to_thread(self._worktree_summary)
        return {
            "mode": "module",
            "distribution": True,
            "ok": True,
            "message": result["message"],
            "updated": result["updated"],
            "backend_updated": result["backend_updated"],
            "frontend_updated": result["frontend_updated"],
            "distribution_updated": result.get("distribution_updated", False),
            "distribution_tag": result.get("distribution_tag", ""),
            "partial": False,
            "skipped_components": [],
            "local_changes": result.get("local_changes") or {},
            "worktree": worktree,
            "worktree_text": self._worktree_text(worktree),
            "restart_required": result["backend_updated"],
            "restart_scheduled": False,
            "requires_app_restart": bool(
                result["frontend_updated"] and self.frontend == "gui"
            ),
            "reload_required": bool(
                result["frontend_updated"] and self.frontend == "webui"
            ),
            "backup_dir": result["backup_dir"],
            "files": result.get("files") or {},
            "frontend_files": result.get("frontend_files") or {},
            "preserved_entries": (
                (result.get("files") or {}).get("preserved_entries") or []
            ),
            "components": result["components"],
        }

    def _load_distribution_release(
        self, release_tag: str
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        try:
            releases = self.client.releases(DISTRIBUTION_REPO)
        except ModuleUpdateError as exc:
            raise ModuleUpdateError("release_not_found", exc.message) from None
        release = next(
            (item for item in releases if str(item.get("tag")) == release_tag),
            None,
        )
        if release is None:
            raise ModuleUpdateError(
                "release_not_found",
                f"KarenBot 仓库找不到发行版 {release_tag}",
            )
        manifest_asset = _manifest_asset(release)
        if manifest_asset is None:
            raise ModuleUpdateError(
                "module_manifest_invalid",
                f"{release_tag} 没有 modules.json",
            )
        raw = self.client.download_json_asset(manifest_asset)
        components = _validate_distribution_manifest(
            release, raw, self.frontend
        )
        return release, components

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 256), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _apply_sync(self, plan: dict[str, Any]) -> dict[str, Any]:
        release_tag = str(plan.get("release_tag") or "")
        if not _safe_tag(release_tag):
            raise ModuleUpdateError("release_invalid", "发行版 tag 不合法")
        release, components = self._load_distribution_release(release_tag)
        frontend_component = FRONTEND_COMPONENT[self.frontend]
        wanted = ["backend", frontend_component]
        timestamp = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + secrets.token_hex(3)
        )
        backup_root = self.data_dir / "module_backups" / timestamp
        try:
            backup_root.mkdir(parents=True, exist_ok=True)
            temp_root = Path(tempfile.mkdtemp(prefix="karen-module-update-"))
        except OSError as exc:
            raise ModuleUpdateError(
                "module_apply_failed",
                f"无法创建更新备份/临时目录：{type(exc).__name__}: {exc}",
            ) from exc
        staged: dict[str, Path] = {}
        staged_meta: dict[str, dict[str, Any]] = {}
        file_changes: list[tuple[Path, Path | None]] = []
        try:
            for component in wanted:
                meta = components[component]
                asset = _find_asset(release, meta["asset"])
                if asset is None:
                    raise ModuleUpdateError(
                        "module_asset_missing",
                        f"Release 缺少资产 {meta['asset']}",
                    )
                archive = temp_root / meta["asset"]
                self.client.download_asset(asset, archive)
                actual_size = archive.stat().st_size
                if actual_size != meta["size"]:
                    raise ModuleUpdateError(
                        "module_checksum_mismatch",
                        f"{component} 资产大小不符："
                        f"期望 {meta['size']}，实际 {actual_size}",
                    )
                actual_hash = self._sha256_file(archive)
                if actual_hash != meta["sha256"]:
                    raise ModuleUpdateError(
                        "module_checksum_mismatch",
                        f"{component} 资产 SHA256 不符",
                    )
                module_dir = temp_root / f"{component}-module"
                module_dir.mkdir(parents=True, exist_ok=True)
                version = self._extract_module(
                    archive, component, module_dir
                )
                if not any(module_dir.iterdir()):
                    raise ModuleUpdateError(
                        "module_layout_invalid",
                        f"{component} 资产中没有找到 "
                        f"{MODULE_DIRS[component]}/ 目录",
                    )
                if version and version != meta["version"]:
                    raise ModuleUpdateError(
                        "module_layout_invalid",
                        f"{component} 资产 VERSION={version} 与 manifest "
                        f"{meta['version']} 不一致",
                    )
                (module_dir / "VERSION").write_text(
                    meta["version"] + "\n", encoding="utf-8"
                )
                staged[component] = module_dir
                staged_meta[component] = {
                    "component": component,
                    "tag": meta["tag"],
                    "version": meta["version"],
                }
            if set(staged) != set(wanted):
                raise ModuleUpdateError(
                    "module_asset_missing",
                    "发行版没有同时提供后端与当前前端",
                )

            component_stats: dict[str, dict[str, Any]] = {}
            for component, module_dir in staged.items():
                if component == "backend":
                    target_root = self.project_root
                    backup_dir = backup_root / "files"
                else:
                    target_root = self.project_root / MODULE_DIRS[component]
                    backup_dir = backup_root / component / "files"
                stats, _changes = self._overlay_tree(
                    module_dir,
                    target_root,
                    backup_dir,
                    report_preserved_entries=(component == "backend"),
                    change_log=file_changes,
                )
                component_stats[component] = stats

            unsafe_total = sum(
                stats["skipped_links"] + stats["skipped_non_files"]
                for stats in component_stats.values()
            )
            if unsafe_total:
                details = []
                for component, stats in component_stats.items():
                    samples = stats.get("skipped_target_samples") or []
                    if samples:
                        details.append(
                            f"{MODULE_LABELS[component]}："
                            + "、".join(samples[:5])
                        )
                raise ModuleUpdateError(
                    "unsafe_update_target",
                    f"有 {unsafe_total} 个更新目标被本地符号链接或目录占用，"
                    "为保证不写入错误位置，本次更新已整体回滚。"
                    + (" 涉及：" + "；".join(details) if details else ""),
                )

            applied = [
                meta
                for component, meta in staged_meta.items()
                if component_stats[component]["updated_files"]
                + component_stats[component]["new_files"]
                > 0
            ]

            manifest = self._manifest()
            manifest["frontend"] = self.frontend
            manifest["distribution"] = {
                "repo": DISTRIBUTION_REPO,
                "tag": release_tag,
                "updated_at": time.time(),
            }
            for item in staged_meta.values():
                entry = manifest["components"].setdefault(item["component"], {})
                entry["repo"] = MODULE_REPOS[item["component"]]
                entry["dir"] = MODULE_DIRS[item["component"]]
                entry["tag"] = item["tag"]
                entry["version"] = item["version"]
                entry["distribution_tag"] = release_tag
                entry["updated_at"] = time.time()
            _write_manifest_atomic(self.project_root, manifest)
        except Exception as exc:  # noqa: BLE001
            logger.exception("模块更新失败，开始回滚")
            rollback_failures: list[str] = []
            for target, backup in reversed(file_changes):
                try:
                    if backup is not None and backup.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup, target)
                    elif target.exists():
                        target.unlink()
                except Exception:  # noqa: BLE001
                    logger.exception("回滚文件 %s 失败", target)
                    try:
                        rollback_failures.append(
                            str(target.relative_to(self.project_root))
                        )
                    except ValueError:
                        rollback_failures.append(str(target))
            if rollback_failures:
                note = (
                    f"；另有 {len(rollback_failures)} 个文件回滚失败，"
                    f"请从 {backup_root} 手动恢复："
                    + "、".join(rollback_failures[:5])
                )
                if isinstance(exc, ModuleUpdateError):
                    raise ModuleUpdateError(
                        exc.code, exc.message + note
                    ) from exc
                raise ModuleUpdateError(
                    "module_apply_failed",
                    f"模块更新失败：{type(exc).__name__}: {exc}{note}",
                ) from exc
            if isinstance(exc, ModuleUpdateError):
                raise
            raise ModuleUpdateError(
                "module_apply_failed", f"模块更新失败：{type(exc).__name__}: {exc}"
            ) from exc
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

        backend_stats = component_stats.get("backend", {})
        frontend_stats = component_stats.get(frontend_component, {})
        messages = [
            self._applied_message(component, stats)
            for component, stats in component_stats.items()
        ]
        messages.append("用户数据、人格、配置与插件均未改动")
        if frontend_stats.get("updated_files") or frontend_stats.get("new_files"):
            if self.frontend == "gui":
                messages.append("GUI 前端文件已更新，需完全退出并重新打开应用")
            else:
                messages.append("WebUI 文件已更新，刷新浏览器页面后生效")
        return {
            "message": "；".join(messages) + "。",
            "updated": applied,
            "backend_updated": any(
                item["component"] == "backend" for item in applied
            ),
            "frontend_updated": any(
                item["component"] == frontend_component for item in applied
            ),
            "distribution_updated": bool(applied),
            "distribution_tag": release_tag,
            "partial": False,
            "skipped_components": [],
            "local_changes": {
                "policy": "backup_then_overwrite",
                "backup_dir": str(backup_root),
            },
            "backup_dir": str(backup_root),
            "files": backend_stats,
            "frontend_files": frontend_stats,
            "components": {
                component: self._installed(component)
                for component in ("backend", frontend_component)
            },
        }

    @staticmethod
    def _applied_message(component: str, stats: dict[str, Any]) -> str:
        label = MODULE_LABELS[component]
        updated = stats.get("updated_files", 0)
        new = stats.get("new_files", 0)
        unchanged = stats.get("unchanged_files", 0)
        if not updated and not new:
            text = f"{label}文件已与发布版一致（{unchanged} 个未改动）"
        else:
            text = (
                f"{label}更新完成：覆盖 {updated} 个（已备份）、"
                f"新增 {new} 个、未改动 {unchanged} 个"
            )
        return text

    @staticmethod
    def _extract_module(
        archive: Path, component: str, destination: Path
    ) -> str:
        """把模块发布包解到 staging 目录。

        后端组件按 ``BACKEND_UPDATE_ENTRIES`` 白名单提取完整后端程序；
        GUI/WebUI 仍只提取各自的模块目录。白名单直接杜绝发布包覆盖用户
        数据、人格、配置和插件目录。
        """

        version = ""
        expected = MODULE_DIRS[component]
        with tarfile.open(archive, "r:*") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                name = member.name.replace("\\", "/")
                parts = [part for part in name.split("/") if part]
                if len(parts) < 2:
                    continue
                relative_parts = parts[1:]
                if relative_parts == ["VERSION"]:
                    try:
                        version = tar.extractfile(member).read().decode(
                            "utf-8", errors="replace"
                        ).strip()
                    except (AttributeError, OSError):
                        pass
                    if component != "backend":
                        continue
                if component == "backend":
                    if relative_parts[0] in BACKEND_PRESERVED_ENTRIES:
                        continue
                    if relative_parts[0] not in BACKEND_UPDATE_ENTRIES:
                        continue
                    relative = Path(*relative_parts)
                else:
                    if len(relative_parts) < 2:
                        continue
                    if relative_parts[0] != expected:
                        continue
                    relative = Path(*relative_parts[1:])
                if relative.is_absolute() or ".." in relative.parts:
                    continue
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    continue
                with source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
                try:
                    target.chmod(member.mode & 0o777)
                except OSError:
                    pass
            if not version:
                # Some archives put VERSION next to the module directory.
                candidates = [destination / "VERSION"]
                if component != "backend":
                    candidates.append(destination / expected / "VERSION")
                for version_file in candidates:
                    if version_file.is_file():
                        version = version_file.read_text(
                            encoding="utf-8", errors="replace"
                        ).strip()
                        break
        return version

    def _overlay_tree(
        self,
        staged_root: Path,
        target_root: Path,
        backup_root: Path,
        *,
        report_preserved_entries: bool = False,
        change_log: list[tuple[Path, Path | None]] | None = None,
    ) -> tuple[dict[str, Any], list[tuple[Path, Path | None]]]:
        """把发布包叠加到目标目录：只写程序文件、不删除任何文件。

        覆盖前把每个将被替换的旧文件备份到 ``backup_root``，用户可从备份
        目录恢复；白名单之外的用户数据不会被写入。
        """

        stats: dict[str, Any] = {
            "total_files": 0,
            "updated_files": 0,
            "new_files": 0,
            "unchanged_files": 0,
            "skipped_links": 0,
            "skipped_non_files": 0,
            "skipped_target_samples": [],
        }
        changes: list[tuple[Path, Path | None]] = (
            change_log if change_log is not None else []
        )
        target_root.mkdir(parents=True, exist_ok=True)
        for source in sorted(staged_root.rglob("*")):
            if not source.is_file() or source.is_symlink():
                continue
            relative = source.relative_to(staged_root)
            stats["total_files"] += 1
            target = target_root / relative
            # Never follow a local symlink, and never write through any
            # symlinked path component (even one pointing inside the project:
            # a release file must not be redirected into data/ or elsewhere).
            cursor = target_root
            unsafe_link = _is_link_like(cursor)
            if not unsafe_link:
                for part in relative.parts[:-1]:
                    cursor = cursor / part
                    if _is_link_like(cursor):
                        unsafe_link = True
                        break
            if unsafe_link or _is_link_like(target):
                stats["skipped_links"] += 1
                if len(stats["skipped_target_samples"]) < 20:
                    stats["skipped_target_samples"].append(relative.as_posix())
                continue
            if target.exists() and not target.is_file():
                # A directory where the release has a file is local content we
                # must not replace.
                stats["skipped_non_files"] += 1
                if len(stats["skipped_target_samples"]) < 20:
                    stats["skipped_target_samples"].append(relative.as_posix())
                continue
            if target.exists():
                try:
                    identical = target.read_bytes() == source.read_bytes()
                except OSError:
                    identical = False
                if identical:
                    stats["unchanged_files"] += 1
                    continue
                backup = backup_root / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                changes.append((target, backup))
                stats["updated_files"] += 1
            else:
                changes.append((target, None))
                stats["new_files"] += 1
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(
                f".{target.name}.karen-update-{os.getpid()}.tmp"
            )
            try:
                shutil.copy2(source, temp)
                os.replace(temp, target)
            finally:
                try:
                    if temp.exists():
                        temp.unlink()
                except OSError:
                    pass
            try:
                target.chmod(source.stat().st_mode & 0o777)
            except OSError:
                pass
        if report_preserved_entries:
            stats["preserved_entries"] = sorted(
                entry.name
                for entry in self.project_root.iterdir()
                if entry.name not in BACKEND_UPDATE_ENTRIES
            )
        return stats, changes

    async def auto_check_if_due(self, *, now: float | None = None) -> dict[str, Any]:
        current_time = time.time() if now is None else float(now)
        state = self._read_state()
        next_at = state.get("next_check_at")
        if next_at is not None and current_time < float(next_at):
            return {"skipped": True, "reason": "not_due", "next_check_at": next_at}
        try:
            result = await self.check()
            return {**result, "skipped": False}
        except ModuleUpdateError as exc:
            state = self._read_state()
            state.update(
                {
                    "last_attempt_at": current_time,
                    "last_error": f"{exc.code}: {exc.message}"[:500],
                    "next_check_at": current_time + RETRY_INTERVAL_SECONDS,
                }
            )
            self._write_state(state)
            return {
                "skipped": False,
                "ok": False,
                "error": exc.code,
                "message": exc.message,
                "next_check_at": current_time + RETRY_INTERVAL_SECONDS,
            }

    def _dir_inside_project(self) -> bool:
        try:
            self.data_dir.relative_to(self.project_root)
            return True
        except ValueError:
            return False


__all__ = [
    "DISTRIBUTION_ASSET_NAMES",
    "DISTRIBUTION_LABEL",
    "DISTRIBUTION_MANIFEST",
    "DISTRIBUTION_REPO",
    "DISTRIBUTION_TAG_PREFIX",
    "FRONTEND_COMPONENT",
    "FRONTEND_LABELS",
    "GITHUB_API_BASE",
    "GitHubClient",
    "MANIFEST_FILENAME",
    "MODULE_DIRS",
    "MODULE_LABELS",
    "MODULE_REPOS",
    "ModuleUpdateError",
    "ModuleUpdateService",
]
