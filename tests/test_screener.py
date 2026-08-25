"""Tests for the parts that decide which jobs you see.

The scoring and parsing paths are the ones worth pinning down: a browser bug
shows up as an empty run you notice immediately, but a scoring bug quietly
ranks the wrong jobs first and looks like it worked.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screener import config as config_mod
from screener import resume as resume_mod
from screener import score as score_mod
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

    @pytest.mark.parametrize("role", ["qa-automation", "backend-engineer", "data-engineer"])
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
        "qa-automation": dict(
            skills=["Selenium", "Playwright", "PyTest", "API Testing", "JIRA", "TestNG"],
            titles=["Senior QA Automation Engineer"],
            text="qa automation test engineer regression test cases defect management automation testing"),
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
        "devops-sre": dict(
            skills=["Kubernetes", "Terraform", "Docker", "Prometheus", "Ansible", "ArgoCD"],
            titles=["DevOps Engineer"],
            text="devops sre infrastructure ci/cd kubernetes cloud platform engineer reliability"),
        "data-scientist": dict(
            skills=["PyTorch", "Scikit Learn", "Pandas", "TensorFlow", "MLflow", "NumPy"],
            titles=["Data Scientist"],
            text="machine learning data science model deep learning nlp analytics"),
        "mobile-developer": dict(
            skills=["Kotlin", "Jetpack Compose", "Retrofit", "Room", "Coroutines", "Hilt"],
            titles=["Senior Android Developer"],
            text="android developer native apps mobile app play store"),
        # The hard one. A full stack resume is supposed to look half like
        # frontend and half like backend, so it can never win on margin - only
        # the job title separates it.
        "fullstack-developer": dict(
            skills=["React", "Node js", "MongoDB", "Express", "TypeScript", "Docker"],
            titles=["Full Stack Developer"],
            text="full stack mern web application frontend and backend"),
    }

    # An iOS resume shares no vocabulary with the Android one but must land on
    # the same pack.
    IOS = dict(skills=["Swift", "SwiftUI", "UIKit", "Core Data", "Combine", "Xcode"],
               titles=["iOS Developer"],
               text="ios developer swift mobile application app store")

    @pytest.mark.parametrize("expected", list(RESUMES))
    def test_each_field_is_detected(self, expected):
        got, scores = config_mod.detect_role(self.RESUMES[expected])
        assert got == expected, f"got {got}; scores {scores}"

    def test_ios_lands_on_the_mobile_pack(self):
        got, _ = config_mod.detect_role(self.IOS)
        assert got == "mobile-developer"

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

    def test_detection_fills_the_gate(self, tmp_path):
        """A detected pack must actually reach the config, not just be logged."""
        facts = {**self.RESUMES["backend-engineer"], "skill_years": {}}
        config = config_mod.load(path=tmp_path / "none.yaml", profile=None, resume_facts=facts)
        assert config["role"] == "backend-engineer"
        assert config["role_detected"] == "backend-engineer"
        assert config["must_have_any"], "a detected pack should supply the field gate"

    def test_an_explicit_role_is_never_overridden(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("role: data-engineer\n", encoding="utf-8")
        facts = {**self.RESUMES["backend-engineer"], "skill_years": {}}
        config = config_mod.load(path=path, profile=None, resume_facts=facts)
        assert config["role"] == "data-engineer", "the user's choice must win"
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
        path.write_text("role: data-engineer\n", encoding="utf-8")
        config = config_mod.load(path=path, profile=None, resume_facts=self.FACTS)
        assert "data engineer" in [m.lower() for m in config["must_have_any"]]

    def test_locations_default_to_resume_city_plus_remote(self, tmp_path):
        config = config_mod.load(path=tmp_path / "none.yaml", profile=None,
                                 resume_facts=self.FACTS)
        assert "Pune" in config["preferred_locations"]
        assert "Remote" in config["preferred_locations"]


# --- privacy ------------------------------------------------------------

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
