"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import json
import logging
import os
import platform
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.live import Live

from jobpilot import config
from jobpilot.database import get_connection
from jobpilot.apply import chrome, dashboard, prompt as prompt_mod
from jobpilot.apply.result import extract_result, is_permanent_failure
from jobpilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT,
)
from jobpilot.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full, get_totals,
)

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from jobpilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Track active Claude Code processes for skip (Ctrl+C) handling
_claude_procs: dict[int, subprocess.Popen] = {}

# An apply attempt that has been 'in_progress' longer than this cannot still be
# alive -- agent_loop's watchdog caps a whole apply cycle well below it.
STALE_CLAIM_MINUTES = 45

# Upper bound on manual-ATS rows skipped in one acquire call.
_MAX_MANUAL_SKIPS = 50
_claude_lock = threading.Lock()

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port.

    Uses npx's fully-resolved path rather than bare "npx" -- Claude Code
    CLI's internal MCP server spawn never completed with the bare command
    even when npx's directory was correctly on this process's PATH (observed
    in production; likely a different resolution mechanism than Python's
    subprocess uses internally). An absolute path removes the ambiguity for
    whichever spawner ends up reading this config.
    """
    npx = config.find_npx() or "npx"
    return {
        "mcpServers": {
            "playwright": {
                "command": npx,
                "args": [
                    "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            },
            "gmail": {
                "command": npx,
                "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
            },
        }
    }


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def acquire_job(target_url: str | None = None, min_score: int = 6,
                worker_id: int = 0) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    _queue_sql = ""
    _queue_params: list = []
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Reclaim orphaned claims. A worker marks a job 'in_progress' before
        # driving the browser; if that process is killed (agent_loop's
        # watchdog, a crash, a reboot) the row keeps that status forever and
        # the job silently leaves the queue -- 'in_progress' matches neither
        # the NULL nor the 'failed' branch below. Anything claimed longer ago
        # than STALE_CLAIM_MINUTES cannot still be running, so put it back.
        stale_before = (datetime.now(timezone.utc)
                        - timedelta(minutes=STALE_CLAIM_MINUTES)).isoformat()
        reclaimed = conn.execute(
            "UPDATE jobs SET apply_status = 'failed', "
            "apply_error = COALESCE(apply_error, 'orphaned in_progress claim') "
            "WHERE apply_status = 'in_progress' "
            "AND (last_attempted_at IS NULL OR last_attempted_at < ?)",
            (stale_before,),
        ).rowcount
        if reclaimed:
            logger.info("Reclaimed %d orphaned in_progress job(s)", reclaimed)

        if target_url:
            like = f"%{target_url.split('?')[0].rstrip('/')}%"
            row = conn.execute("""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                  AND tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status != 'in_progress')
                  AND (scam_verdict = 'clear' OR strategy = 'workday_api')
                LIMIT 1
            """, (target_url, target_url, like, like)).fetchone()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            # Build parameterized filters to avoid SQL injection
            params: list = [min_score]
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join(f"AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            _queue_sql = f"""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status = 'failed')
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  AND fit_score >= ?
                  AND (scam_verdict = 'clear' OR strategy = 'workday_api')
                  {site_clause}
                  {url_clauses}
                -- Freshness-first apply queue:
                -- 1. Primary: discovery day DESC (newest postings first so we apply within minutes
                --    of posting; on competitive boards, first 10-30 applicants get 30-50% higher callback).
                --    NULL or empty discovered_at treated as oldest (sorted last).
                -- 2. Secondary: least-attempts-first WITHIN the same discovery day (so a fresh-untried
                --    job beats a fresh-retried one, but an old job never beats a newer one).
                -- 3. Tertiary: latest exact timestamp (discovered_at DESC) within the same day/attempt bucket.
                -- 4. Quaternary: fit_score DESC, then url ASC for deterministic tie-breaking.
                ORDER BY COALESCE(substr(discovered_at, 1, 10), '') DESC,
                         COALESCE(apply_attempts, 0) ASC,
                         COALESCE(discovered_at, '') DESC,
                         fit_score DESC,
                         url ASC
                LIMIT 1
            """
            _queue_params = [config.DEFAULTS["max_apply_attempts"]] + params
            row = conn.execute(_queue_sql, _queue_params).fetchone()

        if not row:
            conn.rollback()
            return None

        # Skip manual ATS sites (unsolvable CAPTCHAs, aggregator bot walls).
        # Returning None here would tell the worker the queue is empty, which
        # is wrong -- it just means the TOP row is unapplyable. With indeed and
        # linkedin now on the manual list that is most of the queue, so mark it
        # and take the next candidate instead of ending the pass. Bounded so a
        # queue that is entirely manual still terminates.
        from jobpilot.config import is_manual_ats

        skipped = 0
        while is_manual_ats(row["application_url"] or row["url"]):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            skipped += 1
            logger.info("Skipping manual ATS (%d): %s", skipped, row["url"][:80])
            if skipped >= _MAX_MANUAL_SKIPS or target_url:
                return None

            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(_queue_sql, _queue_params).fetchone()
            if not row:
                conn.rollback()
                return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?
            WHERE url = ?
        """, (f"worker-{worker_id}", now, row["url"]))
        conn.commit()

        return dict(row)
    except Exception:
        conn.rollback()
        raise


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, url))
    conn.commit()


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 6,
               model: str = "sonnet", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text)

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """, (now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress')
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False) -> tuple[str, int]:
    """Spawn a Claude Code session for one job application.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    # Build the prompt
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
    )

    # Write per-worker MCP config
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    # Build claude command. Resolved path, not bare "claude" -- shutil.which
    # alone can miss it when invoked through a spawn chain that skips the
    # shell profile script normally adding it to PATH (see config.find_claude_cli).
    cmd = [
        config.find_claude_cli() or "claude",
        "--model", model,
        "-p",
        "--mcp-config", str(mcp_config_path),
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--disallowedTools", (
            "mcp__gmail__draft_email,mcp__gmail__modify_email,"
            "mcp__gmail__delete_email,mcp__gmail__download_attachment,"
            "mcp__gmail__batch_modify_emails,mcp__gmail__batch_delete_emails,"
            "mcp__gmail__create_label,mcp__gmail__update_label,"
            "mcp__gmail__delete_label,mcp__gmail__get_or_create_label,"
            "mcp__gmail__list_email_labels,mcp__gmail__create_filter,"
            "mcp__gmail__list_filters,mcp__gmail__get_filter,"
            "mcp__gmail__delete_filter"
        ),
        "--output-format", "stream-json",
        "--verbose", "-",
    ]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    # npx.cmd internally shells out to bare "node" -- if node's own directory
    # isn't on THIS process's PATH (observed: true whenever apply is launched
    # from a shell that doesn't source the Node install's profile script,
    # e.g. this session), the Playwright MCP server subprocess fails before
    # it ever reaches the CDP connection, and the Claude agent just sees
    # "browser tools unavailable" with no indication why. Same class of
    # fragile-PATH issue find_npx()/find_claude_cli() already exist for.
    npx_path = config.find_npx()
    if npx_path:
        node_dir = str(Path(npx_path).parent)
        if node_dir not in env.get("PATH", ""):
            env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")

    worker_dir = reset_worker_dir(worker_id)

    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    proc = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
        )
        with _claude_lock:
            _claude_procs[worker_id] = proc

        proc.stdin.write(agent_prompt)
        proc.stdin.close()

        text_parts: list[str] = []
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type")
                    if msg_type == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            bt = block.get("type")
                            if bt == "text":
                                text_parts.append(block["text"])
                                lf.write(block["text"] + "\n")
                            elif bt == "tool_use":
                                name = (
                                    block.get("name", "")
                                    .replace("mcp__playwright__", "")
                                    .replace("mcp__gmail__", "gmail:")
                                )
                                inp = block.get("input", {})
                                if "url" in inp:
                                    desc = f"{name} {inp['url'][:60]}"
                                elif "ref" in inp:
                                    desc = f"{name} {inp.get('element', inp.get('text', ''))}"[:50]
                                elif "fields" in inp:
                                    desc = f"{name} ({len(inp['fields'])} fields)"
                                elif "paths" in inp:
                                    desc = f"{name} upload"
                                else:
                                    desc = name

                                lf.write(f"  >> {desc}\n")
                                ws = get_state(worker_id)
                                cur_actions = ws.actions if ws else 0
                                update_state(worker_id,
                                             actions=cur_actions + 1,
                                             last_action=desc[:35])
                    elif msg_type == "result":
                        stats = {
                            "input_tokens": msg.get("usage", {}).get("input_tokens", 0),
                            "output_tokens": msg.get("usage", {}).get("output_tokens", 0),
                            "cache_read": msg.get("usage", {}).get("cache_read_input_tokens", 0),
                            "cache_create": msg.get("usage", {}).get("cache_creation_input_tokens", 0),
                            "cost_usd": msg.get("total_cost_usd", 0),
                            "turns": msg.get("num_turns", 0),
                        }
                        text_parts.append(msg.get("result", ""))
                except json.JSONDecodeError:
                    text_parts.append(line)
                    lf.write(line + "\n")

        proc.wait(timeout=300)
        returncode = proc.returncode
        proc = None

        if returncode and returncode < 0:
            return "skipped", int((time.time() - start) * 1000)

        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"claude_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        status = extract_result(output)
        if status:
            add_event(f"[W{worker_id}] {status.split(':')[0].upper()} ({elapsed}s): {job['title'][:30]}")
            update_state(worker_id, status=status.split(":")[0],
                         last_action=f"{status} ({elapsed}s)")
            return status, duration_ms

        add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
        return "failed:no_result_line", duration_ms

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
        return "failed:timeout", duration_ms
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(e)[:25]}")
        return f"failed:{str(e)[:100]}", duration_ms
    finally:
        with _claude_lock:
            _claude_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)


# ---------------------------------------------------------------------------
# Permanent failure classification is owned by jobpilot.apply.result so both
# engines share one normalization + classification (prevents divergence).
def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried (shared canonical logic)."""
    return is_permanent_failure(result)


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Rate limiting & Pacing Guard
# ---------------------------------------------------------------------------

class RateLimiter:
    """Thread-safe rate-limiting and pacing guard for apply workers.

    Prevents parallel workers from hitting the same job board too rapidly
    (per-site pacing interval, default 60s) and enforces daily action caps
    (e.g. LinkedIn <= 100 actions/day) across all workers.
    """

    def __init__(
        self,
        default_min_interval: float | None = None,
        site_intervals: dict[str, float] | None = None,
        daily_caps: dict[str, int] | None = None,
        default_daily_cap: int | None = None,
    ):
        if default_min_interval is None:
            env_val = os.environ.get("JOBPILOT_APPLY_MIN_INTERVAL") or os.environ.get("APPLY_SITE_MIN_INTERVAL")
            self.default_min_interval = float(env_val) if env_val else 60.0
        else:
            self.default_min_interval = float(default_min_interval)

        self.site_intervals = site_intervals or {}

        # Default daily cap for LinkedIn is 100 actions/day
        env_li_cap = os.environ.get("JOBPILOT_LINKEDIN_DAILY_CAP") or os.environ.get("APPLY_LINKEDIN_DAILY_CAP")
        li_cap = int(env_li_cap) if env_li_cap else 100

        self.daily_caps = {"linkedin": li_cap}
        if daily_caps:
            self.daily_caps.update(daily_caps)

        env_default_cap = os.environ.get("JOBPILOT_SITE_DAILY_CAP") or os.environ.get("APPLY_SITE_DAILY_CAP")
        self.default_daily_cap = int(env_default_cap) if env_default_cap else (default_daily_cap or 250)

        self._lock = threading.Lock()
        self._last_action_times: dict[str, float] = {}
        self._daily_counts: dict[str, tuple[str, int]] = {}  # site -> (date_str, count)

    def normalize_site(self, site_or_url: str | None) -> str:
        """Normalize site name or URL into a canonical site key."""
        if not site_or_url:
            return "unknown"
        s = str(site_or_url).strip().lower()
        if "://" in s or "/" in s:
            domain = s.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]
            domain = domain.split(":")[0]
            if domain.startswith("www."):
                domain = domain[4:]
            for known in (
                "linkedin", "indeed", "workday", "greenhouse", "lever",
                "smartrecruiters", "ashby", "dice", "ziprecruiter", "glassdoor",
            ):
                if known in domain:
                    return known
            return domain or s
        return s

    def get_min_interval(self, site: str) -> float:
        """Get the configured min interval for a site."""
        norm = self.normalize_site(site)
        return self.site_intervals.get(norm, self.default_min_interval)

    def get_daily_cap(self, site: str) -> int:
        """Get the daily action cap for a site."""
        norm = self.normalize_site(site)
        return self.daily_caps.get(norm, self.default_daily_cap)

    def can_apply(self, site_or_url: str | None) -> tuple[bool, str | None]:
        """Check if an application is allowed under the daily cap.

        Returns:
            Tuple of (allowed: bool, reason: str | None).
        """
        norm = self.normalize_site(site_or_url)
        cap = self.get_daily_cap(norm)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        with self._lock:
            recorded_date, mem_count = self._daily_counts.get(norm, (today, 0))
            if recorded_date != today:
                mem_count = 0
                self._daily_counts[norm] = (today, 0)

        db_count = self._get_db_daily_count(norm, today)
        total_count = max(mem_count, db_count)

        if total_count >= cap:
            return False, f"daily cap exceeded for {norm}: {total_count}/{cap}"
        return True, None

    def _get_db_daily_count(self, norm_site: str, today: str) -> int:
        """Query DB for today's attempts on this site."""
        try:
            conn = get_connection()
            like = f"%{norm_site}%"
            row = conn.execute("""
                SELECT COUNT(*) FROM jobs
                WHERE (LOWER(site) LIKE ? OR LOWER(url) LIKE ? OR LOWER(application_url) LIKE ?)
                  AND (
                      (applied_at IS NOT NULL AND applied_at LIKE ?)
                      OR (last_attempted_at IS NOT NULL AND last_attempted_at LIKE ?)
                  )
            """, (like, like, like, f"{today}%", f"{today}%")).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def record_action(self, site_or_url: str | None) -> None:
        """Record an action for a site (increments today's counter)."""
        norm = self.normalize_site(site_or_url)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            recorded_date, count = self._daily_counts.get(norm, (today, 0))
            if recorded_date != today:
                count = 0
            self._daily_counts[norm] = (today, count + 1)

    def acquire_pacing(
        self,
        site_or_url: str | None,
        min_interval: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> float:
        """Enforce per-site pacing between consecutive submissions across workers.

        If another worker submitted/applied to this site less than `min_interval`
        seconds ago, blocks until the interval has elapsed.

        Args:
            site_or_url: Site identifier or URL.
            min_interval: Optional override for interval (seconds).
            stop_event: Optional threading.Event for interruptible wait.

        Returns:
            Seconds waited (0.0 if no wait was needed).
        """
        norm = self.normalize_site(site_or_url)
        interval = min_interval if min_interval is not None else self.get_min_interval(norm)
        if interval <= 0:
            with self._lock:
                self._last_action_times[norm] = time.time()
            return 0.0

        wait_needed = 0.0
        with self._lock:
            now = time.time()
            last_time = self._last_action_times.get(norm)
            if last_time is None or (last_time <= now and (now - last_time) >= interval):
                wait_needed = 0.0
                self._last_action_times[norm] = now
            elif last_time <= now:
                wait_needed = interval - (now - last_time)
                self._last_action_times[norm] = now + wait_needed
            else:  # last_time > now (already scheduled into future)
                wait_needed = (last_time - now) + interval
                self._last_action_times[norm] = last_time + interval

        if wait_needed > 0:
            if stop_event is not None:
                stop_event.wait(timeout=wait_needed)
            else:
                time.sleep(wait_needed)

        return wait_needed

    def reset(self) -> None:
        """Reset in-memory pacing and daily counters (useful in tests)."""
        with self._lock:
            self._last_action_times.clear()
            self._daily_counts.clear()


_default_rate_limiter = RateLimiter()


def get_rate_limiter() -> RateLimiter:
    """Get the global default RateLimiter instance."""
    return _default_rate_limiter


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 6, headless: bool = False,
                model: str = "sonnet", dry_run: bool = False,
                engine: str = "claude",
                rate_limiter: RateLimiter | None = None) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name (engine="claude" only).
        dry_run: Don't click Submit.
        engine: "claude" (spawns Claude Code CLI per job, costs API usage) or
            "local" (drives the same Playwright MCP server via whatever LLM
            is configured in .env -- no Claude Code usage).
        rate_limiter: Optional custom RateLimiter instance for pacing / cap guards.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    if rate_limiter is None:
        rate_limiter = get_rate_limiter()

    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0

        # Site safety guards: check daily cap and enforce per-site pacing
        site = job.get("site") or job.get("application_url") or job.get("url")
        allowed, reason = rate_limiter.can_apply(site)
        if not allowed:
            logger.warning("[W%d] %s", worker_id, reason)
            add_event(f"[W{worker_id}] Rate limit: {reason}")
            release_lock(job["url"])
            if _stop_event.wait(timeout=1.0):
                break
            continue

        waited = rate_limiter.acquire_pacing(site, stop_event=_stop_event)
        if _stop_event.is_set():
            release_lock(job["url"])
            break
        if waited > 0:
            logger.info("[W%d] Paced %s for %.1fs", worker_id, site, waited)
            add_event(f"[W{worker_id}] Pacing {site} ({waited:.1f}s)")

        chrome_proc = None
        try:
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless)

            if engine == "local":
                from jobpilot.apply.local_agent import run_job_local
                result, duration_ms = run_job_local(job, port=port, worker_id=worker_id,
                                                     dry_run=dry_run)
            else:
                result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                                model=model, dry_run=dry_run)

            # Record action for daily cap tracking
            rate_limiter.record_action(site)

            if result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif result == "applied" or result == "dry_run:applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms)
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms)
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Parallel runner and main entry point
# ---------------------------------------------------------------------------

def run_jobs(
    workers: int = 1,
    limit: int = 1,
    target_url: str | None = None,
    min_score: int = 6,
    headless: bool = False,
    model: str = "sonnet",
    dry_run: bool = False,
    continuous: bool = False,
    poll_interval: int = 60,
    engine: str = "claude",
    rate_limiter: RateLimiter | None = None,
) -> tuple[int, int]:
    """Launch apply workers concurrently across N threads.

    Args:
        workers: Number of parallel worker threads.
        limit: Max total jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name (engine="claude" only).
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        engine: "claude" or "local".
        rate_limiter: Optional custom RateLimiter instance.

    Returns:
        Tuple of (total_applied, total_failed).
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    if continuous:
        effective_limit = 0
    else:
        effective_limit = limit

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    if workers <= 1:
        return worker_loop(
            worker_id=0,
            limit=effective_limit,
            target_url=target_url,
            min_score=min_score,
            headless=headless,
            model=model,
            dry_run=dry_run,
            engine=engine,
            rate_limiter=rate_limiter,
        )

    # Multi-worker — distribute limit across workers
    if effective_limit:
        base = effective_limit // workers
        extra = effective_limit % workers
        limits = [base + (1 if i < extra else 0) for i in range(workers)]
    else:
        limits = [0] * workers  # continuous mode

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="apply-worker") as executor:
        futures = {
            executor.submit(
                worker_loop,
                worker_id=i,
                limit=limits[i],
                target_url=target_url,
                min_score=min_score,
                headless=headless,
                model=model,
                dry_run=dry_run,
                engine=engine,
                rate_limiter=rate_limiter,
            ): i
            for i in range(workers)
        }

        results: list[tuple[int, int]] = []
        for future in as_completed(futures):
            wid = futures[future]
            try:
                results.append(future.result())
            except Exception:
                logger.exception("Worker %d crashed", wid)
                results.append((0, 0))

    total_applied = sum(r[0] for r in results)
    total_failed = sum(r[1] for r in results)
    return total_applied, total_failed


def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 6, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1, engine: str = "claude",
         rate_limiter: RateLimiter | None = None) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Claude model name (engine="claude" only).
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
        engine: "claude" (default, costs Claude Code API usage per job) or
            "local" (drives the browser via the configured LLM instead).
        rate_limiter: Optional custom RateLimiter instance.
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    console = Console()

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active Claude processes to skip current jobs
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            total_applied, total_failed = run_jobs(
                workers=workers,
                limit=effective_limit,
                target_url=target_url,
                min_score=min_score,
                headless=headless,
                model=model,
                dry_run=dry_run,
                continuous=continuous,
                poll_interval=poll_interval,
                engine=engine,
                rate_limiter=rate_limiter,
            )

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
