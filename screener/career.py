"""The Career profile section: what Naukri matches recruiter searches against.

Key skills say what you can do; this section says what you *want*, and Naukri's
recruiter filters read it directly - preferred work location, job role, industry,
department, salary and shift. A profile with the right skills and the wrong
preferred locations does not come back in the search at all.

Only the preferred-location list is writable here, because it is the one that
changes when you decide to look at a different city. The rest is read back so a
caller can check it rather than guess.

Two things about the location widget are worth knowing before changing it:

    the suggester is not the chip   Typing "Pune" offers "Pune, Maharashtra",
        and saving that produces a chip reading "Pune". Typing "Bangalore"
        offers "Bengaluru, Karnataka" for a chip reading "Bangalore/Bengaluru".
        So a match here is made on the first comma-separated part of the
        suggestion against every slash-separated part of the name, never on
        equality of the whole strings.

    "Remote" cannot be re-added   It is a live chip on profiles that have it,
        but the suggester answers "No Results Found" for it - Naukri seeds it
        elsewhere. Removing it is therefore permanent, which is why
        `set_locations` refuses to drop a chip the suggester cannot offer back
        unless it is explicitly named in `allow_lossy`.
"""
from __future__ import annotations

import logging

from .edit import EditError, _click_first, _settle, _type_first, reveal

log = logging.getLogger("screener.career")

CARD = "#lazyDesiredProfile"
TRIGGER = [
    "xpath=//*[normalize-space(text())='Career profile']/following-sibling::span[contains(@class,'edit')][1]",
    f"{CARD} .widgetHead .edit.icon",
    f"{CARD} .edit.icon",
]
# The dialog holds two identical chip widgets - preferred job role and preferred
# work location - and `.desiredLoc` wraps BOTH of them, so scoping to it reads
# the job roles as if they were cities. Scope to the `.sugComp` that actually
# contains the location input instead.
LOC = ".sugComp:has(#locationSugg)"
LOC_INPUT = ["#locationSugg"]
LOC_CHIP = f"{LOC} .chipsContainer .chip"
LOC_CHIP_LABEL = ".tagTxt"
LOC_CHIP_REMOVE = "a.close"
LOC_SUGGESTIONS = "#sugDrp_locationSugg li.sugTouple"
SAVE = ["button.btn-dark-ot:visible", "button:has-text('Save'):visible"]

SUGGESTION_WAIT_MS = 6000


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def _variants(name: str) -> set[str]:
    """Every spelling a chip or suggestion might use for one place.

    "Bangalore/Bengaluru" is one chip but two city names, and the suggester
    knows only the second. "Delhi / NCR" spaces its slash. Both have to fold to
    the same set for a name to be recognised as already present.
    """
    whole = _norm(name).lower()
    parts = {part.strip() for part in whole.split("/") if part.strip()}
    return {whole} | parts


def _same_place(a: str, b: str) -> bool:
    return bool(_variants(a) & _variants(b))


def open_editor(page) -> None:
    reveal(page)
    _click_first(page, TRIGGER, "career profile edit")
    page.wait_for_timeout(2500)


def locations(page) -> list[str]:
    """The preferred-location chips, in the order the open dialog holds them.

    The saved card underneath lists the same places in a different order, so it
    cannot be used to verify a change of membership on its own.
    """
    chips = page.locator(LOC_CHIP)
    out = []
    for i in range(chips.count()):
        label = chips.nth(i).locator(LOC_CHIP_LABEL)
        if label.count():
            out.append(_norm(label.first.inner_text(timeout=3000)))
    return out


def card_locations(page) -> list[str]:
    """Preferred work location as the saved card shows it - used to verify."""
    reveal(page)
    text = page.locator(CARD).first.inner_text(timeout=8000)
    lines = [line.strip() for line in text.splitlines()]
    for i, line in enumerate(lines):
        if line.lower().startswith("preferred work location") and i + 1 < len(lines):
            return [p.strip() for p in lines[i + 1].split(",") if p.strip()]
    return []


def _suggestion_for(page, name: str):
    """The dropdown entry that means `name`, or None.

    Never simply the first entry: typing "Bangalore" lists "Bengaluru, Karnataka"
    and "Bangalore Rural, Karnataka", and the second is a different district.
    """
    for _ in range(SUGGESTION_WAIT_MS // 500):
        page.wait_for_timeout(500)
        options = page.locator(LOC_SUGGESTIONS)
        texts = options.all_inner_texts()
        for index, text in enumerate(texts):
            head = _norm(text).split(",")[0]
            if head and _same_place(head, name):
                return options.nth(index)
        if texts and "no results" in _norm(texts[0]).lower():
            return None
    return None


def _dismiss(page) -> None:
    """Empty the suggester and close whatever it is showing.

    Focusing this input opens a "top cities" panel that is laid over the chips
    below it, and it stays up while the box has focus. A chip's remove icon
    underneath then resolves and reports itself visible, but every click on it
    is swallowed by the panel - Playwright names `.topCitiesSuggestions` as the
    element intercepting pointer events. Tab, not Escape: Escape closes the
    whole Career profile dialog and discards the edit.
    """
    try:
        box = page.locator(LOC_INPUT[0]).first
        if box.count():
            box.fill("")
            page.wait_for_timeout(200)
            page.keyboard.press("Tab")
            page.wait_for_timeout(600)
    except Exception as exc:
        log.debug("Could not dismiss the location suggester: %s", exc)


def _typeable(name: str) -> list[str]:
    """What to type to find `name`, most specific first.

    A chip's own label is often not a string the suggester knows: it answers
    nothing for "Hyderabad/Secunderabad" or "Delhi / NCR", because those are
    Naukri's composite labels rather than city names. Typing one side of the
    slash finds them. Without this, every composite-named city looked
    un-re-addable and `set_locations` refused to remove it.
    """
    whole = _norm(name)
    parts = [p.strip() for p in whole.split("/") if p.strip()]
    out = [whole] + [p for p in parts if p.lower() != whole.lower()]
    return out


def offers(page, name: str) -> bool:
    """Whether the suggester can produce `name` - i.e. whether losing it is safe."""
    for term in _typeable(name):
        _type_first(page, LOC_INPUT, term, "preferred location")
        if _suggestion_for(page, name) is not None:
            _dismiss(page)
            return True
    _dismiss(page)
    return False


def _add(page, name: str) -> bool:
    for term in _typeable(name):
        _type_first(page, LOC_INPUT, term, "preferred location")
        option = _suggestion_for(page, name)
        if option is not None:
            option.click(timeout=5000)
            page.wait_for_timeout(700)
            _dismiss(page)
            return True
    _dismiss(page)
    log.warning("Location suggester does not offer %r", name)
    return False


def _remove(page, name: str) -> bool:
    """Take one place off the list. True if the chip actually went.

    The dialog renders a "top cities" quick-pick panel over the chip area, and
    it is part of the layout rather than a dropdown - dismissing the suggester
    does not move it. A real click on a chip's remove icon underneath it is
    swallowed, so this falls back to dispatching the event at the icon, and then
    proves the chip is gone by reading the list back rather than trusting either
    click to have worked.
    """
    _dismiss(page)
    chips = page.locator(LOC_CHIP)
    for i in range(chips.count()):
        label = chips.nth(i).locator(LOC_CHIP_LABEL)
        if not (label.count() and _same_place(label.first.inner_text(timeout=3000), name)):
            continue
        icon = chips.nth(i).locator(LOC_CHIP_REMOVE).first
        try:
            icon.click(timeout=3000)
        except Exception:
            icon.dispatch_event("click")
        page.wait_for_timeout(700)
        return not any(_same_place(c, name) for c in locations(page))
    return False


def set_locations(page, wanted: list[str], allow_lossy: tuple[str, ...] = ()) -> dict:
    """Make the preferred-location list exactly `wanted`.

    Adds before it removes, so a failed add never leaves the list shorter than
    it started. A chip the suggester cannot offer back is kept and reported
    rather than dropped - removing it would be permanent - unless its name is
    passed in `allow_lossy`.
    """
    wanted = [_norm(w) for w in wanted if _norm(w)]
    if not wanted:
        raise EditError("Refusing to empty your preferred work locations.")

    open_editor(page)
    before = locations(page)

    missing = [w for w in wanted if not any(_same_place(w, c) for c in before)]
    extra = [c for c in before if not any(_same_place(w, c) for w in wanted)]

    added, failed_add, removed, kept = [], [], [], []
    for name in missing:
        (added if _add(page, name) else failed_add).append(name)

    for name in extra:
        if any(_same_place(name, a) for a in allow_lossy) or offers(page, name):
            if _remove(page, name):
                removed.append(name)
        else:
            kept.append(name)
            log.warning("Keeping %r: the suggester cannot offer it back, so removing "
                        "it would be permanent", name)

    if not locations(page):
        page.keyboard.press("Escape")
        raise EditError("Every preferred location came off and none went on - the "
                        "dialog was cancelled, nothing was saved.")

    _click_first(page, SAVE, "career profile save")
    _settle(page)

    after = card_locations(page)
    log.info("Preferred locations: +%d -%d, now %d", len(added), len(removed), len(after))
    return {"field": "preferred_locations", "before": before, "after": after,
            "added": added, "removed": removed, "kept": kept, "failed_add": failed_add}


def read(page) -> dict:
    """The Career profile card as a field -> value mapping. Changes nothing."""
    reveal(page)
    text = page.locator(CARD).first.inner_text(timeout=8000)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    known = ("Current industry", "Department", "Role category", "Job role",
             "Desired job type", "Desired employment type", "Preferred job role",
             "Preferred work location", "Preferred annual salary", "Preferred shift")
    out: dict[str, str] = {}
    for i, line in enumerate(lines):
        if line in known and i + 1 < len(lines):
            out[line] = lines[i + 1]
    return out
