"""Live health of the two background loops, the queue, and recent finds.

Deliberately reports what can be *verified* and says so when it cannot. A
status view for a system running unattended is worse than useless if it shows
a green badge from a stale pid file -- this install has already lost days to
an apply stage that "finished" in two seconds and a scoring stage that logged
"Done: 11 scored" while every one was a connection error.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from jobpilot import config
from jobpilot.database import get_connection

console = Console()


def _pid_alive(pid: int) -> bool | None:
    """True/False if determinable, None if we cannot tell on this platform."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            k32.CloseHandle(handle)
            # 259 == STILL_ACTIVE
            return bool(ok) and exit_code.value == 259
        except Exception:  # noqa: BLE001
            return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001
        return None


def _age(path: Path) -> float | None:
    """Seconds since the file was last written, or None if absent."""
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _human(secs: float | None) -> str:
    if secs is None:
        return "never"
    secs = int(secs)
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m ago"
    return f"{secs // 86400}d ago"


def _jobpilot_child_alive() -> bool | None:
    """Is an jobpilot worker process running right now?

    agent_loop.ps1's RunLogged redirects each child's stdout to a temp file and
    only appends it to agent_loop.log after the child exits, so the log is
    silent for the whole of a pipeline cycle -- up to the 180-minute watchdog.
    Log age alone therefore reports a busy loop as stalled. A live child process
    is the signal that actually distinguishes "working" from "wedged".
    """
    if os.name != "nt":
        return None
    try:
        import subprocess

        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq jobpilot.exe"],
                             capture_output=True, text=True, timeout=10).stdout
        return "jobpilot.exe" in out
    except Exception:  # noqa: BLE001
        return None


def _loop(label: str, pid_file: Path, log_file: Path, stale_after: float,
          busy_check: bool = False) -> dict:
    pid = None
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass

    alive = _pid_alive(pid) if pid else False
    log_age = _age(log_file)

    # The pid file alone is not evidence: it survives a kill. Cross-check it
    # against real log activity and report the weaker of the two signals.
    # A busy pipeline buffers its output, so check for a live worker before
    # calling a quiet log a stall.
    busy = _jobpilot_child_alive() if busy_check else None

    if alive is False:
        state, note = "STOPPED", "process not running"
    elif log_age is None:
        state, note = "UNKNOWN", "no log file yet"
    elif busy:
        state, note = "RUNNING", "mid-cycle (output buffered until the stage exits)"
    elif log_age > stale_after:
        state, note = "STALLED", f"no log activity for {_human(log_age)}"
    elif alive is None:
        state, note = "LIKELY UP", "pid check unavailable; log is fresh"
    else:
        state, note = "RUNNING", f"last activity {_human(log_age)}"

    return {"label": label, "pid": pid, "state": state, "note": note}


def _bulk_stale_after() -> float:
    """Seconds of silence before the bulk loop is genuinely suspect.

    agent_loop.ps1 kills a pipeline cycle at JOBPILOT_PIPELINE_MAX_MIN
    (default 180). Anything under that is a normal quiet cycle; give it a
    20-minute grace margin on top.
    """
    try:
        watchdog = int(os.environ.get("JOBPILOT_PIPELINE_MAX_MIN", "180"))
    except ValueError:
        watchdog = 180
    return (watchdog + 20) * 60


def collect() -> dict:
    """Everything the status views show, as plain data.

    Single source of truth: `jobpilot health` renders this, and the web
    dashboard serves it at /api/health. The two disagreeing about whether a
    loop is running would be exactly the failure this project already had.
    """
    import json

    app = config.APP_DIR
    loops = [
        # A fast-lane poll is every 5 min by default; 15 min of silence is wrong.
        _loop("fast lane", app / "fast_lane.pid", app / "logs" / "fast_lane.log", 15 * 60),
        # The bulk pipeline writes nothing for a whole cycle (see
        # _jobpilot_child_alive), so its threshold must clear the watchdog
        # that bounds a cycle, not a typical stage.
        _loop("bulk loop", app / "agent_loop.pid", app / "logs" / "agent_loop.log",
              _bulk_stale_after(), busy_check=True),
    ]

    conn = get_connection()
    q = lambda sql, p=(): conn.execute(sql, p).fetchone()[0]  # noqa: E731
    min_score = config.DEFAULTS["min_score"]
    max_att = config.DEFAULTS["max_apply_attempts"]

    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    stages = []
    for label, col in (("discovered", "discovered_at"), ("enriched", "detail_scraped_at"),
                       ("scored", "scored_at"), ("tailored", "tailored_at"),
                       ("cover letters", "cover_letter_at"), ("applied", "applied_at")):
        stages.append({
            "stage": label,
            "total": q(f"SELECT COUNT(*) FROM jobs WHERE {col} IS NOT NULL"),
            "last_24h": q(f"SELECT COUNT(*) FROM jobs WHERE {col} > ?", (day_ago,)),
        })

    # Acquirable == what apply could actually pick up, blocked sites excluded.
    # The dashboard's older "ready to apply" ignores that filter and reads high.
    blocked, _ = config.load_blocked_sites()
    extra, params = "", [min_score, max_att]
    if blocked:
        extra = f" AND site NOT IN ({','.join('?' * len(blocked))})"
        params.extend(blocked)
    acquirable = q(
        "SELECT COUNT(*) FROM jobs WHERE fit_score >= ? "
        "AND tailored_resume_path IS NOT NULL AND cover_letter_path IS NOT NULL "
        "AND applied_at IS NULL AND (apply_status IS NULL OR apply_status = 'failed') "
        "AND (apply_attempts IS NULL OR apply_attempts < ?)" + extra, params)

    counters = {
        "new_last_hour": q("SELECT COUNT(*) FROM jobs WHERE discovered_at > ?", (hour_ago,)),
        "unscored_backlog": q("SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL "
                              "AND fit_score IS NULL"),
        "acquirable": acquirable,
        "manual": q("SELECT COUNT(*) FROM jobs WHERE apply_status = 'manual'"),
    }

    # Recent fast-lane matches, from the JSONL worklist.
    matches: list[dict] = []
    path = app / "fresh_jobs.jsonl"
    try:
        with open(path, encoding="utf-8") as fh:
            rows = []
            for line in fh.readlines()[-400:]:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        prepped = {r["url"]: r for r in rows if r.get("stage") == "prepped" and r.get("url")}
        matches = list(prepped.values())[-8:][::-1]
    except OSError:
        pass

    return {"loops": loops, "stages": stages, "counters": counters,
            "matches": matches, "worklist": str(path)}


def report() -> dict:
    """Render collect() to the terminal."""
    data = collect()

    t = Table(title="JobPilot loops", title_style="bold")
    for col in ("loop", "pid", "state", "detail"):
        t.add_column(col)
    for lp in data["loops"]:
        colour = {"RUNNING": "green", "LIKELY UP": "green", "STALLED": "yellow",
                  "STOPPED": "red", "UNKNOWN": "yellow"}.get(lp["state"], "white")
        t.add_row(lp["label"], str(lp["pid"] or "-"),
                  f"[{colour}]{lp['state']}[/{colour}]", lp["note"])
    console.print(t)

    f = Table(title="Pipeline", title_style="bold")
    f.add_column("stage")
    f.add_column("total", justify="right")
    f.add_column("last 24h", justify="right")
    for row in data["stages"]:
        f.add_row(row["stage"], str(row["total"]), str(row["last_24h"]))
    console.print(f)

    c = data["counters"]
    console.print(f"\n  new in last hour   : [bold]{c['new_last_hour']}[/bold]")
    console.print(f"  unscored backlog   : {c['unscored_backlog']}")
    console.print(f"  acquirable to apply: [bold]{c['acquirable']}[/bold]   "
                  f"[dim](blocked sites excluded -- the dashboard's count is not)[/dim]")
    console.print(f"  retired as manual  : {c['manual']}")

    if data["matches"]:
        m = Table(title="Recent fast-lane matches (prepared, awaiting you)", title_style="bold")
        for col in ("fit", "title", "where to apply"):
            m.add_column(col)
        for r in data["matches"]:
            app = r.get("application_url")
            if not (isinstance(app, str) and app.startswith("http")):
                app = None  # guard against NULL / legacy literal-"None" strings
            m.add_row(str(r.get("fit_score", "?")), (r.get("title") or "?")[:46],
                      (app or r.get("url") or "")[:58])
        console.print(m)
        console.print(f"  [dim]full worklist with document paths: {data['worklist']}[/dim]")
    else:
        console.print(f"\n  [dim]no prepared matches recorded yet -- {data['worklist']}[/dim]")

    return data
