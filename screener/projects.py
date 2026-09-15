"""Add and remove the Projects section of your Naukri profile.

Projects are where a career change actually shows. An employment record says
where you were; the projects say what you built, and for someone whose last two
years look nothing like their first two, that is the section a recruiter reads
to believe the change is real.

Ordering matters and is deliberate at the call site: add the replacements
BEFORE deleting what they replace. Delete-then-add leaves an empty Projects
section if any add fails, and an empty section is worse than a stale one.

Field mechanics are the same three traps as everywhere else on this profile:

    suggester   client name - must be TYPED; a free-text name is kept
    dropdown    month/year - click opens a list, pick by exact label
    radio       status / onsite / employment type - click the <label>

The end-date fields do not exist until "Finished" is selected, so they are
discovered after that click rather than assumed.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .edit import EditError, _click_first, _fill_first, reveal
from .employment import MONTHS, _pick_dropdown, _set_suggester

log = logging.getLogger("screener.projects")

ROWS = "#lazyProject .row.project-list"
ROW_EDIT = "[class*=edit]"
ADD = ["#add-project",
       "xpath=//*[normalize-space(text())='Projects']/following-sibling::span[contains(@class,'add')][1]",
       "#lazyProject .widgetHead .add"]

TITLE = ["#projectTitle"]
CLIENT = ["#clientName"]
DETAILS = ["#projectDetails"]
STATUS_FINISHED = ["label[for='finished']"]
STATUS_INPROGRESS = ["label[for='inprogress']"]
START_MONTH = "#projStartMonthFor"
START_YEAR = "#projStartYearFor"
SAVE = ["#submitProject", "button.btn-dark-ot:visible"]
DELETE = ["a.delete", "a:has-text('Delete')"]


def rows(page) -> list[str]:
    return [t.strip().replace("\n", " | ")
            for t in page.locator(ROWS).all_inner_texts()]


def _end_fields(page) -> tuple[str, str]:
    """Find the end month/year inputs that appear once 'Finished' is chosen."""
    found = page.evaluate("""() => [...document.querySelectorAll('input')]
        .filter(i => i.offsetParent !== null && /end/i.test(i.id))
        .map(i => i.id)""")
    month = next((f"#{i}" for i in found if "month" in i.lower()), None)
    year = next((f"#{i}" for i in found if "year" in i.lower()), None)
    if not month or not year:
        raise EditError(
            f"Could not find the project end-date fields after choosing "
            f"'Finished' (saw: {found}). Nothing was saved."
        )
    return month, year


def add(page, title: str, details: str, start_month: str, start_year: str,
        end_month: str | None = None, end_year: str | None = None,
        client: str = "Self Practice") -> dict:
    """Add one project - finished with an end date, in progress without one.

    Verifies it appears before returning.
    """
    in_progress = end_month is None and end_year is None
    dates = [("start", start_month)] + ([] if in_progress else [("end", end_month)])
    for name, month in dates:
        if month not in MONTHS:
            raise EditError(f"{month!r} is not a month ({name}).")
    if not in_progress and not end_year:
        raise EditError("A finished project needs both an end month and an end year.")

    reveal(page)
    before = rows(page)
    if any(title.split("-")[0].strip().lower() in r.lower() for r in before):
        raise EditError(f"A project matching {title!r} already exists. Not adding a duplicate.")

    _click_first(page, ADD, "'Add project'")
    page.wait_for_timeout(2500)

    # Title and details are plain fields with no suggester behind them, so they
    # are filled, not typed. That matters: `fill()` does not require the element
    # to be unobstructed, and the client suggester's dropdown sits open right
    # on top of the details textarea. Typing there clicks the dropdown instead.
    _fill_first(page, TITLE, title, "project title")
    _fill_first(page, DETAILS, details, "project details")
    _set_suggester(page, CLIENT, client, "client")

    if in_progress:
        _click_first(page, STATUS_INPROGRESS, "'In progress'")
    else:
        _click_first(page, STATUS_FINISHED, "'Finished'")
    page.wait_for_timeout(1500)

    _pick_dropdown(page, START_YEAR, start_year, "project start year")
    _pick_dropdown(page, START_MONTH, start_month, "project start month")

    if not in_progress:
        end_month_sel, end_year_sel = _end_fields(page)
        _pick_dropdown(page, end_year_sel, end_year, "project end year")
        _pick_dropdown(page, end_month_sel, end_month, "project end month")

    # No Escape here. Picking an option already closes its list, and Escape
    # closes the whole dialog - which then reads as "Save button not found".
    page.wait_for_timeout(500)

    # The dialog used to carry a "skills used" suggester. Naukri removed it
    # (checked 2026-09-15), so nothing sits between the dates and Save.
    _click_first(page, SAVE, "project save")
    page.wait_for_timeout(3500)
    reveal(page)

    after = rows(page)
    key = title.split("-")[0].strip().lower()
    if not any(key in r.lower() for r in after):
        raise EditError(
            f"{title!r} is not in the project list after saving - the dialog "
            f"probably rejected a required field.\n  Now: {after}"
        )
    log.info("Added project: %s", title)
    return {"before": before, "after": after}


def delete(page, match: str) -> dict:
    """Delete the project whose row text contains `match`."""
    reveal(page)
    before = rows(page)
    index = next((i for i, r in enumerate(before) if match.lower() in r.lower()), None)
    if index is None:
        raise EditError(f"No project matches {match!r}.\n  Found: {before}")
    if len(before) <= 1:
        raise EditError(
            f"{match!r} is the only project on the profile. Refusing to leave "
            f"the section empty - add the replacement first."
        )

    page.locator(ROWS).nth(index).locator(ROW_EDIT).first.click(timeout=8000)
    page.wait_for_timeout(2500)
    _click_first(page, DELETE, "project delete")
    page.wait_for_timeout(2000)

    # A confirmation step may follow; take it only if one appears.
    for label in ("Confirm", "Yes", "Delete"):
        try:
            btn = page.get_by_role("button", name=label).first
            if btn.count() and btn.is_visible():
                btn.click(timeout=3000)
                page.wait_for_timeout(1500)
                break
        except Exception:
            continue

    page.wait_for_timeout(2500)
    reveal(page)
    after = rows(page)
    if any(match.lower() in r.lower() for r in after):
        raise EditError(f"{match!r} is still listed after the delete.\n  Now: {after}")
    log.info("Deleted project matching %r", match)
    return {"before": before, "after": after}
