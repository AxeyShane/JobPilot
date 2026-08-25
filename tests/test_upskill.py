"""Tests for jobpilot.upskill. Run: python3 tests/test_upskill.py (cd to repo root)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from jobpilot.upskill import gap_analysis, learning_plan, posting_skills, profile_skills  # noqa: E402

PROFILE = {
    "experience": {"target_role": "backend engineer", "years_of_experience_total": "2"},
    "skills_boundary": {
        "languages": ["Python", "SQL"],
        "frameworks": ["FastAPI"],
        "devops": ["Docker"],
        "databases": ["PostgreSQL"],
        "tools": ["Git"],
    },
}

POSTINGS = [
    {"title": "Backend A", "posting_text": "We need python, sql, postgresql and docker."},
    {"title": "Backend B", "posting_text": "Looking for kubernetes and k8s engineers."},
    {"title": "Backend C", "posting_text": "Python, kubernetes and aws experience required."},
]
POSTINGS_LIST = [{"title": "Full", "skills": ["python", "react", "kubernetes"]}]

def test_profile_skills_gathers_claimed():
    got = profile_skills(PROFILE)
    assert "python" in got and "docker" in got and "postgresql" in got

def test_posting_skills_extracts_text():
    got = posting_skills({"posting_text": "We need python, docker and kubernetes."})
    assert "python" in got and "kubernetes" in got

def test_gap_analysis_counts_demand():
    r = gap_analysis(PROFILE, POSTINGS)
    assert r["total_gaps"] >= 1
    assert any(s["skill"] == "kubernetes" and s["demand_count"] == 2 for s in r["skills"])

def test_gap_analysis_marks_status_and_present_overlap():
    r = gap_analysis(PROFILE, POSTINGS)
    by = {s["skill"]: s for s in r["skills"]}
    assert by["kubernetes"]["current_status"] == "absent"
    assert "python" not in by  # present skills always dropped
    assert all(s["current_status"] != "present" for s in r["skills"])

def test_gap_analysis_returns_heatmap():
    r = gap_analysis(PROFILE, POSTINGS)
    assert r["heatmap"]
    assert r["top_gap"] == r["heatmap"][0]["skill"]

def test_gap_analysis_respects_seen_gaps_present():
    seen = {"kubernetes": {"status": "present"}}
    r = gap_analysis(PROFILE, POSTINGS, seen_gaps=seen)
    assert all(s["skill"] != "kubernetes" for s in r["skills"])

def test_gap_analysis_learnable_and_resources():
    r = gap_analysis(PROFILE, POSTINGS)
    k = next(s for s in r["skills"] if s["skill"] == "kubernetes")
    assert 1 <= k["learnable"] <= 5
    assert k["external_resources"]
    assert k["notes"]

def test_learning_plan_budget_and_priority():
    r = gap_analysis(PROFILE, POSTINGS_LIST)
    plan = learning_plan(r["skills"], time_budget_hours=20)
    assert plan
    assert [p["priority"] for p in plan] == list(range(1, len(plan) + 1))
    assert sum(p["hours"] for p in plan) <= 20
    assert all(p["verify"] and p["steps"] and p["hours"] >= 1 for p in plan)

def test_learning_plan_orders_by_demand():
    r = gap_analysis(PROFILE, POSTINGS)
    plan = learning_plan(r["skills"], time_budget_hours=20)
    assert plan[0]["skill"] == r["top_gap"]

def test_learning_plan_empty_input():
    assert learning_plan([]) == []

def test_learning_plan_webless_pointers():
    r = gap_analysis(PROFILE, POSTINGS_LIST)
    plan = learning_plan(r["skills"])
    assert any("roadmap.sh" in p["steps"] or "https://" in p["steps"] for p in plan)

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print("PASSED", len(fns), "upskill cases")
