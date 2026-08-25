# CLAUDE.md — operating notes for an AI session on this repo

Read this first. It records the current real state of this install, the traps that
have already cost days, and what is genuinely broken versus merely configured.
Everything here was verified against the live database and logs on 2026-08-23.

---

## 1. Two homes, and only one is real

`~` means different things to you and to Python on this machine:

| | resolves to |
|---|---|
| `~` in the user's MSYS2 shell | `C:\msys64\home\aksha` |
| `Path.home()` in Python (Windows `USERPROFILE`) | `C:\Users\aksha` |

**Live data:** `C:\Users\aksha\.jobpilot\profiles\default\`
**Dead decoy:** `C:\msys64\home\aksha\.jobpilot\` — holds a stale `.env` with an old
`GEMINI_API_KEY` and an empty `profiles/default`. Nothing reads it. `config.py` uses
`Path.home()`, which ignores the `HOME` that MSYS2 exports.

Project root is `C:\msys64\home\aksha\projects\JobPilot`; the runtime interpreter is the
**Windows** venv (`.venv\Scripts\python.exe`). It cannot be executed from a Linux shell.

## 2. Which model runs which stage

All routing lives in `~/.jobpilot/profiles/default/.env` and resolves in `llm.py`.

| Stage | Env prefix | Currently |
|---|---|---|
| score | `SCORE_LLM_*` | OpenRouter `google/gemini-2.5-flash-lite` |
| tailor | `TAILOR_LLM_*` | OpenRouter `google/gemini-2.5-flash-lite` |
| enrich | `ENRICH_LLM_*` | local `127.0.0.1:8081` nuextract-2.0-4b |
| cover | `COVER_LLM_*` | OpenRouter `google/gemini-2.5-flash-lite` |
| apply | `APPLY_LLM_*` | OpenRouter `deepseek/deepseek-v4-flash-0731` |
| default | `LLM_URL`/`LLM_MODEL` | local `127.0.0.1:8080` qwen3.5-9b |

`SCORE_LLM_*`, `COVER_LLM_*`, and `APPLY_LLM_*` fall back to `TAILOR_LLM_API_KEY`, so one OpenRouter
credential covers all three.

**`engine=local` does not mean a local model.** It means JobPilot's own agent loop
(`apply/local_agent.py`), as opposed to `engine=claude` (the Claude Code CLI). The model it
uses is whatever `APPLY_LLM_*` points at. This naming has already caused one misdiagnosis.

**Do not put the apply engine back on `claude`.** The worker logs contain 275 hits of
"You've hit your weekly limit", and 141 failures where the Claude Code CLI could never load
Playwright MCP and gave up with `RESULT:FAILED:browser_tools_unavailable`.

## 3. Failure modes that look like success

These have each burned real days. Watch for the shape, not just the message.

- **Scoring writes `0` on transport failure.** `score_job()` catches every exception and
  returns `{"score": 0}`. Because `pending_score` selects on `fit_score IS NULL`, a `0` is
  *permanent* — the row is never retried. A dead LLM server on :8080 silently buried **1,882
  jobs** as "bad matches" between Aug 20-22. Guarded now: transport failures set
  `failed: True`, the row is left `NULL`, and the run logs a loud ERROR. Parse failures still
  write `0`.
- **`Done: N scored in Xs` is not proof of anything.** That line printed happily while every
  one of those N was a connection error. Check the score distribution, not the count.
- **An apply cycle that ends in ~2 seconds is a crash, not an empty queue.** For weeks apply
  died instantly on a `UnicodeEncodeError` printing `\u2192` to a cp1252-redirected stdout.
  `jobpilot/__init__.py` now forces UTF-8 at import; `cli.py` is ASCII-only in printed
  strings. Keep it that way.
- **`Ready to apply: N` in the dashboard is inflated.** It ignores the blocked-site filter
  that `acquire_job` applies. Real acquirable count is far lower — verify with the query in
  `acquire_job`, not the dashboard.

## 3b. The same bug, three times

One root cause kept reappearing: **a transport failure recorded as a failed
attempt at the job**, spending a budget that never refills.

| Counter | Killed by | Buried | Guarded |
|---|---|---|---|
| `fit_score = 0` | :8080 down since Aug 20 | 1,882 jobs | yes |
| `cover_attempts = 5` | :8082 down since Aug 12 | 457 jobs | yes |
| `apply_attempts = 10` | assorted apply failures | 162 jobs | partly |

Each looked like a quality signal ("bad match", "gave up after 5 tries") and was
actually a dead server. `scripts/reset_stuck.py` returns capped rows to the
queue -- dry-run by default, `--apply` to commit, reversible record in
`backups/`, idempotent. Reach for it whenever a stage's 24h count is zero while
the stage above it is producing.

**Symptom to watch:** in `jobpilot health`, compare the `last 24h` column
between adjacent stages. `tailored 217 / cover 0` is what exposed this one.

## 3c. Keeping the loops alive

`powershell -File scripts\agent_loop.ps1` ties the loop to that console --
closing the window kills it silently. Both loops were found stopped this way
after a working session. Use:

```
powershell -File scripts\start_loops.ps1            # start whichever is down
powershell -File scripts\start_loops.ps1 -Restart   # cycle both
```

It launches detached via `Start-Process -WindowStyle Hidden`, is idempotent,
and cross-checks pid files against live processes. Verify with
`jobpilot health`, never with the pid file alone.

Also: **PowerShell parses `<` as a reserved redirection operator even inside a
quoted `-c` argument.** An inline SQL comparison (`apply_attempts < ?`) made
`agent_loop.ps1` unparseable and un-startable. Queries live in their own files
now (`scripts/ready_count.py`). Before editing any `.ps1`, check it parses:

```
$e = $null
[void][System.Management.Automation.Language.Parser]::ParseFile("$PWD\scripts\agent_loop.ps1", [ref]$null, [ref]$e); $e
```

## 3d. Throughput caps that are easy to miss

`run_tailoring` and `run_cover_letters` were hard-coded to **20 jobs per
cycle**, so a 457-job backlog needed 23 full pipeline cycles -- each of which
spends most of its wall clock in discovery first. Now `STAGE_LIMIT`, default
200, override with `JOBPILOT_STAGE_LIMIT`.

## 4. Auto-apply does not work, and it is not the model's fault

Measured, not assumed. `scripts/model_bakeoff.py` replays the exact tool-schema failure:

```
deepseek/deepseek-v4-flash-0731      done=YES   8 turns   0 malformed
anthropic/claude-opus-5              done=YES   6 turns   0 malformed
```

A $0.08/M model and a $5/M model both handle it perfectly. Three hypotheses were tested and
all three were wrong: tool-result truncation, a repeat-call death spiral (the loop-breaker
fires **zero** times in production), and model capability.

What actually happens on a real ATS form: 58-73 **distinct** clicks, real
`browser_fill_form` and `browser_file_upload` calls, genuine progress — and still no
completion inside 90 turns. These are multi-step wizards (account creation, EEO pages,
pagination). More turns will not close it.

**Bigger structural problem:** 78% of `score>=6` jobs resolve to an aggregator's own apply
flow (Indeed/LinkedIn), which blocks automation by design. Only ~22% reach a real ATS.
`indeed.com`, `ae.indeed.com`, and `linkedin.com` are now in `manual_ats` in
`config/sites.yaml` so they are retired rather than retried.

**Current stance: auto-apply is a bonus, not the plan.** The user applies by hand. The
product value is the fast lane.

## 5. The fast lane is the thing that works

`jobpilot watch` / `scripts/fast_lane.ps1`. Measured cycle: **87 seconds** from posting to
a scored, tailored, cover-lettered job with a desktop alert.

- Polls a 2h window every 5 min (JobSpy) plus a rotating slice of 8 Workday employer portals
  per cycle — full registry coverage every ~4 cycles. The Workday portals are where the
  *automatable* postings are; aggregators mostly are not.
- Set-differences against the DB so only genuinely new URLs are touched, newest-first.
- **Notifies before prepping.** Knowing at minute 3 beats knowing at minute 12.
- **Never submits.** Prepares and holds.
- Every match is appended to `~/.jobpilot/fresh_jobs.jsonl` twice — `stage:"found"`, then
  `stage:"prepped"` with `resume` and `cover_letter` paths. This is the manual-apply
  worklist, and the fallback if a toast is missed.
- Toasts are clickable and open the job's `application_url`. `launch=""` renders a toast
  that chimes and does nothing — do not reintroduce that.

Threshold: at ~9 new jobs per 2h window, score >=7 fires roughly once a day and >=6 about a
dozen times. Currently running at 6.

## 6. Dependency traps

- **crawl4ai 0.9.2 renamed `BM25ContentFilter(threshold=)` to `bm25_threshold=`.** These
  filters are built at *module level* in `markdown_extract.py`, so the `TypeError` was an
  import-time failure that took down enrichment, smart extract, and the fast lane at once.
  Now resolved from the live signature, and generator construction degrades to unfiltered
  markdown instead of raising. Verified against old, new, and hypothetical-future APIs.
- **`PruningContentFilter` still uses `threshold`/`threshold_type`.** Only BM25 renamed.
- Pinning crawl4ai would also work, but the degrade-don't-explode guard is the durable fix.

## 7. Ordering rules that matter

- `acquire_job` orders by `COALESCE(apply_attempts,0) ASC` **first**. Ordering by score alone
  meant one hard job was re-acquired every pass — a `--limit 3` run spent all three slots on
  the same Eightfold form, and historical "0 applied, 15 failed" cycles were 15 attempts at a
  handful of jobs.
- `acquire_job` **skips** manual-ATS rows rather than returning `None`. Returning `None` told
  the worker the queue was empty when only the top row was unapplyable — fatal now that most
  of the queue is aggregator URLs.
- Stale `in_progress` claims older than 45 min are reclaimed. A killed worker used to strand
  rows forever, since `in_progress` matches neither the `NULL` nor `'failed'` branch.
- Enrichment scrapes newest-first within each site batch, because the run is frequently cut
  short by the watchdog.

## 8. Operational facts

- `scripts/agent_loop.ps1` works the **backlog** on a 4h cycle and regularly hits its
  180-min watchdog. Do not put anything time-sensitive behind it — that is what the fast lane
  is for. Configurable via `JOBPILOT_PIPELINE_MAX_MIN`.
- Flags in `~/.jobpilot/profiles/default/`: `live.flag` (1=real submits), `engine.flag`
  (`claude`|`local`), `auto_apply.flag`. Re-read every cycle.
- `searches.yaml` bulk discovery is `hours_old: 48`, `results_per_site: 50`. It was 168h/100,
  which is why cycles never finished. The fast lane overrides both.
- The employer registry has 48 entries but only **27 enabled**; the rest are finance/pension
  employers disabled by default. Enabling relevant ones is the cheapest way to grow the
  automatable pool.
- `jobpilot health` is the honest status view: loop liveness (pid cross-checked
  against log activity), per-stage 24h counts, and the acquirable queue. The
  dashboard's `Ready to apply` ignores the blocked-site filter and reads roughly
  3x higher; `health`'s `acquirable to apply` is the real number.
- Windows toasts borrow PowerShell's AppUserModelID, so they display as PowerShell
  notifications. Cosmetic; fixing it needs a registry-registered AUMID.

## 9. Known-inconsistent docs

`PRODUCT.md` refers to an AGPL-3.0 license; `LICENSE` and `README.md` say proprietary.
Unresolved — confirm with the owner before relying on either.
