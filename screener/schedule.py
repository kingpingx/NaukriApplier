"""Run the scan on a timer - the Linux counterpart of Windows Task Scheduler.

A systemd *user* timer rather than cron, for two reasons that each decide whether a
scheduled scan works at all:

    display   the scan drives a real, minimized browser, and its pop-ups need the
              desktop's notification bus. The user service manager already carries
              DISPLAY, XAUTHORITY and DBUS_SESSION_BUS_ADDRESS; cron carries none.
    sleep     `Persistent=true` runs a scan that fell due while the laptop slept as
              soon as it wakes. Cron silently skips it.

Timers run only while you are logged in - the same condition the browser needs,
since there is no display to draw it on otherwise.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from .paths import ROOT

UNIT = "naukri-scan"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
TIME = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


class ScheduleError(RuntimeError):
    """The schedule could not be installed or removed."""


def parse_times(text: str) -> list[str]:
    """"18:30, 9:00" -> ["09:00", "18:30"]. Anything that is not a clock time is refused."""
    times, bad = set(), []
    for raw in (part.strip() for part in (text or "").split(",")):
        match = TIME.fullmatch(raw)
        if match:
            times.add(f"{int(match.group(1)):02d}:{match.group(2)}")
        elif raw:
            bad.append(raw)
    if bad or not times:
        raise ScheduleError("Times must be 24-hour HH:MM, comma-separated - e.g. "
                            f"09:00,13:00,18:00. Not: {', '.join(bad) or '(none given)'}")
    return sorted(times)


def timer_unit(times: list[str]) -> str:
    calendar = "\n".join(f"OnCalendar=*-*-* {t}:00" for t in times)
    return (f"[Unit]\nDescription=Naukri job scan at {', '.join(times)}\n\n"
            f"[Timer]\n{calendar}\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n")


def service_unit(root: Path = ROOT, python: str = sys.executable) -> str:
    return ("[Unit]\nDescription=Naukri job scan\n\n"
            f"[Service]\nType=oneshot\nWorkingDirectory={root}\n"
            f'ExecStart="{python}" "{root / "main.py"}" --scan --notify\n'
            "TimeoutStartSec=2h\n")


def _systemctl(*args: str) -> str:
    if shutil.which("systemctl") is None:
        raise ScheduleError("No systemctl here, so no systemd timers. On Windows use Task "
                            "Scheduler, on macOS launchd - see 'Run it on a schedule' in the README.")
    done = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise ScheduleError(f"systemctl --user {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def install(times: list[str]) -> str:
    """Write and start the timer. Returns systemd's list of upcoming runs."""
    _systemctl("--version")   # fail before writing unit files, not after
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (UNIT_DIR / f"{UNIT}.service").write_text(service_unit(), encoding="utf-8")
    (UNIT_DIR / f"{UNIT}.timer").write_text(timer_unit(times), encoding="utf-8")
    _systemctl("daemon-reload")
    _systemctl("enable", f"{UNIT}.timer")
    # Restart, not start: running --schedule again with new times has to
    # replace the old ones on a timer that is already live.
    _systemctl("restart", f"{UNIT}.timer")
    return _systemctl("list-timers", f"{UNIT}.timer", "--no-pager")


def remove() -> bool:
    """Stop the timer and delete both units. False if none was installed."""
    files = [UNIT_DIR / f"{UNIT}.timer", UNIT_DIR / f"{UNIT}.service"]
    if not any(f.exists() for f in files):
        return False
    try:
        _systemctl("disable", "--now", f"{UNIT}.timer")
    except ScheduleError:
        pass   # already stopped or never enabled - the files still have to go
    for f in files:
        f.unlink(missing_ok=True)
    _systemctl("daemon-reload")
    return True
