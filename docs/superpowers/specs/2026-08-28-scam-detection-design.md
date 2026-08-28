# Scam-posting detection and blocking

## Why

Prompted by a real near-miss: while manually job hunting on LinkedIn, a "recruiter"
DM for a vague remote/project-based role asked for a personal bank account "for
payment processing" and pushed the conversation off-platform to a separate,
unverified Microsoft Teams contact before any real interview happened — a classic
task-scam / money-mule pattern. JobPilot had no equivalent protection for postings
it discovers and auto-applies to on your behalf, so this closes that gap for the
part of the flow JobPilot actually touches: the scraped job posting text.

Out of scope, explicitly: recruiter DMs/InMail after the fact. JobPilot never sees
those — they happen on LinkedIn's own site, outside anything this tool touches.
This protects against a scammy *posting*, not a scammy *follow-up message*.

## Where it plugs in

- New module `src/jobpilot/scam_gate.py`, evaluated once per job during
  `enrichment/detail.py` (same site as `sanitize_posting()`), right after
  sanitization. Result is stored on the row, not recomputed later.
- `apply/launcher.py`'s `acquire_job()` gets one more `WHERE` clause so a blocked
  job never enters the apply queue — same shape as its existing
  `blocked_sites`/`blocked_patterns` filtering.
- Scope: runs on every job **except** `strategy = 'workday_api'` (the curated
  `employers.yaml` allowlist — named real employers, effectively zero scam risk).
  This means it *does* run on LinkedIn/Indeed/Glassdoor-sourced postings even
  though those currently route to manual apply (`manual_ats`/`blocked` in
  `sites.yaml`) — for those, the gate's value is the dashboard warning shown to
  you before you apply by hand, not an auto-apply block. For the ~30
  smartextract-sourced boards (RemoteOK, Dice, SimplyHired, Wellfound, etc.)
  where the bot does auto-apply, the block has real enforcement effect.

## Heuristic gate (fast path — most jobs resolve here, no LLM call)

Same explainable style as the existing `gating.py` pre-score gates: every verdict
carries a `reason` and a `quoted` excerpt of the actual posting text.

Strong match -> blocks immediately, no LLM call needed (these are essentially
never legitimate):
- Upfront payment / fee language: "processing fee", "training fee", "purchase
  your own equipment", "registration fee"
- Payment-processing bank access requests: "personal bank account for payment
  processing", "provide your bank account for payment", wire/e-transfer/gift-card
  instructions, check-cashing/deposit instructions
- Premature sensitive-info requests: SSN, bank/routing numbers, government ID
  requested before any real interview

Soft match -> escalates to the LLM tie-break rather than blocking outright
(real false-positive risk if blocked on regex alone):
- Off-platform pivot to an unverified contact channel before any interview:
  WhatsApp/Telegram, or a *second* email/Teams/Skype contact disconnected from
  the application itself (e.g. "confirm your Teams email, if different from your
  CV email") — the red flag is the disconnected second channel, not the platform
- Too-good-to-be-true / generic-HR-mill framing: "no interview required",
  "guaranteed hire", "start today", "flexible, work around your existing
  commitments", combined with no concrete company name or role specifics
  ("our confidential client", "the project", "multiple opportunities available")

## LLM tie-break (only for soft-match / ambiguous cases)

One extra call, only for the minority of jobs the heuristic can't already
resolve. Env-prefix convention matches the existing pattern (`SCAM_LLM_*`,
falling back the same way `SCORE_LLM_*` falls back to `TAILOR_LLM_API_KEY`).
Modeled on `reviewer_pass`'s structured-verdict style: reads the posting text,
returns legit/suspicious + a one-line reason.

## Fail-closed on error

`CLAUDE.md` documents this exact failure shape three times already — a
transport failure silently recorded as a permanent negative result
(`fit_score = 0` burying 1,882 jobs; `cover_attempts` maxing out on a dead
server). This gate does not repeat that pattern:

- If the LLM tie-break call errors out, `scam_verdict` stays `NULL` (pending) —
  never written as `clear` or `blocked`.
- `acquire_job()`'s new filter is a **positive** check (`scam_verdict = 'clear'`),
  not a negative one (`!= 'blocked'`) — so a pending/`NULL` verdict never
  accidentally lets a job through during an LLM outage. Fails closed, not open.
- Tradeoff, stated plainly: if that LLM endpoint is down for a while, affected
  jobs won't auto-apply until it clears — the safe direction for a scam gate.
  Visible in `jobpilot health` the same way the other stuck-counters already are.

## Schema

Three new columns on `jobs`, via the existing `ensure_columns` migration path:
- `scam_verdict` TEXT — `clear` | `blocked` | NULL (pending)
- `scam_reasons` TEXT — JSON list of `{category, quoted}`
- `scam_checked_at` TEXT

## Dashboard

Both an inline warning badge (with reason) wherever a job already appears, and a
dedicated flagged-jobs view for batch review.

## Testing

Same script-style convention as `test_quality.py` / `test_local_agent.py`:
- Unit cases per heuristic category (positive and negative), including the
  real-world phrasing above (bank-account-for-payment-processing, Teams-pivot,
  vague-project-based language)
- A mocked LLM tie-break test
- A fail-closed test: simulated LLM error leaves `scam_verdict` NULL, not
  defaulting either direction

## Documentation

The commit and `CHANGELOG.md` entry for this feature state the motivation
plainly but generically: prompted by a real scam attempt encountered while job
hunting, without naming the scammer, the shell company, or any identifying
details from the actual conversation — the *pattern* is the useful part to
document, not the specific incident.
