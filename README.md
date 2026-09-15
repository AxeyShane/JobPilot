<div align="center">

# JobPilot

**The job-application copilot with guardrails.**
Discover → score → tailor → apply → *learn* — on any model, one OpenRouter key.

[![PyPI version](https://img.shields.io/pypi/v/job-pilot-ai?color=blue)](https://pypi.org/project/job-pilot-ai/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-126%20passing-brightgreen)](.github/workflows/ci.yml)
[![GitHub stars](https://img.shields.io/github/stars/AxeyShane/JobPilot?style=social)](https://github.com/AxeyShane/JobPilot)

</div>

Most auto-appliers optimize for *volume*: fire 1,000 applications and hope.
JobPilot is built for *signal*: it scores honestly, filters before it wastes
your time, records what actually happens after you apply, and feeds the
results back into the next round. It is a fork of
[ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) that adds the
discipline layer on top of the autonomous pipeline.

It's a desktop app, not a script you babysit in a terminal — install it,
open it, everything from starting a search to reviewing an interview-stage
resume happens in one window.

---

## Why JobPilot

| JobPilot does | Instead of |
|---|---|
| **Standalone control app** — `jobpilot web` opens as a native desktop window, not a browser tab | another tab lost in the pile |
| **Hard pre-score gates** — eligibility & language checked *before* scoring | burning tokens tailoring a job you can't take |
| **Explainable fit scores** — five 0–100 dimensions with a rationale, deal-breakers veto outright | one opaque number you can't argue with |
| **Closed outcome loop** — records interview/offer/rejection on the dashboard and recalibrates scoring from real results | applying into a void with no feedback |
| **ATS-safe documents** — the final PDF's text layer is verified the way a parser reads it | "looks fine in the .tex", broken in the ATS |
| **Hostile-posting defense** — job ads are data, never instructions | letting a crafted job ad steer the agent |
| **Interview + skill-gap tools** — STAR-bridge prep, a learning plan, and once an interview is confirmed a second F-pattern CV written for a human reader, not a parser | stopping the moment you hit submit |

Every feature is honest by construction: **it never fabricates skills,
experience, or metrics** — genuine gaps stay visible as gaps.

---

## Get it running

### Desktop app (recommended)

```powershell
powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1
```

Builds `JobPilotSetup.exe` — a per-user installer, no admin rights and no
Python needed on the machine running it. Installs, creates Start Menu and
Desktop shortcuts, and the app opens as its own native window (WebView2 on
Windows) — no browser tab, no address bar. Not yet published as a release
download, so build it yourself for now; budget a little extra time on the
first run, since this is the first time `playwright` and `crawl4ai` have
been frozen with PyInstaller in this portfolio. Auto-apply needs a one-time
`playwright install chromium` the app will prompt for.

**Reviewing from your phone:** `android/` is a thin WebView client, same
pattern as TechRadar's — it needs a JobPilot server already running
somewhere reachable, it doesn't run JobPilot itself. Build with
`python android/build_apk.py` (needs the Android SDK build-tools and an
Android Studio JBR). See `android/README.md`.

### From source

```bash
pip install job-pilot-ai
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex

jobpilot init           # one-time setup: profile, resume, preferences, LLM key
jobpilot doctor         # verify everything is in place
jobpilot web            # the app, standalone desktop window by default
jobpilot run            # or run headless: discover > enrich > score > tailor > cover letters
```

`jobpilot web` opens as its own desktop window (pywebview + WebView2 on
Windows). Pass `--browser` for a plain browser tab instead (e.g. browser
devtools), or `--no-browser` to run the control server headless. Every CLI
command below works standalone too — the desktop app is a view onto the
same pipeline, not a separate thing.

> **Why two install commands?** `python-jobspy` pins an exact numpy version
> in its metadata that conflicts with pip's resolver but runs fine with modern
> numpy. The `--no-deps` flag bypasses the resolver; the second command
> installs jobspy's actual runtime dependencies.

One **OpenRouter** key powers the whole pipeline (`openrouter.ai/keys` —
free tier works). Gemini, OpenAI, and local llama.cpp/Ollama endpoints are
also supported. See [Configuration](#configuration) for per-stage model
routing.

---

## The loop

```
                        ┌──────────────────────────────────────────────┐
                        │                                              │
   discover  →  enrich  →  [gates]  →  score-dims  →  tailor  →  cover │
     5 boards +             full JD     eligibility     per-job     per-job
     Workday (48) +                     + language      resume      letter
     direct sites (30)                   veto if FAIL   (never       │
                                                         fabricates)  ▼
                        ┌──────────────────────────────────────────────┐
                        │               apply (auto or manual)         │
                        └───────────────────────┬──────────────────────┘
                                                ▼
                        outcome ← interview ←  replies  ←  submitted
                        (interview stage generates a human-facing CV,
                         then recalibrates scoring from what worked)
```

Run the whole loop from the desktop app, or one stage at a time from the
CLI:

```bash
jobpilot run discover          # just discovery
jobpilot run score-tailor      # score + tailor already-discovered jobs
jobpilot gate <posting text>   # eligibility + language check before scoring
jobpilot score-dims --text <posting>  # explainable fit score
jobpilot outcome --list        # where every application actually stands
```

### Fast lane (new postings, sub-2-minute alert)

`jobpilot watch` polls a 2-hour window every 5 minutes, scores only genuinely
new postings, alerts you on the desktop, preps a tailored resume + cover
letter, and **stops without submitting** — measured 87 seconds from posting to
prepared-and-alerted. Runs alongside the full pipeline.

```bash
jobpilot watch --once          # one poll, then exit
jobpilot watch --min-score 6   # alert threshold (default 7)
powershell -File scripts/fast_lane.ps1   # run continuously
```

### When it counts: the interview

The moment you record an interview outcome — on the dashboard or via
`jobpilot outcome <url> -s interview` — JobPilot builds a second resume
automatically. Not the ATS-tailored one that got you in the door: an
F-pattern layout written for a person to read, with a positioning line
under your name and one bolded outcome per bullet, matching how
eye-tracking research says a hiring manager actually scans a page. Same
fabrication guardrails as the ATS pipeline, none of the keyword-stuffing.

```bash
jobpilot interview-cv <url> --notes "what came up in the call"
```

---

## Configuration

All generated by `jobpilot init`:

- **`profile.json`** — your data: contact, work authorization, compensation
  floor and range, experience, skills, and `resume_facts` (the exact facts
  tailoring must preserve).
- **`searches.yaml`** — target roles, queries, locations, boards.
- **`.env`** — LLM keys and runtime tuning.

### Per-stage LLM routing (the OpenRouter way)

Each pipeline stage can use a different model — cheaper for high-volume
scoring, better for quality-critical tailoring, and a dedicated model for the
multi-turn apply loop. One OpenRouter key covers all of it (score & apply
inherit the tailor key when unset):

```bash
OPENROUTER_API_KEY=sk-or-v1-...                     # the one key

TAILOR_LLM_URL=https://openrouter.ai/api/v1
TAILOR_LLM_MODEL=google/gemini-2.5-flash-lite
SCORE_LLM_MODEL=google/gemini-2.5-flash-lite        # high volume, cheap model
ENRICH_LLM_MODEL=nuextract-2.0-4b                   # or a local extractor
COVER_LLM_MODEL=google/gemini-2.5-flash-lite
APPLY_LLM_MODEL=deepseek/deepseek-v4-flash-0731     # long tool-calling loop
```

If you prefer one setting for everything, set only `OPENROUTER_API_KEY` and
`LLM_MODEL` — the fallback chain does the rest. Your current setup **runs on
OpenRouter with zero config beyond the key**.

---

## Requirements

| Component | Needed for | Notes |
|---|---|---|
| Python 3.11+ | Everything (source install) | not needed for the packaged .exe |
| OpenRouter API key | Scoring, tailoring, cover letters | free tier is enough; Gemini/OpenAI/local also work |
| Chrome + Node.js 18+ | Auto-apply only | powers the browser agent (`@playwright/mcp`) |
| Claude Code CLI | Auto-apply only | optional engine for form navigation |
| CapSolver key | Auto-apply only | optional CAPTCHA solving |

---

## CLI reference

Every one of these also works while the desktop app is running — same
database, same pipeline, different door in.

```
jobpilot init                      # first-time setup wizard
jobpilot doctor                    # verify setup, what's missing
jobpilot web                       # standalone desktop app (native window by default)
jobpilot dashboard                 # static HTML report, opened in your browser

jobpilot run [stages...]           # pipeline: discover enrich score tailor cover pdf
jobpilot run --workers 4           # parallel discovery/enrichment
jobpilot run --min-score 8         # higher scoring bar
jobpilot run --validation strict   # strictest validation (retries on any banned word)
jobpilot watch                     # fast lane: poll, alert, prep, hold
jobpilot status                    # pipeline statistics
jobpilot health                    # are the loops actually running? queue depth, recent finds

jobpilot gate <text>               # pre-score hard gates (eligibility + language)
jobpilot score-dims --text <post>  # explainable 5-dimension fit score

jobpilot outcome <url> --status interview   # record a real outcome (auto-builds the interview CV)
jobpilot outcome --list            # where every application stands
jobpilot outcome --recalibrate     # feed real results back into scoring
jobpilot outcome --promote url=sig # drafted -> applied on an ack signal
jobpilot interview --company X --posting TXT   # STAR-bridge prep pack
jobpilot interview-cv <url>        # human-facing CV once an interview is confirmed
jobpilot upskill --text <post>     # skill-gap analysis + learning plan
jobpilot report <url> --note TXT   # report a scam posting; --list / --export for the community feed

jobpilot apply                     # autonomous browser submission
jobpilot apply --dry-run           # fill forms, don't submit
jobpilot apply --url URL           # apply to one specific job
jobpilot notify-test               # confirm desktop toasts actually appear
```

### Auto-apply: the honest current state

Real ATS forms are multi-step wizards; auto-apply reliably handles
non-Cloudflare ATS (Workday, Greenhouse, SmartRecruiters, jobvite, …) but
**does not finish every form** — and ~78% of high-scoring jobs route to an
aggregator's own apply flow (Indeed/LinkedIn) that blocks automation by
design. Those are marked `manual`, not retried.

Treat auto-apply as a bonus. The fast lane — alert fast, prepare the
documents, let a human submit — is what delivers.

---

## Building & publishing

Build, test, and publish instructions are in **[BUILDING.md](BUILDING.md)**:
wheel + sdist, the 126-test suite, local install check, and two publish paths
(GitHub Actions OIDC on `v*` tags, or manual `twine`). For the standalone
desktop app and Android companion, see [Get it running](#get-it-running)
above and `installer/build_installer.ps1` / `android/build_apk.py` directly.

```bash
pip install build twine
python -m build                        # dist/job_pilot_ai-*.whl + .tar.gz
python -m pytest tests/ -v             # 126 tests
```

CI runs tests on Python 3.11/3.12/3.13 for every push and PR.

---

## License & provenance

**AGPL-3.0** — see [LICENSE](LICENSE).

JobPilot is an open-source evolution of ApplyPilot (Pickle-Pixel, AGPL-3.0).
Some design patterns (gates, outcome loop, ATS verification, untrusted-input
policy, interview prep) are drawn from the MIT-licensed
[ai-job-search](https://github.com/MadsLorentzen/ai-job-search) framework and
its release history.

> JobPilot is not affiliated with applypilot.app, useapplypilot.com, or any
> other product using the "Pilot" name. Those sites are unrelated to this
> project.
