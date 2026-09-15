"""Every path the screener writes to, resolved in one place.

Five modules used to each compute their own `ROOT` by counting `.parent` calls
back up the tree, which meant moving a module broke its output location
silently. They all import from here now.

`SCREENER_HOME` lets a user keep their data outside the clone - useful when the
repo is a checkout they pull updates into, and essential for the Apify actor,
where the code ships read-only inside a container and only /tmp is writable.
"""
from __future__ import annotations

import os
from pathlib import Path

# The repository root: one level above this package.
ROOT = Path(__file__).resolve().parent.parent

# Everything user-specific lives under here. Override with SCREENER_HOME to
# keep your profile, sessions and results outside the clone.
HOME = Path(os.environ.get("SCREENER_HOME") or (ROOT / "data")).resolve()

# --- inputs -------------------------------------------------------------
CONFIG_PATH = Path(os.environ.get("SCREENER_CONFIG") or (ROOT / "config.yaml"))
PROFILES_DIR = ROOT / "profiles"          # shipped role packs
RESUME_DIR = HOME / "resume"              # the user drops their CV here

# --- session state (these files ARE the login) --------------------------
NAUKRI_STATE = HOME / "state.json"
LINKEDIN_STATE = HOME / "linkedin_state.json"

# --- derived / generated ------------------------------------------------
PROFILE_JSON = HOME / "profile.json"
PROFILE_TXT = HOME / "profile.txt"
PROFILE_PNG = HOME / "profile.png"
RESUME_JSON = HOME / "resume.json"        # parsed resume facts
REFRESH_STATE = HOME / "refresh.json"     # what --refresh-profile last changed

JOBS_DIR = HOME / "jobs"
LEDGER_PATH = JOBS_DIR / "ledger.json"
SEEN_PATH = JOBS_DIR / "seen.json"

LOG_DIR = ROOT / "logs"


def ensure() -> None:
    """Create the writable directories. Safe to call repeatedly."""
    for directory in (HOME, RESUME_DIR, JOBS_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)
