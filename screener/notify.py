"""Desktop notifications, so a hidden or scheduled scan is not silent.

The browser runs minimized and the timer starts it with nobody watching. Without
this, a scan that found twelve jobs and one that died on an expired login look
exactly the same: nothing happened on screen.
"""
from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger("screener.notify")

APP_NAME = "Naukri screener"


def send(title: str, body: str = "", critical: bool = False) -> None:
    """Pop up a desktop notification. Never raises - a failed pop-up must not fail a scan."""
    # ponytail: notify-send is Linux-only; Windows and macOS fall through to the log.
    binary = shutil.which("notify-send")
    if binary is None:
        log.debug("notify-send not found; not notifying: %s", title)
        return
    command = [binary, "--app-name", APP_NAME,
               "--urgency", "critical" if critical else "normal", title, body]
    try:
        subprocess.run(command, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("notify-send failed: %s", exc)
