"""Job listings via Playwright on this machine, using your saved session.

The default backend, and the one that works with nothing but a pip install.
It drives real search pages in a real browser, reading the JSON each page
fetches for itself - see screener/search.py for why the endpoints cannot be
called directly.

Headed by default, and that is not a stylistic choice: Naukri sits behind
Akamai, which serves "Access Denied" to headless Chromium. `headless: true`
is honoured if you ask for it, and will probably fail.
"""
from __future__ import annotations

import logging

from playwright.sync_api import sync_playwright

from .. import search as search_mod
from ..paths import NAUKRI_STATE
from ..session import NotLoggedIn, open_profile
from .base import SourceError

log = logging.getLogger("screener.sources.local")


class LocalSource:
    name = "local"

    def gather(self, config: dict) -> list:
        headless = bool(config.get("headless", False))
        if headless:
            log.warning(
                "Running headless. Naukri's bot protection usually blocks this - "
                "if every search returns nothing, that is why.")

        try:
            with sync_playwright() as p:
                browser, _context, page = open_profile(p, NAUKRI_STATE, headless=headless)
                try:
                    jobs = search_mod.gather(page, config)
                finally:
                    browser.close()
        except NotLoggedIn:
            raise
        except Exception as exc:
            raise SourceError(f"Local search failed: {exc}") from exc

        if not jobs:
            log.warning(
                "No jobs collected. Common causes: an expired session (re-run "
                "--login), searches too narrow, or a headless run being blocked.")
        return jobs
