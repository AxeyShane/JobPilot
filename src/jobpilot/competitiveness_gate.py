"""Applicant-count competitiveness gate.

LinkedIn (and some other aggregators) surface a running applicant count on
the job detail page -- "43 applicants", "Over 200 applicants", "Be among
the first 25 applicants". A human screener works that queue roughly in
arrival order and stops well short of the bottom once volume gets high, so
the earliest applicants get materially better odds of a resume actually
being opened than anyone who lands after the pile-up starts. By the time
this pipeline could discover, score, tailor, and hand a job back for you to
apply, a posting that already reports 30+ applicants is very unlikely to
get a first look from a fresh application -- however well the resume is
tailored.

This gate does not judge fit. A high fit_score job can still get retired
here because it is already saturated, and a mediocre-fit job with a low
applicant count is left alone -- the two questions are independent.

Fail-open on missing data: most job sites never expose an applicant count
at all. That is not the same thing as "0 applicants", so parsing returns
None rather than a fabricated number, and None is always a pass-through
("ok") rather than "retired" -- the gate should never silently vouch for a
job it did not actually get a real count for, but it also must never punish
a job just because the site doesn't publish the number.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Override with JOBPILOT_MAX_APPLICANTS. Matches the STAGE_LIMIT convention
# used elsewhere (tailor.py, cover_letter.py) -- a module-level constant read
# from the environment once, rather than threaded through config.DEFAULTS.
DEFAULT_MAX_APPLICANTS = int(os.environ.get("JOBPILOT_MAX_APPLICANTS", "30"))

# "Be among the first N applicants" is a CEILING the count has not yet
# reached -- it means fewer than N have applied, not that N have. Checked
# first so it is never misread as a count of N.
_CEILING_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bbe\s+among\s+the\s+first\s+(\d{1,4})\s+applicants\b", re.IGNORECASE),
]

# Real applicant-count phrasings, most-specific first. "Over N" / "N+" report
# a floor, not an exact count -- using the number as-is can only undercount,
# never overcount, so it's safe to treat the same as an exact figure here.
_COUNT_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bover\s+(\d{1,4})\s+applicants\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,4})\+\s+applicants\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,4})\s+applicants\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,4})\s+people\s+clicked\s+apply\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,4})\s+applications?\s+submitted\b", re.IGNORECASE),
]


def parse_applicant_count(text: str | None) -> int | None:
    """Pull a LinkedIn/ATS-style applicant count out of visible page text.

    Returns None when no recognizable phrasing is present -- most sites never
    show this number, and that must not be conflated with a real "0".
    """
    if not text:
        return None

    for pat in _CEILING_PATTERNS:
        m = pat.search(text)
        if m:
            # "Be among the first N applicants" means the true count is
            # below N and the posting is fresh -- record it as a known-low
            # count (0) rather than an unknown, so it doesn't need a second
            # detail-page visit later to confirm the job isn't saturated.
            return 0

    for pat in _COUNT_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue

    return None


@dataclass
class CompetitivenessVerdict:
    verdict: str = "ok"  # "ok" | "retired"
    reason: str = ""
    applicant_count: int | None = None
    threshold: int = DEFAULT_MAX_APPLICANTS


def evaluate_competitiveness(
    applicant_count: int | None,
    threshold: int | None = None,
) -> CompetitivenessVerdict:
    """Decide whether a job is still worth tailoring/applying to.

    ``threshold`` defaults to JOBPILOT_MAX_APPLICANTS (30 applicants) -- past
    that point a fresh application is very unlikely to get a first look,
    however good the fit. Jobs with no known applicant count always pass
    ("ok") -- there is nothing to gate on.
    """
    limit = DEFAULT_MAX_APPLICANTS if threshold is None else threshold

    if applicant_count is None:
        return CompetitivenessVerdict(
            verdict="ok",
            reason="No applicant count available for this posting",
            applicant_count=None,
            threshold=limit,
        )

    if applicant_count >= limit:
        return CompetitivenessVerdict(
            verdict="retired",
            reason=(
                f"{applicant_count} applicants already -- at/over the "
                f"{limit}-applicant threshold, unlikely to get a first look"
            ),
            applicant_count=applicant_count,
            threshold=limit,
        )

    return CompetitivenessVerdict(
        verdict="ok",
        reason=f"{applicant_count} applicants, under the {limit}-applicant threshold",
        applicant_count=applicant_count,
        threshold=limit,
    )
