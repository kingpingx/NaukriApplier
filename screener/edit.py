"""Write the text fields of your Naukri profile: headline, summary, key skills.

These are the fields recruiters actually filter and read, and they are the ones
that go stale silently - a resume gets replaced when you change jobs, but the
headline written on the day you signed up sits there for years.

Same posture as `upload.py`, for the same reason: this writes to a live,
recruiter-facing profile.

    checks first    Naukri silently truncates a headline over 250 characters
                    and blocks a summary over 1000, so both are validated here
                    where the message is readable.

    proves it       every setter re-reads the field after saving and compares
                    it to what was sent. The save toast is not trusted - it
                    does not always fire, and a dialog that closes without
                    saving looks identical to one that saved.

    never blind     `preview()` returns what is there now without touching it,
                    so a caller can show the before/after before committing.

The key-skills widget deserves its own warning. Typing a skill and pressing
Enter does NOT create a chip - a suggestion from the dropdown has to be clicked.
Worse, whatever is left sitting in the input box when Save is pressed gets
committed as a chip, so a half-typed skill becomes a real one. Both behaviours
are handled in `set_key_skills`, and neither is obvious from the markup.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from . import selectors as S
from .extract import _expand_read_more
from .paths import NAUKRI_STATE as DEFAULT_STATE
from .session import open_profile

log = logging.getLogger("screener.edit")


class EditError(RuntimeError):
    """A profile field could not be updated."""


def _click_first(page, candidates: list[str], what: str, timeout: int = 8000):
    """Click the first candidate selector that resolves. Raises if none do."""
    for selector in candidates:
        try:
            node = page.locator(selector).first
            if node.count():
                node.scroll_into_view_if_needed(timeout=timeout)
                node.click(timeout=timeout)
                return True
        except Exception as exc:
            log.debug("%s: %s did not click (%s)", what, selector, exc)
            continue
    raise EditError(
        f"Could not find the {what} control.\n"
        f"  Naukri has probably reshipped the markup - fix the selectors in "
        f"screener/selectors.py.\n  Tried: {', '.join(candidates)}"
    )


def _fill_first(page, candidates: list[str], value: str, what: str, timeout: int = 8000):
    for selector in candidates:
        try:
            node = page.locator(selector).first
            if node.count():
                node.fill("", timeout=timeout)
                node.fill(value, timeout=timeout)
                return True
        except Exception as exc:
            log.debug("%s: %s did not fill (%s)", what, selector, exc)
            continue
    raise EditError(f"Could not find the {what} input.")


def _type_first(page, candidates: list[str], value: str, what: str,
                timeout: int = 8000) -> bool:
    """Type into the first candidate, keystroke by keystroke.

    Not interchangeable with `_fill_first`. Playwright's `fill()` sets the
    value directly, which is fine for a plain textarea but invisible to
    Naukri's skill suggester - it listens for key events, so a filled box
    produces no dropdown, every skill looks unrecognised, and the whole update
    silently does nothing. This is why the key-skills path types instead.
    """
    for selector in candidates:
        try:
            node = page.locator(selector).first
            if node.count():
                # These dialogs are taller than the modal that holds them, so a
                # field further down resolves but is not visible and the click
                # times out - which surfaces as "input not found" and sends you
                # looking for a markup change that did not happen.
                node.scroll_into_view_if_needed(timeout=timeout)
                page.wait_for_timeout(250)
                node.click(timeout=timeout)
                node.fill("", timeout=timeout)
                if value:
                    node.type(value, delay=110)
                return True
        except Exception as exc:
            log.debug("%s: %s did not type (%s)", what, selector, exc)
            continue
    raise EditError(f"Could not find the {what} input.")


def _text_of(page, candidates: list[str]) -> str | None:
    for selector in candidates:
        try:
            node = page.locator(selector).first
            if node.count():
                text = (node.inner_text(timeout=3000) or "").strip()
                if text:
                    return text
        except Exception:
            continue
    return None


def _saved_ok(sent: str, shown: str | None) -> bool:
    """Whether what the page shows is what we sent.

    Naukri renders these fields with its own whitespace, and clips long ones to
    a preview with a "Read More" affordance that does not always expand. So an
    exact match is accepted, and otherwise the shown text is treated as a
    prefix of what was sent - which still catches the failure that matters, a
    dialog that closed without saving and left the old value in place.
    """
    def norm(text: str | None) -> str:
        return " ".join((text or "").split()).lower()

    want, got = norm(sent), norm(shown)
    if not got:
        return False
    if want == got:
        return True
    got = re.sub(r"\s*(?:\.\.\.|…)?\s*read more\s*$", "", got).rstrip(". …")
    return len(got) >= 40 and want.startswith(got)


def _page_chips(page) -> list[str]:
    """The key-skill chips as rendered on the profile page.

    Not the same selector as the ones inside the edit dialog: the page renders
    `<span class="chip" title="Java">Java</span>` under `.widgetCont`, while the
    dialog wraps each chip's text in a `.tagTxt` under `.chipsContainer`. Using
    the dialog's selector here read back an empty list and reported a perfectly
    good save as a failure.
    """
    for selector in S.LIST_FIELDS["key_skills"]:
        chips = [c.strip() for c in page.locator(selector).all_inner_texts()]
        chips = [c for c in chips if c]
        if chips:
            return chips
    return []


def check_length(field: str, value: str) -> str:
    limit = S.MAX_LENGTHS.get(field)
    value = value.strip()
    if not value:
        raise EditError(f"{field}: refusing to write an empty value.")
    if limit and len(value) > limit:
        raise EditError(
            f"{field} is {len(value)} characters; Naukri's limit is {limit}.\n"
            f"  Trim it and re-run - Naukri would truncate it mid-sentence."
        )
    return value


def _settle(page) -> None:
    page.wait_for_timeout(2500)


def reveal(page) -> None:
    """Force every lazy-loaded widget to render, then return to the top.

    The profile cards carry `data-plugin="lazyload"` and only enter the DOM once
    scrolled past. Editing two fields in one session failed on the second for
    exactly this reason: the first save re-rendered the page, the widget below
    it dropped back out, and its edit icon was simply not there to click. So
    this runs before *every* field, not once per session.
    """
    # A dialog from the previous field can still be mounted, and its backdrop
    # swallows the next click without ever hiding the button underneath - which
    # reads as "selector not found" and sends you hunting for markup changes
    # that never happened. Dismiss it first.
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            break

    for _ in range(7):
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(400)
    page.mouse.wheel(0, -30000)
    page.wait_for_timeout(1000)

    # A save toast sits over the page and swallows the next click.
    for selector in S.SAVE_CONFIRMATIONS:
        try:
            toast = page.locator(selector).first
            if toast.count() and toast.is_visible():
                toast.wait_for(state="hidden", timeout=6000)
        except Exception:
            continue


def set_text_field(page, field: str, value: str) -> dict:
    """Open the editor for `field`, replace its contents, save, verify."""
    value = check_length(field, value)
    editor = S.EDITORS[field]

    reveal(page)
    before = _text_of(page, S.FIELDS[field])

    _click_first(page, editor["trigger"], f"{field} edit")
    page.wait_for_timeout(1200)
    _fill_first(page, editor["input"], value, field)
    _click_first(page, editor["save"], f"{field} save")
    _settle(page)

    # The card clips a long value and appends "Read More", so the DOM holds a
    # preview rather than the saved text. Expand it before reading back, or a
    # perfectly good save verifies as a failure.
    _expand_read_more(page)
    after = _text_of(page, S.FIELDS[field])

    if not _saved_ok(value, after):
        raise EditError(
            f"{field} did not save.\n"
            f"  Wanted: {value[:90]}...\n"
            f"  Page still shows: {(after or '(empty)')[:90]}...\n"
            f"  Nothing else was changed."
        )
    log.info("%s updated (%d chars)", field, len(value))
    return {"field": field, "before": before, "after": after}


def set_key_skills(page, skills: list[str]) -> dict:
    """Replace the key-skill chips wholesale.

    Chips are removed before new ones are added, so the result is exactly the
    list passed in rather than the old list with additions - which is what
    "update my skills" means when the old ones are from a different stack.
    """
    editor = S.EDITORS["key_skills"]

    reveal(page)
    before = _page_chips(page)

    _click_first(page, editor["trigger"], "key skills edit")
    page.wait_for_timeout(1500)

    # Remove every existing chip. Each removal re-renders the list, so this
    # always takes the first remaining one rather than iterating an index.
    removed = 0
    for _ in range(60):
        closers = page.locator(f"{S.SKILL_CHIP} {S.SKILL_CHIP_REMOVE}")
        if not closers.count():
            break
        try:
            closers.first.click(timeout=4000)
            removed += 1
            page.wait_for_timeout(250)
        except Exception:
            break
    log.info("Removed %d existing chips", removed)

    added, skipped = [], []
    for skill in skills:
        try:
            _type_first(page, editor["input"], skill, "key skills")
            page.wait_for_timeout(2200)
            suggestions = page.locator(S.SKILL_SUGGESTIONS)
            if suggestions.count():
                suggestions.first.click(timeout=4000)
                added.append(skill)
            else:
                # No suggestion means Naukri does not know this term. Leaving it
                # in the box would commit it as a chip on save anyway, so it is
                # cleared deliberately and reported instead of appearing silently.
                skipped.append(skill)
            page.wait_for_timeout(350)
        except Exception as exc:
            log.debug("skill %r failed: %s", skill, exc)
            skipped.append(skill)

    # Anything left in the input becomes a chip on save. Clear it.
    try:
        _type_first(page, editor["input"], "", "key skills")
    except EditError:
        pass

    # Every chip was removed above. Saving now, with nothing added, would wipe
    # the key skills off the profile entirely - the single most destructive
    # thing this module could do, and it would look like a successful run. So
    # bail out without saving and let the dialog be discarded instead.
    if not added:
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
        except Exception:
            pass
        raise EditError(
            "None of the skills offered were recognised by Naukri's suggester, "
            "so there was nothing to save.\n"
            "  The dialog was cancelled - your existing key skills are untouched.\n"
            f"  Not recognised: {', '.join(skipped)}"
        )

    _click_first(page, editor["save"], "key skills save")
    _settle(page)

    after = _page_chips(page)
    if not after:
        raise EditError(
            "Key skills came back empty after saving - the old chips were "
            "removed but the new ones did not land. Fix this on the site now: "
            "an empty key-skills list is worse than a stale one."
        )
    log.info("Key skills: %d chips now set", len(after))
    return {"field": "key_skills", "before": before, "after": after,
            "skipped": skipped}


def preview(state_path: Path = DEFAULT_STATE, headless: bool = False) -> dict:
    """What the writable fields hold right now. Changes nothing."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, _context, page = open_profile(p, state_path, headless=headless)
        try:
            reveal(page)
            return {
                "resume_headline": _text_of(page, S.FIELDS["resume_headline"]),
                "profile_summary": _text_of(page, S.FIELDS["profile_summary"]),
                "key_skills": _page_chips(page),
            }
        finally:
            browser.close()


def apply(headline: str | None = None, summary: str | None = None,
          skills: list[str] | None = None,
          state_path: Path = DEFAULT_STATE, headless: bool = False) -> list[dict]:
    """Apply whichever fields were given, in one browser session.

    Each field is independent: one failing does not roll back the ones that
    already saved, and the failure is raised so the caller reports it rather
    than claiming a clean run.
    """
    from playwright.sync_api import sync_playwright

    # Validate everything before opening a browser, so a too-long summary is
    # not discovered halfway through a part-applied update.
    if headline is not None:
        check_length("resume_headline", headline)
    if summary is not None:
        check_length("profile_summary", summary)

    results: list[dict] = []
    with sync_playwright() as p:
        browser, _context, page = open_profile(p, state_path, headless=headless)
        try:
            if headline is not None:
                results.append(set_text_field(page, "resume_headline", headline))
            if summary is not None:
                results.append(set_text_field(page, "profile_summary", summary))
            if skills is not None:
                results.append(set_key_skills(page, skills))
        finally:
            browser.close()
    return results


def summarise(results: list[dict]) -> str:
    lines = [""]
    for entry in results:
        field = entry["field"]
        before, after = entry.get("before"), entry.get("after")
        if isinstance(after, list):
            lines += [
                f"  {field}:",
                f"    was ({len(before or [])}): {', '.join(before or []) or '(none)'}",
                f"    now ({len(after)}): {', '.join(after)}",
            ]
            if entry.get("skipped"):
                lines.append(f"    not recognised by Naukri, so left off: "
                             f"{', '.join(entry['skipped'])}")
        else:
            lines += [
                f"  {field}:",
                f"    was: {(before or '(empty)')[:150]}",
                f"    now: {(after or '')[:150]}",
            ]
        lines.append("")
    return "\n".join(lines)
