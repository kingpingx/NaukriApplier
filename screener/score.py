"""Score a job against your facts, 0-100.

The score exists to make one decision: shortlist this, queue it for you to read,
or drop it. So it is deliberately blunt and inspectable - every job carries the
breakdown that produced its number, and the report prints it. When the screener
ranks something you would not have, the reason is in the report rather than
buried in a model you cannot interrogate.

    skills       0-45   how much of what the job asks for you actually have
    title        0-25   is this the kind of role you want
    experience   0-15   are you inside the band they asked for
    location     0-10   is it somewhere you would work
    freshness    0-5    recent postings get replies; month-old ones do not

Those five weights are the default split and can be overridden per user - see
`weights:` in config.yaml. Someone who will relocate anywhere wants location at
0; someone who only wants a step up wants title weighted higher.

Hard rejects (score forced to 0) come first: they encode "never, regardless of
how well the rest matches".

Nothing in this module is specific to a job family. Every vocabulary term and
every synonym arrives from the role pack, so one field's spelling variants get
folded together exactly the way another's do, and adding a new field means
writing a YAML file rather than editing this one.
"""
from __future__ import annotations

import re

DEFAULT_WEIGHTS = {
    "skills": 45.0,
    "title": 25.0,
    "experience": 15.0,
    "location": 10.0,
    "freshness": 5.0,
}

# Spelling variants that are true regardless of job family. Anything
# role-specific belongs in profiles/<role>.yaml under `synonyms:`.
BASE_SYNONYMS = {
    "ci/cd": "ci cd",
    "cicd": "ci cd",
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
    "k8s": "kubernetes",
    "gcp": "google cloud",
    "aws": "amazon web services",
    "ml": "machine learning",
    "ai": "artificial intelligence",
    "db": "database",
    "oop": "object oriented programming",
}

# Title words marking a step up. Overridable per pack via `seniority_terms:`.
DEFAULT_SENIORITY = ("lead", "senior", "sr", "principal", "staff", "architect", "manager", "head")


def _synonyms(config: dict) -> dict:
    return {**BASE_SYNONYMS, **{str(k).lower(): str(v).lower()
                                for k, v in (config.get("synonyms") or {}).items()}}


# Spelled out after the synonyms, before punctuation is stripped. Stripping
# first folded C#, C++ and C into the one token "c", so a C# resume matched
# every C and C++ job. Not in BASE_SYNONYMS: a pack's own synonyms run after
# those and fold "csharp" straight back into "c#".
SYMBOL_LANGUAGES = {"c#": "csharp", "c++": "cplusplus", "f#": "fsharp"}


def _norm(text: str, synonyms: dict) -> str:
    text = (text or "").lower()
    for term, replacement in [*synonyms.items(), *SYMBOL_LANGUAGES.items()]:
        text = re.sub(rf"(?<![\w+#]){re.escape(term)}(?![\w+#])", replacement, text)
    return re.sub(r"[^a-z0-9 ]+", " ", text)


def _tokens(text: str, synonyms: dict) -> set[str]:
    return {t for t in _norm(text, synonyms).split() if len(t) > 1}


def _skill_key(skill: str, synonyms: dict) -> str:
    return " ".join(_norm(skill, synonyms).split())


def _gate_hit(term: str, haystack: str, synonyms: dict) -> bool:
    """Whether a `must_have_any` term appears in a job, as a word.

    A short term like "R" normalises to the single letter "r" - and a plain
    `in` then finds it inside "developer". One term like that
    in the gate lets every job through and the gate silently stops working, so
    the match is anchored to word boundaries, and a term normalising to a single
    character is only ever matched as a whole word.
    """
    key = _skill_key(term, synonyms)
    if not key:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", haystack) is not None


def _weights(config: dict) -> dict:
    weights = dict(DEFAULT_WEIGHTS)
    for key, value in (config.get("weights") or {}).items():
        if key in weights:
            try:
                weights[key] = max(0.0, float(value))
            except (TypeError, ValueError):
                continue
    total = sum(weights.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    # Always renormalise to 100 so a score stays comparable between users and
    # the thresholds in config.yaml keep meaning the same thing.
    return {k: v * 100.0 / total for k, v in weights.items()}


def hard_reject(job, config: dict) -> str | None:
    """Return a reason to drop the job outright, or None to keep scoring."""
    synonyms = _synonyms(config)
    title_l = (job.title or "").lower()
    company_l = (job.company or "").lower()

    for blocked in config.get("exclude_companies") or []:
        needle = str(blocked).lower().strip()
        if needle and needle in company_l:
            return f"company excluded ({blocked})"

    for word in config.get("exclude_title_keywords") or []:
        needle = str(word).lower().strip()
        if needle and needle in title_l:
            return f"title contains '{word}'"

    description_l = (job.description or "").lower()
    for word in config.get("exclude_description_keywords") or []:
        needle = str(word).lower().strip()
        if needle and needle in description_l:
            return f"description contains '{word}'"

    # A hard geography filter, distinct from `preferred_locations`, which only
    # feeds the location component of the score and can never keep a Bengaluru
    # job off a shortlist for someone who will not move there.
    #
    # A job that states no location at all is kept, deliberately - same rule as
    # `min_salary_lpa`. Plenty of Naukri postings leave it blank, and silently
    # dropping every one of them loses more good jobs than the filter saves.
    allowed = config.get("allowed_locations") or []
    if allowed:
        stated = (job.location or "").strip()
        if stated and not any(str(a).strip().lower() in stated.lower()
                              for a in allowed if str(a).strip()):
            return f"location '{stated}' is outside allowed_locations"

    must_have = config.get("must_have_any") or []
    if must_have:
        haystack = _norm(" ".join([job.title or "", " ".join(job.skills or []),
                                   job.description or ""]), synonyms)
        if not any(_gate_hit(term, haystack, synonyms)
                   for term in must_have if str(term).strip()):
            return "matches none of must_have_any"

    years = config.get("profile_years")
    gap = config.get("max_experience_gap_years", 2.0)
    if years is not None and job.min_exp is not None and gap is not None:
        if job.min_exp - years > gap:
            return f"needs {job.min_exp:g}y, you have {years:g}y"

    floor = config.get("min_salary_lpa")
    if floor is not None:
        stated = _salary_lpa(job)
        if stated is not None and stated < float(floor):
            return f"pays up to {stated:g} LPA, floor is {float(floor):g}"

    return None


_SALARY = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)?\s*(\d+(?:\.\d+)?)?\s*(?:lac|lakh|lpa)", re.IGNORECASE)


def _salary_lpa(job) -> float | None:
    """The top of a stated salary range, in lakhs per annum.

    The top on purpose: a "15-25 LPA" posting can pay 20, and the floor exists
    to drop jobs that cannot reach it, not ones that might. The cost is that a
    wide "5-25" range gets through too. A single figure is its own top.
    """
    match = _SALARY.search(job.salary_label or "")
    if not match:
        return None
    try:
        return float(match.group(2) or match.group(1))
    except (TypeError, ValueError):
        return None


# Tags Naukri postings carry that name no skill. The resume body is skill
# evidence, and it says "development" and "software" like every resume does, so
# these matched every posting and lifted weak ones onto the shortlist.
# ponytail: a fixed list; add to it when a filler tag shows up in `matched:`.
GENERIC_TAGS = {"development", "software", "software development", "software engineering",
                "engineering", "cloud", "core", "cd", "coding", "programming", "technology",
                "it", "microsoft"}


def matched_skills(wanted: list[str], config: dict) -> list[str]:
    """The skills in `wanted` your profile covers - by skill list, or by resume text."""
    synonyms = _synonyms(config)
    have = {_skill_key(s, synonyms) for s in config.get("profile_skills") or []}
    evidence = config.get("profile_evidence") or config.get("profile_text", "")
    have_blob = " ".join(have) + " " + _norm(evidence, synonyms)
    matched = []
    for skill in wanted:
        key = _skill_key(skill, synonyms)
        if key and (key in have or re.search(rf"\b{re.escape(key)}\b", have_blob)):
            matched.append(skill)
    return matched


def _skill_score(job, config: dict, cap: float) -> tuple[float, list[str]]:
    """Fraction of the job's asks that you cover, scaled to the skills weight."""
    wanted = [s for s in (job.skills or [])
              if s.strip() and s.strip().lower() not in GENERIC_TAGS]
    if not wanted:
        # No stated skills: fall back to the description so a well-written
        # posting without a tag list is not scored as if it asked for nothing.
        wanted = _description_skills(job, config)
    if not wanted:
        return round(cap * 0.4, 1), []   # neutral: neither reward nor punish

    matched = matched_skills(wanted, config)
    ratio = len(matched) / len(wanted)
    # A job asking for 3 things you all have is weaker evidence than one asking
    # for 10 of which you have 8, so temper the ratio with the absolute count.
    depth = min(len(matched) / 8.0, 1.0)
    return round(cap * (0.65 * ratio + 0.35 * depth), 1), matched


def _description_skills(job, config: dict) -> list[str]:
    """Vocabulary terms named in the description, when the job lists no tags."""
    vocabulary = config.get("vocabulary") or config.get("profile_skills") or []
    text = (job.description or "").lower()
    if not text:
        return []
    return [term for term in vocabulary
            if re.search(rf"(?<![\w+#]){re.escape(str(term).lower())}(?![\w+#])", text)][:12]


def _title_score(job, config: dict, cap: float) -> float:
    synonyms = _synonyms(config)
    title_tokens = _tokens(job.title or "", synonyms)
    if not title_tokens:
        return 0.0

    target_tokens: set[str] = set()
    for target in config.get("must_have_any") or []:
        target_tokens |= _tokens(str(target), synonyms)
    for title in config.get("titles") or []:
        target_tokens |= _tokens(str(title), synonyms)
    target_tokens |= _tokens(config.get("profile_text", ""), synonyms)

    if not target_tokens:
        return round(cap * 0.4, 1)

    overlap = len(title_tokens & target_tokens) / len(title_tokens)
    base = cap * 0.8
    score = base * overlap
    seniority = config.get("seniority_terms") or DEFAULT_SENIORITY
    if any(str(word).lower() in title_tokens for word in seniority):
        score += cap * 0.2
    return round(min(score, cap), 1)


def _experience_score(job, config: dict, cap: float) -> float:
    years = config.get("profile_years")
    if years is None or job.min_exp is None:
        return round(cap * 0.6, 1)   # unknown on either side: mid-band, not a guess
    top = job.max_exp if job.max_exp is not None else job.min_exp + 3
    if job.min_exp <= years <= top:
        return cap
    if years < job.min_exp:
        # Under-qualified is the harder no.
        return round(max(0.0, cap - (job.min_exp - years) * (cap * 0.4)), 1)
    # Over-qualified still gets interviews, so penalise gently.
    return round(max(0.0, cap - (years - top) * (cap / 6.0)), 1)


def _location_score(job, config: dict, cap: float) -> float:
    text = (job.location or "").lower()
    if not text:
        return round(cap * 0.5, 1)
    if "remote" in text or "work from home" in text:
        return cap
    for preferred in config.get("preferred_locations") or []:
        needle = str(preferred).lower().strip()
        if needle and needle in text:
            return cap
    return round(cap * 0.2, 1)


def _freshness_score(job, cap: float) -> float:
    age = job.age_days
    if age is None:
        return round(cap * 0.5, 1)
    if age <= 2:
        return cap
    if age <= 7:
        return round(cap * 0.6, 1)
    if age <= 30:
        return round(cap * 0.2, 1)
    return 0.0


def score(job, config: dict) -> dict:
    """Attach `.score` and `.score_breakdown` to the job; return the breakdown."""
    reason = hard_reject(job, config)
    if reason:
        job.score = 0.0
        job.score_breakdown = {"rejected": reason}
        job.matched_skills = []
        return job.score_breakdown

    weights = _weights(config)
    skills, matched = _skill_score(job, config, weights["skills"])
    breakdown = {
        "skills": skills,
        "title": _title_score(job, config, weights["title"]),
        "experience": _experience_score(job, config, weights["experience"]),
        "location": _location_score(job, config, weights["location"]),
        "freshness": _freshness_score(job, weights["freshness"]),
    }
    job.score = round(sum(breakdown.values()), 1)
    job.score_breakdown = breakdown
    job.matched_skills = matched
    return breakdown


def explain(job) -> str:
    """One-line justification, for the report."""
    breakdown = getattr(job, "score_breakdown", None) or {}
    if "rejected" in breakdown:
        return f"rejected: {breakdown['rejected']}"
    parts = " ".join(f"{k}={v:g}" for k, v in breakdown.items())
    matched = getattr(job, "matched_skills", [])
    tail = f" | matched: {', '.join(matched[:6])}" if matched else ""
    return f"{parts}{tail}"
