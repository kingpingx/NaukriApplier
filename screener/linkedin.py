"""LinkedIn job search, on a session you sign in to by hand.

Same shape as naukri/session.py, and for the same reasons: `login()` opens a
real browser and waits for *you* to sign in, so 2FA, captcha and device
verification all work because a human is there. The password never enters this
codebase. Cookies land in data/linkedin_state.json, which is gitignored and
*is* your login - treat it like a password.

LinkedIn is stricter than Naukri about automation and a restriction costs you
your professional network, not just a job board. So this module only ever
reads: it navigates search pages in a visible browser at human pace and parses
what renders. It does not apply, message, connect, or touch anything else.
"""
from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path
from urllib.parse import urlencode

log = logging.getLogger("screener.linkedin")

from .paths import LINKEDIN_STATE as STATE_PATH

LOGIN_URL = "https://www.linkedin.com/login"
JOBS_URL = "https://www.linkedin.com/jobs/search/"

# URL fragments that only appear once signed in.
LOGGED_IN_URL_MARKERS = ("/feed", "/jobs", "/mynetwork", "/in/")

LOGGED_IN_MARKERS = [
    "img.global-nav__me-photo",
    ".global-nav__me",
    "[data-test-global-nav]",
    "#global-nav",
]


class NotLoggedIn(RuntimeError):
    """No usable saved LinkedIn session."""


def is_logged_in(page) -> bool:
    url = page.url or ""
    if "/login" in url or "/checkpoint" in url or "/authwall" in url:
        return False
    if any(marker in url for marker in LOGGED_IN_URL_MARKERS):
        return True
    for selector in LOGGED_IN_MARKERS:
        try:
            if page.locator(selector).first.is_visible(timeout=1500):
                return True
        except Exception:
            continue
    return False


def login(state_path: Path = STATE_PATH, timeout_sec: int = 420) -> bool:
    """Open a browser, wait for a manual sign-in, save the session."""
    from playwright.sync_api import sync_playwright

    state_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")

        print("\n  A browser window is open. Sign in to LinkedIn there.")
        print("  Complete any 2FA or captcha as normal - just finish the login.")
        print(f"  Waiting up to {timeout_sec // 60} minutes...\n")

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if is_logged_in(page):
                page.wait_for_timeout(3000)  # let post-login redirects settle
                context.storage_state(path=str(state_path))
                log.info("LinkedIn session saved to %s", state_path)
                print(f"  Login captured. Session saved to {state_path}")
                browser.close()
                return True
            page.wait_for_timeout(1000)

        print("  Timed out waiting for login.")
        browser.close()
        return False


def open_session(p, state_path: Path = STATE_PATH, headless: bool = False):
    """Return (browser, context, page) on a signed-in LinkedIn."""
    if not state_path.exists():
        raise NotLoggedIn(
            f"No saved LinkedIn session at {state_path}. "
            "Run: python main.py --linkedin-login"
        )

    browser = p.chromium.launch(headless=headless)
    context = browser.new_context(
        storage_state=str(state_path),
        viewport={"width": 1440, "height": 900},
    )
    page = context.new_page()
    page.goto(JOBS_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)

    if not is_logged_in(page):
        browser.close()
        raise NotLoggedIn(
            "Saved LinkedIn session has expired. Run: python main.py --linkedin-login"
        )
    return browser, context, page


def search_url(keyword: str, location: str | None = None,
               posted_days: int | None = 30, start: int = 0,
               remote_only: bool = False) -> str:
    """Build a LinkedIn job-search URL.

    `remote_only` sets f_WT=2, LinkedIn's own remote workplace-type filter -
    far more reliable than searching for the word "remote", which also matches
    on-site jobs whose description merely mentions remote working.
    """
    params: dict[str, str] = {"keywords": keyword}
    params["location"] = location or "Worldwide"
    if posted_days:
        params["f_TPR"] = f"r{posted_days * 86400}"
    if remote_only:
        params["f_WT"] = "2"
    if start:
        params["start"] = str(start)
    return f"{JOBS_URL}?{urlencode(params)}"


def pause() -> None:
    """Human-paced spacing between navigations."""
    time.sleep(random.uniform(3.5, 8.0))


# Card extraction.
#
# The results <ul> and many wrappers carry per-build hashed class names
# ("UkEyRxhyFDHcDkoheSQJJTkPyisrDmE"), which are worthless as selectors. The
# design-system names - artdeco-entity-lockup__*, job-card-container__* - are
# stable across builds, so everything below anchors on those, and on the
# /jobs/view/ link that every real result card contains.
_EXTRACT_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const pick = (root, sel) => {
    const el = root.querySelector(sel);
    return el ? norm(el.innerText) : null;
  };
  const out = [];
  const seen = new Set();
  document.querySelectorAll('a[href*="/jobs/view/"]').forEach(anchor => {
    const card = anchor.closest('li') || anchor.closest('[data-job-id]');
    if (!card) return;
    const href = anchor.getAttribute('href') || '';
    const match = href.match(/\/jobs\/view\/(\d+)/);
    if (!match) return;
    const jobId = match[1];
    if (seen.has(jobId)) return;
    seen.add(jobId);

    // The visible title is in <strong>; a visually-hidden span repeats it with
    // extra words, so prefer the strong and fall back to the anchor's label.
    const title = pick(card, '.artdeco-entity-lockup__title strong')
               || pick(card, '.artdeco-entity-lockup__title')
               || norm(anchor.getAttribute('aria-label') || anchor.innerText);

    const meta = Array.from(card.querySelectorAll('.job-card-container__metadata-wrapper li'))
      .map(li => norm(li.innerText)).filter(Boolean);
    const footer = Array.from(card.querySelectorAll('.job-card-container__footer-item'))
      .map(li => norm(li.innerText)).filter(Boolean);
    const text = norm(card.innerText);

    out.push({
      job_id: jobId,
      url: 'https://www.linkedin.com/jobs/view/' + jobId + '/',
      title: title,
      company: pick(card, '.artdeco-entity-lockup__subtitle'),
      location: pick(card, '.artdeco-entity-lockup__caption'),
      metadata: meta,
      footer: footer,
      insight: pick(card, '.job-card-container__job-insight-text'),
      easy_apply: /easy apply/i.test(text),
      promoted: /promoted/i.test(text),
      viewed: /\bviewed\b/i.test(text),
    });
  });
  return out;
}
"""


def _load_all_cards(page, want: int = 25, rounds: int = 14) -> int:
    """Scroll results into view until the card count stops growing.

    LinkedIn renders about nine cards and lazy-loads the rest as they are
    scrolled to, so a single read returns a third of the page. Scrolling the
    window is not enough - the list is its own scroll container, so this walks
    the last card into view instead.
    """
    previous = 0
    for _ in range(rounds):
        count = page.evaluate(
            """() => {
                const links = document.querySelectorAll('a[href*="/jobs/view/"]');
                if (!links.length) return 0;
                const last = links[links.length - 1].closest('li') || links[links.length - 1];
                // The results list is its own scroll container, so nudging the
                // window does nothing. Walk up to the nearest actually
                // scrollable ancestor and advance that instead - without this
                // only the first ~9 of 25 cards ever render.
                let el = last.parentElement;
                while (el && el !== document.body) {
                  const style = getComputedStyle(el);
                  if (/(auto|scroll)/.test(style.overflowY)
                      && el.scrollHeight > el.clientHeight + 40) {
                    el.scrollTop = Math.min(el.scrollTop + el.clientHeight * 0.9,
                                            el.scrollHeight);
                    return links.length;
                  }
                  el = el.parentElement;
                }
                last.scrollIntoView({block: 'end'});
                window.scrollBy(0, 700);
                return links.length;
            }"""
        )
        if count >= want or count == previous:
            break
        previous = count
        page.wait_for_timeout(1400)
    page.wait_for_timeout(800)
    return page.evaluate("() => document.querySelectorAll('a[href*=\\\"/jobs/view/\\\"]').length")


def search(page, keyword: str, location: str | None = None,
           posted_days: int | None = 30, pages: int = 1,
           remote_only: bool = False) -> list[dict]:
    """Return raw card dicts for one keyword/location, across `pages` pages."""
    found: dict[str, dict] = {}
    for index in range(pages):
        url = search_url(keyword, location, posted_days, index * 25, remote_only)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            loaded = _load_all_cards(page)
            cards = page.evaluate(_EXTRACT_JS)
        except Exception as exc:
            log.warning("LinkedIn search failed for %s: %s", url, str(exc)[:160])
            continue
        added = 0
        for card in cards:
            if card["job_id"] not in found:
                card["source"] = (f"linkedin:{keyword}"
                                  + (" remote" if remote_only else "")
                                  + (f" in {location}" if location else " worldwide"))
                card["remote_filtered"] = remote_only
                found[card["job_id"]] = card
                added += 1
        log.info("  linkedin %s%s p%d: %d card(s) loaded, +%d new",
                 keyword, f" in {location}" if location else "", index + 1, loaded, added)
        if not added:
            break
        pause()
    return list(found.values())
