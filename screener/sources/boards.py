"""Remote job boards and company career pages, alongside the Naukri run.

Every board here is read over its own public feed - JSON API or RSS - with no
login and no browser. Each one lands in the same `Job` shape as Naukri, so the
scorer, ledger, spreadsheet and page treat them all alike.

    remote boards       himalayas, remotive, remoteok, jobicy,
                        weworkremotely, workingnomads
    hacker news         the latest "Ask HN: Who is hiring?" thread
    company ATS pages   greenhouse, lever, ashby - for companies you name

Three things hold for every one of them, and explain the shape of this module:

    no server-side filter is worth trusting

        Remotive answers every query with the same 15 newest jobs. RemoteOK's
        `?tag=` returns nothing for most tags. Jobicy maps `industry=dev` to
        whatever it likes. So each board pulls its feed and the scorer - which
        already knows your field, skills and title - does the filtering.

    "remote" rarely means "remote from where you are"

        Most listings are open to one country or region. `open_to()` keeps a
        listing only when it names somewhere in `remote_regions`, or names no
        restriction at all. Word-bounded, so "India" never matches "Indiana".

    one board failing must not lose the others

        Boards are fetched in parallel and each failure is reported by name in
        the run summary; the scan carries on with whatever came back.

Not here: Wellfound. It serves a Cloudflare Turnstile challenge to anything
that is not a real browser, and has no public API - see README.
"""
from __future__ import annotations

import html as html_mod
import json
import logging
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from ..model import Job
from .base import SourceError

log = logging.getLogger("screener.sources.boards")

TIMEOUT = 40
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# Places a listing can name that someone working from India can take. Override
# with `remote_regions:` in config.yaml if you are somewhere else.
DEFAULT_REGIONS = ["India", "APAC", "Asia", "Worldwide", "Anywhere", "Global"]

# Board name -> how it is labelled on the page and in the report.
LABELS = {
    "himalayas": "Himalayas",
    "remotive": "Remotive",
    "remoteok": "Remote OK",
    "jobicy": "Jobicy",
    "weworkremotely": "We Work Remotely",
    "workingnomads": "Working Nomads",
    "hn": "Hacker News",
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
}

# Boards that need a list of companies under `companies:` to do anything.
ATS_BOARDS = ("greenhouse", "lever", "ashby")

# Names people will reasonably try, with the reason they are not boards here.
UNSUPPORTED = {
    "wellfound": "Wellfound serves a Cloudflare Turnstile challenge to non-browser "
                 "clients and has no public API, so it cannot be read over HTTP.",
    "angellist": "AngelList Talent is now Wellfound - see `wellfound`.",
    "linkedin": "LinkedIn has its own browser-driven reader - set "
                "`include_linkedin: true` instead.",
    "naukri": "Naukri is the main source - set `source:` instead.",
}


# --- shared helpers -----------------------------------------------------

def strip_html(text: str | None) -> str:
    if not text:
        return ""
    # Greenhouse double-escapes its HTML ("&lt;p&gt;"), so unescape first or
    # the tags survive the strip as literal text.
    text = html_mod.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def open_to(where: str | None, regions: list[str]) -> bool:
    """Whether a listing's stated location includes somewhere in `regions`.

    A listing that states nothing is kept - an unrestricted remote job is open
    worldwide, and dropping blanks would lose more good jobs than it saves.
    """
    text = (where or "").strip()
    if not text:
        return True
    for region in regions:
        term = str(region).strip()
        if term and re.search(r"(?<![a-z])" + re.escape(term.lower()) + r"(?![a-z])",
                              text.lower()):
            return True
    return False


def regions_for(config: dict) -> list[str]:
    return [str(r) for r in (config.get("remote_regions") or DEFAULT_REGIONS) if str(r).strip()]


def posted(when: datetime | None) -> tuple[str | None, int | None]:
    """A Naukri-style posted label plus epoch-ms, from an aware datetime."""
    if when is None:
        return None, None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    days = (datetime.now(tz=timezone.utc) - when).days
    label = "Just now" if days <= 0 else "1 Day Ago" if days == 1 else f"{days} Days Ago"
    return label, int(when.timestamp() * 1000)


def parse_iso(text) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_epoch(seconds) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(seconds), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def as_list(value) -> list[str]:
    """Tags arrive as a list, a comma string, or occasionally None."""
    if not value:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return [str(v).strip() for v in value if str(v).strip()]


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/json, */*"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise SourceError(f"HTTP {exc.code} from {url}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SourceError(f"could not reach {url}: {exc}")


def fetch_json(url: str):
    try:
        return json.loads(fetch(url).decode("utf-8"))
    except ValueError as exc:
        raise SourceError(f"unreadable JSON from {url}: {exc}")


def _job(board: str, key, **fields) -> Job:
    """A Job from a board: namespaced id, always an off-site apply."""
    return Job(job_id=f"{board}:{key}", source=board,
               company_apply=True, has_questionnaire=False, **fields)


# --- remote boards ------------------------------------------------------

def remotive(config: dict) -> list[Job]:
    # The public API returns its 15 newest listings whatever you ask for -
    # `search`, `category` and `limit` are all accepted and ignored.
    payload = fetch_json("https://remotive.com/api/remote-jobs")
    regions = regions_for(config)
    jobs = []
    for r in payload.get("jobs") or []:
        where = r.get("candidate_required_location") or ""
        if not r.get("url") or not open_to(where, regions):
            continue
        label, ms = posted(parse_iso(r.get("publication_date")))
        jobs.append(_job(
            "remotive", r.get("id"),
            title=(r.get("title") or "").strip(),
            company=(r.get("company_name") or "").strip(),
            url=r["url"],
            skills=as_list(r.get("tags")),
            location="Remote - " + where if where else "Remote",
            salary_label=(r.get("salary") or "").strip() or None,
            description=strip_html(r.get("description")),
            posted_label=label, created_ms=ms,
        ))
    return jobs


def remoteok(config: dict) -> list[Job]:
    payload = fetch_json("https://remoteok.com/api")
    regions = regions_for(config)
    jobs = []
    # The first element is the API's terms of use, not a job.
    for r in (payload or [])[1:]:
        where = (r.get("location") or "").strip()
        url = r.get("url") or r.get("apply_url")
        if not url or not r.get("position") or not open_to(where, regions):
            continue
        low, high = r.get("salary_min") or 0, r.get("salary_max") or 0
        salary = f"USD {low:,.0f}-{high:,.0f} annual" if low and high else None
        label, ms = posted(parse_epoch(r.get("epoch")) or parse_iso(r.get("date")))
        jobs.append(_job(
            "remoteok", r.get("id"),
            title=r["position"].strip(),
            company=(r.get("company") or "").strip(),
            url=url,
            skills=as_list(r.get("tags")),
            location="Remote - " + where if where else "Remote",
            salary_label=salary,
            description=strip_html(r.get("description")),
            posted_label=label, created_ms=ms,
        ))
    return jobs


def jobicy(config: dict) -> list[Job]:
    # 100 is the API's cap per request.
    payload = fetch_json("https://jobicy.com/api/v2/remote-jobs?count=100&industry=dev")
    regions = regions_for(config)
    jobs = []
    for r in payload.get("jobs") or []:
        where = re.sub(r"\s*,\s*", ", ", r.get("jobGeo") or "").strip()
        if not r.get("url") or not open_to(where, regions):
            continue
        low, high = r.get("annualSalaryMin"), r.get("annualSalaryMax")
        salary = None
        if low and high:
            salary = f"{r.get('salaryCurrency') or 'USD'} {float(low):,.0f}-{float(high):,.0f} annual"
        label, ms = posted(parse_iso(r.get("pubDate")))
        jobs.append(_job(
            "jobicy", r.get("id"),
            title=strip_html(r.get("jobTitle")),
            company=(r.get("companyName") or "").strip(),
            url=r["url"],
            skills=as_list(r.get("jobIndustry")),
            location="Remote - " + where if where else "Remote",
            experience_label=r.get("jobLevel") or None,
            salary_label=salary,
            description=strip_html(r.get("jobDescription") or r.get("jobExcerpt")),
            posted_label=label, created_ms=ms,
        ))
    return jobs


WWR_CATEGORIES = [
    "remote-full-stack-programming-jobs",
    "remote-back-end-programming-jobs",
    "remote-front-end-programming-jobs",
    "remote-devops-sysadmin-jobs",
]


def weworkremotely(config: dict) -> list[Job]:
    settings = config.get("weworkremotely") or {}
    categories = settings.get("categories") or WWR_CATEGORIES
    regions = regions_for(config)
    jobs = []
    for category in categories:
        root = ET.fromstring(fetch(f"https://weworkremotely.com/categories/{category}.rss"))
        for item in root.findall("./channel/item"):
            jobs += [j for j in [_wwr_item(item, regions)] if j]
    return jobs


def _wwr_item(item, regions: list[str]) -> Job | None:
    link = (item.findtext("link") or item.findtext("guid") or "").strip()
    heading = (item.findtext("title") or "").strip()
    region = (item.findtext("region") or "").strip()
    if not link or not heading or not open_to(region, regions):
        return None
    # Titles are "Company: Role".
    company, _, title = heading.partition(": ")
    if not title:
        company, title = "", heading
    when = None
    try:
        when = parsedate_to_datetime(item.findtext("pubDate") or "")
    except (TypeError, ValueError):
        pass
    label, ms = posted(when)
    skills = [s.strip() for s in re.split(r",|\band\b", item.findtext("skills") or "") if s.strip()]
    return _job(
        "weworkremotely", link.rstrip("/").rsplit("/", 1)[-1],
        title=title.strip(), company=company.strip(), url=link,
        skills=skills,
        location="Remote - " + region if region else "Remote",
        description=strip_html(item.findtext("description")),
        posted_label=label, created_ms=ms,
    )


def workingnomads(config: dict) -> list[Job]:
    payload = fetch_json("https://www.workingnomads.com/api/exposed_jobs/")
    regions = regions_for(config)
    jobs = []
    for r in payload or []:
        where = (r.get("location") or "").strip()
        url = r.get("url")
        if not url or not open_to(where, regions):
            continue
        label, ms = posted(parse_iso(r.get("pub_date")))
        jobs.append(_job(
            "workingnomads", url.rstrip("/").rsplit("/", 1)[-1],
            title=(r.get("title") or "").strip(),
            company=(r.get("company_name") or "").strip(),
            url=url,
            skills=as_list(r.get("tags")),
            location="Remote - " + where if where else "Remote",
            description=strip_html(r.get("description")),
            posted_label=label, created_ms=ms,
        ))
    return jobs


def himalayas(config: dict) -> list[Job]:
    from .himalayas import HimalayasSource
    return HimalayasSource().gather(config)


# --- Hacker News --------------------------------------------------------

HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=10"
HN_ITEM = "https://hn.algolia.com/api/v1/items/{}"

# Named in an HN header, these narrow a remote role to somewhere specific.
HN_RESTRICTED = ("us", "usa", "u.s", "united states", "north america", "americas",
                 "canada", "europe", "eu", "uk", "emea", "latam", "cet", "est",
                 "pst", "pt", "et", "us-only", "us only")

ROLE_WORDS = re.compile(r"engineer|developer|swe|programmer|architect|devops|sre|"
                        r"scientist|analyst|lead|manager|designer|full[- ]?stack|"
                        r"back[- ]?end|front[- ]?end", re.I)


def hn(config: dict) -> list[Job]:
    hits = fetch_json(HN_SEARCH).get("hits") or []
    story = next((h for h in hits if "who is hiring" in (h.get("title") or "").lower()), None)
    if not story:
        raise SourceError("no 'Who is hiring?' thread found")
    thread = fetch_json(HN_ITEM.format(story["objectID"]))
    regions = regions_for(config)
    jobs = []
    for comment in thread.get("children") or []:
        job = hn_comment(comment, regions)
        if job:
            jobs.append(job)
    return jobs


def hn_comment(comment: dict, regions: list[str]) -> Job | None:
    """One top-level comment, by the thread's "Company | Role | Where" convention."""
    text = comment.get("text") or ""
    if not text or not comment.get("id"):
        return None
    header = strip_html(re.split(r"<p>", text, maxsplit=1)[0])
    parts = [p.strip() for p in header.split("|") if p.strip()]
    if len(parts) < 2:
        return None
    if not hn_remote_ok(header, regions):
        return None

    company = re.sub(r"\s*\(?https?://\S+\)?", "", parts[0]).strip()
    title = next((p for p in parts[1:] if ROLE_WORDS.search(p)), parts[1])
    where = next((p for p in parts[1:] if re.search(r"remote", p, re.I)), "Remote")
    label, ms = posted(parse_epoch(comment.get("created_at_i")))
    return _job(
        "hn", comment["id"],
        title=title[:120], company=company[:80],
        url=f"https://news.ycombinator.com/item?id={comment['id']}",
        location=where if "remote" in where.lower() else "Remote - " + where,
        description=strip_html(text),
        posted_label=label, created_ms=ms,
    )


def hn_remote_ok(header: str, regions: list[str]) -> bool:
    """Remote, and either open to one of `regions` or restricted nowhere."""
    lowered = header.lower()
    if "remote" not in lowered:
        return False
    if open_to(header, regions):
        return True
    return not any(re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", lowered)
                   for word in HN_RESTRICTED)


# --- company career pages (ATS) -----------------------------------------

def _ats_open(where: str, config: dict) -> bool:
    """On a company board, "open to you" also covers your own cities.

    "Remote" and "Hybrid" are left out of that list on purpose: "Remote - US"
    contains "Remote" and would otherwise let every US-only role through.
    """
    places = regions_for(config) + [
        str(p) for p in config.get("preferred_locations") or []
        if str(p).strip().lower() not in ("remote", "hybrid", "work from home")]
    return open_to(where, places)


def _companies(config: dict, board: str) -> list[str]:
    return [str(c).strip() for c in ((config.get("companies") or {}).get(board) or []) if str(c).strip()]


def greenhouse(config: dict) -> list[Job]:
    jobs = []
    for company in _companies(config, "greenhouse"):
        payload = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true")
        for r in payload.get("jobs") or []:
            where = ((r.get("location") or {}).get("name") or "").strip()
            if not r.get("absolute_url") or not _ats_open(where, config):
                continue
            label, ms = posted(parse_iso(r.get("first_published") or r.get("updated_at")))
            jobs.append(_job(
                "greenhouse", f"{company}-{r.get('id')}",
                title=(r.get("title") or "").strip(),
                company=(r.get("company_name") or company).strip(),
                url=r["absolute_url"],
                skills=[d.get("name") for d in r.get("departments") or [] if d.get("name")],
                location=where or None,
                description=strip_html(r.get("content")),
                posted_label=label, created_ms=ms,
            ))
    return jobs


def lever(config: dict) -> list[Job]:
    jobs = []
    for company in _companies(config, "lever"):
        payload = fetch_json(f"https://api.lever.co/v0/postings/{company}?mode=json")
        for r in payload or []:
            cats = r.get("categories") or {}
            places = cats.get("allLocations") or [cats.get("location") or ""]
            where = ", ".join(p for p in places if p)
            if (r.get("workplaceType") or "").lower() == "remote" and "remote" not in where.lower():
                where = f"Remote - {where}" if where else "Remote"
            if not r.get("hostedUrl") or not _ats_open(where, config):
                continue
            lists = " ".join(f"{block.get('text', '')} {strip_html(block.get('content'))}"
                             for block in r.get("lists") or [])
            label, ms = posted(parse_epoch((r.get("createdAt") or 0) / 1000))
            jobs.append(_job(
                "lever", r.get("id"),
                title=(r.get("text") or "").strip(),
                company=company,
                url=r["hostedUrl"],
                skills=[cats.get("team")] if cats.get("team") else [],
                location=where or None,
                description=" ".join([r.get("descriptionPlain") or "", lists]).strip(),
                posted_label=label, created_ms=ms,
            ))
    return jobs


def ashby(config: dict) -> list[Job]:
    jobs = []
    for company in _companies(config, "ashby"):
        payload = fetch_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{company}?includeCompensation=true")
        for r in payload.get("jobs") or []:
            if r.get("isListed") is False or not r.get("jobUrl"):
                continue
            places = [r.get("location") or ""] + [
                s.get("location") or "" for s in r.get("secondaryLocations") or []]
            where = ", ".join(p for p in places if p)
            if r.get("isRemote") and "remote" not in where.lower():
                where = f"Remote - {where}" if where else "Remote"
            if not _ats_open(where, config):
                continue
            label, ms = posted(parse_iso(r.get("publishedAt")))
            jobs.append(_job(
                "ashby", r.get("id"),
                title=(r.get("title") or "").strip(),
                company=company,
                url=r["jobUrl"],
                skills=[x for x in (r.get("department"), r.get("team")) if x],
                location=where or None,
                salary_label=(r.get("compensation") or {}).get("compensationTierSummary"),
                description=(r.get("descriptionPlain") or "").strip(),
                posted_label=label, created_ms=ms,
            ))
    return jobs


# --- running them -------------------------------------------------------

READERS = {
    "himalayas": himalayas,
    "remotive": remotive,
    "remoteok": remoteok,
    "jobicy": jobicy,
    "weworkremotely": weworkremotely,
    "workingnomads": workingnomads,
    "hn": hn,
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
}

# What `boards: [all]` means: every board that needs no further setup.
KEYLESS = [name for name in READERS if name not in ATS_BOARDS]


def selected(config: dict) -> list[str]:
    """The boards this run reads, in order, with `all` expanded.

    Company boards are added on their own when `companies:` names any, so
    listing companies is enough - there is no second switch to forget.
    """
    names = [str(b).strip().lower() for b in (config.get("boards") or []) if str(b).strip()]
    if config.get("include_himalayas"):
        names.append("himalayas")
    expanded: list[str] = []
    for name in names:
        for board in (KEYLESS if name == "all" else [name]):
            if board not in expanded:
                expanded.append(board)
    for board in ATS_BOARDS:
        if _companies(config, board) and board not in expanded:
            expanded.append(board)
    return expanded


def unknown(names) -> list[str]:
    """Board names that are not readers, with the reason when there is one."""
    problems = []
    for name in names or []:
        key = str(name).strip().lower()
        if key == "all" or key in READERS:
            continue
        problems.append(f"{key}: {UNSUPPORTED[key]}" if key in UNSUPPORTED else
                        f"{key}: not a board. Choose from: all, {', '.join(READERS)}")
    return problems


def too_old(job: Job, days) -> bool:
    if not days:
        return False
    age = job.age_days
    return age is not None and age > float(days)


def gather(config: dict) -> tuple[list[Job], list[str]]:
    """Read every selected board in parallel. Returns (jobs, failed boards)."""
    names = selected(config)
    if not names:
        return [], []

    def read(name: str):
        try:
            return name, READERS[name](config), None
        except Exception as exc:          # one board's bad day is not the run's
            return name, [], exc

    with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
        results = list(pool.map(read, names))

    window = config.get("posted_within_days")
    jobs: list[Job] = []
    failed: list[str] = []
    seen: set[str] = set()
    for name, found, error in results:
        if error is not None:
            log.warning("%s skipped: %s", name, error)
            failed.append(name)
            continue
        fresh = [j for j in found if not too_old(j, window)]
        kept = 0
        for job in fresh:
            if job.job_id in seen or not job.title:
                continue
            seen.add(job.job_id)
            jobs.append(job)
            kept += 1
        log.info("%s: %d listing(s) open to you, %d kept after the posted window "
                 "and de-duplication", name, len(found), kept)
    return jobs, failed
