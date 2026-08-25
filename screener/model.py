"""The Job record, normalised out of Naukri's own search API.

Naukri's site fetches its results as JSON from `/jobapi/v3/search` (keyword
search) and `/jobapi/v2/search/recom-jobs` (its recommendations for you). We
read those payloads rather than scraping result cards - same data, but with
fields the rendered card never shows, two of which decide everything about
whether a job can be applied to unattended:

    companyApplyJob       apply redirects off Naukri to the company's own
                          portal, where the form is bespoke and unautomatable
    questionnaireIdPresent applying opens a recruiter questionnaire

Both are recorded here so the daily run can route jobs correctly instead of
discovering the problem halfway through an application.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone

BASE = "https://www.naukri.com"


def _placeholder(record: dict, kind: str) -> str | None:
    for item in record.get("placeholders") or []:
        if item.get("type") == kind:
            label = (item.get("label") or "").strip()
            return label or None
    return None


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"&amp;?", "&", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Job:
    job_id: str
    title: str
    company: str
    url: str
    skills: list[str] = field(default_factory=list)
    location: str | None = None
    experience_label: str | None = None
    salary_label: str | None = None
    min_exp: float | None = None
    max_exp: float | None = None
    description: str = ""
    posted_label: str | None = None
    created_ms: int | None = None
    company_apply: bool = False
    has_questionnaire: bool = False
    company_rating: float | None = None
    source: str = ""

    @classmethod
    def from_api(cls, record: dict, source: str = "") -> "Job":
        jd_url = record.get("jdURL") or record.get("staticUrl") or ""
        if jd_url and not jd_url.startswith("http"):
            jd_url = BASE + jd_url

        raw_skills = record.get("tagsAndSkills") or ""
        skills = [s.strip() for s in raw_skills.split(",") if s.strip()]

        ambition = record.get("ambitionBoxData") or {}
        try:
            rating = float(ambition.get("AggregateRating")) if ambition.get("AggregateRating") else None
        except (TypeError, ValueError):
            rating = None

        return cls(
            job_id=str(record.get("jobId") or ""),
            title=(record.get("title") or "").strip(),
            company=(record.get("companyName") or "").strip(),
            url=jd_url,
            skills=skills,
            location=_placeholder(record, "location"),
            experience_label=_placeholder(record, "experience"),
            salary_label=_placeholder(record, "salary"),
            min_exp=_as_float(record.get("minimumExperience")),
            max_exp=_as_float(record.get("maximumExperience")),
            description=_strip_html(record.get("jobDescription")),
            posted_label=record.get("footerPlaceholderLabel"),
            created_ms=record.get("createdDate"),
            company_apply=bool(record.get("companyApplyJob")),
            has_questionnaire=bool(record.get("questionnaireIdPresent")),
            company_rating=rating,
            source=source,
        )

    @property
    def age_days(self) -> float | None:
        """How long ago the job was posted, in days."""
        if not self.created_ms:
            return None
        try:
            posted = datetime.fromtimestamp(self.created_ms / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return (datetime.now(tz=timezone.utc) - posted).total_seconds() / 86400

    @property
    def auto_applicable(self) -> bool:
        """True only when a single click on Naukri completes the application.

        An off-site apply lands on a company portal with a bespoke form, and a
        questionnaire asks the recruiter's own screening questions. Neither is
        something to hand to an unattended run - the second especially, since
        answering it means inventing answers on your behalf.
        """
        return not self.company_apply and not self.has_questionnaire

    def to_dict(self) -> dict:
        return asdict(self)


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
