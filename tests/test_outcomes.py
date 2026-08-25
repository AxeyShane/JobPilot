"""Standalone tests for jobpilot.outcomes.

Runnable directly:

    cd /repo && python3 tests/test_outcomes.py

(no pytest required, no venv — uses only stdlib). Adds src/ to sys.path,
then runs ~16 assertions across schema, upsert, status vocabulary,
recalibration and the draft promotion.

Exit code 0 == all passed; a failing case prints the message and exits 1.
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

from jobpilot import outcomes  # noqa: E402


def _db() -> sqlite3.Connection:
    """Fresh in-memory-ish DB in a tempdir; also seeds a jobs row pattern."""
    tmp = Path(tempfile.mkdtemp(prefix="jobpilot_outcomes_"))
    path = tmp / "jobpilot.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            url TEXT PRIMARY KEY, title TEXT, fit_score INTEGER,
            apply_status TEXT
        )
    """)
    conn.commit()
    outcomes.init_outcomes(conn)
    return conn


def seed_job(conn, url, fit_score=8, apply_status=None, status=None):
    apply_status = status if status is not None else apply_status
    conn.execute(
        "INSERT OR REPLACE INTO jobs (url, title, fit_score, apply_status) "
        "VALUES (?, ?, ?, ?)",
        (url, "Some Role", fit_score, apply_status),
    )
    conn.commit()


def test_init_idempotent():
    conn = _db()
    conn.close()
    conn2 = sqlite3.connect(":memory:")
    conn2.row_factory = sqlite3.Row
    outcomes.init_outcomes(conn2)
    outcomes.init_outcomes(conn2)  # second call must not error / duplicate
    cols = {r[1] for r in conn2.execute("PRAGMA table_info(outcomes)")}
    expected = {"url", "status", "status_date", "source", "notes",
                "interview_rounds", "offer_amount", "offer_currency",
                "rejected_reason", "created_at", "updated_at", "raw"}
    assert cols == expected, f"outcomes cols mismatch: {cols}"
    assert conn2.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0


def test_url_is_primary_key():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    outcomes.init_outcomes(conn)
    pk = [r[5] for r in conn.execute("PRAGMA table_info(outcomes)") if r[5]]
    assert pk == [1], f"expected url to be PRIMARY KEY, got {pk}"


def test_roundtrip_record_and_get():
    conn = _db()
    seed_job(conn, "https://example.com/j/1", 8)
    outcomes.record_outcome(conn, "https://example.com/j/1",
                            status="interview", source="gmail",
                            interview_rounds=2, notes="screener done")
    row = outcomes.get_outcome(conn, "https://example.com/j/1")
    assert row is not None
    assert row["status"] == "interview"
    assert row["source"] == "gmail"
    assert row["interview_rounds"] == 2
    assert row["notes"] == "screener done"
    assert row["status_date"] is None


def test_upsert_preserves_created_at():
    conn = _db()
    seed_job(conn, "https://example.com/j/2", fit_score=8)
    a = outcomes.record_outcome(conn, "https://example.com/j/2",
                                status="applied", created_at="2020-01-01T00:00:00+00:00")
    b = outcomes.record_outcome(conn, "https://example.com/j/2",
                                status="offer", offer_amount="120000", offer_currency="USD")
    assert a["created_at"] == "2020-01-01T00:00:00+00:00", "created_at must be preserved on upsert"
    assert b["status"] == "offer"
    assert b["offer_amount"] == "120000"
    assert b["created_at"] == "2020-01-01T00:00:00+00:00"
    assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 1


def test_missing_url_rejected():
    conn = _db()
    try:
        outcomes.record_outcome(conn, "", status="applied")
        assert False, "empty url should raise"
    except ValueError:
        pass


def test_spelling_variants_normalize_both_ways():
    """Both spellings of each pair must read back as the canonical token."""
    conn = _db()
    seed_job(conn, "https://e.com/a", fit_score=6)
    outcomes.record_outcome(conn, "https://e.com/a", status="no response")
    row = outcomes.get_outcome(conn, "https://e.com/a")
    assert row["status"] == "no_response", row["status"]

    seed_job(conn, "https://e.com/b", fit_score=6)
    outcomes.record_outcome(conn, "https://e.com/b", status="no_response")  # underscored form
    row2 = outcomes.get_outcome(conn, "https://e.com/b")
    assert row2["status"] == "no_response", row2["status"]

    seed_job(conn, "https://e.com/c", fit_score=6)
    outcomes.record_outcome(conn, "https://e.com/c", status="offer declined")
    assert outcomes.get_outcome(conn, "https://e.com/c")["status"] == "offer_declined"
    seed_job(conn, "https://e.com/d", fit_score=6)
    outcomes.record_outcome(conn, "https://e.com/d", status="offer_declined")
    assert outcomes.get_outcome(conn, "https://e.com/d")["status"] == "offer_declined"


def test_mixed_spelling_rows_summary_counts():
    """Reading a DB with both spellings must never double-count."""
    conn = _db()
    seed_job(conn, "https://e.com/1", fit_score=6)
    outcomes.record_outcome(conn, "https://e.com/1", status="no response")
    seed_job(conn, "https://e.com/2", fit_score=6)
    outcomes.record_outcome(conn, "https://e.com/2", status="no_response")
    s = outcomes.outcomes_summary(conn)
    assert s["no_response"] == 2, f"expected 2 no_response rows, got {s['no_response']}"
    assert s["total"] == 2, f"expected total 2, got {s['total']}"


def test_summary_all_buckets():
    conn = _db()
    for i, st in enumerate(["applied", "drafted", "waiting", "interview",
                            "offer", "accepted", "rejected", "no_response",
                            "closed", "assessment", "offer_declined"]):
        seed_job(conn, f"https://e.com/{i}", fit_score=1)
        outcomes.record_outcome(conn, f"https://e.com/{i}", status=st)
    s = outcomes.outcomes_summary(conn)
    assert s["applied"] == 1
    assert s["drafted"] == 1
    assert s["waiting"] == 1
    assert s["interview"] == 1
    assert s["offer"] == 1
    assert s["accepted"] == 1
    assert s["rejected"] == 1
    assert s["no_response"] == 1
    assert s["closed"] == 1
    assert s["total"] == 9  # only the 9 summary buckets


def test_recalibrate_empty_and_isolated():
    conn = _db()
    assert outcomes.recalibrate(conn) == []


def test_recalibrate_bands():
    conn = _db()
    # high scored, no interview -> over-prediction lesson
    seed_job(conn, "https://e.com/o1", fit_score=9)
    outcomes.record_outcome(conn, "https://e.com/o1", status="rejected")
    seed_job(conn, "https://e.com/hi", fit_score=8)
    outcomes.record_outcome(conn, "https://e.com/hi", status="rejected")
    # low scored but offered -> converts despite low score
    seed_job(conn, "https://e.com/lo", fit_score=2)
    outcomes.record_outcome(conn, "https://e.com/lo", status="offer")
    lessons = outcomes.recalibrate(conn)
    assert lessons, "expected at least one lesson"
    band_by = {l["band"]: l for l in lessons}
    assert "9-10" in band_by
    assert band_by["9-10"]["interview_rate"] == 0.0
    assert "over-predict" in band_by["9-10"]["lesson"]
    assert "0-3" in band_by
    assert band_by["0-3"]["offer_rate"] == 1.0
    # unlinked outcomes (no jobs row) are skipped, not errors
    outcomes.record_outcome(conn, "https://unknown.example.com/x", status="rejected")
    lessons2 = outcomes.recalibrate(conn)
    assert any("over-predict" in l["lesson"] for l in lessons2)


def test_detect_rejected():
    d = outcomes.detect_from_signal("We regret to inform you that your application was not moving forward.",
                                    "Backend Engineer")
    assert d["status"] == "rejected", d
    assert d["confidence"] > 0.0
    assert d["evidence"], d
    assert "rejected" in d["matched"]


def test_detect_offer():
    d = outcomes.detect_from_signal("Congratulations! We are delighted to extend an job offer with salary of 150000.",
                           "Engineer")
    assert d["status"] == "offer", d
    assert d["confidence"] >= 0.4


def test_detect_interview():
    d = outcomes.detect_from_signal("We are excited to invite you to an interview. Schedule a call with the recruiter.",
                           "Code")
    assert d["status"] == "interview", d


def test_detect_ack_application():
    d = outcomes.detect_from_signal("Thanks for applying! We received your application.",
                           "Engineer")
    assert d["status"] == "applied", d


def test_promote_draft():
    conn = _db()
    seed_job(conn, "https://e.com/p1", fit_score=7, status="drafted")
    ok = outcomes.promote_draft(conn, "https://e.com/p1",
                        "Thank you for applying! We received your application.")
    assert ok is True, "drafted should promote on ack signal"
    row = conn.execute("SELECT apply_status FROM jobs WHERE url = 'https://e.com/p1'").fetchone()
    assert row[0] == "applied", row[0]

    # non-draft job must not change
    seed_job(conn, "https://e.com/p2", fit_score=7, status="waiting")
    ok2 = outcomes.promote_draft(conn, "https://e.com/p2",
                         "Thank you for applying! We received your application.")
    assert ok2 is False
    row2 = conn.execute("SELECT apply_status FROM jobs WHERE url = 'https://e.com/p2'").fetchone()
    assert row2[0] == "waiting", row2[0]

    # non-ack signal must never promote a draft
    seed_job(conn, "https://e.com/p3", fit_score=7, status="drafted")
    ok3 = outcomes.promote_draft(conn, "https://e.com/p3",
                         "We regret to inform you we are not moving forward.")
    assert ok3 is False
    row3 = conn.execute("SELECT apply_status FROM jobs WHERE url = 'https://e.com/p3'").fetchone()
    assert row3[0] == "drafted", row3[0]


def test_raw_json_roundtrip():
    conn = _db()
    seed_job(conn, "https://e.com/r1", fit_score=5)
    outcomes.record_outcome(conn, "https://e.com/r1", status="offer",
                            raw={"from": "recruiter@", "subject": "Offer", "score": 88.5})
    row = outcomes.get_outcome(conn, "https://e.com/r1")
    import json as jsonm
    assert jsonm.loads(row["raw"])["score"] == 88.5


def test_outcome_statuses_membership():
    assert "no_response" in outcomes.OUTCOME_STATUSES
    assert "offer_declined" in outcomes.OUTCOME_STATUSES
    assert "applied" in outcomes.OUTCOME_STATUSES


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  FAIL {t.__name__}: {e!r}")
    print(f"\n{len(tests)} tests, {len(tests) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
