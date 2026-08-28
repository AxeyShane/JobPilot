"""JobPilot scam report helpers and community sharing.

Handles local scam reporting, privacy-safe export to community format,
fetching community feeds with cached-copy fallback, and token-aware,
word-boundary-conscious signature matching.

Privacy Boundary (NON-NEGOTIABLE):
- Exported reports contain ONLY facts from scraped postings and sanitized notes.
- NEVER includes personal names, applicant emails/phones, CV details, tailored
  resumes, cover letters, or agent execution logs.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

log = logging.getLogger(__name__)

# Trigger patterns for candidate signature extraction
_SCAM_TRIGGER_PATTERNS = [
    r"personal bank account",
    r"bank account for payment",
    r"payment processing",
    r"wire transfer",
    r"equipment purchase",
    r"home office workstation",
    r"processing fee",
    r"training fee",
    r"registration fee",
    r"background check fee",
    r"security deposit",
    r"deposit (?:the |a |our )?check",
    r"cash (?:the |a |our )?check",
    r"gift card",
    r"e-transfer",
    r"telegram",
    r"whatsapp",
    r"no interview required",
    r"guaranteed hire",
    r"purchase your own equipment",
]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _get_connection(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    if conn is not None:
        return conn
    from jobpilot.database import get_connection
    return get_connection()


def init_reported_signatures(conn: sqlite3.Connection | None = None) -> None:
    """Ensure the reported_signatures table and jobs columns exist."""
    conn = _get_connection(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reported_signatures (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_url       TEXT,
            company       TEXT,
            domain        TEXT,
            signature     TEXT,
            source_text   TEXT,
            note          TEXT,
            reported_at   TEXT,
            source        TEXT DEFAULT 'local'
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reported_signatures_source ON reported_signatures(source)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reported_signatures_job_url ON reported_signatures(job_url)"
    )

    # Check reported_signatures columns for pattern_type
    sig_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(reported_signatures)").fetchall()
    }
    if "pattern_type" not in sig_cols:
        try:
            conn.execute("ALTER TABLE reported_signatures ADD COLUMN pattern_type TEXT")
        except Exception:
            pass

    # Ensure jobs table has scam columns if jobs table exists
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "jobs" in tables:
        job_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for col, col_type in (
            ("scam_verdict", "TEXT"),
            ("scam_reasons", "TEXT"),
            ("scam_checked_at", "TEXT"),
        ):
            if col not in job_cols:
                try:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

    conn.commit()


def sanitize_report_note(note: str) -> str:
    """Sanitize user note to prevent leaking PII into reports/exports."""
    if not note:
        return ""
    # Redact email addresses
    s = re.sub(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", "[REDACTED_EMAIL]", note)
    # Redact phone numbers
    s = re.sub(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", "[REDACTED_PHONE]", s)
    # Normalize whitespace
    s = " ".join(s.strip().split())
    return s[:500]


def sanitize_signature_text(text: str) -> str:
    """Normalize signature text by stripping excess whitespace and outer quotes."""
    if not text:
        return ""
    s = " ".join(text.strip().split())
    return s.strip("'\".,;")


def extract_domain(url_or_text: str | None) -> str:
    """Extract a clean domain name from a URL or raw text."""
    if not url_or_text:
        return ""
    try:
        if "://" in url_or_text:
            parsed = urlparse(url_or_text)
            netloc = parsed.netloc.split(":")[0].strip()
            if netloc:
                return netloc.lower()
        m = re.search(r"\b([a-zA-Z0-9][-a-zA-Z0-9]*\.[a-zA-Z]{2,})\b", url_or_text)
        if m:
            return m.group(1).lower()
    except Exception:
        pass
    return ""


def extract_signature_candidates(text: str, max_candidates: int = 3) -> list[str]:
    """Extract candidate signature sentences/clauses from job posting text."""
    if not text:
        return []

    # Split text into sentences / clauses
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    candidates: list[str] = []
    seen: set[str] = set()

    for pat in _SCAM_TRIGGER_PATTERNS:
        for sent in sentences:
            sent_clean = " ".join(sent.strip().split())
            if not sent_clean or len(sent_clean) < 15 or len(sent_clean) > 300:
                continue
            if re.search(r"\b" + pat + r"\b", sent_clean, re.IGNORECASE):
                norm = sanitize_signature_text(sent_clean)
                if norm and norm.lower() not in seen:
                    seen.add(norm.lower())
                    candidates.append(norm)
                    if len(candidates) >= max_candidates:
                        return candidates

    if not candidates:
        for sent in sentences:
            sent_clean = " ".join(sent.strip().split())
            if 20 <= len(sent_clean) <= 200:
                norm = sanitize_signature_text(sent_clean)
                if norm and norm.lower() not in seen:
                    seen.add(norm.lower())
                    candidates.append(norm)
                    if len(candidates) >= max_candidates:
                        break

    return candidates


def tokenize(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    """Tokenize text into lowercase words and return their (start, end) character spans."""
    tokens: list[str] = []
    spans: list[tuple[int, int]] = []
    for m in re.finditer(r"\b[a-zA-Z0-9_'-]+\b", text):
        tokens.append(m.group().lower())
        spans.append((m.start(), m.end()))
    return tokens, spans


def match_text_signature(text: str, signature: str, threshold: float = 0.80) -> tuple[bool, float, str]:
    """Word-boundary and token-aware match of a signature against posting text.

    Guarantees that plain substring containment NEVER triggers a false positive
    (e.g., 'wireless' will not match 'wire', 'coffee' will not match 'fee').

    Returns:
        (is_match, match_score, quoted_excerpt)
    """
    if not text or not signature:
        return False, 0.0, ""

    sig_clean = sanitize_signature_text(signature)
    if not sig_clean:
        return False, 0.0, ""

    # 1. Exact phrase with regex word boundaries (fast path)
    escaped = re.escape(sig_clean)
    pattern = rf"\b{escaped}\b"
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        return True, 1.0, m.group()

    # 2. Token-level fuzzy sliding window check
    text_tokens, text_spans = tokenize(text)
    sig_tokens, _ = tokenize(sig_clean)

    if not sig_tokens or not text_tokens:
        return False, 0.0, ""

    n_sig = len(sig_tokens)

    # Short signatures (< 3 tokens) require exact token sequence match on word boundaries
    if n_sig < 3:
        for i in range(len(text_tokens) - n_sig + 1):
            if text_tokens[i : i + n_sig] == sig_tokens:
                start_idx = text_spans[i][0]
                end_idx = text_spans[i + n_sig - 1][1]
                return True, 1.0, text[start_idx:end_idx]
        return False, 0.0, ""

    # For 3+ token signatures, use token overlap and sequence similarity over sliding windows
    min_w = max(3, n_sig - 2)
    max_w = min(len(text_tokens), n_sig + 3)
    sig_set = set(sig_tokens)

    best_ratio = 0.0
    best_quoted = ""

    for w in range(min_w, max_w + 1):
        for i in range(len(text_tokens) - w + 1):
            window = text_tokens[i : i + w]
            window_set = set(window)
            overlap = len(sig_set & window_set) / max(len(sig_set), len(window_set))
            if overlap >= threshold:
                seq_ratio = difflib.SequenceMatcher(None, sig_tokens, window).ratio()
                if seq_ratio >= threshold and seq_ratio > best_ratio:
                    best_ratio = seq_ratio
                    best_quoted = text[text_spans[i][0] : text_spans[i + w - 1][1]]

    if best_ratio >= threshold:
        return True, best_ratio, best_quoted

    return False, 0.0, ""


def get_active_signatures(
    conn: sqlite3.Connection | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve active scam signatures from the database."""
    conn = _get_connection(conn)
    init_reported_signatures(conn)

    query = "SELECT * FROM reported_signatures"
    params: list[Any] = []
    if source:
        if source in ("local", "local_user"):
            query += " WHERE source IN ('local', 'local_user')"
        elif source in ("community", "community_feed"):
            query += " WHERE source IN ('community', 'community_feed')"
        else:
            query += " WHERE source = ?"
            params.append(source)

    sig_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(reported_signatures)").fetchall()
    }
    if "reported_at" in sig_cols:
        query += " ORDER BY reported_at DESC"
    elif "created_at" in sig_cols:
        query += " ORDER BY created_at DESC"

    rows = conn.execute(query, params).fetchall()
    signatures: list[dict[str, Any]] = []
    for r in rows:
        row_dict = dict(r) if hasattr(r, "keys") else {}
        if not row_dict:
            col_list = [c[1] for c in conn.execute("PRAGMA table_info(reported_signatures)").fetchall()]
            row_dict = dict(zip(col_list, r))

        sig_text = row_dict.get("signature") or row_dict.get("signature_text") or row_dict.get("source_text") or ""
        norm_sig = " ".join(sig_text.strip().lower().split())
        stable_id = hashlib.sha256(norm_sig.encode("utf-8")).hexdigest() if norm_sig else str(row_dict.get("id", ""))

        signatures.append({
            "id": stable_id,
            "raw_id": row_dict.get("id"),
            "job_url": row_dict.get("job_url"),
            "company": row_dict.get("company") or "",
            "domain": row_dict.get("domain") or "",
            "signature_text": sig_text,
            "signature": sig_text,
            "source_text": row_dict.get("source_text") or sig_text,
            "note": row_dict.get("note") or "",
            "pattern_type": row_dict.get("pattern_type") or "user-reported",
            "source": row_dict.get("source") or "local_user",
            "reported_at": row_dict.get("reported_at") or row_dict.get("created_at") or "",
            "created_at": row_dict.get("reported_at") or row_dict.get("created_at") or "",
        })
    return signatures


def match_against_reported_signatures(
    text: str,
    conn: sqlite3.Connection | None = None,
    signatures: list[dict[str, Any]] | list[str] | None = None,
    threshold: float = 0.80,
) -> dict[str, Any] | None:
    """Check posting text against known reported scam signatures.

    Uses word-boundary and token-aware matching. Never matches on plain substrings.

    Returns:
        Dict with match details and category 'known-signature' if matched, else None.
    """
    if not text:
        return None

    active_sigs: list[dict[str, Any]] = []
    if signatures is not None:
        for s in signatures:
            if isinstance(s, str):
                norm = " ".join(s.strip().lower().split())
                active_sigs.append({
                    "id": hashlib.sha256(norm.encode("utf-8")).hexdigest(),
                    "signature_text": s,
                    "pattern_type": "known-signature",
                })
            elif isinstance(s, dict):
                active_sigs.append(s)
    else:
        active_sigs = get_active_signatures(conn)

    for sig in active_sigs:
        sig_text = sig.get("signature_text") or sig.get("signature") or ""
        matched, score, quoted = match_text_signature(text, sig_text, threshold=threshold)
        if matched:
            return {
                "matched": True,
                "signature_id": sig.get("id", ""),
                "signature_text": sig_text,
                "company": sig.get("company", ""),
                "domain": sig.get("domain", ""),
                "pattern_type": sig.get("pattern_type", "known-signature"),
                "score": round(score, 3),
                "quoted": quoted,
                "category": "known-signature",
            }

    return None


def record_report(
    job: dict[str, Any] | str,
    note: str = "",
    conn: sqlite3.Connection | None = None,
    snippet: str | None = None,
    pattern_type: str = "user-reported",
) -> dict[str, Any]:
    """Record a user scam report for a job.

    1. Sets jobs.scam_verdict = 'blocked'.
    2. Appends user-reported reason to jobs.scam_reasons.
    3. Extracts signatures from posting text (or snippet) and stores in reported_signatures.

    Returns:
        Dict with report summary.
    """
    conn = _get_connection(conn)
    init_reported_signatures(conn)

    # Resolve job dict
    job_dict: dict[str, Any] = {}
    job_url: str = ""
    if isinstance(job, str):
        job_url = job
        row = conn.execute("SELECT * FROM jobs WHERE url = ?", (job_url,)).fetchone()
        if row:
            job_dict = dict(zip(row.keys(), row)) if hasattr(row, "keys") else {"url": job_url}
        else:
            job_dict = {"url": job_url}
    elif isinstance(job, dict):
        job_dict = dict(job)
        job_url = str(job_dict.get("url") or "")

    sanitized_note = sanitize_report_note(note)
    domain = job_dict.get("domain") or extract_domain(job_dict.get("application_url") or job_url)
    company = job_dict.get("company") or job_dict.get("title") or ""
    full_text = str(
        job_dict.get("full_description")
        or job_dict.get("description")
        or job_dict.get("title")
        or ""
    )

    # Extract or use snippet for signature candidates
    sigs: list[str] = []
    if snippet:
        clean_snip = sanitize_signature_text(snippet)
        if clean_snip:
            sigs.append(clean_snip)

    if not sigs:
        sigs = extract_signature_candidates(full_text)
        if not sigs and full_text.strip():
            sigs = [sanitize_signature_text(full_text.strip()[:200])]

    # Update jobs table
    now_iso = _now()
    if job_url:
        row = conn.execute("SELECT scam_reasons FROM jobs WHERE url = ?", (job_url,)).fetchone()
        current_reasons: list[dict[str, Any]] = []
        if row and row[0]:
            try:
                current_reasons = json.loads(row[0])
                if not isinstance(current_reasons, list):
                    current_reasons = []
            except Exception:
                current_reasons = []

        new_reason: dict[str, Any] = {
            "category": "user-reported",
            "note": sanitized_note or "User reported scam posting",
        }
        if snippet:
            new_reason["quoted"] = snippet
        elif sigs:
            new_reason["quoted"] = sigs[0]

        current_reasons.append(new_reason)

        conn.execute(
            "UPDATE jobs SET scam_verdict = 'blocked', scam_reasons = ?, scam_checked_at = ? WHERE url = ?",
            (json.dumps(current_reasons), now_iso, job_url),
        )

    # Insert signatures into reported_signatures
    sig_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(reported_signatures)").fetchall()
    }
    inserted_ids: list[str] = []
    for sig in sigs:
        norm_sig = " ".join(sig.strip().lower().split())
        sig_hash = hashlib.sha256(norm_sig.encode("utf-8")).hexdigest()

        cols_to_insert = ["company", "domain", "note", "source"]
        vals_to_insert = [company or None, domain or None, sanitized_note, "local_user"]

        if "job_url" in sig_cols:
            cols_to_insert.append("job_url")
            vals_to_insert.append(job_url or None)
        if "signature" in sig_cols:
            cols_to_insert.append("signature")
            vals_to_insert.append(sig)
        if "source_text" in sig_cols:
            cols_to_insert.append("source_text")
            vals_to_insert.append(full_text[:500] if full_text else sig)
        if "signature_text" in sig_cols:
            cols_to_insert.append("signature_text")
            vals_to_insert.append(sig)
        if "reported_at" in sig_cols:
            cols_to_insert.append("reported_at")
            vals_to_insert.append(now_iso)
        if "created_at" in sig_cols:
            cols_to_insert.append("created_at")
            vals_to_insert.append(now_iso)
        if "pattern_type" in sig_cols:
            cols_to_insert.append("pattern_type")
            vals_to_insert.append(pattern_type)

        placeholders = ", ".join("?" for _ in cols_to_insert)
        col_names = ", ".join(cols_to_insert)
        conn.execute(
            f"INSERT INTO reported_signatures ({col_names}) VALUES ({placeholders})",
            vals_to_insert,
        )
        inserted_ids.append(sig_hash)

    conn.commit()

    return {
        "status": "blocked",
        "url": job_url,
        "signature_ids": inserted_ids,
        "signatures": sigs,
        "note": sanitized_note,
    }


def export_reports(
    conn: sqlite3.Connection | None = None,
    source: str | None = "local_user",
) -> list[dict[str, Any]]:
    """Export recorded scam reports in the community-file format shape.

    Guarantees strict privacy: no personal names, candidate PII, or internal logs.

    Returns:
        List of report dicts adhering to the version 1 schema.
    """
    conn = _get_connection(conn)
    init_reported_signatures(conn)

    sigs = get_active_signatures(conn, source=source)
    if not sigs:
        return []

    # Group signatures by job_url / grouping key
    groups: dict[str, list[dict[str, Any]]] = {}
    for s in sigs:
        key = s.get("job_url") or f"{s.get('company')}:{s.get('domain')}:{s.get('reported_at')}"
        groups.setdefault(key, []).append(s)

    reports: list[dict[str, Any]] = []
    for key, group_sigs in groups.items():
        first = group_sigs[0]
        job_url = first.get("job_url")
        note = first.get("note") or "User reported scam posting"
        source_channel = "local-report"

        if job_url:
            row = conn.execute(
                "SELECT scam_reasons, site FROM jobs WHERE url = ?",
                (job_url,),
            ).fetchone()
            if row:
                if row[0]:
                    try:
                        reasons = json.loads(row[0])
                        for r in reasons:
                            if isinstance(r, dict) and r.get("category") == "user-reported":
                                n = r.get("note")
                                if n:
                                    note = sanitize_report_note(n)
                                    break
                    except Exception:
                        pass
                if row[1]:
                    source_channel = f"{str(row[1]).lower()}-scraped"

        sig_texts: list[str] = []
        seen_sig: set[str] = set()
        for s in group_sigs:
            txt = s.get("signature_text") or s.get("signature") or ""
            if txt and txt.lower() not in seen_sig:
                seen_sig.add(txt.lower())
                sig_texts.append(txt)

        report_item = {
            "id": first.get("id"),
            "reported_at": first.get("reported_at") or first.get("created_at") or _now(),
            "company": first.get("company") or "Unknown Company",
            "domain": first.get("domain") or "",
            "signatures": sig_texts,
            "pattern_type": first.get("pattern_type") or "user-reported",
            "source_channel": source_channel,
            "notes": sanitize_report_note(note),
        }
        reports.append(report_item)

    return reports


def export_reports_yaml(
    conn: sqlite3.Connection | None = None,
    source: str | None = "local_user",
) -> str:
    """Export recorded scam reports as a paste-ready YAML string."""
    reports = export_reports(conn, source=source)
    data = {
        "version": 1,
        "reports": reports,
    }
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def fetch_community_reports(
    source_path_or_url: str | Path | None = None,
    conn: sqlite3.Connection | None = None,
    ingest: bool = False,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """Fetch community scam reports from a local YAML file or remote URL.

    Falls back to the local cached / starter YAML file if a remote fetch fails.

    Args:
        source_path_or_url: Local file path or HTTP(S) URL. Defaults to bundled config.
        conn: Optional SQLite connection.
        ingest: If True, ingests parsed signatures into reported_signatures table.
        timeout: Network timeout in seconds for remote fetch.

    Returns:
        List of parsed report dicts.
    """
    content: str = ""
    bundled_path = Path(__file__).parent / "config" / "community_scam_reports.yaml"

    target = str(source_path_or_url) if source_path_or_url else ""

    if target.startswith(("http://", "https://")):
        fetched = False
        try:
            import httpx
            resp = httpx.get(target, timeout=timeout, follow_redirects=True)
            if resp.status_code == 200:
                content = resp.text
                fetched = True
        except Exception as err:
            log.warning("httpx fetch failed for %s: %s", target, err)

        if not fetched:
            try:
                import urllib.request
                with urllib.request.urlopen(target, timeout=timeout) as response:
                    content = response.read().decode("utf-8")
                    fetched = True
            except Exception as err:
                log.warning("urllib fetch failed for %s: %s", target, err)

        if not fetched:
            log.warning("Remote community feed fetch failed; falling back to local copy.")
            if bundled_path.exists():
                content = bundled_path.read_text(encoding="utf-8")
            else:
                content = "version: 1\nreports: []\n"
    elif target:
        p = Path(target)
        if p.exists():
            content = p.read_text(encoding="utf-8")
        elif bundled_path.exists():
            content = bundled_path.read_text(encoding="utf-8")
        else:
            content = "version: 1\nreports: []\n"
    else:
        if bundled_path.exists():
            content = bundled_path.read_text(encoding="utf-8")
        else:
            content = "version: 1\nreports: []\n"

    try:
        parsed = yaml.safe_load(content) or {}
        reports = parsed.get("reports", [])
        if not isinstance(reports, list):
            reports = []
    except Exception as err:
        log.error("Failed to parse community reports YAML: %s", err)
        reports = []

    if ingest:
        conn = _get_connection(conn)
        init_reported_signatures(conn)
        sig_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(reported_signatures)").fetchall()
        }
        for r in reports:
            company = r.get("company", "")
            domain = r.get("domain", "")
            pattern_type = r.get("pattern_type", "community-feed")
            notes = sanitize_report_note(r.get("notes") or "")
            reported_at = r.get("reported_at") or _now()
            signatures = r.get("signatures", [])
            for sig in signatures:
                sig_clean = sanitize_signature_text(str(sig))
                if not sig_clean:
                    continue

                cols_to_insert = ["company", "domain", "note", "source"]
                vals_to_insert = [company or None, domain or None, notes, "community_feed"]

                if "signature" in sig_cols:
                    cols_to_insert.append("signature")
                    vals_to_insert.append(sig_clean)
                if "source_text" in sig_cols:
                    cols_to_insert.append("source_text")
                    vals_to_insert.append(sig_clean)
                if "signature_text" in sig_cols:
                    cols_to_insert.append("signature_text")
                    vals_to_insert.append(sig_clean)
                if "reported_at" in sig_cols:
                    cols_to_insert.append("reported_at")
                    vals_to_insert.append(reported_at)
                if "created_at" in sig_cols:
                    cols_to_insert.append("created_at")
                    vals_to_insert.append(reported_at)
                if "pattern_type" in sig_cols:
                    cols_to_insert.append("pattern_type")
                    vals_to_insert.append(pattern_type)

                placeholders = ", ".join("?" for _ in cols_to_insert)
                col_names = ", ".join(cols_to_insert)
                conn.execute(
                    f"INSERT INTO reported_signatures ({col_names}) VALUES ({placeholders})",
                    vals_to_insert,
                )
        conn.commit()

    return reports
