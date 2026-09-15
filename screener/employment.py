"""Correct the employment records on your Naukri profile.

This exists because of one specific failure mode, and it is worth naming: an
old job left marked "to Present" does not just look untidy, it decides the
"Experience" figure in your profile header. A candidate with two years of work
shows up as a Fresher and is filtered out of every recruiter search that asks
for experience - which is most of them. It is the single most expensive stale
field on the profile, and the least visible.

Two operations, deliberately no more:

    end_role    flip an open-ended record to a closed one with an end date
    add_role    add a new record, optionally as the current one

There is no delete. Removing employment history is not something worth
automating, and a wrong call there is not recoverable from this side.

The dialog has three input styles and they are not interchangeable:

    suggester   company, designation - must be TYPED, then a suggestion picked,
                the same trap as the key-skills box
    dropdown    the month and year fields - click opens a list, pick an item
    radio       "currently working here" - the input is styled away, so the
                <label> is what takes the click
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import selectors as S
from .edit import EditError, _click_first, _type_first, reveal
from .paths import NAUKRI_STATE as DEFAULT_STATE
from .session import open_profile

log = logging.getLogger("screener.employment")

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# The month and year lists render as a bare <ul> - no id, no class, nothing to
# select on - sitting among a dozen other <ul>s belonging to the site nav. So
# the right option is found by exact label AND proximity to the field that
# opened it, which is the only thing that actually distinguishes it. Tagging the
# winner with an attribute lets Playwright do the clicking, so React sees a real
# user event rather than a synthetic one.
_PICK_JS = """(args) => {
    const value = args[0], fieldId = args[1];
    const field = document.getElementById(fieldId);
    if (!field) return 'no-field';
    const fr = field.getBoundingClientRect();
    let best = null, bestDist = Infinity, seen = 0;
    document.querySelectorAll('li').forEach(li => {
        if (li.offsetParent === null) return;
        if ((li.innerText || '').trim() !== value) return;
        const ul = li.parentElement;
        if (!ul || ul.children.length < 5) return;   // excludes the nav menus
        seen++;
        const r = li.getBoundingClientRect();
        const d = Math.abs(r.left - fr.left) + Math.abs(r.top - fr.top);
        if (d < bestDist) { bestDist = d; best = li; }
    });
    if (!best) return 'not-found';
    document.querySelectorAll('[data-screener-pick]').forEach(
        e => e.removeAttribute('data-screener-pick'));
    best.setAttribute('data-screener-pick', '1');
    return 'ok:' + seen;
}"""


def _pick_dropdown(page, selector: str, value: str, what: str) -> None:
    """Set one of the dialog's month/year dropdowns.

    Matched on the option's exact label: a substring test would take "202" for
    "2023", and would happily pick "Mar" when asked for "May" on a bad day.
    """
    field = page.locator(selector).first
    if not field.count():
        raise EditError(f"Could not find the {what} field ({selector}).")
    field_id = selector.lstrip("#")
    # Two tries: when another list is still open, the first click only closes
    # it and this one never opens - that failed the first IT-skill row live.
    for _ in range(2):
        field.scroll_into_view_if_needed(timeout=6000)
        field.click(timeout=6000)
        page.wait_for_timeout(1200)
        outcome = page.evaluate(_PICK_JS, [value, field_id])
        if str(outcome).startswith("ok"):
            break
    if not str(outcome).startswith("ok"):
        raise EditError(
            f"'{value}' was not offered in the {what} dropdown ({outcome}). "
            f"Nothing was saved."
        )

    picked = page.locator("[data-screener-pick='1']").first
    picked.click(timeout=5000)
    page.wait_for_timeout(600)
    try:
        page.evaluate("""() => document.querySelectorAll('[data-screener-pick]')
            .forEach(e => e.removeAttribute('data-screener-pick'))""")
    except Exception:
        pass

    # Confirm the field took the value; the list can close without selecting.
    got = (field.input_value(timeout=3000) or "").strip()
    if got != value:
        raise EditError(
            f"The {what} field reads {got!r} after picking {value!r}. "
            f"Nothing was saved."
        )


def _set_suggester(page, candidates: list[str], value: str, what: str) -> None:
    """Type into a suggester field and take its first suggestion if offered."""
    _type_first(page, candidates, value, what)
    page.wait_for_timeout(2000)
    suggestions = page.locator(S.SKILL_SUGGESTIONS)
    if suggestions.count():
        try:
            suggestions.first.click(timeout=3000)
            page.wait_for_timeout(400)
            return
        except Exception:
            pass
    # No suggestion is normal here - Naukri accepts a free-text company name
    # that is not in its directory, unlike the key-skills box.
    log.debug("%s: no suggestion offered for %r; keeping the typed value", what, value)


def _rows(page) -> list[str]:
    return [t.strip().replace("\n", " | ")
            for t in page.locator(S.EMPLOYMENT_ROWS).all_inner_texts()]


def end_role(page, match: str, end_month: str, end_year: str) -> dict:
    """Close the open-ended record whose text contains `match`."""
    if end_month not in MONTHS:
        raise EditError(f"{end_month!r} is not a month. Use one of: {', '.join(MONTHS)}")

    reveal(page)
    before = _rows(page)

    index = next((i for i, row in enumerate(before)
                  if match.lower() in row.lower()), None)
    if index is None:
        raise EditError(
            f"No employment record mentions {match!r}.\n"
            f"  Found: {before}"
        )

    row = page.locator(S.EMPLOYMENT_ROWS).nth(index)
    row.locator(S.EMPLOYMENT_ROW_EDIT).first.click(timeout=8000)
    page.wait_for_timeout(2500)

    _click_first(page, S.EMPLOYMENT_IS_CURRENT_NO, "'currently working here: No'")
    page.wait_for_timeout(1500)

    _pick_dropdown(page, S.EMPLOYMENT_END_YEAR, end_year, "worked till year")
    _pick_dropdown(page, S.EMPLOYMENT_END_MONTH, end_month, "worked till month")

    _click_first(page, S.EMPLOYMENT_SAVE, "employment save")
    page.wait_for_timeout(3500)
    reveal(page)

    after = _rows(page)
    closed = [r for r in after if match.lower() in r.lower()]
    if not closed:
        raise EditError(f"The {match!r} record disappeared after saving - check the profile.")
    if "present" in closed[0].lower():
        raise EditError(
            f"The {match!r} record still reads 'to Present' after saving:\n"
            f"  {closed[0]}\n  Nothing else was changed."
        )
    log.info("Ended %s: %s", match, closed[0])
    return {"before": before, "after": after}


def add_role(page, company: str, designation: str,
             start_month: str, start_year: str, current: bool = True) -> dict:
    """Add an employment record, optionally as the current one."""
    if start_month not in MONTHS:
        raise EditError(f"{start_month!r} is not a month. Use one of: {', '.join(MONTHS)}")

    reveal(page)
    before = _rows(page)
    if any(company.lower() in row.lower() for row in before):
        raise EditError(
            f"There is already an employment record for {company!r}:\n"
            f"  {[r for r in before if company.lower() in r.lower()]}\n"
            f"  Edit it rather than adding a duplicate."
        )

    _click_first(page, S.EMPLOYMENT_ADD, "'Add employment'")
    page.wait_for_timeout(2500)

    _click_first(page,
                 S.EMPLOYMENT_IS_CURRENT_YES if current else S.EMPLOYMENT_IS_CURRENT_NO,
                 "'currently working here'")
    page.wait_for_timeout(1200)

    _set_suggester(page, S.EMPLOYMENT_COMPANY, company, "company")
    _set_suggester(page, S.EMPLOYMENT_DESIGNATION, designation, "designation")

    # The Add dialog is taller than the Edit one - it carries salary and notice
    # period too - so the date fields start below its fold. They resolve but are
    # not visible, and a click on them simply times out.
    page.keyboard.press("Escape")          # close any lingering suggestion list
    page.wait_for_timeout(400)
    page.mouse.move(710, 450)
    for _ in range(4):
        page.mouse.wheel(0, 320)
        page.wait_for_timeout(400)

    _pick_dropdown(page, S.EMPLOYMENT_START_YEAR, start_year, "started year")
    _pick_dropdown(page, S.EMPLOYMENT_START_MONTH, start_month, "started month")

    _click_first(page, S.EMPLOYMENT_SAVE, "employment save")
    page.wait_for_timeout(3500)
    reveal(page)

    after = _rows(page)
    if not any(company.lower() in row.lower() for row in after):
        raise EditError(
            f"{company!r} is not in the employment list after saving. "
            f"The dialog probably rejected a required field.\n  Now: {after}"
        )
    log.info("Added %s - %s", designation, company)
    return {"before": before, "after": after}


def session(state_path: Path = DEFAULT_STATE, headless: bool = False):
    """Context manager yielding a page on the profile, for chaining operations."""
    from playwright.sync_api import sync_playwright

    class _Session:
        def __enter__(self):
            self._pw = sync_playwright().start()
            self.browser, _ctx, self.page = open_profile(
                self._pw, state_path, headless=headless)
            return self.page

        def __exit__(self, *exc):
            try:
                self.browser.close()
            finally:
                self._pw.stop()
            return False

    return _Session()
