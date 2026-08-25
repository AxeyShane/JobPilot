"""Print the number of jobs auto-apply could actually pick up right now.

A separate file on purpose. This query contains `<` and quotes, and PowerShell
parses `<` as a reserved redirection operator even inside a quoted -c argument,
so inlining it into agent_loop.ps1 makes the whole script unparseable.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

try:
    from jobpilot import config
    from jobpilot.config import load_env
    from jobpilot.database import get_connection

    load_env()
    conn = get_connection()

    min_score = int(sys.argv[1]) if len(sys.argv) > 1 else config.DEFAULTS["min_score"]
    params: list = [min_score, config.DEFAULTS["max_apply_attempts"]]

    # Blocked sites never enter the queue, so a count that ignores them is
    # the inflated number the dashboard shows. Exclude them here.
    blocked, _ = config.load_blocked_sites()
    site_clause = ""
    if blocked:
        site_clause = f" AND site NOT IN ({','.join('?' * len(blocked))})"
        params.extend(blocked)

    sql = (
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score >= ? "
        "AND tailored_resume_path IS NOT NULL "
        "AND cover_letter_path IS NOT NULL "
        "AND applied_at IS NULL "
        "AND (apply_status IS NULL OR apply_status = 'failed') "
        "AND (apply_attempts IS NULL OR apply_attempts < ?)" + site_clause
    )
    print(conn.execute(sql, params).fetchone()[0])
except Exception as e:  # noqa: BLE001
    # Never let a status line break the loop that reports it.
    print(f"? ({type(e).__name__})")
