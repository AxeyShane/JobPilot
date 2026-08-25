"""Fast lane: hear about a fresh, high-fit job within minutes.

Why this exists
---------------
The bulk pipeline (`jobpilot run`) crawls the whole catalog, enriches
everything pending, then scores/tailors/covers in bulk. That is the right
shape for working a backlog and the wrong shape for catching a posting while
it is still new: a job that went up ten minutes ago queues behind several
hundred older ones, and agent_loop's watchdog usually kills the run before it
gets there. Measured end-to-end latency on real applications was 1-7 days.

The fast lane inverts every one of those choices:

  * narrow discovery window (hours_old=2, not 72)
  * JobSpy only -- skips the Workday and smart-extract crawlers, which are
    where the bulk cycle spends most of its wall clock
  * set-difference against the DB, so only genuinely new URLs are touched
  * newest-first ordering at every stage
  * notify BEFORE prepping, because knowing at minute 3 beats knowing at
    minute 12 with a resume attached
  * never submits -- it prepares and holds (see docstring on run_cycle)

It is meant to run alongside the bulk pipeline, not replace it. The bulk
pipeline keeps grinding the backlog; the fast lane makes sure that grind never
stands between you and something posted this morning.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from rich.console import Console

from jobpilot import config
from jobpilot.database import get_connection

log = logging.getLogger(__name__)
console = Console()

# JobSpy asks each board for this many results per query in fast-lane mode.
# The bulk default (100) is for backlog sweeps; with a 2-hour window there is
# rarely more than a handful of genuinely new postings per query, and a small
# page keeps a poll under a minute.
FAST_RESULTS_PER_SITE = 25

# Workday employer portals polled per cycle. The aggregators (Indeed, LinkedIn)
# host their own apply flow behind bot detection, so ~78% of what they surface
# can never be auto-applied to -- the ATS-native postings that CAN be are on
# employer portals. Crawling all 48 every 5 minutes would blow the cycle time,
# so rotate a slice: 8 per cycle covers the full registry every 6 cycles
# (~30 min at the default interval) while keeping a poll under two minutes.
FAST_WORKDAY_SLICE = 8


def _all_urls(conn) -> set[str]:
    """Every job URL currently known. Used as the before/after snapshot."""
    return {row[0] for row in conn.execute("SELECT url FROM jobs")}


def _fast_search_config(hours_old: int, results_per_site: int) -> dict:
    """The user's real search config, narrowed to a recency window."""
    cfg = dict(config.load_search_config() or {})
    if not cfg:
        return {}
    defaults = dict(cfg.get("defaults") or {})
    defaults["hours_old"] = hours_old
    defaults["results_per_site"] = results_per_site
    cfg["defaults"] = defaults
    return cfg


def _record_fresh(jobs: list[dict], stage: str = "found") -> None:
    """Append fresh finds to a JSONL file.

    A record independent of whether a desktop notification was seen, and --
    since applications are submitted by hand -- the place to find the prepared
    resume and cover letter for each match. Written twice per job: once the
    moment it is found, again once prep produces the documents.
    """
    path = config.APP_DIR / "fresh_jobs.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for j in jobs:
                fh.write(json.dumps({
                    "seen_at": now,
                    "stage": stage,
                    "url": j.get("url"),
                    "title": j.get("title"),
                    "site": j.get("site"),
                    "location": j.get("location"),
                    "fit_score": j.get("fit_score"),
                    "application_url": j.get("application_url"),
                    "resume": j.get("tailored_resume_path"),
                    "cover_letter": j.get("cover_letter_path"),
                }, ensure_ascii=False) + "\n")
    except OSError:
        log.debug("Could not write fresh_jobs.jsonl", exc_info=True)


def _workday_slice(cycle: int, size: int) -> dict:
    """A rotating slice of the Workday employer registry for this cycle."""
    try:
        from jobpilot.discovery.workday import load_employers
        employers = load_employers() or {}
    except Exception:  # noqa: BLE001
        log.debug("Could not load Workday employers", exc_info=True)
        return {}
    if not employers or size <= 0:
        return {}
    names = sorted(employers)
    start = (cycle * size) % len(names)
    picked = [names[(start + i) % len(names)] for i in range(min(size, len(names)))]
    return {n: employers[n] for n in picked}


def run_cycle(min_score: int = 7, hours_old: int = 2, workers: int = 4,
              prep: bool = True, validation_mode: str = "lenient",
              results_per_site: int = FAST_RESULTS_PER_SITE,
              cycle: int = 0, workday_slice: int = FAST_WORKDAY_SLICE) -> dict:
    """One fast-lane poll: discover -> enrich -> score -> notify -> prep -> hold.

    Deliberately stops before submitting. The last step prepares a tailored
    resume and cover letter so the application is ready to fire, then waits
    for you: `jobpilot apply` (or the dashboard) is what actually submits.

    Returns a stats dict describing what this poll found.
    """
    from jobpilot import notify as notifier
    from jobpilot.discovery.jobspy import run_discovery
    from jobpilot.enrichment.detail import run_enrichment
    from jobpilot.scoring.scorer import run_scoring

    t0 = time.time()
    stats = {"discovered": 0, "scored": 0, "matched": 0, "prepped": 0, "elapsed": 0.0}

    conn = get_connection()
    before = _all_urls(conn)

    cfg = _fast_search_config(hours_old, results_per_site)
    if not cfg:
        console.print("[red]No search configuration.[/red] Run `jobpilot init` first.")
        return stats

    # 1. Discover -- JobSpy only. Workday and smart-extract are the slow crawlers.
    console.print(f"  [cyan]polling boards[/cyan] (last {hours_old}h)...")
    try:
        run_discovery(cfg)
    except Exception as e:  # noqa: BLE001
        log.error("Fast-lane discovery failed: %s", e)
        console.print(f"  [red]discovery error:[/red] {e}")
        return stats

    # Rotating slice of Workday employer portals. These are where the
    # automatable postings live -- an aggregator listing usually resolves to a
    # bot-walled apply page, an employer portal resolves to a real form.
    slice_ = _workday_slice(cycle, workday_slice)
    if slice_:
        console.print(f"  [cyan]workday portals[/cyan] ({len(slice_)}: "
                      f"{', '.join(sorted(slice_)[:4])}{'...' if len(slice_) > 4 else ''})")
        try:
            from jobpilot.discovery.workday import run_workday_discovery
            run_workday_discovery(employers=slice_, workers=workers)
        except Exception as e:  # noqa: BLE001
            # A portal being down must not cost the whole poll -- JobSpy
            # results are already in the DB by this point.
            log.error("Fast-lane Workday sweep failed: %s", e)
            console.print(f"  [yellow]workday sweep error:[/yellow] {e}")

    new_urls = sorted(_all_urls(conn) - before)
    stats["discovered"] = len(new_urls)
    if not new_urls:
        stats["elapsed"] = time.time() - t0
        console.print(f"  nothing new ({stats['elapsed']:.0f}s)")
        return stats

    console.print(f"  [green]{len(new_urls)} new[/green] -> enriching")

    # 2. Enrich only the new URLs.
    try:
        run_enrichment(limit=len(new_urls), workers=workers, only_urls=new_urls)
    except Exception as e:  # noqa: BLE001
        log.error("Fast-lane enrichment failed: %s", e)

    # 3. Score only the new URLs, freshest first.
    try:
        result = run_scoring(limit=len(new_urls), workers=workers,
                             urls=new_urls, newest_first=True)
        stats["scored"] = result.get("scored", 0)
    except Exception as e:  # noqa: BLE001
        log.error("Fast-lane scoring failed: %s", e)

    # 4. Which of them cleared the bar?
    placeholders = ",".join("?" for _ in new_urls)
    matched = conn.execute(
        f"SELECT url, title, site, location, fit_score, application_url "
        f"FROM jobs WHERE url IN ({placeholders}) AND fit_score >= ? "
        f"ORDER BY fit_score DESC, discovered_at DESC",
        (*new_urls, min_score),
    ).fetchall()
    matched = [dict(zip(("url", "title", "site", "location", "fit_score",
                         "application_url"), row)) for row in matched]
    stats["matched"] = len(matched)

    if not matched:
        stats["elapsed"] = time.time() - t0
        console.print(f"  no matches at score >= {min_score} ({stats['elapsed']:.0f}s)")
        return stats

    # 5. Notify FIRST. Prep can take minutes; the alert should not wait on it.
    _record_fresh(matched)
    notifier.notify_batch(matched, prepped=False)
    for j in matched:
        console.print(f"  [bold green]{j['fit_score']}/10[/bold green] {j['title'][:60]} "
                      f"[dim]{j['site']}[/dim]")

    # 6. Prep and HOLD -- tailored resume + cover letter, no submission.
    if prep:
        urls = [j["url"] for j in matched]
        console.print(f"  [cyan]prepping {len(urls)}[/cyan] (resume + cover letter, not submitting)")
        try:
            from jobpilot.scoring.cover_letter import run_cover_letters
            from jobpilot.scoring.tailor import run_tailoring

            run_tailoring(min_score=min_score, limit=len(urls), workers=workers,
                          validation_mode=validation_mode, urls=urls, newest_first=True)
            run_cover_letters(min_score=min_score, limit=len(urls), workers=workers,
                              validation_mode=validation_mode, urls=urls, newest_first=True)
        except Exception as e:  # noqa: BLE001
            log.error("Fast-lane prep failed: %s", e)

        cols = ("url", "title", "site", "location", "fit_score",
                "application_url", "tailored_resume_path", "cover_letter_path")
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM jobs "
            f"WHERE url IN ({','.join('?' for _ in urls)}) "
            f"AND tailored_resume_path IS NOT NULL AND cover_letter_path IS NOT NULL "
            f"ORDER BY fit_score DESC",
            urls,
        ).fetchall()
        ready = [dict(zip(cols, r)) for r in rows]
        stats["prepped"] = len(ready)
        if ready:
            _record_fresh(ready, stage="prepped")
            for j in ready:
                console.print(f"    [dim]resume:[/dim] {j['tailored_resume_path']}")
                console.print(f"    [dim]cover :[/dim] {j['cover_letter_path']}")
            top = ready[0]
            notifier.notify(
                f"{len(ready)} application(s) ready",
                f"{(top.get('title') or '?')[:70]} -- resume + cover letter prepared",
                "Click to open the job page",
                launch=(top.get("application_url")
                        if isinstance(top.get("application_url"), str) and top["application_url"].startswith("http")
                        else None) or top.get("url") or "",
            )

    stats["elapsed"] = time.time() - t0
    return stats


def watch(interval: int = 300, min_score: int = 7, hours_old: int = 2,
          workers: int = 4, prep: bool = True, once: bool = False,
          validation_mode: str = "lenient") -> None:
    """Poll forever on `interval` seconds. Ctrl-C to stop."""
    console.print(
        f"[bold]JobPilot fast lane[/bold] - every {interval}s, "
        f"window {hours_old}h, min score {min_score}, "
        f"prep={'on' if prep else 'off'}, submit=never"
    )
    cycle = 0
    while True:
        cycle += 1
        ts = datetime.now().strftime("%H:%M:%S")
        console.print(f"\n[dim]{ts}[/dim] [bold]cycle {cycle}[/bold]")
        try:
            s = run_cycle(min_score=min_score, hours_old=hours_old, workers=workers,
                          prep=prep, validation_mode=validation_mode, cycle=cycle - 1)
            console.print(
                f"  [dim]{s['discovered']} new | {s['matched']} matched | "
                f"{s['prepped']} prepped | {s['elapsed']:.0f}s[/dim]"
            )
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            # A poll must never kill the watcher -- a transient board timeout
            # at 3am should cost one cycle, not the whole night.
            log.error("Fast-lane cycle failed: %s", e, exc_info=True)
            console.print(f"  [red]cycle error:[/red] {e}")

        if once:
            return
        time.sleep(interval)
