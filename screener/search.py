"""Collect job listings by driving Naukri's own search pages.

Naukri renders results from two JSON endpoints, and we read the payloads the
page fetches for itself rather than scraping the result cards:

    /jobapi/v3/search          keyword + location search
    /jobapi/v2/search/recom-jobs  Naukri's recommendations for your profile

Calling those endpoints directly does not work. They are signed: the request
carries an `nkparam` header and a bearer token minted by the page, and a fetch
issued from inside the page without them comes back 406. So every result set
costs one real navigation. That is slower than a bare HTTP client and it is
also the point - the traffic is a logged-in browser loading search pages, at
the rate a person loads them.

Breadth beats depth: several distinct searches return more usable jobs than
paging deep into one, because page 5 of a query is the tail of a ranking that
already put its best matches on page 1.
"""
from __future__ import annotations

import logging
import random
import re
import time
from urllib.parse import urlencode

from .model import Job

log = logging.getLogger("screener.search")

BASE = "https://www.naukri.com"
SEARCH_API = "jobapi/v3/search"
RECOM_API = "search/recom-jobs"
RECOMMENDED_URL = f"{BASE}/mnjuser/recommendedjobs"

# How long to wait for the page's own XHR after the document has loaded.
API_WAIT_SEC = 25


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def search_url(keyword: str, location: str | None = None,
               experience: float | None = None, page_no: int = 1,
               job_age: int | None = None) -> str:
    """Build the same URL the site's own search box would produce.

    `job_age` is Naukri's own Freshness facet, in days - the same filter as
    clicking "Last 1 day" in the sidebar. Asking the board for fresh results
    matters more than it looks: a query returns ~20 results ranked by
    relevance, not by date, so filtering a stale page afterwards leaves you
    with almost nothing. This moves the filter to where the ranking happens.

    It is still only a hint. If Naukri ever renames the facet the search
    degrades to unfiltered results, which the caller's own age check then
    trims - fewer jobs, never wrong ones.
    """
    path = f"{_slug(keyword)}-jobs"
    if location:
        path += f"-in-{_slug(location)}"
    if page_no > 1:
        path += f"-{page_no}"

    params: dict[str, str] = {"k": keyword}
    if location:
        params["l"] = location
    if experience is not None:
        params["experience"] = str(int(experience))
    if job_age:
        params["jobAge"] = str(int(job_age))
    if page_no > 1:
        params["pageNo"] = str(page_no)
    return f"{BASE}/{path}?{urlencode(params)}"


def _capture(page, url: str, marker: str) -> list[dict]:
    """Navigate and return the jobDetails from the page's own API call."""
    payloads: list[dict] = []

    def handler(response):
        if marker not in response.url:
            return
        try:
            if "json" not in (response.headers.get("content-type") or ""):
                return
            data = response.json()
        except Exception:
            return
        if isinstance(data, dict) and data.get("jobDetails"):
            payloads.append(data)

    page.on("response", handler)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        deadline = time.time() + API_WAIT_SEC
        while time.time() < deadline and not payloads:
            page.wait_for_timeout(500)
        # Some result sets only fire once the list scrolls into view.
        if not payloads:
            page.mouse.wheel(0, 1600)
            page.wait_for_timeout(3000)
    except Exception as exc:
        log.warning("Navigation failed for %s: %s", url, str(exc)[:160])
    finally:
        page.remove_listener("response", handler)

    records: list[dict] = []
    for payload in payloads:
        records.extend(payload.get("jobDetails") or [])
    return records


def _pause() -> None:
    """Space navigations out the way a person reading results would."""
    time.sleep(random.uniform(2.5, 6.0))


def gather(page, config: dict) -> list[Job]:
    """Run every configured search plus recommendations; dedupe by jobId."""
    found: dict[str, Job] = {}

    def absorb(records: list[dict], source: str) -> int:
        added = 0
        for record in records:
            job = Job.from_api(record, source=source)
            if not job.job_id or not job.url:
                continue
            if job.job_id not in found:
                found[job.job_id] = job
                added += 1
        return added

    job_age = config.get("posted_within_days")
    job_age = int(job_age) if job_age else None

    if config.get("include_recommended"):
        # The recommendations feed has no freshness facet, so under a date
        # filter most of what it returns is trimmed by the caller's age check.
        log.info("Fetching Naukri's recommendations for your profile")
        added = absorb(_capture(page, RECOMMENDED_URL, RECOM_API), "recommended")
        log.info("  recommended: +%d", added)
        _pause()

    for entry in config.get("searches") or []:
        keyword = (entry or {}).get("keyword")
        if not keyword:
            continue
        location = entry.get("location")
        pages = max(1, int(entry.get("pages", 1)))
        for page_no in range(1, pages + 1):
            url = search_url(keyword, location, config.get("profile_years"), page_no,
                             job_age=job_age)
            label = f"{keyword}" + (f" in {location}" if location else "") + (f" p{page_no}" if page_no > 1 else "")
            added = absorb(_capture(page, url, SEARCH_API), f"search:{label}")
            log.info("  %s: +%d new (%d total)", label, added, len(found))
            _pause()

    log.info("Collected %d distinct jobs", len(found))
    return list(found.values())
