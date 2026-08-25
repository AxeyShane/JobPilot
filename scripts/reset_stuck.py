"""Return jobs that a dead provider pushed out of the queue.

Two counters silently retire jobs when an LLM endpoint is unreachable, because
a transport failure was being recorded as a failed attempt at the job:

  cover_attempts >= 5   -> excluded from the cover-letter stage
  apply_attempts >= 10  -> excluded from the apply queue

Both are now guarded at the source, but rows already at the cap stay there
forever. This resets them. Safe to re-run; writes a reversible JSON record to
backups/ before changing anything.

Usage:
    .venv\\Scripts\\python.exe scripts\\reset_stuck.py            # show only
    .venv\\Scripts\\python.exe scripts\\reset_stuck.py --apply    # do it
"""

from __future__ import annotations

import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from jobpilot import config          # noqa: E402
from jobpilot.config import load_env  # noqa: E402
from jobpilot.database import get_connection  # noqa: E402

GROUPS = {
    "cover": (
        "cover_attempts",
        """tailored_resume_path IS NOT NULL
           AND (cover_letter_path IS NULL OR cover_letter_path = '')
           AND COALESCE(cover_attempts, 0) >= 5""",
    ),
    "apply": (
        "apply_attempts",
        """tailored_resume_path IS NOT NULL
           AND applied_at IS NULL
           AND COALESCE(apply_attempts, 0) >= 10
           AND (apply_status IS NULL OR apply_status = 'failed')""",
    ),
}


def main() -> None:
    do_it = "--apply" in sys.argv
    load_env()
    conn = get_connection()

    total = 0
    snapshot: dict[str, list] = {}

    for name, (column, where) in GROUPS.items():
        rows = conn.execute(
            f"SELECT url, title, {column} FROM jobs WHERE {where}"
        ).fetchall()
        snapshot[name] = [{"url": r[0], "title": r[1], column: r[2]} for r in rows]
        total += len(rows)
        print(f"  {name:<6} stuck at the {column} cap: {len(rows)}")

    if not total:
        print("\nNothing stuck. No action needed.")
        return

    if not do_it:
        print(f"\n{total} job(s) would be returned to the queue.")
        print("Re-run with --apply to do it.")
        return

    backups = config.APP_DIR / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = backups / f"reset_stuck_{stamp}.json"
    path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(f"\nreversible record: {path}")

    for name, (column, where) in GROUPS.items():
        cur = conn.execute(f"UPDATE jobs SET {column} = 0 WHERE {where}")
        print(f"  {name:<6} reset: {cur.rowcount}")
    conn.commit()

    eligible = conn.execute(
        """SELECT COUNT(*) FROM jobs WHERE fit_score >= ?
           AND tailored_resume_path IS NOT NULL
           AND (cover_letter_path IS NULL OR cover_letter_path = '')
           AND COALESCE(cover_attempts, 0) < 5""",
        (config.DEFAULTS["min_score"],),
    ).fetchone()[0]
    print(f"\nnow eligible for a cover letter: {eligible}")


if __name__ == "__main__":
    main()
