#!/usr/bin/env python3
"""Headful GUI smoke test: open every page, optionally take screenshots.

Requires an X11/Wayland display and the system PyGObject/GTK4 stack.  This is
not collected by pytest; it is a developer/QA helper.

    DISPLAY=:1.0 .venv/bin/python scripts/gui_smoke.py
    DISPLAY=:1.0 .venv/bin/python scripts/gui_smoke.py --screenshot-dir /tmp/karen-shots
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib  # noqa: E402

from gui.app import NAV_ITEMS, KarenApplication  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot-dir", default="")
    parser.add_argument("--page-delay", type=float, default=3.0)
    parser.add_argument("--theme", choices=("auto", "light", "dark"), default="auto")
    parser.add_argument("--data-dir", default="", help="use an alternate data root")
    args = parser.parse_args()

    screenshot_dir = Path(args.screenshot_dir).expanduser() if args.screenshot_dir else None
    if screenshot_dir is not None:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
    pages = [item[0] for item in NAV_ITEMS]
    data_root = Path(args.data_dir).expanduser() if args.data_dir else None
    app = KarenApplication(data_root=data_root, theme=args.theme)
    state = {"phase": "wait", "index": -1, "shown_at": 0.0, "errors": 0}

    def _shot(page_id: str) -> None:
        if screenshot_dir is None:
            return
        out = screenshot_dir / f"{state['index']:02d}-{page_id}.png"
        try:
            subprocess.run(["gnome-screenshot", "-f", str(out)], timeout=20, check=False)
            print(f"SHOT {out}", flush=True)
        except Exception:
            traceback.print_exc()

    def _show(index: int) -> None:
        page_id = pages[index]
        print(f"PAGE {page_id}", flush=True)
        try:
            app.window.show_page(page_id)
        except Exception:
            state["errors"] += 1
            traceback.print_exc()
        state["shown_at"] = time.monotonic()

    def tick() -> bool:
        window = app.window
        if window is None or window.api is None:
            return True
        if state["phase"] == "wait":
            try:
                status = window.api.get("/api/status", timeout=5)
            except Exception:
                return True
            if status.get("setup_required"):
                print("SMOKE: first-run setup required; open the GUI and finish setup first.", flush=True)
                app.quit()
                return False
            if status.get("state") != "running":
                return True
            state["phase"] = "pages"
        if state["index"] < 0:
            state["index"] = 0
            _show(0)
            return True
        if time.monotonic() - state["shown_at"] < max(0.5, args.page_delay):
            return True
        _shot(pages[state["index"]])
        state["index"] += 1
        if state["index"] >= len(pages):
            print(f"SMOKE_OK pages={len(pages)} errors={state['errors']}", flush=True)
            app.quit()
            return False
        _show(state["index"])
        return True

    GLib.timeout_add(1000, tick)
    try:
        code = app.run([sys.argv[0]])
    except Exception:
        traceback.print_exc()
        return 1
    return 0 if code == 0 and state["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
