"""Upskill / skill-gap analysis for JobPilot (mirror of ai-job-search's /upskill).

Pure stdlib. Computes a PIKTORIZED gap set from the profile + target postings,
then produces a web-less learning plan (generic README/roadmap pointers only)
with a priority ordering.
"""

from __future__ import annotations

import re
from typing import Any

# Heuristic complexity: learnable 1 = hardest to self-learn cold, 5 = easy.
_LEARNABLE: dict[str, int] = {
    "distributed systems": 1, "kubernetes": 1, "k8s": 1, "machine learning": 1,
    "terraform": 2, "microservices": 2, "kafka": 2, "aws": 2, "gcp": 2,
    "design patterns": 3, "docker": 3, "leadership": 2,
    "git": 5, "linux": 4, "python": 4, "sql": 4, "postgresql": 4,
    "mongodb": 4, "react": 4, "fastapi": 4, "django": 4, "flask": 4,
    "redis": 4, "pandas": 4, "numpy": 4, "pytest": 4, "rest": 4,
    "communication": 3, "agile": 4, "scrum": 4, "oop": 3,
}

# README/docs-style pointers (web-less: we never fetch their content here).
_LEARN_URLS: dict[str, list[str]] = {
    "python": ["https://docs.python.org/3/tutorial/"],
    "sql": ["https://www.postgresql.org/docs/", "https://roadmap.sh/sql"],
    "postgresql": ["https://www.postgresql.org/docs/"],
    "react": ["https://react.dev/learn"],
    "docker": ["https://docs.docker.com/get-started/"],
    "kubernetes": ["https://kubernetes.io/docs/tutorials/"],
    "k8s": ["https://kubernetes.io/docs/tutorials/"],
    "aws": ["https://aws.amazon.com/getting-started/"],
    "git": ["https://git-scm.com/book/en/v2/"],
    "fastapi": ["https://fastapi.tiangolo.com/tutorial/"],
    "flask": ["https://flask.palletsprojects.com/tutorial/"],
    "django": ["https://docs.djangoproject.com/en/stable/"],
    "pytest": ["https://docs.pytest.org/en/stable/"],
    "pandas": ["https://pandas.pydata.org/docs/getting_started/"],
    "rest": ["https://roadmap.sh/backend"],
    "oop": ["https://refactoring.guru/design-patterns/"],
    "design patterns": ["https://refactoring.guru/design-patterns/"],
    "machine learning": ["https://roadmap.sh/ai-data-scientist"],
    "ml": ["https://roadmap.sh/ai-data-scientist"],
}
_GENERIC_ROADMAP = "https://roadmap.sh/"

COMMON_SKILLS = [
    "python", "sql", "postgresql", "mysql", "mongodb", "react", "angular", "vue",
    "node", "nodejs", "docker", "kubernetes", "k8s", "aws", "gcp", "terraform",
    "go", "golang", "rust", "java", "c#", "fastapi", "flask", "django", "git",
    "redis", "kafka", "pandas", "numpy", "machine learning", "ml", "ci/cd", "pytest",
    "spark", "elastic", "microservices", "graphql", "rest", "leadership",
    "communication", "agile", "scrum", "linux", "scala", "api",
]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def profile_skills(profile: dict[str, Any]) -> set:
    """Set of all skills/experiences claimed in the profile (lowercased)."""
    present = set()
    boundary = profile.get("skills_boundary") or {}
    for key in ("languages", "frameworks", "devops", "databases", "tools"):
        for item in (boundary.get(key) or []):
            if isinstance(item, str) and item.strip():
                present.add(item.strip().lower())
    for exp in (profile.get("experiences") or []):
        for item in (exp.get("skills") or []):
            if isinstance(item, str) and item.strip():
                present.add(item.strip().lower())
    return present


def posting_skills(posting: dict[str, Any]) -> list[str]:
    """Extract recognized required skills from a posting dict."""
    raw = []
    for key in ("skills", "required_skills", "description", "posting_text", "full_description", "details"):
        val = posting.get(key)
        if isinstance(val, list):
            raw.extend(x for x in val if isinstance(x, str))
        elif isinstance(val, str):
            raw.append(val)
    hay = _norm(" ".join(raw))
    found = set()
    for term in COMMON_SKILLS:
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", hay):
            found.add(term)
    return sorted(found)


def _status(skill: str, present: set) -> str:
    if skill in present:
        return "present"
    for p in present:
        if len(skill) >= 3 and (skill in p or p in skill):
            return "weak"
    return "absent"


def _learnable(skill: str) -> int:
    return _LEARNABLE.get(skill, 3)


def _roadmap_url(skill: str) -> str:
    key = skill.replace(" ", "").lower()
    slugs = {"machinelearning": "ai-data-scientist", "ml": "ai-data-scientist",
             "k8s": "kubernetes", "c#": "csharp", "postgresql": "postgresql-dba"}
    slug = slugs.get(key, skill.replace(" ", "-").lower())
    return f"{_GENERIC_ROADMAP}{slug}"


def _resources(skill: str) -> list[str]:
    return list(_LEARN_URLS.get(skill, [f"README pointer: {_roadmap_url(skill)}"]))


def _status_rank(status: str) -> int:
    return {"absent": 0, "weak": 1, "present": 2}[status]


def gap_analysis(
    profile: dict[str, Any],
    target_postings: list[dict[str, Any]],
    seen_gaps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Analyze skill gaps across target postings.

    Produces ``skills`` (PIKTORIZED gap records) and a career ``heatmap``.
    Already-present skills are ignored, including those sealed as ``present``
    in ``seen_gaps``. Only real weak/absent required skills are surfaced.
    """
    seen_gaps = seen_gaps or {}
    present = profile_skills(profile)

    demand: dict[str, int] = {}
    status: dict[str, str] = {}
    closed: set = set()  # gaps already sealed as "present" by the learner
    for posting in target_postings or []:
        for skill in posting_skills(posting):
            demand[skill] = demand.get(skill, 0) + 1
            st = _status(skill, present)
            if st == "present":
                continue
            prev = seen_gaps.get(skill)
            if isinstance(prev, dict) and prev.get("status") == "present":
                closed.add(skill)  # already closed -> never resurfaces
                continue
            status[skill] = st

    skills_out = []
    for skill in sorted(demand):
        if skill in closed:
            continue
        st = status.get(skill, _status(skill, present))
        if st == "present":
            continue
        learn = _learnable(skill)
        if st == "weak":
            rel = next((p for p in present if skill in p or p in skill), "")
            notes = (
                f"Required by {demand[skill]} posting(s); you have a related entry "
                f"('{rel}') but not a defensible standalone one."
            )
        else:
            rel = ""
            notes = f"Required by {demand[skill]} posting(s); absent from your profile."
        skills_out.append({
            "skill": skill,
            "demand_count": demand[skill],
            "current_status": st,
            "learnable": learn,
            "notes": notes,
            "external_resources": _resources(skill),
        })

    heatmap = sorted(
        skills_out,
        key=lambda s: (-s["demand_count"], _status_rank(s["current_status"]), s["learnable"]),
    )
    return {
        "skills": skills_out,
        "heatmap": [
            {
                "skill": s["skill"], "demand_count": s["demand_count"],
                "status": s["current_status"], "learnable": s["learnable"],
            }
            for s in heatmap
        ],
        "profile_skills": sorted(present),
        "total_gaps": len(skills_out),
        "top_gap": heatmap[0]["skill"] if heatmap else None,
    }


def learning_plan(
    gap_list: list[dict[str, Any]],
    time_budget_hours: int = 20,
) -> list[dict[str, Any]]:
    """Build an ordered plan for PIKTORIZED gap records.

    Allocates a share of the time budget proportionally by demand and
    learnability, orders by priority (demand desc, gap severity, learnable asc)
    and attaches web-less step pointers plus a self-check ``verify``.
    """
    if not gap_list:
        return []
    n = len(gap_list)
    demand = [max(1, int(g.get("demand_count", 1))) for g in gap_list]
    learn = [max(1, int(g.get("learnable", 3) or 3)) for g in gap_list]
    weights = [d * (0.9 + 0.03 * l) for d, l in zip(demand, learn)]
    total_w = sum(weights)

    raw_hours = [max(1, round(time_budget_hours * w / total_w)) for w in weights]
    # Trim/correct rounding drift so the sum stays <= budget.
    drift = sum(raw_hours) - time_budget_hours
    idx = 0
    while drift > 0:
        if raw_hours[idx] > 1:
            raw_hours[idx] -= 1
            drift -= 1
        idx = (idx + 1) % n

    ordered = sorted(
        range(n),
        key=lambda i: (
            -weights[i],
            0 if gap_list[i].get("current_status") == "absent" else 1,
            raw_hours[i],
        ),
    )
    plans = []
    for rank, i in enumerate(ordered):
        g = gap_list[i]
        skill = g["skill"]
        hours = raw_hours[i]
        res = g.get("external_resources") or _resources(skill)
        plans.append({
            "skill": skill,
            "steps": _steps_text(skill, hours),
            "hours": hours,
            "priority": rank + 1,
            "verify": _verify_text(skill),
            "resources": res,
        })
    return plans


def _steps_text(skill: str, hours: int) -> str:
    url = _roadmap_url(skill)
    first = (list(_LEARN_URLS.get(skill, [url])) or [url])[0]
    return (
        f"Read the {skill} README/docs ({first}); follow the roadmap.sh track "
        f"({url}); practice one small build per module for ~{hours}h total."
    )


def _verify_text(skill: str) -> str:
    return (
        f"Self-check: build a tiny {skill} demo, commit it, and write a 3-bullet "
        "post mortem you can recite honestly in an interview."
    )
