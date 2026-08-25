# Tests for jobpilot.scoring.dimensions (dimensioned, explainable scoring).
# Run:  cd /mnt/c/msys64/home/aksha/projects/JobPilot && python3 tests/test_dimensions.py
# Pure stdlib, simple asserts, no pytest dependency.
import os
import sys
# Make the src-layout importable without the repo venv.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))
from jobpilot.scoring import dimensions as dim


def _profile(**over):
    p = {
        "skills_boundary": {
            "languages": ["Python", "SQL", "JavaScript", "English"],
            "frameworks": ["FastAPI", "React"],
            "devops": ["Docker", "AWS"],
            "databases": ["PostgreSQL"],
            "tools": ["Git", "Linux"],
        },
        "experience": {
            "years_of_experience_total": "5",
            "education_level": "Bachelors Degree",
            "current_job_title": "Backend Engineer",
            "target_role": "backend engineer",
        },
        "personal": {"city": "Toronto"},
        "compensation": {"salary_expectation": "90000", "salary_range_min": "85000"},
        "work_authorization": {"legally_authorized_to_work": "Yes",
                               "require_sponsorship": "No"},
        "preferences": ["remote friendly"],
    }
    p.update(over)
    return p


_GOOD = {"title": "Senior Backend Engineer",
         "location": "Toronto (hybrid)",
         "full_description": ("Python, FastAPI, PostgreSQL, Docker, AWS, SQL. 5+ years. "
                              "Fluent in English. Collaborate across teams. "
                              "Salary 120k-140k USD.")}


_bad = 0
_ok = 0


def check(name, cond, msg=""):
    global _bad, _ok
    if cond:
        _ok += 1
    else:
        _bad += 1
        print(f"FAIL [{name}]: {msg}")


def test_dimensions_present():
    r = dim.score_dimensions(_GOOD, _profile())
    checks = set(r["dimensions"].keys()) == set(dim.DIMENSIONS)
    for name, d in r["dimensions"].items():
        checks = checks and (0 <= d["score"] <= 100)
        checks = checks and ({"score", "rationale", "rubric"} <= set(d))
    checks = checks and (0 <= r["overall"] <= 100)
    check("dimensions present / 0-100", checks)


def test_return_shape():
    r = dim.score_dimensions(_GOOD, _profile())
    for key in ("dimensions", "overall", "dealbreakers", "deal_breakers",
                "warnings", "gaps", "composite", "computed"):
        check(f"key {key}", key in r)


def test_perfect_match_strong():
    r = dim.score_dimensions(_GOOD, _profile())
    check("perfect overall high", r["overall"] >= 40, r["overall"])
    check("perfect computed", r["computed"] is True)
    check("technical strong", r["dimensions"]["technical_skills"]["score"] >= 50)


def test_gap_recorded():
    gap_job = {"title": "Rust Systems Engineer",
               "description": "Rust, C++, Kubernetes required. German."}
    r = dim.score_dimensions(gap_job, _profile())
    any_gap = any("honesty gap" in w for w in r["gaps"] + r["warnings"])
    check("gap warning", any_gap, repr(r["gaps"]))
    check("gap not stuffed", r["dimensions"]["technical_skills"]["score"] < 60)


def test_deal_breaker_veto():
    veto = {"title": "Engineer", "description": "Must be a US citizen. Salary 100k."}
    r = dim.score_dimensions(veto, _profile())
    check("veto overall 0", r["overall"] == 0, r["overall"])
    check("veto composite 0", r["composite"] == 0)
    check("veto computed False", r["computed"] is False)
    check("veto deals non-empty", len(r["dealbreakers"]) > 0, r["dealbreakers"])


def test_salary_floor_veto():
    low = {"title": "Engineer", "description": "Salary $60,000 - $70,000. Python."}
    r = dim.score_dimensions(low, _profile())
    check("salary veto overall 0", r["overall"] == 0, r["overall"])
    check("salary veto listed", any("salary" in x for x in r["dealbreakers"]), r["dealbreakers"])


def test_language_veto():
    lang = {"title": "Engineer", "description": "Fluent in Spanish required. Salary 100k-120k."}
    r = dim.score_dimensions(lang, _profile())
    check("language veto", r["overall"] == 0 and r["computed"] is False)


def test_language_ok():
    ok = {"title": "Engineer", "description": "Fluent in English and Python. Salary 100k-120k."}
    r = dim.score_dimensions(ok, _profile())
    check("language ok computed", r["computed"] is True)


def test_preference_satisfied():
    pref_p = _profile(preferences=["remote friendly", "hybrid"])
    remote_job = {"title": "Engineer", "location": "Remote",
                  "description": "100% remote hybrid-friendly. Salary 100k-120k. Python."}
    r = dim.score_dimensions(remote_job, pref_p)
    check("pref satisfied computed", r["computed"] is True)
    check("pref dim > 0", r["dimensions"]["determination_prefs"]["score"] > 0)


def test_honesty_gap_recorded():
    r = dim.score_dimensions(_GOOD, _profile())
    check("gaps is list", isinstance(r["gaps"], list))
    check("warnings is list", isinstance(r["warnings"], list))


def test_hdeal_breakers_override():
    o = _profile(hdeal_breakers=["language"])
    job = {"description": "Must be a US citizen. Salary 100k. Fluent in Spanish."}
    r = dim.score_dimensions(job, o)
    check("override only language", all("language" in x for x in r["dealbreakers"]), r["dealbreakers"])
    check("override forces veto", r["overall"] == 0 and r["computed"] is False)


def test_years_shortfall_warning():
    job = {"title": "Engineer", "description": "10+ years of experience. Python. Salary 100k-120k."}
    r = dim.score_dimensions(job, _profile())
    has = any("years" in w and "honesty" in w for w in r["warnings"])
    check("years gap warning", has, r["warnings"])


def test_weights_sum():
    check("weights sum 1.0", abs(sum(dim.DIMENSION_WEIGHTS.values()) - 1.0) < 1e-9)


def test_rubric_bands():
    rb = dim.rubric("technical_skills")
    check("rubric excellent", "excellent" in rb)
    check("rubric tiles 0-100", rb["poor"][0] == 0 and rb["excellent"][1] == 100)


def test_rubric_band_label():
    check("band label", dim.rubric_band("technical_skills", 85) == "excellent")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ran {len(tests)} tests; asserts ok={_ok} bad={_bad}")
    sys.exit(1 if _bad else 0)


if __name__ == "__main__":
    main()