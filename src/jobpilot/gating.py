"""JOBPILOT pre-score hard gates.

Pure-logic, stdlib-only gates (re, json, dataclasses, typing) that run BEFORE any
scoring:

A) ELIGIBILITY GATE -- classify a posting's eligibility / work-rights section verbatim
   against the candidate's ``work_authorization`` profile dict.

B) LANGUAGE GATE -- classify a posting's language REQUIREMENTS (a job condition, not the
   ad's written language) against the candidate's declared languages.

The gates never rely on the network or third-party packages, so they are trivially
unit-testable. They are honest by construction: every verdict carries a ``reason`` and
FAIL is never silently dropped -- ``quoted`` holds the exact posting wording.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Lowercase + collapse whitespace for heuristic matching."""
    return _WS.sub(" ", (text or "").lower()).strip()


def _quote_sentence(text: str, snippet: str, max_chars: int = 240) -> Optional[str]:
    """Return the sentence of ``text`` containing ``snippet``, verbatim (trimmed)."""
    text = text or ""
    m = re.search(re.escape(snippet), text, re.IGNORECASE)
    if not m:
        return None
    lo = text.rfind(".", 0, m.start())
    hi = text.find(".", m.end())
    seg = text[(lo + 1 if lo >= 0 else 0):(hi if hi >= 0 else len(text))]
    seg = _WS.sub(" ", seg).strip()
    return seg[:max_chars] or None


# --------------------------------------------------------------------------- #
# A) ELIGIBILITY GATE
# --------------------------------------------------------------------------- #

# (regex, description) for statuses the candidate does not hold unless proven.
_HARD_ELIGIBILITY = [
    (r"\bsecurity\s+clearance\b", "security clearance"),
    (r"\bmust\s+be\s+a(?:n)?\s+US\s+citizen\b", "US citizenship"),
    (r"\bUS\s+citizen(?:ship)?\b", "US citizenship"),
    (r"\bcitizenship(?:\s+or\s+permanent\s+resident)?\b", "citizenship / PR"),
    (r"\bpermanent\s+resident\b", "permanent residency"),
    (r"\bgreen\s+card\b", "green card"),
]

# Language that clearly welcomes/sponsors international talent.
_SPONSOR_OK = [
    "visa sponsorship", "we sponsor", "sponsor visas", "will sponsor",
    "sponsorship available", "offer visa", "offers visa", "h-1b", "h1b",
    "international applicants welcome", "international candidates welcome",
    "we welcome international", "work permit sponsorship",
]

# Right-to-work phrasings (a validity check the employer will perform).
_RIGHT_TO_WORK = [
    "legally authorized to work", "right to work", "authorized to work",
    "eligible to work", "valid work permit", "work authorization",
    "work authorisation",
]


@dataclass
class EligibilityGateVerdict:
    gate: str = "eligibility"
    verdict: str = ""            # PASS | FAIL | PROCEED | UNVERIFIED
    reason: str = ""
    quoted: Optional[str] = None
    details: str = ""


def evaluate_eligibility(text: str, work_authorization: dict) -> dict:
    """Classify a posting's eligibility / work-rights section.

    Args:
        text: job posting text (at least its eligibility section).
        work_authorization: profile dict with keys ``legally_authorized_to_work``
            (bool), ``require_sponsorship`` (bool), ``work_permit_type`` (str).

    Returns dict: {gate, verdict, reason, quoted, details}.
    """
    wa = work_authorization or {}
    legal: bool = bool(wa.get("legally_authorized_to_work", True))
    sponsor_needed: bool = bool(wa.get("require_sponsorship", False))
    permit: str = (wa.get("work_permit_type") or "").strip().lower()

    norm = _norm(text)
    out = EligibilityGateVerdict(verdict="", reason="", quoted=None, details="")

    # 1) Hard required status -> FAIL (quote exact wording).
    for pattern, desc in _HARD_ELIGIBILITY:
        if re.search(pattern, norm):
            out.verdict = "FAIL"
            out.details = f"Required status not held: {desc}."
            out.quoted = _quote_sentence(text, _first_match(pattern, norm))
            out.reason = (f"Posting requires {desc} (a status the candidate profile does "
                          f"not declare). Quoted: {out.quoted!r}")
            return asdict(out)

    # 2) Posting explicitly names permit type or offers sponsorship.
    if permit and permit and permit in norm:
        out.verdict = "PASS"
        out.details = f"Posting explicitly mentions permit type '{permit}'."
        out.quoted = _quote_sentence(text, permit)
        out.reason = (f"Posting explicitly names candidate's permit/work class '{permit}'. "
                      + ("Profile requires sponsorship; posting appears to allow the class."
                         if sponsor_needed else "Candidate is authorized for this class."))
        return asdict(out)

    if any(k in norm for k in _SPONSOR_OK):
        out.verdict = "PASS"
        out.details = "Posting sponsors visas or welcomes international applicants."
        kw = next(k for k in _SPONSOR_OK if k in norm)
        out.quoted = _quote_sentence(text, kw)
        out.reason = "Posting explicitly welcomes sponsorship/international applicants."
        return asdict(out)

    # 3) Right-to-work language: it is a verification, not a closed status.
    rt = next((k for k in _RIGHT_TO_WORK if k in norm), None)
    if rt:
        out.quoted = _quote_sentence(text, rt)
        if legal and not sponsor_needed:
            out.verdict = "PASS"
            out.reason = "Posting requires legal right to work; candidate is authorized."
        elif sponsor_needed:
            out.verdict = "PROCEED"
            out.reason = ("Posting requires legal right to work; candidate requires sponsorship "
                          "that the posting does not confirm. Verify at employer site.")
        else:
            out.verdict = "PROCEED"
            out.reason = "Posting requires legal right to work but candidate status is unconfirmed."
        out.details = f"Right-to-work language found: {rt}."
        return asdict(out)

    # 4) Silent -> UNVERIFIED (never silently dropped).
    out.verdict = "UNVERIFIED"
    out.quoted = None
    out.details = "No eligibility keywords found."
    out.reason = ("Posting silent on work-rights/eligibility; cannot verify. "
                  "Employer site may gate.")
    return asdict(out)


def _first_match(pattern: str, norm: str) -> str:
    m = re.search(pattern, norm)
    return m.group(0) if m else pattern


# --------------------------------------------------------------------------- #
# B) LANGUAGE GATE
# --------------------------------------------------------------------------- #

# Language level buckets used to compare candidate level vs posting bar.
_LEVEL_INDEX = {
    # native / fluent / expert / C-level
    "native": 4, "mother tongue": 4, "fluent": 4, "excellent": 3, "c2": 3,
    "c1": 3, "proficient": 3, "professional": 2, "advanced": 2, "c1+": 3,
    # business / professional / B2 / B1
    "business": 2, "professional working": 2, "b2": 2, "upper": 2,
    "conversational": 1, "b1": 1, "working knowledge": 1, "basic": 1, "a2": 1, "a1": 0,
}

# Level words to search for near the language name.
_LEVEL_WORDS = [
    "fluent", "native", "excellent", "proficient", "advanced", "professional",
    "business", "conversational", "working knowledge", "basic", "c2", "c1", "b2",
    "b1", "a2", "a1",
]


def _parse_level(segment: str) -> tuple[int, str]:
    """Score a text segment's language level. Returns (level_index, matched_word)."""
    seg = _norm(segment)
    for key, idx in sorted(_LEVEL_INDEX.items(), key=lambda kv: -len(kv[0])):
        if key in seg:
            return idx, key
    return -1, "unstated"


def _extract_language_requirements(text: str) -> list[dict[str, Any]]:
    """Heuristic scan for language requirements in text.

    Recognizes both ``"Language: level"`` and ``"level Language"`` orders, e.g.
    "fluent English" and "Korean: fluent". Returns {name, level, level_index, quoted}.
    """
    reqs: list[dict[str, Any]] = []
    LANGUAGES = _DEFAULT_LANGUAGES
    langs = set(LANGUAGES)
    for raw in (text or "").split("\n"):
        line = _norm(raw)
        for name in LANGUAGES:
            m = re.search(
                rf"\b{re.escape(name)}\b\s*[:\-]?\s*([^;,.\n]{{0,30}})", line)
            m2 = re.search(
                rf"\b([a-z0-9+\-/ ]{{0,40}})\s+{re.escape(name)}\b", line)
            phrase = None
            for mm in (m, m2):
                if not mm:
                    continue
                cand = " ".join(mm.group(1).split())
                # skip multi-language chains like "excellent french and german"
                others = langs - {name}
                if any(o in cand for o in others):
                    continue
                if any(w in cand.lower() for w in _LEVEL_WORDS) or _parse_level(cand)[0] >= 0:
                    phrase = cand or None
                    break
            if phrase is None:
                continue
            lvl, kw = _parse_level(phrase)
            if lvl < 0:
                continue
            reqs.append({"name": name.capitalize(), "level": phrase or kw,
                         "level_index": lvl, "quoted": _quote_sentence(raw, name)})
    return list({r["name"].lower(): r for r in reqs}.values())


_DEFAULT_LANGUAGES = [
    "english", "spanish", "french", "german", "mandarin", "chinese", "japanese",
    "portuguese", "italian", "dutch", "russian", "arabic", "korean", "hindi",
]


@dataclass
class LanguageGateVerdict:
    gate: str = "language"
    verdict: str = ""            # PASS | FAIL | FLAG
    language_details: list = field(default_factory=list)
    reason: str = ""


def _candidate_language_map(skills_languages: list) -> dict[str, dict[str, Any]]:
    """Turn ``skills_boundary.languages`` (dicts or strings) into a name->row map."""
    out: dict[str, dict[str, Any]] = {}
    for row in skills_languages or []:
        if isinstance(row, str):
            m = re.match(r"^\s*([A-Za-z]+)\s*[\-:]\s*(.*?)\s*$", row)
            if m:
                name, level = m.group(1), m.group(2)
            else:
                name, level = row, ""
        else:
            row = dict(row or {})
            name = str(row.get("language") or row.get("name") or "").strip()
            level = str(row.get("level") or row.get("proficiency") or "").strip()
        if not name:
            continue
        lvl, kw = _parse_level(level)
        out[name.lower()] = {"name": name, "level": level or kw, "level_index": lvl}
    return out


def evaluate_language(text: str, languages: list) -> dict:
    """Classify posting language REQUIREMENTS vs the candidate's declared languages.

    Args:
        text: job posting text.
        languages: profile ``skills_boundary.languages`` list of dicts or strings.

    Returns dict: {gate, verdict, language_details, reason}.
    """
    reqs = _extract_language_requirements(text)
    cand = _candidate_language_map(languages)
    details: list[dict[str, Any]] = []
    hard_fail: Optional[str] = None

    for r in reqs:
        name = r["name"].lower()
        row = cand.get(name)
        entry = {"language": r["name"], "required_level": r["level"],
                 "candidate_level": row["level"] if row else None,
                 "level_index": r["level_index"],
                 "candidate_index": row["level_index"] if row else -1}
        details.append(entry)
        if row is None:
            if hard_fail is None:
                hard_fail = f"Posting requires {r['name']} ('{r['level']}'); candidate has no such language declared."
            continue
        if r["level_index"] > row["level_index"] and r["level_index"] >= 0:
            entry["flag"] = "level_below_required"
            entry.setdefault("flag_reason", f"Posting bar '{r['level']}' exceeds candidate '{row['level']}'.")

    if hard_fail:
        return asdict(LanguageGateVerdict(
            verdict="FAIL",
            language_details=details,
            reason=hard_fail + " Hard stop unless candidate confirms otherwise.",
        ))
    if any(d.get("flag") == "level_below_required" for d in details):
        return asdict(LanguageGateVerdict(
            verdict="FLAG",
            language_details=details,
            reason="One or more language requirements exceed the candidate's declared level; proceed with caution.",
        ))
    return asdict(LanguageGateVerdict(
        verdict="PASS",
        language_details=details,
        reason="All posting language requirements are met by the candidate's declared languages.",
    ))


__all__ = ["evaluate_eligibility", "evaluate_language", "EligibilityGateVerdict",
           "LanguageGateVerdict", "asdict"]
