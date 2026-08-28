"""JobPilot document-quality, reviewer, and untrusted-input safety.

Pure-logic, stdlib-only module. Another agent wires these into the pipeline
(tailor -> cover -> apply) later. Nothing here touches the filesystem, network,
or an LLM.

A) ATS text-layer checks  -- ``ats_check``, ``check_ascii_dates``
B) Reviewer second pass   -- ``reviewer_pass``, ``revise``
C) Untrusted-input safety -- ``sanitize_posting``, ``no_follow_links``
"""
from __future__ import annotations

import base64
import binascii
import re

# ---------------------------------------------------------------------------
# Shared regex helpers
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_HIDDEN_INVISIBLE = re.compile(r"[\u200b-\u200d\u2028-\u202f\ufeff\u2060-\u2064\u00ad]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_REPLACEMENT_CHAR = "\ufffd"  # the U+FFFD replacement char (the '�')
_NDASH = "\u2013"
_RANSOM_GLYPHS = re.compile(r"[\u2212\u00a0\u00b7\u2022\u2026\u2014]")

_GENERIC_PHRASES = [
    "passionate about", "hard-working", "team player", "results-oriented",
    "self-starter", "detail-oriented", "strong communication skills",
    "strong work ethic", "dynamic", "motivated",
]

_STRENGTH_LABELS = [
    "expertise", "expert", "extensive", "deep knowledge", "senior", "lead",
    "proficient", "fluent", "specialis", "mastered", "years of experience",
    "years of exposure",
]

_INJECT_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+)?previous", re.IGNORECASE),
    re.compile(r"ignore\s+(?:the\s+)?(?:prior|above|previous|original)\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+prior\s+instructions?", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\b", re.IGNORECASE),
    # Narrowed to require an AI-persona object so ordinary job-posting
    # boilerplate ("You are a self-starter...", "You are an experienced
    # engineer...") is not treated as an injection attempt. The separate
    # you_are_now pattern above already covers role-reassignment framing
    # ("you are now the recruiter's assistant").
    re.compile(
        r"you\s+are\s+(?:an?\s+)?(?:ai|assistant|agent|system|chatbot|bot|"
        r"language\s+model|llm|gpt)\b",
        re.IGNORECASE,
    ),
    re.compile(r"act\s+as\s+(?:an?\s+)?(?:AI|assistant|agent|system)", re.IGNORECASE),
    re.compile(r"include\s+this", re.IGNORECASE),
    re.compile(r"remember\s+to\s+include", re.IGNORECASE),
    re.compile(r"be\s+sure\s+to\s+include", re.IGNORECASE),
    re.compile(r"begin\s+(?:your|the)\s+response", re.IGNORECASE),
    re.compile(r"say\s+(?:yes|ok)\s+to", re.IGNORECASE),
    re.compile(r"repeat\s+(?:the\s+)?(?:word|phrase|statement)", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
    re.compile(r"<\|?(?:start|system|end|user)>\|?", re.IGNORECASE),
    re.compile(r"do\s+not\s+tell\s+(?:them|anyone|the user|the recruiter)", re.IGNORECASE),
    re.compile(r"jailbreak", re.IGNORECASE),
    re.compile(r"override\s+previous", re.IGNORECASE),
]

# a run of base64-alphabet characters possibly split by padding/space
_BLOB_RUN_RE = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")


def _find_email(text):
    m = _EMAIL_RE.search(text or "")
    return m.group(0) if m else None


def _find_phone(text):
    m = re.search(r"(?<!\d)(?:\+?\d[\d\s().-]{6,19}\d)", text or "")
    return m.group(0).strip() if m else None


def _urls_in(text):
    return [m.group(0) for m in _URL_RE.finditer(text or "")]


def _is_b64ish(s):
    t = s.strip()
    if len(t) < 16 or re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", t) is None:
        return False
    # Very long unbroken base64-alphabet runs (>=40 chars) are almost certainly
    # obfuscated payloads, not prose - flag even when they do not cleanly decode.
    if len(t) >= 40:
        return True
    try:
        base64.b64decode(t, validate=True)
        return True
    except (binascii.Error, ValueError):
        return False


def _is_hexish(s):
    t = s.strip()
    return len(t) >= 40 and re.fullmatch(r"[0-9a-fA-F]+", t) is not None


# ---------------------------------------------------------------------------
# A) ATS text-layer check
# ---------------------------------------------------------------------------


def check_ascii_dates(text):
    """Flag/remediate the ai-Release v1.3.0 NDASH bug: date/word ranges rendered
    with a mid-line en-dash (2021\u20132024, May\u2013Jun) instead of an ASCII hyphen.
    ATS extractors split on the en-dash and corrupt the range.

    Returns (found_issue, fixed_text).
    """
    if not text:
        return False, text
    found = [False]

    def _year(m):
        found[0] = True
        return m.group(0).replace(_NDASH, "-")

    def _word(m):
        found[0] = True
        return m.group(0).replace(_NDASH, "-")

    fixed = re.sub(r"[0-9]{4}" + _NDASH + r"[0-9]{2,4}", _year, text)
    fixed = re.sub(r"(?<=\w)" + _NDASH + r"(?=\w)", _word, fixed)
    return found[0], fixed


def _section_issues(text):
    if not text:
        return []
    low = text.lower()

    def pos(label, *tokens):
        terms = (label,) + tokens
        hits = [low.find(t) for t in terms if low.find(t) != -1]
        return min(hits) if hits else None

    order = [
        ("summary", pos("summary", "profile summary", "objective", "professional summary")),
        ("education", pos("education", "academic", "qualifications", "degrees")),
        ("experience", pos("experience", "professional experience", "work history", "employment")),
        ("skills", pos("skills", "technical skills", "core competencies")),
    ]
    named = [(k, v) for k, v in order if v is not None]
    out = []
    for (a, ai), (b, bi) in zip(named, named[1:]):
        if ai > bi:
            out.append("reading order: section '%s' appears after '%s' (possible interleave)" % (a, b))
    return out


def ats_check(doc_text, contact, job_keywords, genuine_supported=None):
    """Verify what an ATS parser would see in the RAW text layer.

    Guards: contact literals (email + phone), sane section order, no mid-doc
    ransom glyphs (en-dash, U+FFFD, control chars) that corrupt date/email/phone,
    and an HONEST keyword split: matched requires (a) the profile genuinely
    supports the keyword (genuine_supported) AND (b) it is visibly in the text.
    A keyword the profile does not genuinely back stays a gap (honesty, never stuffed).

    Returns {ok, issues, keyword_coverage:{matched,gaps}, extraction_warnings}.
    """
    if doc_text is None:
        doc_text = ""
    if not job_keywords:
        job_keywords = []
    if not genuine_supported:
        genuine_supported = []

    issues = []
    warnings = []
    text = doc_text

    personal = contact.get("personal") if isinstance(contact, dict) else None
    if isinstance(personal, dict):
        contact = dict(contact)
        contact.update(personal)
    email = (contact or {}).get("email")
    phone = (contact or {}).get("phone")

    if email:
        if email.lower() not in text.lower():
            detected = _find_email(text)
            if detected is None:
                issues.append("contact: email '%s' not found in ATS text layer" % email)
            else:
                warnings.append("contact: expected '%s' differs from extracted '%s' -- likely glyph/unicode rewrite" % (email, detected))
    if phone:
        digits = re.sub(r"[^\d]", "", phone)
        if digits and re.search(re.escape(digits), re.sub(r"[^\d]", "", text)) is None:
            found_phone = _find_phone(text)
            msg = "contact: phone '%s' not found in ATS text layer" % phone
            if found_phone:
                msg += " (found '%s' instead)" % found_phone
            issues.append(msg)
    if not email and not phone:
        warnings.append("contact: no email/phone provided in contact dict; nothing to verify")

    if _REPLACEMENT_CHAR in text:
        issues.append("glyph: replacement char 'U+FFFD' (the '\ufffd') present in text layer")
    ndash_found, _fixed = check_ascii_dates(text)
    if ndash_found:
        warnings.append("glyph: en-dash (U+2013) inside date/word ranges (ai-Release v1.3.0 NDASH bug); ATS date extraction may corrupt. Use check_ascii_dates().")
    if _CTRL_RE.search(text):
        issues.append("glyph: control characters present in ATS text layer")
    for ch in _RANSOM_GLYPHS.findall(text):
        issues.append("glyph: non-ASCII punctuation '%s' (U+%04X) may corrupt parsing" % (ch, ord(ch)))
        break

    issues.extend(_section_issues(text))

    text_low = text.lower()
    genuine = {k.lower() for k in genuine_supported}
    matched = []
    gaps = []
    for kw in job_keywords:
        k = (kw or "").lower()
        if not k:
            continue
        in_doc = k in text_low
        supported = k in genuine
        if supported and in_doc:
            matched.append(kw)
        else:
            gaps.append(kw)

    return {
        "ok": not issues,
        "issues": issues,
        "keyword_coverage": {"matched": matched, "gaps": gaps},
        "extraction_warnings": warnings,
    }


# ---------------------------------------------------------------------------
# B) Reviewer second pass
# ---------------------------------------------------------------------------


def _knowledge_from_profile(profile):
    """Explicit skill boundary + resume facts (what the profile genuinely claims)."""
    if not isinstance(profile, dict):
        return set()
    out = set()
    skills = profile.get("skills_boundary") or {}
    for group in ("languages", "frameworks", "devops", "databases", "tools", "soft_skills", "certifications"):
        out.update(str(s).lower() for s in (skills.get(group) or []))
    resume = profile.get("resume_facts") or {}
    for key in ("preserved_companies", "preserved_projects", "preserved_school", "real_metrics"):
        v = resume.get(key)
        if isinstance(v, list):
            out.update(str(x).lower() for x in v)
    return out


def _posting_keywords(posting):
    text = "%s\n%s" % (posting.get("title", ""), posting.get("description", ""))
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_.#+-]{1,40}", text)
    stop = {"and", "or", "the", "with", "will", "for", "have", "has", "are",
            "this", "that", "from", "into", "work", "role", "team", "candidate",
            "your", "resume", "cover", "letter", "apply", "you", "years", "year",
            "need", "needed", "developer", "engineer"}
    seen = set()
    out = []
    for t in tokens:
        tl = t.lower()
        if tl in stop or tl in seen:
            continue
        seen.add(tl)
        out.append(t)
    return out


def _overclaims(doc_text, supported):
    if not doc_text:
        return []
    low = doc_text.lower()
    findings = []
    label_re = "|".join(re.escape(l) for l in _STRENGTH_LABELS)
    for m in re.finditer(r"\b(" + label_re + r")\b[^.\n;]{0,45}?\b([A-Za-z][A-Za-z0-9_.-]+)\b", low):
        chased = m.group(2)
        if chased.lower() in supported:
            continue
        if chased.lower() in {"years", "year", "experience", "exposure", "skill",
                              "role", "field", "tools", "technologies", "concept", "expertise"}:
            continue
        findings.append({
            "severity": "high",
            "aspect": "overclaim",
            "note": "honesty: '%s' claims '%s' the profile does not back" % (m.group(0).strip(), chased),
            "fix": "remove or soften the strength claim on '%s'" % chased,
        })
    return findings


def reviewer_pass(drafts, posting, profile):
    """Second, fresh-context critique of each draft.

    drafts maps label -> text, e.g. {"cv": ..., "cover_letter": ...}.
    Against posting + profile, flags per document: genuinely-supported keywords
    missed, over claims (a phrase claiming a skill/experience the profile does
    NOT back), generic language repeated across every cohort, structure concerns.

    Returns {"reviews": [{"doc": label, "issues": [{severity,aspect,note,fix}]}]}.
    """
    drafts = drafts or {}
    posting = posting or {}
    profile = profile or {}
    supported = _knowledge_from_profile(profile)
    posting_kws = _posting_keywords(posting)

    reviews = []
    for label, text in drafts.items():
        text = text or ""
        low = text.lower()
        issues = []
        for kw in posting_kws:
            if kw.lower() in supported and kw.lower() not in low:
                issues.append({
                    "severity": "medium",
                    "aspect": "missed-keyword",
                    "note": "posting keyword '%s' the profile genuinely supports is absent from '%s'" % (kw, label),
                    "fix": "weave '%s' in truthfully" % kw,
                })
        issues.extend(_overclaims(text, supported))
        for phrase in _GENERIC_PHRASES:
            if phrase in low:
                issues.append({
                    "severity": "low",
                    "aspect": "generic-language",
                    "note": "'%s' is a generic phrase recruiters see repeated" % phrase,
                    "fix": "replace with a concrete, measurable example",
                })
        if low.strip() == "":
            issues.append({"severity": "high", "aspect": "structure",
                           "note": "'%s' is empty" % label, "fix": "provide draft text"})
        elif label.lower() in {"cv", "cv-file", "resume"} and len(text.split()) < 150:
            issues.append({"severity": "low", "aspect": "structure",
                           "note": "'%s' is short (%d words)" % (label, len(text.split())),
                           "fix": "add concrete metrics"})
        reviews.append({"doc": label, "issues": issues})
    return {"reviews": reviews}


# Low-risk, whitelist-only, context-safe swaps for ``revise``.
_LOW_RISK_SWAPS = [
    ("passionate about", "interested in"),
    ("team player", "collaborator"),
    ("results-oriented", "results-focused"),
    ("self-starter", "able to work independently"),
]


def revise(drafts, critiques):
    """Apply only low-risk, whitelisted fixes to each draft and log them.

    Always applies the check_ascii_dates NDASH fix. Applies the generic-phrase
    swaps ONLY when the reviewer's issue list carries the generic-language
    aspect for that doc. Overclaim / structure edits need human judgement and
    are never auto-applied.

    Returns {"revised": {label: text}, "changelog": [{"doc": label, "changes": [
        {"rule": ..., "count": n}]}]}.
    """
    drafts = drafts or {}
    reviews = (critiques or {}).get("reviews", []) if isinstance(critiques, dict) else []
    generic_flagged = {}
    for review in reviews:
        doc = review.get("doc")
        flagged = set()
        for i in review.get("issues", []):
            if i.get("aspect") == "generic-language":
                note = i.get("note", "")
                if "'" in note:
                    flagged.add(note.split("'")[1])
        generic_flagged.setdefault(doc, set()).update(flagged)

    revised = {}
    change_log = []
    for label, text in drafts.items():
        out = check_ascii_dates(text or "")[1]
        changes = []
        for phrase, replacement in _LOW_RISK_SWAPS:
            if phrase not in generic_flagged.get(label, set()):
                continue
            count = 0
            buf = out
            while phrase.lower() in buf.lower():
                idx = buf.lower().find(phrase.lower())
                buf = buf[:idx] + replacement + buf[idx + len(phrase):]
                count += 1
                if count > 100:
                    break
            if count:
                out = buf
                changes.append({"rule": "swap:%s->%s" % (phrase, replacement), "count": count})
        if changes:
            change_log.append({"doc": label, "changes": changes})
        revised[label] = out
    return {"revised": revised, "changelog": change_log}


# ---------------------------------------------------------------------------
# C) Untrusted-input safety
# ---------------------------------------------------------------------------


def no_follow_links(posting, allowed_url=None):
    """List the URLs found in a posting body that the workflow must NOT fetch.

    The pipeline is only allowed to fetch the single board URL the user confirmed
    (allowed_url, if given). Every other URL in the body is returned and must
    never be followed.
    """
    urls = _urls_in(posting or "")
    if allowed_url:
        return [u for u in urls if u.rstrip("/") != allowed_url.rstrip("/")]
    return urls


def sanitize_posting(raw_posting):
    """Treat posting text as DATA, never as instructions.

    Strips embedded instructions (ignore previous, you are now, include this,
    hidden injection patterns), base64/hex blobs, stray URLs, and Unicode
    invisibles. Returns {cleaned, removed:[items], flags:[...]}.

    The pipeline must treat cleaned as plain text and never let it steer the
    agent: the confirmed board URL is the only fetch allowed (see no_follow_links).
    """
    raw = raw_posting if isinstance(raw_posting, str) else str(raw_posting or "")
    removed = []
    flags = []
    cleaned_lines = []

    for line in raw.split("\n"):
        text = line
        # remove each injection phrase in place; keep the rest of the line so
        # legitimate content (and any URL/blob tokens) are still handled.
        for pat in _INJECT_PATTERNS:
            while True:
                m = pat.search(text)
                if not m:
                    break
                removed.append("injection:%s" % m.group(0)[:60])
                flags.append("instruction-attempt")
                text = text.replace(m.group(0), "", 1)

        urls = _urls_in(text)
        for u in urls:
            removed.append("url:%s" % u)
        if urls:
            flags.append("url-present")
        text = _URL_RE.sub("", text)

        if _HIDDEN_INVISIBLE.search(text):
            removed.append("invisible-characters:%r" % (_HIDDEN_INVISIBLE.findall(text)[:4],))
            text = _HIDDEN_INVISIBLE.sub("", text)
            flags.append("hidden-injection")

        for run in _BLOB_RUN_RE.findall(text):
            if _is_b64ish(run) or _is_hexish(run):
                removed.append("blob:%s..." % run[:40])
                text = text.replace(run, "")
                flags.append("data-blob")

        text = text.strip()
        if text:
            cleaned_lines.append(text)

    seen = set()
    unique_flags = []
    for f in flags:
        if f not in seen:
            seen.add(f)
            unique_flags.append(f)
    unique_removed = list(dict.fromkeys(removed))
    return {"cleaned": "\n".join(cleaned_lines), "removed": unique_removed, "flags": unique_flags}
