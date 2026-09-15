"""Build the dated HTML tracker page from a scan's results.

One page per day, listing every opening the scan found, with the ones seen for
the first time today marked NEW. Ticking a row records that you applied; the
ticks live in localStorage under a single shared key, so they carry across
every day's page rather than resetting each morning.

Uniqueness comes from data/jobs/seen.json, which records the first date each
job id appeared. Without it, "today's openings" would be the same list of
long-standing postings every day and the page would be useless by Thursday.
"""
from __future__ import annotations

import html
import json
import re
import logging
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("screener.page")

from .paths import JOBS_DIR, SEEN_PATH
from .sources.boards import LABELS as BOARD_LABELS


def load_seen() -> dict:
    if not SEEN_PATH.exists():
        return {}
    try:
        data = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("seen.json unreadable (%s); starting fresh", exc)
        return {}


def save_seen(seen: dict) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = SEEN_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(seen, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(SEEN_PATH)


_AGE_UNITS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}


def age_days(text: str | None) -> int | None:
    """Turn a posted label into a number of days, or None if it has no date.

    Handles both boards' wording - Naukri's "1 Day Ago" / "Just Now" /
    "30+ Days Ago" and LinkedIn's "3 days ago" / "2 weeks ago". Returns None
    rather than a guess when there is no date to read: LinkedIn's promoted
    cards show "Promoted" where the date would be, and treating those as
    brand-new would put paid placements at the top of a freshness filter.
    """
    if not text:
        return None
    low = str(text).strip().lower()
    if "just now" in low or "today" in low:
        return 0
    # Naukri's "Few Hours Ago" has no digit for the regex below to find, so it
    # read as undated and the Posted filters hid the freshest rows on the page.
    if "hour" in low or "minute" in low:
        return 0
    if "yesterday" in low:
        return 1
    match = re.search(r"(\d+)\s*\+?\s*(minute|hour|day|week|month|year)", low)
    if not match:
        return None
    return int(match.group(1)) * _AGE_UNITS[match.group(2)]


def linkedin_posted(card: dict) -> str | None:
    """The posted-time entry in a LinkedIn card footer, if it has one."""
    for item in card.get("footer") or []:
        if age_days(item) is not None:
            return item
    return None


def _salary_of(card: dict) -> str:
    for item in card.get("metadata") or []:
        if any(mark in item for mark in ("/yr", "/hr", "₹", "$", "LPA", "INR")):
            return item
    return ""


def created_age_days(created_ms, day: str) -> int | None:
    """Whole days between Naukri's createdDate and the day the scan ran.

    The posted label is a rounding of this timestamp and loses to it: "30+ Days
    Ago" can be a year old. Measured against the scan's day, not the wall clock,
    so rebuilding a stored day gives back that day's ages.
    """
    if not created_ms:
        return None
    try:
        posted = datetime.fromtimestamp(created_ms / 1000).date()
        return (date.fromisoformat(day) - posted).days
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def build_rows(results: dict, seen: dict, today: str) -> list[dict]:
    """Flatten both boards into one row list, stamping first-seen dates.

    Accepts either shape of results dict, which is not cosmetic: `scan.write`
    calls this with {"shortlist", "review"} while this function only ever read
    {"naukri", "linkedin"}. The keys never matched, `build_rows` returned an
    empty list every single run, and the page was written with zero rows in it
    - silently, because an empty page is still a valid page. The scan log said
    "Wrote 0 row(s)" and nothing treated that as an error.
    """
    rows = []

    naukri = list(results.get("naukri") or [])
    if not naukri:
        # Deduped on job_id: a job can legitimately appear in only one band,
        # but a caller merging bands itself should not produce doubles here.
        merged, seen_ids = [], set()
        for band in ("shortlist", "review"):
            for job in results.get(band) or []:
                key = job.get("job_id")
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                merged.append(job)
        naukri = merged

    for job in naukri:
        # Board ids already carry their own prefix, and double-prefixing
        # them would mint a new seen.json key for a job already tracked - so
        # every one of them would read as NEW on every run, forever.
        raw = str(job.get("job_id") or "")
        job_id = raw if ":" in raw else f"naukri:{raw}"
        first = seen.setdefault(job_id, today)
        location = job.get("location") or ""
        age = created_age_days(job.get("created_ms"), today)
        rows.append({
            "id": job_id,
            "board": BOARD_LABELS.get(job.get("source") or "", "Naukri"),
            "title": job.get("title") or "",
            "company": job.get("company") or "",
            "location": location,
            "salary": job.get("salary_label") or "",
            "experience": job.get("experience_label") or "",
            "posted": job.get("posted_label") or "",
            "age": age if age is not None else age_days(job.get("posted_label")),
            "rank": job.get("score"),
            "rank_label": "score",
            "url": job.get("url") or "",
            "remote": "remote" in location.lower(),
            "easy": not job.get("company_apply") and not job.get("has_questionnaire"),
            "note": ", ".join((job.get("matched_skills") or [])[:6]),
            "new": first == today,
            "first_seen": first,
        })

    for card in results.get("linkedin") or []:
        job_id = f"linkedin:{card.get('job_id')}"
        first = seen.setdefault(job_id, today)
        location = card.get("location") or ""
        rows.append({
            "id": job_id,
            "board": "LinkedIn",
            "title": card.get("title") or "",
            "company": card.get("company") or "",
            "location": location,
            "salary": _salary_of(card),
            "experience": "",
            "posted": linkedin_posted(card) or "",
            "age": age_days(linkedin_posted(card)),
            "rank": card.get("_match"),
            "rank_label": "title match",
            "url": card.get("url") or "",
            "remote": bool(card.get("_remote")) or "remote" in location.lower(),
            "easy": bool(card.get("easy_apply")),
            "note": card.get("insight") or "",
            "new": first == today,
            "first_seen": first,
        })

    # Remote first, then newest, then best-ranked.
    rows.sort(key=lambda r: (not r["remote"], not r["new"], -(r["rank"] or 0)))
    return rows


TEMPLATE = """<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=Source+Sans+3:wght@400;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
  :root {
    --bg: #f2f4f7;
    --surface: #ffffff;
    --surface-2: #fafbfc;
    --line: #dde2e9;
    --ink: #161b22;
    --muted: #5b6675;
    --accent: #1f5c8b;
    --accent-soft: #e8f0f7;
    --accent-on: #ffffff;
    --new: #a15c07;
    --new-soft: #fdf3e3;
    --good: #166534;
    --good-soft: #e6f4ea;
    --shadow: 0 1px 2px rgba(22, 27, 34, .06), 0 4px 12px rgba(22, 27, 34, .04);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #0e1218;
      --surface: #161c25;
      --surface-2: #1b222c;
      --line: #2a3441;
      --ink: #e7ecf3;
      --muted: #8d99a9;
      --accent: #6aa9d8;
      --accent-soft: #17293a;
      --accent-on: #0e1218;
      --new: #e0a54a;
      --new-soft: #2e2413;
      --good: #6ec48c;
      --good-soft: #14291c;
      --shadow: 0 1px 2px rgba(0, 0, 0, .4), 0 4px 14px rgba(0, 0, 0, .3);
    }
  }
  :root[data-theme="dark"] {
    --bg: #0e1218;
    --surface: #161c25;
    --surface-2: #1b222c;
    --line: #2a3441;
    --ink: #e7ecf3;
    --muted: #8d99a9;
    --accent: #6aa9d8;
    --accent-soft: #17293a;
    --accent-on: #0e1218;
    --new: #e0a54a;
    --new-soft: #2e2413;
    --good: #6ec48c;
    --good-soft: #14291c;
    --shadow: 0 1px 2px rgba(0, 0, 0, .4), 0 4px 14px rgba(0, 0, 0, .3);
  }

  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: "Source Sans 3", ui-sans-serif, system-ui, -apple-system, sans-serif;
    font-size: 15px;
    line-height: 1.5;
  }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 32px 20px 80px; }

  header { display: flex; flex-direction: column; gap: 6px; margin-bottom: 24px; }
  .eyebrow {
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: 12px; letter-spacing: .09em; text-transform: uppercase; color: var(--muted);
  }
  h1 {
    font-family: Archivo, ui-sans-serif, system-ui, sans-serif;
    font-weight: 700; font-size: clamp(26px, 4vw, 36px);
    margin: 0; letter-spacing: -.02em; text-wrap: balance;
  }
  .sub { color: var(--muted); max-width: 62ch; }
  /* Each scan of the day keeps its own page; this is how the evening page gets
     back to the morning one. */
  nav.runs {
    display: flex; flex-wrap: wrap; gap: 6px; align-items: baseline;
    margin: 0 0 16px; font: 400 12.5px/1.6 "Source Sans 3", sans-serif;
  }
  nav.runs .label { color: var(--muted); margin-right: 2px; }
  nav.runs a, nav.runs span.here {
    padding: 3px 9px; border-radius: 999px; text-decoration: none;
    border: 1px solid var(--line); color: var(--muted);
    font: 500 12.5px/1.6 "IBM Plex Mono", monospace;
  }
  nav.runs a:hover { color: var(--ink); background: var(--surface-2); }
  nav.runs span.here { color: var(--ink); border-color: var(--accent); background: var(--surface-2); }
  nav.runs .sep { color: var(--line); padding: 0 4px; }

  .stats { display: flex; flex-wrap: wrap; gap: 10px; margin: 22px 0 18px; }
  .stat {
    background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
    padding: 12px 16px; min-width: 116px; box-shadow: var(--shadow);
  }
  .stat b {
    display: block; font-family: "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums;
    font-size: 24px; font-weight: 500; line-height: 1.15;
  }
  .stat span {
    font-size: 11px; letter-spacing: .07em; text-transform: uppercase; color: var(--muted);
    font-family: "IBM Plex Mono", monospace;
  }
  .stat.is-applied b { color: var(--good); }
  .stat.is-new b { color: var(--new); }

  .bar {
    position: sticky; top: 0; z-index: 5;
    display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
    background: var(--bg); padding: 12px 0; border-bottom: 1px solid var(--line); margin-bottom: 4px;
  }
  .chip {
    font: 500 13px/1 Archivo, sans-serif;
    border: 1px solid var(--line); background: var(--surface); color: var(--ink);
    padding: 8px 13px; border-radius: 999px; cursor: pointer;
  }
  .chip[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: var(--accent-on); }
  .chip:focus-visible, .row a:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .spacer { flex: 1 1 auto; }
  .bar-sep { width: 1px; align-self: stretch; background: var(--line); margin: 0 3px; }
  .bar-label {
    font-family: "IBM Plex Mono", monospace; font-size: 11px; letter-spacing: .08em;
    text-transform: uppercase; color: var(--muted); padding-right: 2px;
  }
  .search {
    font: 400 14px "Source Sans 3", sans-serif; color: var(--ink);
    background: var(--surface); border: 1px solid var(--line);
    border-radius: 8px; padding: 8px 12px; min-width: 200px;
  }

  .group-head {
    display: flex; align-items: baseline; gap: 10px;
    margin: 26px 0 10px; padding-bottom: 6px; border-bottom: 2px solid var(--ink);
  }
  .group-head h2 {
    font-family: Archivo, sans-serif; font-size: 15px; font-weight: 700;
    letter-spacing: .06em; text-transform: uppercase; margin: 0;
  }
  .group-head .count { font-family: "IBM Plex Mono", monospace; font-size: 13px; color: var(--muted); }

  .row {
    display: grid;
    grid-template-columns: 34px 1fr 172px 120px 92px;
    gap: 14px; align-items: start;
    background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
    padding: 13px 15px; margin-bottom: 7px; box-shadow: var(--shadow);
  }
  .row.done { background: var(--good-soft); border-color: color-mix(in srgb, var(--good) 30%, var(--line)); }
  .row.done .title a { text-decoration: line-through; opacity: .62; }
  .row input[type="checkbox"] { width: 19px; height: 19px; margin-top: 2px; accent-color: var(--good); cursor: pointer; }

  .title { font-family: Archivo, sans-serif; font-weight: 600; font-size: 15.5px; line-height: 1.3; }
  .title a { color: var(--ink); text-decoration: none; }
  .title a:hover { color: var(--accent); text-decoration: underline; }
  .company { color: var(--muted); font-size: 13.5px; margin-top: 2px; }
  .tags { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
  .tag {
    font-family: "IBM Plex Mono", monospace; font-size: 10.5px; letter-spacing: .05em;
    text-transform: uppercase; padding: 2px 7px; border-radius: 4px;
    border: 1px solid var(--line); color: var(--muted); background: var(--surface-2);
  }
  .tag.new { color: var(--new); background: var(--new-soft); border-color: color-mix(in srgb, var(--new) 32%, var(--line)); }
  .tag.remote { color: var(--accent); background: var(--accent-soft); border-color: color-mix(in srgb, var(--accent) 32%, var(--line)); }
  .tag.easy { color: var(--good); background: var(--good-soft); border-color: color-mix(in srgb, var(--good) 32%, var(--line)); }
  .note { color: var(--muted); font-size: 12.5px; margin-top: 6px; }

  .meta { font-size: 13px; color: var(--muted); }
  .meta div + div { margin-top: 3px; }
  .rank {
    font-family: "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums;
    font-size: 17px; text-align: right; color: var(--accent);
  }
  .rank small { display: block; font-size: 9.5px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }

  /* Shown whenever a filter is hiding rows. Without it the tiles report one
     number and the list shows another, and a stale chip looks like a bug in
     the scan rather than a filter left switched on. */
  #filter-note {
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    margin: 0 0 14px; padding: 10px 14px; font-size: 13.5px;
    color: var(--new); background: var(--new-soft);
    border: 1px solid color-mix(in srgb, var(--new) 32%, var(--line));
    border-radius: 10px;
  }
  #filter-note[hidden] { display: none; }

  .empty { color: var(--muted); padding: 26px 0; text-align: center; }
  footer { margin-top: 40px; padding-top: 18px; border-top: 1px solid var(--line); color: var(--muted); font-size: 13px; }
  footer code { font-family: "IBM Plex Mono", monospace; font-size: 12px; }

  @media (max-width: 760px) {
    .row { grid-template-columns: 30px 1fr; }
    .meta, .rank { grid-column: 2; text-align: left; }
    .rank { font-size: 15px; }
  }
  @media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
</style>

<div class="wrap">
  <nav class="runs">__RUNS__</nav>

  <header>
    <div class="eyebrow">__DATELINE__ &middot; __SCOPE__</div>
    <h1>__HEADING__</h1>
    <p class="sub">Every opening this scan found, remote first. Tick a row when you have applied &mdash; ticks are saved in this browser and carry across every day&rsquo;s page.</p>
  </header>

  <div class="stats">
    <div class="stat"><b id="s-total">0</b><span>Openings</span></div>
    <div class="stat is-new"><b id="s-new">0</b><span>New today</span></div>
    <div class="stat"><b id="s-remote">0</b><span>Remote</span></div>
    <div class="stat is-applied"><b id="s-applied">0</b><span>Applied</span></div>
    <div class="stat"><b id="s-left">0</b><span>Still to do</span></div>
  </div>

  <div class="bar">
    <button class="chip" data-filter="all" aria-pressed="true">All</button>
    <button class="chip" data-filter="new" aria-pressed="false">New today</button>
    <button class="chip" data-filter="remote" aria-pressed="false">Remote</button>
    <button class="chip" data-filter="easy" aria-pressed="false">One-click apply</button>
    <button class="chip" data-filter="todo" aria-pressed="false">Not applied</button>
    <span class="bar-sep" aria-hidden="true"></span>
    <span class="bar-label">Posted</span>
    <button class="chip" data-posted="any" aria-pressed="true">Any</button>
    <button class="chip" data-posted="1" aria-pressed="false">1 day</button>
    <button class="chip" data-posted="2" aria-pressed="false">2 days</button>
    <button class="chip" data-posted="7" aria-pressed="false">7 days</button>
    <span class="spacer"></span>
    <input class="search" id="q" type="search" placeholder="Filter by title or company" aria-label="Filter by title or company">
  </div>

  <div id="filter-note" hidden>
    <span id="fn-text"></span>
    <button class="chip" id="clear-filters">Show all</button>
  </div>

  <div id="list"></div>

  <footer>
    <p>Generated __GENERATED__ from __NAUKRI_N__ Naukri and __LINKEDIN_N__ LinkedIn listings.
    Naukri scores are 0&ndash;100 across skills, title, experience, location and freshness.
    LinkedIn shows title match only &mdash; its cards carry no skills or experience range, so the two are not the same measure.</p>
    <p>Ticks are stored in this browser under <code>jobtracker.applied</code>. Clearing site data clears them.</p>
    <p>__UNDATED_NOTE__</p>
  </footer>
</div>

<script id="data" type="application/json">__DATA__</script>
<script>
  const ROWS = JSON.parse(document.getElementById('data').textContent);
  const KEY = 'jobtracker.applied';
  const TODAY = '__TODAY__';

  const load = () => { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { return {}; } };
  const save = (v) => { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) {} };
  let applied = load();
  let filter = 'all';
  let postedMax = null;   // null = any age
  let query = '';

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  function visible(r) {
    if (filter === 'new' && !r.new) return false;
    if (filter === 'remote' && !r.remote) return false;
    if (filter === 'easy' && !r.easy) return false;
    if (filter === 'todo' && applied[r.id]) return false;
    // Posted is a separate axis, so "remote" and "2 days" combine rather than
    // replacing each other. A row with no readable date is excluded whenever a
    // date filter is on - it cannot be shown to satisfy it.
    if (postedMax !== null && (r.age === null || r.age > postedMax)) return false;
    if (query) {
      const hay = (r.title + ' ' + r.company + ' ' + r.location).toLowerCase();
      if (!hay.includes(query)) return false;
    }
    return true;
  }

  function rowHtml(r) {
    const done = !!applied[r.id];
    const tags = [
      r.new ? '<span class="tag new">New</span>' : '',
      r.remote ? '<span class="tag remote">Remote</span>' : '',
      r.easy ? '<span class="tag easy">One-click</span>' : '',
      '<span class="tag">' + esc(r.board) + '</span>',
      (!r.new && r.first_seen) ? '<span class="tag">seen ' + esc(r.first_seen) + '</span>' : '',
    ].join('');
    return '' +
      '<div class="row' + (done ? ' done' : '') + '" data-id="' + esc(r.id) + '">' +
        '<input type="checkbox" ' + (done ? 'checked' : '') +
          ' aria-label="Mark ' + esc(r.title) + ' as applied">' +
        '<div>' +
          '<div class="title"><a href="' + esc(r.url) + '" target="_blank" rel="noopener">' + esc(r.title) + '</a></div>' +
          '<div class="company">' + esc(r.company) + '</div>' +
          '<div class="tags">' + tags + '</div>' +
          (r.note ? '<div class="note">' + esc(r.note) + '</div>' : '') +
        '</div>' +
        '<div class="meta"><div>' + (r.location ? esc(r.location) : '&mdash;') + '</div>' +
          (r.salary ? '<div>' + esc(r.salary) + '</div>' : '') + '</div>' +
        '<div class="meta">' + (r.experience ? '<div>' + esc(r.experience) + '</div>' : '') +
          (r.posted ? '<div>' + esc(r.posted) + '</div>' : '') + '</div>' +
        '<div class="rank">' + (r.rank == null ? '&mdash;' : esc(r.rank)) +
          '<small>' + esc(r.rank_label) + '</small></div>' +
      '</div>';
  }

  function render() {
    const list = document.getElementById('list');
    const shown = ROWS.filter(visible);
    // Naukri first, LinkedIn last, every other board in the order it appears.
    const boards = ['Naukri'];
    ROWS.forEach(r => { if (!boards.includes(r.board) && r.board !== 'LinkedIn') boards.push(r.board); });
    boards.push('LinkedIn');
    let html = '';
    boards.forEach(board => {
      const group = shown.filter(r => r.board === board);
      if (!group.length) return;
      html += '<div class="group-head"><h2>' + board + '</h2>' +
              '<span class="count">' + group.length + ' shown</span></div>';
      html += group.map(rowHtml).join('');
    });
    list.innerHTML = html || '<p class="empty">Nothing matches this filter.</p>';
    stats();
  }

  function stats() {
    const n = (id, v) => document.getElementById(id).textContent = v;
    // Count what is on screen, not what was collected. These used to report
    // ROWS.length unconditionally, so with a filter on the page said "80
    // openings, 80 new today" above a list of 24 - which reads as a broken
    // scan rather than an active filter.
    const shown = ROWS.filter(visible);
    const doneCount = ROWS.filter(r => applied[r.id]).length;
    n('s-total', shown.length);
    n('s-new', shown.filter(r => r.new).length);
    n('s-remote', shown.filter(r => r.remote).length);
    n('s-applied', doneCount);
    n('s-left', ROWS.length - doneCount);

    const hidden = ROWS.length - shown.length;
    const note = document.getElementById('filter-note');
    note.hidden = hidden === 0;
    if (hidden) {
      document.getElementById('fn-text').textContent =
        'Showing ' + shown.length + ' of ' + ROWS.length +
        ' — a filter is hiding ' + hidden + '.';
    }
  }

  document.getElementById('list').addEventListener('change', (e) => {
    if (e.target.type !== 'checkbox') return;
    const row = e.target.closest('.row');
    const id = row.getAttribute('data-id');
    if (e.target.checked) applied[id] = { date: TODAY };
    else delete applied[id];
    save(applied);
    row.classList.toggle('done', e.target.checked);
    if (filter === 'todo') render(); else stats();
  });

  function wire(selector, apply) {
    document.querySelectorAll(selector).forEach(chip => {
      chip.addEventListener('click', () => {
        apply(chip);
        document.querySelectorAll(selector).forEach(c =>
          c.setAttribute('aria-pressed', String(c === chip)));
        render();
      });
    });
  }
  wire('.chip[data-filter]', chip => { filter = chip.dataset.filter; });
  wire('.chip[data-posted]', chip => {
    postedMax = chip.dataset.posted === 'any' ? null : Number(chip.dataset.posted);
  });

  document.getElementById('clear-filters').addEventListener('click', () => {
    filter = 'all';
    postedMax = null;
    query = '';
    document.getElementById('q').value = '';
    document.querySelectorAll('.chip[data-filter]').forEach(c =>
      c.setAttribute('aria-pressed', String(c.dataset.filter === 'all')));
    document.querySelectorAll('.chip[data-posted]').forEach(c =>
      c.setAttribute('aria-pressed', String(c.dataset.posted === 'any')));
    render();
  });

  document.getElementById('q').addEventListener('input', (e) => {
    query = e.target.value.trim().toLowerCase();
    render();
  });

  render();
</script>
"""


def day_runs(day: str) -> list[Path]:
    """Every tracker page written for `day`, oldest run first.

    Includes the un-suffixed `openings-<day>.html` written before run slots
    existed, so those days stay reachable from the picker.
    """
    runs = sorted(JOBS_DIR.glob(f"openings-{day}-r*.html"),
                  key=lambda p: int(p.stem.rsplit("-r", 1)[1]))
    legacy = JOBS_DIR / f"openings-{day}.html"
    return ([legacy] if legacy.exists() else []) + runs


def next_run(day: str) -> int:
    """The run slot this scan should claim.

    Every scan used to write `openings-<day>.html`, so an evening scan destroyed
    the morning's page. Counted over every file a run writes, not only pages,
    so a --no-html scan still takes a slot of its own.
    """
    used = [int(slot) for p in JOBS_DIR.glob(f"*-{day}-r*")
            if (slot := p.stem.rsplit("-r", 1)[1]).isdigit()]
    return max(used) + 1 if used else 1


def _run_label(path: Path) -> str:
    stem = path.stem
    return f"r{stem.rsplit('-r', 1)[1]}" if "-r" in stem else "r0"


def runs_nav(day: str, this_name: str, extra: str | None = None) -> str:
    """The picker: every scan of `day`, then the four days before it.

    `extra` names a page about to be written and so not on disk yet - without
    it the run doing the writing would leave itself out of its own picker.
    """
    names = [p.name for p in day_runs(day)]
    if extra and extra not in names:
        names.append(extra)
    names.sort(key=lambda n: int(n.rsplit("-r", 1)[1].split(".")[0]) if "-r" in n else 0)

    parts = ['<span class="label">Today&rsquo;s scans</span>']
    for name in names:
        label = _run_label(Path(name))
        parts.append(f'<span class="here">{html.escape(label)}</span>' if name == this_name
                     else f'<a href="{html.escape(name)}">{html.escape(label)}</a>')

    earlier = sorted({p.name.replace("openings-", "").split("-r")[0].replace(".html", "")
                      for p in JOBS_DIR.glob("openings-*.html")} - {day}, reverse=True)[:4]
    if earlier:
        parts.append('<span class="sep">|</span>')
        for past in earlier:
            pages = day_runs(past)
            if pages:
                parts.append(f'<a href="{html.escape(pages[-1].name)}">{html.escape(past[5:])}</a>')
    return "\n    ".join(parts)


_RUNS_NAV_RE = re.compile(r'(<nav class="runs">)(.*?)(</nav>)', re.S)


def refresh_run_navs(day: str) -> int:
    """Re-point every earlier page of `day` at the runs that now exist.

    A page written in the morning cannot link to the evening run, which did not
    exist yet - so each run rewrites the picker of the pages before it. Only the
    nav block is touched; the rows, counts and tick state are that run's record.
    """
    patched = 0
    for path in day_runs(day):
        text = path.read_text(encoding="utf-8")
        replacement = runs_nav(day, path.name)
        updated, count = _RUNS_NAV_RE.subn(
            lambda m: m.group(1) + "\n    " + replacement + "\n  " + m.group(3), text)
        if count and updated != text:
            path.write_text(updated, encoding="utf-8")
            patched += 1
    return patched


def build(results: dict, out_path: Path | None = None, today: str | None = None,
          run: int | None = None) -> Path:
    """Write this run's tracker page and re-point the day's earlier pages at it.

    Without `out_path` the page claims a run slot, `openings-<day>-r<N>.html`,
    so a second scan the same day adds a page instead of overwriting the first.
    An explicit `out_path` is a scratch rebuild: no slot, no other page touched.
    """
    today = today or date.today().isoformat()
    seen = load_seen()
    rows = build_rows(results, seen, today)
    save_seen(seen)

    scope = "Worldwide, remote first" if results.get("worldwide") else \
        ", ".join(results.get("locations") or []) or "All locations"
    # Say so when the scan itself was filtered, or the page reads as a thin day
    # rather than a deliberately narrow one. Escape the free text first, then
    # join with entities - escaping afterwards would print "&amp;middot;".
    parts = [html.escape(scope)]
    window = results.get("posted_days")
    if window:
        parts.append("posted in the last "
                     + ("24 hours" if window == 1 else f"{window:g} days"))
    if results.get("new_only"):
        parts.append("first seen today")
    scope_html = " &middot; ".join(parts)
    new_count = sum(1 for r in rows if r["new"])
    undated = sum(1 for r in rows if r["age"] is None)
    undated_note = (
        f"{undated} listing(s) carry no posted date - LinkedIn shows &ldquo;Promoted&rdquo; "
        "where the date would be. The Posted filters hide those rather than guessing at their age."
        if undated else "Every listing carries a posted date."
    )

    is_real_run = out_path is None
    this_run = next_run(today) if run is None else run
    this_name = f"openings-{today}-r{this_run}.html" if is_real_run else out_path.name
    runs_html = runs_nav(today, this_name, extra=this_name if is_real_run else None)

    page = (TEMPLATE
            .replace("__RUNS__", runs_html)
            .replace("__TITLE__", f"Job Openings {today} r{this_run}")
            .replace("__HEADING__", f"{len(rows)} openings, {new_count} new today")
            .replace("__DATELINE__", f"{today} &middot; scan {this_run}")
            .replace("__SCOPE__", scope_html)
            .replace("__GENERATED__", today)
            .replace("__NAUKRI_N__", str(len(results.get("naukri") or [])))
            .replace("__LINKEDIN_N__", str(len(results.get("linkedin") or [])))
            .replace("__TODAY__", today)
            .replace("__UNDATED_NOTE__", undated_note)
            # json.dumps leaves "<" alone, and "</script" is the one sequence
            # that ends the data block early - a pasted JD must not break the page.
            .replace("__DATA__", json.dumps(rows, ensure_ascii=False).replace("<", "\\u003c")))

    out_path = out_path or (JOBS_DIR / this_name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    log.info("Wrote %d row(s) (%d new) to %s", len(rows), new_count, out_path)
    # Only a real run re-points other pages; a scratch rebuild must not reach
    # into data/jobs and rewrite pages it is not part of.
    if is_real_run:
        refresh_run_navs(today)
    return out_path


def build_from_file(results_path: Path, out_path: Path | None = None) -> Path:
    results = json.loads(Path(results_path).read_text(encoding="utf-8"))
    return build(results, out_path)
