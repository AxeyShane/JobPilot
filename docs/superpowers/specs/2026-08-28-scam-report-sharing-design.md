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
  judgment call overrides whatever the automated heuristic/LLM gate said),
  stamps `scam_checked_at`, and appends an entry to `scam_reasons` using the
  shared shape defined in the detection spec:
  `{category: "user-reported", quoted: null, note: <optional free text>}`.
  Same list, same field names as automated entries — the dashboard badge and
  any export never need to know which produced a given entry.
- Also stores the posting's `full_description` at the time of the report in
  the new `reported_signatures` table (below) as a "known-bad" signature — not
  just the URL. Scammers commonly repost the same pitch under a new URL;
  matching only the original link would miss the repost.
- A `jobpilot report --export` CLI command dumps the user's local reports in
  the exact shape the community file (below) expects — output only, no
  network call, no automatic submission anywhere.

### Matching future postings against reported signatures

Deliberately conservative and stdlib-only, matching the rest of the gate's
regex-based, no-new-dependency approach (no embeddings, no fuzzy-matching
library):

	- **Primary signal — word-boundary / token-aware matching with a similarity
  threshold.** Lowercase and normalize whitespace on both the stored signature
  and the new posting, then match against the *tokens* of the signature, not
  raw substrings.
  - An exact phrase match is checked first with regex word boundaries
    (`\b`), so a stored phrase never false-positives against a longer word
    that merely contains it ("wireless" does not match "wire"; "coffee" does
    not match "fee").
  - Otherwise a token-level sliding-window comparison scores overlap: short
    signatures (< 3 tokens) require an exact token-sequence match on word
    boundaries; longer signatures use token overlap / sequence similarity
    above a threshold (default 0.80). Plain substring containment is
    deliberately NOT a signal — this repo hit a real substring-false-positive
    bug in `validator.py`'s fabrication check, and the same trap is avoided
    here.
  - NET: stricter against false positives than character-substring matching,
    while still catching near-identical reposts of the same pitch.
- **Secondary signal — domain, corroborating only, never sufficient alone.**
  Aggregator-sourced postings (the source this feature targets most) often
  expose the aggregator's own apply-flow URL rather than the true poster's
  domain, so a domain match by itself is weak evidence. Domain match only
  raises confidence when paired with a text-window match; it never triggers a
  block on its own.

### The shared file

- New file: `src/jobpilot/config/community_scam_reports.yaml`, committed to
  the repo — same pattern as the existing `sites.yaml`/`employers.yaml`.
- Per entry: company/entity name as claimed in the posting, any known
  domain(s), and the quoted scam-pitch excerpt (the same text used for the
  token-aware match above). Explicitly excluded: the individual
  "recruiter's" name, personal profile links, or any DM content beyond the
  pitch text itself — same reasoning as keeping the design-doc commit message
  generic.
- JobPilot fetches the latest copy from the repo's raw GitHub URL once per
  pipeline run (not per job — avoids hammering GitHub under worker
  concurrency), caches it locally with that cadence as the refresh TTL. A
  fetch failure logs a warning and falls back to the last cached copy — this
  is a supplementary signal on top of the local heuristic/LLM gate, not the
  primary defense, so it doesn't need that gate's fail-closed strictness (a
  stale or missing community list should never block the pipeline).

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
  possible `scam_reasons` category value: `user-reported`, using that spec's
  shared entry shape (`category`, `quoted`, `note`).
- New local-only table `reported_signatures`: stores the reported posting text
  signature + company/domain, for the token-aware matching above.
  Not synced automatically — feeds `--export` only.

## Explicitly out of scope for this spec

- Any live network sync, hosting, or accounts.
- Automated PR submission.
- Any personal/identifying information about the individual scammer.
- Embeddings- or edit-distance-based matching beyond the token-aware
  similarity approach (a possible future upgrade, not part of this design).

## Testing

- Local report action: verdict override + `scam_checked_at` stamp + signature
  stored, unit-testable the same way as the existing gate tests.
- Token-aware match against a reported signature: positive
  (near-identical repost, same long window present) and negative (unrelated
  posting, generic phrase overlap only, below the length threshold) cases —
  the negative case specifically proving generic-phrasing overlap alone does
  not trigger a block.
- Domain-only match (no text-window match) confirmed to never block by itself.
- Community-file fetch: cached-copy fallback on network failure, never
  blocking the pipeline; confirms the fetch happens once per pipeline run, not
  once per job.
- `--export` output shape: matches what the community file expects, byte for
  byte on a fixed fixture.
