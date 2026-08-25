# Changelog

All notable changes to JobPilot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added - 2026-08-25 (post-fork polish)
- **First-class OpenRouter support** — `OPENROUTER_API_KEY` is now the
  primary provider (default model `google/gemini-2.5-flash-lite`), wired
  through `llm.py` provider detection, per-stage routing fallback,
  `jobpilot init` wizard (default choice), `jobpilot doctor`, and
  `.env.example`. One key powers score/tailor/cover/apply; Gemini, OpenAI,
  and local llama.cpp/Ollama remain supported.
- **README rewritten** — removed inherited ApplyPilot marketing claims
  (e.g. "applied to 1,000 jobs in 2 days"), repositioned around JobPilot's
  actual differentiators: hard gates, explainable scoring, the outcome loop,
  ATS-safe documents, hostile-posting defense, interview/upskill tools.


### Added - 2026-08-25 (JobPilot fork)
- **Fork of ApplyPilot** into JobPilot: renamed package/CLI/data-home
  (`applypilot` → `jobpilot`, `~/.applypilot` → `~/.jobpilot`), re-licensed to
  **AGPL-3.0** (matching the parent project), PyPI distribution `job-pilot-ai`.
- **CI runs on push/PR** (was manual-trigger only) and actually runs the test
  suite (previously referenced a `tests/` dir that did not exist).
- **`jobpilot gate <text>`** — pre-score hard gates: **eligibility gate**
  (citizenship/PR/security-clearance read verbatim; hard stop, reported, never
  silently dropped) and **language gate** (undeclared required language = FAIL;
  a bar above your declared level = FLAG for your judgment).
- **`jobpilot score-dims`** — explainable, dimensioned fit scoring (5 dims,
  each 0-100 with rubric + rationale), deal-breakers veto outright, honest gaps
  stay visible (never stuffed). New module `jobpilot/scoring/dimensions.py`.
- **`jobpilot outcome`** — closed feedback loop: record real application
  outcomes, promote `drafted`→`applied` on an acknowledgement, `--recalibrate`
  feeds what actually got replies back into scoring. Single-source-of-truth
  outcome vocabulary. New module `jobpilot/outcomes.py`.
- **Document-quality + security** (`jobpilot/quality.py`): ATS text-layer check
  (catches the en-dash date bug), honest keyword-gap reporting, second-pass
  reviewer critique, and untrusted-input sanitization (postings are data, never
  instructions — strips injected instructions, URLs, base64/hex blobs).
- **`jobpilot interview`** — interview prep pack (company brief, likely
  questions, STAR bridge; honest gaps, never invented experience).
- **`jobpilot upskill`** — skill-gap analysis vs postings + a learning plan.
- **74 unit tests** across the new modules (gating, outcomes, quality,
  dimensions, interview, upskill) — all pass.


### Added - 2026-08-23 (later)
- **`jobpilot health`** - loop liveness (pid cross-checked against real log activity,
  so a stale pid file never reads as healthy), per-stage totals and 24h counts, acquirable
  queue depth, and recent prepared matches.
- **`scripts/start_loops.ps1`** - starts both loops detached via `Start-Process -WindowStyle
  Hidden`, so closing the terminal no longer kills them. Idempotent; `-Restart` cycles both.
- **`scripts/reset_stuck.py`** - returns jobs capped at `cover_attempts`/`apply_attempts` to
  the queue. Dry-run by default, reversible record in `backups/`, idempotent.
- **`scripts/ready_count.py`** - acquirable-queue count for the loop log, in its own file
  because PowerShell cannot parse a `<` inside an inline `-c` argument.
- **Fast lane status on the dashboard** - `/api/loop/status` now carries the fast lane, which
  the dashboard previously knew nothing about; it reported "stopped" while the fast lane was
  polling every five minutes.
- **Clickable toasts** - notifications carry the job's `application_url` as launch URI plus an
  "Open job" button, restricted to `http(s)`. They previously used `launch=""`, which renders
  and chimes and does nothing on click.

### Fixed - 2026-08-23 (later)
- **Cover letters dead since 2026-08-12.** Port 8082 refused connections; every failure
  incremented `cover_attempts` until 457 tailored jobs hit the cap of 5 and left the queue
  permanently. This is why only 6 jobs were acquirable despite 722 tailored resumes. Cover is
  now routed to OpenRouter (`get_cover_client` accepts an API key; it passed `""` before, so
  it could only ever reach a local server), transport failures no longer consume a retry, and
  the capped rows were reset. Third occurrence of one root cause -- see also `fit_score = 0`
  (1,882 jobs) and `apply_attempts` (162 jobs).
- **`agent_loop.ps1` was unparseable and could not start.** The "ready to apply" logging line
  added earlier embedded `apply_attempts < ?`; PowerShell treats `<` as a reserved redirection
  operator even inside a quoted `-c` argument. Query moved to `scripts/ready_count.py`.
- **Stage throughput capped at 20.** `run_tailoring` and `run_cover_letters` were hard-coded
  to 20 jobs per cycle, so a 457-job backlog needed 23 full pipeline cycles. Now `STAGE_LIMIT`
  (default 200, `JOBPILOT_STAGE_LIMIT`).
- **XML attribute escaping in toasts** - `saxutils.escape` does not escape quotes, and these
  strings go into `launch=`/`arguments=` attributes; a job URL containing `"` produced
  malformed toast XML.

### Added - 2026-08-23
- **Fast lane (`jobpilot watch`, `scripts/fast_lane.ps1`)** - polls a 2h window every 5 min,
  scores only genuinely-new URLs newest-first, alerts immediately, prepares resume + cover
  letter, and **never submits**. Measured 87s from posting to prepared-and-alerted, against a
  previous discovery-to-submission latency of 1-7 days. Runs alongside `agent_loop.ps1`, which
  keeps working the backlog.
- **Desktop notifications (`jobpilot notify-test`, `src/jobpilot/notify.py`)** - Windows
  toast via WinRT with a PowerShell balloon fallback and a log-only fallback elsewhere. Toasts
  carry the job's `application_url` as the launch URI plus an "Open job" action button. Only
  `http(s)` is accepted as a launch target.
- **`~/.jobpilot/fresh_jobs.jsonl`** - every fast-lane match recorded twice (`stage:"found"`,
  then `stage:"prepped"` with `resume`/`cover_letter` paths), so the worklist survives a missed
  notification.
- **Workday rotation in the fast lane** - 8 employer portals per cycle, full registry coverage
  every ~4 cycles. Aggregator listings mostly cannot be auto-applied to; employer portals can.
- **`SCORE_LLM_URL`/`SCORE_LLM_API_KEY` and `APPLY_LLM_*`** - scoring and auto-apply can now be
  routed to an explicit gateway, falling back to `TAILOR_LLM_API_KEY`. Auto-apply moved off the
  Claude Code CLI engine entirely.
- **`scripts/model_bakeoff.py`** - replays the real tool-schema failure against OpenRouter
  candidates and reports malformed-call and self-recovery counts.
- **`CLAUDE.md`** - operating notes: verified state, failure modes, and traps.
- **`urls=` / `newest_first=` filters** on `get_jobs_by_stage`, scoring, tailoring, cover
  letters, and `only_urls=` on enrichment, so the fast lane touches only what it just found.
- **MAX_TURNS diagnostics** - exhausting the turn budget now dumps tool-usage counts, call
  order, and the last assistant message instead of an empty transcript.

### Fixed - 2026-08-23
- **Auto-apply crashed on every cycle for weeks.** `cli.py` printed `\u2192` to a
  cp1252-redirected stdout, raising `UnicodeEncodeError` before a single job was acquired -
  visible only as an apply cycle that "finished" in 2 seconds. `jobpilot/__init__.py` now
  forces UTF-8 at import; printed strings in `cli.py` are ASCII.
- **Scoring silently wrote off 1,882 jobs.** With the local server on :8080 down since Aug 20,
  `score_job()` returned `score=0` on connection failure and `run_scoring` persisted it; because
  `pending_score` selects on `fit_score IS NULL`, those rows could never be retried. Transport
  failures now leave the row `NULL` and log a loud ERROR. The affected rows were reset
  (reversible record in `backups/score_reset_*.json`).
- **`crawl4ai` 0.9.2 renamed `BM25ContentFilter(threshold=)` to `bm25_threshold=`**, an
  import-time `TypeError` that took down enrichment, smart extract, and the fast lane together.
  The keyword is now resolved from the live signature and filter construction degrades to
  unfiltered markdown rather than raising.
- **`/api/stage-progress` returned 500 on every dashboard poll** - queried a `jobs.failed_at`
  column that does not exist. Rewritten against `apply_status`/`apply_attempts`.
- **One hard job could consume an entire apply run.** `acquire_job` ordered by `fit_score DESC`,
  so a failing job stayed the top row; a `--limit 3` run made three attempts at the same form.
  Now orders by `COALESCE(apply_attempts,0) ASC` first.
- **A manual-ATS row at the top of the queue ended the pass.** `acquire_job` returned `None`,
  which the worker read as an empty queue. It now marks and skips to the next candidate.
- **Orphaned `in_progress` claims are reclaimed** after 45 minutes; previously a killed worker
  stranded rows permanently, as `in_progress` matches neither eligibility branch.
- **Score parsing tolerates markdown** - `**SCORE:** 8`, headers, bullets, lowercase, and
  preamble. A stray asterisk used to mean `score=0`, indistinguishable from a real bad match.

### Changed - 2026-08-23
- **`indeed.com`, `ae.indeed.com`, `linkedin.com` added to `manual_ats`** - 78% of `score>=6`
  jobs resolve to an aggregator's own apply flow, which blocks automation by design. They are
  now retired as `manual` instead of consuming retry attempts.
- **`MAX_TURNS` 40 -> 90** (`APPLY_MAX_TURNS`) and **`_TOOL_RESULT_CHARS` 3000 -> 30000**
  (`APPLY_TOOL_RESULT_CHARS`); the old ceilings were sized for a 9B local model.
- **Repeat-call loop breaker** - three identical tool calls trigger a corrective message. The
  agent preamble now instructs a `browser_snapshot` before retrying a failed call.
- **Bulk discovery narrowed** to `hours_old: 48`, `results_per_site: 50` (was 168h/100).
  Re-trawling a 7-day window every 4 hours is why cycles never survived the watchdog.
- **Enrichment scrapes newest-first** within each site batch, so a run cut short by the watchdog
  still processes the freshest postings.

### Known limitations - 2026-08-23
- **Auto-apply does not complete real ATS forms.** Verified not to be a model limitation:
  `deepseek-v4-flash` and `claude-opus-5` both solve the tool-schema scenario in 6-8 turns with
  zero malformed calls. Real runs make 58-73 *distinct* clicks with genuine form-filling and
  still do not finish inside 90 turns. Multi-step wizards, not agent capability.


### Maintenance — 2026-08-14
- **Job database dedup** — removed **71 duplicate job rows** from the active profile DB
  (`~/.jobpilot/profiles/default/jobpilot.db`; 5411 → 5340 jobs). Duplicates were the
  *same posting* stored under multiple tracking URLs:
  - Endpoint (`app.jobvite.com`) postings repeated up to 19×, differing only by `jk=`/`loc=`
    tracking params (same `j=` requisition).
  - 13 same-ATS pairs (flydubai, Mandarin Oriental, nextventures, Hitachi, naffco, HSBC,
    Yagroup/Dayforce, etc.).
- Dedup rule: same **canonical application_url + title** (tracking params ignored). The kept
  row per group was chosen by *has apply status → highest fit score → earliest activity*.
- Prior to the run the DB+WAL were backed up to `~/.jobpilot/backups/jobpilot-<ts>/`.

## [0.2.0] - 2026-02-17

### Added
- **Parallel workers for discovery/enrichment** - `jobpilot run --workers N` enables
  ThreadPoolExecutor-based parallelism for Workday scraping, smart extract, and detail
  enrichment. Default is sequential (1); power users can scale up.
- **Apply utility modes** - `--gen` (generate prompt for manual debugging), `--mark-applied`,
  `--mark-failed`, `--reset-failed` flags on `jobpilot apply`
- **Dry-run mode** - `jobpilot apply --dry-run` fills forms without clicking Submit
- **5 new tracking columns** - `agent_id`, `last_attempted_at`, `apply_duration_ms`,
  `apply_task_id`, `verification_confidence` for better apply-stage observability
- **Manual ATS detection** - `manual_ats` list in `config/sites.yaml` skips sites with
  unsolvable CAPTCHAs (e.g. TCS iBegin)
- **Qwen3 `/no_think` optimization** - automatically saves tokens when using Qwen models
- **`config.DEFAULTS`** - centralized dict for magic numbers (`min_score`, `max_apply_attempts`,
  `poll_interval`, `apply_timeout`, `viewport`)

### Fixed
- **Config YAML not found after install** - moved `config/` into the package at
  `src/jobpilot/config/` so YAML files (employers, sites, searches) ship with `pip install`
- **Search config format mismatch** - wizard wrote `searches:` key but discovery code
  expected `queries:` with tier support. Aligned wizard output and example config
- **JobSpy install isolation** - removed python-jobspy from package dependencies due to
  broken numpy==1.26.3 exact pin in jobspy metadata. Installed separately with `--no-deps`
- **Scoring batch limit** - default limit of 50 silently left jobs unscored across runs.
  Changed to no limit (scores all pending jobs in one pass)
- **Missing logging output** - added `logging.basicConfig(INFO)` so per-job progress for
  scoring, tailoring, and cover letters is visible during pipeline runs

### Changed
- **Blocked sites externalized** - moved from hardcoded sets in launcher.py to
  `config/sites.yaml` under `blocked:` key
- **Site base URLs externalized** - moved from hardcoded dict in detail.py to
  `config/sites.yaml` under `base_urls:` key
- **SSO domains externalized** - moved from hardcoded list in prompt.py to
  `config/sites.yaml` under `blocked_sso:` key
- **Prompt improvements** - screening context uses `target_role` from profile,
  salary section includes `currency_conversion_note` and dynamic hourly rate examples
- **`acquire_job()` fixed** - writes `agent_id` and `last_attempted_at` to proper columns
  instead of misusing `apply_error`
- **`profile.example.json`** - added `currency_conversion_note` and `target_role` fields

## [0.1.0] - 2026-02-17

### Added
- 6-stage pipeline: discover, enrich, score, tailor, cover letter, apply
- Multi-source job discovery: Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs
- Workday employer portal support (46 preconfigured employers)
- Direct career site scraping (28 preconfigured sites)
- 3-tier job description extraction cascade (JSON-LD, CSS selectors, AI fallback)
- AI-powered job scoring (1-10 fit scale with rationale)
- Resume tailoring with factual preservation (no fabrication)
- Cover letter generation per job
- Autonomous browser-based application submission via Playwright
- Interactive setup wizard (`jobpilot init`)
- Cross-platform Chrome/Chromium detection (Windows, macOS, Linux)
- Multi-provider LLM support (Gemini, OpenAI, local models via OpenAI-compatible endpoints)
- Pipeline stats and HTML results dashboard
- YAML-based configuration for employers, career sites, and search queries
- Job deduplication across sources
- Configurable score threshold filtering
- Safety limits for maximum applications per run
- Detailed application results logging
