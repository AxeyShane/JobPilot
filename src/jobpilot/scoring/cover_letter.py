"""Cover letter generation: LLM-powered, profile-driven, with validation.

Generates concise, engineering-voice cover letters tailored to specific job
postings. All personal data (name, skills, achievements) comes from the user's
profile at runtime. No hardcoded personal information.
"""

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from jobpilot.config import COVER_LETTER_DIR, RESUME_PATH, load_profile
from jobpilot.database import get_connection, get_jobs_by_stage
from jobpilot.llm import get_cover_client
from jobpilot.scoring.validator import (
    BANNED_WORDS,
    LLM_LEAK_PHRASES,
    sanitize_text,
    validate_cover_letter,
)

log = logging.getLogger(__name__)

# Jobs processed per stage per pipeline cycle. The old hard-coded 20 meant a
# 457-job cover-letter backlog needed 23 full cycles to clear, and each cycle
# spends most of its wall clock in discovery before ever reaching this stage.
# Override with JOBPILOT_STAGE_LIMIT.
STAGE_LIMIT = int(os.environ.get("JOBPILOT_STAGE_LIMIT", "200"))


MAX_ATTEMPTS = 5  # max cross-run retries before giving up


# ── Prompt Builder (profile-driven) ──────────────────────────────────────

def _build_cover_letter_prompt(profile: dict) -> str:
    """Build the cover letter system prompt from the user's profile.

    All personal data, skills, and sign-off name come from the profile.
    """
    personal = profile.get("personal", {})
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Preferred name for the sign-off (falls back to full name)
    sign_off_name = personal.get("preferred_name") or personal.get("full_name", "")

    # Flatten all allowed skills
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "the tools listed in the resume"

    # Real metrics from resume_facts
    real_metrics = resume_facts.get("real_metrics", [])
    preserved_projects = resume_facts.get("preserved_projects", [])

    # Build achievement examples for the prompt
    projects_hint = ""
    if preserved_projects:
        projects_hint = f"\nKnown projects to reference: {', '.join(preserved_projects)}"

    metrics_hint = ""
    if real_metrics:
        metrics_hint = f"\nReal metrics to use: {', '.join(real_metrics)}"

    # Build the full banned list from the validator so the prompt stays in sync
    # with what will actually be rejected — the validator checks all of these.
    all_banned = ", ".join(f'"{w}"' for w in BANNED_WORDS)
    leak_banned = ", ".join(f'"{p}"' for p in LLM_LEAK_PHRASES)

    # "Use numbers" is only safe to instruct when real numbers actually exist.
    # Telling the model to use numbers AND separately telling it not to invent
    # any, with no real ones on hand, is a direct instruction conflict --
    # smaller/local models resolve it by inventing a number anyway (observed
    # in production: fabricated percentages/counts survived every retry).
    # When there's nothing real to cite, drop the number-pressure entirely
    # instead of fighting it with a second, competing instruction.
    para2_directive = (
        "Use numbers." if real_metrics
        else "Use concrete specifics (what shipped, what broke, what got solved) -- no numbers, none exist for this candidate."
    )
    voice_number_line = (
        "- Every sentence should contain either a number, a tool name, or a specific outcome. If it doesn't, cut it."
        if real_metrics else
        "- Every sentence should contain either a tool name or a specific outcome. If it doesn't, cut it."
    )

    return f"""Write a cover letter for {sign_off_name}. The goal is to get an interview.

STRUCTURE: 3 short paragraphs. Under 250 words. Every sentence must earn its place.

PARAGRAPH 1 (2-3 sentences): Open with a specific thing YOU built that solves THEIR problem. Not "I'm excited about this role." Not "This role aligns with my experience." Start with the work.

PARAGRAPH 2 (3-4 sentences): Pick 2 achievements from the resume that are MOST relevant to THIS job. {para2_directive} Frame as solving their problem, not listing your accomplishments.{projects_hint}{metrics_hint}

PARAGRAPH 3 (1-2 sentences): One specific thing about the company from the job description (a product, a technical challenge, a team structure). Then close. "Happy to walk through any of this in more detail." or "Let's discuss." Nothing else.

BANNED WORDS AND PHRASES (automated validator rejects ANY of these — do not use even once):
{all_banned}

ALSO BANNED (meta-commentary the validator catches):
{leak_banned}

BANNED PUNCTUATION: No em dashes (—) or en dashes (–). Use commas or periods.

VOICE:
- Write like a real engineer emailing someone they respect. Not formal, not casual. Just direct.
- NEVER narrate or explain what you're doing. BAD: "This demonstrates my commitment to X." GOOD: Just state the fact and move on.
- NEVER hedge. BAD: "might address some of your challenges." GOOD: "solves the same problem your team is facing."
{voice_number_line}
- Read it out loud. If it sounds like a robot wrote it, rewrite it.

FABRICATION = INSTANT REJECTION:
The candidate's real tools are ONLY: {skills_str}.
Do NOT mention ANY tool not in this list. If the job asks for tools not listed, talk about the work you did, not the tools.
Do NOT invent metrics, percentages, or numbers.{" The ONLY real metrics you may use are listed above under 'Real metrics to use' -- do not use any number not in that list." if real_metrics else " No real metrics were provided for this candidate -- write with zero numbers rather than inventing one. A specific outcome ('shipped the capsule filling machine', 'led the robotic arm redesign') satisfies the voice rule just as well as a number."}

Sign off: just "{sign_off_name}"

Output ONLY the letter text. No subject lines. No "Here is the cover letter:" preamble. No notes after the sign-off.
Start DIRECTLY with "Dear Hiring Manager," and end with the name."""


# ── Helpers ──────────────────────────────────────────────────────────────

def _strip_preamble(text: str) -> str:
    """Remove LLM preamble before 'Dear Hiring Manager,' if present.

    Gemini and other models sometimes output "Here is the cover letter:" or
    similar meta-commentary before the actual letter text. Strip everything
    before the first occurrence of "Dear" so the validator's start-check passes.
    """
    dear_idx = text.lower().find("dear")
    if dear_idx > 0:
        return text[dear_idx:]
    return text


def _strip_fabricated_sentences(text: str, real_metrics: list[str], known_terms: list[str] | None = None) -> str:
    """Last-resort cleanup: drop any sentence containing a quantified claim
    not backed by real_metrics or known_terms, rather than ship a letter
    known to fabricate.

    Mirrors validator.validate_cover_letter's fabrication regex. Used only
    after every generation retry still failed validation -- the prompt/retry
    fix is the primary defense, this is the backstop.
    """
    allowed = list(real_metrics or []) + list(known_terms or [])

    def _is_fabricated(sentence: str) -> bool:
        pcts = re.findall(r"\d+(?:\.\d+)?\s?%", sentence)
        counts = [
            n for n in re.findall(r"\b\d{2,}(?:,\d{3})*\b", sentence)
            if not (len(n) == 4 and 1950 <= int(n) <= 2039)
            and int(n.replace(",", "")) >= 10
        ]
        for n in pcts + counts:
            if not any(n.replace(" ", "") in m.replace(" ", "") for m in allowed):
                return True
        return False

    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = [s for s in sentences if not _is_fabricated(s)]
    return " ".join(kept).strip()


# ── Core Generation ──────────────────────────────────────────────────────

def generate_cover_letter(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
) -> str:
    """Generate a cover letter with fresh context on each retry + auto-sanitize.

    Same design as tailor_resume: fresh conversation per attempt, issues noted
    in the prompt, no conversation history stacking.

    Args:
        resume_text:      The candidate's resume text (base or tailored).
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".

    Returns:
        The cover letter text (best attempt even if validation failed).
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    avoid_notes: list[str] = []
    letter = ""
    client = get_cover_client()
    cl_prompt_base = _build_cover_letter_prompt(profile)
    real_metrics = profile.get("resume_facts", {}).get("real_metrics", [])
    # Tool/skill names can contain a number (e.g. "Fusion 360") -- exclude
    # those from the fabrication check so a real tool isn't flagged as an
    # invented metric.
    known_terms: list[str] = []
    for items in profile.get("skills_boundary", {}).values():
        if isinstance(items, list):
            known_terms.extend(items)

    for attempt in range(max_retries + 1):
        # Fresh conversation every attempt
        prompt = cl_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES:\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (
                f"RESUME:\n{resume_text}\n\n---\n\n"
                f"TARGET JOB:\n{job_text}\n\n"
                "Write the cover letter:"
            )},
        ]

        letter = client.chat(messages, max_tokens=1024, temperature=0.7)
        letter = sanitize_text(letter)  # auto-fix em dashes, smart quotes
        letter = _strip_preamble(letter)  # remove any "Here is the letter:" prefix

        validation = validate_cover_letter(letter, mode=validation_mode, real_metrics=real_metrics, known_terms=known_terms)
        if validation["passed"]:
            return letter

        avoid_notes.extend(validation["errors"])
        # Warnings never block — only hard errors trigger a retry
        log.debug(
            "Cover letter attempt %d/%d failed: %s",
            attempt + 1, max_retries + 1, validation["errors"],
        )

    # All retries exhausted and still failing validation. If the only
    # remaining problem is fabricated numbers, strip the offending sentence(s)
    # rather than ship known-fabricated content -- returning "the last
    # attempt even if failed" previously meant a bad letter could go out
    # unfiltered (observed in production: a 25% claim survived every retry).
    final_check = validate_cover_letter(letter, mode=validation_mode, real_metrics=real_metrics, known_terms=known_terms)
    if not final_check["passed"]:
        letter = _strip_fabricated_sentences(letter, real_metrics, known_terms)
    return letter


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_cover_letters(min_score: int = 6, limit: int = 0,
                      validation_mode: str = "normal", workers: int = 1,
                      urls: list[str] | None = None, newest_first: bool = False) -> dict:
    """Generate cover letters for high-scoring jobs that have tailored resumes.

    Args:
        min_score:       Minimum fit_score threshold.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", or "lenient".
        workers:         Concurrent LLM calls (thread pool).
        urls:            Generate only for these job URLs (fast lane).
        newest_first:    Order by discovery time instead of fit score.

    Returns:
        {"generated": int, "errors": int, "elapsed": float}
    """
    # 0 means 'use the configured batch size' (see STAGE_LIMIT).
    if limit == 0:
        limit = STAGE_LIMIT
    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    # Fetch jobs that have tailored resumes but no cover letter yet
    where = (
        "fit_score >= ? AND tailored_resume_path IS NOT NULL "
        "AND full_description IS NOT NULL "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
        "AND COALESCE(cover_attempts, 0) < ? "
        "AND (scam_verdict IS NULL OR scam_verdict != 'blocked')"
    )
    params: list = [min_score, MAX_ATTEMPTS]
    if urls:
        placeholders = ",".join("?" for _ in urls)
        where += f" AND url IN ({placeholders})"
        params.extend(urls)
    order = "discovered_at DESC" if newest_first else "fit_score DESC"
    params.append(limit)
    jobs = conn.execute(
        f"SELECT * FROM jobs WHERE {where} ORDER BY {order} LIMIT ?", params
    ).fetchall()

    if not jobs:
        log.info("No jobs needing cover letters (score >= %d).", min_score)
        return {"generated": 0, "errors": 0, "elapsed": 0.0}

    # Convert rows to dicts
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    def _cover_one(job: dict) -> dict:
        try:
            letter = generate_cover_letter(resume_text, job, profile,
                                          validation_mode=validation_mode)

            # Build safe filename prefix
            safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
            safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
            prefix = f"{safe_site}_{safe_title}"

            cl_path = COVER_LETTER_DIR / f"{prefix}_CL.txt"
            cl_path.write_text(letter, encoding="utf-8")

            # Generate PDF (human preview) and DOCX (what actually gets
            # uploaded during apply, see apply/prompt.py) -- best-effort.
            pdf_path = None
            try:
                from jobpilot.scoring.pdf import convert_to_pdf, convert_cover_letter_to_docx
                pdf_path = str(convert_to_pdf(cl_path))
                convert_cover_letter_to_docx(cl_path)
            except Exception:
                log.debug("PDF/DOCX generation failed for %s", cl_path, exc_info=True)

            return {
                "url": job["url"],
                "path": str(cl_path),
                "pdf_path": pdf_path,
                "title": job["title"],
                "site": job["site"],
            }
        except Exception as e:
            log.error("[ERROR] %s -- %s", job["title"][:40], e)
            # A provider that is unreachable is not a failed attempt at this
            # job -- it is no attempt at all. Counting it burns the retry
            # budget: a dead local server on :8082 pushed 457 jobs to
            # cover_attempts=5 and permanently out of the queue, the same way
            # a dead :8080 buried 1,882 jobs at fit_score=0.
            transport = isinstance(e, (ConnectionError, OSError, TimeoutError)) or \
                any(t in str(e) for t in ("10061", "Connection refused",
                                          "actively refused", "Connect call failed",
                                          "Max retries", "timed out"))
            return {
                "url": job["url"], "title": job["title"], "site": job["site"],
                "path": None, "pdf_path": None, "error": str(e),
                "transport_error": transport,
            }

    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    log.info(
        "Generating cover letters for %d jobs (score >= %d, %d concurrent)...",
        len(jobs), min_score, workers,
    )
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    error_count = 0

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_cover_one, job): job for job in jobs}
        for future in as_completed(futures):
            result = future.result()
            completed += 1
            results.append(result)
            if result.get("error"):
                error_count += 1

            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            status = "ERROR" if result.get("error") else "OK"
            log.info(
                "%d/%d [%s] | %.1f jobs/min | %s",
                completed, len(jobs), status, rate * 60, result["title"][:40],
            )

            # Persist incrementally -- survives a mid-run kill/crash.
            now = datetime.now(timezone.utc).isoformat()
            if result.get("path"):
                conn.execute(
                    "UPDATE jobs SET cover_letter_path=?, cover_letter_at=?, "
                    "cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                    (result["path"], now, result["url"]),
                )
            elif result.get("transport_error"):
                # Leave cover_attempts untouched so the job stays retryable
                # once the provider comes back.
                log.warning("Provider unreachable, not counting an attempt: %s",
                            (result.get("title") or "?")[:50])
            else:
                conn.execute(
                    "UPDATE jobs SET cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                    (result["url"],),
                )
            conn.commit()

    saved = sum(1 for r in results if r.get("path"))

    elapsed = time.time() - t0
    log.info("Cover letters done in %.1fs: %d generated, %d errors", elapsed, saved, error_count)

    return {
        "generated": saved,
        "errors": error_count,
        "elapsed": elapsed,
    }
