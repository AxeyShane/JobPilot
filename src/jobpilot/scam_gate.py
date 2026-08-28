"""Scam job posting detection gate and LLM tie-break.

Heuristic pre-filter for known scam patterns (upfront payment/fees, payment processing
money mule schemes, premature SSN/bank details) and LLM tie-break for ambiguous
signals (off-platform contact pivots, too-good-to-be-true framing).

Fail-closed on error: if the LLM tie-break errors out, scam_verdict is left NULL (None)
rather than defaulting to 'clear' or 'blocked'.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from jobpilot.llm import LLMClient, get_client

log = logging.getLogger(__name__)

_WS = re.compile(r"\s+")

# ---------------------------------------------------------------------------
# Heuristic Patterns
# ---------------------------------------------------------------------------

# Strong match patterns: essentially never legitimate. Block immediately, no LLM call.
STRONG_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    (
        "payment_fee",
        re.compile(
            r"\b(?:processing|training|registration|application|equipment|onboarding|background\s+check)\s+fees?\b"
            r"|\bpurchase\s+your\s+own\s+equipment\b"
            r"|\bbuy\s+(?:your\s+own\s+)?equipment\s+from\s+(?:us|our\s+vendor|approved\s+vendor)\b"
            r"|\bpay\s+(?:for\s+)?(?:the\s+)?(?:background\s+check|training|registration|equipment|materials)\b"
            r"|\bequipment\s+deposit\b"
            r"|\bupfront\s+(?:payment|fee|cost|deposit)s?\b"
            r"|\bfee\s+required\s+to\s+apply\b",
            re.IGNORECASE,
        ),
        "Upfront payment or fee requested from applicant",
    ),
    (
        "bank_account_processing",
        re.compile(
            r"\b(?:personal\s+)?bank\s+account\s+for\s+payment\s+processing\b"
            r"|\bprovide\s+(?:your\s+)?(?:personal\s+)?bank\s+account\s+for\s+payment\b"
            r"|\buse\s+your\s+(?:personal\s+)?bank\s+account\s+(?:to|for)\b"
            r"|\b(?:receive|accept)\s+(?:funds|payments|checks|cheques|transfers|money)\s+"
            r"(?:in|into|to|through|via)\s+your\s+(?:personal\s+)?(?:bank\s+account|account)\b"
            r"|\b(?:deposit|cash)\s+(?:the\s+)?(?:check|cheque|cashier'?s\s+check)\s+"
            r"(?:and|then)\s+(?:wire|transfer|send|forward|remit|keep|buy)\b"
            r"|\bcheck[- ]cashing\b"
            r"|\bmoney\s+mule\b"
            r"|\bwire\s+funds\s+(?:back|to|via)\s+(?:western\s+union|moneygram)\b"
            r"|\b(?:pay|paid|receive\s+payment)\s+(?:in|via|through|using)\s+gift\s+cards?\b"
            r"|\bpurchase\s+(?:crypto|cryptocurrency|bitcoin|gift\s+cards?)\s+with\s+"
            r"(?:the\s+)?(?:funds|check|money)\b",
            re.IGNORECASE,
        ),
        "Personal bank account requested for payment processing or money transfer scheme",
    ),
    (
        "premature_sensitive_info",
        re.compile(
            r"\b(?:provide|submit|send|enter|require[s]?)\s+(?:your\s+)?"
            r"(?:ssn|social\s+security\s+number|sin|social\s+insurance\s+number)\b"
            r"|\b(?:ssn|social\s+security\s+number|social\s+insurance\s+number)\s+(?:is\s+)?"
            r"required\s+(?:to\s+apply|for\s+application|upfront|before\s+(?:an\s+|the\s+)?interview)\b"
            r"|\b(?:provide|submit|send)\s+(?:your\s+)?"
            r"(?:bank\s+routing\s+number|routing\s+number\s+and\s+account\s+number|credit\s+card\s+number)\b"
            r"|\b(?:provide|submit|send|upload)\s+(?:a\s+)?(?:copy\s+of\s+)?"
            r"(?:passport|driver'?s\s+license|government\s+id)\s+"
            r"(?:to\s+apply|with\s+application|before\s+(?:an\s+|the\s+)?interview)\b",
            re.IGNORECASE,
        ),
        "Premature request for SSN, banking details, or government ID before interview",
    ),
]

# Soft match patterns: escalate to LLM tie-break (false-positive risk if blocked on regex alone).
SOFT_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    (
        "off_platform_contact",
        re.compile(
            r"\b(?:contact|reach\s+out|message|send\s+(?:your\s+)?(?:cv|resume)|dm|chat)\s+"
            r"(?:us|me|recruiter|hiring\s+manager)?\s*(?:on|via|through|at)\s+(?:whatsapp|telegram|signal)\b"
            r"|\b(?:whatsapp|telegram|signal)\s*(?::|#|\+|number|username|handle|@)\s*[+\w@]+"
            r"|\bconfirm\s+your\s+(?:teams|skype|zoom)\s+email(?:,|\s+if\s+different\s+from\s+your\s+cv\s+email)?\b"
            r"|\bsecond(?:ary)?\s+(?:email|contact\s+channel|teams\s+email)\b"
            r"|\b(?:teams|skype)\s+interview\s+code\b",
            re.IGNORECASE,
        ),
        "Off-platform contact channel pivot (WhatsApp, Telegram, or disconnected second channel)",
    ),
    (
        "too_good_to_be_true",
        re.compile(
            r"\bno\s+interview\s+required\b"
            r"|\bguaranteed\s+(?:hire|job|employment|income|placement)\b"
            r"|\bstart\s+today\b"
            r"|\bstart\s+immediately\s+(?:with\s+)?no\s+experience\b"
            r"|\bearn\s+\$\d+(?:,\d+)?\s+(?:daily|per\s+day|weekly|per\s+week)\s+with\s+no\s+experience\b"
            r"|\bflexible,\s*work\s+around\s+your\s+existing\s+commitments\b",
            re.IGNORECASE,
        ),
        "Too-good-to-be-true or generic HR-mill phrasing",
    ),
]


def _quote_sentence(text: str, span: tuple[int, int], max_chars: int = 240) -> str:
    """Extract the sentence or surrounding span containing the match."""
    start, end = span
    text = text or ""
    lo = text.rfind(".", 0, start)
    hi = text.find(".", end)
    seg = text[(lo + 1 if lo >= 0 else 0):(hi + 1 if hi >= 0 else len(text))]
    seg = _WS.sub(" ", seg).strip()
    return seg[:max_chars] or text[start:end]


# ---------------------------------------------------------------------------
# LLM Tie-Break
# ---------------------------------------------------------------------------

_scam_client_instance: LLMClient | None = None


def get_scam_client() -> LLMClient:
    """Return LLM client for scam tie-break using SCAM_LLM_* env prefix with fallback."""
    global _scam_client_instance
    if _scam_client_instance is None:
        openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        openai_key = os.environ.get("OPENAI_API_KEY", "")
        model_override = os.environ.get("SCAM_LLM_MODEL", "") or os.environ.get("SCORE_LLM_MODEL", "")

        scam_url = os.environ.get("SCAM_LLM_URL", "") or os.environ.get("SCORE_LLM_URL", "")
        if scam_url:
            base_url = scam_url.rstrip("/")
            model = model_override or "local-model"
            api_key = (
                os.environ.get("SCAM_LLM_API_KEY", "")
                or os.environ.get("SCORE_LLM_API_KEY", "")
                or os.environ.get("TAILOR_LLM_API_KEY", "")
            )
            _scam_client_instance = LLMClient(base_url, model, api_key, timeout=120)
        elif openrouter_key:
            _scam_client_instance = LLMClient(
                "https://openrouter.ai/api/v1",
                model_override or "google/gemini-2.5-flash-lite",
                openrouter_key,
                timeout=120,
            )
        elif gemini_key:
            _scam_client_instance = LLMClient(
                "https://generativelanguage.googleapis.com/v1beta/openai",
                model_override or "gemini-2.0-flash",
                gemini_key,
                timeout=120,
            )
        elif openai_key:
            _scam_client_instance = LLMClient(
                "https://api.openai.com/v1",
                model_override or "gpt-4o-mini",
                openai_key,
                timeout=120,
            )
        else:
            return get_client()

    return _scam_client_instance


SCAM_TIEBREAK_PROMPT = """You are an expert scam job detection reviewer for JobPilot.

A scraped job posting triggered soft heuristic flags:
{flags_summary}

JOB POSTING TEXT:
{text}

Task:
Determine if this job posting is a LEGITIMATE opportunity or a SCAM / TASK SCAM / MONEY MULE / PHISHING attempt.
Legitimate job postings from real companies (even with unconventional perks or standard Teams interviews) are "legit".
Scams (off-platform pivots to Telegram/WhatsApp, vague task scams, money mule schemes, fake checks) are "suspicious".

Return ONLY a JSON object:
{{"verdict": "legit" | "suspicious", "reason": "<one-sentence explanation>"}}
"""


def llm_tie_break(
    text: str,
    soft_matches: list[dict[str, Any]],
    client: LLMClient | None = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Perform LLM tie-break for ambiguous / soft-flagged postings.

    Fail-closed: returns (None, [error_dict]) if transport, API, or parsing fails.
    """
    flags_summary = "\n".join(
        f"- {m.get('category')}: {m.get('reason')} (quoted: \"{m.get('quoted')}\")"
        for m in soft_matches
    )
    prompt = SCAM_TIEBREAK_PROMPT.format(
        flags_summary=flags_summary,
        text=text[:8000],
    )

    try:
        llm = client or get_scam_client()
        raw = llm.ask(prompt, temperature=0.0, max_tokens=512)

        # Parse JSON
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            log.error("Scam LLM tie-break returned unparseable output: %s", raw[:200])
            return None, [{"category": "error", "error": f"Unparseable LLM output: {raw[:100]}"}]

        data = json.loads(m.group(0))
        verdict_str = str(data.get("verdict", "")).strip().lower()
        reason_str = str(data.get("reason", "")).strip()

        if verdict_str in ("suspicious", "scam", "blocked"):
            reasons = [
                {
                    "category": "llm_tie_break",
                    "reason": reason_str or "LLM tie-break classified posting as suspicious",
                    "quoted": soft_matches[0]["quoted"] if soft_matches else text[:120],
                }
            ]
            # Also preserve soft match reasons
            for sm in soft_matches:
                if sm not in reasons:
                    reasons.append(sm)
            return "blocked", reasons
        elif verdict_str in ("legit", "clear", "clean", "valid"):
            return "clear", []
        else:
            log.error("Scam LLM tie-break unknown verdict: %s", verdict_str)
            return None, [{"category": "error", "error": f"Unknown LLM verdict: {verdict_str}"}]

    except Exception as exc:
        log.error("Scam LLM tie-break failed: %s", exc)
        return None, [{"category": "error", "error": f"LLM error: {str(exc)}"}]


# ---------------------------------------------------------------------------
# Core Gate Function
# ---------------------------------------------------------------------------

def evaluate_scam_posting(
    text: str | None,
    client: LLMClient | None = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Evaluate job posting text for scam patterns.

    Returns:
        (verdict, reasons)
        verdict: 'clear' | 'blocked' | None (pending / LLM error, fail-closed)
        reasons: list of {"category": str, "quoted": str, "reason": str}
    """
    if not text or not text.strip():
        return "clear", []

    # 1. Fast path: Check strong patterns (blocks immediately, no LLM call)
    strong_hits: list[dict[str, Any]] = []
    for cat, pattern, desc in STRONG_PATTERNS:
        for match in pattern.finditer(text):
            span = match.span()
            quoted = _quote_sentence(text, span)
            strong_hits.append({
                "category": cat,
                "quoted": quoted,
                "reason": desc,
            })

    if strong_hits:
        log.info("Scam gate strong match (%d hit(s)): %s", len(strong_hits), strong_hits[0]["category"])
        return "blocked", strong_hits

    # 2. Check soft patterns (escalates to LLM tie-break)
    soft_hits: list[dict[str, Any]] = []
    for cat, pattern, desc in SOFT_PATTERNS:
        for match in pattern.finditer(text):
            span = match.span()
            quoted = _quote_sentence(text, span)
            soft_hits.append({
                "category": cat,
                "quoted": quoted,
                "reason": desc,
            })

    if soft_hits:
        log.info("Scam gate soft match (%d hit(s)), escalating to LLM tie-break", len(soft_hits))
        return llm_tie_break(text, soft_hits, client=client)

    # 3. Clean path: no hits
    return "clear", []


# ---------------------------------------------------------------------------
# Reported Signature Match Helper
# ---------------------------------------------------------------------------

def _tokenize_words(text: str) -> list[str]:
    """Tokenize text into lowercase word tokens."""
    return re.findall(r"\b[a-zA-Z0-9_'-]+\b", (text or "").lower())


def _token_ngrams(tokens: list[str], n: int = 3) -> set[tuple[str, ...]]:
    """Generate n-grams from a list of tokens."""
    if len(tokens) < n:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def match_reported_signature(
    posting_text: str | None,
    signatures: list[dict[str, Any] | str] | None = None,
    threshold: float = 0.50,
    db_conn: Any = None,
) -> dict[str, Any] | None:
    """Check if posting text matches any known reported scam signature.

    Word-boundary and token-aware: computes token n-gram Jaccard similarity and
    token overlap, avoiding naive substring containment.

    Args:
        posting_text: The job posting text to check.
        signatures: Optional list of signatures (strings or dicts with 'signature'/'source_text').
                    If None, queries reported_signatures table in DB.
        threshold: Jaccard similarity threshold for n-gram match (default 0.50).
        db_conn: Optional SQLite connection.

    Returns:
        Dict with match details or None if no match.
    """
    if not posting_text:
        return None

    posting_tokens = _tokenize_words(posting_text)
    if len(posting_tokens) < 10:
        return None

    posting_ngrams = _token_ngrams(posting_tokens, n=3)
    if not posting_ngrams:
        return None

    sig_records = signatures
    if sig_records is None:
        try:
            from jobpilot.database import get_connection
            conn = db_conn or get_connection()
            rows = conn.execute(
                "SELECT id, company, domain, signature, source_text, note FROM reported_signatures"
            ).fetchall()
            sig_records = [
                {
                    "id": row[0],
                    "company": row[1],
                    "domain": row[2],
                    "signature": row[3],
                    "source_text": row[4],
                    "note": row[5],
                }
                for row in rows
            ]
        except Exception:
            sig_records = []

    if not sig_records:
        return None

    for item in sig_records:
        if isinstance(item, str):
            sig_text = item
            sig_id = None
            note = None
        else:
            sig_text = item.get("source_text") or item.get("signature") or ""
            sig_id = item.get("id")
            note = item.get("note")

        if not sig_text:
            continue

        sig_tokens = _tokenize_words(sig_text)
        if len(sig_tokens) < 10:
            continue

        sig_ngrams = _token_ngrams(sig_tokens, n=3)
        if not sig_ngrams:
            continue

        intersection = posting_ngrams.intersection(sig_ngrams)
        union = posting_ngrams.union(sig_ngrams)
        sim = len(intersection) / len(union) if union else 0.0
        containment = len(intersection) / len(sig_ngrams) if sig_ngrams else 0.0

        if sim >= threshold or containment >= 0.65:
            return {
                "matched": True,
                "signature_id": sig_id,
                "similarity": round(max(sim, containment), 3),
                "reason": f"Matches reported scam signature (similarity: {round(max(sim, containment), 2)})",
                "note": note,
            }

    return None
