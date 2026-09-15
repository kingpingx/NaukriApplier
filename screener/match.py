"""Check one opening against your resume, and against Naukri's view of you.

Two verdicts, because they answer different questions:

    yours    your CV scored against the posting by the same scorer --scan
             uses - does this job fit you?
    Naukri   the site's own `matchscore` for your live profile - does Naukri
             think you fit? It reads your profile's key-skill chips, not the
             resume file, and it is what a recruiter's filtered search sees.

Where they disagree - the CV has C#, Naukri says you lack it - the profile is
stale, and that is fixable. Where both say a skill is missing it is a real gap,
and nothing here will claim it for you: the same line the README draws around
applying.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from . import score as score_mod
from .model import Job
from .paths import NAUKRI_STATE
from .session import open_profile

log = logging.getLogger("screener.match")

# The job page loads its own data from these, keyed by job id. Like the search
# endpoints they are signed, so they are read off the page's traffic.
JOB_API = "/jobapi/v4/job/"
MATCH_API = "/matchscore"

# How long to wait for both payloads once the page has loaded.
API_WAIT_SEC = 25

JOB_ID = re.compile(r"naukri\.com/\S*?(\d{9,})(?:[/?#]|$)")

# Naukri's yes/no checks, in the order worth reading them.
NAUKRI_CHECKS = (("workExperience", "experience"), ("location", "location"),
                 ("education", "education"), ("industry", "industry"),
                 ("functionalArea", "function"))


class MatchError(RuntimeError):
    """The opening could not be read."""


def job_id_from_url(url: str) -> str:
    """The job id a Naukri posting URL ends with."""
    found = JOB_ID.search((url or "").strip())
    if not found:
        raise MatchError(f"Not a Naukri job URL: {url}\n"
                         "  Copy it from the job page - it ends in a long number.")
    return found.group(1)


def fetch(url: str, state_path: Path = NAUKRI_STATE,
          headless: bool = False) -> tuple[dict, dict | None]:
    """Open the posting; return (jobDetails, Naukri's matchscore or None)."""
    from playwright.sync_api import sync_playwright

    job_id = job_id_from_url(url)
    got: dict[str, dict] = {}

    def on_response(response):
        if job_id not in response.url:
            return
        kind = "match" if MATCH_API in response.url else "job" if JOB_API in response.url else None
        if kind:
            try:
                got[kind] = response.json()
            except Exception:
                pass

    with sync_playwright() as p:
        browser, _context, page = open_profile(p, state_path, headless=headless)
        try:
            page.on("response", on_response)
            page.goto(url.strip(), wait_until="domcontentloaded", timeout=60000)
            deadline = time.time() + API_WAIT_SEC
            while len(got) < 2 and time.time() < deadline:
                page.wait_for_timeout(500)
        except Exception as exc:
            # A window closed mid-wait still leaves whatever already arrived.
            log.debug("Job page ended early: %s", exc)
        finally:
            try:
                browser.close()
            except Exception:
                pass

    details = (got.get("job") or {}).get("jobDetails")
    if not details:
        raise MatchError("The job page never sent its details. The posting may have "
                         "expired - open the URL yourself to check.")
    return details, got.get("match")


def compare(details: dict, naukri: dict | None, config: dict) -> dict:
    """Both verdicts for one job, with Naukri's gaps split by what your CV backs."""
    job = Job.from_detail(details)
    breakdown = score_mod.score(job, config)
    have = score_mod.matched_skills(job.skills, config)
    preferred = {str(s.get("label") or "").strip()
                 for s in (details.get("keySkills") or {}).get("preferred") or []}
    flagged = [s.strip() for s in str((naukri or {}).get("skillMismatch") or "").split(",")
               if s.strip()]
    fixable = score_mod.matched_skills(flagged, config)
    return {
        "job": job, "breakdown": breakdown, "preferred": preferred,
        "have": have, "missing": [s for s in job.skills if s not in have],
        "naukri": naukri,
        "fixable": fixable,                                 # on your CV, not your profile
        "gaps": [s for s in flagged if s not in fixable],   # on neither
    }


def _band(score: float, config: dict) -> str:
    if score >= float(config.get("auto_apply_min_score", 72)):
        return "shortlist"
    if score >= float(config.get("review_min_score", 55)):
        return "worth a read"
    return "below your thresholds"


def _cv_lines(result: dict, config: dict) -> list[str]:
    job, breakdown = result["job"], result["breakdown"]

    def listed(skills: list[str]) -> str:
        return ", ".join(s + ("*" if s in result["preferred"] else "") for s in skills) or "-"

    if "rejected" in breakdown:
        verdict = f"rejected - {breakdown['rejected']}"
    else:
        parts = " ".join(f"{k}={v:g}" for k, v in breakdown.items())
        verdict = f"{job.score:g}/100, {_band(job.score, config)}   ({parts})"
    return [f"  Your CV:  {verdict}",
            f"  Has:      {listed(result['have'])}",
            f"  Missing:  {listed(result['missing'])}",
            "            * = a must-have for this job", ""]


def _share(value) -> str:
    """Naukri's `Keyskills` is a 0-1 fraction: 0 with seven skills flagged
    missing, 1.0 once the profile carried them all."""
    return f"{value:.0%}" if isinstance(value, (int, float)) else "?"


def _naukri_lines(result: dict) -> list[str]:
    naukri = result["naukri"]
    if not naukri:
        return ["  Naukri sent no match verdict for this job.", ""]
    checks = ", ".join(f"{label} {'yes' if naukri.get(key) else 'no'}"
                       for key, label in NAUKRI_CHECKS)
    lines = ["  Naukri's own check, against your live profile:",
             f"    {checks}",
             f"    key skills matched: {_share(naukri.get('Keyskills'))}"]
    if result["fixable"]:
        lines += [f"    says you lack, but your CV has:  {', '.join(result['fixable'])}",
                  "      -> your Naukri key skills are out of date. Fixable below."]
    if result["gaps"]:
        lines += [f"    missing from your CV too:        {', '.join(result['gaps'])}",
                  "      -> real gaps. Worth learning, not worth claiming."]
    return lines + [""]


def summarise(result: dict, config: dict) -> str:
    job = result["job"]
    if job.min_exp is not None and job.max_exp is not None:
        experience = f"{job.min_exp:g}-{job.max_exp:g} yrs"
    else:
        experience = "experience not stated"
    header = ["", f"  {job.title} - {job.company}",
              f"  {job.location or '-'} | {experience} | {job.salary_label or 'salary not stated'}",
              f"  {job.url}", ""]
    return "\n".join(header + _cv_lines(result, config) + _naukri_lines(result))
