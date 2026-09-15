"""Assemble the GitHub Pages site.

    _site/index.html, app.js, style.css    the browser app, copied from web/
    _site/packs.json                       every role pack, for the app's picker
    _site/scan/index.html                  the latest scan's tracker page
    _site/scan/latest.json                 its ranked jobs, for the app

The scan files come from SCREENER_HOME (data/ by default). Only the job rows
are carried into latest.json - the raw results file also records your
searches and cities, and this site is public.

    python tools/build_site.py --out _site
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
PROFILES = ROOT / "profiles"

PACK_KEYS = ("description", "searches", "must_have_any", "exclude_title_keywords",
             "synonyms", "vocabulary", "seniority_terms")

# Fields a job row needs on the site. Everything else stays behind.
JOB_KEYS = ("job_id", "title", "company", "url", "location", "salary_label",
            "experience_label", "posted_label", "created_ms", "source", "score",
            "why", "skills", "company_apply", "career_url", "career_kind")

PLACEHOLDER = """<!doctype html><meta charset="utf-8"><title>No scan yet</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<body style="font:16px system-ui;max-width:40rem;margin:3rem auto;padding:0 1rem">
<h1>No scheduled scan yet</h1>
<p>The scan runs every 4 hours once the <code>RESUME_JSON</code> secret is set.
Meanwhile, <a href="../">search live</a>.</p></body>"""


def packs() -> dict:
    found = {}
    for path in sorted(PROFILES.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        found[path.stem] = {k: data.get(k) for k in PACK_KEYS if data.get(k) is not None}
    return found


def newest(folder: Path, pattern: str) -> Path | None:
    files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def slim(results: dict) -> dict:
    def rows(key):
        return [{k: job.get(k) for k in JOB_KEYS} for job in results.get(key) or []]
    return {"at": results.get("at"), "collected": results.get("collected"),
            "rejected": results.get("rejected"),
            "failed": results.get("failed_searches") or [],
            "shortlist": rows("shortlist"), "review": rows("review")}


def build(out: Path, home: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(WEB, out)
    (out / ".nojekyll").write_text("", encoding="utf-8")
    (out / "packs.json").write_text(json.dumps(packs(), ensure_ascii=False), encoding="utf-8")

    scan = out / "scan"
    scan.mkdir()
    jobs = home / "jobs"
    page = newest(jobs, "openings-*.html") if jobs.exists() else None
    results = newest(jobs, "results-*.json") if jobs.exists() else None
    if page and results:
        shutil.copy(page, scan / "index.html")
        data = json.loads(results.read_text(encoding="utf-8"))
        (scan / "latest.json").write_text(json.dumps(slim(data), ensure_ascii=False),
                                          encoding="utf-8")
        print(f"Published scan {results.name}: {len(data.get('shortlist') or [])} shortlisted")
    else:
        (scan / "index.html").write_text(PLACEHOLDER, encoding="utf-8")
        print("No scan output found - published the app with a placeholder scan page")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="_site", type=Path)
    parser.add_argument("--home", type=Path,
                        default=Path(os.environ.get("SCREENER_HOME") or ROOT / "data"))
    args = parser.parse_args()
    build(args.out.resolve(), args.home.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
