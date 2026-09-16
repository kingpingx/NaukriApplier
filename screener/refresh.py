"""Keep the profile's "updated" date fresh with one small, real edit.

Recruiter search leans on recency, and Naukri stamps a profile as updated
whenever a section of it is saved. There are three kinds of edit it can make:

    skill       one skill from `refresh_skills` goes on, and a later run takes
                it back off. Only a chip this job added itself is ever removed -
                data/refresh.json records which one - so a skill you listed by
                hand is never touched, even when it is also in the pool.

    headline    a trailing full stop is added or removed.

    location    one city from `refresh_locations` joins your preferred work
                locations, and a later run takes it back off. Same rule: only a
                city this job added itself comes off, so the list you chose is
                never quietly edited. Off unless you set a pool, because your
                preferred locations are a deliberate choice and a city blinking
                in and out of them is visible to recruiters.

Every one is proved by reading the page back, as every other write here is.

Two modes. By default a run does every kind it can, which suits one run a day.
With `rotate=True` it does exactly one kind and moves to the next kind on the
next run, which is what a schedule running several times a day wants: each run
still moves the date, but no single field is churned.

The pools should hold things that are true of you: for the hours a skill or a
city is on, a recruiter filtering on it will find you.
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

# The order a rotating run works through. Skills first: it is the kind with the
# most to say to a recruiter, so if only one kind ever runs it should be that.
KINDS = ("skill", "headline", "location")


class RefreshError(RuntimeError):
    """The refresh had nothing it was allowed to change."""


def next_kind(record: dict, available: list[str]) -> str | None:
    """The kind this run should do, one step on from the last one that ran.

    Rotates only through kinds that are actually available, so turning a pool
    off in config.yaml drops its kind out of the cycle rather than wasting every
    third run on a kind that can do nothing.
    """
    order = [kind for kind in KINDS if kind in available]
    if not order:
        return None
    last = record.get("last_kind")
    if last in order:
        return order[(order.index(last) + 1) % len(order)]
    return order[0]


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


def _toggle(present: list[str], pool: list[str], added: str, last: str,
            setting: str, what: str, problems: list[str]):
    """Plan one add-or-take-back-off step over `pool`. Shared by skills and cities.

    The rule both kinds follow: something this job put there comes off next, and
    nothing else ever comes off. Anything already on the profile that the job did
    not add is left alone even when the pool names it.
    """
    have = {_norm(item).lower() for item in present}
    if not pool:
        return None
    added = _norm(added)
    if added and added.lower() in have:
        if len(present) < 2:
            problems.append(f"{added} is your only {what} - it stays until you add others.")
            return None
        return ("remove", added)

    # Start after the last one used, so the pool rotates rather than the same
    # entry going on and off forever.
    names = [item.lower() for item in pool]
    last = _norm(last).lower()
    start = names.index(last) + 1 if last in names else 0
    candidates = [item for item in pool[start:] + pool[:start] if item.lower() not in have]
    if candidates:
        return ("add", candidates[0])
    problems.append(
        f"every entry in {setting} is already on your profile, and none of them was put "
        f"there by this job, so there is none it may toggle. Add one you have but have "
        f"not listed.")
    return None


def plan(chips: list[str], headline: str | None, pool: list[str], record: dict,
         toggle_headline: bool = True, locations: list[str] | None = None,
         location_pool: list[str] | None = None, only: str | None = None) -> dict:
    """Decide this run's changes. Pure, so the rules are tested without a browser.

    Returns {"skill": ("add"|"remove", name) or None, "headline": str or None,
    "location": ("add"|"remove", name) or None, "notes": [...], "problems": [...]}.
    A problem blocks its own kind only; the others still go ahead.

    `only` restricts the plan to one kind, which is how a rotating run keeps each
    run to a single edit. Left None, every kind that can act does - the original
    behaviour, and the right one for a single run a day.
    """
    notes: list[str] = []
    problems: list[str] = []

    skill = None
    if only in (None, "skill"):
        skill = _toggle(chips, pool, record.get("added_skill"), record.get("last_skill"),
                        "refresh_skills", "key skill", problems)

    new_headline = None
    if toggle_headline and only in (None, "headline"):
        new_headline = headline_toggled(headline)
        if new_headline is None:
            notes.append("headline left alone - " + (
                "none found on the profile" if not _norm(headline).rstrip(".")
                else "it is at Naukri's 250-character limit, so there is no room for a full stop"))

    location = None
    if only in (None, "location") and location_pool:
        location = _toggle(locations or [], location_pool, record.get("added_location"),
                           record.get("last_location"), "refresh_locations",
                           "preferred work location", problems)

    return {"skill": skill, "headline": new_headline, "location": location,
            "notes": notes, "problems": problems}


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
        dry_run: bool = False, rotate: bool = False,
        location_pool: list[str] | None = None) -> dict:
    """Read the profile, make this run's changes, record how it went.

    Returns {"plan", "changes", "errors", "dry_run", "kind"}. A failure in one
    toggle is collected in "errors" rather than raised, so the others still run.

    With `rotate`, one kind runs and the next run takes the next kind.
    """
    location_pool = location_pool or []
    available = ([k for k in ("skill",) if pool]
                 + [k for k in ("headline",) if toggle_headline]
                 + [k for k in ("location",) if location_pool])
    if not available:
        raise RefreshError("Nothing to refresh: set refresh_skills or refresh_locations "
                           "in config.yaml, or leave refresh_headline on.")
    from playwright.sync_api import sync_playwright

    record = load_record(record_path)
    only = next_kind(record, available) if rotate else None
    result: dict = {"dry_run": dry_run, "plan": {}, "changes": [], "errors": [], "kind": only}
    try:
        with sync_playwright() as p:
            browser, _context, page = open_profile(p, state_path, headless=headless)
            try:
                edit.reveal(page)
                chips = edit._page_chips(page)
                edit._expand_read_more(page)
                headline = edit._text_of(page, S.FIELDS[HEADLINE])

                # Reading this costs a dialog open, so only pay for it when a
                # location change is actually on the cards.
                current_locations = None
                if location_pool and only in (None, "location"):
                    from . import career
                    current_locations = career.card_locations(page)

                todo = plan(chips, headline, pool, record, toggle_headline,
                            current_locations, location_pool, only)
                result["plan"] = todo
                result["errors"] += todo["problems"]
                if not dry_run:
                    _apply(page, todo, record, result)
            finally:
                browser.close()
    except Exception as exc:
        if not dry_run:
            _finish(record, record_path, result, str(exc), only)
        raise
    if not dry_run:
        _finish(record, record_path, result, kind=only)
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

    if todo.get("location"):
        action, name = todo["location"]
        from . import career
        try:
            if action == "add":
                # Recorded before the save, for the same reason as the skill
                # above: a save that lands but fails its read-back must still be
                # taken back off next time.
                record.update(added_location=name, last_location=name)
                wanted = (todo.get("current_locations") or career.card_locations(page)) + [name]
                career.set_locations(page, wanted)
                result["changes"].append(f"preferred locations: added {name}")
            else:
                wanted = [c for c in career.card_locations(page)
                          if not career._same_place(c, name)]
                career.set_locations(page, wanted, allow_lossy=(name,))
                record["added_location"] = None
                result["changes"].append(
                    f"preferred locations: removed {name}, added by the last refresh")
        except Exception as exc:
            log.warning("Location toggle failed: %s", exc)
            result["errors"].append(f"preferred locations: {exc}")

    if not result["changes"] and not result["errors"]:
        result["errors"].append("nothing was changed, so the profile date did not move - "
                                + "; ".join(todo["notes"]))


def _finish(record: dict, path: Path, result: dict, error: str | None = None,
            kind: str | None = None) -> None:
    errors = result["errors"] + ([error] if error else [])
    record.update(last_run=datetime.now().isoformat(timespec="seconds"),
                  ok=not errors, error="\n".join(errors) or None,
                  changes=result["changes"])
    # Only move the rotation on when the kind actually did something. A kind
    # that failed should come round again rather than be skipped for a full
    # cycle - otherwise one broken selector quietly halves the run rate.
    if kind and result["changes"]:
        record["last_kind"] = kind
    _save_record(record, path)


def summarise(result: dict) -> str:
    todo = result.get("plan") or {}
    lines = [""]
    if result.get("kind"):
        lines.append(f"  This run's kind: {result['kind']}")
    if result.get("dry_run"):
        lines.append("  Would change (nothing was written):")
        if todo.get("skill"):
            action, name = todo["skill"]
            lines.append(f"    key skills: {action} {name}")
        if todo.get("location"):
            action, name = todo["location"]
            lines.append(f"    locations:  {action} {name}")
        if todo.get("headline") is not None:
            # The change is at the very end, so show the end - not the start,
            # which is all a long headline's first 150 characters would show.
            new = todo["headline"]
            change = "add a full stop" if new.endswith(".") else "remove the full stop"
            tail = new if len(new) <= 70 else "..." + new[-70:]
            lines.append(f"    headline:   {change} -> {tail}")
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
