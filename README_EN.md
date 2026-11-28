# KarenBot-backend

[简体中文](README.md) · [KarenBot unified repository](https://github.com/CN-BI5UNF/KarenBot) · [Changelog](CHANGELOG.md)

KarenBot-backend is the **core runtime and control-plane module** for KarenBot. It contains the
launcher, configuration and migration code, Runtime, adapters, memory, tools, model plugins, and
the FastAPI/WebSocket control API. It does not contain a desktop or browser console.

The current unified release is [v2.1.3-alpha](https://github.com/CN-BI5UNF/KarenBot/releases/tag/v2.1.3-alpha).
It is the GitHub **Latest normal release**, not a pre-release; the alpha suffix is retained for tag
compatibility. The official installer is maintained in
[KarenBot-Installer](https://github.com/CN-BI5UNF/KarenBot-Installer).

## Repository boundary

| Repository | Scope |
|---|---|
| **KarenBot-backend** | Core runtime, data paths, adapters, and control API |
| [KarenBot-GUI](https://github.com/CN-BI5UNF/KarenBot-GUI) | Native GTK4/libadwaita `gui/` frontend |
| [KarenBot-WebUI](https://github.com/CN-BI5UNF/KarenBot-WebUI) | Browser `webui/` frontend |
| [KarenBot](https://github.com/CN-BI5UNF/KarenBot) | Unified release, component lock, and distribution assets |

The two frontends use the same authenticated control API. A development checkout must copy one
frontend module into the backend root; a unified installer does this from the release manifest.

## Layout

```text
main.py                 launcher (`--frontend gui|webui`)
main_cli.py             headless launcher and setup flow
core/                   Runtime, events, media, chat timeline, backup/restore
app_config/             schemas, migrations, paths, and secrets
adapters/               OneBot 11/NapCat, Telegram, and transports
agents/ memory/ mind/   persona agents, history, RAG, and background state
providers/ tools/       model providers, tool registry, and built-ins
backend/                FastAPI control plane, auth, API routes, plugin protocol
docs/                   architecture, deployment, protocol, and data contracts
```

User data is outside the source package: `data/`, `personas/`, `config.yaml`, encrypted secrets,
RSA private keys, logs, model weights, and virtual environments must never be committed.

## Quick start

Copy either frontend into the checkout, then install the backend package:

```bash
cp -a /path/to/KarenBot-WebUI/webui ./webui   # or KarenBot-GUI/gui
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e .
.venv/bin/python main.py --list-frontends
.venv/bin/python main.py --frontend webui
```

The WebUI listens on `127.0.0.1` by default. Enable LAN access in Settings → Network, set
`webui.allow_lan: true`, or pass `--host 0.0.0.0` explicitly. The first launch opens the setup
wizard. You can leave the channel disabled to test models and local features first.
Re-submitting the wizard preserves settings it does not display, including network, backup,
storage, external programs, hidden provider/agent fields, and secret references. A running Runtime
must be stopped before applying the wizard again.

For the native GUI on Linux, install system GTK4/libadwaita/PyGObject and make the virtual
environment see system packages:

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -e .
.venv/bin/python main.py --frontend gui
```

## Selective backup and restore

The GUI and WebUI share the backend backup API under Settings → Backup. Users can select these
categories independently:

- configuration and preferences;
- encrypted credentials and RSA key material;
- chats/memory and vector memory;
- workspace files, with incoming media and attachments as a subordinate option;
- emoji assets, custom provider presets, and personas.

Before creating an archive, each category reports its source size. During the job, the UI reports
per-category processed bytes and archive bytes. These sizes describe uncompressed data; the final
ZIP size depends on compression.

Existing archives are deeply verified before restore. The user selects categories, previews archive
and current-target sizes plus overwrite/new-file counts, and enters the confirmation phrase
`恢复备份`. The restore runs asynchronously. It pauses the Runtime, creates a complete rollback
archive for the selected categories, then overlays verified archived paths: matching files are
replaced, missing files are added, and current files absent from the archive remain untouched.
Unselected categories are not written. The job reports progress, warnings, and the rollback
archive; write failures and a failed post-restore restart trigger a rollback attempt.

Credential restore is allowed only when the archive contains a verified RSA key pair, and the
private key is imported into the current secret store. Archives are not encrypted, so protect them
like passwords. User exports snapshot SQLite stores independently and do not promise a global
cross-database transaction; strict migration snapshots remain a separate maintenance-only feature.
See [backup and restore details](docs/backup.md) for limits, offline verification, isolation drills,
and the full API.

## Version and testing

`VERSION`, `backend.__version__`, and `pyproject.toml` use `2.1.3-alpha`; `uv.lock` stores the
PEP 440 normalized form `2.1.3a0`.

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q tests -k 'not api_builds_module_service'
.venv/bin/python -m ruff check agents core memory mind providers tools tests
git diff --check
```

CI covers Python 3.11 and 3.12.

## Documentation

- [Architecture](docs/architecture.md) · [Features](docs/features.md) · [Background running](docs/background_running.md)
- [Protocol catalog](docs/protocols/README.md) · [Frontend integration](docs/protocols/frontend-integration.md)
- [Data-root contract](docs/data_root_contract.md) · [Storage maintenance](docs/storage_maintenance.md)
- [Destructive administration](docs/destructive_admin.md) · [Persona guide](docs/persona_writing_guide.md)
- [Provider development](docs/provider_development.md)

## Security and license

The control API requires authentication. Do not expose it to an untrusted network. Never commit
configuration, tokens, API keys, RSA private keys, chat history, or logs. Updates only write the
program-file allowlist and create per-file backups before replacement. The project is licensed
under Apache-2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
