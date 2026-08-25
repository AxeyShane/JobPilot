# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Akshay Kharvi — the sole user. A non-developer (Production Manager / mechatronics background) directing and monitoring his own autonomous AI job-application pipeline during an active, time-pressured job search (Dubai relocation, visa sponsorship, real salary target). He's technically comfortable enough to review logs, toggle settings, and make judgment calls on model/config tradeoffs, but the dashboard is the primary interface — not the code.

## Product Purpose

The web dashboard is the control and monitoring surface for JobPilot's autonomous pipeline (discover → score → tailor → cover letter → apply). It exists so the user can see what the pipeline is actually doing, control when real applications go out, and step in when something needs a human (a login wall, a stuck job, a blocked site) — without reading logs or touching the CLI. Success is the dashboard being a trustworthy source of truth about a system that runs partly unattended, and giving the user confident control over the one truly consequential action it can take: submitting a real application to a real employer.

## Positioning

A self-hosted, local-first control panel for one person's own autonomous job-application agent — not a multi-tenant SaaS. Everything runs on the user's own machine (local LLMs, local browser automation), with full visibility into every pipeline stage and a manual override at every consequential gate (live/dry-run toggle, per-site blocking, mark applied/failed, engine selection). This directness — nothing hidden behind a hosted black box — is the thing a commercial competitor product couldn't truthfully copy without becoming a different kind of product.

## Operating Context

Runs locally on Windows alongside local llama.cpp LLM servers (CPU instances for enrichment and cover letters) and Chrome browser-automation workers, with scoring, tailoring, and auto-apply routed to a cloud gateway (OpenRouter) so those stages survive the local GPU server being down — a failure that silently corrupted three weeks of scoring before it was caught. Interacts with real job boards (Indeed, LinkedIn) and real employer ATS systems. Persistence is a single SQLite database per profile at `~/.jobpilot/profiles/<active>/jobpilot.db` in WAL mode; the active profile is named in `~/.jobpilot/active_profile.txt` (currently `default`). LIVE/DRY is toggled by `~/.jobpilot/profiles/<active>/live.flag` ("1"/"0"), re-read every cycle by `scripts/agent_loop.ps1`. Used actively during a live job search with real stakes — real applications go to real employers, real resumes represent the candidate's actual work history, and mistakes (a fabricated resume claim, a silently-stalled loop, LinkedIn triggering a security review) have real consequences, not just UX friction.

## Capabilities and Constraints

- Tiered feature gating (Discovery only / +AI Scoring & Tailoring / +Full Auto-Apply) based on what's installed (LLM key, Claude Code CLI, Chrome).
- Live vs. dry-run submission gate — real submissions require explicit, visible opt-in; dry-run is the safe default.
- Per-stage LLM routing: scoring/tailoring on a GPU model (quality-sensitive, fabrication validator enforced), enrichment/cover-letters on smaller CPU models (cost/speed-sensitive).
- Site-level blocklist (currently holding LinkedIn out of the apply pool until its login/security-review is manually resolved).
- Autonomous loop supervisor/health check (added after a real incident where the dashboard showed "running" for hours while genuinely uncertain what was happening) — must reflect real state, not cached or stale state, as a hard product requirement, not a nice-to-have.
- Windows-only automation stack (PowerShell process orchestration, llama-server, Playwright/Chrome) — not currently cross-platform.
- **Fast lane (added 2026-08-23):** a short-interval discovery loop that alerts on the desktop and prepares documents within minutes of a posting going live, deliberately stopping short of submission. Measured 87s posting-to-prepared, against a previously measured 1-7 day discovery-to-submission latency.
- **Auto-apply is a bonus capability, not the product.** Verified on 2026-08-23 that it does not reliably complete real ATS forms, and that this is not a model limitation (a $0.08/M and a $5/M model both solve the tool-schema scenario perfectly; real runs make 58-73 distinct clicks with genuine form-filling and still do not finish in 90 turns). Roughly **78% of high-scoring jobs resolve to an aggregator's own apply flow**, which blocks automation by design. Product framing should reflect this: the system's reliable promise is *know first, with materials ready*, not *applies for you*.

## Brand Commitments

None beyond the open-source project's own name and AGPL-3.0 license. Explicitly **not affiliated** with commercial products of a similar name (jobpilot.app, usejobpilot.com) — this disclaimer is already load-bearing in the project's README and should not be contradicted by anything the dashboard implies. No visual identity established yet.

## Evidence on Hand

Real, live data only — a SQLite database with thousands of actually-discovered jobs (8,228 as of 2026-08-23; 5,340 after the 2026-08-14 dedup pass that removed 71 duplicate rows: the same posting stored under multiple Indeed `jk`/`loc` tracking URLs), real fit scores, real tailored resumes and cover letters, and real submitted applications. Two cautions when citing numbers from this system: the dashboard's `Ready to apply` count ignores the blocked-site filter that `acquire_job` actually applies and is therefore inflated; and 1,882 jobs carried a `fit_score` of 0 that meant "the scoring server was unreachable", not "poor match" (reset 2026-08-23, guard added). No fabricated or mock data anywhere in the system; future design/content work must use real numbers from the live database, never invented testimonials, sample users, or placeholder success metrics.

## Product Principles

1. **Show real state, never stale or misleading state.** The dashboard's core job is being a trustworthy source of truth about a system operating partly unattended — a wrong "running" badge is a product failure, not a cosmetic bug.
2. **Every consequential action stays behind an explicit, visible gate.** Real submissions, unblocking a held site — never silently automatic, always a deliberate user action.
3. **Local-first and self-contained.** No cloud dependency required to operate; the whole system should keep working if the user's internet or a cloud API goes away.
4. **Honesty over polish, end to end.** The pipeline actively rejects fabricated resume content; the dashboard should carry the same value — real numbers, real statuses, no decorative or placeholder data.
