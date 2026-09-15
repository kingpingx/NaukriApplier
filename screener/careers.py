"""The employer's own page for a job found on an aggregator board.

We Work Remotely's "Apply" goes through We Work Remotely. For listings from
there the screener also finds where the employer publishes the same opening,
so you can apply at the source. In order:

    1. a link in the posting    the employer's careers/apply URL, when the
                                description carries one
    2. the exact posting        on the company's own applicant tracking system
                                - Greenhouse, Lever, Ashby, SmartRecruiters,
                                Recruitee or Workable - matched by title
    3. the company's job board  when the board is found but no title matches
    4. a web search             always there as the last resort

Measured on 2026-09-15 across 146 We Work Remotely listings: 26 carried an
employer link, 55 matched their exact posting, and 68 of 107 companies had a
findable board.

A board is found by guessing its slug from the company name, and a guess can
land on a different company with the same name. An exact title match on that
board makes the mix-up unlikely, so step 2 accepts any guess. Step 3 has no
such check, so it only accepts the two strongest guesses: the name run
together, with and without suffixes like "Inc" or "Labs".
"""
from __future__ import annotations

import html as html_mod
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("screener.careers")

# Boards whose listings get an employer page looked up.
BOARDS = {"weworkremotely"}

TIMEOUT = 20
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

SKIP_LINK = re.compile(r"weworkremotely|imgix|linkedin\.com|twitter\.com|//x\.com|facebook|"
                       r"instagram|youtube|glassdoor|//t\.co/", re.I)
ATS_HOST = re.compile(r"greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|smartrecruiters\.com|"
                      r"recruitee\.com|bamboohr\.com|myworkdayjobs\.com|teamtailor\.com|breezy\.hr|"
                      r"jobvite\.com|personio\.|click2apply", re.I)
# A link to one opening, versus a link to the careers site in general.
# "/jobs/" alone is the second kind; "/jobs/backend-engineer" is the first.
APPLY_LINK = re.compile(r"apply|gh_jid|/jobs?/[^/?#]+|/job/|/positions?/[^/?#]+|/openings?/[^/?#]+", re.I)
CAREERS_LINK = re.compile(r"career|/jobs?\b", re.I)
# Pages a posting links from its "careers" section that are not a job at all -
# Samsara's links "careers/benefits", which read as an apply link before this.
NOT_A_JOB = re.compile(r"benefit|culture|about|blog|privacy|values|life-at|/team\b|press|news", re.I)

SUFFIXES = re.compile(r"\b(inc|llc|ltd|gmbh|ab|bv|co|corp|corporation|limited|labs?|"
                      r"technologies|technology|software|group|hq)\b\.?", re.I)
STOP = {"remote", "the", "a", "an", "and", "of", "for", "to", "in", "at", "with", "m", "f", "d", "w"}
MATCH_AT = 0.6

LABEL = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby",
         "smartrecruiters": "SmartRecruiters", "recruitee": "Recruitee", "workable": "Workable"}
BOARD_URL = {
    "greenhouse": "https://job-boards.greenhouse.io/{}",
    "lever": "https://jobs.lever.co/{}",
    "ashby": "https://jobs.ashbyhq.com/{}",
    "smartrecruiters": "https://careers.smartrecruiters.com/{}",
    "recruitee": "https://{}.recruitee.com/",
    "workable": "https://apply.workable.com/{}/",
}

KIND_POSTING = "apply link in the posting"
KIND_CAREERS = "careers page named in the posting"
KIND_SEARCH = "web search"


def employer_link(description_html: str | None) -> tuple[str | None, str | None]:
    """(url, kind) for the employer's own link in a posting, or (None, None).

    An apply link or an ATS link to the opening wins. A link to the careers
    site in general comes second, and `resolve` still tries to swap it for the
    exact posting.
    """
    if not description_html:
        return None, None
    links = [html_mod.unescape(h) for h in re.findall(r'href="(https?://[^"]+)"', description_html)]
    links = [link for link in links if not SKIP_LINK.search(link) and not NOT_A_JOB.search(link)]
    for link in links:
        if ATS_HOST.search(link) or APPLY_LINK.search(link):
            return link, KIND_POSTING
    for link in links:
        if CAREERS_LINK.search(link):
            return link, KIND_CAREERS
    return None, None


def clean_title(title: str) -> str:
    """Drop the "[Job -26953]" reference codes some postings prefix titles with."""
    return re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]", " ", title or "")).strip()


def slugs(company: str) -> list[str]:
    """Board slugs to try, strongest guess first."""
    full = re.findall(r"[a-z0-9]+", company.lower())
    core = re.findall(r"[a-z0-9]+", SUFFIXES.sub(" ", company.lower())) or full
    guesses = ["".join(full), "".join(core), "-".join(core), core[0] if core else ""]
    return [s for s in dict.fromkeys(guesses) if s]


def _tokens(title: str) -> set[str]:
    text = clean_title(title).lower().replace(".net", " dotnet ").replace("c#", " csharp ")
    return set(re.findall(r"[a-z0-9]+", text)) - STOP


def similarity(a: str, b: str) -> float:
    left, right = _tokens(a), _tokens(b)
    return len(left & right) / len(left | right) if left | right else 0.0


def search_url(company: str, title: str) -> str:
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(
        f'"{company}" {clean_title(title)} careers')


# --- the applicant tracking systems -------------------------------------
# Each returns [(title, url), ...] for a board that exists and has jobs, and
# None or [] otherwise. Empty boards are treated as missing: SmartRecruiters
# answers any slug at all with an empty list.

def _get(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def _greenhouse(slug):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return [(j.get("title"), j.get("absolute_url")) for j in data.get("jobs") or []] if isinstance(data, dict) else None


def _lever(slug):
    data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return [(j.get("text"), j.get("hostedUrl")) for j in data] if isinstance(data, list) else None


def _ashby(slug):
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return [(j.get("title"), j.get("jobUrl")) for j in data.get("jobs") or []] if isinstance(data, dict) else None


def _smartrecruiters(slug):
    data = _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    if not isinstance(data, dict):
        return None
    return [(j.get("name"), f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}")
            for j in data.get("content") or []]


def _recruitee(slug):
    data = _get(f"https://{slug}.recruitee.com/api/offers/")
    return [(j.get("title"), j.get("careers_url")) for j in data.get("offers") or []] if isinstance(data, dict) else None


def _workable(slug):
    data = _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    return [(j.get("title"), j.get("url") or j.get("shortlink")) for j in data.get("jobs") or []] \
        if isinstance(data, dict) else None


# Workable last: it rate-limits hard, and the others answer most companies.
READERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby,
           "smartrecruiters": _smartrecruiters, "recruitee": _recruitee, "workable": _workable}


def find_board(company: str):
    """(ats, slug, [(title, url)]) for the company's board, or None."""
    for slug in slugs(company):
        for ats, read in READERS.items():
            found = read(slug)
            if found:
                return ats, slug, found
    return None


def choose(company: str, title: str, board) -> tuple[str, str]:
    """The best (url, kind) for one job, given its company's board or None."""
    if board:
        ats, slug, found = board
        best = max(found, key=lambda item: similarity(title, item[0] or ""))
        if best[1] and similarity(title, best[0] or "") >= MATCH_AT:
            return best[1], f"exact posting on {LABEL[ats]}"
        if slug in slugs(company)[:2]:
            return BOARD_URL[ats].format(slug), f"company jobs on {LABEL[ats]}"
    return search_url(company, title), KIND_SEARCH


def resolve(jobs: list) -> dict[str, int]:
    """Fill `career_url` / `career_kind` on every job that lacks a specific one.

    A job with only a general careers page from its posting is looked up too,
    and the page is replaced only by the exact posting - never by a guess.
    """
    todo = [job for job in jobs if not job.career_url or job.career_kind == KIND_CAREERS]
    companies = sorted({job.company for job in todo if job.company})
    boards = {}
    if companies:
        with ThreadPoolExecutor(max_workers=min(8, len(companies))) as pool:
            boards = dict(zip(companies, pool.map(find_board, companies)))
    counts: dict[str, int] = {}
    for job in todo:
        url, kind = choose(job.company, job.title, boards.get(job.company))
        if not job.career_url or kind.startswith("exact posting"):
            job.career_url, job.career_kind = url, kind
        key = job.career_kind.split(" on ")[0]
        counts[key] = counts.get(key, 0) + 1
    log.info("Employer pages for %d listing(s): %s", len(todo),
             ", ".join(f"{n} {k}" for k, n in counts.items()) or "none")
    return counts
