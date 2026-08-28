"""Tests for scam detection gate, LLM tie-break, database columns, and launcher filtering.

Script-style test suite following repo convention (like test_quality.py).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Ensure src is importable
repo_root = Path(__file__).resolve().parent.parent
src_dir = repo_root / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

import jobpilot.database as db
from jobpilot.scam_gate import (
    evaluate_scam_posting,
    match_reported_signature,
)

RESULT: list[bool] = []


def check(name: str, cond: bool) -> None:
    RESULT.append(bool(cond))
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}")
    if not cond:
        print(f"  FAILED: {name}")


# ---------------------------------------------------------------------------
# 1. Strong Heuristic Gate - Payment / Fee Language
# ---------------------------------------------------------------------------

# Positive cases
v_fee1, r_fee1 = evaluate_scam_posting("Please note a $50 processing fee is required with your application.")
check("Strong: processing fee blocked",
      v_fee1 == "blocked" and any(r["category"] == "payment_fee" for r in r_fee1))

v_fee2, r_fee2 = evaluate_scam_posting("All new hires must purchase your own equipment from our verified supplier.")
check("Strong: purchase equipment blocked",
      v_fee2 == "blocked" and any(r["category"] == "payment_fee" for r in r_fee2))

v_fee3, r_fee3 = evaluate_scam_posting("A registration fee of $25 is needed to reserve your training slot.")
check("Strong: registration fee blocked",
      v_fee3 == "blocked" and any(r["category"] == "payment_fee" for r in r_fee3))

v_fee4, r_fee4 = evaluate_scam_posting("An equipment deposit must be transferred prior to receiving home workstation.")
check("Strong: equipment deposit blocked",
      v_fee4 == "blocked" and any(r["category"] == "payment_fee" for r in r_fee4))

v_fee5, r_fee5 = evaluate_scam_posting("Mandatory training fee applies before starting the role.")
check("Strong: training fee blocked",
      v_fee5 == "blocked" and any(r["category"] == "payment_fee" for r in r_fee5))

# Negative controls (legitimate postings)
v_fee_neg1, _ = evaluate_scam_posting(
    "We offer competitive compensation, comprehensive health benefits, and 401(k) match."
)
check("Negative: standard benefits clear", v_fee_neg1 == "clear")

v_fee_neg2, _ = evaluate_scam_posting(
    "Company provides all required equipment including Apple MacBook Pro and monitor."
)
check("Negative: company provides equipment clear", v_fee_neg2 == "clear")


# ---------------------------------------------------------------------------
# 2. Strong Heuristic Gate - Bank Account & Payment Processing / Money Mule
# ---------------------------------------------------------------------------

v_bank1, r_bank1 = evaluate_scam_posting(
    "Role requires using your personal bank account for payment processing and forwarding."
)
check("Strong: bank account for payment processing blocked",
      v_bank1 == "blocked" and any(r["category"] == "bank_account_processing" for r in r_bank1))

v_bank2, r_bank2 = evaluate_scam_posting(
    "Please provide your bank account for payment disbursement and transfers."
)
check("Strong: provide bank account for payment blocked",
      v_bank2 == "blocked" and any(r["category"] == "bank_account_processing" for r in r_bank2))

v_bank3, r_bank3 = evaluate_scam_posting(
    "You will deposit the check and wire the remaining funds to our vendor via Western Union."
)
check("Strong: deposit check and wire funds blocked",
      v_bank3 == "blocked" and any(r["category"] == "bank_account_processing" for r in r_bank3))

v_bank4, r_bank4 = evaluate_scam_posting(
    "Receive funds into your personal account and purchase gift cards for clients."
)
check("Strong: receive funds and buy gift cards blocked",
      v_bank4 == "blocked" and any(r["category"] == "bank_account_processing" for r in r_bank4))

# Negative controls
v_bank_neg1, _ = evaluate_scam_posting(
    "Senior Backend Engineer: Experience building payment processing gateways and Stripe integrations."
)
check("Negative: payment processing software engineer clear", v_bank_neg1 == "clear")

v_bank_neg2, _ = evaluate_scam_posting(
    "Accountant role: Responsible for monthly bank account reconciliation and ledger balancing."
)
check("Negative: bank account reconciliation clear", v_bank_neg2 == "clear")


# ---------------------------------------------------------------------------
# 3. Strong Heuristic Gate - Premature Sensitive Information Requests
# ---------------------------------------------------------------------------

v_ssn1, r_ssn1 = evaluate_scam_posting("Please submit your SSN and date of birth to apply for this position.")
check("Strong: submit SSN upfront blocked",
      v_ssn1 == "blocked" and any(r["category"] == "premature_sensitive_info" for r in r_ssn1))

v_ssn2, r_ssn2 = evaluate_scam_posting("Upload a copy of passport before the interview to proceed.")
check("Strong: upload passport before interview blocked",
      v_ssn2 == "blocked" and any(r["category"] == "premature_sensitive_info" for r in r_ssn2))

v_ssn3, r_ssn3 = evaluate_scam_posting("Send your bank routing number and account number with your resume.")
check("Strong: send routing number blocked",
      v_ssn3 == "blocked" and any(r["category"] == "premature_sensitive_info" for r in r_ssn3))

# Negative controls
v_ssn_neg1, _ = evaluate_scam_posting(
    "A criminal background check will be conducted after a conditional offer of employment."
)
check("Negative: background check after offer clear", v_ssn_neg1 == "clear")


# ---------------------------------------------------------------------------
# 4. Soft Heuristics + Mocked LLM Tie-Break
# ---------------------------------------------------------------------------

class MockLLMSuspicious:
    def ask(self, prompt: str, **kwargs) -> str:
        return json.dumps({
            "verdict": "suspicious",
            "reason": "Off-platform pivot to unverified Telegram handle before interview",
        })


class MockLLMLegit:
    def ask(self, prompt: str, **kwargs) -> str:
        return json.dumps({
            "verdict": "legit",
            "reason": "Standard flexible work description from legitimate recruiter",
        })


class MockLLMError:
    def ask(self, prompt: str, **kwargs) -> str:
        raise ConnectionError("503 Service Unavailable: Gateway down")


# Soft case: Telegram contact pivot -> LLM suspicious
v_soft1, r_soft1 = evaluate_scam_posting(
    "Contact hiring manager directly on Telegram @fast_hire_recruiter to discuss the project.",
    client=MockLLMSuspicious(),
)
check("Soft + LLM suspicious: blocked",
      v_soft1 == "blocked" and any(r["category"] == "llm_tie_break" for r in r_soft1))

# Soft case: WhatsApp contact pivot -> LLM suspicious
v_soft2, r_soft2 = evaluate_scam_posting(
    "Reach out via WhatsApp at +1-800-555-0199 for an instant interview.",
    client=MockLLMSuspicious(),
)
check("Soft + LLM suspicious: WhatsApp pivot blocked",
      v_soft2 == "blocked" and any(r["category"] == "llm_tie_break" for r in r_soft2))

# Soft case: Teams secondary email pivot -> LLM suspicious
v_soft3, r_soft3 = evaluate_scam_posting(
    "Please confirm your Teams email, if different from your CV email, so we can contact you.",
    client=MockLLMSuspicious(),
)
check("Soft + LLM suspicious: disconnected Teams email blocked",
      v_soft3 == "blocked" and any(r["category"] == "llm_tie_break" for r in r_soft3))

# Soft case: Too-good-to-be-true -> LLM legit tie-break
v_soft4, r_soft4 = evaluate_scam_posting(
    "Flexible, work around your existing commitments on our open source platform project.",
    client=MockLLMLegit(),
)
check("Soft + LLM legit: clear", v_soft4 == "clear" and len(r_soft4) == 0)

# Negative controls for soft categories (standard legitimate mentions)
v_teams_neg, _ = evaluate_scam_posting("First-round interviews will be conducted via Microsoft Teams or Zoom.")
check("Negative: Teams interview tool mention clear without LLM call", v_teams_neg == "clear")


# ---------------------------------------------------------------------------
# 5. Fail-Closed on LLM Error (Crucial Guarantee)
# ---------------------------------------------------------------------------

v_fail_closed, r_fail_closed = evaluate_scam_posting(
    "Reach out via Telegram @urgent_hire to apply immediately.",
    client=MockLLMError(),
)
check("Fail-closed: LLM transport error leaves verdict NULL (None)",
      v_fail_closed is None and any(r["category"] == "error" for r in r_fail_closed))


# ---------------------------------------------------------------------------
# 6. Reported Signature Matching (Word-Boundary & Token-Aware)
# ---------------------------------------------------------------------------

base_reported_signature = (
    "We are seeking a Remote Project Assistant for our confidential financial client. "
    "You will process daily transaction spreadsheets and verify client account confirmations. "
    "Earn $45 per hour with flexible hours. Must have personal computer and stable internet. "
    "To begin immediately, submit your application and confirm your availability."
)

near_repost_variation = (
    "We are seeking a Remote Project Assistant for our confidential financial enterprise. "
    "You will process daily transaction spreadsheets and verify client account confirmations. "
    "Earn $50 per hour with flexible hours. Must have personal computer and stable internet. "
    "To begin immediately, submit your application and confirm your availability."
)

unrelated_good_posting = (
    "Staff Site Reliability Engineer with 7+ years managing Kubernetes clusters on AWS. "
    "Proficiency with Terraform, Prometheus, Datadog, and Python automation. "
    "Competitive salary, 401(k), unlimited PTO."
)

# Positive match against reported signature
match_pos = match_reported_signature(near_repost_variation, signatures=[base_reported_signature])
check("Signature match: near-repost detected", match_pos is not None and match_pos["matched"] is True)

# Negative match against reported signature
match_neg = match_reported_signature(unrelated_good_posting, signatures=[base_reported_signature])
check("Signature match: unrelated posting not matched", match_neg is None)

# Word boundary awareness: token boundary check (no substring containment false positives)
check("Word boundary: substring check", match_reported_signature("assist", signatures=["assistant"]) is None)


# ---------------------------------------------------------------------------
# 7. Database Migration & Schema Columns
# ---------------------------------------------------------------------------

with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
    db_file = tf.name

try:
    conn = db.init_db(db_file)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    check("DB schema: scam_verdict column present", "scam_verdict" in cols)
    check("DB schema: scam_reasons column present", "scam_reasons" in cols)
    check("DB schema: scam_checked_at column present", "scam_checked_at" in cols)

    sig_cols = {row[1] for row in conn.execute("PRAGMA table_info(reported_signatures)").fetchall()}
    check("DB schema: reported_signatures table present", "signature" in sig_cols and "job_url" in sig_cols)

    # Test acquire_job positive check behavior against SQLite
    # Insert 4 test jobs:
    # 1. Clear job (should acquire)
    # 2. Blocked job (should NOT acquire)
    # 3. NULL/pending job (should NOT acquire - fail closed)
    # 4. Workday API job with NULL scam_verdict (should acquire - curated allowlist)
    conn.execute("""
        INSERT INTO jobs (url, title, fit_score, tailored_resume_path, apply_status, scam_verdict, strategy)
        VALUES
        ('https://ex.com/job1', 'Clear Job', 8, '/tmp/resume.pdf', NULL, 'clear', 'smartextract'),
        ('https://ex.com/job2', 'Blocked Scam', 9, '/tmp/resume.pdf', NULL, 'blocked', 'smartextract'),
        ('https://ex.com/job3', 'Pending Error Job', 9, '/tmp/resume.pdf', NULL, NULL, 'smartextract'),
        ('https://ex.com/job4', 'Workday Real Job', 8, '/tmp/resume.pdf', NULL, NULL, 'workday_api')
    """)
    conn.commit()

    # Query using launcher's positive filter
    eligible_rows = conn.execute("""
        SELECT url FROM jobs
        WHERE tailored_resume_path IS NOT NULL
          AND (apply_status IS NULL OR apply_status = 'failed')
          AND fit_score >= 6
          AND (scam_verdict = 'clear' OR strategy = 'workday_api')
        ORDER BY url
    """).fetchall()
    eligible_urls = [r[0] for r in eligible_rows]

    check("Acquire filter: clear job acquired", "https://ex.com/job1" in eligible_urls)
    check("Acquire filter: blocked scam NOT acquired", "https://ex.com/job2" not in eligible_urls)
    check("Acquire filter: NULL pending job NOT acquired (fail-closed)", "https://ex.com/job3" not in eligible_urls)
    check("Acquire filter: workday_api job acquired", "https://ex.com/job4" in eligible_urls)

    conn.close()
finally:
    if Path(db_file).exists():
        Path(db_file).unlink()


# ---------------------------------------------------------------------------
# Test Summary
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    passed = sum(1 for x in RESULT if x)
    print(f"\n{passed}/{len(RESULT)} checks passed")
    raise SystemExit(0 if passed == len(RESULT) else 1)
