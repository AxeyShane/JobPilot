"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from jobpilot.config import RESUME_PATH, load_profile
from jobpilot.database import get_connection, get_jobs_by_stage
from jobpilot.llm import get_score_client

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight technical skills heavily (programming languages, frameworks, tools)
- Consider transferable experience (automation, scripting, API work)
- Factor in the candidate's project experience
- Be realistic about experience level vs. job requirements (years of experience, seniority)

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    reasoning = response

    # Tolerate the ways a model decorates the required format: markdown bold
    # or headers around the label ("**SCORE:** 8", "## SCORE: 8"), bullets,
    # and lowercase. The prompt asks for bare "SCORE: n", but a single stray
    # asterisk used to mean score=0 -- and a 0 is indistinguishable in the DB
    # from a genuine bad match, so a cosmetic mismatch quietly cost a job.
    def _label(line: str, name: str) -> str | None:
        stripped = line.strip().lstrip("#*->\u2022 \t")
        stripped = stripped.replace("**", "").replace("__", "").strip()
        if stripped[:len(name) + 1].upper() == name + ":":
            return stripped[len(name) + 1:].strip()
        return None

    for line in response.split("\n"):
        value = _label(line, "SCORE")
        if value is not None:
            match = re.search(r"\d+", value)
            if match:
                score = max(1, min(10, int(match.group())))
            continue

        value = _label(line, "KEYWORDS")
        if value is not None:
            keywords = value
            continue

        value = _label(line, "REASONING")
        if value is not None:
            reasoning = value

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_score_client()
        response = client.chat(messages, max_tokens=512, temperature=0.2)
        return _parse_score_response(response)
    except Exception as e:
        # "failed" separates "the call did not happen" from "the model judged
        # this a 0". run_scoring() leaves fit_score NULL for these so the row
        # stays in the pending_score queue and is retried. Writing a 0 here is
        # what silently buried 1,882 jobs when the local server went down:
        # pending_score selects on `fit_score IS NULL`, so a 0 is permanent.
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}", "failed": True}


def run_scoring(limit: int = 0, rescore: bool = False, workers: int = 1,
                urls: list[str] | None = None, newest_first: bool = False) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).
        workers: Concurrent LLM calls (thread pool). Local llama.cpp servers
            handle several requests at once via continuous batching (see
            --parallel/n_slots) -- match this to that slot count.
        urls: Score only these job URLs (fast lane: just this poll's finds).
        newest_first: Order by discovery time instead of fit score.

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit,
                                 urls=urls, newest_first=newest_first)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    log.info("Scoring %d jobs (%d concurrent)...", len(jobs), workers)
    t0 = time.time()
    completed = 0
    errors = 0
    unscored = 0   # provider failures; deliberately left NULL for retry
    results: list[dict] = []

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(score_job, resume_text, job): job for job in jobs}
        for future in as_completed(futures):
            job = futures[future]
            result = future.result()
            result["url"] = job["url"]
            completed += 1

            if result.get("failed"):
                # Transport/provider failure: leave the row untouched so it is
                # picked up again next run instead of being written off as a 0.
                errors += 1
                unscored += 1
                log.warning(
                    "[%d/%d] NOT SCORED (left pending): %s",
                    completed, len(jobs), job.get("title", "?")[:60],
                )
                continue

            if result["score"] == 0:
                errors += 1

            results.append(result)

            log.info(
                "[%d/%d] score=%d  %s",
                completed, len(jobs), result["score"], job.get("title", "?")[:60],
            )

            # Commit incrementally -- a killed/crashed run keeps whatever
            # already scored instead of losing the whole batch.
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
                (result["score"], f"{result['keywords']}\n{result['reasoning']}", now, job["url"]),
            )
            conn.commit()

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed,
             len(results) / elapsed if elapsed > 0 else 0)
    if unscored:
        # Loud on purpose. The old line read "Done: 11 scored in 6.2s" while
        # every one of those 11 was a connection error -- three days of that
        # went unnoticed.
        log.error(
            "%d of %d job(s) COULD NOT BE SCORED (provider unreachable) and were "
            "left pending for retry -- check SCORE_LLM_URL / the LLM server",
            unscored, len(jobs),
        )

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "unscored": unscored,
        "elapsed": elapsed,
        "distribution": distribution,
    }
