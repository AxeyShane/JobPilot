"""Tests for freshness-first apply queue in jobpilot.apply.launcher.

Verifies:
(a) Newest eligible job is picked first even if an older job has fit_score 9 and 0 attempts.
(b) Blocked/pending scam jobs are never picked (fail-closed positive gate).
(c) Same-day retries come after fresh same-day jobs, but before older days.
(d) NULL/empty discovered_at sorts last.
(e) Atomic claim still holds (in_progress jobs not re-picked).
(f) target_url branch unchanged (direct URL targeting works).
(g) Stale claim reclaim resets orphaned in_progress jobs.

Run directly:  python tests/test_queue_speed.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure src is in sys.path
repo_root = Path(__file__).resolve().parent.parent
src_dir = repo_root / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

import jobpilot.config as config
import jobpilot.database as db
import jobpilot.apply.launcher as launcher

RESULT: list[bool] = []


def check(name: str, cond: bool) -> None:
    RESULT.append(bool(cond))
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}")
    if not cond:
        print(f"  FAILED: {name}")


def create_test_db() -> tuple[str, db.sqlite3.Connection]:
    tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_file = tf.name
    tf.close()
    conn = db.init_db(db_file)
    return db_file, conn


def cleanup_db(db_file: str) -> None:
    db.close_connection(db_file)
    p = Path(db_file)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


def insert_job(
    conn: db.sqlite3.Connection,
    url: str,
    title: str = "Software Engineer",
    site: str = "greenhouse",
    application_url: str | None = None,
    tailored_resume_path: str | None = "/resumes/resume.pdf",
    fit_score: int = 8,
    apply_status: str | None = None,
    apply_attempts: int | None = 0,
    scam_verdict: str | None = "clear",
    strategy: str = "direct",
    discovered_at: str | None = None,
    last_attempted_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, site, application_url, tailored_resume_path,
            fit_score, apply_status, apply_attempts, scam_verdict,
            strategy, discovered_at, last_attempted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            url,
            title,
            site,
            application_url or url,
            tailored_resume_path,
            fit_score,
            apply_status,
            apply_attempts,
            scam_verdict,
            strategy,
            discovered_at,
            last_attempted_at,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Test 1: (a) Newest eligible job picked first vs older higher score job
# ---------------------------------------------------------------------------
def test_newest_first_over_older_high_score():
    db_file, conn = create_test_db()
    try:
        launcher.get_connection = lambda: db.get_connection(db_file)

        # 5-day old job with fit_score 9, 0 attempts
        insert_job(
            conn,
            url="https://greenhouse.io/old_job_score9",
            fit_score=9,
            apply_attempts=0,
            discovered_at="2026-08-22T10:00:00+00:00",
        )
        # Yesterday job with fit_score 9, 0 attempts
        insert_job(
            conn,
            url="https://greenhouse.io/yest_job_score9",
            fit_score=9,
            apply_attempts=0,
            discovered_at="2026-08-26T20:00:00+00:00",
        )
        # 4-hour old job today with fit_score 8, 0 attempts
        insert_job(
            conn,
            url="https://greenhouse.io/today_4h_score8",
            fit_score=8,
            apply_attempts=0,
            discovered_at="2026-08-27T10:00:00+00:00",
        )
        # 20-minute old job today with fit_score 6, 0 attempts (minimum score)
        insert_job(
            conn,
            url="https://greenhouse.io/today_20m_score6",
            fit_score=6,
            apply_attempts=0,
            discovered_at="2026-08-27T15:40:00+00:00",
        )

        # Freshness first: 20-minute-old job must be acquired first
        j1 = launcher.acquire_job(min_score=6)
        check("Newest job picked 1st (20m old, score 6 beats older score 9)",
              j1 is not None and j1["url"] == "https://greenhouse.io/today_20m_score6")

        # Next: today 4h old job
        j2 = launcher.acquire_job(min_score=6)
        check("Newest job picked 2nd (today 4h old, score 8)",
              j2 is not None and j2["url"] == "https://greenhouse.io/today_4h_score8")

        # Next: yesterday job
        j3 = launcher.acquire_job(min_score=6)
        check("Newest job picked 3rd (yesterday job)",
              j3 is not None and j3["url"] == "https://greenhouse.io/yest_job_score9")

        # Next: 5-day old job
        j4 = launcher.acquire_job(min_score=6)
        check("Newest job picked 4th (5-day old job)",
              j4 is not None and j4["url"] == "https://greenhouse.io/old_job_score9")

        # Queue empty
        j5 = launcher.acquire_job(min_score=6)
        check("Queue drained returns None", j5 is None)
    finally:
        cleanup_db(db_file)


# ---------------------------------------------------------------------------
# Test 2: (b) Blocked / pending scam jobs never picked
# ---------------------------------------------------------------------------
def test_scam_gate_filtering():
    db_file, conn = create_test_db()
    try:
        launcher.get_connection = lambda: db.get_connection(db_file)

        # High score jobs with scam_verdict != 'clear'
        insert_job(
            conn,
            url="https://greenhouse.io/scam_blocked",
            fit_score=10,
            scam_verdict="blocked",
            discovered_at="2026-08-27T18:00:00+00:00",
        )
        insert_job(
            conn,
            url="https://greenhouse.io/scam_suspicious",
            fit_score=10,
            scam_verdict="suspicious",
            discovered_at="2026-08-27T18:00:00+00:00",
        )
        insert_job(
            conn,
            url="https://greenhouse.io/scam_pending_null",
            fit_score=10,
            scam_verdict=None,
            discovered_at="2026-08-27T18:00:00+00:00",
        )
        insert_job(
            conn,
            url="https://greenhouse.io/scam_pending_str",
            fit_score=10,
            scam_verdict="pending",
            discovered_at="2026-08-27T18:00:00+00:00",
        )
        # Legitimate job with clear scam verdict
        insert_job(
            conn,
            url="https://greenhouse.io/clear_legit_job",
            fit_score=7,
            scam_verdict="clear",
            discovered_at="2026-08-27T12:00:00+00:00",
        )
        # Workday API strategy bypasses scam_verdict
        insert_job(
            conn,
            url="https://myworkdayjobs.com/workday_job",
            fit_score=7,
            scam_verdict=None,
            strategy="workday_api",
            discovered_at="2026-08-27T11:00:00+00:00",
        )

        j1 = launcher.acquire_job(min_score=6)
        check("Scam filter: clear legit job acquired",
              j1 is not None and j1["url"] == "https://greenhouse.io/clear_legit_job")

        j2 = launcher.acquire_job(min_score=6)
        check("Scam filter: workday_api bypass acquired",
              j2 is not None and j2["url"] == "https://myworkdayjobs.com/workday_job")

        j3 = launcher.acquire_job(min_score=6)
        check("Scam filter: blocked/suspicious/pending never acquired", j3 is None)
    finally:
        cleanup_db(db_file)


# ---------------------------------------------------------------------------
# Test 3: (c) Same-day retries come after fresh same-day, before older days
# ---------------------------------------------------------------------------
def test_same_day_retries_and_cross_day_ordering():
    db_file, conn = create_test_db()
    try:
        launcher.get_connection = lambda: db.get_connection(db_file)

        # Fresh today (attempts=0, 10:00)
        insert_job(
            conn,
            url="https://greenhouse.io/today_fresh_10am",
            fit_score=7,
            apply_attempts=0,
            discovered_at="2026-08-27T10:00:00+00:00",
        )
        # Retried today (attempts=1, status=failed, 15:00, higher score)
        insert_job(
            conn,
            url="https://greenhouse.io/today_retried_15pm",
            fit_score=9,
            apply_attempts=1,
            apply_status="failed",
            discovered_at="2026-08-27T15:00:00+00:00",
        )
        # Fresh yesterday (attempts=0, 20:00, higher score)
        insert_job(
            conn,
            url="https://greenhouse.io/yesterday_fresh_20pm",
            fit_score=9,
            apply_attempts=0,
            discovered_at="2026-08-26T20:00:00+00:00",
        )

        # 1. Fresh today job (0 attempts) beats today's retried job (1 attempt)
        j1 = launcher.acquire_job(min_score=6)
        check("Same-day: fresh untried (att=0) beats fresh retried (att=1)",
              j1 is not None and j1["url"] == "https://greenhouse.io/today_fresh_10am")

        # 2. Retried today job beats yesterday's untried job (new day beats old day)
        j2 = launcher.acquire_job(min_score=6)
        check("Cross-day: today retried (att=1) beats yesterday untried (att=0)",
              j2 is not None and j2["url"] == "https://greenhouse.io/today_retried_15pm")

        # 3. Yesterday's fresh job
        j3 = launcher.acquire_job(min_score=6)
        check("Cross-day: yesterday untried picked after today's jobs",
              j3 is not None and j3["url"] == "https://greenhouse.io/yesterday_fresh_20pm")
    finally:
        cleanup_db(db_file)


# ---------------------------------------------------------------------------
# Test 4: (d) NULL and empty discovered_at sorts last
# ---------------------------------------------------------------------------
def test_null_discovered_at_sorts_last():
    db_file, conn = create_test_db()
    try:
        launcher.get_connection = lambda: db.get_connection(db_file)

        # Old dated job (e.g. 2026-01-01) with score 6
        insert_job(
            conn,
            url="https://greenhouse.io/dated_old_job",
            fit_score=6,
            apply_attempts=0,
            discovered_at="2026-01-01T00:00:00+00:00",
        )
        # NULL discovered_at with score 10
        insert_job(
            conn,
            url="https://greenhouse.io/null_disc_score10",
            fit_score=10,
            apply_attempts=0,
            discovered_at=None,
        )
        # Empty string discovered_at with score 9
        insert_job(
            conn,
            url="https://greenhouse.io/empty_disc_score9",
            fit_score=9,
            apply_attempts=0,
            discovered_at="",
        )

        # 1. Dated job picked first
        j1 = launcher.acquire_job(min_score=6)
        check("NULL discovered_at: dated job beats NULL/empty discovered_at",
              j1 is not None and j1["url"] == "https://greenhouse.io/dated_old_job")

        # 2. NULL discovered_at sorted by fit_score DESC (score 10 beats score 9)
        j2 = launcher.acquire_job(min_score=6)
        check("NULL discovered_at: score 10 picked before score 9",
              j2 is not None and j2["url"] == "https://greenhouse.io/null_disc_score10")

        # 3. Empty discovered_at picked last
        j3 = launcher.acquire_job(min_score=6)
        check("NULL discovered_at: empty string disc picked next",
              j3 is not None and j3["url"] == "https://greenhouse.io/empty_disc_score9")
    finally:
        cleanup_db(db_file)


# ---------------------------------------------------------------------------
# Test 5: (e) Atomic claim (in_progress jobs not re-picked)
# ---------------------------------------------------------------------------
def test_atomic_claim():
    db_file, conn = create_test_db()
    try:
        launcher.get_connection = lambda: db.get_connection(db_file)

        insert_job(
            conn,
            url="https://greenhouse.io/single_available_job",
            fit_score=8,
            apply_attempts=0,
            discovered_at="2026-08-27T12:00:00+00:00",
        )

        # Worker 1 acquires
        j1 = launcher.acquire_job(min_score=6, worker_id=1)
        check("Atomic claim: Worker 1 acquires job",
              j1 is not None and j1["url"] == "https://greenhouse.io/single_available_job")

        # Verify DB state
        row = conn.execute(
            "SELECT apply_status, agent_id, last_attempted_at FROM jobs WHERE url = ?",
            (j1["url"],),
        ).fetchone()
        check("Atomic claim: DB marked in_progress with agent_id",
              row["apply_status"] == "in_progress" and row["agent_id"] == "worker-1" and row["last_attempted_at"])

        # Worker 2 attempts acquire -> must be None
        j2 = launcher.acquire_job(min_score=6, worker_id=2)
        check("Atomic claim: Worker 2 gets None (job is in_progress)", j2 is None)
    finally:
        cleanup_db(db_file)


# ---------------------------------------------------------------------------
# Test 6: (f) target_url branch unchanged
# ---------------------------------------------------------------------------
def test_target_url_branch():
    db_file, conn = create_test_db()
    try:
        launcher.get_connection = lambda: db.get_connection(db_file)

        # Regular high score job
        insert_job(
            conn,
            url="https://greenhouse.io/regular_high_score",
            fit_score=9,
            discovered_at="2026-08-27T15:00:00+00:00",
        )
        # Targeted low score job (score 3 < min_score 6)
        insert_job(
            conn,
            url="https://jobs.lever.co/target_specific_job?source=direct",
            application_url="https://jobs.lever.co/target_specific_job",
            fit_score=3,
            discovered_at="2026-08-20T10:00:00+00:00",
        )

        # Acquire by target_url (matching exact or stripped URL)
        j = launcher.acquire_job(target_url="https://jobs.lever.co/target_specific_job", min_score=6)
        check("Target URL: targeted job acquired regardless of queue sort and min_score",
              j is not None and "target_specific_job" in j["url"])

        # Verify in_progress status set
        row = conn.execute("SELECT apply_status FROM jobs WHERE url LIKE '%target_specific_job%'").fetchone()
        check("Target URL: apply_status marked in_progress", row["apply_status"] == "in_progress")
    finally:
        cleanup_db(db_file)


# ---------------------------------------------------------------------------
# Test 7: (g) Stale claim reclaim
# ---------------------------------------------------------------------------
def test_stale_claim_reclaim():
    db_file, conn = create_test_db()
    try:
        launcher.get_connection = lambda: db.get_connection(db_file)

        # Stale job claimed 60 minutes ago (STALE_CLAIM_MINUTES = 45)
        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        insert_job(
            conn,
            url="https://greenhouse.io/stale_orphaned_job",
            fit_score=8,
            apply_status="in_progress",
            apply_attempts=1,
            discovered_at="2026-08-27T10:00:00+00:00",
            last_attempted_at=stale_time,
        )

        # Calling acquire_job should reclaim the stale job and acquire it
        j = launcher.acquire_job(min_score=6, worker_id=0)
        check("Stale claim: orphaned job reclaimed and re-acquired",
              j is not None and j["url"] == "https://greenhouse.io/stale_orphaned_job")
    finally:
        cleanup_db(db_file)


# ---------------------------------------------------------------------------
# Main Runner
# ---------------------------------------------------------------------------
def main():
    print("Running freshness-first apply queue tests...")
    test_newest_first_over_older_high_score()
    test_scam_gate_filtering()
    test_same_day_retries_and_cross_day_ordering()
    test_null_discovered_at_sorts_last()
    test_atomic_claim()
    test_target_url_branch()
    test_stale_claim_reclaim()

    passed = sum(RESULT)
    total = len(RESULT)
    print(f"\n{passed}/{total} checks passed")
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()
