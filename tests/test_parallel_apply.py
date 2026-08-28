"""Tests for parallel apply execution, per-site rate limiting, concurrency safety,
dry-run enforcement, turn budget / repeat breaker, and atomic job claims.

Run:  cd <repo> && python tests/test_parallel_apply.py
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from jobpilot.apply.launcher import RateLimiter, acquire_job, get_rate_limiter, run_jobs, worker_loop
from jobpilot.apply.local_agent import _looks_like_submit, _run_agent_turns, run_job_local
from jobpilot.apply.dashboard import init_worker

RESULT = []


def check(name: str, cond: bool) -> None:
    RESULT.append(bool(cond))
    print("[PASS] " + name if cond else "[FAIL] " + name)
    if not cond:
        raise SystemExit("TEST FAILED: " + name)


# ---------------------------------------------------------------------------
# 1. RateLimiter: Per-site pacing & Daily Action Caps
# ---------------------------------------------------------------------------

limiter = RateLimiter(default_min_interval=0.2, daily_caps={"linkedin": 2})

# First action on linkedin: immediate (no wait)
w1 = limiter.acquire_pacing("linkedin", min_interval=0.2)
check("RateLimiter: first call for site requires 0 wait", w1 == 0.0)

# Second immediate action on linkedin: delayed by interval
t0 = time.time()
w2 = limiter.acquire_pacing("https://www.linkedin.com/jobs/view/123", min_interval=0.2)
elapsed = time.time() - t0
check("RateLimiter: consecutive call to same site is delayed", w2 > 0.15 and elapsed >= 0.15)

# Action on different site: immediate (no wait)
w3 = limiter.acquire_pacing("https://boards.greenhouse.io/job/456", min_interval=0.2)
check("RateLimiter: call to different site proceeds with 0 wait", w3 == 0.0)

# Daily cap check
limiter.reset()
allowed1, _ = limiter.can_apply("linkedin")
check("RateLimiter: daily cap initially allows apply", allowed1)

limiter.record_action("linkedin")
limiter.record_action("https://www.linkedin.com/jobs/view/999")
allowed2, reason = limiter.can_apply("linkedin")
check("RateLimiter: daily cap blocks after limit reached", not allowed2 and "daily cap" in str(reason))

allowed_gh, _ = limiter.can_apply("greenhouse")
check("RateLimiter: other site unaffected by linkedin daily cap", allowed_gh)


# ---------------------------------------------------------------------------
# 2. Worker Concurrency: workers=N spawns N concurrent fake-driver calls
# ---------------------------------------------------------------------------

def test_concurrent_worker_spawning():
    N = 4
    barrier = threading.Barrier(N)
    active_workers = 0
    max_active = 0
    lock = threading.Lock()
    worker_ids_seen = set()

    def fake_driver(job, port, worker_id=0, **kwargs):
        nonlocal active_workers, max_active
        with lock:
            active_workers += 1
            if active_workers > max_active:
                max_active = active_workers
            worker_ids_seen.add(worker_id)
        # Wait until all N workers are concurrently in the driver
        barrier.wait(timeout=5.0)
        time.sleep(0.05)
        with lock:
            active_workers -= 1
        return "applied", 100

    jobs = [
        {"url": f"https://example.com/job{i}", "title": f"Engineer {i}", "site": f"site_{i}", "fit_score": 8}
        for i in range(N)
    ]
    job_idx = 0
    job_lock = threading.Lock()

    def fake_acquire_job(target_url=None, min_score=6, worker_id=0):
        nonlocal job_idx
        with job_lock:
            if job_idx < len(jobs):
                j = jobs[job_idx]
                job_idx += 1
                return j
            return None

    with patch("jobpilot.apply.launcher.acquire_job", side_effect=fake_acquire_job),          patch("jobpilot.apply.launcher.launch_chrome", return_value=MagicMock()),          patch("jobpilot.apply.launcher.cleanup_worker"),          patch("jobpilot.apply.launcher.mark_result"),          patch("jobpilot.apply.local_agent.run_job_local", side_effect=fake_driver),          patch("jobpilot.apply.launcher.run_job", side_effect=fake_driver):

        applied, failed = run_jobs(workers=N, limit=N, engine="local")

    check(f"Concurrency: spawned {N} workers concurrently (max active={max_active})", max_active == N)
    check(f"Concurrency: distinct worker IDs seen {worker_ids_seen}", len(worker_ids_seen) == N)
    check("Concurrency: all N jobs applied", applied == N and failed == 0)

test_concurrent_worker_spawning()


# ---------------------------------------------------------------------------
# 3. Dry-Run Enforcement: dry_run passes through and no submit happens
# ---------------------------------------------------------------------------

class _FakeMCPResult:
    def __init__(self, content=None, is_error=False):
        self.content = content or []
        self.is_error = is_error


class _FakeSession:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if _looks_like_submit(name, args):
            raise AssertionError(f"Submit action {name}({args}) reached call_tool in dry-run mode!")
        return _FakeMCPResult()


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


async def test_dry_run_interception():
    init_worker(88)
    session = _FakeSession()
    messages = [{"role": "user", "content": "apply to this job"}]

    responses = [
        # Model tries to navigate (allowed)
        _FakeResponse({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "c1",
                        "function": {"name": "browser_navigate", "arguments": '{"url": "https://example.com"}'},
                    }],
                }
            }]
        }),
        # Model tries to click Submit (must be intercepted by dry_run)
        _FakeResponse({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "c2",
                        "function": {"name": "browser_click", "arguments": '{"element": "Submit Application"}'},
                    }],
                }
            }]
        }),
        # Model acknowledges and finishes
        _FakeResponse({
            "choices": [{"message": {"role": "assistant", "content": "Form ready.\nRESULT:APPLIED"}}]
        }),
    ]

    async def fake_post(*args, **kwargs):
        return responses.pop(0)

    with patch("httpx.AsyncClient.post", new=fake_post):
        status, transcript = await _run_agent_turns(
            session, openai_tools=[], messages=messages, worker_id=88,
            base_url="http://fake", model="fake-model", headers={},
            dry_run=True,
        )

    check("Dry-run: status is applied", status == "applied")
    check("Dry-run: submit click was never sent to MCP session",
          all(not _looks_like_submit(name, args) for name, args in session.calls))
    check("Dry-run: navigation tool call was allowed through",
          any(name == "browser_navigate" for name, args in session.calls))

asyncio.run(test_dry_run_interception())


# ---------------------------------------------------------------------------
# 4. Turn budget & Repeat-breaker: Caps runaway loops cleanly
# ---------------------------------------------------------------------------

async def test_repeat_breaker():
    init_worker(77)
    session = _FakeSession()
    messages = [{"role": "user", "content": "fill form"}]

    # Return identical failing tool call 4 times in a row, then finish
    call_payload = _FakeResponse({
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "rep_call",
                    "function": {"name": "browser_click", "arguments": '{"element": "stuck_button"}'},
                }],
            }
        }]
    })
    final_payload = _FakeResponse({
        "choices": [{"message": {"role": "assistant", "content": "Stuck.\nRESULT:FAILED:element_not_found"}}]
    })

    resp_list = [call_payload, call_payload, call_payload, final_payload]

    async def fake_post(*args, **kwargs):
        return resp_list.pop(0)

    with patch("httpx.AsyncClient.post", new=fake_post):
        status, transcript = await _run_agent_turns(
            session, openai_tools=[], messages=messages, worker_id=77,
            base_url="http://fake", model="fake-model", headers={},
            dry_run=False,
        )

    # Verify repeat warning was injected into conversation
    has_repeat_warning = any(
        isinstance(m, dict) and m.get("role") == "user" and "Stop retrying it" in str(m.get("content", ""))
        for m in messages
    )
    check("Repeat-breaker: injected loop warning into conversation", has_repeat_warning)
    check("Repeat-breaker: cleanly concluded with failure reason", status.startswith("failed:"))

asyncio.run(test_repeat_breaker())


async def test_turn_budget_exhaustion():
    init_worker(66)
    session = _FakeSession()
    messages = [{"role": "user", "content": "apply"}]

    # Model sends text with no RESULT line repeatedly
    no_result_resp = _FakeResponse({
        "choices": [{"message": {"role": "assistant", "content": "Thinking about next step..."}}]
    })

    async def fake_post(*args, **kwargs):
        return no_result_resp

    with patch("jobpilot.apply.local_agent.MAX_TURNS", 3),          patch("httpx.AsyncClient.post", new=fake_post):
        status, summary = await _run_agent_turns(
            session, openai_tools=[], messages=messages, worker_id=66,
            base_url="http://fake", model="fake-model", headers={},
            dry_run=False,
        )

    check("Turn budget: capped runaway turns at MAX_TURNS", status == "failed:local_agent_max_turns")
    check("Turn budget: summary generated", "MAX_TURNS" in summary)

asyncio.run(test_turn_budget_exhaustion())


# ---------------------------------------------------------------------------
# 5. acquire_job Concurrency: N threads claiming jobs get N distinct URLs
# ---------------------------------------------------------------------------

def test_atomic_acquire_job_concurrency():
    # Create an isolated temporary SQLite database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_db = f.name

    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    from jobpilot.database import init_db
    init_db(tmp_db)

    num_jobs = 12
    for i in range(num_jobs):
        conn.execute("""
            INSERT INTO jobs (url, title, site, tailored_resume_path, fit_score, scam_verdict)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (f"https://example.com/job_{i}", f"Job {i}", f"site_{i % 3}", f"/tmp/resume_{i}.txt", 8, "clear"))
    conn.commit()
    conn.close()

    claimed_urls = []
    claimed_lock = threading.Lock()
    threads = []
    num_threads = 6

    def worker_claim(tid: int):
        # Point get_connection to tmp_db for this thread
        with patch("jobpilot.database.DB_PATH", tmp_db):
            for _ in range(2):  # Each thread claims 2 jobs
                job = acquire_job(min_score=6, worker_id=tid)
                if job:
                    with claimed_lock:
                        claimed_urls.append((tid, job["url"]))
                time.sleep(0.01)

    from jobpilot.database import get_connection as get_db_conn
    with patch("jobpilot.apply.launcher.get_connection", lambda: get_db_conn(tmp_db)):
        for t in range(num_threads):
            th = threading.Thread(target=worker_claim, args=(t,))
            threads.append(th)
            th.start()

        for th in threads:
            th.join()

    # Verify results
    urls_only = [u for _, u in claimed_urls]
    check("Atomic claim: claimed all available jobs", len(urls_only) == num_jobs)
    check("Atomic claim: NO duplicate claims across concurrent threads", len(set(urls_only)) == num_jobs)

    # Verify DB state
    conn = sqlite3.connect(tmp_db)
    rows = conn.execute("SELECT url, apply_status, agent_id FROM jobs").fetchall()
    conn.close()
    try:
        os.remove(tmp_db)
    except Exception:
        pass

    check("Atomic claim: all rows marked in_progress", all(r[1] == "in_progress" for r in rows))
    check("Atomic claim: agent_id assigned to each row", all(r[2] is not None and r[2].startswith("worker-") for r in rows))


test_atomic_acquire_job_concurrency()


# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    passed = sum(1 for x in RESULT if x)
    print(f"\n{passed}/{len(RESULT)} checks passed")
    raise SystemExit(0 if passed == len(RESULT) else 1)
