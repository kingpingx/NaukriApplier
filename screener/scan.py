"""The scan: collect listings, score them, write the results out.

    gather   ask the configured source for listings
    filter   drop what the ledger has already shown you
    score    rank what is left against your facts
    write    results.json, a ranked .xlsx, and a report you can read

Nothing here applies to anything. This tool finds and ranks jobs; the decision
to apply, and the application itself, stay with you. That is a deliberate line -
an unattended bot answering a recruiter's screening questions is inventing
answers in your name, and no ranking is worth that.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

from . import page as page_mod
from . import score as score_mod
from .config import ConfigError
from .ledger import Ledger
from .paths import JOBS_DIR
from .sources import get_source

log = logging.getLogger("screener.scan")


def run(config: dict, *, refresh: bool = False, limit: int | None = None) -> dict:
    """Collect, score and rank. Returns a summary dict; writes nothing yet."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    ledger = Ledger()

    source = get_source(config)
    log.info("Collecting listings via %s", source.name)
    jobs = source.gather(config)
    failed = list(getattr(source, "failed", []))

    # Supplementary boards are additive, not alternatives: `source:` decides how
    # the main board is reached, and `boards:` bolts others alongside it. A
    # failing board must not lose the results already in hand, so it is named
    # in `failed` and the run continues.
    from .sources import boards as boards_mod
    extra, failed_boards = boards_mod.gather(config)
    if extra:
        log.info("Adding %d listing(s) from %s", len(extra),
                 ", ".join(sorted({j.source for j in extra})))
    jobs = dedupe(list(jobs) + extra)
    failed += [f"board:{name}" for name in failed_boards]

    if not jobs:
        return {
            "at": datetime.now().isoformat(timespec="seconds"),
            "source": source.name,
            "collected": 0, "seen_before": 0, "rejected": 0,
            "shortlist": [], "review": [], "dropped": [],
            "searches": config.get("searches") or [],
            "failed_searches": failed,
        }

    seen_before = 0
    scored, rejected = [], []
    for job in jobs:
        # `refresh` re-scores everything, which is what you want after editing
        # your config - otherwise yesterday's ledger hides the jobs whose score
        # just changed.
        if not refresh and ledger.seen(job.job_id):
            seen_before += 1
            continue
        breakdown = score_mod.score(job, config)
        (rejected if "rejected" in breakdown else scored).append(job)

    scored.sort(key=lambda j: j.score, reverse=True)
    if limit:
        scored = scored[:limit]

    strong_at = float(config.get("auto_apply_min_score", 72))
    review_at = float(config.get("review_min_score", 55))

    shortlist = [j for j in scored if j.score >= strong_at]
    review = [j for j in scored if review_at <= j.score < strong_at]
    dropped = [j for j in scored if j.score < review_at]

    target = int(config.get("daily_target", 50))
    shortlist = shortlist[:target]
    review = review[:max(0, target - len(shortlist))]

    # Listings from boards whose "Apply" goes through the board also get the
    # employer's own page. Only for jobs that made a band: finding a company's
    # board costs a few requests per company.
    from . import careers
    todo = [j for j in shortlist + review if j.source in careers.BOARDS]
    if todo:
        try:
            careers.resolve(todo)
        except Exception as exc:          # a lookup failure must not lose the scan
            log.warning("Employer page lookup skipped: %s", exc)

    for job in shortlist + review:
        ledger.record(job, "shortlisted" if job.score >= strong_at else "review",
                      score_mod.explain(job))
    for job in rejected:
        ledger.record(job, "dropped", score_mod.explain(job))
    ledger.save()

    return {
        "at": datetime.now().isoformat(timespec="seconds"),
        "source": source.name,
        "collected": len(jobs),
        "seen_before": seen_before,
        "rejected": len(rejected),
        "shortlist": [j.to_dict() | {"score": j.score, "why": score_mod.explain(j)} for j in shortlist],
        "review": [j.to_dict() | {"score": j.score, "why": score_mod.explain(j)} for j in review],
        "dropped": [j.to_dict() | {"score": j.score, "why": score_mod.explain(j)} for j in dropped[:40]],
        "searches": config.get("searches") or [],
        "failed_searches": failed,
        "_jobs": shortlist + review,   # live objects, stripped before serialising
    }


def _same_job_key(job) -> tuple[str, str]:
    def norm(text):
        return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    return norm(job.company), norm(job.title)


def dedupe(jobs: list) -> list:
    """Drop the same opening seen on a second board, keeping the first.

    Remote companies post one role to three boards at once; without this the
    shortlist fills with one job three times. Order is preserved, so the main
    source wins, then boards in the order `boards:` lists them.

    Only across boards, never within one. Two Naukri postings with the same
    title at the same company are usually different openings - another city,
    another experience band - and merging them dropped 42 of 301 Naukri jobs
    on the first scan after this was added.
    """
    from .sources.boards import READERS
    kept, first_board = [], {}
    for job in jobs:
        key = _same_job_key(job)
        board = job.source if job.source in READERS else "naukri"
        if all(key) and first_board.setdefault(key, board) != board:
            continue
        kept.append(job)
    return kept


def write(summary: dict, config: dict, *, excel: bool = True, html: bool = True) -> dict[str, Path]:
    """Persist the scan. Returns {kind: path} for whatever was written.

    Every file carries the run slot - `results-<day>-r2.json` - so a second scan
    the same day, which lists only jobs the first had not shown you, adds its own
    files instead of overwriting the morning's.
    """
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    run = page_mod.next_run(today)
    tag = f"{today}-r{run}"
    written: dict[str, Path] = {}

    jobs = summary.pop("_jobs", [])

    results_path = JOBS_DIR / f"results-{tag}.json"
    results_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str),
                            encoding="utf-8")
    written["results"] = results_path

    if excel and jobs:
        try:
            from .export import to_excel
            path = JOBS_DIR / f"job-matches-{tag}.xlsx"
            to_excel(jobs, path, Ledger())
            written["excel"] = path
        except ImportError:
            log.info("openpyxl not installed - skipping the spreadsheet. pip install openpyxl")
        except Exception as exc:
            log.warning("Could not write the spreadsheet: %s", exc)

    if html and jobs:
        try:
            path = page_mod.build({"shortlist": summary["shortlist"],
                                   "review": summary["review"]}, today=today, run=run)
            written["html"] = Path(path)
        except Exception as exc:
            log.warning("Could not write the HTML page: %s", exc)

    report_path = JOBS_DIR / f"report-{tag}.md"
    report_path.write_text(report(summary, config), encoding="utf-8")
    written["report"] = report_path

    summary["_jobs"] = jobs
    return written


def report(summary: dict, config: dict) -> str:
    """A markdown report: what was searched, what scored, and why."""
    lines = [
        f"# Job scan - {summary.get('at', '')[:16].replace('T', ' ')}",
        "",
        f"- source: `{summary.get('source')}`",
        f"- collected: {summary.get('collected')}",
        f"- already in the ledger: {summary.get('seen_before')}",
        f"- hard-rejected: {summary.get('rejected')}",
        f"- shortlist: {len(summary.get('shortlist') or [])}",
        f"- review: {len(summary.get('review') or [])}",
        "",
        "## Searches run",
        "",
    ]
    for entry in summary.get("searches") or []:
        lines.append(f"- `{entry.get('keyword')}` in {entry.get('location') or 'all India'}")

    for heading, key in (("Shortlist", "shortlist"), ("Worth a read", "review")):
        rows = summary.get(key) or []
        if not rows:
            continue
        lines += ["", f"## {heading}", ""]
        for job in rows:
            lines.append(f"### {job.get('score')}  {job.get('title')} - {job.get('company')}")
            location = job.get("location") or "-"
            experience = job.get("experience_label") or "-"
            salary = job.get("salary_label") or "not stated"
            lines.append(f"{location} | {experience} | {salary} | {job.get('posted_label') or ''}")
            if job.get("company_apply"):
                lines.append("Applies on the company's own site.")
            if job.get("has_questionnaire"):
                lines.append("Opens a screening questionnaire.")
            lines.append(f"`{job.get('why')}`")
            lines.append(f"<{job.get('url')}>")
            lines.append("")

    dropped = summary.get("dropped") or []
    if dropped:
        lines += ["", "## Scored too low", ""]
        for job in dropped[:25]:
            lines.append(f"- {job.get('score')} {job.get('title')} - {job.get('company')} | `{job.get('why')}`")

    return "\n".join(lines) + "\n"


def summarise(summary: dict, written: dict[str, Path] | None = None) -> str:
    shortlist = summary.get("shortlist") or []
    review = summary.get("review") or []
    lines = [
        "",
        f"  Collected {summary.get('collected')} listings via {summary.get('source')}.",
        f"    {summary.get('seen_before')} already seen, {summary.get('rejected')} rejected outright.",
        "",
        f"  Shortlist:    {len(shortlist)}",
        f"  Worth a read: {len(review)}",
        "",
    ]
    failed = summary.get("failed_searches") or []
    if failed:
        lines += [f"  {len(failed)} search(es) failed: {', '.join(failed)}",
                  "    See logs/screener.log - this run saw fewer jobs than it should.", ""]
    for job in shortlist[:10]:
        lines.append(f"    {job.get('score'):>5}  {(job.get('title') or '')[:44]:<44}  {(job.get('company') or '')[:26]}")
    if len(shortlist) > 10:
        lines.append(f"           ... and {len(shortlist) - 10} more")
    if not shortlist and not review:
        lines.append("    Nothing cleared the thresholds. Lower `review_min_score` in")
        lines.append("    config.yaml, or widen `searches:`.")

    if written:
        lines.append("")
        for kind, path in written.items():
            lines.append(f"  {kind:<8} {path}")
    lines.append("")
    return "\n".join(lines)


def headline(summary: dict, written: dict[str, Path] | None = None) -> tuple[str, str]:
    """Title and body for the desktop notification a finished scan sends."""
    shortlist = summary.get("shortlist") or []
    review = summary.get("review") or []
    if not shortlist and not review:
        title = "Naukri scan: no new matches"
    else:
        title = f"Naukri scan: {len(shortlist)} shortlisted, {len(review)} worth a read"
    lines = [f"{job.get('score') or 0:g}  {job.get('title')} - {job.get('company')}"
             for job in (shortlist + review)[:3]]
    failed = summary.get("failed_searches") or []
    if failed:
        title += f" - {len(failed)} search{'es' if len(failed) != 1 else ''} failed"
        lines.append("Failed: " + ", ".join(failed))
    page = (written or {}).get("html")
    if page:
        lines.append(str(page))
    return title, "\n".join(lines)
