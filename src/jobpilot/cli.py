"""JobPilot CLI — the main entry point."""

from __future__ import annotations

import logging

import typer
from rich.console import Console
from rich.table import Table

from jobpilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="jobpilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from jobpilot.config import ensure_dirs, load_env
    from jobpilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]jobpilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """JobPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from jobpilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: list[str] | None = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(6, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment/score/tailor/cover stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
    fast: bool = typer.Option(False, "--fast", "-f", help="Speed mode: enables --stream and sets --workers 4."),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    if fast:
        stream = True
        workers = max(workers, 4)
        console.print(f"[green][fast] Fast mode:[/green] stream=True, workers={workers}")
    _bootstrap()

    from jobpilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from jobpilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def health() -> None:
    """Show whether the loops are actually running, plus queue depth and recent finds."""
    _bootstrap()

    from jobpilot.health import report

    report()


@app.command()
def watch(
    interval: int = typer.Option(300, "--interval", "-i", help="Seconds between polls."),
    min_score: int = typer.Option(7, "--min-score", help="Only alert on jobs at or above this fit score."),
    hours_old: int = typer.Option(2, "--hours-old", help="Discovery window. Small is the whole point."),
    workers: int = typer.Option(4, "--workers", "-w", help="Parallel threads for enrich/score/prep."),
    no_prep: bool = typer.Option(False, "--no-prep", help="Alert only; skip resume/cover-letter prep."),
    once: bool = typer.Option(False, "--once", help="Run a single poll and exit (useful for testing)."),
    validation: str = typer.Option("lenient", "--validation", help="Validation strictness for prep."),
) -> None:
    """Fast lane: poll for freshly-posted jobs, alert immediately, prep, and hold.

    Runs alongside `jobpilot run`, not instead of it. The bulk pipeline works
    the backlog; this catches new postings within minutes. It never submits --
    it prepares a tailored resume and cover letter, then waits for you to run
    `jobpilot apply`.
    """
    _bootstrap()

    from jobpilot.config import check_tier
    check_tier(2, "fast-lane scoring")

    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    if interval < 60:
        console.print(
            "[yellow]Warning:[/yellow] intervals under 60s will get you rate-limited "
            "or IP-blocked by the job boards, which makes you slower, not faster."
        )

    from jobpilot.fastlane import watch as run_watch

    try:
        run_watch(
            interval=interval,
            min_score=min_score,
            hours_old=hours_old,
            workers=workers,
            prep=not no_prep,
            once=once,
            validation_mode=validation,
        )
    except KeyboardInterrupt:
        console.print("\n[dim]Fast lane stopped.[/dim]")


@app.command()
def notify_test(
    message: str = typer.Option("Fast lane is wired up", "--message", "-m", help="Toast body."),
) -> None:
    """Send a test desktop notification, to confirm toasts actually appear."""
    from jobpilot.notify import notify

    ok = notify("JobPilot", message, "notification test")
    if ok:
        console.print("[green]Notification sent.[/green] If you did not see it, check "
                      "Windows Settings > System > Notifications, and that Focus Assist is off.")
    else:
        console.print("[yellow]No desktop backend accepted it[/yellow] -- the message was "
                      "logged instead. On Windows this usually means PowerShell could not "
                      "be found on PATH.")
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: int | None = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(6, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("haiku", "--model", "-m", help="Claude model name (engine=claude only)."),
    engine: str = typer.Option(
        "claude", "--engine",
        help=(
            "Apply engine. 'claude': spawns Claude Code CLI per job (costs API usage). "
            "'local': drives the same browser via whatever LLM is configured in .env "
            "(GEMINI_API_KEY/OPENAI_API_KEY/LLM_URL) -- no Claude Code usage, requires "
            "that LLM to support OpenAI-style tool calling."
        ),
    ),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: str | None = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: str | None = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: str | None = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: str | None = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
    poll_interval: int = typer.Option(60, "--poll-interval", help="Seconds between DB polls when queue is empty (lower = faster continuous mode)."),
    fast: bool = typer.Option(False, "--fast", "-f", help="Speed mode: sets workers=4, poll-interval=5, headless, engine=local. Combine with --continuous for max throughput."),
) -> None:
    """Launch auto-apply to submit job applications."""
    # --fast mode: auto-configure speed settings
    if fast:
        workers = max(workers, 4)
        poll_interval = min(poll_interval, 5)
        headless = True
        if engine == "claude":
            engine = "local"
            console.print("[yellow]--fast[/yellow] switched engine from claude -> local")
        console.print(f"[green][fast] Fast mode:[/green] workers={workers}, poll={poll_interval}s, headless, engine={engine}")
    _bootstrap()

    from jobpilot.config import PROFILE_PATH as _profile_path
    from jobpilot.config import check_tier
    from jobpilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from jobpilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from jobpilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from jobpilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    if engine not in ("claude", "local"):
        console.print(f"[red]Invalid --engine:[/red] '{engine}'. Choose 'claude' or 'local'.")
        raise typer.Exit(code=1)

    # Check 1: browser + agent brain available
    if engine == "local":
        import os

        from jobpilot.config import get_chrome_path

        missing = []
        if not any(os.environ.get(k) for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL")):
            missing.append("LLM API key/URL -- set GEMINI_API_KEY, OPENAI_API_KEY, or LLM_URL")
        try:
            get_chrome_path()
        except FileNotFoundError:
            missing.append("Chrome/Chromium -- install or set CHROME_PATH")
        from jobpilot.apply.local_agent import _find_node_dir
        if not _find_node_dir():
            missing.append("Node.js (npx) -- install from nodejs.org")
        if missing:
            console.print("[red]'auto-apply (local engine)' is missing:[/red]")
            for m in missing:
                console.print(f"  - {m}")
            raise typer.Exit(code=1)
    else:
        check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]jobpilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]jobpilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from jobpilot.apply.launcher import gen_prompt
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print("\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from jobpilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Engine:   {engine}")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    if engine == "claude":
        console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
        engine=engine,
        poll_interval=poll_interval,
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from jobpilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]JobPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from jobpilot.view import open_dashboard

    open_dashboard()


@app.command()
def web(
    port: int = typer.Option(8765, "--port", "-p", help="Port to serve the web UI on."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't auto-open a browser tab."),
) -> None:
    """Launch the JobPilot management web UI (view jobs, manage config, control the agent loop)."""
    _bootstrap()

    from jobpilot.webui import run

    console.print(f"\n[bold blue]JobPilot Control[/bold blue] -- http://127.0.0.1:{port}\n")
    run(port=port, open_browser=not no_browser)


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil

    from jobpilot.config import (
        PROFILE_PATH,
        RESUME_PATH,
        RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH,
        get_chrome_path,
        load_env,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'jobpilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found -- plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'jobpilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config -- run 'jobpilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    has_openrouter = bool(os.environ.get("OPENROUTER_API_KEY"))
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_openrouter:
        model = os.environ.get("LLM_MODEL", "google/gemini-2.5-flash-lite")
        results.append(("LLM API key", ok_mark, f"OpenRouter ({model})"))
    elif has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY in ~/.jobpilot/.env (run 'jobpilot init')"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    from jobpilot.config import find_claude_cli
    claude_bin = find_claude_cli()
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Render results ---
    console.print()
    console.print("[bold]JobPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from jobpilot.config import TIER_LABELS, get_tier
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} -- {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  -> Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  -> Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  -> Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()



# ---------------------------------------------------------------------------
# New capability commands (outcomes feedback loop, gates, dimensional score,
# interview prep, upskill) - added in the JobPilot fork.
# ---------------------------------------------------------------------------

@app.command("outcome")
def outcome_cmd(
    url: str | None = typer.Argument(None, help="Job URL to record an outcome for."),
    status: str | None = typer.Option(None, "--status", "-s", help="Outcome status: applied, waiting, interview, offer, accepted, rejected, no response, offer declined, closed."),
    note: str | None = typer.Option(None, "--note", "-n", help="Free-text note / reason."),
    source: str = typer.Option("manual", "--source", help="Where the signal came from (manual, gmail, notion)."),
    list_all: bool = typer.Option(False, "--list", "-l", help="Show the outcomes summary (counts)."),
    recal: bool = typer.Option(False, "--recalibrate", "-r", help="Show score->outcome recalibration lessons."),
    promote: str | None = typer.Option(None, "--promote", help="[url=signal] promote a drafted app to applied on an ack signal."),
) -> None:
    """Record and review application outcomes; recalibrate scoring from real results."""
    from jobpilot.database import get_connection
    from jobpilot.outcomes import (
        get_outcome,
        init_outcomes,
        outcomes_summary,
        promote_draft,
        recalibrate,
        record_outcome,
    )
    _bootstrap()
    conn = get_connection()
    init_outcomes(conn)

    if promote:
        if "=" in promote:
            u, sig = promote.split("=", 1)
        else:
            u, sig = promote, "acknowledgement"
        ok = promote_draft(conn, u, sig)
        console.print(f"[{'green' if ok else 'yellow'}]promote_draft: {u} -> {'applied' if ok else 'no draft to promote'}[/]")
        return

    if list_all:
        summary = outcomes_summary(conn)
        table = Table(title="Outcome Summary")
        table.add_column("Outcome")
        table.add_column("Count")
        for k, v in summary.items():
            table.add_row(k, str(v))
        console.print(table)
        return

    if recal:
        for lesson in recalibrate(conn):
            console.print(f"[bold]{lesson.get('band','')}[/bold] {lesson.get('lesson','')}")
        return

    if url:
        from jobpilot.database import get_connection as _gc  # noqa: F401
        if status:
            record_outcome(conn, url, status=status, source=source, notes=note,
                           status_date=None)
            console.print(f"[green]Recorded outcome {status!r} for {url}[/]")
        else:
            o = get_outcome(conn, url)
            if o:
                console.print(o)
            else:
                console.print(f"[yellow]No outcome recorded for {url}[/]")
        return

    console.print("[dim]Provide --url/--status, or use --list / --recalibrate.[/dim]")


@app.command("gate")
def gate_cmd(
    text: str = typer.Argument(..., help="Job posting text to gate before scoring."),
    check: str = typer.Option("all", "--check", "-c", help="eligibility | language | all"),
) -> None:
    """Run pre-score hard gates (eligibility + language) on a posting."""
    from jobpilot.config import load_profile
    from jobpilot.gating import evaluate_eligibility, evaluate_language

    try:
        profile = load_profile()
    except Exception:
        console.print("[red]No profile found. Run 'jobpilot init' first.[/]")
        raise typer.Exit(1)

    work_auth = profile.get("work_authorization", {})
    langs = profile.get("skills_boundary", {}).get("languages", [])

    if check in ("all", "eligibility"):
        v = evaluate_eligibility(text, work_auth)
        color = "green" if v["verdict"] in ("PASS", "PROCEED") else ("yellow" if v["verdict"]=="UNVERIFIED" else "red")
        console.print(f"[bold]{color}]Eligibility: {v['verdict']}[/] {v.get('reason','')}")
        if v.get("quoted"):
            console.print(f"  [dim]quoted: {v['quoted']}[/dim]")

    if check in ("all", "language"):
        v = evaluate_language(text, langs)
        color = "green" if v["verdict"]=="PASS" else ("yellow" if v["verdict"]=="FLAG" else "red")
        console.print(f"[bold]{color}]Language: {v['verdict']}[/] {v.get('reason','')}")
        for d in v.get("language_details", []):
            console.print(f"  [dim]{d}[/dim]")


@app.command("score-dims")
def score_dims_cmd(
    url: str | None = typer.Option(None, "--url", help="Job URL (reads full_description/short from DB)."),
    text: str | None = typer.Option(None, "--text", help="Raw job text directly."),
) -> None:
    """Explainable, dimensioned fit scoring (5 dims, 0-100)."""
    from jobpilot.config import load_profile
    from jobpilot.scoring.dimensions import score_dimensions

    if not text and not url:
        console.print("Provide --url or --text.")
        raise typer.Exit(1)
    if not text:
        from jobpilot.database import get_connection
        conn = get_connection()
        row = conn.execute("SELECT full_description, title, location, description FROM jobs WHERE url=?",
                           (url,)).fetchone()
        if not row:
            console.print(f"[red]No job found for {url}[/]")
            raise typer.Exit(1)
        text = row[0] or row[3] or ""
        job = {"title": row[1], "location": row[2], "full_description": text, "description": text}
    else:
        job = {"full_description": text, "description": text}

    try:
        profile = load_profile()
    except Exception:
        console.print("[red]No profile found. Run 'jobpilot init' first.[/]")
        raise typer.Exit(1)

    res = score_dimensions(job, profile)
    if not res.get("computed", True):
        console.print("[red]Deal-breakers veto this posting:[/]")
        for db in res.get("deal_breakers") or res.get("dealbreakers") or []:
            console.print(f"  - [red]{db}[/]")
        return
    table = Table(title=f"Dimensioned Fit Score (overall {res.get('overall','-')}/100)")
    table.add_column("Dimension")
    table.add_column("Score")
    table.add_column("Rationale")
    for dim, info in res.get("dimensions", {}).items():
        table.add_row(dim, str(info.get("score")), info.get("rationale", "")[:120])
    console.print(table)
    for w in res.get("warnings", []):
        console.print(f"[yellow]warn: {w}[/]")
    for g in res.get("gaps", []):
        console.print(f"[dim]gap: {g}[/dim]")


@app.command("interview")
def interview_cmd(
    company: str = typer.Option(..., "--company", help="Company name."),
    posting_text: str | None = typer.Option(None, "--posting", help="Posting text."),
) -> None:
    """Build an interview prep pack (questions, STAR bridge, company brief)."""
    from jobpilot.config import load_profile
    from jobpilot.interview import build_prep_pack, company_briefing

    try:
        profile = load_profile()
    except Exception:
        console.print("[red]No profile found. Run 'jobpilot init' first.[/]")
        raise typer.Exit(1)

    archive = {"company": company, "job_title": profile.get("experience", {}).get("target_role", ""),
               "posting_text": posting_text or "", "submitted_cv": "", "submitted_cover": "",
               "feedback": [], "round_idx": 1}
    pack = build_prep_pack(archive, profile)
    brief = company_briefing(company, external_facts=None)
    console.print(f"[bold]{company} prep pack[/]")
    console.print(f"  Brief used/verify: {brief.get('used', 'no external facts')}")
    for q in pack.get("likely_questions", []):
        console.print(f"  Q: {q.get('q','')}")
        console.print(f"     [dim]{q.get('bridge','')}[/dim]")
    for g in pack.get("gaps", []):
        console.print(f"[yellow]  gap: {g.get('topic','')} -> {g.get('honest_bridge','')}[/]")


@app.command("upskill")
def upskill_cmd(
    text: str | None = typer.Option(None, "--text", help="One posting text."),
) -> None:
    """Analyze skill gaps vs a posting and produce a learning plan."""
    from jobpilot.config import load_profile
    from jobpilot.upskill import gap_analysis, learning_plan

    try:
        profile = load_profile()
    except Exception:
        console.print("[red]No profile found. Run 'jobpilot init' first.[/]")
        raise typer.Exit(1)

    postings = [] if not text else [{"title": "target", "full_description": text, "description": text}]
    gaps = gap_analysis(profile, postings)
    plan = learning_plan(gaps.get("gaps", []))
    console.print("[bold]Skill gap heatmap[/]")
    for g in gaps.get("heatmap", []):
        console.print(f"  [yellow]{g}[/]")
    console.print("[bold]Learning plan[/]")
    for step in plan:
        console.print(f"  - {step}")


@app.command("report")
def report_cmd(
    url_or_action: str | None = typer.Argument(
        None,
        metavar="[URL | list | export]",
        help="Job URL to report, or action 'list' / 'export'.",
    ),
    note: str | None = typer.Option(
        None,
        "--note",
        "-n",
        help="Free-text note explaining the scam pattern / red flag.",
    ),
    snippet: str | None = typer.Option(
        None,
        "--snippet",
        "-s",
        help="Specific text snippet / phrase from the posting to use as signature.",
    ),
    export: bool = typer.Option(
        False,
        "--export",
        "-e",
        help="Export local scam reports as paste-ready YAML.",
    ),
    list_reports: bool = typer.Option(
        False,
        "--list",
        "-l",
        help="List all reported scam signatures / jobs in the database.",
    ),
    import_file: str | None = typer.Option(
        None,
        "--import",
        "-i",
        help="Import community scam reports from a local YAML file.",
    ),
) -> None:
    """Report scam job postings, manage signatures, and export community reports."""
    _bootstrap()
    from jobpilot.database import get_connection
    from jobpilot.scam_report import (
        export_reports_yaml,
        fetch_community_reports,
        get_active_signatures,
        init_reported_signatures,
        record_report,
    )

    conn = get_connection()
    init_reported_signatures(conn)

    # 1. Export mode: jobpilot report --export or jobpilot report export
    if export or url_or_action == "export":
        yaml_out = export_reports_yaml(conn)
        console.print(yaml_out, highlight=False)
        return

    # 2. List mode: jobpilot report --list or jobpilot report list
    if list_reports or url_or_action == "list":
        sigs = get_active_signatures(conn)
        if not sigs:
            console.print("[yellow]No reported signatures found in database.[/yellow]")
            return
        table = Table(title="Reported Scam Signatures", show_header=True, header_style="bold red")
        table.add_column("ID", style="dim", max_width=12)
        table.add_column("Source", style="cyan")
        table.add_column("Pattern Type", style="yellow")
        table.add_column("Company / Domain", style="green")
        table.add_column("Signature Excerpt")
        for s in sigs:
            comp = s.get("company") or s.get("domain") or "-"
            sig_text = (s.get("signature_text") or "")[:80]
            table.add_row(
                (s.get("id") or "")[:8],
                s.get("source") or "local_user",
                s.get("pattern_type") or "user-reported",
                comp,
                sig_text,
            )
        console.print(table)
        return

    # 3. Import community YAML file: jobpilot report --import <path>
    if import_file:
        imported = fetch_community_reports(import_file, conn=conn, ingest=True)
        console.print(f"[green]Imported {len(imported)} community report(s) into database.[/green]")
        return

    # 4. Report a specific job: jobpilot report <url> [--note ...]
    if url_or_action:
        url = url_or_action
        result = record_report(
            job=url,
            note=note or "",
            conn=conn,
            snippet=snippet,
        )
        console.print(f"[bold red]Blocked and reported job:[/] {url}")
        if result.get("note"):
            console.print(f"  [dim]Note:[/] {result['note']}")
        if result.get("signatures"):
            console.print("  [bold]Extracted signature(s):[/]")
            for sig in result["signatures"]:
                console.print(f"    - {sig}")
        return

    console.print(
        "[dim]Usage: jobpilot report <URL> [--note <text>] | "
        "jobpilot report --export | jobpilot report --list[/dim]"
    )


if __name__ == "__main__":
    app()
