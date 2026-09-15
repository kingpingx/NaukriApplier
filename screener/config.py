"""Search criteria and thresholds, assembled from four layers.

    1. DEFAULTS          thresholds that work for anyone
    2. role pack         profiles/<role>.yaml - vocabulary for a job family
    3. your facts        resume.json and/or profile.json
    4. config.yaml       your explicit overrides, final word

Nothing here knows what job you are looking for. The original version of this
file hardcoded "SDET" and "QA Automation" as fallback searches, which is fine
for one person and useless for everyone else; role packs replace that, and a
role pack is a data file a user can write without touching Python.

Layers 3 and 4 are both optional, but not both at once - with neither, there is
nothing to search for and nothing to score against, and the error says so.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import yaml

from .paths import CONFIG_PATH, PROFILE_JSON as PROFILE_PATH, PROFILES_DIR, RESUME_JSON

log = logging.getLogger("screener.config")

DEFAULTS = {
    # --- who you are (filled from resume/profile when absent) ------------
    "role": None,                  # name of a pack in profiles/
    "years": None,                 # total experience; overrides what was parsed
    "skills": [],                  # added to whatever was parsed
    "skill_years": {},
    "titles": [],

    # --- what to search --------------------------------------------------
    "searches": [],
    "preferred_locations": [],
    "allowed_locations": [],       # hard geography filter; empty = anywhere
    "must_have_any": [],
    "exclude_title_keywords": [],
    "exclude_companies": [],
    "exclude_description_keywords": [],
    "min_salary_lpa": None,
    "posted_within_days": None,

    # --- thresholds ------------------------------------------------------
    "auto_apply_min_score": 72,
    "review_min_score": 55,
    "daily_target": 50,
    "max_experience_gap_years": 2.0,
    "include_recommended": True,

    # --- where jobs come from --------------------------------------------
    "source": "local",             # local | apify | none (boards only)
    "headless": False,             # minimize the browser window - see session.minimize
    "include_linkedin": False,
    "boards": [],                  # remote boards to read too - see sources/boards.py
    "companies": {},               # greenhouse/lever/ashby: [company slugs]
    "remote_regions": [],          # where you can work from; empty = India defaults
    "include_himalayas": False,    # older spelling of `boards: [himalayas]`
    "himalayas": {},               # pages: how deep to page the feed
    "weworkremotely": {},          # categories: which RSS feeds to read
    "apify": {},                   # token, actor, proxy - see docs/apify.md

    # --- scoring ---------------------------------------------------------
    "synonyms": {},                # merged over the role pack's own
    "weights": {},                 # override the 45/25/15/10/5 split
}

# Keys a role pack is allowed to contribute. A pack that sets a threshold or a
# token would be surprising - packs describe a job family, not your account.
PACK_KEYS = {
    "must_have_any", "exclude_title_keywords", "exclude_description_keywords",
    "searches", "synonyms", "vocabulary", "seniority_terms", "description",
}


class ConfigError(RuntimeError):
    """Configuration exists but cannot be used."""


# Distinguishes "argument not given, go look on disk" from an explicit None
# meaning "there is no profile / no resume".
_UNSET: dict = {"__unset__": True}


# --- role packs ---------------------------------------------------------

def available_roles(directory: Path = PROFILES_DIR) -> list[str]:
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.yaml"))


PACK_TEMPLATE = """\
description: {description}

# Searches to start from. These are job TITLES, the way a posting words them -
# not skills. "Hibernate" returns noise; "Java Developer" returns jobs.
searches:
{searches}

# A job must mention at least one of these somewhere or it is dropped before
# scoring. This is the "is this even my field" gate, and it is what makes a
# shortlist short. Keep it broad enough to catch odd wordings, narrow enough
# to exclude the field next door.
must_have_any:
{gate}

# Dropped on the title alone.
exclude_title_keywords:
  - intern
  - fresher
  - trainee

# Spellings folded together before matching, so a posting asking for one
# wording scores against a resume using the other.
synonyms: {{}}
  # abbreviation: full form

# Terms recognised in a job description when a posting lists no skill tags -
# and the vocabulary this pack is matched against when detecting your role.
# The longer and more specific this is, the better both work.
vocabulary:
{vocabulary}

seniority_terms:
  - lead
  - senior
  - sr
  - principal
  - staff
  - architect
  - manager
  - head
"""


def scaffold_pack(role: str, facts: dict | None = None,
                  directory: Path = PROFILES_DIR) -> Path:
    """Write a starter profiles/<role>.yaml, seeded from a resume if there is one.

    A blank YAML file is a worse starting point than it looks - the three lists
    below have to agree with each other, and it is not obvious which ones matter.
    Seeding from the user's own resume means the first version is already half
    right and the shape is visible.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-")
    if not slug:
        raise ConfigError(f"{role!r} is not a usable role name.")

    path = directory / f"{slug}.yaml"
    if path.exists():
        raise ConfigError(f"{path} already exists - edit it rather than overwriting.")

    facts = facts or {}
    skills = [str(s) for s in (facts.get("skills") or [])][:40]
    titles = [str(t) for t in (facts.get("titles") or [])][:4]

    searches = [t for t in (_clean_title(x) for x in titles) if t]
    noun = _title_noun(titles)
    for skill in skills[:4]:
        if len(skill) <= 24 and skill.lower() not in TOO_BROAD:
            searches.append(f"{skill} {noun}")
    searches = _dedupe(searches)[:5] or [f"{slug.replace('-', ' ').title()}"]

    gate = _dedupe([t.lower() for t in (_clean_title(x) for x in titles) if t]
                   + [slug.replace("-", " ")])

    def block(items: list[str], fallback: str) -> str:
        if not items:
            return f"  # - {fallback}"
        return "\n".join(f"  - {item}" for item in items)

    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(PACK_TEMPLATE.format(
        description=role.replace("-", " ").capitalize(),
        searches=block(searches, "Some Job Title"),
        gate=block(gate, "your field"),
        vocabulary=block(skills, "A Tool You Use"),
    ), encoding="utf-8")
    return path


def _fold(text: str) -> str:
    """Lowercase, and collapse every separator so spelling variants compare equal.

    "Full-Stack", "Full Stack" and "fullstack" all fold to "full stack" once the
    separators go, which is the only way a substring test can treat them as the
    one title they are.
    """
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _fold_squashed(text: str) -> str:
    """`_fold`, with the spaces removed too - catches "fullstack" written solid."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _token_match(term: str, folded_text: str) -> bool:
    """Whole-token containment in a folded string.

    Plain `in` is not safe here. Folding drops the punctuation that carries the
    meaning of a short term - "c#" folds to "c" - and a bare substring test then
    finds it inside "accountant" and matches a .NET pack to an accounting CV.
    Comparing on token boundaries stops that, and a term that folds to almost
    nothing is declined outright; `_mentions` still scores it against the body,
    where the punctuation survives.
    """
    folded = _fold(term)
    if len(folded) < 2:
        return False
    return f" {folded} " in f" {folded_text} "


def _mentions(term: str, text: str) -> bool:
    """Whether `text` names `term` as a word, not as part of a longer one.

    The lookarounds admit "+" and "#" so "c++" and "c#" survive, and stop "net"
    from matching every "network" in a resume.
    """
    if not term:
        return False
    return re.search(rf"(?<![\w+#]){re.escape(term)}(?![\w+#])", text) is not None


def detect_role(facts: dict, directory: Path = PROFILES_DIR) -> tuple[str | None, dict]:
    """Guess which role pack fits a resume. Returns (role, scores).

    Without this, a user who never sets `role:` gets searches derived from bare
    skill names - and "Hibernate jobs in Hyderabad" is not a search anyone runs.
    The pack supplies real job titles, so picking one automatically is the
    difference between the tool working out of the box and working after you
    have read enough of the README to configure it.

    Scored on three signals, weighted by how much each one tells you:

        titles      3  "Data Engineer" in your last title is near-conclusive
        vocabulary  2  the tools you list are the strongest ordinary signal
        gate terms  1  broad by design, so worth the least

    Everything is normalised by pack size, or the pack with the longest
    vocabulary would win every time.
    """
    haystack_skills = {str(s).lower() for s in (facts.get("skills") or [])}
    # Hyphens and spacing are a coin toss in a job title - "Full-Stack",
    # "Full Stack" and "Fullstack" are one title written three ways, and a
    # substring test against the raw text agrees with only one of them. Folding
    # the separators away is what lets the title tiebreak below fire at all.
    titles_text = _fold(" ".join(str(t) for t in (facts.get("titles") or [])))
    body = (facts.get("text") or "").lower()

    scores: dict[str, float] = {}
    named_in_title: set[str] = set()
    for role in available_roles(directory):
        try:
            pack = load_pack(role, directory)
        except ConfigError:
            continue

        vocabulary = [str(v).lower() for v in (pack.get("vocabulary") or [])]
        gate = [str(g).lower() for g in (pack.get("must_have_any") or [])]

        vocab_hits = sum(1 for term in vocabulary if term in haystack_skills)
        # A resume that lists few skills explicitly still names its tools in the
        # body, so fall back to the text - at a discount, since a passing
        # mention is weaker evidence than a skills-section entry.
        vocab_text_hits = sum(
            1 for term in vocabulary
            if term not in haystack_skills and _mentions(term, body))

        title_hits = sum(1 for term in gate if _token_match(term, titles_text))
        gate_hits = sum(1 for term in gate if _mentions(term, body))

        # `must_have_any` is a deliberately broad filter - "full stack" belongs
        # in the frontend AND backend gates, because a full stack posting suits
        # either. That breadth is right for filtering and useless for telling
        # packs apart, so identity is matched against the pack's job titles,
        # which are specific by construction.
        pack_titles = [str(s) for s in (pack.get("searches") or [])]
        squashed = _fold_squashed(titles_text)
        if any(_fold(t) in titles_text or _fold_squashed(t) in squashed
               for t in pack_titles):
            named_in_title.add(role)

        if not vocabulary or not gate:
            continue

        score = (
            3.0 * (title_hits / len(gate))
            + 2.0 * ((vocab_hits + 0.4 * vocab_text_hits) / len(vocabulary))
            + 1.0 * (gate_hits / len(gate))
        )
        scores[role] = round(score, 4)

    if not scores:
        return None, {}

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0

    # Two guards against a confident-looking wrong answer. A low absolute score
    # means the resume matched nothing shipped, and a near-tie between
    # neighbouring fields is better left unset than guessed.
    if best_score < 0.25:
        return None, scores

    if runner_up and best_score < runner_up * 1.25:
        # One exception, for the case the margin rule handles badly. A full
        # stack resume is *supposed* to look half like frontend and half like
        # backend, so it can never clear a margin test - but "Full Stack
        # Developer" in the job title is not ambiguous evidence. When the winner
        # is named in the person's own title and the runner-up is not, that
        # settles it.
        if not (best in named_in_title and ranked[1][0] not in named_in_title):
            return None, scores
    return best, scores


def load_pack(role: str | None, directory: Path = PROFILES_DIR) -> dict:
    """Load profiles/<role>.yaml. Returns {} when no role is set."""
    if not role:
        return {}
    path = directory / f"{role}.yaml"
    if not path.exists():
        known = available_roles(directory)
        raise ConfigError(
            f"No role pack named '{role}'.\n"
            f"  Available: {', '.join(known) if known else '(none installed)'}\n"
            f"  Or drop your own at {directory / (role + '.yaml')}"
        )
    try:
        pack = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}")
    if not isinstance(pack, dict):
        raise ConfigError(f"{path} must be a YAML mapping.")

    unknown = set(pack) - PACK_KEYS
    if unknown:
        log.warning("Ignoring unsupported key(s) in %s: %s", path.name, ", ".join(sorted(unknown)))
    return {k: v for k, v in pack.items() if k in PACK_KEYS}


# --- fact sources -------------------------------------------------------

def load_profile(path: Path = PROFILE_PATH) -> dict | None:
    """The Naukri profile extract, if one exists."""
    if not path.exists():
        return None
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("profile.json unreadable (%s); ignoring", exc)
        return None
    return profile or None


def load_resume_facts(path: Path = RESUME_JSON) -> dict | None:
    from . import resume as resume_mod
    return resume_mod.load(path)


def profile_years(profile: dict) -> float | None:
    """Total experience in years, parsed from '6 Years 3 Months'."""
    text = profile.get("experience") or ""
    years = re.search(r"(\d+)\s*Year", text, re.IGNORECASE)
    months = re.search(r"(\d+)\s*Month", text, re.IGNORECASE)
    if not years and not months:
        return None
    total = float(years.group(1)) if years else 0.0
    total += (float(months.group(1)) / 12) if months else 0.0
    return round(total, 2)


def profile_skills(profile: dict) -> list[str]:
    """Key skills plus the skill column of the IT-skills table."""
    skills = list(profile.get("key_skills") or [])
    for row in profile.get("it_skills") or []:
        # Rows read "Playwright - 2025 1 Year 3 Months"; take the leading name.
        name = re.split(r"\s+[-\d]", row, maxsplit=1)[0].strip()
        if name:
            skills.append(name)
    return skills


def profile_skill_years(profile: dict) -> dict[str, float]:
    """Per-skill years off the IT-skills table: 'Python - 2024 2 Years 1 Month'."""
    out: dict[str, float] = {}
    for row in profile.get("it_skills") or []:
        name = re.split(r"\s+[-\d]", row, maxsplit=1)[0].strip()
        if not name:
            continue
        years = re.search(r"(\d+)\s*Year", row, re.IGNORECASE)
        months = re.search(r"(\d+)\s*Month", row, re.IGNORECASE)
        if not years and not months:
            continue
        total = float(years.group(1)) if years else 0.0
        total += (float(months.group(1)) / 12) if months else 0.0
        if total > 0:
            out[name.lower()] = round(total, 2)
    return out


def _dedupe(values) -> list[str]:
    seen, out = set(), []
    for value in values or []:
        text = str(value).strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


# --- assembly -----------------------------------------------------------

def load(path: Path = CONFIG_PATH,
         profile: dict | None = _UNSET,
         resume_facts: dict | None = _UNSET) -> dict:
    """Build the effective config.

    `profile` and `resume_facts` default to whatever is on disk. Pass None
    explicitly to mean "there is none" - the distinction matters to tests and
    to any caller assembling a config for someone other than the local user.
    """
    config = {k: (v.copy() if isinstance(v, (dict, list)) else v) for k, v in DEFAULTS.items()}

    user: dict = {}
    if path and path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML: {exc}")
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path} must be a YAML mapping.")
        unknown = set(loaded) - set(DEFAULTS)
        if unknown:
            log.warning("Ignoring unknown key(s) in %s: %s", path.name, ", ".join(sorted(unknown)))
        user = {k: v for k, v in loaded.items() if k in DEFAULTS}

    if profile is _UNSET:
        profile = load_profile()
    if resume_facts is _UNSET:
        resume_facts = load_resume_facts()

    role = user.get("role") or config.get("role")
    detected = None
    if not role and resume_facts:
        # Nobody set a role. Guess one rather than fall back to searching for
        # bare skill names, which produces queries no job board answers well.
        detected, _scores = detect_role(resume_facts)
        if detected:
            role = detected
            log.info("No role set - matched your resume to the '%s' pack. "
                     "Set `role:` in config.yaml to pin it.", detected)

    config["role"] = role
    config["role_detected"] = detected
    pack = load_pack(role)
    vocabulary = _dedupe(list(pack.get("vocabulary") or []))

    if not profile and not resume_facts and not user.get("skills"):
        raise ConfigError(
            "Nothing to match jobs against. Do one of:\n"
            "    python main.py --resume path/to/your_cv.pdf   parse your resume\n"
            "    python main.py --extract                      read your Naukri profile\n"
            "  or list `skills:` and `years:` directly in config.yaml."
        )

    # --- merge the facts -------------------------------------------------
    skills: list[str] = []
    skill_years: dict[str, float] = {}
    titles: list[str] = []
    years: float | None = None
    location: str | None = None
    # Two texts, because they answer different questions. Title scoring treats
    # every word of `profile_text` as "a title word that is yours", so a whole
    # resume there lets almost any job title match. The resume body is still
    # the best evidence of a skill, so it goes in `profile_evidence` instead.
    identity: list[str] = []
    evidence: list[str] = []

    if resume_facts:
        skills += resume_facts.get("skills") or []
        skill_years.update({k.lower(): v for k, v in (resume_facts.get("skill_years") or {}).items()})
        titles += resume_facts.get("titles") or []
        years = resume_facts.get("years")
        location = resume_facts.get("location")
        evidence.append(resume_facts.get("text") or "")

    if profile:
        skills += profile_skills(profile)
        # The profile's IT-skills table is dated by Naukri itself, so it wins
        # over a number typed into a resume years ago.
        skill_years.update(profile_skill_years(profile))
        designation = (profile.get("current_designation") or "").strip()
        if designation:
            titles.insert(0, designation)
        years = profile_years(profile) or years
        location = (profile.get("location") or "").split(",")[0].strip() or location
        identity += [str(profile.get(f) or "") for f in
                     ("resume_headline", "profile_summary", "current_designation")]
        for field in ("career_profile", "employment", "projects", "it_skills"):
            value = profile.get(field) or ""
            evidence.append(" ".join(map(str, value)) if isinstance(value, list) else str(value))

    # User overrides sit on top of everything derived.
    skills += user.get("skills") or []
    skill_years.update({k.lower(): float(v) for k, v in (user.get("skill_years") or {}).items()})
    titles += user.get("titles") or []
    if user.get("years") is not None:
        years = float(user["years"])

    config["profile_skills"] = _dedupe(skills)
    config["skill_years"] = skill_years
    config["profile_years"] = years
    config["titles"] = _dedupe(titles)
    config["profile_text"] = " ".join(p for p in identity if p)
    config["profile_evidence"] = " ".join(p for p in identity + evidence if p)[:20000]
    config["vocabulary"] = vocabulary
    config["role_description"] = pack.get("description")

    # --- layer the pack, then the user, over the defaults ----------------
    for key in ("must_have_any", "exclude_title_keywords", "exclude_description_keywords"):
        config[key] = _dedupe(list(pack.get(key) or []) + list(user.get(key) or []))

    config["synonyms"] = {**(pack.get("synonyms") or {}), **(user.get("synonyms") or {})}
    config["seniority_terms"] = _dedupe(pack.get("seniority_terms") or [])

    for key, value in user.items():
        # `role` is resolved above (possibly by detection) - re-applying a null
        # from the file here would undo that.
        if key in ("role", "skills", "skill_years", "titles", "years", "synonyms",
                   "must_have_any", "exclude_title_keywords", "exclude_description_keywords"):
            continue
        config[key] = value

    if not config["searches"]:
        config["searches"] = _default_searches(config, pack, location)
    if not config["preferred_locations"]:
        config["preferred_locations"] = [l for l in (location, "Remote") if l]

    _validate(config)
    return config


# Nouns that make a skill into a job title. A search for "Hibernate" returns
# noise; "Hibernate Developer" returns jobs.
ROLE_NOUNS = ("Developer", "Engineer")

# Words that, alone, are too broad to be a useful query.
TOO_BROAD = {"software", "engineer", "developer", "programmer", "it", "computer",
             "technology", "consultant", "analyst", "specialist", "professional"}


def _title_noun(titles: list[str]) -> str:
    """The role noun this person's own titles use - Developer vs Engineer."""
    joined = " ".join(titles).lower()
    for noun in ROLE_NOUNS:
        if noun.lower() in joined:
            return noun
    return ROLE_NOUNS[0]


def _clean_title(title: str) -> str | None:
    """A job title reduced to something worth searching for."""
    # A dash only ends the title when it is spaced like a separator. Cutting at
    # any hyphen turned "Full-Stack Developer" into "Full", which is not a
    # search - and hyphenated titles are common enough to matter.
    cleaned = re.split(r"\s*[|(,]|\s+[-–—]\s+|\s*[–—]", title)[0].strip()
    cleaned = re.sub(r"^(sr\.?|senior|jr\.?|junior|lead|principal|staff)\s+",
                     "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s+(i{1,3}|iv|v|\d)$", "", cleaned, flags=re.IGNORECASE).strip()
    if not (2 < len(cleaned) < 40):
        return None
    if cleaned.lower() in TOO_BROAD:
        return None
    return cleaned


def _default_searches(config: dict, pack: dict, location: str | None) -> list[dict]:
    """Search terms inferred from the role pack and your own facts.

    Breadth beats depth: five distinct keyword/location pairs return more usable
    jobs than paging five deep into one, because page 5 of a query is the tail
    of a ranking that already put its best matches on page 1.

    Order matters. A pack's searches were written by someone who knows the job
    family, so they lead. Your own last title comes next - the single best
    predictor of your next one. Bare skills come last and only ever paired with
    a role noun, because job boards index titles, not tech stacks.
    """
    keywords: list[str] = []

    for entry in pack.get("searches") or []:
        if isinstance(entry, str):
            keywords.append(entry)
        elif isinstance(entry, dict) and entry.get("keyword"):
            keywords.append(entry["keyword"])

    titles = [str(t) for t in (config.get("titles") or [])]
    for title in titles[:2]:
        cleaned = _clean_title(title)
        if cleaned:
            keywords.append(cleaned)

    # Skills as titles, not as bare terms: "Java Developer", never "Hibernate".
    noun = _title_noun(titles)
    for skill in (config.get("profile_skills") or [])[:6]:
        skill = str(skill).strip()
        if not skill or skill.lower() in TOO_BROAD or len(skill) > 24:
            continue
        keywords.append(f"{skill} {noun}")

    terms = _dedupe(keywords)
    if not terms:
        raise ConfigError(
            "No search keywords could be derived.\n"
            "  Set `role:` to one of the packs in profiles/ (see `--roles`), or "
            "list `searches:` in config.yaml explicitly."
        )
    return [{"keyword": k, "location": location} for k in terms[:5]]


def _validate(config: dict) -> None:
    if config["auto_apply_min_score"] < config["review_min_score"]:
        raise ConfigError(
            "auto_apply_min_score must be >= review_min_score "
            f"(got {config['auto_apply_min_score']} and {config['review_min_score']})."
        )
    if config["source"] not in ("local", "apify", "none"):
        raise ConfigError(
            f"source must be 'local', 'apify' or 'none', not {config['source']!r}.")
    from .sources import boards as boards_mod
    if isinstance(config.get("boards"), str):
        config["boards"] = [b for b in config["boards"].split(",") if b.strip()]
    problems = boards_mod.unknown(config.get("boards"))
    if problems:
        raise ConfigError("Unknown entries in `boards:`\n  " + "\n  ".join(problems))
    if config["source"] == "none" and not boards_mod.selected(config):
        raise ConfigError(
            "source is 'none' but no boards are selected, so there is nothing to read.\n"
            "  Add e.g. `boards: [all]` to config.yaml, or pass --boards all.")
    if config["source"] == "apify":
        token = (config.get("apify") or {}).get("token")
        import os
        if not token and not os.environ.get("APIFY_TOKEN"):
            raise ConfigError(
                "source is 'apify' but no API token is set.\n"
                "  Either set APIFY_TOKEN in your environment, or add:\n"
                "      apify:\n        token: apify_api_...\n"
                "  See docs/apify.md."
            )
    if not config.get("profile_skills"):
        raise ConfigError(
            "No skills to match against. Check data/resume.json parsed correctly, "
            "or list `skills:` in config.yaml."
        )


def summarise(config: dict) -> str:
    years = config.get("profile_years")
    searches = config.get("searches") or []
    role = config.get("role")
    if role and config.get("role_detected"):
        role = f"{role}  (matched to your resume - set `role:` to pin it)"
    lines = [
        f"  Role pack:  {role or '(none - searches derived from your resume alone)'}",
        f"  Experience: {years:g} years" if years is not None else
        "  Experience: unknown - set `years:` in config.yaml for experience scoring",
        f"  Skills:     {len(config.get('profile_skills') or [])}",
        f"  Locations:  {', '.join(config.get('preferred_locations') or []) or '(any)'}",
        f"  Source:     {config.get('source')}"
        + ("  (Naukri skipped)" if config.get("source") == "none" else ""),
    ]
    from .sources import boards as boards_mod
    selected = boards_mod.selected(config)
    if selected:
        lines.append(f"  Boards:     {', '.join(boards_mod.LABELS.get(b, b) for b in selected)}")
        lines.append(f"  Open from:  {', '.join(boards_mod.regions_for(config))}"
                     "  (set `remote_regions:` to change)")
    lines.append(f"  Searches:   {len(searches)}")
    for entry in searches:
        where = entry.get("location") or "all India"
        lines.append(f"                - {entry.get('keyword')} in {where}")
    return "\n".join(lines)
