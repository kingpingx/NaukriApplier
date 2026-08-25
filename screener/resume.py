"""Read a resume into the same shape the Naukri profile extract produces.

The screener scores jobs against a set of facts: what you can do, how long you
have done it, what you have been called, and where you are. A Naukri profile
carries those as structured fields. A resume carries them as prose, so this
module's whole job is to get prose into that same shape - after which nothing
downstream needs to know which source a fact came from.

Deliberately not an LLM call. A parser you can read is a parser you can correct,
and every fact it extracts is written to data/resume.json where you can edit it
by hand before a scan uses it. Anything this gets wrong, you fix once.

    PDF   pdfplumber, falling back to pypdf
    DOCX  python-docx
    TXT   read as-is

All three extras are optional. A missing one degrades to "convert your resume to
.txt" rather than a stack trace on import.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path

from .paths import RESUME_DIR, RESUME_JSON

log = logging.getLogger("screener.resume")

SUPPORTED = {".pdf", ".docx", ".txt", ".md"}

# Section headings that introduce a skills list. A resume that uses none of
# these still works - skills are also matched against the vocabulary below.
SKILL_HEADINGS = re.compile(
    r"^\s*(technical\s+)?(skills?|competenc(y|ies)|technolog(y|ies)|tech\s+stack|"
    r"tools?\s*(&|and)?\s*(technolog(y|ies))?|expertise|proficienc(y|ies))\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# A heading that ends the skills section.
SECTION_BREAK = re.compile(
    r"^\s*(work\s+)?(experience|employment|education|projects?|certificat|"
    r"achievements?|summary|profile|objective|awards?|publications?|"
    r"languages?|interests?|references?|declaration)\b",
    re.IGNORECASE | re.MULTILINE,
)

# Splits a skills block into individual skills. Resumes use every one of these.
SKILL_SPLIT = re.compile(r"[,;|•▪·‣⁃\n\t]+|\s{3,}|(?<=\w)\s+/\s+(?=\w)")

# Tokens that are never a skill, however they were punctuated.
SKILL_NOISE = {
    "and", "or", "with", "using", "etc", "various", "other", "others", "including",
    "knowledge", "hands", "on", "hands-on", "experience", "expertise", "familiar",
    "proficient", "strong", "good", "excellent", "basic", "advanced", "working",
    "years", "year", "yrs", "months", "level", "skills", "skill", "tools", "tool",
    "technologies", "technology", "frameworks", "framework", "languages", "language",
}

DATE_RANGE = re.compile(
    r"(?P<from>(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*,?\s*'?\d{2,4}|\d{1,2}/\d{4}|\d{4})"
    r"\s*(?:-|–|—|to|until|through)\s*"
    r"(?P<to>(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*,?\s*'?\d{2,4}|\d{1,2}/\d{4}|\d{4}|present|current|now|till\s*date|ongoing)",
    re.IGNORECASE,
)

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# "6+ years of experience", "over 6 years", "6.5 yrs"
STATED_YEARS = re.compile(
    r"(?:(?:over|more\s+than|nearly|about|around|approx\w*)\s+)?"
    r"(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?)\b(?:[^.\n]{0,40}?experien)?",
    re.IGNORECASE,
)

# "Playwright - 2 years", "Python (3 yrs)", "Selenium: 4+ years"
SKILL_YEARS = re.compile(
    r"([A-Za-z][A-Za-z0-9+#./ _-]{1,34}?)\s*[\-:–(]{1,2}\s*(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
PHONE = re.compile(r"(?:\+\d{1,3}[\s-]?)?(?:\(\d{2,4}\)[\s-]?)?\d{3,5}[\s-]?\d{3,5}(?:[\s-]?\d{2,4})?")
URL = re.compile(r"(?:https?://|www\.)[^\s,;)\]]+", re.IGNORECASE)

# Indian metros plus the common remote markers, matched against the header
# block. Enough to fill preferred_locations; the config file overrides it.
CITIES = [
    "Bengaluru", "Bangalore", "Pune", "Mumbai", "Hyderabad", "Chennai", "Delhi",
    "New Delhi", "Gurgaon", "Gurugram", "Noida", "Kolkata", "Ahmedabad", "Jaipur",
    "Indore", "Chandigarh", "Kochi", "Coimbatore", "Thiruvananthapuram", "Nagpur",
    "Bhubaneswar", "Mysuru", "Mysore", "Vadodara", "Surat", "Lucknow",
    "Remote", "Hybrid", "Work From Home",
]


class ResumeError(RuntimeError):
    """The resume exists but could not be read."""


# --- text extraction ----------------------------------------------------

def _read_pdf(path: Path) -> str:
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None
    if pdfplumber is not None:
        try:
            with pdfplumber.open(str(path)) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            if text.strip():
                return text
            log.warning("pdfplumber found no text in %s - trying pypdf", path.name)
        except Exception as exc:
            log.warning("pdfplumber failed on %s (%s) - trying pypdf", path.name, exc)

    try:
        from pypdf import PdfReader
    except ImportError:
        raise ResumeError(
            f"Reading {path.name} needs a PDF library. Either:\n"
            f"    pip install pdfplumber\n"
            f"  or save your resume as .docx / .txt and drop it in {RESUME_DIR}"
        )
    try:
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    except Exception as exc:
        raise ResumeError(f"Could not read {path.name}: {exc}")


def _read_docx(path: Path) -> str:
    try:
        import docx
    except ImportError:
        raise ResumeError(
            f"Reading {path.name} needs python-docx:\n"
            f"    pip install python-docx\n"
            f"  or save your resume as .txt"
        )
    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise ResumeError(f"Could not read {path.name}: {exc}")

    parts = [p.text for p in document.paragraphs]
    # Skills are very often laid out in a borderless table, which the paragraph
    # walk above skips entirely - a resume can lose its whole skills section here.
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def read_text(path: Path) -> str:
    """Extract raw text from a resume file."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text = _read_pdf(path)
    elif suffix == ".docx":
        text = _read_docx(path)
    elif suffix in (".txt", ".md"):
        text = path.read_text(encoding="utf-8", errors="replace")
    elif suffix == ".doc":
        raise ResumeError(
            f"{path.name} is the old .doc format, which cannot be read directly. "
            "Open it and 'Save As' .docx or .pdf."
        )
    else:
        raise ResumeError(f"Unsupported resume format '{suffix}'. Use one of: {', '.join(sorted(SUPPORTED))}")

    if not text.strip():
        raise ResumeError(
            f"{path.name} produced no text. If it is a scanned image, export a "
            "text-based copy - this parser does not do OCR."
        )
    return text


# --- fact extraction ----------------------------------------------------

def _clean_skill(raw: str) -> str | None:
    skill = re.sub(r"\(.*?\)", " ", raw)                  # drop parentheticals
    skill = re.sub(r"[^\w+#./ -]+", " ", skill)
    skill = re.sub(r"\s+", " ", skill).strip(" -./")
    if not skill or len(skill) > 40:
        return None
    words = skill.lower().split()
    if not words or all(w in SKILL_NOISE for w in words):
        return None
    if len(words) > 5:                                     # a sentence, not a skill
        return None
    if len(skill) < 2 or skill.isdigit():
        return None
    return skill


def extract_skills(text: str, vocabulary: list[str] | None = None) -> list[str]:
    """Skills from the resume's own skills section, plus any known vocabulary hit.

    Two passes because neither alone is enough: the section pass catches skills
    no shipped vocabulary could know about, and the vocabulary pass catches the
    ones mentioned only in a bullet under a job.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(skill: str | None) -> None:
        if not skill:
            return
        key = skill.lower()
        if key not in seen:
            seen.add(key)
            found.append(skill)

    for heading in SKILL_HEADINGS.finditer(text):
        block = text[heading.end():]
        stop = SECTION_BREAK.search(block)
        if stop:
            block = block[:stop.start()]
        # A skills section runs to the next heading, but an unheaded resume can
        # run to the end of the file - cap it so one missing heading does not
        # swallow the entire document into the skills list.
        block = block[:2000]
        for piece in SKILL_SPLIT.split(block):
            add(_clean_skill(piece))

    lowered = text.lower()
    for known in vocabulary or []:
        if re.search(rf"(?<![\w+#]){re.escape(known.lower())}(?![\w+#])", lowered):
            add(known)

    return found


def _month_year(token: str) -> tuple[int, int] | None:
    token = token.strip().lower().replace(".", "").replace(",", "").replace("'", "")
    if re.fullmatch(r"\d{4}", token):
        return 1, int(token)
    slash = re.fullmatch(r"(\d{1,2})/(\d{4})", token)
    if slash:
        return int(slash.group(1)), int(slash.group(2))
    named = re.match(r"([a-z]{3})[a-z]*\s*(\d{2,4})", token)
    if named and named.group(1) in MONTHS:
        year = int(named.group(2))
        if year < 100:
            year += 2000 if year < 70 else 1900
        return MONTHS[named.group(1)], year
    return None


def extract_years(text: str) -> float | None:
    """Total years of experience.

    A stated "6+ years of experience" is trusted over anything computed: it is
    what the candidate claims and what a recruiter reads. Only when the resume
    states nothing do we add up the date ranges, which is the fragile path -
    overlapping roles and undated side projects both inflate it.
    """
    header = text[:1200]
    for match in STATED_YEARS.finditer(header):
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        if 0 < value <= 50:
            return round(value, 2)

    today = date.today()
    spans: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for match in DATE_RANGE.finditer(text):
        start = _month_year(match.group("from"))
        end_raw = match.group("to").strip().lower()
        if re.match(r"present|current|now|till\s*date|ongoing", end_raw):
            end = (today.month, today.year)
        else:
            end = _month_year(end_raw)
        if not start or not end:
            continue
        if (end[1], end[0]) < (start[1], start[0]):
            continue
        if not (1970 <= start[1] <= today.year and 1970 <= end[1] <= today.year + 1):
            continue
        spans.append((start, end))

    if not spans:
        return None

    # Merge overlaps so two concurrent roles are not counted twice.
    ordered = sorted(spans, key=lambda s: (s[0][1], s[0][0]))
    merged: list[list[tuple[int, int]]] = []
    for start, end in ordered:
        if merged and (start[1], start[0]) <= (merged[-1][1][1], merged[-1][1][0]):
            if (end[1], end[0]) > (merged[-1][1][1], merged[-1][1][0]):
                merged[-1][1] = end
        else:
            merged.append([start, end])

    months = sum((e[1] - s[1]) * 12 + (e[0] - s[0]) for s, e in merged)
    return round(months / 12, 2) if months > 0 else None


def extract_skill_years(text: str) -> dict[str, float]:
    """Per-skill years where the resume states them outright."""
    out: dict[str, float] = {}
    for match in SKILL_YEARS.finditer(text):
        skill = _clean_skill(match.group(1))
        if not skill:
            continue
        try:
            years = float(match.group(2))
        except ValueError:
            continue
        if 0 < years <= 50:
            key = skill.lower()
            out[key] = max(out.get(key, 0.0), years)
    return out


def extract_titles(text: str) -> list[str]:
    """Job titles, most recent first, from the lines around each date range."""
    titles: list[str] = []
    seen: set[str] = set()
    for match in DATE_RANGE.finditer(text):
        window = text[max(0, match.start() - 200):match.start() + 120]
        for line in window.splitlines():
            line = line.strip(" \t-•|")
            if not (3 < len(line) < 70):
                continue
            if DATE_RANGE.search(line) and len(line) < 30:
                continue
            if not re.search(
                r"\b(engineer|developer|analyst|lead|manager|architect|consultant|"
                r"specialist|administrator|designer|scientist|tester|qa|sdet|intern|"
                r"associate|executive|officer|head|director|principal|staff|senior|"
                r"junior|sr|jr)\b", line, re.IGNORECASE):
                continue
            cleaned = re.sub(r"\s{2,}", " ", line)
            key = cleaned.lower()
            if key not in seen:
                seen.add(key)
                titles.append(cleaned)
    return titles[:8]


def extract_location(text: str) -> str | None:
    header = text[:900]
    for city in CITIES:
        if re.search(rf"\b{re.escape(city)}\b", header, re.IGNORECASE):
            return city
    return None


def extract_contact(text: str) -> dict:
    """Contact details - found so they can be *excluded* from scoring text.

    A phone number or a personal domain in the profile blob would otherwise
    match against job descriptions as if it were a skill.
    """
    header = text[:900]
    return {
        "emails": sorted(set(EMAIL.findall(header))),
        "urls": sorted(set(URL.findall(header))),
        "phones": sorted({p.strip() for p in PHONE.findall(header) if len(re.sub(r"\D", "", p)) >= 10}),
    }


def redact(text: str) -> str:
    """Strip contact details out of resume text before it is used for matching."""
    text = EMAIL.sub(" ", text)
    text = URL.sub(" ", text)
    return PHONE.sub(" ", text)


# --- top level ----------------------------------------------------------

def find_resume(directory: Path = RESUME_DIR) -> Path | None:
    """The newest supported resume file in the resume directory."""
    if not directory.exists():
        return None
    candidates = [p for p in directory.iterdir()
                  if p.is_file() and p.suffix.lower() in SUPPORTED and not p.name.startswith(("~", "."))]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def parse(path: Path | None = None, vocabulary: list[str] | None = None) -> dict:
    """Parse a resume into the fact shape the config layer consumes."""
    path = path or find_resume()
    if path is None:
        raise ResumeError(
            f"No resume found in {RESUME_DIR}.\n"
            f"  Drop a .pdf, .docx or .txt there and re-run, or pass --resume <path>."
        )
    if not path.exists():
        raise ResumeError(f"No such file: {path}")

    text = read_text(path)
    body = redact(text)

    facts = {
        "source_file": path.name,
        "skills": extract_skills(body, vocabulary),
        "years": extract_years(body),
        "skill_years": extract_skill_years(body),
        "titles": extract_titles(body),
        "location": extract_location(body),
        "contact": extract_contact(text),
        "text": re.sub(r"\s+", " ", body).strip()[:20000],
    }
    log.info("Parsed %s: %d skills, %s years, %d titles",
             path.name, len(facts["skills"]),
             facts["years"] if facts["years"] is not None else "unknown",
             len(facts["titles"]))
    return facts


def save(facts: dict, path: Path = RESUME_JSON) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(facts, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load(path: Path = RESUME_JSON) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("resume.json unreadable (%s); ignoring", exc)
        return None


def summarise(facts: dict) -> str:
    years = facts.get("years")
    lines = [
        f"  Resume:    {facts.get('source_file')}",
        f"  Experience: {years:g} years" if years is not None else
        "  Experience: not stated - set `years:` in config.yaml",
        f"  Location:   {facts.get('location') or 'not found'}",
        f"  Skills:     {len(facts.get('skills') or [])} found",
    ]
    skills = facts.get("skills") or []
    if skills:
        lines.append("              " + ", ".join(skills[:14]) + ("..." if len(skills) > 14 else ""))
    titles = facts.get("titles") or []
    if titles:
        lines.append(f"  Titles:     {titles[0]}")
    lines.append("")
    lines.append(f"  Written to {RESUME_JSON}")
    lines.append("  Read it - anything wrong there, fix it by hand and it stays fixed.")
    return "\n".join(lines)
