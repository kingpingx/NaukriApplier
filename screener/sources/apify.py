"""Job listings via an Apify actor.

Read this before enabling it, because the interesting constraints are not the
ones people expect.

WHY THIS IS NOT A CHEAP HTTP SCRAPER
    Naukri renders results from /jobapi/v3/search, and that endpoint is
    request-signed: it wants an `nkparam` header and a bearer token minted by
    the page itself, and a fetch without them returns 406. So the actor cannot
    skip the browser. It runs the same navigation the local backend does, in a
    container, which means Apify compute units are spent at roughly the rate a
    local run spends seconds.

WHY IT RUNS HEADFUL IN THE CLOUD
    Akamai serves "Access Denied" to headless Chromium. The actor image starts
    Xvfb and runs a headed browser inside it. This is what the base image
    apify/actor-python-playwright already supports; it is not extra work, but
    it does mean the actor cannot be shrunk to a plain requests job.

WHY YOUR SESSION HAS TO GO UP THERE
    Recommendations and the full result payloads need a logged-in session. The
    actor takes your storage state as input and holds it in an Apify key-value
    store. That store is private to your account, but it is still your Naukri
    login sitting on someone else's infrastructure. If that is not a trade you
    want to make, use the local backend - it is the default for this reason.

Set `source: apify` plus an `apify:` block in config.yaml, or APIFY_TOKEN in
the environment. See docs/apify.md for the full setup.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from urllib import error, parse, request

from ..model import Job
from ..paths import NAUKRI_STATE
from .base import SourceError

log = logging.getLogger("screener.sources.apify")

API = "https://api.apify.com/v2"

# The actor published alongside this repo. Point `actor:` at your own copy if
# you fork it - see apify_actor/ for the source.
DEFAULT_ACTOR = "naukri-job-screener/naukri-search"

# An actor doing real navigations with pauses between them is not fast.
DEFAULT_TIMEOUT_SEC = 900
POLL_SEC = 10


class ApifySource:
    name = "apify"

    def gather(self, config: dict) -> list:
        settings = config.get("apify") or {}
        token = settings.get("token") or os.environ.get("APIFY_TOKEN")
        if not token:
            raise SourceError(
                "No Apify token. Set APIFY_TOKEN or add apify.token to config.yaml.")

        actor = settings.get("actor") or DEFAULT_ACTOR
        timeout = int(settings.get("timeout_sec") or DEFAULT_TIMEOUT_SEC)

        payload = {
            "searches": config.get("searches") or [],
            "includeRecommended": bool(config.get("include_recommended")),
            "postedWithinDays": config.get("posted_within_days"),
            "experienceYears": config.get("profile_years"),
            "sessionState": _load_session(settings),
            "proxy": settings.get("proxy") or {
                # Datacenter IPs are on every bot-protection blocklist there is.
                # Residential is the only setting that reliably gets through, and
                # it is the expensive one. Stated here rather than buried.
                "useApifyProxy": True,
                "apifyProxyGroups": ["RESIDENTIAL"],
                "apifyProxyCountry": settings.get("country") or "IN",
            },
        }

        if not payload["sessionState"]:
            log.warning(
                "No session uploaded - the actor will run signed out. Naukri's "
                "recommendations feed and some result fields need a login.")

        run = self._start(actor, token, payload)
        run_id = run.get("id")
        log.info("Apify run %s started (actor %s)", run_id, actor)
        log.info("  Watch it: https://console.apify.com/actors/runs/%s", run_id)

        finished = self._wait(run_id, token, timeout)
        status = finished.get("status")
        if status != "SUCCEEDED":
            raise SourceError(
                f"Apify run {run_id} ended as {status}. "
                f"Logs: https://console.apify.com/actors/runs/{run_id}")

        dataset_id = (finished.get("defaultDatasetId") or "")
        records = self._dataset(dataset_id, token)
        log.info("Apify returned %d records", len(records))

        jobs: dict[str, Job] = {}
        for record in records:
            # The actor forwards Naukri's own payloads untouched, so the same
            # parser handles both backends and neither can drift from the other.
            job = Job.from_api(record, source=record.get("_source") or "apify")
            if job.job_id and job.url and job.job_id not in jobs:
                jobs[job.job_id] = job
        return list(jobs.values())

    # --- HTTP ----------------------------------------------------------

    def _call(self, url: str, token: str, data: dict | None = None,
              method: str = "GET") -> dict | list:
        separator = "&" if "?" in url else "?"
        full = f"{url}{separator}{parse.urlencode({'token': token})}"
        body = json.dumps(data).encode() if data is not None else None
        req = request.Request(full, data=body, method=method,
                              headers={"Content-Type": "application/json"})
        try:
            with request.urlopen(req, timeout=60) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            if exc.code == 401:
                raise SourceError("Apify rejected the token (401). Check APIFY_TOKEN.")
            if exc.code == 404:
                raise SourceError(
                    f"Apify actor not found (404). Check `actor:` in config.yaml.\n  {detail}")
            raise SourceError(f"Apify API error {exc.code}: {detail}")
        except (error.URLError, TimeoutError) as exc:
            raise SourceError(f"Could not reach the Apify API: {exc}")
        except json.JSONDecodeError as exc:
            raise SourceError(f"Apify returned a non-JSON response: {exc}")
        return parsed.get("data", parsed) if isinstance(parsed, dict) else parsed

    def _start(self, actor: str, token: str, payload: dict) -> dict:
        slug = actor.replace("/", "~")
        result = self._call(f"{API}/acts/{slug}/runs", token, payload, method="POST")
        if not isinstance(result, dict) or not result.get("id"):
            raise SourceError(f"Unexpected response starting the actor: {result!r}"[:300])
        return result

    def _wait(self, run_id: str, token: str, timeout: int) -> dict:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            run = self._call(f"{API}/actor-runs/{run_id}", token)
            status = run.get("status") if isinstance(run, dict) else None
            if status != last:
                log.info("  run %s: %s", run_id, status)
                last = status
            if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                return run
            time.sleep(POLL_SEC)

        raise SourceError(
            f"Apify run {run_id} did not finish within {timeout}s. It may still be "
            f"running: https://console.apify.com/actors/runs/{run_id}")

    def _dataset(self, dataset_id: str, token: str) -> list[dict]:
        if not dataset_id:
            return []
        items = self._call(f"{API}/datasets/{dataset_id}/items?clean=true&limit=10000", token)
        return items if isinstance(items, list) else []


def _load_session(settings: dict) -> dict | None:
    """Your Playwright storage state, to be replayed by the actor."""
    if settings.get("upload_session") is False:
        return None
    path = Path(settings.get("session_path") or NAUKRI_STATE)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read session at %s (%s); running signed out", path, exc)
        return None


# The public Store actor that reads Naukri's search without a login. Its output
# is its own shape, not Naukri's raw payload, so it gets its own mapping.
FEED_ACTOR = "blackfalcondata/naukri-jobs-feed"


def feed_to_job(record: dict) -> Job | None:
    """One record from FEED_ACTOR, as a Job.

    `portalUrl` is the job; `staticUrl` is the company's careers page, and
    linking that would send every "Apply" click to the wrong place.
    Ids stay bare Naukri ids, so a job seen by a local run and by this actor is
    the same ledger entry rather than two.
    """
    job_id, url = str(record.get("jobId") or ""), record.get("portalUrl") or ""
    if not job_id or not url:
        return None
    created_ms = None
    stamp = record.get("createdDate")
    if stamp:
        try:
            created_ms = int(datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                             .timestamp() * 1000)
        except ValueError:
            pass
    salary = (record.get("salary") or "").strip()
    rating = (record.get("ambitionBox") or {}).get("rating")
    return Job(
        job_id=job_id,
        title=(record.get("title") or "").strip(),
        company=(record.get("companyName") or "").strip(),
        url=url,
        skills=[str(s).strip() for s in record.get("skills") or [] if str(s).strip()],
        location=record.get("location") or None,
        experience_label=record.get("experienceText") or None,
        salary_label=None if not salary or salary.lower() == "not disclosed" else salary,
        min_exp=_as_float(record.get("minimumExperience")),
        max_exp=_as_float(record.get("maximumExperience")),
        description=(record.get("description") or record.get("descriptionSnippet") or "").strip(),
        posted_label=record.get("footerLabel") or None,
        created_ms=created_ms,
        company_apply=bool(record.get("companyApplyJob")),
        # The feed does not say whether applying opens a questionnaire.
        has_questionnaire=False,
        company_rating=_as_float(rating),
        source="apify",
    )


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
