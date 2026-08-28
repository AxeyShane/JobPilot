"""Tests for scam report recording, fuzzy signature matching, sharing, and export.

Follows repo test conventions (script-style standalone executable tests).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import yaml

# Ensure src is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from jobpilot.scam_report import (
    export_reports,
    export_reports_yaml,
    fetch_community_reports,
    get_active_signatures,
    init_reported_signatures,
    match_against_reported_signatures,
    match_text_signature,
    record_report,
    sanitize_report_note,
)


def _make_mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE jobs (
            url                   TEXT PRIMARY KEY,
            title                 TEXT,
            salary                TEXT,
            description           TEXT,
            location              TEXT,
            site                  TEXT,
            strategy              TEXT,
            discovered_at         TEXT,
            full_description      TEXT,
            application_url       TEXT,
            detail_scraped_at     TEXT,
            detail_error          TEXT,
            fit_score             INTEGER,
            score_reasoning       TEXT,
            scored_at             TEXT,
            tailored_resume_path  TEXT,
            tailored_at           TEXT,
            tailor_attempts       INTEGER DEFAULT 0,
            keyword_match_rate    REAL,
            cover_letter_path     TEXT,
            cover_letter_at       TEXT,
            cover_attempts        INTEGER DEFAULT 0,
            applied_at            TEXT,
            apply_status          TEXT,
            apply_error           TEXT,
            apply_attempts        INTEGER DEFAULT 0,
            agent_id              TEXT,
            last_attempted_at     TEXT,
            apply_duration_ms     INTEGER,
            apply_task_id         TEXT,
            verification_confidence TEXT,
            scam_verdict          TEXT,
            scam_reasons          TEXT,
            scam_checked_at       TEXT
        )
    """)
    init_reported_signatures(conn)
    return conn


def test_record_report_override_clear() -> None:
    """User flagging immediately overrides a previous scam_verdict = 'clear'."""
    conn = _make_mem_db()
    job_url = "https://example.com/jobs/101"
    conn.execute(
        "INSERT INTO jobs (url, title, full_description, site, scam_verdict) VALUES (?, ?, ?, ?, ?)",
        (
            job_url,
            "Customer Support Representative",
            "Send wire transfer of the equipment purchase funds to our approved supplier.",
            "RemoteOK",
            "clear",
        ),
    )
    conn.commit()

    res = record_report(
        job=job_url,
        note="Applicant asked to wire equipment money",
        conn=conn,
        snippet="wire transfer of the equipment purchase funds",
    )

    assert res["status"] == "blocked"
    assert res["url"] == job_url

    # Verify DB row updated
    row = conn.execute(
        "SELECT scam_verdict, scam_reasons, scam_checked_at FROM jobs WHERE url = ?",
        (job_url,),
    ).fetchone()
    assert row["scam_verdict"] == "blocked"
    assert row["scam_checked_at"] is not None

    reasons = json.loads(row["scam_reasons"])
    assert len(reasons) == 1
    assert reasons[0]["category"] == "user-reported"
    assert "wire equipment money" in reasons[0]["note"]
    assert "wire transfer" in reasons[0]["quoted"]

    # Verify signature inserted into reported_signatures
    sigs = get_active_signatures(conn, source="local_user")
    assert len(sigs) >= 1
    assert any("wire transfer" in s["signature_text"] for s in sigs)


def test_fuzzy_match_positive_near_identical_repost() -> None:
    """Imported/reported signature catches a re-posted variation with slight wording changes."""
    sig = "wire transfer of the equipment purchase funds to our verified supplier"

    # Near-identical repost with slight phrasing variation
    repost_text = (
        "Global Operations Corp is hiring! Candidates must complete wire transfer of the "
        "equipment purchase funds to our verified supplier before day one."
    )
    matched, score, quoted = match_text_signature(repost_text, sig, threshold=0.80)
    assert matched is True
    assert score >= 0.80
    assert "wire transfer" in quoted

    # Match via match_against_reported_signatures helper
    res = match_against_reported_signatures(repost_text, signatures=[sig], threshold=0.80)
    assert res is not None
    assert res["matched"] is True
    assert res["category"] == "known-signature"
    assert res["score"] >= 0.80


def test_fuzzy_match_negative_unrelated_and_substring_traps() -> None:
    """Token-aware match prevents false positives on standard boilerplate and substring traps."""
    # Standard engineering boilerplate
    normal_posting = (
        "Acme Software is looking for a Senior Backend Engineer. You will build REST APIs with FastAPI, "
        "manage PostgreSQL databases, and deploy with Docker. Great benefits, 401k match, health insurance."
    )
    sig = "wire transfer of the equipment purchase funds to our supplier"
    matched, score, _ = match_text_signature(normal_posting, sig, threshold=0.80)
    assert matched is False
    assert score == 0.0

    # Substring traps: "wireless" vs "wire"
    wireless_text = "We are seeking a wireless network engineer for equipment installation in our office."
    matched, _, _ = match_text_signature(wireless_text, "wire transfer", threshold=0.80)
    assert matched is False

    # Substring trap: "coffee" vs "fee"
    coffee_text = "Enjoy unlimited free coffee, tea, and snacks in our headquarters cafeteria."
    matched, _, _ = match_text_signature(coffee_text, "training fee", threshold=0.80)
    assert matched is False

    # Substring trap: "career" vs "car"
    career_text = "Explore exciting career opportunities in our cloud engineering division."
    matched, _, _ = match_text_signature(career_text, "car rental", threshold=0.80)
    assert matched is False


def test_community_fetch_fallback_on_failure() -> None:
    """Remote fetch failure gracefully falls back to local bundled community reports."""
    # Dead / unreachable domain
    reports = fetch_community_reports("https://invalid-non-existent-domain-9999.invalid/scam_feed.yaml", timeout=0.5)
    assert isinstance(reports, list)
    assert len(reports) >= 3
    assert any(r.get("company") == "Apex Horizon Solutions" for r in reports)
    assert any("wire transfer" in str(r.get("signatures")) for r in reports)


def test_export_output_matches_community_file_shape_exactly() -> None:
    """Exported reports strictly follow the version 1 YAML schema."""
    conn = _make_mem_db()
    job_url = "https://example.com/job/202"
    conn.execute(
        "INSERT INTO jobs (url, title, full_description, site, scam_verdict) VALUES (?, ?, ?, ?, ?)",
        (
            job_url,
            "Cloud Data Assistant",
            "Please provide your personal bank account for payment processing before contract start.",
            "Indeed",
            "clear",
        ),
    )
    conn.commit()

    record_report(
        job=job_url,
        note="Asked for personal bank account details",
        conn=conn,
        snippet="personal bank account for payment processing",
        pattern_type="fake-payment-processing",
    )

    reports = export_reports(conn, source="local_user")
    assert len(reports) == 1
    rep = reports[0]

    # Schema validation
    assert "id" in rep and isinstance(rep["id"], str) and len(rep["id"]) > 10
    assert "reported_at" in rep and isinstance(rep["reported_at"], str)
    assert "company" in rep
    assert "domain" in rep
    assert "signatures" in rep and isinstance(rep["signatures"], list) and len(rep["signatures"]) >= 1
    assert "pattern_type" in rep
    assert "source_channel" in rep and rep["source_channel"] == "indeed-scraped"
    assert "notes" in rep

    # YAML export round-trip validation
    yaml_text = export_reports_yaml(conn, source="local_user")
    parsed = yaml.safe_load(yaml_text)
    assert parsed["version"] == 1
    assert len(parsed["reports"]) == 1
    assert parsed["reports"][0]["id"] == rep["id"]
    assert parsed["reports"][0]["signatures"] == rep["signatures"]


def test_privacy_boundary_no_pii_in_reports() -> None:
    """Sanitizer redacts emails, phone numbers, and applicant PII from report notes."""
    raw_note = "Recruiter John (john.fake@scam-mail.org) asked me to call +1 (555) 019-2834 on Telegram."
    sanitized = sanitize_report_note(raw_note)
    assert "john.fake@scam-mail.org" not in sanitized
    assert "[REDACTED_EMAIL]" in sanitized
    assert "555" not in sanitized
    assert "[REDACTED_PHONE]" in sanitized

    conn = _make_mem_db()
    record_report(
        job="https://example.com/job/303",
        note=raw_note,
        conn=conn,
        snippet="telegram contact with recruiter",
    )

    yaml_text = export_reports_yaml(conn, source="local_user")
    assert "john.fake@scam-mail.org" not in yaml_text
    assert "+1 (555) 019-2834" not in yaml_text
    assert "[REDACTED_EMAIL]" in yaml_text
    assert "[REDACTED_PHONE]" in yaml_text


def main() -> None:
    tests = [
        test_record_report_override_clear,
        test_fuzzy_match_positive_near_identical_repost,
        test_fuzzy_match_negative_unrelated_and_substring_traps,
        test_community_fetch_fallback_on_failure,
        test_export_output_matches_community_file_shape_exactly,
        test_privacy_boundary_no_pii_in_reports,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
            passed += 1
        except Exception as e:
            print(f" FAIL  {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{len(tests)} tests, {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
