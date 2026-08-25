"""Scrape the Naukri profile into a structured file for analysis.

Extraction is deliberately best-effort per field: a selector that stops
matching after a Naukri redesign degrades that one field to null instead of
killing the run. Three artifacts are written every time, so the analysis can
proceed even when structured scraping partly fails:

    data/profile.json  - structured fields
    data/profile.txt   - raw visible text of the whole page
    data/profile.png   - full-page screenshot

The raw text dump is the safety net. Class names churn; the words on the page
do not.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from . import selectors as S
from .session import DEFAULT_STATE, open_profile

log = logging.getLogger("screener.extract")

from .paths import HOME as DATA_DIR

# Shared text reader, injected into both helpers below.
#
# Naukri draws its icons with an icon font whose ligature name *is* the
# element's text content - an edit pencil is literally the string
# "editOneTheme", a location pin is "locationOt". A plain innerText read
# therefore mixes control names into the profile data, which is how
# resume_headline once came back as "editOneTheme".
#
# This walks visible text nodes and skips icons, action links and the hidden
# edit drawers. It deliberately avoids the shorter clone-strip-innerText
# trick: a detached clone has no layout, so every hidden drawer would read as
# visible and its form contents would land in the extract.
_CLEAN_JS_BODY = """
  const SKIP = '[class*="icon" i], .morelink, .lightbox, .crossLayer, .add, .hide, script, style, noscript';
  const clean = (el) => {
    if (!el) return null;
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const parts = [];
    let node;
    while ((node = walker.nextNode())) {
      const parent = node.parentElement;
      if (!parent || parent.closest(SKIP)) continue;
      if (parent.checkVisibility && !parent.checkVisibility()) continue;
      const text = (node.nodeValue || '').replace(/\\s+/g, ' ').trim();
      if (text) parts.push(text);
    }
    return parts.join(' ').replace(/\\s+/g, ' ').trim() || null;
  };
"""

_CLEAN_TEXT_JS = "(el) => {" + _CLEAN_JS_BODY + " return clean(el); }"

# Fallback lookup, used when a field's CSS selectors all miss.
#
# Anchors on the visible section title, then takes the .widgetCont of the card
# that owns it. Climbing to the nearest #lazy* root is what makes this safe:
# the "Quick links" sidebar repeats every section name verbatim, and an earlier
# version that merely climbed N parents from the matched text walked straight
# into that nav and returned the same list of link labels for every field.
_WIDGET_JS = """
(payload) => {
""" + _CLEAN_JS_BODY + """
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const titles = Array.from(document.querySelectorAll('.widgetTitle, .widgetHead .typ-16Bold'));
  for (const want of payload.headings) {
    for (const title of titles) {
      if (norm(title.textContent).toLowerCase() !== want.toLowerCase()) continue;
      const root = title.closest('[id^="lazy"]') || title.closest('.card');
      if (!root) continue;
      const content = root.querySelector('.widgetCont') || root;
      const text = clean(content);
      if (text) return text;
    }
  }
  return null;
}
"""


def _first_text(page, candidates: list[str]) -> str | None:
    """Text of the first candidate selector that resolves to something."""
    for selector in candidates:
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            text = locator.evaluate(_CLEAN_TEXT_JS)
            if text:
                return text
        except Exception as exc:
            log.debug("selector %s failed: %s", selector, exc)
    return None


def _all_texts(page, candidates: list[str]) -> list[str]:
    """Texts of every match for the first candidate selector that hits."""
    for selector in candidates:
        try:
            locator = page.locator(selector)
            count = locator.count()
            if count == 0:
                continue
            out = []
            for i in range(min(count, 60)):
                text = locator.nth(i).evaluate(_CLEAN_TEXT_JS)
                if text and text not in S.LIST_NOISE:
                    out.append(text)
            if out:
                return out
        except Exception as exc:
            log.debug("selector %s failed: %s", selector, exc)
    return []


def _by_widget(page, field: str) -> str | None:
    """Fallback: find a field by the visible title of the card that holds it."""
    headings = S.HEADING_FALLBACKS.get(field)
    if not headings:
        return None
    try:
        return page.evaluate(_WIDGET_JS, {"headings": headings})
    except Exception as exc:
        log.debug("widget fallback for %s failed: %s", field, exc)
        return None


def _expand_read_more(page) -> int:
    """Click every 'Read More' so long descriptions dump in full, not truncated.

    The employment description and profile summary - the two blocks worth the
    most analysis - are collapsed by default. Returns how many were expanded.
    """
    clicked = 0
    for selector in ("a.morelink", "text=/^\\s*Read More\\s*$/i", ".read-more", "[class*='readMore']"):
        try:
            locator = page.locator(selector)
            for i in range(min(locator.count(), 12)):
                try:
                    locator.nth(i).click(timeout=2500)
                    page.wait_for_timeout(400)
                    clicked += 1
                except Exception:
                    continue
        except Exception as exc:
            log.debug("read-more selector %s failed: %s", selector, exc)
    if clicked:
        page.wait_for_timeout(800)
        log.info("Expanded %d truncated section(s)", clicked)
    return clicked


def extract(state_path: Path = DEFAULT_STATE, headless: bool = True) -> dict:
    """Scrape the profile and write profile.json / .txt / .png. Returns the dict."""
    from playwright.sync_api import sync_playwright

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    profile: dict = {"source": S.PROFILE_URL}

    with sync_playwright() as p:
        browser, _context, page = open_profile(p, state_path, headless=headless)
        try:
            # Lazy widgets only render once scrolled into view.
            for _ in range(6):
                page.mouse.wheel(0, 1200)
                page.wait_for_timeout(600)
            page.mouse.wheel(0, -20000)
            page.wait_for_timeout(1000)

            _expand_read_more(page)

            for field, candidates in S.FIELDS.items():
                profile[field] = _first_text(page, candidates) or _by_widget(page, field)

            for field, candidates in S.LIST_FIELDS.items():
                values = _all_texts(page, candidates)
                profile[field] = values if values else (_by_widget(page, field) or [])

            # Sections with no stable selector of their own.
            for field in ("certifications", "languages"):
                profile[field] = _by_widget(page, field)

            # "Languages" is a sub-heading inside the Personal details card,
            # so the widget lookup resolves both to that card's whole content.
            # Report it missing rather than duplicating the wrong block.
            if profile.get("languages") == profile.get("personal_details"):
                profile["languages"] = None

            raw = page.locator("body").inner_text(timeout=10000)
            (DATA_DIR / "profile.txt").write_text(raw, encoding="utf-8")
            page.screenshot(path=str(DATA_DIR / "profile.png"), full_page=True)

            profile["last_updated_hint"] = _find_last_updated(raw)
        finally:
            browser.close()

    (DATA_DIR / "profile.json").write_text(
        json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    filled = sum(1 for v in profile.values() if v)
    log.info("Extracted %d/%d fields", filled, len(profile))
    return profile


def _find_last_updated(raw_text: str) -> str | None:
    """Pull the 'last updated' line out of the raw page text, if present."""
    match = re.search(r"(last\s*updated[^\n]{0,60})", raw_text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def summarise(profile: dict) -> str:
    """Short human-readable report of what came back - printed after a run."""
    lines = ["", "  Extracted profile", "  " + "-" * 40]
    for key, value in profile.items():
        if isinstance(value, list):
            status = f"{len(value)} item(s)" if value else "MISSING"
        elif value:
            flat = re.sub(r"\s+", " ", str(value))
            status = flat[:70] + ("..." if len(flat) > 70 else "")
        else:
            status = "MISSING"
        lines.append(f"  {key:22} {status}")
    lines.append("")
    return "\n".join(lines)
