"""Run the scan, the profile refresh or the resume re-upload on a timer.

Linux gets a systemd *user* timer, Windows a Task Scheduler task. Either way,
two things decide whether a scheduled run works at all:

    display   the job drives a real, minimized browser, and its pop-ups need the
              desktop's notification bus. The systemd user manager already
              carries DISPLAY, XAUTHORITY and DBUS_SESSION_BUS_ADDRESS; cron
              carries none. The Windows task runs with your interactive token,
              on your desktop.
    sleep     a run that fell due while the laptop slept runs as soon as it
              wakes - `Persistent=true` on systemd, `StartWhenAvailable` on
              Windows. Cron silently skips it.

Both run only while you are logged in - the same condition the browser needs,
since there is no display to draw it on otherwise.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from .paths import ROOT

# What each schedulable job runs. The refresh starts at a slightly different
# minute each day: an edit landing at exactly 09:30:00 every morning is the one
# thing about it that would look automated.
JOBS = {
    "scan": {"unit": "naukri-scan", "what": "Naukri job scan",
             "args": ["--scan", "--notify"], "jitter_minutes": 0},
    "refresh": {"unit": "naukri-refresh", "what": "Naukri profile refresh",
                "args": ["--refresh-profile", "--headless", "--notify"], "jitter_minutes": 20},
    # Downloads the resume attached to the profile and uploads it again under the
    # same name, so no local file decides what goes up. A short window rather
    # than the refresh's 20 minutes: it runs at times you chose, and it has to
    # stay clear of the hourly refresh either side of it.
    "upload": {"unit": "naukri-upload", "what": "Naukri resume re-upload",
               "args": ["--reupload-resume", "--headless", "--notify"], "jitter_minutes": 5},
}
UNIT = JOBS["scan"]["unit"]
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


def _windows() -> bool:
    return sys.platform == "win32"


# --- systemd (Linux) ------------------------------------------------------

def timer_unit(times: list[str], job: str = "scan") -> str:
    spec = JOBS[job]
    calendar = "\n".join(f"OnCalendar=*-*-* {t}:00" for t in times)
    jitter = f"RandomizedDelaySec={spec['jitter_minutes']}m\n" if spec["jitter_minutes"] else ""
    return (f"[Unit]\nDescription={spec['what']} at {', '.join(times)}\n\n"
            f"[Timer]\n{calendar}\n{jitter}Persistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n")


def service_unit(root: Path = ROOT, python: str = sys.executable, job: str = "scan") -> str:
    spec = JOBS[job]
    root = Path(root).as_posix()   # a systemd unit is always read on Linux
    return (f"[Unit]\nDescription={spec['what']}\n\n"
            f"[Service]\nType=oneshot\nWorkingDirectory={root}\n"
            f'ExecStart="{python}" "{root}/main.py" {" ".join(spec["args"])}\n'
            "TimeoutStartSec=2h\n")


def _systemctl(*args: str) -> str:
    if shutil.which("systemctl") is None:
        raise ScheduleError("No systemctl here, so no systemd timers. On macOS use launchd "
                            "- see 'Run it on a schedule' in the README.")
    done = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise ScheduleError(f"systemctl --user {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


# --- Task Scheduler (Windows) ---------------------------------------------

def task_xml(times: list[str], job: str = "scan", root: Path = ROOT,
             python: str = sys.executable) -> str:
    """Task Scheduler's definition of the job: one daily trigger per time.

    XML rather than `schtasks /Create /SC DAILY`, because the command line
    cannot set StartWhenAvailable or a random delay.
    """
    spec = JOBS[job]
    delay = (f"      <RandomDelay>PT{spec['jitter_minutes']}M</RandomDelay>\n"
             if spec["jitter_minutes"] else "")
    triggers = "".join(
        "    <CalendarTrigger>\n"
        f"      <StartBoundary>2026-01-01T{t}:00</StartBoundary>\n"
        f"{delay}"
        "      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>\n"
        "    </CalendarTrigger>\n" for t in times)
    arguments = " ".join([f'"{Path(root) / "main.py"}"', *spec["args"]])
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(spec['what'])} at {', '.join(times)}</Description>
  </RegistrationInfo>
  <Triggers>
{triggers}  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(python))}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(root))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    if shutil.which("schtasks") is None:
        raise ScheduleError("schtasks was not found, so no Task Scheduler task can be made.")
    done = subprocess.run(["schtasks", *args], capture_output=True, text=True)
    if check and done.returncode != 0:
        raise ScheduleError(f"schtasks {args[0]} failed: "
                            f"{(done.stderr or done.stdout or '').strip()}")
    return done


def _install_task(times: list[str], job: str) -> str:
    name = JOBS[job]["unit"]
    # schtasks only reads the definition from a file, and only as UTF-16.
    xml_path = Path(tempfile.gettempdir()) / f"{name}.xml"
    xml_path.write_text(task_xml(times, job), encoding="utf-16")
    try:
        _schtasks("/Create", "/TN", name, "/XML", str(xml_path), "/F")
    finally:
        xml_path.unlink(missing_ok=True)
    return _schtasks("/Query", "/TN", name, "/FO", "LIST").stdout


# --- either ---------------------------------------------------------------

def install(times: list[str], job: str = "scan") -> str:
    """Write and start the timer. Returns the scheduler's view of it."""
    if _windows():
        return _install_task(times, job)
    unit = JOBS[job]["unit"]
    _systemctl("--version")   # fail before writing unit files, not after
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (UNIT_DIR / f"{unit}.service").write_text(service_unit(job=job), encoding="utf-8")
    (UNIT_DIR / f"{unit}.timer").write_text(timer_unit(times, job), encoding="utf-8")
    _systemctl("daemon-reload")
    _systemctl("enable", f"{unit}.timer")
    # Restart, not start: running --schedule again with new times has to
    # replace the old ones on a timer that is already live.
    _systemctl("restart", f"{unit}.timer")
    return _systemctl("list-timers", f"{unit}.timer", "--no-pager")


def remove(job: str = "scan") -> bool:
    """Stop the timer and delete it. False if none was installed."""
    unit = JOBS[job]["unit"]
    if _windows():
        if _schtasks("/Query", "/TN", unit, check=False).returncode != 0:
            return False
        _schtasks("/Delete", "/TN", unit, "/F")
        return True
    files = [UNIT_DIR / f"{unit}.timer", UNIT_DIR / f"{unit}.service"]
    if not any(f.exists() for f in files):
        return False
    try:
        _systemctl("disable", "--now", f"{unit}.timer")
    except ScheduleError:
        pass   # already stopped or never enabled - the files still have to go
    for f in files:
        f.unlink(missing_ok=True)
    _systemctl("daemon-reload")
    return True
