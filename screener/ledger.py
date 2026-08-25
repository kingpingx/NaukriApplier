"""A record of every job the agent has already seen, applied to or skipped.

Without this the daily run re-applies to the same postings every morning:
Naukri's search returns the same jobs day after day, and a second application
to a job you already applied to is the one thing guaranteed to read as a bot.

Keyed by Naukri's own jobId, which is stable across searches and pages.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("screener.ledger")

from .paths import LEDGER_PATH

# Outcomes worth never revisiting.
TERMINAL = {"applied", "skipped", "offsite", "questionnaire-declined"}


class Ledger:
    def __init__(self, path: Path = LEDGER_PATH):
        self.path = path
        self.entries: dict[str, dict] = {}
        if path.exists():
            try:
                self.entries = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                # A corrupt ledger must not silently reset to empty - that would
                # re-apply to everything. Move it aside and start clean, loudly.
                backup = path.with_suffix(".corrupt.json")
                log.error("Ledger at %s unreadable (%s); moved to %s", path, exc, backup)
                try:
                    path.replace(backup)
                except OSError:
                    pass
                self.entries = {}

    def seen(self, job_id: str) -> bool:
        return job_id in self.entries

    def is_terminal(self, job_id: str) -> bool:
        return self.entries.get(job_id, {}).get("status") in TERMINAL

    def status(self, job_id: str) -> str | None:
        return self.entries.get(job_id, {}).get("status")

    def record(self, job, status: str, note: str = "") -> None:
        """Store an outcome for a job. Later calls overwrite earlier ones."""
        self.entries[job.job_id] = {
            "title": job.title,
            "company": job.company,
            "url": job.url,
            "status": status,
            "note": note,
            "score": getattr(job, "score", None),
            "at": datetime.now().isoformat(timespec="seconds"),
        }

    def applied_on(self, day: date) -> int:
        """How many applications were sent on a given day."""
        stamp = day.isoformat()
        return sum(
            1
            for entry in self.entries.values()
            if entry.get("status") == "applied" and str(entry.get("at", "")).startswith(stamp)
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write via a temp file so an interrupted run cannot truncate the
        # ledger and lose the record of what has already been applied to.
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.entries, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.path)
        log.info("Ledger saved: %d entries", len(self.entries))
