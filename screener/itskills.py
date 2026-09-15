"""Add rows to the IT skills table on your Naukri profile.

Key skills say what you know; this table says for how long. Each row carries a
version, the year you last used it and your experience with it, and an empty
table reads as depth on nothing, whatever the key skills claim.

Same posture as the other profile writers: bad input is refused before a browser
opens, and a row only counts once it is read back off the saved profile.

    suggester   the skill name - typed; only an exact suggestion is clicked, and
                with none the typed name stands (the read-back proves it saved)
    free text   the version
    dropdown    last used, years, months - picked by their exact labels

Experience is optional on purpose. A duration is a claim a recruiter reads; when
it is not known, leaving it blank is honest and a guess is not.
"""
from __future__ import annotations

import logging
from datetime import date

from .edit import EditError, _click_first, _fill_first, _pick_suggestion, _type_first, reveal
from .employment import _pick_dropdown

log = logging.getLogger("screener.itskills")

CARD = "#lazyITSkills"
ADD = [f"{CARD} .widgetHead .add"]
NAME = ["#itSkillSugg"]
VERSION = ["#version"]
LAST_USED = "#lastUsedDroopeFor"
YEARS = "#expYearDroopeFor"
MONTHS = "#expMonthDroopeFor"
SAVE = ["button.btn-dark-ot:visible", "button:has-text('Save'):visible"]
SUGGESTIONS = "#sugDrp_itSkillSugg li.sugTouple"
MAX_YEARS = 30          # the Years dropdown stops at "30 Years"
OLDEST_YEAR = 1940      # and "Last used" at 1940


def years_label(years: int) -> str:
    """The Years dropdown's own wording: "0 Year", "1 Year", "2 Years"."""
    return f"{years} Year" + ("s" if years > 1 else "")


def months_label(months: int) -> str:
    """The Months dropdown's own wording: "0 Month", "1 Month", "2 Months"."""
    return f"{months} Month" + ("s" if months > 1 else "")


def check(name: str, last_used: int | None, years: int | None, months: int | None) -> None:
    """Refuse a row the dialog cannot hold, before any browser opens."""
    if not name.strip():
        raise EditError("An IT skill needs a name.")
    if last_used is not None and not OLDEST_YEAR <= last_used <= date.today().year:
        raise EditError(f"{name}: last used {last_used} is not a year the dialog offers.")
    if years is not None and not 0 <= years <= MAX_YEARS:
        raise EditError(f"{name}: {years} years is outside 0-{MAX_YEARS}.")
    if months is not None and not 0 <= months <= 11:
        raise EditError(f"{name}: {months} months is outside 0-11.")
    if months is not None and years is None:
        raise EditError(f"{name}: months without years - give both, or neither.")


def listed(name: str, lines: list[str]) -> bool:
    """Whether the card shows `name` as a cell of its own, not inside another name.

    A substring test would call "SQL" present the moment "PostgreSQL" was.
    """
    want = name.strip().lower()
    return any(line.strip().lower() == want for line in lines)


def card_lines(page) -> list[str]:
    """The IT skills card as text lines. Read from the card, not a row selector,
    so a markup change cannot make a saved row look missing."""
    return page.locator(CARD).first.inner_text(timeout=5000).splitlines()


def add(page, name: str, version: str = "", last_used: int | None = None,
        years: int | None = None, months: int | None = None) -> dict:
    """Add one IT-skill row. Verifies it is on the saved profile before returning."""
    check(name, last_used, years, months)
    reveal(page)
    before = card_lines(page)
    if listed(name, before):
        raise EditError(f"{name!r} is already in your IT skills. Not adding a duplicate.")

    _click_first(page, ADD, "'IT skills: Add details'")
    page.wait_for_timeout(2000)
    _type_first(page, NAME, name, "IT skill name")
    match = _pick_suggestion(page, name, SUGGESTIONS)
    if match is not None:
        match.click(timeout=4000)
        # Let the suggestion list finish closing: a click on "Last used" while it
        # is still up only dismisses it, and the year list never opens.
        page.wait_for_timeout(800)
    else:
        # The typed name stands, but its suggestion list stays open over the
        # fields below. Tab closes it; Escape would close the whole dialog.
        page.keyboard.press("Tab")
    if version:
        _fill_first(page, VERSION, version, "software version")
    if last_used is not None:
        _pick_dropdown(page, LAST_USED, str(last_used), "last used")
    if years is not None:
        _pick_dropdown(page, YEARS, years_label(years), "experience years")
        _pick_dropdown(page, MONTHS, months_label(months or 0), "experience months")

    _click_first(page, SAVE, "IT skill save")
    page.wait_for_timeout(3000)
    reveal(page)
    after = card_lines(page)
    if not listed(name, after):
        raise EditError(f"{name!r} is not in your IT skills after saving - the dialog "
                        f"probably rejected a field. Nothing else was changed.")
    log.info("Added IT skill: %s", name)
    return {"before": before, "after": after}
