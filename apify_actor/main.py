"""Apify actor: run the Naukri searches in the cloud, return the raw payloads.

The actor deliberately does no scoring. It navigates, captures the JSON each
search page fetches for itself, and pushes those records to the dataset
untouched - the same `Job.from_api` parser then handles both backends locally,
so the two can never drift apart in how they read a listing.

Two things about this actor are not optional, and both come from Naukri rather
than from Apify:

    headed browser   Akamai serves "Access Denied" to headless Chromium. The
                     base image starts Xvfb so a headed browser has a display.

    real navigation  /jobapi/v3/search is request-signed - it wants an nkparam
                     header and a token minted by the page. A direct fetch
                     returns 406, so each result set costs one page load.

Together those mean this actor is not cheap. It is a browser doing what a
person does, at roughly the pace a person does it.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from urllib.parse import urlencode

from apify import Actor
from playwright.async_api import async_playwright

BASE = "https://www.naukri.com"
SEARCH_API = "jobapi/v3/search"
RECOM_API = "search/recom-jobs"
RECOMMENDED_URL = f"{BASE}/mnjuser/recommendedjobs"

API_WAIT_SEC = 25

log = logging.getLogger("actor")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def search_url(keyword: str, location: str | None = None,
               experience: float | None = None, page_no: int = 1,
               job_age: int | None = None) -> str:
    """The same URL the site's own search box would produce."""
    path = f"{slug(keyword)}-jobs"
    if location:
        path += f"-in-{slug(location)}"
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


async def capture(page, url: str, marker: str) -> list[dict]:
    """Navigate and return the jobDetails from the page's own API call."""
    payloads: list[dict] = []

    async def handler(response):
        if marker not in response.url:
            return
        try:
            if "json" not in (response.headers.get("content-type") or ""):
                return
            data = await response.json()
        except Exception:
            return
        if isinstance(data, dict) and data.get("jobDetails"):
            payloads.append(data)

    page.on("response", handler)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        for _ in range(API_WAIT_SEC * 2):
            if payloads:
                break
            await page.wait_for_timeout(500)
        if not payloads:
            # Some result sets only fire once the list scrolls into view.
            await page.mouse.wheel(0, 1600)
            await page.wait_for_timeout(3000)
    except Exception as exc:
        log.warning("Navigation failed for %s: %s", url, str(exc)[:160])
    finally:
        page.remove_listener("response", handler)

    records: list[dict] = []
    for payload in payloads:
        records.extend(payload.get("jobDetails") or [])
    return records


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}

        searches = actor_input.get("searches") or []
        include_recommended = bool(actor_input.get("includeRecommended"))
        posted_days = actor_input.get("postedWithinDays")
        experience = actor_input.get("experienceYears")
        session_state = actor_input.get("sessionState")
        proxy_config = actor_input.get("proxy") or {"useApifyProxy": True,
                                                    "apifyProxyGroups": ["RESIDENTIAL"]}

        if not searches and not include_recommended:
            await Actor.fail(status_message="No searches given and recommendations are off.")
            return

        proxy = await Actor.create_proxy_configuration(actor_proxy_input=proxy_config)
        proxy_url = await proxy.new_url() if proxy else None

        job_age = int(posted_days) if posted_days else None
        seen: set[str] = set()
        pushed = 0

        async with async_playwright() as p:
            launch: dict = {
                # Headed under Xvfb - see the module docstring.
                "headless": False,
                "args": ["--no-sandbox", "--disable-dev-shm-usage",
                         "--disable-blink-features=AutomationControlled"],
            }
            if proxy_url:
                launch["proxy"] = {"server": proxy_url}

            browser = await p.chromium.launch(**launch)
            context_args: dict = {
                "viewport": {"width": 1440, "height": 900},
                "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/122.0.0.0 Safari/537.36"),
                "locale": "en-IN",
                "timezone_id": "Asia/Kolkata",
            }
            if session_state:
                context_args["storage_state"] = session_state

            context = await browser.new_context(**context_args)
            page = await context.new_page()

            async def absorb(records: list[dict], source: str) -> int:
                nonlocal pushed
                added = 0
                for record in records:
                    job_id = str(record.get("jobId") or "")
                    if not job_id or job_id in seen:
                        continue
                    seen.add(job_id)
                    await Actor.push_data({**record, "_source": source})
                    added += 1
                    pushed += 1
                return added

            try:
                if include_recommended:
                    if not session_state:
                        log.warning("Recommendations need a login; skipping.")
                    else:
                        Actor.log.info("Fetching recommendations")
                        added = await absorb(await capture(page, RECOMMENDED_URL, RECOM_API),
                                             "recommended")
                        Actor.log.info("  recommended: +%d", added)
                        await asyncio.sleep(random.uniform(2.5, 6.0))

                for entry in searches:
                    keyword = (entry or {}).get("keyword")
                    if not keyword:
                        continue
                    location = entry.get("location")
                    pages = max(1, int(entry.get("pages", 1)))
                    for page_no in range(1, pages + 1):
                        url = search_url(keyword, location, experience, page_no, job_age)
                        label = keyword + (f" in {location}" if location else "")
                        added = await absorb(await capture(page, url, SEARCH_API),
                                             f"search:{label}")
                        Actor.log.info("  %s p%d: +%d new (%d total)",
                                       label, page_no, added, pushed)
                        # Spaced the way a person reading results would.
                        await asyncio.sleep(random.uniform(2.5, 6.0))
            finally:
                await context.close()
                await browser.close()

        if pushed == 0:
            Actor.log.warning(
                "Nothing collected. Usually one of: an expired sessionState, "
                "an Akamai block (try a different proxy country), or searches "
                "that genuinely return nothing.")
        Actor.log.info("Done: %d records", pushed)


if __name__ == "__main__":
    asyncio.run(main())
