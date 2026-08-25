"""JobPilot outcomes feedback — pure-stdlib data layer + logic for real outcomes.

This is the closed feedback loop that lets JobPilot learn from REAL application
outcomes (a clone of the ai-job-search outcome/gmail-sync loop, ported into the
pure-python layer): a schema + idempotent migration, an upsert record API, an
aggregate recalibrate() producing trainer "lessons", and a beta heuristic
"status-ring" detector plus drafted->applied promotion. No network, no heavy
deps (sqlite3, re, json, pathlib, dataclasses/typing only).

--------------------------------------------------------------------------
SINGLE-SOURCE-OF-TRUTH STATUS VOCABULARY (from ai-job-search v1.0.0)
--------------------------------------------------------------------------
Writers/receivers accept BOTH canonical spellings of a status on read and
always store the canonical form. The canonical statuses live in
:data:`OUTCOME_STATUSES`. Known spelling pairs unified by
:func:`_normalize_status`:

    "no response"   <-> "no_response"
    "offer declined" <-> "offer_declined"

For those two pairs either spelling stored in the DB, or passed to a reader,
always reads back as the canonical token ("no_response" / "offer_declined"),
so a mixed database never double-counts or mislabels a status.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from jobpilot.config import DB_PATH

# --- single source of truth: canonical outcome statuses ---------------------
OUTCOME_STATUSES = frozenset({
    "applied", "drafted", "waiting", "assessment", "interview",
    "offer", "accepted", "rejected", "no_response", "closed", "offer_declined",
})

# The subset surfaced by outcomes_summary().
_SUMMARY_KEYS = (
    "applied", "drafted", "waiting", "interview", "offer",
    "accepted", "rejected", "no_response", "closed",
)

# Spelling variants -> canonical token. Single-token collapse is applied first.
_SPELLING_VARIANTS = {
    "no response": "no_response",
    "no_response": "no_response",
    "offer declined": "offer_declined",
    "offer_declined": "offer_declined",
}

_COLUMNS = (
    "url", "status", "status_date", "source", "notes",
    "interview_rounds", "offer_amount", "offer_currency",
    "rejected_reason", "created_at", "updated_at", "raw",
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS outcomes (
    url              TEXT PRIMARY KEY,
    status           TEXT,
    status_date      TEXT,
    source           TEXT,      -- 'manual' | 'gmail' | 'notion'
    notes            TEXT,
    interview_rounds INTEGER,
    offer_amount     TEXT,
    offer_currency   TEXT,
    rejected_reason  TEXT,
    created_at       TEXT,
    updated_at       TEXT,
    raw              TEXT       -- JSON payload
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: Optional[Path | str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _normalize_status(raw: Optional[str]) -> Optional[str]:
    """Map any accepted spelling onto its canonical OUTCOME_STATUSES value."""
    if raw is None:
        return None
    collapsed = " ".join(str(raw).lower().split())
    return _SPELLING_VARIANTS.get(collapsed, collapsed)


# ---------------------------------------------------------------------------
# A) Schema + persistence
# ---------------------------------------------------------------------------

def init_outcomes(conn: Optional[sqlite3.Connection] = None,
                  db_path: Optional[Path | str] = None) -> sqlite3.Connection:
    """Idempotent migration creating the ``outcomes`` table.

    Uses CREATE TABLE IF NOT EXISTS, so it is safe to call on every startup.
    Ensures the parent directory of the DB exists first.
    """
    conn = conn or _connect(db_path)
    db_path = str(db_path or DB_PATH)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn.execute(_SCHEMA_SQL)
    conn.commit()
    return conn


def record_outcome(conn: Optional[sqlite3.Connection],
                   url: str,
                   **fields: Any) -> dict:
    """Upsert a single outcome row for a job URL. Returns the stored row.

    Unknown keyword fields are ignored. ``status`` is normalized through
    :func:`_normalize_status`. ``created_at`` is set on first insert only
    (preserved across upserts); ``updated_at`` is always bumped to now unless
    supplied. ``raw`` may be any JSON-serialisable object.
    """
    conn = conn or _connect()
    if not url:
        raise ValueError("url is required")
    now = _now()

    values: dict[str, Any] = {c: fields.get(c) for c in _COLUMNS}
    values["url"] = url
    values["status"] = _normalize_status(fields.get("status"))
    values["created_at"] = fields.get("created_at") or now
    values["updated_at"] = fields.get("updated_at") or now
    if "raw" in fields and not isinstance(fields["raw"], str):
        values["raw"] = json.dumps(fields["raw"], ensure_ascii=True)

    cols = ",".join(_COLUMNS)
    ph = ",".join("?" for _ in _COLUMNS)
    conn.execute(
        f"INSERT INTO outcomes ({cols}) VALUES ({ph}) "
        f"ON CONFLICT(url) DO UPDATE SET "
        f"status=excluded.status, status_date=excluded.status_date, source=excluded.source, "
        f"notes=excluded.notes, interview_rounds=excluded.interview_rounds, "
        f"offer_amount=excluded.offer_amount, offer_currency=excluded.offer_currency, "
        f"rejected_reason=excluded.rejected_reason, updated_at=excluded.updated_at, "
        f"raw=excluded.raw",
        [values[c] for c in _COLUMNS],
    )
    conn.commit()
    row = get_outcome(conn, url)
    assert row is not None
    return row


def get_outcome(conn: Optional[sqlite3.Connection], url: str) -> Optional[dict]:
    """Return the (normalized) outcome row for ``url``, or None."""
    conn = conn or _connect()
    row = conn.execute("SELECT * FROM outcomes WHERE url = ?", (url,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["status"] = _normalize_status(d.get("status"))
    return d


def outcomes_summary(conn: Optional[sqlite3.Connection] = None) -> dict:
    """Return outcome counts keyed by canonical status, plus ``total``.

    Counts are normalised through :func:`_normalize_status`, so rows written as
    either spelling ("no response" or "no_response") count into the same bin.
    """
    conn = conn or _connect()
    summary: dict[str, int] = {k: 0 for k in _SUMMARY_KEYS}
    for row in conn.execute("SELECT status FROM outcomes"):
        s = _normalize_status(row["status"])
        if s in _SUMMARY_KEYS:
            summary[s] += 1
    summary["total"] = sum(summary[k] for k in _SUMMARY_KEYS)
    return summary


# ---------------------------------------------------------------------------
# C) Beta "status-ring" detector (heuristic, pure function) + draft promotion
# ---------------------------------------------------------------------------

_KEYWORD_BUCKETS: dict[str, list[str]] = {
    "rejected": [
        "we regret", "regret to inform", "not moving forward",
        "not be moving forward", "decided to move forward with other",
        "other candidates", "not selected", "unfortunately",
        "no longer under consideration", "did not select", "declined",
    ],
    "interview": [
        "invite you to an interview", "schedule an interview", "interviewer",
        "video screen", "phone screen", "conduct an interview", "recruiter touch",
        "meet the team", "round 1", "to discuss", "let us know your availability",
    ],
    "assessment": [
        "assessment", "coding challenge", "coding exercise", "take-home",
        "hackerrank", "codesignal", "technical exercise", "hirevue",
    ],
    "offer": [
        "congratulations", "pleased to offer", "excited to offer",
        "delighted to extend", "extend an offer", "offer letter",
        "welcome aboard", "we'd like to welcome", "salary of", "total compensation",
    ],
    "accepted": [
        "offer accepted", "accepted your offer", "accepted the offer",
        "joining us", "your start date", "day one", "onboarding details",
    ],
    "waiting": [
        "still reviewing", "in review", "under consideration",
        "longer than expected", "will update you", "keep you posted",
    ],
    "applied": [
        "received your application", "received your resume", "received your",
        "thank you for applying", "thanks for applying", "we received",
        "application is complete",
    ],
}

# Higher-status buckets win keyword-count ties.
_BUCKET_PRIORITY = (
    "rejected", "offer", "accepted", "interview", "assessment", "waiting", "applied",
)


def detect_from_signal(signal_text: str, role_label: str = "") -> dict:
    """Heuristic classifier turning a signal snippet into a candidate status.

    A pure function (no network, no email account). Returns a dict with
    ``status`` (canonical OUTCOME_STATUSES value or None), ``confidence``
    (0.0–1.0), ``matched`` (list of buckets that fired) and ``evidence`` (the
    exact keywords that matched for the winning bucket). ``role_label`` is
    reserved for future weighting and is not used during classification today.
    """
    text = (signal_text or "").lower()

    hits: dict[str, list[str]] = {}
    for status, keywords in _KEYWORD_BUCKETS.items():
        matched = [kw for kw in keywords if kw in text]
        if matched:
            hits[status] = matched

    if not hits:
        return {"status": None, "confidence": 0.0, "matched": [], "evidence": []}

    best = max(hits, key=lambda s: (len(hits[s]), _BUCKET_PRIORITY.index(s)))
    evidence = hits[best]
    confidence = min(1.0, 0.4 + 0.2 * min(len(evidence), 3))
    return {
        "status": best,
        "confidence": round(confidence, 2),
        "matched": list(hits.keys()),
        "evidence": evidence,
    }


def promote_draft(conn: Optional[sqlite3.Connection] | None,
                  url: str, signal: str) -> bool:
    """Turn an ack-signal into a real application (drafts never count as replied).

    If the job's apply_status is 'drafted' and the signal is detected as an
    acknowledgement ("applied" bucket), this flips apply_status to 'applied'
    (ai-v1.4.0 rule: drafts must never count as waiting/replied). Returns True
    if the flip happened, False otherwise.
    """
    detection = detect_from_signal(signal)
    if detection["status"] != "applied":
        return False
    conn = conn or _connect()
    row = conn.execute("SELECT apply_status FROM jobs WHERE url = ?", (url,)).fetchone()
    if row is None:
        return False
    current = _normalize_status(row[0])  # tuple-safe
    if current == "drafted":
        conn.execute("UPDATE jobs SET apply_status = 'applied' WHERE url = ?", (url,))
        conn.commit()
        return True
    return False


# ---------------------------------------------------------------------------
# B) Recalibration: aggregate trainer lessons from outcomes + job scores
# ---------------------------------------------------------------------------

def recalibrate(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Aggregate outcome feedback into per-score-band trainer "lessons".

    Joins outcomes to jobs on URL to get each outcome's fit_score, groups them
    into bands (0-3, 4-6, 7-8, 9-10) and computes interview / offer / accept
    rates per band. Returns a list of dicts (one per band with data) that a
    trainer can feed into the scoring prompt to fix over- and under-prediction,
    e.g. "high scores but low interview yield — may over-predict interviews".
    stdlib-only; return is meant for debugging / training, not wired into the scorer.
    """
    conn = conn or _connect()
    rows = conn.execute(
        "SELECT o.status AS st, j.fit_score AS score "
        "FROM outcomes o LEFT JOIN jobs j ON j.url = o.url"
    ).fetchall()
    if not rows:
        return []

    bands: dict[str, dict] = {}
    for st_raw, score in rows:
        st = _normalize_status(st_raw)
        if score is None or st not in OUTCOME_STATUSES:
            continue
        b = "0-3" if score <= 3 else "4-6" if score <= 6 else "7-8" if score <= 8 else "9-10"
        d = bands.setdefault(b, {"scores": [], "interview": 0, "offer": 0, "accept": 0})
        d["scores"].append(score)
        if st in ("interview", "offer", "accepted"):
            d["interview"] += 1
        if st in ("offer", "accepted"):
            d["offer"] += 1
        if st == "accepted":
            d["accept"] += 1

    lessons: list[dict] = []
    for b in ("0-3", "4-6", "7-8", "9-10"):
        d = bands.get(b)
        if not d:
            continue
        n = len(d["scores"])
        avg = round(sum(d["scores"]) / n, 2)
        ir, offer, acc = (round(x / n, 2) for x in (d["interview"], d["offer"], d["accept"]))
        lessons.append({
            "band": b,
            "n": n,
            "avg_fit_score": avg,
            "interview_rate": ir,
            "offer_rate": offer,
            "accept_rate": acc,
            "lesson": _band_lesson(b, ir, offer),
        })
    return lessons


def _band_lesson(band: str, interview_rate: float, offer_rate: float) -> str:
    if offer_rate >= 0.3:
        return f"{band}: band historically converts — keep priority high."
    if band in ("7-8", "9-10") and interview_rate < 0.25:
        return f"{band}: high scores, low interview yield — scoring may over-predict interviews."
    if interview_rate > 0.6:
        return f"{band}: strong interview signal — scoring may under-predict interviews here."
    if interview_rate == 0:
        return f"{band}: no interviews observed yet — collecting data."
    return f"{band}: neutral signal — continue collecting outcomes."
