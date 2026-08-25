<!-- logo here -->

> **⚠️ JobPilot** is an open-source evolution of the [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) project, created by [Pickle-Pixel](https://github.com/Pickle-Pixel). JobPilot is **not affiliated** with applypilot.app, useapplypilot.com, jobpilot.app, usejobpilot.com, or any other product using the "Pilot" name. Those sites are not associated with this project and may misrepresent what they offer.

# JobPilot

**Applied to 1,000 jobs in 2 days. Fully autonomous. Open source.**

[![PyPI version](https://img.shields.io/pypi/v/jobpilot?color=blue)](https://pypi.org/project/job-pilot-ai/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Pickle-Pixel/JobPilot?style=social)](https://github.com/Pickle-Pixel/JobPilot)
[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/S6S01UL5IO)




https://github.com/user-attachments/assets/7ee3417f-43d4-4245-9952-35df1e77f2df


---

## What It Does

JobPilot is a 6-stage autonomous job application pipeline. It discovers jobs across 5+ boards, scores them against your resume with AI, tailors your resume per job, writes cover letters, and **submits applications for you**. It navigates forms, uploads documents, answers screening questions, all hands-free.

Three commands. That's it.

```bash
pip install job-pilot-ai   # installs the `jobpilot` CLI
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
jobpilot init          # one-time setup: resume, profile, preferences, API keys
jobpilot doctor        # verify your setup — shows what's installed and what's missing
jobpilot run           # discover > enrich > score > tailor > cover letters
jobpilot run -w 4      # same but parallel (4 threads for discovery/enrichment)
jobpilot apply         # autonomous browser-driven submission
jobpilot apply -w 3    # parallel apply (3 Chrome instances)
jobpilot apply --dry-run  # fill forms without submitting
```

> **Why two install commands?** `python-jobspy` pins an exact numpy version in its metadata that conflicts with pip's resolver, but works fine at runtime with any modern numpy. The `--no-deps` flag bypasses the resolver; the second command installs jobspy's actual runtime dependencies. Everything except `python-jobspy` installs normally.

---

## Two Paths

### Full Pipeline (recommended)
**Requires:** Python 3.11+, Node.js (for npx), Gemini API key (free), Claude Code CLI, Chrome

Runs all 6 stages, from job discovery to autonomous application submission. This is the full power of JobPilot.

### Discovery + Tailoring Only
**Requires:** Python 3.11+, Gemini API key (free)

Runs stages 1-5: discovers jobs, scores them, tailors your resume, generates cover letters. You submit applications manually with the AI-prepared materials.

---

## The Pipeline

| Stage | What Happens |
|-------|-------------|
| **1. Discover** | Scrapes 5 job boards (Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs) + 48 Workday employer portals + 30 direct career sites |
| **2. Enrich** | Fetches full job descriptions via JSON-LD, CSS selectors, or AI-powered extraction |
| **3. Score** | AI rates every job 1-10 based on your resume and preferences. Only high-fit jobs proceed |
| **4. Tailor** | AI rewrites your resume per job: reorganizes, emphasizes relevant experience, adds keywords. Never fabricates |
| **5. Cover Letter** | AI generates a targeted cover letter per job |
| **6. Auto-Apply** | Claude Code navigates application forms, fills fields, uploads documents, answers questions, and submits |

### Fast lane (runs alongside, not instead)

`jobpilot watch` polls a **2-hour** window every 5 minutes, scores only genuinely-new
postings, alerts you on the desktop, prepares a tailored resume and cover letter, and then
**stops without submitting**. Measured 87 seconds from posting to prepared-and-alerted.

The full pipeline above works the backlog on a 4-hour cycle; the fast lane exists so nothing
posted this morning has to wait behind it.

```bash
jobpilot notify-test                  # confirm desktop toasts actually appear
jobpilot watch --once                 # one poll, then exit
jobpilot watch --min-score 6          # alert threshold (default 7)
powershell -File scripts/fast_lane.ps1  # run it continuously
```

Every match is also appended to `~/.jobpilot/fresh_jobs.jsonl` with the paths to the
prepared documents, so the worklist survives a missed notification.

Each stage is independent. Run them all or pick what you need.

---

## JobPilot vs The Alternatives

| Feature | JobPilot | AIHawk | Manual |
|---------|-----------|--------|--------|
| Job discovery | 5 boards + Workday + direct sites | LinkedIn only | One board at a time |
| AI scoring | 1-10 fit score per job | Basic filtering | Your gut feeling |
| Resume tailoring | Per-job AI rewrite | Template-based | Hours per application |
| Auto-apply | Full form navigation + submission | LinkedIn Easy Apply only | Click, type, repeat |
| Supported sites | Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs, 46 Workday portals, 28 direct sites | LinkedIn | Whatever you open |
| License | AGPL-3.0 | MIT | N/A |

---

## Requirements

| Component | Required For | Details |
|-----------|-------------|---------|
| Python 3.11+ | Everything | Core runtime |
| Node.js 18+ | Auto-apply | Needed for `npx` to run Playwright MCP server |
| Gemini API key | Scoring, tailoring, cover letters | Free tier (15 RPM / 1M tokens/day) is enough |
| Chrome/Chromium | Auto-apply | Auto-detected on most systems |
| Claude Code CLI | Auto-apply | Install from [claude.ai/code](https://claude.ai/code) |

**Gemini API key is free.** Get one at [aistudio.google.com](https://aistudio.google.com). OpenAI and local models (Ollama/llama.cpp) are also supported.

### Optional

| Component | What It Does |
|-----------|-------------|
| CapSolver API key | Solves CAPTCHAs during auto-apply (hCaptcha, reCAPTCHA, Turnstile, FunCaptcha). Without it, CAPTCHA-blocked applications just fail gracefully |

> **Note:** python-jobspy is installed separately with `--no-deps` because it pins an exact numpy version in its metadata that conflicts with pip's resolver. It works fine with modern numpy at runtime.

---

## Configuration

All generated by `jobpilot init`:

### `profile.json`
Your personal data in one structured file: contact info, work authorization, compensation, experience, skills, resume facts (preserved during tailoring), and EEO defaults. Powers scoring, tailoring, and form auto-fill.

### `searches.yaml`
Job search queries, target titles, locations, boards. Run multiple searches with different parameters.

### `.env`
API keys and runtime config: `GEMINI_API_KEY`, `LLM_MODEL`, `CAPSOLVER_API_KEY` (optional).

**Per-stage LLM routing.** Each stage can point at its own endpoint; all fall back to
`LLM_URL`/`LLM_MODEL` when unset. `SCORE_*` and `APPLY_*` inherit `TAILOR_LLM_API_KEY` if they
have no key of their own, so one gateway credential covers all three.

| Stage | Variables |
|---|---|
| score | `SCORE_LLM_URL`, `SCORE_LLM_MODEL`, `SCORE_LLM_API_KEY` |
| tailor | `TAILOR_LLM_URL`, `TAILOR_LLM_MODEL`, `TAILOR_LLM_API_KEY` |
| enrich | `ENRICH_LLM_URL`, `ENRICH_LLM_MODEL` |
| cover | `COVER_LLM_URL`, `COVER_LLM_MODEL` |
| apply | `APPLY_LLM_URL`, `APPLY_LLM_MODEL`, `APPLY_LLM_API_KEY` |

Scoring is high-volume and low-complexity; tailoring is low-volume and quality-critical; apply
is a long multi-turn tool-calling loop. Routing them separately is usually cheaper *and* better
than one model for everything. `scripts/model_bakeoff.py` compares candidates on the apply
stage's actual tool-calling demands.

Also honoured: `APPLY_MAX_TURNS` (default 90), `APPLY_TOOL_RESULT_CHARS` (default 30000),
`JOBPILOT_NOTIFY=0` to silence desktop notifications.

### Package configs (shipped with JobPilot)
- `config/employers.yaml` - Workday employer registry (48 preconfigured)
- `config/sites.yaml` - Direct career sites (30+), blocked sites, base URLs, manual ATS domains
- `config/searches.example.yaml` - Example search configuration

---

## How Stages Work

### Discover
Queries Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs via JobSpy. Scrapes 48 Workday employer portals (configurable in `employers.yaml`). Hits 30 direct career sites with custom extractors. Deduplicates by URL.

### Enrich
Visits each job URL and extracts the full description. 3-tier cascade: JSON-LD structured data, then CSS selector patterns, then AI-powered extraction for unknown layouts.

### Score
AI scores every job 1-10 against your profile. 9-10 = strong match, 7-8 = good, 5-6 = moderate, 1-4 = skip. Only jobs above your threshold proceed to tailoring.

### Tailor
Generates a custom resume per job: reorders experience, emphasizes relevant skills, incorporates keywords from the job description. Your `resume_facts` (companies, projects, metrics) are preserved exactly. The AI reorganizes but never fabricates.

### Cover Letter
Writes a targeted cover letter per job referencing the specific company, role, and how your experience maps to their requirements.

### Auto-Apply
Claude Code launches a Chrome instance, navigates to each application page, detects the form type, fills personal information and work history, uploads the tailored resume and cover letter, answers screening questions with AI, and submits. A live dashboard shows progress in real-time.

The Playwright MCP server is configured automatically at runtime per worker. No manual MCP setup needed.

```bash
# Utility modes (no Chrome/Claude needed)
jobpilot apply --mark-applied URL    # manually mark a job as applied
jobpilot apply --mark-failed URL     # manually mark a job as failed
jobpilot apply --reset-failed        # reset all failed jobs for retry
jobpilot apply --gen --url URL       # generate prompt file for manual debugging
```

---

## CLI Reference

```
jobpilot init                         # First-time setup wizard
jobpilot doctor                       # Verify setup, diagnose missing requirements
jobpilot run [stages...]              # Run pipeline stages (or 'all')
jobpilot run --workers 4              # Parallel discovery/enrichment
jobpilot run --stream                 # Concurrent stages (streaming mode)
jobpilot run --min-score 8            # Override score threshold
jobpilot run --dry-run                # Preview without executing
jobpilot run --validation lenient     # Relax validation (recommended for Gemini free tier)
jobpilot run --validation strict      # Strictest validation (retries on any banned word)
jobpilot apply                        # Launch auto-apply
jobpilot apply --workers 3            # Parallel browser workers
jobpilot apply --dry-run              # Fill forms without submitting
jobpilot apply --continuous           # Run forever, polling for new jobs
jobpilot apply --headless             # Headless browser mode
jobpilot apply --url URL              # Apply to a specific job
jobpilot watch                        # Fast lane: poll, alert, prep, hold (never submits)
jobpilot watch --once                 # Single poll, then exit
jobpilot watch --min-score 6          # Alert threshold (default 7)
jobpilot watch --hours-old 2          # Discovery window (default 2h)
jobpilot watch --no-prep              # Alert only, skip resume/cover-letter prep
jobpilot notify-test                  # Send a test desktop notification
jobpilot status                       # Pipeline statistics
jobpilot dashboard                    # Open HTML results dashboard
```

### Auto-apply: current reality

Auto-apply **does not reliably complete real ATS forms**, and this is not a model limitation -
`scripts/model_bakeoff.py` shows `deepseek-v4-flash` and `claude-opus-5` both solving the
tool-schema scenario in 6-8 turns with zero malformed calls. Real runs make 58-73 *distinct*
clicks with genuine form-filling and still do not finish inside 90 turns, because production
ATS flows are multi-step wizards.

Separately, **~78% of high-scoring jobs resolve to an aggregator's own apply flow**
(Indeed/LinkedIn), which blocks automation by design; those are routed to `manual` rather than
retried. Only jobs whose apply URL reaches a real ATS are candidates at all.

Treat auto-apply as a bonus. The fast lane - alert fast, prepare the documents, let a human
submit - is what delivers.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and PR guidelines.

---

## License

JobPilot is licensed under the [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0), matching the license of its parent project, ApplyPilot. It is free software: you may use, modify, and redistribute it under the terms of the AGPL-3.0.


---

## Machine-specific run notes (this install: Windows + MSYS2)

> **See [CLAUDE.md](CLAUDE.md) for the authoritative current state.** It supersedes anything
> below that disagrees, and covers the traps that have actually cost days: the two competing
> `.jobpilot` homes, per-stage model routing, failure modes that look like success, and why
> auto-apply does not complete real ATS forms.

Concrete facts for operating JobPilot on this machine (so a future session doesn't have to rediscover them):

- **Project root**: `C:\msys64\home\aksha\projects\JobPilot`. Python used at runtime is the
  Windows venv at `.venv\Scripts\jobpilot.exe` / `.venv\Scripts\python.exe`.
- **Data home (WAL SQLite)** per profile: `~/.jobpilot/profiles/<active>/` — active profile is in
  `~/.jobpilot/active_profile.txt`; the DB is `jobpilot.db` (currently 5,340 jobs after the
  2026-08-14 dedup). Inner profile copies are also created for workers
  (`apply-workers/`, `chrome-workers/`).
- **Run control flags** (re-read each cycle by `scripts/agent_loop.ps1`): `live.flag` ("1"=real
  submits / "0"=dry-run), `engine.flag` ("claude"|"local"), `auto_apply.flag`. Cycle = 4 h by default.
- **Dashboard**: web UI runs from `src/jobpilot/webui.py`; launch via the desktop `.bat`/`.lnk`
  shortcuts or `scripts/open_dashboard.ps1`. When run standalone it uses Edge in app-mode
  (`APP_MODE=1`). If the page renders but is unclickable, it's a mangled HTML/JS string bug —
  `PAGE_HTML` must be a raw `r"""..."""` so escaped newlines survive.
- **Auto-apply reality check**: workers are headed (visible Chrome, `--profile-directory=Default`,
  Person 1 logins synced). Cloudflare-protected boards (notably **Indeed UAE**) will still flag
  programmatic browsers — those jobs get marked failed with a manual URL. Non-cloudflare ATS
  (Workday, Greenhouse, SmartRecruiters, jobvite, flydubai, etc.) are where auto-apply wins.
- **Backups**: before any one-off DB maintenance the full DB+WAL is copied to
  `~/.jobpilot/backups/jobpilot-<timestamp>/`.
