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
from typing import Any

# ── Dimension constants ────────────────────────────────────────────────────

DIMENSIONS: tuple[str, ...] = (
    "technical_skills",
    "experience_match",
    "behavioral_culture",
    "career_alignment",
    "determination_prefs",
)

# Weights exactly sum to 1.0.
DIMENSION_WEIGHTS: dict[str, float] = {
    "technical_skills": 0.30,
    "experience_match": 0.25,
    "behavioral_culture": 0.15,
    "career_alignment": 0.15,
    "determination_prefs": 0.15,
}

# Rubric bands (name, lo, hi, meaning); in-band when lo <= score < hi (tiles 0-100).
_BAND_BASE: tuple[tuple[str, int, int, str], ...] = (
    ("excellent", 80, 100, "direct, hands-on fit, little or no ramp-up"),
    ("strong", 60, 80, "clear fit; small gaps easily bridged"),
    ("moderate", 40, 60, "partial fit; notable gaps, relevant background"),
    ("weak", 20, 40, "limited fit; significant gaps or mismatch"),
    ("poor", 0, 20, "poor fit; largely mismatched requirements"),
)

_DIM_MEANING: dict[str, str] = {
    "technical_skills": (
        "how well the profile's declared skills (languages, frameworks, "
        "databases, devops, tools) cover the job's core requirements"
    ),
    "experience_match": (
        "how well the nature/function of the work matches the profile "
        "(not literal titles); years signalled vs years owned"
    ),
    "behavioral_culture": (
        "soft-skill and team-fit factors the job asks for and the profile "
        "genuinely supports or explicitly declines"
    ),
    "career_alignment": (
        "target role, seniority, career goals, location and time fit"
    ),
    "determination_prefs": (
        "how well the job satisfies the candidate's stated preferences "
        "and deal-breakers"
    ),
}


def rubric(dimension: str) -> dict[str, tuple[int, int, str]]:
    """Return the 0-100 rubric for a dimension as {band: (lo, hi, meaning)}."""
    base = _DIM_MEANING.get(dimension, _DIM_MEANING["technical_skills"])
    out: dict[str, tuple[int, int, str]] = {}
    for name, lo, hi, meaning in _BAND_BASE:
        out[name] = (lo, hi, f"{meaning}. {base}")
    return out


def rubric_band(dimension: str, score: int) -> str:
    """Human label for a dimension score, e.g. 'strong' or 'poor'."""
    for name, lo, hi, _m in _BAND_BASE:
        if lo <= score < hi:
            return name
    return "poor"


def _clamp(v: float) -> int:
    return max(0, min(100, int(round(v))))


# ── Text / matching helpers ────────────────────────────────────────────────


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9 ./+_#-]*", (text or "").lower())


def _matches(candidate: list[str], corpus: list[str]) -> list[str]:
    """Candidate terms that appear as a phrase in the corpus (boundary match)."""
    joined = " ".join(corpus)
    hits: list[str] = []
    for term in candidate:
        t = term.strip().lower()
        if not t:
            continue
        if re.search(r"(^|[\s,./_(])" + re.escape(t) + r"($|[\s,./_)])", joined):
            hits.append(term)
    return hits


def _flat_profile_skills(profile: dict) -> dict[str, list[str]]:
    """skills_boundary -> {category: [normalized terms]} (non-empty only)."""
    sb = (profile or {}).get("skills_boundary", {}) or {}
    cats = ("languages", "frameworks", "devops", "databases", "tools")
    matrix: dict[str, list[str]] = {}
    for c in cats:
        raw = sb.get(c, []) or []
        if isinstance(raw, str):
            raw = [raw]
        items: list[str] = []
        for it in raw:
            for w in re.findall(r"[a-z0-9][a-z0-9 ./+_-]*", str(it).lower()):
                w = w.strip().rstrip(".")
                if w:
                    items.append(w)
        if items:
            matrix[c] = items
    return matrix


def _years_required(job_text: str, description: str) -> int | None:
    """Best-effort years-of-experience requirement, or None if not stated."""
    for hay in (description, job_text):
        for m in re.finditer(r"(\d{1,2})\s*\+?\s*years?\b", hay.lower()):
            v = int(m.group(1))
            if 1 <= v <= 40:
                return v
    return None


def _profile_years(profile: dict) -> int:
    v = (profile or {}).get("experience", {}).get("years_of_experience_total", 0)
    try:
        return int(float(str(v)))
    except (TypeError, ValueError):
        return 0


def _job_pieces(job: dict) -> tuple[list[str], str]:
    """Flatten a job dict into a token corpus + full description text."""
    title = str(job.get("title") or "")
    location = str(job.get("location") or "")
    site = str(job.get("site") or "")
    desc = str(job.get("description") or "")
    full = str(job.get("full_description") or "")
    job_text = " ".join([title, site, location, desc, full]).lower()
    return _words(job_text), full or desc


def _raw_job_text(job: dict) -> str:
    """Raw (un-tokenized) job text preserving currency/punctuation for rules."""
    parts = [str(job.get(k) or "") for k in ("title", "site", "location",
                                             "description", "full_description")]
    return " ".join(parts)


# ── Dimension scorers ───────────────────────────────────────────────────────


def _score_technical(job_tokens: list[str], profile: dict) -> tuple[int, list[str]]:
    """Base 45; each declared skill the job requires adds credit (cap +50)."""
    declared: list[str] = []
    for _cat, items in _flat_profile_skills(profile).items():
        declared.extend(items)
    hits = _matches(declared, job_tokens)
    score = _clamp(45 + min(len(hits) * 4, 50))
    return score, hits


def _score_experience(job_tokens: list[str], desc: str, profile: dict,
                      warnings: list[str]) -> int:
    """Base 55 + role-family signal; a years shortfall is a clear penalty."""
    exp = (profile or {}).get("experience", {}) or {}
    current = str(exp.get("current_job_title") or "").lower()
    p_years = _profile_years(profile)
    family = ["engineer", "developer", "software", "data", "devops", "ops",
              "designer", "product", "manager", "analyst", "scientist"]
    signal = 0
    for t in job_tokens:
        if any(kw in t for kw in family):
            signal += 1
    if current:
        signal += len(_matches(_words(current), job_tokens)) * 2
    years_gap = 0
    req = _years_required(" ".join(job_tokens), desc)
    if req is not None and p_years < req:
        years_gap = req - p_years
        warnings.append(
            f"honesty gap: job signals ~{req}+ years of experience, but the "
            f"profile declares {p_years}."
        )
    return _clamp(55 + min(signal, 25) - years_gap * 5)


def _score_behavioral(job_tokens: list[str], profile: dict,
                      gaps: list[str]) -> int:
    """Base 50; declared behavioral factors add credit, ask-for signals tracked."""
    factors = (profile or {}).get("behavioral_factors", []) or []
    if isinstance(factors, str):
        factors = [factors]
    owned = _words(" ".join([str(f) for f in factors]) if factors else "")
    score = _clamp(50 + min(len(factors) * 6, 30))
    for want in ["collaboration", "communication", "ownership", "fast-paced",
                 "leadership", "autonomous"]:
        if want in job_tokens and not any(w in owned for w in _words(want)):
            gaps.append(f"honesty gap: job soft-skill '{want}' not explicit in profile.")
    return score


def _score_career(job_tokens: list[str], job: dict, profile: dict,
                  warnings: list[str]) -> int:
    """Base 60; target-role hit, location/time alignment, stay honest."""
    exp = (profile or {}).get("experience", {}) or {}
    target = _words(str(exp.get("target_role") or ""))
    personal = (profile or {}).get("personal", {}) or {}
    city = str(personal.get("city") or "").lower()
    score = 60
    if target and _matches(target, job_tokens):
        score += 25
    loc = str(job.get("location") or "").lower()
    blob = " ".join(job_tokens)
    if city and loc and city in loc:
        score += 10
    elif city and re.search(r"remote|hybrid", blob):
        score += 5
    elif city and loc.strip():
        warnings.append(f"honesty gap: job location '{loc.strip()}' != profile city '{city}'.")
    return _clamp(score)


def _score_prefs(job_tokens: list[str], profile: dict, gaps: list[str]) -> int:
    """How many stated preferences the job satisfies (proportional, honest)."""
    prefs = (profile or {}).get("preferences", []) or []
    if isinstance(prefs, str):
        prefs = [prefs]
    if not prefs:
        return 60
    satisfied = 0
    for p in prefs:
        # match individual keyword words (not grouped phrases) against the job
        p_tokens = [w for w in p.lower().split() if w]
        hit = any(any(w in tok for tok in job_tokens) for w in p_tokens)
        if hit:
            satisfied += 1
        else:
            gaps.append(f"honesty gap: preference '{p}' is not met by this job.")
    return _clamp(round(100 * satisfied / len(prefs)))
# ── Deal-breakers ───────────────────────────────────────────────────────────

# Default hard deal-breaker ids (each has a check in _default_deal_breakers).
DEFAULT_DEAL_BREAKER_IDS: tuple[str, ...] = (
    "eligibility",
    "work_authorization",
    "salary_floor",
    "language",
    "on_call",
    "travel",
)


def dealbreaker_hit(job_text: str, rule: dict[str, Any], profile: dict) -> bool:
    """True when the job text triggers the hard deal-breaker ``rule``.

    A rule is a mapping with an ``id`` and a ``check`` callable of signature
    ``(job_text, profile)`` (a one-argument check is tolerated too) plus an
    optional human ``describe``.  Honest because it never guesses profile
    facts -- it compares real declared profile fields to the job text.
    """
    check = rule.get("check")
    if not callable(check):
        return False
    try:
        return bool(check(job_text, profile))
    except TypeError:
        return bool(check(job_text))


def _default_deal_breakers(profile: dict) -> dict[str, dict[str, Any]]:
    comp = (profile or {}).get("compensation", {}) or {}

    def _eligibility(job: str, prof: dict) -> bool:
        return bool(re.search(
            r"(must be a (us|u\.s\.|canadian|uk|european) citizen|security clearance|top.?secret)",
            job, re.IGNORECASE))

    def _authorized(job: str, prof: dict) -> bool:
        wa = (prof or {}).get("work_authorization", {}) or {}
        ok = str(wa.get("legally_authorized_to_work", "")).lower().startswith("yes")
        need = str(wa.get("require_sponsorship", "")).lower().startswith("yes")
        if (not ok or need) and re.search(
                r"(authorization to work|authorized|sponsorship)", job, re.IGNORECASE):
            return True
        return False

    def _salary_floor(job: str, prof: dict) -> bool:
        try:
            floor = float(comp.get("salary_range_min") or comp.get("salary_expectation") or 0)
        except (TypeError, ValueError):
            floor = 0.0
        if floor <= 0:
            return False
        m = re.search(r"\$?(\d{2,3}(?:,\d{3})+)\b", job)
        if m:
            return float(m.group(1).replace(",", "")) < floor
        # "120k", "120k-140k", "$120k" -> require digits right before "k"
        m = re.search(r"(?:\$)?(\d{1,3})\s*k\b", job.lower())
        if m:
            return float(m.group(1)) * 1000 < floor
        return False

    def _language(job: str, prof: dict) -> bool:
        sb = (prof or {}).get("skills_boundary", {}) or {}
        langs = {x.lower() for x in (sb.get("languages") or []) if isinstance(x, str)}
        for m in re.finditer(r"(fluent in|fluent|proficient in|native)\s+([a-z]{2,})\b",
                             job, re.IGNORECASE):
            if m.group(2).lower() not in langs:
                return True
        return False

    def _on_call(job: str, prof: dict) -> bool:
        return bool(re.search(r"\bon[- ]?call\b", job, re.IGNORECASE))

    def _travel(job: str, prof: dict) -> bool:
        return bool(re.search(
            r"(travel (up to |required )?(\d{2,3}\s*%|50%|100%)|frequent travel|on the road)",
            job, re.IGNORECASE))

    def _remote_only(job: str, prof: dict) -> bool:
        if re.search(r"(remote only|100% remote|fully remote)", job, re.IGNORECASE):
            return bool(re.search(r"onsite|hybrid", job, re.IGNORECASE))
        return False

    return {
        "eligibility": {"id": "eligibility", "check": _eligibility,
                        "describe": "requires citizenship/clearance the profile lacks"},
        "work_authorization": {"id": "work_authorization", "check": _authorized,
                               "describe": "requires sponsorship/authorization the profile cannot provide"},
        "salary_floor": {"id": "salary_floor", "check": _salary_floor,
                         "describe": "posted salary is below the profile compensation floor"},
        "language": {"id": "language", "check": _language,
                     "describe": "requires a language not declared in the profile"},
        "on_call": {"id": "on_call", "check": _on_call,
                    "describe": "requires on-call duties the profile treats as a hard veto"},
        "travel": {"id": "travel", "check": _travel,
                   "describe": "requires heavy or frequent travel"},
        "remote_only": {"id": "remote_only", "check": _remote_only,
                        "describe": "remote-only role conflicts with an onsite/hybrid stance"},
    }


def deal_breakers(job_text: str, profile: dict) -> tuple[list[str], list[str]]:
    """Return ``(hit_ids, hit_descriptions)`` for all active deal-breakers.

    Active rules come from :data:`DEFAULT_DEAL_BREAKER_IDS` unless the profile
    overrides them with ``profile["hdeal_breakers"] = ["...", "..."]``, which
    replaces the default set. ``profile["preferences"]`` may also reference a
    built-in id (e.g. "no on-call") to force it on.
    """
    table = _default_deal_breakers(profile)
    overrides = (profile or {}).get("hdeal_breakers")
    prefs = (profile or {}).get("preferences", []) or []
    if isinstance(prefs, str):
        prefs = [prefs]
    forced = [str(p).lower().replace(" ", "_").replace("no_", "") for p in prefs]
    forced = [f for f in forced if f in table]

    if overrides is not None:
        raw = overrides if isinstance(overrides, list) else [overrides]
        ids: list[str] = []
        for o in raw:
            k = str(o).strip()
            lookup = k.lower().replace(" ", "_")
            ids.append(lookup)
    else:
        ids = list(DEFAULT_DEAL_BREAKER_IDS)
        for f in forced:
            if f not in ids:
                ids.append(f)

    hit_ids: list[str] = []
    descs: list[str] = []
    for key in ids:
        rule = table.get(key)
        if rule is None:
            continue
        if dealbreaker_hit(job_text, rule, profile):
            hit_ids.append(key)
            descs.append(rule.get("describe", key))
    return hit_ids, descs


# ── Top-level entry point ──────────────────────────────────────────────────

def score_dimensions(job: dict, profile: dict) -> dict:
    """Dimensioned, explainable fit evaluation of ``job`` for ``profile``.

    Args:
        job: mapping with optional ``title, location, description,
            full_description, site``.
        profile: mapping using the JobPilot profile shape (skills_boundary,
            experience{...}, compensation{...}, work_authorization{...}, plus
            optional ``preferences`` and ``hdeal_breakers``).

    Returns:
        {
          "dimensions": {dim: {"score": int, "rationale": str, "rubric": str}},
          "overall": int,          # weighted mean of the 5 dims (0 when vetoed)
          "composite": int,        # = overall (the number a pipeline may persist)
          "dealbreakers": list,    # non-empty description list when vetoed
          "deal_breakers": list,   # alias of dealbreakers (for clarity)
          "warnings": list,        # honest fit notes / soft gaps
          "gaps": list,            # explicit honesty gaps (never stuffed)
          "computed": bool,        # False -> do-not-score sentinel (deal-breaker veto)
        }

    Deal-breakers are a VETO: any hit forces ``overall == composite == 0``,
    ``computed == False`` and a non-empty ``dealbreakers`` / ``deal_breakers``.
    """
    warnings: list[str] = []
    gaps: list[str] = []
    job_tokens, desc = _job_pieces(job)
    job_text = _raw_job_text(job)

    tech, tech_hits = _score_technical(job_tokens, profile)
    exp = _score_experience(job_tokens, desc, profile, warnings)
    beh = _score_behavioral(job_tokens, profile, gaps)
    car = _score_career(job_tokens, job, profile, warnings)
    pre = _score_prefs(job_tokens, profile, gaps)

    dims = {
        "technical_skills": tech,
        "experience_match": exp,
        "behavioral_culture": beh,
        "career_alignment": car,
        "determination_prefs": pre,
    }

    hit_ids, hit_desc = deal_breakers(job_text, profile)
    vetoed = bool(hit_ids)

    # Rationales: honest, concrete, per dimension.
    def _rationale(name: str, score: int) -> str:
        band = rubric_band(name, score)
        if name == "technical_skills":
            extra = (
                f"Declared skills matched by the job: {', '.join(tech_hits) or 'none'}."
                if tech_hits else "No declared skill matched the job's core requirements."
            )
            return f"{band} ({score}/100). {extra}"
        if name == "experience_match":
            return f"{band} ({score}/100). Assessed by nature of work + years signal."
        if name == "behavioral_culture":
            return f"{band} ({score}/100). Soft-skill/team-fit factors considered."
        if name == "career_alignment":
            target = (profile or {}).get("experience", {}).get("target_role", "")
            return f"{band} ({score}/100). Target role '{target}' and location/time fit."
        return f"{band} ({score}/100). Proportion of stated preferences satisfied."

    dimensions_out: dict[str, dict[str, Any]] = {}
    for name, score in dims.items():
        dimensions_out[name] = {
            "score": score,
            "rationale": _rationale(name, score),
            "rubric": rubric_band(name, score),
        }

    # Veto
    if vetoed:
        overall = 0
        composite = 0
        warnings.append("VETOED: a hard deal-breaker applies (see dealbreakers).")
    else:
        overall = int(round(
            DIMENSION_WEIGHTS["technical_skills"] * tech
            + DIMENSION_WEIGHTS["experience_match"] * exp
            + DIMENSION_WEIGHTS["behavioral_culture"] * beh
            + DIMENSION_WEIGHTS["career_alignment"] * car
            + DIMENSION_WEIGHTS["determination_prefs"] * pre
        ))
        composite = overall

    return {
        "dimensions": dimensions_out,
        "overall": overall,
        "composite": composite,
        "dealbreakers": hit_desc,
        "deal_breakers": hit_desc,
        "warnings": warnings,
        "gaps": gaps,
        "computed": not vetoed,
    }

