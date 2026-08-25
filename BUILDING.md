# Building and Publishing JobPilot

This page documents how to build the `jobpilot` package from source, verify
it, and publish it to PyPI. JobPilot is distributed on PyPI as
**`job-pilot-ai`** (the import package and CLI stay `jobpilot`).

---

## 1. Prerequisites

- **Python 3.11+** (3.11, 3.12, or 3.13 are tested in CI)
- **git**
- For the full pipeline: Node.js 18+ (auto-apply), Chrome, Claude Code CLI,
  and an LLM API key — see the main [README.md](README.md#requirements).

---

## 2. Build from source

```bash
# Clone
git clone https://github.com/AxeyShane/JobPilot.git
cd JobPilot

# (Optional) create a virtualenv
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# Install build tooling
pip install build twine

# Build the wheel + sdist
python -m build

# Artifacts land in dist/
ls dist/
#   job_pilot_ai-0.4.0-py3-none-any.whl
#   job_pilot_ai-0.4.0.tar.gz
```

> The wheel is platform-independent (`py3-none-any`) — JobPilot is pure
> Python, so one wheel serves Windows, macOS, and Linux.

---

## 3. Install the built package locally (test before publishing)

```bash
# In a clean venv (do NOT use --no-index; deps come from PyPI)
pip install dist/job_pilot_ai-0.4.0-py3-none-any.whl

# Verify the CLI is installed and the new commands are registered
jobpilot --version        # -> jobpilot 0.4.0
jobpilot --help           # shows: init run gate score-dims outcome interview upskill ...

# Verify imports
python -c "import jobpilot; print(jobpilot.__version__)"
```

---

## 4. Run the test suite

The single source of truth for quality is the test suite (74 tests across the
new capability modules plus smoke tests).

```bash
pip install -e ".[dev]"     # dev extras: pytest, ruff

# Run everything
python -m pytest tests/ -v

# Or run individual modules (stdlib-only, no deps needed):
python3 tests/test_gating.py       # 15 tests
python3 tests/test_outcomes.py     # 17 tests
python3 tests/test_quality.py      # 24 checks
python3 tests/test_dimensions.py   # 15 functions / 33 asserts
python3 tests/test_interview.py    # 14 cases
python3 tests/test_upskill.py      # 11 cases

# Lint (new-capability modules)
ruff check src/jobpilot/gating.py src/jobpilot/outcomes.py \
         src/jobpilot/quality.py src/jobpilot/interview.py \
         src/jobpilot/upskill.py src/jobpilot/scoring/dimensions.py
```

CI runs the same tests on Python 3.11/3.12/3.13 for every push and PR.

---

## 5. Run a quick smoke test of the pipeline

```bash
# Point JobPilot at a sandbox data dir so it never touches your real profile
mkdir -p /tmp/jobpilot-smoke && echo '{"personal":{},"work_authorization":{},"skills_boundary":{}}' > /tmp/jobpilot-smoke/profile.json
export JOBPILOT_DIR=/tmp/jobpilot-smoke

# Hard gates
jobpilot gate "Must be a US citizen. Fluent English required." --check eligibility

# Dimensioned scoring (deal-breakers veto)
jobpilot score-dims --text "Senior Data Scientist, Python, salary 100k"

# Outcome loop
jobpilot outcome https://jobs.example/1 --status interview --note "round 1"
jobpilot outcome --list
```

---

## 6. Publish to PyPI

### Recommended: GitHub Actions (OIDC trusted publishing — no tokens stored)

The repo ships `.github/workflows/publish.yml`. Publishing happens
automatically when you push a `v*` tag:

```bash
git tag v0.4.0
git push origin v0.4.0
```

First time only, on GitHub:

1. Open **repo Settings → Environments** and create an environment named
   `pypi`.
2. Add the **PyPI publisher** as an environment rule:
   - Go to **PyPI → Account settings → Publishing → Add a new pending
     publisher**.
   - Publisher: `GitHub`, owner: `AxeyShane`, repo: `JobPilot`,
     workflow name: `publish.yml`, environment: `pypi`.
3. Future `v*` tags auto-build and auto-publish.

### Alternative: manual upload

```bash
pip install build twine
python -m build
twine check dist/*
# Upload to Test PyPI first
twine upload --repository testpypi dist/*
# Then to production
twine upload dist/*
```

You'll be prompted for your PyPI username/password (or use an API token).

---

## 7. Versioning

- Version lives in two places: `pyproject.toml` (`version = "0.4.0"`) and
  `src/jobpilot/__init__.py` (`__version__`). Bump both.
- The project follows [Semantic Versioning](https://semver.org).
- Update [CHANGELOG.md](CHANGELOG.md) under `[Unreleased]` → move to a dated
  section at release.

---

## 8. Repo layout (where things live)

```
src/jobpilot/            package root
  cli.py                 typer CLI (entry: jobpilot)
  gating.py              pre-score hard gates (eligibility, language)
  outcomes.py            closed feedback loop (outcomes table, recalibrate)
  quality.py             ATS/PDF checks, reviewer pass, untrusted-input sanitizer
  interview.py           interview prep pack (STAR bridge)
  upskill.py             skill-gap analysis + learning plan
  scoring/dimensions.py  5-dim explainable fit scoring
  config.py              paths, per-stage LLM routing, profile loading
tests/                   pytest + standalone-runnable tests
.github/workflows/       ci.yml (push/PR), publish.yml (tag -> PyPI)
```

## License

AGPL-3.0 — see [LICENSE](LICENSE). JobPilot is an open-source evolution of
[ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) and is not
affiliated with any product using the "Pilot" name.
