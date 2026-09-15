"""Tests for the parts that decide which jobs you see.

The scoring and parsing paths are the ones worth pinning down: a browser bug
shows up as an empty run you notice immediately, but a scoring bug quietly
ranks the wrong jobs first and looks like it worked.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screener import config as config_mod
from screener import edit as edit_mod
from screener import itskills as itskills_mod
from screener import match as match_mod
from screener import notify as notify_mod
from screener import page as page_mod
from screener import projects as projects_mod
from screener import resume as resume_mod
from screener import scan as scan_mod
from screener import schedule as schedule_mod
from screener import score as score_mod
from screener import search as search_mod
from screener import session as session_mod
from screener.model import Job


def make_job(**kwargs) -> Job:
    defaults = dict(
        job_id="1", title="Data Engineer", company="Acme",
        url="https://naukri.com/job/1", skills=["Python", "Spark", "Airflow"],
        location="Bengaluru", min_exp=5.0, max_exp=9.0, description="",
        created_ms=None,
    )
    defaults.update(kwargs)
    return Job(**defaults)


BASE_CONFIG = {
    "profile_skills": ["Python", "Spark", "Airflow", "SQL"],
    "profile_years": 7.0,
    "profile_text": "Senior Data Engineer",
    "preferred_locations": ["Bengaluru", "Remote"],
    "must_have_any": ["data engineer", "etl"],
    "titles": ["Senior Data Engineer"],
    "synonyms": {},
    "weights": {},
}


# --- scoring ------------------------------------------------------------

class TestScoring:
    def test_strong_match_scores_high(self):
        job = make_job(title="Senior Data Engineer", description="ETL pipelines")
        breakdown = score_mod.score(job, BASE_CONFIG)
        assert "rejected" not in breakdown
        assert job.score > 70, f"expected a strong match, got {job.score} ({breakdown})"

    def test_unrelated_job_is_rejected_by_must_have(self):
        job = make_job(title="Sales Manager", skills=["Negotiation"],
                       description="Selling software")
        score_mod.score(job, BASE_CONFIG)
        assert job.score == 0.0
        assert "must_have_any" in job.score_breakdown["rejected"]

    def test_skill_overlap_moves_the_score(self):
        strong = make_job(job_id="a", skills=["Python", "Spark", "Airflow", "SQL"])
        weak = make_job(job_id="b", skills=["COBOL", "Fortran", "AS400", "RPG"])
        for job in (strong, weak):
            job.description = "data engineer role"
            score_mod.score(job, BASE_CONFIG)
        assert strong.score > weak.score

    def test_experience_gap_rejects(self):
        config = {**BASE_CONFIG, "max_experience_gap_years": 2.0}
        job = make_job(min_exp=12.0, description="data engineer")
        score_mod.score(job, config)
        assert job.score == 0.0
        assert "needs 12y" in job.score_breakdown["rejected"]

    def test_over_qualified_is_penalised_gently_not_rejected(self):
        job = make_job(min_exp=1.0, max_exp=3.0, description="data engineer")
        score_mod.score(job, BASE_CONFIG)
        assert job.score > 0, "over-qualified should still be scored, not dropped"

    def test_excluded_title_keyword(self):
        config = {**BASE_CONFIG, "exclude_title_keywords": ["intern"]}
        job = make_job(title="Data Engineer Intern", description="etl")
        score_mod.score(job, config)
        assert "intern" in job.score_breakdown["rejected"]

    def test_excluded_description_keyword(self):
        config = {**BASE_CONFIG, "exclude_description_keywords": ["service bond"]}
        job = make_job(description="data engineer. Requires a 2-year service bond.")
        score_mod.score(job, config)
        assert "service bond" in job.score_breakdown["rejected"]

    def test_excluded_company(self):
        config = {**BASE_CONFIG, "exclude_companies": ["Acme"]}
        job = make_job(description="etl")
        score_mod.score(job, config)
        assert "company excluded" in job.score_breakdown["rejected"]

    def test_remote_scores_full_location(self):
        remote = make_job(job_id="a", location="Remote", description="etl")
        elsewhere = make_job(job_id="b", location="Kolkata", description="etl")
        for job in (remote, elsewhere):
            score_mod.score(job, BASE_CONFIG)
        assert remote.score_breakdown["location"] > elsewhere.score_breakdown["location"]

    def test_salary_floor_rejects_below_but_keeps_unstated(self):
        config = {**BASE_CONFIG, "min_salary_lpa": 20}
        low = make_job(job_id="a", salary_label="8-12 Lacs PA", description="etl")
        unstated = make_job(job_id="b", salary_label=None, description="etl")
        score_mod.score(low, config)
        score_mod.score(unstated, config)
        assert low.score == 0.0
        assert unstated.score > 0, "a job with no stated salary must not be dropped"

    def test_salary_floor_keeps_a_range_that_reaches_it(self):
        config = {**BASE_CONFIG, "min_salary_lpa": 20}
        reaches = make_job(job_id="c", salary_label="15-25 Lacs PA", description="etl")
        score_mod.score(reaches, config)
        assert reaches.score > 0, "15-25 can pay 20, so it must not be dropped"

    def test_synonyms_fold_spellings_together(self):
        config = {**BASE_CONFIG, "profile_skills": ["extract transform load"],
                  "synonyms": {"etl": "extract transform load"}}
        job = make_job(skills=["ETL"], description="data engineer")
        score_mod.score(job, config)
        assert job.matched_skills, "ETL should have matched via the synonym"

    def test_weights_renormalise_to_100(self):
        config = {**BASE_CONFIG, "weights": {"skills": 90, "title": 10,
                                             "experience": 0, "location": 0, "freshness": 0}}
        job = make_job(title="Senior Data Engineer", description="etl")
        breakdown = score_mod.score(job, config)
        assert breakdown["location"] == 0.0
        assert breakdown["experience"] == 0.0
        assert job.score <= 100.0

    def test_zeroed_location_weight_ignores_city(self):
        config = {**BASE_CONFIG, "weights": {"skills": 45, "title": 25,
                                             "experience": 15, "location": 0, "freshness": 5}}
        far = make_job(location="Guwahati", description="etl")
        breakdown = score_mod.score(far, config)
        assert breakdown["location"] == 0.0

    def test_no_skill_tags_falls_back_to_description(self):
        config = {**BASE_CONFIG, "vocabulary": ["Spark", "Airflow", "Kafka"]}
        job = make_job(skills=[], description="Data engineer working with Spark and Airflow daily")
        score_mod.score(job, config)
        assert job.matched_skills, "should have read skills out of the description"

    def test_score_is_reproducible(self):
        a, b = make_job(description="etl"), make_job(description="etl")
        score_mod.score(a, BASE_CONFIG)
        score_mod.score(b, BASE_CONFIG)
        assert a.score == b.score


class TestTheFieldGate:
    """`must_have_any` is what makes a shortlist short. It has to actually bite."""

    def test_a_punctuated_gate_term_does_not_match_everything(self):
        """"c#" normalises to "c", which is a substring of almost any job.

        Left unanchored, one such term in the gate quietly admitted every
        posting on the board - an accounting job included.
        """
        config = {**BASE_CONFIG, "must_have_any": ["c#", "asp net"]}
        unrelated = make_job(title="Data Scientist", skills=["Python", "PyTorch"],
                             description="Train machine learning models.")
        assert score_mod.hard_reject(unrelated, config) is not None

    def test_the_punctuated_term_still_matches_its_own_job(self):
        config = {**BASE_CONFIG, "must_have_any": ["c#", "asp net"]}
        match = make_job(title="Dot Net Developer", skills=["C#", "ASP.NET Core"],
                         description="Build APIs in ASP.NET Core.")
        assert score_mod.hard_reject(match, config) is None

    def test_allowed_locations_drops_an_out_of_region_job(self):
        config = {**BASE_CONFIG, "allowed_locations": ["Pune", "Mumbai"]}
        job = make_job(title="Data Engineer", description="etl", location="Bengaluru")
        assert score_mod.hard_reject(job, config) is not None

    def test_allowed_locations_keeps_a_substring_match(self):
        """'Navi Mumbai' and 'Pune, Bengaluru' are both reachable from the region."""
        config = {**BASE_CONFIG, "allowed_locations": ["Pune", "Mumbai"]}
        for where in ("Navi Mumbai", "Pune, Bengaluru", "Mumbai Suburban"):
            job = make_job(title="Data Engineer", description="etl", location=where)
            assert score_mod.hard_reject(job, config) is None, where

    def test_a_job_with_no_stated_location_is_kept(self):
        """Naukri leaves this blank often; dropping them all loses good jobs."""
        config = {**BASE_CONFIG, "allowed_locations": ["Pune"]}
        job = make_job(title="Data Engineer", description="etl", location=None)
        assert score_mod.hard_reject(job, config) is None

    def test_no_allowed_locations_means_anywhere(self):
        config = {**BASE_CONFIG, "allowed_locations": []}
        job = make_job(title="Data Engineer", description="etl", location="Bengaluru")
        assert score_mod.hard_reject(job, config) is None

    def test_a_gate_term_is_not_matched_inside_a_longer_word(self):
        config = {**BASE_CONFIG, "must_have_any": ["net"]}
        job = make_job(title="Network Support Engineer", skills=["Cisco"],
                       description="Maintain network switches and routers.")
        assert score_mod.hard_reject(job, config) is not None, \
            "'net' matched inside 'network'"


# --- the universality claim --------------------------------------------

class TestRoleNeutrality:
    """The whole point of the fork: nothing may be hardcoded to one job family."""

    def test_no_role_specific_terms_in_the_scoring_module(self):
        source = (Path(__file__).resolve().parent.parent / "screener" / "score.py").read_text(encoding="utf-8")
        # These belong in profiles/*.yaml, never in Python.
        for term in ("sdet", "qa automation", "selenium", "playwright"):
            assert term not in source.lower(), f"{term!r} is hardcoded in score.py"

    def test_every_shipped_pack_loads(self):
        roles = config_mod.available_roles()
        assert roles, "no role packs shipped"
        for role in roles:
            pack = config_mod.load_pack(role)
            assert pack.get("description"), f"{role} has no description"
            assert pack.get("must_have_any"), f"{role} has no must_have_any gate"
            assert pack.get("vocabulary"), f"{role} has no vocabulary"

    def test_unknown_role_names_the_alternatives(self):
        with pytest.raises(config_mod.ConfigError) as exc:
            config_mod.load_pack("astronaut")
        assert "Available:" in str(exc.value)

    @pytest.mark.parametrize("role", ["backend-engineer", "data-engineer", "dotnet-fullstack"])
    def test_each_pack_produces_its_own_searches(self, role, tmp_path):
        facts = {"skills": ["Python"], "years": 5.0, "titles": [], "text": "", "skill_years": {}}
        config = config_mod.load(path=tmp_path / "missing.yaml",
                                 profile=None, resume_facts=facts)
        # No role set, so this falls through to the resume - the point of the
        # test is the pack below producing something different.
        assert config["searches"]

        pack = config_mod.load_pack(role)
        assert pack["searches"], f"{role} ships no searches"


# --- role detection -----------------------------------------------------

class TestRoleDetection:
    """The out-of-the-box path: a user who never opens config.yaml.

    Without detection they get searches built from bare skill names, and
    "Hibernate jobs in Hyderabad" is not a query any job board answers well.
    """

    RESUMES = {
        "backend-engineer": dict(
            skills=["Java", "Spring Boot", "Microservices", "Kafka", "PostgreSQL", "REST"],
            titles=["Software Engineer"],
            text="backend microservices api rest spring boot server side development"),
        "frontend-engineer": dict(
            skills=["React", "TypeScript", "Redux", "CSS", "Webpack", "Next js"],
            titles=["UI Developer"],
            text="frontend react ui developer responsive design web application"),
        "data-engineer": dict(
            skills=["Spark", "Airflow", "Kafka", "Snowflake", "dbt", "PySpark"],
            titles=["Senior Data Engineer"],
            text="data engineer etl pipeline big data warehouse batch streaming"),
        "dotnet-fullstack": dict(
            skills=["C#", "ASP.NET Core", "Entity Framework Core", "Angular", "SQL Server", "TypeScript"],
            titles=[".NET Developer"],
            text="dotnet full stack developer asp net core web api angular sql server"),
        # The hard one. A full stack resume is supposed to look half like
        # frontend and half like backend, so it can never win on margin - only
        # the job title separates it.
        "fullstack-developer": dict(
            skills=["React", "Node js", "MongoDB", "Express", "TypeScript", "Docker"],
            titles=["Full Stack Developer"],
            text="full stack mern web application frontend and backend"),
    }

    @pytest.mark.parametrize("expected", list(RESUMES))
    def test_each_field_is_detected(self, expected):
        got, scores = config_mod.detect_role(self.RESUMES[expected])
        assert got == expected, f"got {got}; scores {scores}"

    def test_the_winner_wins_clearly(self):
        """A narrow margin means a coin flip, which is worse than not guessing."""
        # fullstack is excluded by design: overlapping with its neighbours is
        # what full stack *is*, and the title tiebreak is what settles it.
        for expected, facts in self.RESUMES.items():
            if expected == "fullstack-developer":
                continue
            _, scores = config_mod.detect_role(facts)
            ranked = sorted(scores.values(), reverse=True)
            assert ranked[0] > ranked[1] * 1.5, f"{expected} was nearly a tie: {scores}"

    def test_fullstack_is_settled_by_its_title_not_its_margin(self):
        facts = self.RESUMES["fullstack-developer"]
        got, scores = config_mod.detect_role(facts)
        assert got == "fullstack-developer"
        ranked = sorted(scores.values(), reverse=True)
        assert ranked[0] < ranked[1] * 1.25, (
            "this test is meaningless unless it really is a near-tie")

    def test_a_vague_resume_is_not_guessed(self):
        got, _ = config_mod.detect_role(
            dict(skills=["Python", "Git", "Docker"], titles=["Engineer"],
                 text="software engineering"))
        assert got is None

    def test_an_unrelated_resume_is_not_guessed(self):
        got, _ = config_mod.detect_role(
            dict(skills=["Tally", "GST", "Excel"], titles=["Accountant"],
                 text="accounts payable ledger taxation audit"))
        assert got is None, "should decline rather than guess a wrong field"

    def test_an_empty_resume_is_not_guessed(self):
        got, _ = config_mod.detect_role(dict(skills=[], titles=[], text=""))
        assert got is None

    def test_a_hyphenated_title_matches_an_unhyphenated_pack(self):
        """"Full-Stack Developer" and "Full Stack Developer" are one title."""
        base = {**self.RESUMES["dotnet-fullstack"], "skill_years": {}}
        hyphenated = config_mod.detect_role({**base, "titles": ["Full-Stack Developer"]})[1]
        spaced = config_mod.detect_role({**base, "titles": ["Full Stack Developer"]})[1]
        assert hyphenated["dotnet-fullstack"] == spaced["dotnet-fullstack"], \
            "the hyphen changed the score"

    def test_detection_fills_the_gate(self, tmp_path):
        """A detected pack must actually reach the config, not just be logged."""
        facts = {**self.RESUMES["dotnet-fullstack"], "skill_years": {}}
        config = config_mod.load(path=tmp_path / "none.yaml", profile=None, resume_facts=facts)
        assert config["role"] == "dotnet-fullstack"
        assert config["role_detected"] == "dotnet-fullstack"
        assert config["must_have_any"], "a detected pack should supply the field gate"

    def test_an_explicit_role_is_never_overridden(self, tmp_path):
        # A resume detection would decline, so only the explicit role can set it.
        path = tmp_path / "config.yaml"
        path.write_text("role: dotnet-fullstack\n", encoding="utf-8")
        facts = dict(skills=["Tally", "GST", "Excel"], titles=["Accountant"],
                     text="accounts payable ledger taxation audit", skill_years={})
        config = config_mod.load(path=path, profile=None, resume_facts=facts)
        assert config["role"] == "dotnet-fullstack", "the user's choice must win"
        assert config["role_detected"] is None


class TestScaffoldPack:
    """The escape hatch for a field with no pack."""

    FACTS = {"skills": ["Kotlin", "Jetpack Compose", "Retrofit"],
             "titles": ["Senior Android Developer"], "text": "android developer"}

    def test_scaffold_is_valid_yaml_with_the_required_keys(self, tmp_path):
        path = config_mod.scaffold_pack("Android Developer", self.FACTS, tmp_path)
        pack = config_mod.load_pack(path.stem, tmp_path)
        for key in ("description", "searches", "must_have_any", "vocabulary"):
            assert pack.get(key), f"scaffold produced no {key}"

    def test_scaffold_seeds_from_the_resume(self, tmp_path):
        path = config_mod.scaffold_pack("Android Developer", self.FACTS, tmp_path)
        pack = config_mod.load_pack(path.stem, tmp_path)
        assert "Kotlin" in pack["vocabulary"]
        assert any("Android Developer" in s for s in pack["searches"])

    def test_scaffold_searches_are_titles_not_bare_skills(self, tmp_path):
        path = config_mod.scaffold_pack("Android Developer", self.FACTS, tmp_path)
        pack = config_mod.load_pack(path.stem, tmp_path)
        assert "Kotlin" not in pack["searches"], "a bare skill is not a job title"

    def test_a_scaffolded_pack_becomes_detectable(self, tmp_path):
        config_mod.scaffold_pack("Android Developer", self.FACTS, tmp_path)
        got, _ = config_mod.detect_role(self.FACTS, tmp_path)
        assert got == "android-developer", "the loop should close: scaffold then detect"

    def test_scaffold_works_with_no_resume(self, tmp_path):
        path = config_mod.scaffold_pack("Embedded Firmware", None, tmp_path)
        pack = config_mod.load_pack(path.stem, tmp_path)
        assert pack.get("description")

    def test_scaffold_refuses_to_overwrite(self, tmp_path):
        config_mod.scaffold_pack("Android Developer", self.FACTS, tmp_path)
        with pytest.raises(config_mod.ConfigError) as exc:
            config_mod.scaffold_pack("Android Developer", self.FACTS, tmp_path)
        assert "already exists" in str(exc.value)

    def test_role_name_is_slugified(self, tmp_path):
        path = config_mod.scaffold_pack("Android / iOS Developer!", self.FACTS, tmp_path)
        assert path.stem == "android-ios-developer"


class TestDerivedSearches:
    """Searches are what the whole run rests on - a bad query returns nothing."""

    FACTS = {"skills": ["Java", "Spring Boot", "Hibernate"], "years": 4.0,
             "titles": ["Software Engineer"], "skill_years": {},
             "text": "backend microservices api spring boot", "location": "Hyderabad"}

    def _searches(self, tmp_path, **overrides):
        facts = {**self.FACTS, **overrides}
        config = config_mod.load(path=tmp_path / "none.yaml", profile=None, resume_facts=facts)
        return [s["keyword"] for s in config["searches"]]

    def test_no_bare_skill_names(self, tmp_path):
        """'Hibernate' is a library, not a job title."""
        keywords = self._searches(tmp_path)
        for bare in ("Java", "Hibernate", "Spring Boot"):
            assert bare not in keywords, f"{bare!r} is not a searchable job title"

    def test_skills_are_paired_with_a_role_noun(self, tmp_path):
        keywords = self._searches(tmp_path, titles=[], text="")
        assert any(k.endswith(("Developer", "Engineer")) for k in keywords), keywords

    def test_too_broad_terms_are_dropped(self, tmp_path):
        keywords = [k.lower() for k in self._searches(tmp_path)]
        assert "software" not in keywords
        assert "it" not in keywords

    def test_searches_carry_the_location(self, tmp_path):
        config = config_mod.load(path=tmp_path / "none.yaml", profile=None,
                                 resume_facts=self.FACTS)
        assert all(s["location"] == "Hyderabad" for s in config["searches"])

    def test_seniority_is_stripped_from_titles(self, tmp_path):
        keywords = self._searches(tmp_path, titles=["Senior Data Engineer"],
                                  skills=["Spark"], text="data engineer etl pipeline")
        assert not any(k.lower().startswith(("senior", "sr ")) for k in keywords), keywords

    def test_capped_at_five(self, tmp_path):
        keywords = self._searches(tmp_path, skills=["A", "B", "C", "D", "E", "F", "G", "H"])
        assert len(keywords) <= 5, "each search is a real navigation - do not run ten"

    def test_a_hyphenated_title_is_not_cut_at_its_hyphen(self):
        """Cutting at any dash turned "Full-Stack Developer" into "Full"."""
        assert config_mod._clean_title("Full-Stack Software Developer") == \
            "Full-Stack Software Developer"
        assert config_mod._clean_title("Sr. Full-Stack Developer") == "Full-Stack Developer"

    def test_a_spaced_dash_still_ends_a_title(self):
        assert config_mod._clean_title("Software Developer - Backend") == "Software Developer"
        assert config_mod._clean_title("Software Developer — Payments") == "Software Developer"


# --- resume parsing -----------------------------------------------------

class TestResume:
    SAMPLE = """
    Priya Nair
    Bengaluru, India | priya@example.com | +91 98765 43210

    SUMMARY
    Data Engineer with 7+ years of experience.

    TECHNICAL SKILLS
    Python, Spark, Airflow, Kafka, Snowflake

    EXPERIENCE
    Senior Data Engineer
    Acme, Bengaluru
    Mar 2021 - Present

    Data Engineer
    Globex, Pune
    Jun 2018 - Feb 2021
    """

    def test_stated_years_win(self):
        assert resume_mod.extract_years(self.SAMPLE) == 7.0

    def test_computed_years_when_none_stated(self):
        text = "Engineer\nAcme\nJan 2019 - Jan 2023\n"
        years = resume_mod.extract_years(text)
        assert years is not None and 3.5 < years < 4.5

    def test_overlapping_roles_are_not_double_counted(self):
        text = "Role A\nJan 2019 - Jan 2023\nRole B\nJan 2020 - Jan 2022\n"
        years = resume_mod.extract_years(text)
        assert years is not None and years < 5, f"overlap double-counted: {years}"

    def test_skills_from_the_skills_section(self):
        skills = [s.lower() for s in resume_mod.extract_skills(self.SAMPLE)]
        assert "python" in skills and "spark" in skills

    def test_vocabulary_catches_skills_outside_the_section(self):
        text = "EXPERIENCE\nBuilt pipelines with Terraform and Kubernetes daily."
        skills = [s.lower() for s in resume_mod.extract_skills(text, ["Terraform", "Kubernetes"])]
        assert "terraform" in skills and "kubernetes" in skills

    def test_location_found(self):
        assert resume_mod.extract_location(self.SAMPLE) == "Bengaluru"

    def test_titles_found(self):
        titles = resume_mod.extract_titles(self.SAMPLE)
        assert any("data engineer" in t.lower() for t in titles)

    def test_per_skill_years(self):
        years = resume_mod.extract_skill_years("Airflow - 5 years, Spark: 6 years")
        assert years.get("airflow") == 5.0
        assert years.get("spark") == 6.0

    def test_contact_details_are_stripped_before_matching(self):
        redacted = resume_mod.redact(self.SAMPLE)
        assert "priya@example.com" not in redacted
        assert "98765" not in redacted

    def test_noise_words_are_not_skills(self):
        skills = [s.lower() for s in resume_mod.extract_skills(
            "SKILLS\nStrong knowledge of, hands-on experience with, and\n")]
        assert not any(s in ("strong", "knowledge", "and", "with") for s in skills)

    def test_unsupported_format_is_a_clear_error(self, tmp_path):
        bad = tmp_path / "cv.pages"
        bad.write_text("x", encoding="utf-8")
        with pytest.raises(resume_mod.ResumeError) as exc:
            resume_mod.read_text(bad)
        assert "Unsupported" in str(exc.value)

    def test_old_doc_format_says_what_to_do(self, tmp_path):
        bad = tmp_path / "cv.doc"
        bad.write_text("x", encoding="utf-8")
        with pytest.raises(resume_mod.ResumeError) as exc:
            resume_mod.read_text(bad)
        assert "Save As" in str(exc.value)

    def test_empty_resume_is_a_clear_error(self, tmp_path):
        empty = tmp_path / "cv.txt"
        empty.write_text("   \n  ", encoding="utf-8")
        with pytest.raises(resume_mod.ResumeError) as exc:
            resume_mod.read_text(empty)
        assert "no text" in str(exc.value)


class TestCategorisedResume:
    """A skills section laid out as labelled rows, which is the common shape.

    Every assertion here is a bug that shipped: this layout parsed to zero
    skills, because "Languages:" is both how a resume labels a row of a skills
    table and how it titles the section about French and Hindi.
    """

    SAMPLE = """
JANE EXAMPLE
Full-Stack Software Developer | .NET - Angular - Cross-Platform
someone@example.com

PROFESSIONAL SUMMARY
Full-stack engineer with 2+ years building enterprise systems.

TECHNICAL SKILLS
Languages: C#, Java, TypeScript, SQL, Dart
Frameworks & Libraries: ASP.NET Core (.NET 6), Angular 17, Flutter
Data & Search: SQL Server, PostgreSQL, Milvus (vector / similarity search)

PROFESSIONAL EXPERIENCE
I2V Systems Pvt. Ltd. — Software Developer Dec 2023 – Present
• Optimized a PostgreSQL query and tuned database indexes.

EDUCATION
B.E., Mumbai University — CGPA 7.02 2021
"""

    def skills(self):
        return [s.lower() for s in resume_mod.extract_skills(self.SAMPLE)]

    def test_a_labelled_row_does_not_end_the_skills_section(self):
        assert "c#" in self.skills(), "'Languages:' was read as a section heading"

    def test_every_labelled_row_is_read(self):
        found = self.skills()
        for skill in ("c#", "typescript", "angular 17", "flutter", "postgresql"):
            assert skill in found, f"{skill} missing - a later row was dropped"

    def test_the_row_label_is_not_itself_a_skill(self):
        found = self.skills()
        assert not any(s.startswith(("languages", "frameworks", "data &"))
                       for s in found), f"a row label leaked in: {found}"

    def test_a_qualified_heading_ends_the_section(self):
        """'PROFESSIONAL EXPERIENCE' is the same heading as 'EXPERIENCE'."""
        found = self.skills()
        assert not any("optimized" in s or "indexes" in s for s in found), \
            f"the section ran on into the job bullets: {found}"

    def test_a_parenthetical_is_not_split_into_half_skills(self):
        found = self.skills()
        assert "milvus" in found
        assert "similarity search" not in found

    def test_the_title_is_separated_from_the_company_and_the_dates(self):
        titles = resume_mod.extract_titles(self.SAMPLE)
        assert "Software Developer" in titles, titles
        assert not any("I2V" in t or "2023" in t for t in titles), \
            f"company or dates left on the title: {titles}"

    def test_the_headline_is_kept_as_a_title(self):
        titles = resume_mod.extract_titles(self.SAMPLE)
        assert any("full-stack" in t.lower() for t in titles), titles

    def test_a_held_job_outranks_the_headline(self):
        titles = resume_mod.extract_titles(self.SAMPLE)
        assert titles[0] == "Software Developer"

    def test_location_falls_back_to_the_body(self):
        """No address in the header, but the university names the city."""
        assert resume_mod.extract_location(self.SAMPLE) == "Mumbai"

    def test_the_header_still_wins_when_it_has_an_address(self):
        text = "Priya Nair\nPune, India\n\nEDUCATION\nMumbai University Mumbai Mumbai"
        assert resume_mod.extract_location(text) == "Pune"


# --- config layering ----------------------------------------------------

class TestConfig:
    FACTS = {"skills": ["Python", "Spark"], "years": 7.0,
             "titles": ["Senior Data Engineer"], "text": "data engineer",
             "skill_years": {"spark": 6.0}, "location": "Pune"}

    def test_resume_alone_is_enough(self, tmp_path):
        config = config_mod.load(path=tmp_path / "none.yaml", profile=None,
                                 resume_facts=self.FACTS)
        assert config["profile_years"] == 7.0
        assert config["searches"], "should have derived searches from the resume"

    def test_nothing_at_all_is_a_helpful_error(self, tmp_path):
        with pytest.raises(config_mod.ConfigError) as exc:
            config_mod.load(path=tmp_path / "none.yaml", profile=None, resume_facts=None)
        assert "--resume" in str(exc.value)

    def test_user_config_overrides_the_resume(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("years: 12\nsource: local\n", encoding="utf-8")
        config = config_mod.load(path=path, profile=None, resume_facts=self.FACTS)
        assert config["profile_years"] == 12.0

    def test_profile_it_skills_beat_resume_skill_years(self, tmp_path):
        profile = {"key_skills": ["Spark"], "it_skills": ["Spark - 2025 3 Years 0 Months"],
                   "experience": "7 Years 0 Months"}
        config = config_mod.load(path=tmp_path / "none.yaml", profile=profile,
                                 resume_facts=self.FACTS)
        assert config["skill_years"]["spark"] == 3.0, "the dated profile table should win"

    def test_thresholds_must_be_consistent(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("auto_apply_min_score: 40\nreview_min_score: 80\n", encoding="utf-8")
        with pytest.raises(config_mod.ConfigError) as exc:
            config_mod.load(path=path, profile=None, resume_facts=self.FACTS)
        assert "must be >=" in str(exc.value)

    def test_apify_without_a_token_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.delenv("APIFY_TOKEN", raising=False)
        path = tmp_path / "config.yaml"
        path.write_text("source: apify\n", encoding="utf-8")
        with pytest.raises(config_mod.ConfigError) as exc:
            config_mod.load(path=path, profile=None, resume_facts=self.FACTS)
        assert "APIFY_TOKEN" in str(exc.value)

    def test_unknown_source_is_refused(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("source: carrier-pigeon\n", encoding="utf-8")
        with pytest.raises(config_mod.ConfigError):
            config_mod.load(path=path, profile=None, resume_facts=self.FACTS)

    def test_role_pack_gate_reaches_the_config(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("role: dotnet-fullstack\n", encoding="utf-8")
        config = config_mod.load(path=path, profile=None, resume_facts=self.FACTS)
        assert "dotnet" in [m.lower() for m in config["must_have_any"]]

    def test_locations_default_to_resume_city_plus_remote(self, tmp_path):
        config = config_mod.load(path=tmp_path / "none.yaml", profile=None,
                                 resume_facts=self.FACTS)
        assert "Pune" in config["preferred_locations"]
        assert "Remote" in config["preferred_locations"]


# --- privacy ------------------------------------------------------------

class TestProfileRows:
    """IT-skill and project rows are refused before a browser opens, not after."""

    def test_experience_labels_match_the_dropdowns(self):
        assert [itskills_mod.years_label(n) for n in (0, 1, 4)] == ["0 Year", "1 Year", "4 Years"]
        assert [itskills_mod.months_label(n) for n in (0, 1, 11)] == ["0 Month", "1 Month", "11 Months"]

    @pytest.mark.parametrize("row", [("", 2026, None, None), ("C#", 2099, None, None),
                                     ("C#", 2026, 31, 0), ("C#", 2026, 4, 12),
                                     ("C#", 2026, None, 3)])
    def test_rows_the_dialog_cannot_hold_are_refused(self, row):
        with pytest.raises(edit_mod.EditError):
            itskills_mod.check(*row)

    def test_a_blank_duration_is_allowed(self):
        itskills_mod.check("Docker", 2026, None, None)

    def test_listed_matches_whole_cells_not_substrings(self):
        lines = ["IT skills", "PostgreSQL", "c#"]
        assert not itskills_mod.listed("SQL", lines)
        assert itskills_mod.listed("C#", lines)

    def test_a_bad_project_month_is_refused_before_touching_the_page(self):
        with pytest.raises(edit_mod.EditError):
            projects_mod.add(None, "T", "details", "Foo", "2026")

    def test_a_finished_project_needs_its_end_year(self):
        with pytest.raises(edit_mod.EditError):
            projects_mod.add(None, "T", "details", "Aug", "2026", end_month="Aug")


class TestSchedule:
    def test_times_are_normalised_sorted_and_deduplicated(self):
        assert schedule_mod.parse_times("18:30, 9:00,09:00") == ["09:00", "18:30"]

    @pytest.mark.parametrize("bad", ["25:00", "9am", "", "09:00,noon"])
    def test_anything_but_a_clock_time_is_refused(self, bad):
        with pytest.raises(schedule_mod.ScheduleError):
            schedule_mod.parse_times(bad)

    def test_timer_fires_at_every_time_and_catches_up_after_sleep(self):
        unit = schedule_mod.timer_unit(["09:00", "18:30"])
        assert "OnCalendar=*-*-* 09:00:00" in unit and "OnCalendar=*-*-* 18:30:00" in unit
        assert "Persistent=true" in unit

    def test_service_runs_a_notifying_scan_from_the_repo(self):
        unit = schedule_mod.service_unit(Path("/repo"), "/repo/.venv/bin/python")
        assert "WorkingDirectory=/repo" in unit
        assert '"/repo/main.py" --scan --notify' in unit


class TestNotify:
    def test_missing_notify_send_is_a_quiet_no_op(self, monkeypatch):
        monkeypatch.setattr(notify_mod.shutil, "which", lambda name: None)
        monkeypatch.setattr(notify_mod.subprocess, "run",
                            lambda *a, **k: pytest.fail("no notifier, so nothing may run"))
        notify_mod.send("title", "body")

    def test_headline_counts_and_leads_with_the_best_job(self):
        summary = {"shortlist": [{"score": 85.4, "title": "Dot Net Developer", "company": "Acme"}],
                   "review": [{"score": 60.0, "title": "C# Developer", "company": "Globex"}]}
        title, body = scan_mod.headline(summary, {"html": Path("openings-r1.html")})
        assert title == "Naukri scan: 1 shortlisted, 1 worth a read"
        assert body.splitlines()[0] == "85.4  Dot Net Developer - Acme"
        assert body.splitlines()[-1] == "openings-r1.html"

    def test_headline_says_so_when_nothing_is_new(self):
        title, _ = scan_mod.headline({"shortlist": [], "review": []})
        assert title == "Naukri scan: no new matches"

    def test_failed_searches_are_named_not_hidden(self):
        summary = {"shortlist": [], "review": [], "failed_searches": ["C# Developer in Gurugram"]}
        title, body = scan_mod.headline(summary)
        assert title == "Naukri scan: no new matches - 1 search failed"
        assert "C# Developer in Gurugram" in body


class TestClosedPage:
    """A page that closes mid-scan must not silently fail every search after it."""

    def test_a_closed_page_is_replaced_from_the_same_session(self):
        fresh = SimpleNamespace(is_closed=lambda: False)
        closed = SimpleNamespace(is_closed=lambda: True,
                                 context=SimpleNamespace(new_page=lambda: fresh))
        assert search_mod._live(closed) is fresh

    def test_an_open_page_is_kept(self):
        page = SimpleNamespace(is_closed=lambda: False)
        assert search_mod._live(page) is page


class TestRunPages:
    """A second scan the same day adds a page. It must not overwrite the first."""

    DAY = "2026-09-15"
    RESULTS = {"shortlist": [{"job_id": "1", "title": "A </script> B", "company": "Acme",
                              "url": "https://www.naukri.com/job-listings-a-1", "score": 80.0}],
               "review": []}

    @pytest.fixture(autouse=True)
    def jobs_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(page_mod, "JOBS_DIR", tmp_path)
        monkeypatch.setattr(page_mod, "SEEN_PATH", tmp_path / "seen.json")
        return tmp_path

    def test_each_scan_keeps_its_own_page_and_links_the_others(self):
        first = page_mod.build(self.RESULTS, today=self.DAY)
        second = page_mod.build(self.RESULTS, today=self.DAY)
        assert (first.name, second.name) == (f"openings-{self.DAY}-r1.html",
                                             f"openings-{self.DAY}-r2.html")
        assert f'href="{second.name}"' in first.read_text(encoding="utf-8"), "r1 must now link r2"

    def test_a_scratch_rebuild_claims_no_slot(self, jobs_dir):
        page_mod.build(self.RESULTS, out_path=jobs_dir / "scratch.html", today=self.DAY)
        assert page_mod.next_run(self.DAY) == 1

    def test_markup_in_a_title_cannot_close_the_data_block(self):
        text = page_mod.build(self.RESULTS, today=self.DAY).read_text(encoding="utf-8")
        assert "A </script> B" not in text
        assert "A \\u003c/script> B" in text


class TestScoringSplit:
    """The resume body proves skills. It does not decide which titles are yours."""

    def test_resume_body_is_evidence_not_identity(self, tmp_path):
        facts = {"skills": ["C#"], "titles": ["Software Engineer"], "years": 4.0,
                 "text": "Built RabbitMQ pipelines in C#"}
        config = config_mod.load(tmp_path / "none.yaml", profile=None, resume_facts=facts)
        assert config["profile_text"] == ""
        assert "RabbitMQ pipelines" in config["profile_evidence"]

    def test_a_skill_named_only_in_the_resume_body_still_counts(self):
        config = {**BASE_CONFIG, "profile_skills": [], "profile_text": "",
                  "profile_evidence": "Scaled RabbitMQ consumers"}
        assert score_mod.matched_skills(["RabbitMQ"], config) == ["RabbitMQ"]

    def test_title_words_found_only_in_the_resume_body_do_not_score(self):
        job = make_job(title="Data Engineer")
        narrow = {**BASE_CONFIG, "must_have_any": ["c#"], "titles": ["Software Engineer"],
                  "profile_text": "", "profile_evidence": "data pipelines"}
        wide = {**narrow, "profile_text": "data pipelines"}
        assert score_mod._title_score(job, narrow, 25) < score_mod._title_score(job, wide, 25)

    def test_filler_tags_are_not_counted_as_matches(self):
        config = {**BASE_CONFIG, "profile_skills": ["C#"], "profile_text": "software development",
                  "must_have_any": ["c#"]}
        job = make_job(skills=["Development", "Software", "C#"], description="c#")
        score_mod.score(job, config)
        assert job.matched_skills == ["C#"]


class TestHiddenBrowser:
    """Naukri blocks true headless, so hiding the window must never mean it."""

    def test_the_browser_is_always_headed(self):
        calls: list[dict] = []
        fake = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kw: calls.append(kw)))
        session_mod.launch(fake)
        assert calls == [{"headless": False}]

    def test_hiding_minimizes_the_window(self):
        sent: list[tuple] = []

        def send(method, params=None):
            sent.append((method, params))
            return {"windowId": 7}

        page = SimpleNamespace(context=SimpleNamespace(
            new_cdp_session=lambda pg: SimpleNamespace(send=send)))
        session_mod.minimize(page)
        assert sent[-1] == ("Browser.setWindowBounds",
                            {"windowId": 7, "bounds": {"windowState": "minimized"}})


class TestSkillSuggestions:
    """Adding a key skill must click the suggestion that IS the skill."""

    def test_first_suggestion_is_not_taken_on_trust(self):
        # Naukri's real dropdown for "Azure" - there is no plain "Azure" in it.
        options = ["Azure Data Factory", "Azure Active Directory", "Azure DevOps"]
        assert edit_mod._exact_index(options, "Azure") is None

    def test_exact_match_is_found_wherever_it_sits(self):
        options = ["Angularjs", "Angular Material", " angular "]
        assert edit_mod._exact_index(options, "Angular") == 2


class TestSymbolLanguages:
    """C#, C++ and C are three languages. Stripping punctuation made them one."""

    def test_csharp_resume_does_not_cover_c_or_cplusplus(self):
        config = {**BASE_CONFIG, "profile_skills": ["C#"], "profile_text": ""}
        assert score_mod.matched_skills(["C++", "C"], config) == []

    def test_csharp_still_matches_its_own_spellings(self):
        config = {**BASE_CONFIG, "profile_skills": ["C#"], "profile_text": "",
                  "synonyms": {"c sharp": "c#", "csharp": "c#"}}
        wanted = ["C#", "c sharp", "CSharp"]
        assert score_mod.matched_skills(wanted, config) == wanted


# --- one opening (--match) ----------------------------------------------

JOB_PAGE = {
    "jobId": "123456789012",
    "title": "Dot Net Developer",
    "staticUrl": "https://www.naukri.com/job-listings-dot-net-developer-acme-pune-3-to-6-years-123456789012",
    "companyDetail": {"name": "Acme"},
    "minimumExperience": 3,
    "maximumExperience": 6,
    "locations": [{"label": "Pune"}, {"label": "Remote"}],
    "salaryDetail": {"label": "Not Disclosed"},
    "createdDate": "2026-09-07 17:02:05",
    "description": "<p>Build APIs in <b>C#</b></p>",
    "keySkills": {"preferred": [{"label": "C#"}, {"label": "Entity Framework"}],
                  "other": [{"label": "Azure"}]},
}

MATCH_CONFIG = {**BASE_CONFIG, "profile_skills": ["C#", "EF Core", "SQL"], "profile_text": "",
                "must_have_any": ["c#"], "titles": ["Software Engineer"],
                "synonyms": {"entity framework": "ef core"}}


class TestMatch:
    def test_job_id_is_read_off_the_url(self):
        url = JOB_PAGE["staticUrl"] + "?src=jobsearchDesk"
        assert match_mod.job_id_from_url(url) == "123456789012"

    def test_a_url_that_is_not_a_posting_is_refused(self):
        with pytest.raises(match_mod.MatchError):
            match_mod.job_id_from_url("https://www.naukri.com/mnjuser/profile")

    def test_job_page_payload_becomes_a_job(self):
        job = Job.from_detail(JOB_PAGE)
        assert job.skills == ["C#", "Entity Framework", "Azure"], "must-haves first"
        assert (job.company, job.location) == ("Acme", "Pune, Remote")
        assert (job.min_exp, job.max_exp) == (3.0, 6.0)
        assert job.description == "Build APIs in C#"
        # Stamped in IST: 17:02 there is 11:32 UTC.
        utc = datetime(2026, 9, 7, 11, 32, 5, tzinfo=timezone.utc)
        assert job.created_ms == int(utc.timestamp() * 1000)

    def test_skills_split_into_have_and_missing(self):
        result = match_mod.compare(JOB_PAGE, None, MATCH_CONFIG)
        assert result["have"] == ["C#", "Entity Framework"], "EF Core covers it via the synonym"
        assert result["missing"] == ["Azure"]
        assert result["preferred"] == {"C#", "Entity Framework"}

    def test_only_skills_the_cv_backs_are_offered_for_the_profile(self):
        naukri = {"skillMismatch": "C#,Kubernetes,Entity Framework", "Keyskills": 0}
        result = match_mod.compare(JOB_PAGE, naukri, MATCH_CONFIG)
        assert result["fixable"] == ["C#", "Entity Framework"]
        assert result["gaps"] == ["Kubernetes"], "a skill you lack must never be added"

    def test_summary_carries_both_verdicts(self):
        naukri = {"skillMismatch": "Kubernetes", "workExperience": True}
        text = match_mod.summarise(match_mod.compare(JOB_PAGE, naukri, MATCH_CONFIG), MATCH_CONFIG)
        assert "Azure*" not in text and "Azure" in text, "Azure is not a must-have"
        assert "C#*" in text and "Kubernetes" in text and "experience yes" in text


class TestNoPersonalDataShipped:
    """A public repo must not carry anyone's personal details in it.

    This runs on every `pytest`, which is the point: the repo started life as a
    fork of a personal tool, and the way personal data gets published is never a
    decision - it is a file someone forgot about. Cheaper to fail a test than to
    rewrite public history.
    """

    ROOT = Path(__file__).resolve().parent.parent
    SCANNED = (".py", ".yaml", ".yml", ".md", ".txt", ".json", ".toml", ".cfg", ".ini")
    SKIP_DIRS = {"data", ".git", "__pycache__", ".pytest_cache", "logs", ".venv",
                 "venv", "node_modules", ".mypy_cache", ".ruff_cache"}

    # Split so this file does not itself trip the scan it is running.
    NAMES = ("sag" + "ar", "jad" + "hav", "ish" + "ita", "sam" + "ja")

    PATTERNS = {
        "email address": r"[\w.+-]+@[\w-]+\.[a-z]{2,}",
        "Indian mobile": r"(?:\+91[\s-]?)?\b[6-9]\d{9}\b",
        "LinkedIn profile": r"linkedin\.com/in/[\w-]+",
        "Windows user path": r"[Cc]:[\\/]+Users[\\/]+[\w.-]+",
        "unix home path": r"/(?:home|Users)/[\w.-]+/",
        "API token": r"apify_api_[A-Za-z0-9]{10,}|sk-[A-Za-z0-9]{20,}",
        "session cookie": r"nauk_at|NKWAP",
        "stated CTC": r"expected_ctc|notice_buyout|willing_to_relocate",
    }

    # Documentation and invented fixtures, not leaked data.
    ALLOWED = re.compile(
        r"example\.com|<YOUR NAME>|github\.com/<you>|yourname/|apify_api_\.\.\."
        r"|users\.noreply\.github\.com|naukri-job-screener/",
        re.IGNORECASE)

    def _files(self):
        for path in self.ROOT.rglob("*"):
            if not path.is_file() or set(path.parts) & self.SKIP_DIRS:
                continue
            if path.resolve() == Path(__file__).resolve():
                continue
            # A copyright holder's name in LICENSE is intentional, not a leak.
            if path.name == "LICENSE":
                continue
            if path.suffix.lower() in self.SCANNED:
                yield path

    def test_no_personal_names(self):
        offenders = []
        for path in self._files():
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            offenders += [f"{path.relative_to(self.ROOT)}: {n}"
                          for n in self.NAMES if n in text]
        assert not offenders, "personal names in tracked files: " + "; ".join(offenders)

    @pytest.mark.parametrize("label", list(PATTERNS))
    def test_no_contact_details_or_secrets(self, label):
        pattern = re.compile(self.PATTERNS[label], re.IGNORECASE)
        offenders = []
        for path in self._files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            lines = text.splitlines()
            for match in pattern.finditer(text):
                line_no = text[:match.start()].count("\n")
                line = lines[line_no] if line_no < len(lines) else ""
                if self.ALLOWED.search(match.group(0)) or self.ALLOWED.search(line):
                    continue
                offenders.append(f"{path.relative_to(self.ROOT)}:{line_no + 1}  {match.group(0)[:60]}")
        assert not offenders, f"{label} found: " + "; ".join(offenders[:5])

    def test_no_binary_files_tracked(self):
        """A stray .pdf or .docx in the tree is almost certainly someone's CV."""
        risky = [p.relative_to(self.ROOT) for p in self.ROOT.rglob("*")
                 if p.is_file() and not set(p.parts) & self.SKIP_DIRS
                 and p.suffix.lower() in (".pdf", ".docx", ".doc", ".xlsx", ".png",
                                          ".jpg", ".jpeg", ".odt", ".rtf")]
        assert not risky, "binary/document files in the tree: " + "; ".join(map(str, risky))

    def test_gitignore_covers_everything_personal(self):
        ignored = (self.ROOT / ".gitignore").read_text(encoding="utf-8")
        for entry in ("data/", "config.yaml", "*.pdf", "*.docx", ".env", "logs/"):
            assert entry in ignored, f".gitignore is missing {entry!r}"

    def test_example_config_has_no_real_values(self):
        """config.example.yaml is tracked - it must stay a template."""
        text = (self.ROOT / "config.example.yaml").read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = re.sub(r"\s+#.*$", "", line).strip()   # drop trailing comments
            if not stripped or stripped.startswith("#"):
                continue
            # Every non-comment line must be an empty container, a null, or a
            # neutral threshold - never a name, a city, a salary or a skill.
            assert re.fullmatch(
                r"[\w_]+:\s*(\[\]|\{\}|null|true|false|\d+(\.\d+)?|local)?", stripped), (
                f"config.example.yaml carries a real value: {stripped!r}")


class TestTrackerPage:
    """The HTML tracker is the thing you actually look at. It must have rows."""

    def test_rows_are_built_from_the_shortlist_shape(self):
        """`scan.write` passes {shortlist, review}; build_rows read {naukri}.

        The keys never matched, so every page ever written had zero rows in it
        and nothing complained - an empty page is still a valid page.
        """
        from screener import page as page_mod
        results = {
            "shortlist": [{"job_id": "1", "title": "Dot Net Developer",
                           "company": "Acme", "location": "Pune", "score": 88.0,
                           "url": "https://naukri.com/1"}],
            "review": [{"job_id": "2", "title": "Angular Developer",
                        "company": "Globex", "location": "Mumbai", "score": 61.0,
                        "url": "https://naukri.com/2"}],
        }
        rows = page_mod.build_rows(results, {}, "2026-08-28")
        assert len(rows) == 2, f"expected both bands, got {rows}"
        assert {r["title"] for r in rows} == {"Dot Net Developer", "Angular Developer"}

    def test_the_original_naukri_shape_still_works(self):
        from screener import page as page_mod
        results = {"naukri": [{"job_id": "9", "title": "X", "company": "Y",
                               "location": "Pune", "score": 70.0, "url": "u"}]}
        assert len(page_mod.build_rows(results, {}, "2026-08-28")) == 1

    def test_a_job_in_both_bands_is_not_duplicated(self):
        from screener import page as page_mod
        job = {"job_id": "1", "title": "T", "company": "C", "location": "Pune",
               "score": 80.0, "url": "u"}
        rows = page_mod.build_rows({"shortlist": [job], "review": [job]}, {}, "2026-08-28")
        assert len(rows) == 1


class TestHimalayas:
    """The remote board. Its API takes no filters, so this module is the filter."""

    def test_unrestricted_postings_are_open_to_you(self):
        from screener.sources import himalayas as h
        assert h.eligible(None) is True
        assert h.eligible([]) is True

    def test_us_only_postings_are_dropped(self):
        """A 'remote' job restricted to the US is not one you can take."""
        from screener.sources import himalayas as h
        assert h.eligible(["United States"]) is False
        assert h.eligible(["Germany", "Ireland"]) is False

    def test_postings_naming_india_are_kept(self):
        from screener.sources import himalayas as h
        assert h.eligible(["India"]) is True
        assert h.eligible(["Worldwide"]) is True
        assert h.eligible(["United States", "India"]) is True

    def test_a_record_maps_onto_the_job_shape(self):
        from screener.sources import himalayas as h
        job = h.to_job({
            "title": "Senior .NET Engineer",
            "companyName": "Acme Remote",
            "applicationLink": "https://himalayas.app/companies/acme/jobs/dotnet",
            "guid": "https://himalayas.app/companies/acme/jobs/dotnet",
            "categories": ["Backend-Development", "Fullstack-Development"],
            "locationRestrictions": [],
            "description": "<p>Build APIs in <b>ASP.NET Core</b>.</p>",
            "seniority": ["Mid-level"],
        })
        assert job is not None
        assert job.source == "himalayas"
        assert job.location == "Remote"
        # Hyphenated categories would never match a resume skill.
        assert "Backend Development" in job.skills
        assert "<b>" not in job.description and "ASP.NET Core" in job.description
        # No invented years - Himalayas states a band, not a number.
        assert job.min_exp is None

    def test_a_record_with_no_link_is_skipped(self):
        from screener.sources import himalayas as h
        assert h.to_job({"title": "X", "companyName": "Y"}) is None

    def test_restricted_locations_are_visible_in_the_row(self):
        from screener.sources import himalayas as h
        job = h.to_job({"title": "T", "companyName": "C", "applicationLink": "u",
                        "locationRestrictions": ["India", "Singapore"]})
        assert "India" in job.location

    def test_the_page_does_not_double_prefix_the_id(self):
        """A doubled prefix would mint a new seen.json key every single run."""
        from screener import page as page_mod
        rows = page_mod.build_rows(
            {"shortlist": [{"job_id": "himalayas:dotnet", "title": "T", "company": "C",
                            "location": "Remote", "score": 70.0, "url": "u",
                            "source": "himalayas"}]}, {}, "2026-08-31")
        assert rows[0]["id"] == "himalayas:dotnet"
        assert rows[0]["board"] == "Himalayas"

    def test_remote_regions_override_the_india_default(self):
        from screener.sources import himalayas as h
        assert h.eligible(["Germany"], ["Germany"]) is True
        assert h.eligible(["India"], ["Germany"]) is False


class TestBoards:
    """The other remote boards. Each parser is fed the shape its API really returns."""

    REGIONS = ["India", "APAC", "Worldwide", "Anywhere"]

    def test_open_to_keeps_unrestricted_and_matching_listings(self):
        from screener.sources import boards as b
        assert b.open_to("", self.REGIONS) is True
        assert b.open_to("Anywhere in the World", self.REGIONS) is True
        assert b.open_to("LATAM, Europe, USA, Canada, APAC", self.REGIONS) is True
        assert b.open_to("USA Only", self.REGIONS) is False

    def test_india_does_not_match_indiana(self):
        from screener.sources import boards as b
        assert b.open_to("Indianapolis, Indiana", self.REGIONS) is False
        assert b.open_to("Anywhere in India", self.REGIONS) is True

    def test_remotive_record(self, monkeypatch):
        from screener.sources import boards as b
        monkeypatch.setattr(b, "fetch_json", lambda url: {"jobs": [
            {"id": 7, "url": "https://remotive.com/x-7", "title": ".NET Developer",
             "company_name": "Acme ", "tags": ["c#", "azure"],
             "publication_date": "2026-09-11T20:16:48",
             "candidate_required_location": "Worldwide", "salary": "$50k",
             "description": "<p>ASP.NET Core</p>"},
            {"id": 8, "url": "https://remotive.com/x-8", "title": "T", "company_name": "C",
             "candidate_required_location": "USA"},
        ]})
        jobs = b.remotive({})
        assert [j.job_id for j in jobs] == ["remotive:7"]
        job = jobs[0]
        assert job.company == "Acme" and job.skills == ["c#", "azure"]
        assert job.location == "Remote - Worldwide" and job.created_ms
        assert job.description == "ASP.NET Core" and job.company_apply

    def test_remoteok_skips_the_terms_element(self, monkeypatch):
        from screener.sources import boards as b
        monkeypatch.setattr(b, "fetch_json", lambda url: [
            {"legal": "API Terms of Service"},
            {"id": "1", "epoch": 1789344012, "position": "Backend Engineer",
             "company": "Sleek", "tags": ["python"], "location": "",
             "url": "https://remoteok.com/1", "salary_min": 50000, "salary_max": 80000},
        ])
        jobs = b.remoteok({})
        assert len(jobs) == 1 and jobs[0].title == "Backend Engineer"
        assert jobs[0].salary_label == "USD 50,000-80,000 annual"

    def test_wwr_title_splits_company_and_role(self):
        import xml.etree.ElementTree as ET
        from screener.sources import boards as b
        item = ET.fromstring(
            "<item><title>AccuLynx: Senior Software Engineer </title>"
            "<region>Anywhere in the World</region><skills>Vue.js, C#, and REST APIs</skills>"
            "<pubDate>Tue, 15 Sep 2026 14:12:47 +0000</pubDate>"
            "<link>https://weworkremotely.com/remote-jobs/acculynx-senior</link></item>")
        job = b._wwr_item(item, self.REGIONS)
        assert job.company == "AccuLynx" and job.title == "Senior Software Engineer"
        assert job.skills == ["Vue.js", "C#", "REST APIs"]
        assert job.job_id == "weworkremotely:acculynx-senior"

    def test_hn_header_is_parsed_and_region_restricted_roles_dropped(self):
        from screener.sources import boards as b
        ok = b.hn_comment({"id": 1, "created_at_i": 1788274914, "text":
                           "Quill | Fullstack SWE | Full-time | REMOTE (worldwide) | $150K<p>More"},
                          self.REGIONS)
        assert ok.company == "Quill" and ok.title == "Fullstack SWE"
        assert ok.url.endswith("item?id=1")
        assert b.hn_comment({"id": 2, "text": "Modash | Senior Engineer | Remote (Europe)"},
                            self.REGIONS) is None
        assert b.hn_comment({"id": 3, "text": "Smarkets | Engineer | Onsite (London, UK)"},
                            self.REGIONS) is None
        # A bare REMOTE with no region named is open to anyone.
        assert b.hn_comment({"id": 4, "text": "Acme | Backend Engineer | REMOTE"},
                            self.REGIONS) is not None

    def test_greenhouse_keeps_preferred_cities_but_not_remote_us(self, monkeypatch):
        from screener.sources import boards as b
        monkeypatch.setattr(b, "fetch_json", lambda url: {"jobs": [
            {"id": 1, "absolute_url": "u1", "title": "Engineer", "company_name": "GitLab",
             "location": {"name": "Remote, Bangalore"}, "content": "&lt;p&gt;Go&lt;/p&gt;"},
            {"id": 2, "absolute_url": "u2", "title": "Engineer", "company_name": "GitLab",
             "location": {"name": "Remote - US"}},
            {"id": 3, "absolute_url": "u3", "title": "Engineer", "company_name": "GitLab",
             "location": {"name": "Pune"}},
        ]})
        config = {"companies": {"greenhouse": ["gitlab"]},
                  "preferred_locations": ["Bangalore", "Remote"]}
        jobs = b.greenhouse(config)
        assert [j.job_id for j in jobs] == ["greenhouse:gitlab-1"]
        assert jobs[0].description == "Go", "double-escaped HTML must be stripped"

    def test_selection_expands_all_and_adds_company_boards(self):
        from screener.sources import boards as b
        chosen = b.selected({"boards": ["all"], "companies": {"lever": ["palantir"]}})
        assert "remoteok" in chosen and "hn" in chosen and "lever" in chosen
        assert "greenhouse" not in chosen, "no companies listed for it"
        assert b.selected({"include_himalayas": True}) == ["himalayas"]

    def test_unknown_and_unsupported_boards_are_explained(self):
        from screener.sources import boards as b
        problems = b.unknown(["all", "remoteok", "wellfound", "monster"])
        assert len(problems) == 2
        assert "Cloudflare" in problems[0] and "not a board" in problems[1]

    def test_one_failing_board_does_not_sink_the_rest(self, monkeypatch):
        from screener.sources import boards as b
        good = Job(job_id="remoteok:1", title="Dev", company="C", url="u", source="remoteok")

        def broken(config):
            raise b.SourceError("down")
        monkeypatch.setitem(b.READERS, "remotive", broken)
        monkeypatch.setitem(b.READERS, "remoteok", lambda config: [good])
        jobs, failed = b.gather({"boards": ["remotive", "remoteok"]})
        assert jobs == [good] and failed == ["remotive"]

    def test_posted_window_applies_to_boards(self, monkeypatch):
        from screener.sources import boards as b
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        fresh = Job(job_id="remoteok:1", title="A", company="C", url="u", created_ms=now_ms)
        stale = Job(job_id="remoteok:2", title="B", company="C", url="u",
                    created_ms=now_ms - 40 * 86_400_000)
        monkeypatch.setitem(b.READERS, "remoteok", lambda config: [fresh, stale])
        jobs, _ = b.gather({"boards": ["remoteok"], "posted_within_days": 7})
        assert jobs == [fresh]

    def test_the_same_opening_on_two_boards_is_listed_once(self):
        first = Job(job_id="remotive:1", title="Senior .NET Engineer", company="Acme",
                    url="a", source="remotive")
        again = Job(job_id="remoteok:9", title="Senior .NET engineer", company="ACME",
                    url="b", source="remoteok")
        other = Job(job_id="remoteok:10", title="QA Lead", company="Acme", url="c")
        assert scan_mod.dedupe([first, again, other]) == [first, other]

    def test_page_labels_every_board(self):
        rows = page_mod.build_rows(
            {"shortlist": [{"job_id": "weworkremotely:x", "title": "T", "company": "C",
                            "location": "Remote", "score": 70.0, "url": "u",
                            "source": "weworkremotely"}]}, {}, "2026-09-15")
        assert rows[0]["board"] == "We Work Remotely"

    def test_source_none_needs_a_board(self):
        from screener.sources.base import get_source
        assert get_source({"source": "none"}).gather({}) == []


class TestNaukriFeedActor:
    """A record exactly as blackfalcondata/naukri-jobs-feed returned it on 2026-09-15."""

    RECORD = {
        "jobId": "230426030362", "title": "Full Stack .Net Developer",
        "companyName": "Infosys", "location": "Hyderabad",
        "minimumExperience": 3, "maximumExperience": 8, "experienceText": "3-8 Yrs",
        "salary": "Not disclosed",
        "skills": [".NET", "Angular", ".NET Core", "Full Stack"],
        "createdDate": "2026-09-15T09:17:40.881Z", "footerLabel": "Few Hours Ago",
        "staticUrl": "https://www.naukri.com/infosys-jobs-careers-11244",
        "portalUrl": "https://www.naukri.com/job-listings-full-stack-net-developer-infosys-hyderabad-3-to-8-years-230426030362",
        "companyApplyJob": False,
        "descriptionSnippet": "Strong expertise in C#, .NET, ASP.NET, .NET Core",
    }

    def test_maps_onto_the_job_shape(self):
        from screener.sources.apify import feed_to_job
        job = feed_to_job(self.RECORD)
        assert job.job_id == "230426030362", "bare id, same ledger key as a local run"
        assert job.url.endswith("-230426030362"), "the job, not the company page"
        assert (job.min_exp, job.max_exp) == (3.0, 8.0)
        assert job.salary_label is None and "Angular" in job.skills
        assert job.created_ms and job.auto_applicable

    def test_a_record_with_no_link_is_skipped(self):
        from screener.sources.apify import feed_to_job
        assert feed_to_job({"jobId": "1", "title": "T"}) is None


class TestSiteBuild:
    """The published site is public: only job rows may leave the machine."""

    def test_latest_json_carries_no_searches_or_profile(self, tmp_path):
        import json as json_mod
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import build_site
        jobs = tmp_path / "home" / "jobs"
        jobs.mkdir(parents=True)
        (jobs / "openings-2026-09-15-r1.html").write_text("<p>page</p>", encoding="utf-8")
        (jobs / "results-2026-09-15-r1.json").write_text(json_mod.dumps({
            "at": "2026-09-15T20:00:00", "collected": 3,
            "searches": [{"keyword": ".NET Developer", "location": "Springfield"}],
            "shortlist": [{"job_id": "remoteok:1", "title": "Dev", "company": "C", "url": "u",
                           "score": 80.0, "why": "skills=40", "description": "long text"}],
            "review": [], "dropped": [{"job_id": "x"}],
        }), encoding="utf-8")
        build_site.build(tmp_path / "site", tmp_path / "home")
        published = (tmp_path / "site" / "scan" / "latest.json").read_text(encoding="utf-8")
        assert "Springfield" not in published and "searches" not in published
        assert "long text" not in published and '"dropped"' not in published
        assert json_mod.loads(published)["shortlist"][0]["score"] == 80.0
        assert (tmp_path / "site" / "packs.json").exists()
        assert (tmp_path / "site" / "index.html").exists()
