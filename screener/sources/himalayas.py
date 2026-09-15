"""Remote listings from himalayas.app, filtered to ones you can actually take.

An addition to the Naukri run rather than a replacement for it: Naukri is where
the Indian market is, and this is where the worldwide-remote roles are. Both
end up in one shortlist, scored by the same rules.

Two things about this API shape the whole module, and neither is in the docs:

    it ignores every filter parameter

        `?country=India`, `?search=.NET`, `?categories=...` all return the
        same unfiltered feed, with the same totalCount. Nothing narrows the
        query server-side, so the only honest approach is to page through and
        filter here. That is why `pages` exists and why it costs a request per
        hundred listings.

    most of the board is closed to you

        `locationRestrictions` is the field that matters. Roughly 95% of
        postings are US- or EU-only, and a "remote" job restricted to the
        United States is not a remote job an applicant in India can take.
        Those are dropped at the source rather than scored and ranked into a
        shortlist you cannot act on.

Yield is therefore low by design - a few relevant jobs per run, not dozens.
That is the board being honest about what is open to you, not a bug here.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

from ..model import Job
from .base import SourceError

log = logging.getLogger("screener.sources.himalayas")

API = "https://himalayas.app/jobs/api"

# The server caps a page at 20 however large a `limit` you ask for - requesting
# 100 silently returns 20, so a "15 page" fetch quietly read 300 listings
# instead of the 1,500 it looked like. Stating the real number here keeps the
# page count in config.yaml meaning what it says.
PAGE_SIZE = 20
DEFAULT_PAGES = 60          # ~1,200 listings, roughly a minute
TIMEOUT = 40

# A posting with no restrictions at all is open worldwide. Otherwise it has to
# name somewhere you could work from.
OPEN_TO_INDIA = ("india", "worldwide", "anywhere", "global", "asia", "remote")


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"&amp;?", "&", text)
    return re.sub(r"\s+", " ", text).strip()


def eligible(restrictions: list | None) -> bool:
    """Whether someone working from India could hold this role."""
    if not restrictions:
        return True                      # unrestricted means worldwide
    joined = " ".join(str(r) for r in restrictions).lower()
    return any(term in joined for term in OPEN_TO_INDIA)


def _salary_label(record: dict) -> str | None:
    low, high = record.get("minSalary"), record.get("maxSalary")
    if not low and not high:
        return None
    currency = record.get("currency") or "USD"
    period = record.get("salaryPeriod") or "annual"
    if low and high:
        return f"{currency} {low:,.0f}-{high:,.0f} {period}"
    return f"{currency} {(low or high):,.0f} {period}"


def _posted(record: dict) -> tuple[str | None, int | None]:
    stamp = record.get("pubDate")
    if not stamp:
        return None, None
    try:
        when = datetime.fromtimestamp(int(stamp), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None, None
    days = (datetime.now(tz=timezone.utc) - when).days
    if days <= 0:
        return "Just now", int(when.timestamp() * 1000)
    if days == 1:
        return "1 Day Ago", int(when.timestamp() * 1000)
    return f"{days} Days Ago", int(when.timestamp() * 1000)


def to_job(record: dict) -> Job | None:
    url = record.get("applicationLink") or record.get("guid") or ""
    if not url:
        return None
    posted_label, created_ms = _posted(record)

    # Categories are the closest thing to Naukri's skill tags. They arrive
    # hyphenated ("Backend-Development"), which would never match a resume
    # skill, so they are unhyphenated before the scorer sees them.
    skills = [str(c).replace("-", " ").strip()
              for c in (record.get("categories") or []) if c]

    restrictions = record.get("locationRestrictions") or []
    where = "Remote"
    if restrictions:
        where = "Remote - " + ", ".join(str(r) for r in restrictions[:3])

    return Job(
        job_id="himalayas:" + (record.get("guid") or url).rsplit("/", 1)[-1],
        title=(record.get("title") or "").strip(),
        company=(record.get("companyName") or "").strip(),
        url=url,
        skills=skills,
        location=where,
        experience_label=", ".join(record.get("seniority") or []) or None,
        salary_label=_salary_label(record),
        # Left as None on purpose: Himalayas states a seniority band, not a
        # number of years, and inventing one would feed the experience-gap
        # check a figure nobody wrote down.
        min_exp=None,
        max_exp=None,
        description=_strip_html(record.get("description") or record.get("excerpt")),
        posted_label=posted_label,
        created_ms=created_ms,
        company_apply=True,        # applying always leaves the board
        has_questionnaire=False,
        source="himalayas",
    )


def fetch(pages: int = DEFAULT_PAGES) -> list[dict]:
    """Page the feed with its cursor. Returns raw records."""
    records: list[dict] = []
    cursor = None
    for page in range(max(1, pages)):
        url = f"{API}?limit={PAGE_SIZE}" + (f"&cursor={cursor}" if cursor else "")
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                payload = json.load(response)
        except urllib.error.URLError as exc:
            if not records:
                raise SourceError(f"Could not reach himalayas.app: {exc}")
            log.warning("himalayas: stopping at page %d (%s)", page, exc)
            break
        except (ValueError, TimeoutError) as exc:
            log.warning("himalayas: unreadable response on page %d (%s)", page, exc)
            break

        batch = payload.get("jobs") or []
        if not batch:
            break
        records += batch
        cursor = payload.get("nextCursor")
        if not cursor:
            break
    return records


class HimalayasSource:
    """Worldwide-remote listings, as a supplement to the main board."""

    name = "himalayas"

    def gather(self, config: dict) -> list[Job]:
        settings = config.get("himalayas") or {}
        pages = int(settings.get("pages") or DEFAULT_PAGES)

        records = fetch(pages)
        open_to_you = [r for r in records if eligible(r.get("locationRestrictions"))]

        jobs, seen = [], set()
        for record in open_to_you:
            job = to_job(record)
            if job and job.job_id not in seen:
                seen.add(job.job_id)
                jobs.append(job)

        log.info("himalayas: %d listings fetched, %d open to India, %d usable",
                 len(records), len(open_to_you), len(jobs))
        return jobs
