"""Dimensioned, explainable job-fit scoring (pure, stdlib-only).

Replaces an opaque 1-10 LLM score with a 5-dimension evaluation, each scored
0-100 against an explicit rubric, inspired by ai-job-search's "Job Evaluation
Framework". This is a pre-ranking / explainability module: it computes per-job
dimension scores and a decomposed ``overall`` fit score, but does NOT touch the
existing LLM scorer (``scorer.py``) -- a separate agent wires this into the
pipeline later.

Honesty rule: keywords the profile genuinely supports add to a dimension;
genuine gaps stay visible in ``warnings`` / ``gaps`` and are never stuffed.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# ── Dimension constants ────────────────────────────────────────────────────

DIMENSIONS: Tuple[str, ...] = (
    "technical_skills",
    "experience_match",
    "behavioral_culture",
    "career_alignment",
    "determination_prefs",
)

# Weights sum to 1.0 exactly.
DIMENSION_WEIGHTS: Dict[str, float] = {
    "technical_skills": 0.30,
    "experience_match": 0.25,
    "behavioral_culture": 0.15,
    "career_alignment": 0.15,
    "determination_prefs": 0.15,
}

# Per-dimension rubric bands: (name, lo, hi, meaning); score is in band when
# ``lo <= score < hi`` so the bands tile 0-100 without overlap.
_RUBRIC_BANDS: Tuple[Tuple[str, int, int, str], ...] = (
    ("excellent", 80, 100, "direct, hands-on fit with little or no ramp-up"),
    ("strong", 60, 80, "clear fit; small gaps easily bridged"),
    ("moderate", 40, 60, "partial fit; notable gaps, relevant background present"),
    ("weak", 20, 40, "limited fit; significant gaps or mismatched direction"),
    ("poor", 0, 20, "poor fit; largely mismatched requirements or experience"),
)

_DIM_MEANING: Dict[str, str] = {
    "technical_skills": (
        "how well the profile's declared skills cover the job's core "
        "requirements (languages, frameworks, databases, tools, devops)"
    ),
    "experience_match": (
        "how well the nature/function of the work matches the profile (not "
        "literal titles); years signalled vs years owned"
    ),
    "behavioral_culture": (
        "soft-skill and team-fit signals the job asks for that the profile "
        "genuinely supports or explicitly declines"
    ),
    "career_alignment": (
        "target role, seniority, career goals, and location/time fit"
    ),
    "determination_prefs": (
        "how many of the profile's stated preferences/deal-breakers the job "
        "satisfies"
    ),
}


def _band_for(score: int) -> str:
    for name, lo, hi, _m in _RUBRIC_BANDS:
        if lo <= score < hi:
            return name
    return "poor"


def rubric(dimension: str) -> Dict[str, Tuple[int, int, str]]:
    """Return the 0-100 rubric for a dimension as {band: (lo, hi, meaning)}."""
    dim = dimension if dimension in DIMENSIONS else dimension.lower()
    # validate against DIMENSIONS (or accept any string for leniency)
    base = _DIM_MEANING.get(dim, "dimension fit rubric")
    out: Dict[str, Tuple[int, int, str]] = {}
    for band, lo, hi, meaning in _RUBRIC_BANDS:
        out[band] = (lo, hi, f"{meaning}. [{base}]")
    return out


def rubric_band(dimension: str, score: int) -> str:
    """Short human label for a score on a dimension (e.g. 'strong')."""
    return _band_for(score)


def _clamp(v: float) -> int:
    return max(0, min(100, int(round(v))))


# ── Text / matching helpers ────────────────────────────────────────────────


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9][a-z0-9 ./+_#-]*", (text or "").lower())


def _matches(candidate: List[str], corpus: List[str]) -> List[str]:
    """Candidate terms that appear as a phrase in the corpus (boundary match)."""
    corp = " ".join(corpus)
    hits: List[str] = []
    for term in candidate:
        t = term.strip().lower()
        if not t:
            continue
        if re.search(r"(^|[\s,./_(])" + re.escape(t) + r"($|[\s,./_)])", corp):
            hits.append(term)
    return hits


def _flat_profile_skills(profile: dict) -> Dict[str, List[str]]:
    """skills_boundary -> {category: [terms]} (only non-empty)."""
    sb = (profile or {}).get("skills_boundary", {}) or {}
    cats = ("languages", "frameworks", "devops", "databases", "tools")
    matrix: Dict[str, List[str]] = {}
    for c in cats:
        raw = sb.get(c, []) or []
        if isinstance(raw, str):
            raw = [raw]
        items: List[str] = []
        for it in raw:
            for w in re.findall(r"[a-z0-9][a-z0-9 ./+_-]*", str(it).lower()):
                w = w.strip().rstrip(".")
                if w:
                    items.append(w)
        if items:
            matrix[c] = items
    return matrix


def _years_required(job_text: str, description: str) -> Optional[int]:
    """Best-guess years-of-experience requirement, or None if not stated."""
    for hay in (description, job_text):
        for m in re.finditer(r"(\d{1,2})\s*\+?\s*years?\b", hay.lower()):
            v = int(m.group(1))
            if 1 <= v <= 40:
                return v
    return None


def _profile_years(profile: Dict) -> int:
    v = (profile or {}).get("experience", {}).get("years_of_experience_total", 0)
    try:
        return int(float(str(v)))
    except (TypeError, ValueError):
        return 0

# ── Dimension scorers ───────────────────────────────────────────────────────


def _job_pieces(job: dict) -> Tuple[List[str], str]:
    """Flatten a job dict into a token corpus + the full description string."""
    title = str(job.get("title") or "")
    location = str(job.get("location") or "")
    site = str(job.get("site") or "")
    desc = str(job.get("description") or "")
    full = str(job.get("full_description") or "")
    job_text = " ".join([title, site, location, desc, full])
    return _words(job_text.lower()), full or desc


def _score_technical(job_tokens: List[str], profile: dict) -> Tuple[int, List[str]]:
    """Core requirements covered by declared skills. Base 45, +matched terms."""
    matrix = _flat_profile_skills(profile)
    declared: List[str] = []
    for _cat, items in matrix.items():
        declared.extend(items)
    hits = _matches(declared, job_tokens)
    # Cap credit so no single dimension can overweight a keyword dump.
    bonus = min(len(hits) * 4, 50)
    score = _clamp(45 + bonus)
    return score, hits


def _score_experience(job_tokens: List[str], desc: str, profile: dict,
                      warnings: List[str]) -> Tuple[int, List[str]]:
    """Function/nature of work fit. Base 55 + role-family signal +/- years."""
    exp = (profile or {}).get("experience", {}) or {}
    current = str(exp.get("current_job_title") or "").lower()
    p_years = _profile_years(profile)
    family = ["engineer", "developer", "software", "data", "devops", "ops",
              "designer", "product", "manager", "analyst", "scientist",
              "frontend", "backend", "fullstack", "qa", "ml", "ai"]
    role_tokens = _words(current)
    # signal: does the nature of the job overlap the profile's function words?
    signal = 0
    for t in job_tokens:
        for kw in family:
            if kw in t:
                signal += 1
                break
    if role_tokens:
        signal += len(_matches(role_tokens, job_tokens)) * 2
    req_years = _years_required(" ".join(job_tokens), desc)
    years_gap = 0
    if req_years is not None and p_years < req_years:
        years_gap = req_years - p_years
        warnings.append(
            f"honesty gap: job signals ~{req_years}+ years of experience but the "
            f"profile declares {p_years}."
        )
    score = _clamp(55 + min(signal, 25) - years_gap * 5)
    return score, []  # rationale assembled by caller using warnings


def _score_behavioral(job_tokens: List[str], profile: dict,
                      gaps: List[str]) -> Tuple[int, List[str]]:
    """Soft / team-fit signals. Base 50 + (~)factors the profile genuinely has."""
    factors = (profile or {}).get("behavioral_factors", []) or []
    if isinstance(factors, str):
        factors = [factors]
    owned = _words(" ".join(factors))
    hit = _matches(owned, job_tokens)
    base = 50
    score = _clamp(base + min(len(factors) * 8, 30))
    gap_signal = [t for t in ["collaborative", "ownership", "fast-paced",
                              "communication", "leadership", "mentorship",
                              "autonomous", "agile"] if t in job_tokens]
    return score, gap_signal


def _score_career(job_tokens: List[str], job_raw: dict, profile: dict,
                  warnings: List[str]) -> Tuple[int, List[str]]:
    """Target role, seniority, career goals, location/time fit. Base 60."""
    exp = (profile or {}).get("experience", {}) or {}
    target = _words(str(exp.get("target_role") or ""))
    personal = (profile or {}).get("personal", {}) or {}
    city = str(personal.get("city") or "").lower()
    score = 60
    if target and _matches(target, job_tokens):
        score += 25
    loc = str(job_raw.get("location") or "").lower()
    if city and city and loc and city in loc:
        score += 10
    elif city and loc and re.search(r"(remote|hybrid)", " ".join(job_tokens).lower()):
        score += 5
    elif city and loc and loc.strip():
        warnings.append(
            f"honesty gap: job location '{loc.strip()}' does not match profile "
            f"city '{city}'"
        )
    return _clamp(score), warnings


def _score_prefs(job_tokens: List[str], profile: dict, gaps: List[str]
                 ) -> Tuple[int, List[str]]:
    """How many stated preferences the job satisfies. Base 60, pref-aware."""
    prefs = (profile or {}).get("preferences", []) or []
    if isinstance(prefs, str):
        prefs = [prefs]
    if not prefs:
        return 60, gaps
    satisfied = 0
    for p in prefs:
        for t in _words(p):
            if t in job_tokens:
                satisfied += 1
                break
    score = _clamp(round(100 * satisfied / len(prefs)))
    miss = [p for p in prefs if not any(t in job_tokens for t in _words(p))]
    for p in miss:
        gaps.append(f"honesty gap: preference '{p}' is not met by this job.")
    return score, gaps

# ── Deal-breakers ───────────────────────────────────────────────────────────


def dealbreaker_hit(job_text: str, rule: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    """Return True when the job text triggers the hard deal-breaker ``rule``.

    A rule is a mapping with at least ``id`` and a ``check`` callable of
    signature ``(job_text: str, profile: dict) -> bool`` (a one-arg check is
    also tolerated).  ``describe`` is a human explanation.  The built-in set
    is overridable via ``profile["hdeal_breakers"]``.
    """
    check = rule.get("check")
    if not callable(check):
        return False
    try:
        return bool(check(job_text, profile))
    except TypeError:
        return bool(check(job_text))


# ── Default hard deal-breakers ─────────────────────────────────────────────

HARD_DEAL_BREAKERS: Tuple[str, ...] = (
    "eligibility",
    "work_authorization",
    "salary_floor",
    "language",
    "on_call",
    "travel",
)


def _default_dealrules(profile: dict) -> Dict[str, Dict[str, Any]]:
    wa = (profile or {}).get("work_authorization", {}) or {}
    comp = (profile or {}).get("compensation", {}) or {}

    def _authorized(job: str, prof: dict) -> bool:
        ra = str(wa.get("legally_authorized_to_work", "")).lower().startswith("yes")
        need = str(wa.get("require_sponsorship", "")).lower().startswith("yes")
        if (not ra or need) and re.search(
            r"(must be .*authorized|authorization to work|can[^ ]* work|sponsorship)", job
        ):
            return True
        return False

    def _citizenship(job: str, prof: dict) -> bool:
        return bool(re.search(
            r"(must be a (us|u\.s\.|canadian|uk|european|eU) citizen|"
            r"security clearance|top.?secret clearance)", job, re.I))

    def _salary_floor(job: str, prof: dict) -> bool:
        try:
            floor = float(comp.get("salary_range_min") or comp.get("salary_expectation") or 0)
        except (TypeError, ValueError):
            floor = 0.0
        if floor <= 0:
            return False
        # "$90,000" or "$90000"
        m = re.search(r"\$(\d{2,3}(?:,\d{3})+)\b", job)
        if m:
            if float(m.group(1).replace(",", "")) < floor:
                return True
            return False
        # "90k" or "$90k"
        m = re.search(r"(?:\$)?(\d{1,2})\s*k\b", job.lower())
        if m and float(m.group(1)) * 1000 < floor:
            return True
        return False

    def _language(job: str, prof: dict) -> bool:
        sb = (prof or {}).get("skills_boundary", {}) or {}
        langs = {x.lower() for x in (sb.get("languages") or []) if isinstance(x, str)}
        for m in re.finditer(r"(fluent in|proficient in|native)\s+([a-zA-Z]+)\b", job, re.I):
            need = m.group(2).lower()
            if need not in langs:
                return True
        return False

    def _on_call(job: str, prof: dict) -> bool:
        return bool(re.search(r"\bon[- ]?call\b", job, re.I))

    def _travel(job: str, prof: dict) -> bool:
        return bool(re.search(
            r"(travel (up to )?(\d{2,3}\s*%|50%|100%)|frequent travel|on the road)", job, re.I))

    def _remote_only(job: str, prof: dict) -> bool:
        if re.search(r"(remote only|100% remote|fully remote)", job, re.I):
            return bool(re.search(r"onsite|hybrid", job, re.I))
        return False

    return {
        "eligibility": {"id": "eligibility", "check": _citizenship,
                        "describe": "requires citizenship/clearance the profile lacks"},
        "work_authorization": {"id": "work_authorization", "check": _authorized,
                               "describe": "requires legal authorization/sponsorship the profile cannot provide"},
        "salary_floor": {"id": "salary_floor", "check": _salary_floor,
                         "describe": "posted salary is below the profile's compensation floor"},
        "language": {"id": "language", "check": _language,
                     "describe": "requires a language not present in the profile"},
        "on_call": {"id": "on_call", "check": _on_call,
                    "describe": "requires on-call duties the profile treats as a hard veto"},
        "travel": {"id": "travel", "check": _travel,
                   "describe": "requires heavy or frequent travel"},
        "remote_only": {"id": "remote_only", "check": _remote_only,
                        "describe": "remote-only role conflicts with the profile's onsite/hybrid stance"},
    }


def deal_candidates(job_text: str, job: dict, profile: dict
                    ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Return ``(hits, active_ids)`` — every triggered deal-breaker as a dict
    with ``id``/``describe`` (so callers get an honest audit trail).

    ``profile["hdeal_breakers"] = [...]`` replaces the default set entirely;
    ``profile["preferences"] = [...]`` may also name built-ins like "on_call".
    """
    table = _default_deal_breakers(profile)
    overrides = (profile or {}).get("hdeal_breakers", None)
    prefs = (profile or {}).get("preferences", []) or []
    if isinstance(prefs, str):
        prefs = [prefs]

    # Merge built-in ids the profile's preferences explicitly reference.
    extra_ids = [str(x) for x in prefs if str(x).lower().replace(" ", "_") in table]

    if overrides is not None:
        if isinstance(overrides, str):
            overrides = [overrides]
        active: List[Dict[str, Any]] = []
        for o in overrides:
            key = str(o).strip()
            base = key.lower().replace(" ", "_")
            if base in table:
                rule = dict(table[base])
            else:
                rule = {"id": base, "describe": f"profile override: {key}",
                        "check": lambda j, p, k=key.lower(): k in j.lower()}
            active.append(rule)
    else:
        active = []
        ids = list(HARD_DEAL_BREAKERS)
        for pid in extra_ids(ids, prefs):
            if pid in table:
                active.append(dict(table[pid]))

    hits: List[Dict[str, Any]] = []
    for r in active:
        if dealbreaker_hit(job_text, r, profile):
            hits.append({k: v for k, v in r.items() if k != "check"})
    active_ids = [r["id"] for r in active if "id" in r]
    return hits, active_ids


def _default_break(profile: dict) -> Dict[str, Dict[str, Any]]:
    """Deprecated alias kept for compatibility; maps to the canonical table."""
    return _default_deal_breakers(profile)

T