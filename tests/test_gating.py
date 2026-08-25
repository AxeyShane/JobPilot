"""Tests for jobpilot.gating -- pre-score hard gates.

Run directly (no deps):  python3 tests/test_gating.py
or with pytest:          python3 -m pytest tests/test_gating.py
"""

import os
import sys
import traceback

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from jobpilot.gating import evaluate_eligibility, evaluate_language

# --------------------------------------------------------------------------- #
# A) ELIGIBILITY GATE
# --------------------------------------------------------------------------- #

US_OK = {"legally_authorized_to_work": True, "require_sponsorship": False, "work_permit_type": ""}
NEEDS_SPONSOR = {"legally_authorized_to_work": True, "require_sponsorship": True, "work_permit_type": "H-1B"}


def test_eligibility_hard_citizenship_fail():
    v = evaluate_eligibility(
        "Must be a US citizen, permanent resident, or otherwise authorized to work "
        "in the United States for this federal role.", US_OK)
    assert v["gate"] == "eligibility"
    assert v["verdict"] == "FAIL", v
    assert v["quoted"], "FAIL must quote exact wording"
    assert "citizen" in v["quoted"].lower()


def test_eligibility_clearance_fail():
    v = evaluate_eligibility(
        "Position requires active Top Secret security clearance.", US_OK)
    assert v["verdict"] == "FAIL", v
    assert v["quoted"] and "clearance" in v["quoted"].lower()


def test_eligibility_named_permit_pass():
    v = evaluate_eligibility(
        "We welcome H-1B candidates and offer visa sponsorship.", NEEDS_SPONSOR)
    assert v["verdict"] == "PASS", v


def test_eligibility_permit_type_explicit_pass():
    v = evaluate_eligibility(
        "Candidates on an H-1B visa are encouraged to apply.", NEEDS_SPONSOR)
    assert v["verdict"] == "PASS", v


def test_eligibility_right_to_work_authorized_pass():
    v = evaluate_eligibility(
        "You must be legally authorized to work in the United States.", US_OK)
    assert v["verdict"] == "PASS", v


def test_eligibility_right_to_work_needs_sponsor_proceed():
    v = evaluate_eligibility(
        "Candidates must have the right to work in the US.", NEEDS_SPONSOR)
    assert v["verdict"] == "PROCEED", v


def test_eligibility_international_welcome_pass():
    v = evaluate_eligibility(
        "International applicants welcome. Visa sponsorship available.", US_OK)
    assert v["verdict"] == "PASS", v


def test_eligibility_silent_unverified():
    v = evaluate_eligibility(
        "We are seeking a senior software engineer with 5 years of Python experience.", US_OK)
    assert v["verdict"] == "UNVERIFIED", v
    assert v["quoted"] is None


def test_eligibility_shared_dict_shape():
    v = evaluate_eligibility("silent posting here", US_OK)
    assert set(v) >= {"gate", "verdict", "reason", "quoted", "details"}


# --------------------------------------------------------------------------- #
# B) LANGUAGE GATE
# --------------------------------------------------------------------------- #

LANG_DICT = [
    {"language": "English", "level": "native"},
    {"language": "Spanish", "level": "B2"},
]
LANG_STRINGS = ["English - native", "German - C1"]


def test_language_all_met_pass():
    v = evaluate_language("We require fluent English for all positions.", LANG_DICT)
    assert v["gate"] == "language"
    assert v["verdict"] == "PASS", v
    assert v["language_details"]


def test_language_missing_row_fail():
    v = evaluate_language("Must speak fluent Japanese.", LANG_DICT)
    assert v["verdict"] == "FAIL", v
    assert any(d["language"] == "Japanese" for d in v["language_details"])


def test_language_lower_level_flag():
    v = evaluate_language("Strong business-level Spanish required.", LANG_DICT)
    # candidate Spanish = B2 (index 2); "business" == 2 -> at/equal => PASS
    assert v["verdict"] in ("PASS", "FLAG"), v
    v2 = evaluate_language("Native Spanish required.", LANG_DICT)
    assert v2["verdict"] == "FLAG", v2


def test_language_strings_input():
    v = evaluate_language("Native English required.", LANG_STRINGS)
    assert v["verdict"] == "PASS", v


def test_language_format_requirement_pass():
    v = evaluate_language("Excellent French and German skills are a plus.", [{"language": "French", "level": "b2"}])
    assert v["verdict"] in ("PASS", "FLAG"), v


def test_language_no_requirements_pass():
    v = evaluate_language("No language requirements for this role.", LANG_DICT)
    assert v["verdict"] == "PASS", v


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def _all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
            print(f"  ok  {fn.__name__}")
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa
            print(f"ERROR {fn.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{passed}/{len(fns)} tests passed")
    return passed == len(fns)


if __name__ == "__main__":
    sys.exit(0 if _all() else 1)
