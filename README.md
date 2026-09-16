<div align="center">

# JobPilot

**The job-application copilot with guardrails.**
Discover → score → tailor → apply → *learn* — on any model, one OpenRouter key.

[![PyPI version](https://img.shields.io/pypi/v/job-pilot-ai?color=blue)](https://pypi.org/project/job-pilot-ai/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-126%20passing-brightgreen)](.github/workflows/ci.yml)

</div>

Most auto-appliers optimize for *volume*. JobPilot optimizes for *signal*: it
filters jobs you can't take before wasting tokens on them, scores the rest
with an explainable rationale, never fabricates skills or metrics, tracks
what actually happens after you apply, and feeds that back into scoring. A
fork of [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) with a
discipline layer on top.

## Quick start

```bash
pip install job-pilot-ai
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex

jobpilot init          # one-time setup: profile, resume, preferences, LLM key
jobpilot run           # discover -> enrich -> score -> tailor -> cover letters
jobpilot apply         # autonomous browser submission (optional)
```

One **OpenRouter** key (free tier works) powers the whole pipeline; Gemini,
OpenAI, and local llama.cpp/Ollama also work. See [Configuration](#configuration).

Prefer a GUI installer, or a phone-friendly dashboard view? See
`installer/` and `android/README.md`.

## Why JobPilot

- **Hard pre-score gates** — eligibility and language are checked before a
  job burns a single scoring token.
- **Explainable fit scores** — five 0–100 dimensions with a rationale;
  deal-breakers veto outright instead of hiding in one opaque number.
- **Closed outcome loop** — `jobpilot outcome` records interview/offer/
  rejection and recalibrates scoring from what actually worked.
- **ATS-safe documents** — the final PDF's text layer is verified the way a
  parser actually reads it, not just how it looks.
- **Hostile-posting defense** — job ads are treated as data, never as
  instructions to the agent.
- **Interview + skill-gap tools** — STAR-bridge prep, a learning plan, and
  (once an interview lands) a human-optimized, F-pattern resume separate
  from the ATS-tailored one.

## The loop

```
discover -> enrich -> gate -> score -> tailor -> cover -> apply -> outcome
                                                              |
                                        (recalibrate scoring)_|
```

Run the whole thing, or one stage at a time — `jobpilot run discover`,
`jobpilot score-dims --text <post>`, `jobpilot outcome --list`, etc. A fast
lane (`jobpilot watch`) polls new postings every 5 minutes and alerts you
with a prepared resume in under 2 minutes — no auto-submit.

## Configuration

`jobpilot init` generates `profile.json` (your data + the facts tailoring
must never invent), `searches.yaml` (target roles/locations/boards), and
`.env` (LLM keys). Each pipeline stage can point at a different model via
env vars (`TAILOR_LLM_MODEL`, `SCORE_LLM_MODEL`, etc.) — or just set
`OPENROUTER_API_KEY` and `LLM_MODEL` and let the fallback chain handle it.

## Requirements

| Component | Needed for |
|---|---|
| Python 3.11+ | Everything |
| OpenRouter API key (or Gemini/OpenAI/local) | Scoring, tailoring, cover letters |
| Chrome + Node.js 18+ | Auto-apply only |
| CapSolver key | Auto-apply CAPTCHA solving (optional) |

## CLI reference

```
jobpilot init | doctor | run [stages] | status | dashboard
jobpilot gate <text>                        # pre-score hard gates
jobpilot score-dims --text <post>           # explainable fit score
jobpilot outcome <url> -s interview         # record a real outcome
jobpilot interview-cv <url>                 # human-optimized CV once you land an interview
jobpilot interview --company X --posting T  # STAR-bridge prep pack
jobpilot upskill --text <post>              # skill-gap analysis
jobpilot apply [--dry-run] [--url URL]      # autonomous browser submission
jobpilot watch                              # fast lane: poll, alert, prep, hold
```

**Auto-apply, honestly:** it handles non-Cloudflare ATS forms (Workday,
Greenhouse, SmartRecruiters, ...) well but not every form finishes, and
roughly three-quarters of high-scoring jobs route through aggregator apply
flows that block automation by design — those are marked `manual`. The fast
lane (alert + prepare, human submits) is the reliable path.

## Building & testing

See [BUILDING.md](BUILDING.md) for the full build/publish flow.

```bash
python -m build                # dist/job_pilot_ai-*.whl + .tar.gz
python -m pytest tests/ -v     # 126 tests, CI on Python 3.11/3.12/3.13
```

## License & provenance

**AGPL-3.0** — see [LICENSE](LICENSE). JobPilot evolves ApplyPilot
(Pickle-Pixel, AGPL-3.0); some patterns (gates, outcome loop, untrusted-input
policy) are drawn from the MIT-licensed
[ai-job-search](https://github.com/MadsLorentzen/ai-job-search) project.

> Not affiliated with applypilot.app, useapplypilot.com, or any other
> product using the "Pilot" name.
