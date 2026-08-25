"""Regenerate the resume and cover letter for jobs already prepared.

The pipeline never redoes work: `pending_tailor` selects on
`tailored_resume_path IS NULL`, so once a job has documents it is skipped
forever. That is right for normal running and wrong after you edit
resume.txt -- the new material would only ever reach jobs discovered later.

This clears the document fields for the jobs you name and rebuilds them from
the current resume.txt and profile.json.

Usage:
    ... scripts\\retailor.py <url> [<url> ...]      # specific jobs
    ... scripts\\retailor.py --acquirable           # everything apply could pick up
    ... scripts\\retailor.py <url> --dry-run        # show what would change
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from jobpilot import config                      # noqa: E402
from jobpilot.cli import _bootstrap              # noqa: E402

CLEAR = """UPDATE jobs SET
             tailored_resume_path = NULL, tailored_at = NULL, tailor_attempts = 0,
             cover_letter_path = NULL,    cover_letter_at = NULL, cover_attempts = 0
           WHERE url = ?"""


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    dry = "--dry-run" in flags

    _bootstrap()
    from jobpilot.database import get_connection
    from jobpilot.scoring.cover_letter import run_cover_letters
    from jobpilot.scoring.tailor import run_tailoring

    conn = get_connection()
    min_score = config.DEFAULTS["min_score"]

    if "--acquirable" in flags:
        blocked, _ = config.load_blocked_sites()
        params: list = [min_score]
        clause = ""
        if blocked:
            clause = f" AND site NOT IN ({','.join('?' * len(blocked))})"
            params.extend(blocked)
        urls = [r[0] for r in conn.execute(
            "SELECT url FROM jobs WHERE fit_score >= ? AND tailored_resume_path IS NOT NULL "
            "AND applied_at IS NULL" + clause, params)]
    else:
        urls = args

    if not urls:
        print("Nothing to do. Pass one or more job URLs, or --acquirable.")
        return

    rows = conn.execute(
        f"SELECT url, title, fit_score FROM jobs WHERE url IN ({','.join('?' * len(urls))})",
        urls).fetchall()
    found = {r[0] for r in rows}
    for u in urls:
        if u not in found:
            print(f"  NOT IN DB: {u}")
    if not rows:
        return

    print(f"{'Would regenerate' if dry else 'Regenerating'} {len(rows)} job(s):")
    for r in rows:
        print(f"   fit {r[2]}  {str(r[1])[:60]}")
    if dry:
        return

    for r in rows:
        conn.execute(CLEAR, (r[0],))
    conn.commit()

    targets = [r[0] for r in rows]
    run_tailoring(min_score=min_score, limit=len(targets), urls=targets, workers=1)
    run_cover_letters(min_score=min_score, limit=len(targets), urls=targets, workers=1)

    print("\nResult:")
    for r in conn.execute(
        f"SELECT title, tailored_resume_path, cover_letter_path FROM jobs "
        f"WHERE url IN ({','.join('?' * len(targets))})", targets
    ):
        print(f"  {str(r[0])[:52]}")
        print(f"     resume: {r[1] or 'FAILED'}")
        print(f"     cover : {r[2] or 'FAILED'}")


if __name__ == "__main__":
    main()
