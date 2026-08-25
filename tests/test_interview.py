"""Tests for jobpilot.interview. Run: python3 tests/test_interview.py
(cd to repo root; adds src/ to sys.path).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from jobpilot.interview import (  # noqa: E402
    build_prep_pack, company_briefing, extract_star_stories, map_questions_to_star,
    mock_interview, present_keywords, profile_skills,
)

PROFILE = {
    "personal": {"full_name": "Ada Lovelace", "email": "ada@example.com"},
    "experience": {"target_role": "software engineer", "years_of_experience_total": "3"},
    "skills_boundary": {
        "languages": ["Python", "SQL"],
        "frameworks": ["FastAPI", "Flask"],
        "devops": ["Docker", "AWS"],
        "databases": ["PostgreSQL"],
        "tools": ["Git"],
    },
    "resume_facts": {
        "preserved_companies": ["Analytical Engines"],
        "preserved_projects": ["Analytical Machine"],
        "preserved_school": "University of London",
        "real_metrics": ["4x faster query time"],
    },
}


def test_present_keywords_detects_role_terms():
    out = present_keywords("We need React, Docker and python for a backend role.")
    assert "react" in out and "docker" in out and "python" in out


def test_present_keywords_case_insensitive():
    out = present_keywords("CI/CD and K8S experience is nice to have.")
    assert "ci/cd" in out and "k8s" in out


def test_profile_skills_flattens_boundary():
    got = profile_skills(PROFILE)
    assert "python" in got and "postgresql" in got and "git" in got


def test_star_uses_real_facts_verbatim():
    stories = extract_star_stories(PROFILE)
    assert stories
    s = stories[0]
    assert "Analytical Machine" in s["situation"]
    assert "Analytical Engines" in s["situation"]
    assert "4x faster query time" in s["result"]


def test_company_briefing_marks_usage():
    b = company_briefing("ACME", {"founded": 2001, "industry": "ai"})
    assert b["used_or_not"] is True
    assert "2001" in b["verified_facts"]
    assert b["source"]


def test_company_briefing_without_facts_is_unverified():
    b = company_briefing("ACME", None)
    assert b["used_or_not"] is False
    assert "UNVERIFIED" in b["source"]


def test_company_briefing_never_claims_unverified():
    b = company_briefing("ACME", {})
    assert b["used_or_not"] is False
    assert "never assert any unverified fact" in b["brief"]


def test_questions_honest_for_missing_skill():
    res = map_questions_to_star("We use kubernetes in production.", PROFILE)
    assert res and any(q["skill"] == "kubernetes" and q["matched"] is False for q in res)


def test_questions_mark_present_skill_matched():
    res = map_questions_to_star("Python is required.", PROFILE)
    assert any(q["skill"] == "python" and q["matched"] is True for q in res)


def test_build_prep_pack_shape_and_honest_bridge():
    archive = {
        "job_title": "Software Engineer", "company": "ACME",
        "posting_text": "Looking for python and kubernetes engineers.",
        "round_idx": 2, "feedback": ["talk slower", "more depth on metrics"],
    }
    pack = build_prep_pack(archive, PROFILE, external_facts={"founded": "2010"})
    assert pack["round_idx"] == 2
    assert pack["company_brief"]["used_or_not"] is True
    assert "ACME" in pack["interviewer_brief"]
    assert any(g["topic"] == "kubernetes" and "Do not claim" in g["honest_bridge"] for g in pack["gaps"])
    assert pack["star_map"]["situation"]


def test_prep_pack_safe_without_facts():
    archive = {"job_title": "Role", "company": "", "posting_text": "", "round_idx": 1}
    pack = build_prep_pack(archive, PROFILE)
    assert pack["company_brief"]["used_or_not"] is False
    assert pack["likely_questions"]


def test_mock_interview_scales_with_depth():
    mm = mock_interview(
        "Tell me about Python work.",
                        {"situation": "s", "task": "t", "action": "a", "result": "r"}, depth=2)
    assert isinstance(mm, str)
    assert "FOLLOW-UP 1" in mm and "FOLLOW-UP 2" in mm
    assert "EVALUATION RUBRIC" in mm


def test_mock_interview_depth_floor_and_rubric():
    mm = mock_interview("Why this role?", {}, depth=0)
    assert "FOLLOW-UP 1" in mm
    assert "Honesty: every claim" in mm


def test_matched_keyword_no_story_no_crash():
    """A keyword the profile declares but that has no written STAR story must
    not raise IndexError; it should produce an honest bridge instead."""
    from jobpilot.interview import build_prep_pack, present_keywords
    # Profile claims 'python' as a skill but has no resume-fact projects/stories.
    prof = {
        "personal": {"full_name": "Test"},
        "experience": {"target_role": "Data Scientist"},
        "skills_boundary": {"frameworks": ["python"], "tools": ["python"],
                            "languages": []},
        "resume_facts": {"preserved_projects": [], "preserved_companies": [],
                         "real_metrics": []},
    }
    archive = {"company": "Acme", "job_title": "Data Scientist",
               "posting_text": "Data Scientist role using python",
               "submitted_cv": "", "submitted_cover": "", "feedback": [],
               "round_idx": 1}
    pack = build_prep_pack(archive, prof)  # must not raise
    assert isinstance(pack.get("likely_questions"), list)
    # The honest-bridge fallback text should appear for the declared-but-story-less keyword.
    all_text = " ".join(q.get("bridge", "") for q in pack["likely_questions"])
    assert "invent" in all_text or "no written STAR story" in all_text

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print("PASSED", len(fns), "interview cases")
