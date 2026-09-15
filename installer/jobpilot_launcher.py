"""PyInstaller entry point.

Ported from Prospector's installer\prospector_launcher.py -- same reasoning
applies unchanged: a frozen build has no console by default, so anything
that would normally be printed has to go somewhere the user can find, and a
failure to start shows a message box rather than vanishing silently.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _log_path() -> Path:
    folder = Path.home() / ".jobpilot"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "startup.log"


def _show_error(message: str) -> None:
    """Tell the user something, even with no console attached."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            f"JobPilot could not start.\n\n{message}\n\n"
            f"Details were written to:\n{_log_path()}",
            "JobPilot", 0x10,
        )
    except Exception:  # noqa: BLE001 - not on Windows, or no user32
        print(message, file=sys.stderr)


def _warn_if_playwright_browser_missing() -> None:
    """The apply step needs a real Chromium, which PyInstaller does not (and
    should not) bundle -- it's a ~300MB download that `playwright install`
    manages separately. The dashboard itself works without it; only the
    auto-apply feature needs this. Warn once, don't block startup on it.
    """
    cache = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"
    if cache.exists() and any(cache.iterdir()):
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            "The dashboard will still work, but auto-apply needs a one-time "
            "browser download first. Open a command prompt and run:\n\n"
            "    playwright install chromium\n\n"
            "(This only needs to happen once.)",
            "JobPilot -- one-time setup for auto-apply", 0x40,
        )
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    log = _log_path()
    try:
        # Frozen builds get no stdout/stderr, so anything the app prints (and
        # any traceback) is captured here instead of being lost.
        sys.stdout = sys.stderr = log.open("w", encoding="utf-8", buffering=1)
    except OSError:
        pass

    try:
        from jobpilot.config import ensure_dirs, load_env
        from jobpilot.database import init_db
        from jobpilot.webui import run

        load_env()
        ensure_dirs()
        init_db()
        _warn_if_playwright_browser_missing()

        port = int(os.environ.get("JOBPILOT_PORT", "8765"))
        run(port=port, open_browser=True)
        return 0

    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _show_error(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
