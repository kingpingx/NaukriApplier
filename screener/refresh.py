"""Keep the profile's "updated" date fresh with one small, real edit a day.

Recruiter search leans on recency, and Naukri stamps a profile as updated
whenever a section of it is saved. Each run makes two saves in one browser
session:

    key skill   one skill from `refresh_skills` goes on, and the next run takes
                it back off. Only a chip this job added itself is ever removed -
                data/refresh.json records which one - so a skill you listed by
                hand is never touched, even when it is also in the pool.

    headline    a trailing full stop is added or removed.

Both are proved by reading the page back, as every other write here is. The
pool should hold skills you actually have: for the days a skill is on, a
recruiter filtering on it will find you.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from . import edit
from . import selectors as S
from .paths import NAUKRI_STATE, REFRESH_STATE
from .session import open_profile

log = logging.getLogger("screener.refresh")

HEADLINE = "resume_headline"


class RefreshError(RuntimeError):
    """The daily refresh had nothing it was allowed to change."""


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def pool_from(value) -> list[str]:
    """`refresh_skills` as written in config.yaml - a list, or one comma-separated string."""
    if isinstance(value, str):
        value = value.split(",")
    seen, pool = set(), []
    for item in value or []:
        skill = _norm(str(item))
        if skill and skill.lower() not in seen:
            seen.add(skill.lower())
            pool.append(skill)
    return pool


def headline_toggled(text: str | None) -> str | None:
    """The headline with its trailing full stop flipped, or None if that cannot be done."""
    text = _norm(text)
    if not text:
        return None
    if text.endswith("."):
        return text.rstrip(". ") or None
    if len(text) + 1 > S.MAX_LENGTHS.get(HEADLINE, 250):
        return None
    return text + "."


def headline_saved(sent: str, shown: str | None) -> bool:
    """Exact comparison, last character included.

    `edit._saved_ok` accepts the shown text as a prefix of what was sent, to
    cope with clipped summaries. For a change that IS one trailing character,
    that would pass a save that never landed.
    """
    return _norm(sent).lower() == _norm(shown).lower()


def plan(chips: list[str], headline: str | None, pool: list[str], record: dict,
         toggle_headline: bool = True) -> dict:
    """Decide today's changes. Pure, so the rules are tested without a browser.

    Returns {"skill": ("add"|"remove", name) or None, "headline": str or None,
    "notes": [...], "problems": [...]}. A problem blocks the skill toggle only;
    the headline still goes ahead.
    """
    have = {_norm(c).lower() for c in chips}
    notes: list[str] = []
    problems: list[str] = []
    skill = None

    added = _norm(record.get("added_skill"))
    if pool and added and added.lower() in have:
        if len(chips) < 2:
            problems.append(f"{added} is your only key skill - it stays until you add others.")
        else:
            skill = ("remove", added)
    elif pool:
        # Start after the last skill used, so the pool rotates rather than the
        # same skill going on and off forever.
        names = [s.lower() for s in pool]
        last = _norm(record.get("last_skill")).lower()
        start = names.index(last) + 1 if last in names else 0
        candidates = [s for s in pool[start:] + pool[:start] if s.lower() not in have]
        if candidates:
            skill = ("add", candidates[0])
        else:
            problems.append(
                "every skill in refresh_skills is already on your profile, and none of "
                "them was put there by this job, so there is none it may toggle. Add a "
                "skill you have but have not listed.")

    new_headline = None
    if toggle_headline:
        new_headline = headline_toggled(headline)
        if new_headline is None:
            notes.append("headline left alone - " + (
                "none found on the profile" if not _norm(headline).rstrip(".")
                else "it is at Naukri's 250-character limit, so there is no room for a full stop"))

    return {"skill": skill, "headline": new_headline, "notes": notes, "problems": problems}


def load_record(path: Path = REFRESH_STATE) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_record(record: dict, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def run(pool: list[str], toggle_headline: bool = True, state_path: Path = NAUKRI_STATE,
        record_path: Path = REFRESH_STATE, headless: bool = False,
        dry_run: bool = False) -> dict:
    """Read the profile, make today's changes, record how it went.

    Returns {"plan", "changes", "errors", "dry_run"}. A failure in one toggle
    is collected in "errors" rather than raised, so the other still runs.
    """
    if not pool and not toggle_headline:
        raise RefreshError("Nothing to refresh: set refresh_skills in config.yaml, "
                           "or leave refresh_headline on.")
    from playwright.sync_api import sync_playwright

    record = load_record(record_path)
    result: dict = {"dry_run": dry_run, "plan": {}, "changes": [], "errors": []}
    try:
        with sync_playwright() as p:
            browser, _context, page = open_profile(p, state_path, headless=headless)
            try:
                edit.reveal(page)
                chips = edit._page_chips(page)
                edit._expand_read_more(page)
                headline = edit._text_of(page, S.FIELDS[HEADLINE])
                todo = plan(chips, headline, pool, record, toggle_headline)
                result["plan"] = todo
                result["errors"] += todo["problems"]
                if not dry_run:
                    _apply(page, todo, record, result)
            finally:
                browser.close()
    except Exception as exc:
        if not dry_run:
            _finish(record, record_path, result, str(exc))
        raise
    if not dry_run:
        _finish(record, record_path, result)
    return result


def _apply(page, todo: dict, record: dict, result: dict) -> None:
    if todo["skill"]:
        action, name = todo["skill"]
        try:
            if action == "add":
                # Recorded before the save, not after: a save that lands but
                # then fails its read-back must still be taken back tomorrow.
                # If it never landed, the next plan sees it absent and adds again.
                record.update(added_skill=name, last_skill=name)
                edit.add_key_skills(page, [name])
                result["changes"].append(f"key skills: added {name}")
            else:
                edit.remove_key_skill(page, name)
                record["added_skill"] = None
                result["changes"].append(f"key skills: removed {name}, added by the last refresh")
        except Exception as exc:
            log.warning("Key-skill toggle failed: %s", exc)
            result["errors"].append(f"key skills: {exc}")

    if todo["headline"] is not None:
        try:
            saved = edit.set_text_field(page, HEADLINE, todo["headline"])
            if not headline_saved(todo["headline"], saved["after"]):
                raise RefreshError(f"the page shows {saved['after']!r}, not the headline sent")
            result["changes"].append("headline: " + (
                "added a full stop" if todo["headline"].endswith(".") else "removed the full stop"))
        except Exception as exc:
            log.warning("Headline toggle failed: %s", exc)
            result["errors"].append(f"headline: {exc}")

    if not result["changes"] and not result["errors"]:
        result["errors"].append("nothing was changed, so the profile date did not move - "
                                + "; ".join(todo["notes"]))


def _finish(record: dict, path: Path, result: dict, error: str | None = None) -> None:
    errors = result["errors"] + ([error] if error else [])
    record.update(last_run=datetime.now().isoformat(timespec="seconds"),
                  ok=not errors, error="\n".join(errors) or None,
                  changes=result["changes"])
    _save_record(record, path)


def summarise(result: dict) -> str:
    todo = result.get("plan") or {}
    lines = [""]
    if result.get("dry_run"):
        lines.append("  Would change (nothing was written):")
        if todo.get("skill"):
            action, name = todo["skill"]
            lines.append(f"    key skills: {action} {name}")
        if todo.get("headline") is not None:
            lines.append(f"    headline:   {todo['headline'][:150]}")
    else:
        lines += [f"  {change}" for change in result.get("changes", [])] or ["  Nothing was changed."]
    lines += [f"  note: {note}" for note in todo.get("notes", [])]
    lines += [f"  problem: {error}" for error in result.get("errors", [])]
    return "\n".join(lines) + "\n"


def status_line(path: Path = REFRESH_STATE) -> str | None:
    """How the last refresh went, for --check. None if it has never run."""
    record = load_record(path)
    if not record.get("last_run"):
        return None
    when = str(record["last_run"]).replace("T", " ")
    if record.get("ok"):
        return f"  Last profile refresh: {when} - ok ({'; '.join(record.get('changes') or [])})"
    first = (record.get("error") or "unknown error").splitlines()[0]
    return (f"  Last profile refresh: {when} - FAILED: {first}\n"
            "    If that is about the login: python main.py --login")
