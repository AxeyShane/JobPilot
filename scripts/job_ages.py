"""How stale is the apply queue?

JobPilot stores no posting date and no expiry, so `discovered_at` is the only
age signal -- and it is a floor, not the truth: with hours_old=168 a job could
already have been a week old when first seen. Treat these numbers as "at least
this old".

Usage:
    ... scripts\\job_ages.py            # age profile of the acquirable queue
    ... scripts\\job_ages.py --list     # every job, oldest first
"""

from __future__ import annotations

import collections
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from jobpilot import config          # noqa: E402
from jobpilot.cli import _bootstrap  # noqa: E402


def main() -> None:
    _bootstrap()
    from jobpilot.database import get_connection

    conn = get_connection()
    min_score = config.DEFAULTS["min_score"]
    blocked, _ = config.load_blocked_sites()

    params: list = [min_score, config.DEFAULTS["max_apply_attempts"]]
    clause = ""
    if blocked:
        clause = f" AND site NOT IN ({','.join('?' * len(blocked))})"
        params.extend(blocked)

    rows = conn.execute(
        "SELECT url, title, site, fit_score, discovered_at, application_url "
        "FROM jobs WHERE fit_score >= ? "
        "AND tailored_resume_path IS NOT NULL AND cover_letter_path IS NOT NULL "
        "AND applied_at IS NULL AND (apply_status IS NULL OR apply_status = 'failed') "
        "AND (apply_attempts IS NULL OR apply_attempts < ?)" + clause +
        " ORDER BY discovered_at ASC", params).fetchall()

    if not rows:
        print("Queue is empty.")
        return

    now = datetime.datetime.now(datetime.timezone.utc)

    def age_days(v):
        try:
            return (now - datetime.datetime.fromisoformat(v)).days
        except Exception:  # noqa: BLE001
            return None

    BUCKETS = [(2, "0-2 days"), (7, "3-7 days"), (14, "8-14 days"),
               (30, "15-30 days"), (10**6, "31+ days")]
    counts = collections.Counter()
    for r in rows:
        a = age_days(r[4])
        counts[next(lbl for lim, lbl in BUCKETS if a is not None and a <= lim)
               if a is not None else "unknown"] += 1

    print(f"acquirable queue: {len(rows)} job(s)")
    print("\nage since DISCOVERED (a floor -- the posting was already older):")
    for _, lbl in BUCKETS:
        if counts[lbl]:
            bar = "#" * min(50, counts[lbl])
            print(f"  {lbl:<11} {counts[lbl]:>4}  {bar}")
    if counts["unknown"]:
        print(f"  {'unknown':<11} {counts['unknown']:>4}")

    fresh = sum(counts[lbl] for _, lbl in BUCKETS[:2])
    stale = sum(counts[lbl] for _, lbl in BUCKETS[3:])
    print(f"\n  under a week : {fresh}   <- worth regenerating documents for")
    print(f"  over 15 days : {stale}   <- many of these are probably closed")

    if "--list" in sys.argv:
        print("\noldest first:")
        for r in rows:
            a = age_days(r[4])
            print(f"  {str(a):>4}d  fit {r[3]}  {str(r[1])[:44]:<44} "
                  f"{str(r[5] or r[0])[:52]}")


if __name__ == "__main__":
    main()
