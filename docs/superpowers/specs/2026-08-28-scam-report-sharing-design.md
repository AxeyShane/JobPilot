# Cross-user scam report sharing

## Why

A companion to the local scam-posting gate (see
`2026-08-28-scam-detection-design.md`). That gate catches scam signals baked
into a posting's own text at enrichment time. This piece covers the case where
a user encounters a scam directly (e.g. through a recruiter DM after applying)
and wants that knowledge to protect other JobPilot users too, not just
themselves. Checked first: no existing free/open API covers this — BBB's API
requires a vetted application and doesn't expose Scam Tracker data externally;
the FTC's public API covers only Do-Not-Call and merger filings. There's
nothing to integrate with; this has to be JobPilot's own mechanism.

## Why not a live synced backend

Considered and deliberately rejected for now. JobPilot has no backend today —
it's a single-user, self-hosted, local-SQLite tool. A live shared database
that any user can write to needs: somewhere to run it, user identity/auth,
and — the real blocker — moderation. A writable shared database of
accusations against named companies and individuals, with no review step, is
straightforwardly abusable (false reports, competitor sabotage) and carries
real defamation-liability exposure for whoever operates it. None of that is
proportionate to what this project is.

## Design: local reporting + a repo-hosted community file

### Local reporting (always on, no network dependency)

- A "Report as scam" action on the job card in the dashboard (`webui.py`),
  next to the existing scam-gate badge.
- Reporting immediately sets `scam_verdict = 'blocked'` on that row (a human
  judgment call overrides whatever the automated heuristic/LLM gate said) and
  appends a `{"category": "user-reported", "note": <optional free text>}`
  entry to `scam_reasons`.
- Also stores the posting's `full_description` at the time of the report as a
  "known-bad" signature — not just the URL. Scammers commonly repost the same
  pitch under a new URL; matching only the original link would miss the
  repost. Future postings get fuzzy/substring-matched against previously
  reported text by the same heuristic-gate machinery, not a separate system.
- A `jobpilot report --export` CLI command dumps the user's local reports in
  the exact shape the community file (below) expects — output only, no
  network call, no automatic submission anywhere.

### The shared file

- New file: `src/jobpilot/config/community_scam_reports.yaml`, committed to
  the repo — same pattern as the existing `sites.yaml`/`employers.yaml`.
- Per entry: company/entity name as claimed in the posting, any known
  domain(s), and the quoted scam-pitch excerpt. Explicitly excluded: the
  individual "recruiter's" name, personal profile links, or any DM content
  beyond the pitch text itself — same reasoning as keeping the design-doc
  commit message generic.
- JobPilot fetches the latest copy from the repo's raw GitHub URL on the
  normal pipeline cadence, caches it locally. A fetch failure logs a warning
  and falls back to the last cached copy — this is a supplementary signal on
  top of the local heuristic/LLM gate, not the primary defense, so it doesn't
  need that gate's fail-closed strictness (a stale or missing community list
  should never block the pipeline).

### Getting a local report into the shared file

- Deliberately manual, not automated. `jobpilot report --export` produces
  the paste-ready snippet; turning it into a pull request is a human action.
- The repo maintainer reviewing and merging each PR is the actual moderation
  step. Worth stating plainly: this bounds the liability concern above, it
  doesn't remove it — only entries the maintainer personally reviews and
  approves ever reach other users, rather than anything anyone submits going
  out automatically.

## Schema

- `jobs.scam_verdict` / `scam_reasons` (from the local-gate spec) get a new
  possible `scam_reasons` category value: `user-reported`.
- New local-only table `reported_signatures` (or similar): stores the
  reported posting text signature + company/domain, for fuzzy-matching future
  postings. Not synced automatically — feeds `--export` only.

## Explicitly out of scope for this spec

- Any live network sync, hosting, or accounts.
- Automated PR submission.
- Any personal/identifying information about the individual scammer.

## Testing

- Local report action: verdict override + signature stored, unit-testable
  the same way as the existing gate tests.
- Fuzzy-match of a new posting against a previously reported signature:
  positive (near-identical repost) and negative (unrelated posting) cases.
- Community-file fetch: cached-copy fallback on network failure, never
  blocking the pipeline.
- `--export` output shape: matches what the community file expects, byte for
  byte on a fixed fixture.
