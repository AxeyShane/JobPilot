"""Interview prep support for JobPilot (port of ai-job-search's /interview tool).

Pure stdlib. Turns an application archive + a realistic profile into a
prep pack: company briefing, likely questions with honest STAR bridges, a
primary STAR story, and a honest gap list -- it never invents experience.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Vocab used to recognize skills mentioned in a posting's text.
SKILL_TERMS: List[str] = [
    "python", "java", "javascript", "typescript", "golang", "ruby", "c++", "c#",
    "sql", "postgresql", "mysql", "mongo", "mongodb", "graphql", "rest", "api",
    "react", "redux", "vue", "angular", "node", "nodejs", "django", "flask", "fastapi",
    "docker", "kubernetes", "k8s", "terraform", "aws", "gcp", "azure", "cloud",
    "linux", "git", "ci/cd", "ci", "microservices", "oop", "design patterns",
    "distributed", "performance", "security", "ml", "machine learning", "data",
    "analytics", "spark", "kafka", "redis", "elastic", "rabbitmq", "pandas",
    "numpy", "leadership", "communication", "team", "delivery", "testing", "pytest",
]

# Generic behavioral questions worth prepping regardless of the STAR mapping
# (sanity fallback so a pack is never empty).
GENERIC_QUESTIONS: List[Dict[str, str]] = [
    {"q": "Tell me about yourself and your background.",
     "intent": "Screen for narrative, fit, and pacing.",
     "bridge": "Give a compressed arc: who you are, what you have shipped, what you want next."},
    {"q": "Why are you interested in this company and this role?",
     "intent": "Motivation and research depth.",
     "bridge": "Connect a concrete fact about the company to your target role and skills."},
    {"q": "What is a skill you are still developing?",
     "intent": "Honest self-awareness and growth mindset.",
     "bridge": "Name a genuine gap (see gaps list) and the concrete step you take now."},
]

# Skills that are more soft/meta attributes; never a fake certificate.
_SOFT_SKILLS = frozenset({"leadership", "communication", "teamwork", "delivery"})


def _norm(text: str) -> str:
    """Lowercase and collapse whitespace for keyword matching."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()


def profile_skills(profile: Dict[str, Any]) -> List[str]:
    """Flatten the profile's claimed skills into a sorted, de-duplicated list.

    Reads ``skills_boundary.{languages,frameworks,devops,databases,tools}`` plus
    any per-experience ``skills`` lists. Never invents a skill.
    """
    seen = {
        item.strip().lower()
        for key in ("languages", "frameworks", "devops", "databases", "tools")
        for item in ((profile.get("skills_boundary") or {}).get(key) or [])
        if isinstance(item, str) and item.strip()
    }
    for exp in (profile.get("experiences") or []):
        for item in (exp.get("skills") or []):
            if isinstance(item, str) and item.strip():
                seen.add(item.strip().lower())
    return sorted(seen)


def present_keywords(text: str) -> List[str]:
    """Return posting/role keywords (in SKILL_TERMS) found in ``text``."""
    hay = _norm(text)
    found: List[str] = []
    for term in SKILL_TERMS:
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", hay):
            if term not in found:
                found.append(term)
    return found


def extract_star_stories(profile: Dict[str, Any], limit: int = 6) -> List[Dict[str, str]]:
    """Build honest STAR stories from real resume facts only.

    Situation/task come from preserved companies/projects, action lists the
    claimed skills, result uses ``real_metrics`` verbatim. If no verified metric
    exists, the story says so instead of inventing a number.
    """
    facts = profile.get("resume_facts") or {}
    companies = [c for c in (facts.get("preserved_companies") or []) if c]
    projects = [p for p in (facts.get("preserved_projects") or []) if p]
    metrics = [m for m in (facts.get("real_metrics") or []) if m]
    school = (facts.get("preserved_school") or "").strip() or "the team"
    skills = profile_skills(profile)

    if not projects:
        return []

    stories = []
    for idx, proj in enumerate(projects[:limit]):
        action = ", ".join(skills[:5]) if skills else "the tools this role already calls for"
        result = metrics[idx % len(metrics)] if metrics else "an outcome I can verify (no metric recorded)"
        anchor = (companies[idx % len(companies)] if companies else school)
        stories.append({
            "situation": f"Working on {proj} at {anchor}.",
            "task": f"Own and deliver {proj} end to end.",
            "action": f"Applied {action} and made the calls within scope.",
            "result": result,
            "source_role": anchor,
        })
    return stories


def map_questions_to_star(posting_text: str, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Map likely interview questions from posting keywords onto STAR stories.

    Always honest: a question whose keyword has no STAR example is returned with
    ``matched=False`` and an honest bridge (never invented experience).
    """
    skills = profile_skills(profile)
    stories = extract_star_stories(profile)

    # Distribute verified STAR stories across matched keyword topics.
    by_token = {}
    for story in stories:
        for tok in present_keywords(" ".join(story.values())):
            by_token.setdefault(tok, []).append(story)

    questions: List[Dict[str, Any]] = []
    for keyword in present_keywords(posting_text):
        matched_stories = by_token.get(keyword, [])
        matched = bool(matched_stories)
        if matched:
            star = matched_stories[0]
            q = f"Walk me through a time you applied {keyword} to a real problem."
            intent = f"Probe hands-on {keyword} experience."
            bridge = f"Tell the {keyword} story (see STAR map): {star['situation']} {star['action']}"
        else:
            star = None
            missing_note = "not in your profile" if keyword not in skills else "not yet covered by a written story"
            q = f"How would you handle a task that needs {keyword}?"
            intent = f"Test {keyword} depth ({missing_note})."
            bridge = (
                "Be honest you have not shipped it professionally; connect one transferable "
                "skill you do have and the concrete step you are taking now."
            )
        questions.append({
            "q": q, "intent": intent, "matched": matched, "skill": keyword,
            "star": star, "bridge": bridge,
        })
    return questions


def company_briefing(company: str, external_facts: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a company briefing with a verify-before-use contract.

    Only sentence-level claims come from ``external_facts``; sets ``used_or_not``
    and a ``source`` tag so callers can see whether anything was taken verbatim.
    """
    if not company:
        company = "the company"
    facts = external_facts or {}
    fact_lines = []
    for key in ("founded", "hq", "size", "industry", "mission", "notes"):
        val = facts.get(key)
        if val:
            fact_lines.append(f"- {key}: {val}")
    used = bool(fact_lines)
    source = facts.get("source") or facts.get("source_tag")
    source = source or ("external_facts passed" if used else "UNVERIFIED")
    return {
        "company": company,
        "brief": (
            f"{company}. The claims below are only usable when verified; never assert any "
            "unverified fact in the interview."
        ),
        "verified_facts": "\n".join(fact_lines),
        "source": source,
        "used_or_not": used,
    }


def build_prep_pack(
    application_archive: Dict[str, Any],
    profile: Dict[str, Any],
    external_facts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Produce a full interview prep pack.

    Uses archive's posting_text + profile to map questions to STAR, builds
    company + interviewer briefs and a honest gaps list.
    """
    job_title = application_archive.get("job_title", "the role")
    company = application_archive.get("company", "") or ""
    posting_text = application_archive.get("posting_text", "") or ""
    round_idx = int(application_archive.get("round_idx") or 1)
    feedback = application_archive.get("feedback") or []

    questions = map_questions_to_star(posting_text, profile)
    if len(questions) < 4:
        for g in GENERIC_QUESTIONS[:2]:
            questions.append({
                "q": g["q"], "intent": g["intent"], "matched": False,
                "skill": None, "star": None, "bridge": g["bridge"],
            })

    stories = extract_star_stories(profile)
    star_map: Dict[str, str] = {}
    if stories:
        best = stories[0]
        star_map = {
            "situation": best["situation"],
            "task": best["task"],
            "action": best["action"],
            "result": best["result"],
            "source_role": best["source_role"],
        }

    gaps = []
    for q in questions:
        if not q["matched"] and q.get("skill"):
            gaps.append({
                "topic": q["skill"],
                "honest_bridge": (
                    f"Do not claim '{q['skill']}'. Acknowledge the gap, then describe your "
                    "transferable skill and the concrete step of your current learning plan."
                ),
            })
    if not gaps:
        gaps.append({
            "topic": "technical vocabulary depth (posting keywords)",
            "honest_bridge": "Confirm you can defend every claim in your profile; if not, "
                             "remove it from the CV before the interview. Never overstate.",
        })

    interviewer_brief = (
        f"Hiring at {company or 'the company'} for {job_title}, round {round_idx}. "
        "Preferred: candid, honest STAR answers -- never invented experience."
    )
    if feedback:
        notes = "; ".join(str(f) for f in feedback[:3])
        interviewer_brief += f" Previous feedback to weave in: {notes}"

    return {
        "company_brief": company_briefing(company, external_facts),
        "interviewer_brief": interviewer_brief,
        "round_idx": round_idx,
        "likely_questions": questions,
        "star_map": star_map,
        "gaps": gaps,
    }


def mock_interview(question: str, star_map: Dict[str, Any], depth: int = 1) -> str:
    """Return a scripted no-LLM mock interviewer script for one question."""
    depth = max(1, int(depth or 1))
    placeholders = {k: star_map.get(k, "(not supplied)") for k in ("situation", "task", "action", "result")}

    lines = [
        "=== MOCK INTERVIEW - scripted turn (no LLM) ===",
        f"Q: {question}",
        "",
        "> You (candidate): answer OUT LOUD for 60-90 seconds.",
        "> Structure: SITUATION -> TASK -> ACTION -> RESULT.",
        f"   Situation: {placeholders['situation']}",
        f"   Task: {placeholders['task']}",
        f"   Action: {placeholders['action']}",
        f"   Result: {placeholders['result']}",
    ]
    follow = {
        1: "What part was hardest, and what did you do first?",
        2: "What specifically measured the result you claim?",
        3: "What would you do differently today?",
        4: "What risk did you spot early that you then avoided?",
    }
    for d in range(1, min(depth, 4) + 1):
        lines.append(f"  [FOLLOW-UP {d}]: {follow[d]}  -> answer again, 45 seconds.")

    lines.append("")
    lines.append("=== EVALUATION RUBRIC (self-score 1-5 each) ===")
    rubric = [
        "Honesty: every claim is verifiable from my own profile?",
        "STAR: situation, task, action, result all present?",
        "Concision: stayed under the time limit, no rambling?",
        "Relevance: connected benefits back to the role's needs?",
        "Recovery: short, honest follow-up lines that do not dodge?",
    ]
    for i, r in enumerate(rubric, start=1):
        lines.append(f"[{i}] {r} __/5")
    lines.append("Run twice: once timed, once scored. Target is 4+ on every line.")
    return "\n".join(lines)
