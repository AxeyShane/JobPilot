"""Tests for jobpilot.competitiveness_gate -- applicant-count saturation gate.

Run directly (no deps):  python3 tests/test_competitiveness_gate.py
or with pytest:          python3 -m pytest tests/test_competitiveness_gate.py
"""

import os
import sys
import traceback

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from jobpilot.competitiveness_gate import evaluate_competitiveness, parse_applicant_count

# --------------------------------------------------------------------------- #
# parse_applicant_count
# --------------------------------------------------------------------------- #

def test_parse_plain_count():
    assert parse_applicant_count("Alex Doe  ·  43 applicants") == 43


def test_parse_over_count():
    assert parse_applicant_count("Over 200 applicants") == 200


def test_parse_plus_count():
    assert parse_applicant_count("150+ applicants") == 150


def test_parse_clicked_apply_phrasing():
    assert parse_applicant_count("87 people clicked apply") == 87


def test_parse_ceiling_phrasing_is_not_a_count():
    # "Be among the first N applicants" means fewer than N have applied --
    # it must not be read as a count of N.
    assert parse_applicant_count("Be among the first 25 applicants") == 0


def test_parse_no_signal_returns_none():
    # Most job sites never expose this number at all -- that's not "0".
    assert parse_applicant_count("Senior Backend Engineer at Acme Corp") is None


def test_parse_empty_and_none_input():
    assert parse_applicant_count("") is None
    assert parse_applicant_count(None) is None


def test_parse_case_insensitive():
    assert parse_applicant_count("OVER 55 APPLICANTS") == 55


# --------------------------------------------------------------------------- #
# evaluate_competitiveness
# --------------------------------------------------------------------------- #

def test_under_threshold_is_ok():
    v = evaluate_competitiveness(12, threshold=30)
    assert v.verdict == "ok"
    assert v.applicant_count == 12
    assert "12 applicants" in v.reason


def test_at_threshold_is_retired():
    v = evaluate_competitiveness(30, threshold=30)
    assert v.verdict == "retired"
    assert "30" in v.reason


def test_over_threshold_is_retired():
    v = evaluate_competitiveness(212, threshold=30)
    assert v.verdict == "retired"


def test_none_count_always_passes():
    # Fail-open: no data means no verdict against it, regardless of threshold.
    v = evaluate_competitiveness(None, threshold=1)
    assert v.verdict == "ok"
    assert v.applicant_count is None


def test_default_threshold_is_30():
    v = evaluate_competitiveness(29)
    assert v.verdict == "ok"
    v = evaluate_competitiveness(30)
    assert v.verdict == "retired"


# --------------------------------------------------------------------------- #
# Runner (mirrors tests/test_gating.py so this works without pytest too)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception:
            failed += 1
            print(f"FAILED: {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
