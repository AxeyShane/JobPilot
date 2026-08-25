"""JobPilot management web UI.

A small local Flask app for viewing pipeline state and managing config
(LLM provider, profile, searches) and the autonomous agent_loop without
touching files by hand.

Run with: jobpilot web
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml
from flask import Flask, jsonify, request

from jobpilot import config as cfg
from jobpilot.database import get_connection, get_stats, init_db

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_SCRIPT = REPO_ROOT / "scripts" / "agent_loop.ps1"
VENV_JOBPILOT = REPO_ROOT / ".venv" / "Scripts" / "jobpilot.exe"
LIVE_FLAG_PATH = cfg.APP_DIR / "live.flag"
ENGINE_FLAG_PATH = cfg.APP_DIR / "engine.flag"
PID_PATH = cfg.APP_DIR / "agent_loop.pid"
LOOP_LOG_PATH = cfg.APP_DIR / "logs" / "agent_loop.log"

# Ad-hoc, user-triggered single-stage pipeline runs (separate from the
# scheduled agent_loop) -- lets the UI stop discovery specifically, or kick
# off just a score/tailor/cover pass, without touching the 4h loop.
PIPELINE_PID_PATH = cfg.APP_DIR / "pipeline_run.pid"
PIPELINE_LOG_PATH = cfg.APP_DIR / "logs" / "pipeline_run.log"
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# .env upsert helper (line-based, preserves comments/ordering)
# ---------------------------------------------------------------------------

def _read_env_lines() -> list[str]:
    if cfg.ENV_PATH.exists():
        return cfg.ENV_PATH.read_text(encoding="utf-8").splitlines()
    return []


def _upsert_env(updates: dict[str, str | None]) -> None:
    """Set/remove KEY=VALUE lines in ~/.jobpilot/.env. None deletes the key."""
    lines = _read_env_lines()
    seen = set()
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            seen.add(key)
            val = updates[key]
            if val is not None:
                out.append(f"{key}={val}")
            # val is None -> drop the line (delete key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in seen and val is not None:
            out.append(f"{key}={val}")
    cfg.ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def _load_searches_yaml() -> dict:
    if not cfg.SEARCH_CONFIG_PATH.exists():
        return {}
    return yaml.safe_load(cfg.SEARCH_CONFIG_PATH.read_text(encoding="utf-8")) or {}


# ---------------------------------------------------------------------------
# Agent loop process control
# ---------------------------------------------------------------------------

def _pid_alive(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None
    # Confirm it's actually alive (Windows: query via tasklist)
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        if str(pid) in out:
            return pid
    except Exception:
        pass
    return None


def _loop_pid() -> int | None:
    return _pid_alive(PID_PATH)


def _pipeline_pid() -> int | None:
    return _pid_alive(PIPELINE_PID_PATH)


def _is_live() -> bool:
    if not LIVE_FLAG_PATH.exists():
        return False
    return LIVE_FLAG_PATH.read_text(encoding="utf-8").strip() == "1"


def _engine() -> str:
    if not ENGINE_FLAG_PATH.exists():
        return "claude"
    val = ENGINE_FLAG_PATH.read_text(encoding="utf-8").strip()
    return val if val in ("claude", "local") else "claude"


def _any_jobpilot_child_alive() -> bool:
    """Is a real jobpilot.exe pipeline/apply-stage process currently
    running? (Distinct from the loop's own powershell.exe wrapper PID.)"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq jobpilot.exe"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return "jobpilot.exe" in out
    except Exception:
        return False


# How long the loop can go with no log activity and no active child before
# it's flagged stalled rather than just "between cycles." Generous on
# purpose: a single pipeline stage's own watchdog allows up to 180min, apply
# up to 120min -- this needs to clear the worst legitimate case (a stage that
# ran the full 180min before its own watchdog fired and logged the kill)
# plus buffer, or a healthy-but-slow run gets misreported as stalled.
_STALL_THRESHOLD_SECONDS = 4 * 3600


def _loop_status() -> dict:
    pid = _loop_pid()
    tail = ""
    stalled = False
    last_activity_seconds_ago: float | None = None
    if LOOP_LOG_PATH.exists():
        lines = LOOP_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-40:])
        import time
        last_activity_seconds_ago = time.time() - LOOP_LOG_PATH.stat().st_mtime
        last_line = lines[-1].strip() if lines else ""
        currently_sleeping = "sleeping" in last_line  # normal idle phase between cycles, not a hang
        if (
            pid is not None
            and not currently_sleeping
            and not _any_jobpilot_child_alive()
            and last_activity_seconds_ago > _STALL_THRESHOLD_SECONDS
        ):
            stalled = True
    return {
        "running": pid is not None,
        "pid": pid,
        "live": _is_live(),
        "engine": _engine(),
        "log_tail": tail,
        "stalled": stalled,
        "last_activity_seconds_ago": last_activity_seconds_ago,
        "fastlane": _fastlane_status(),
    }


def _fastlane_status() -> dict:
    """State of the fast lane (`scripts/fast_lane.ps1`).

    The dashboard previously knew only about agent_loop, so it would report
    "stopped" while the fast lane was polling happily every five minutes --
    exactly the kind of misleading state this dashboard exists to prevent.
    A pid file survives a kill, so cross-check it against real log activity.
    """
    import time

    pid_path = cfg.APP_DIR / "fast_lane.pid"
    log_path = cfg.APP_DIR / "logs" / "fast_lane.log"
    pid = _pid_alive(pid_path)

    age = None
    if log_path.exists():
        age = time.time() - log_path.stat().st_mtime

    if pid is None:
        state = "stopped"
    elif age is None:
        state = "starting"
    elif age > 15 * 60:      # a poll is every 5 min by default
        state = "stalled"
    else:
        state = "running"

    if age is None:
        last = "no log yet"
    elif age < 60:
        last = f"{int(age)}s ago"
    elif age < 3600:
        last = f"{int(age // 60)}m ago"
    else:
        last = f"{int(age // 3600)}h ago"

    return {"state": state, "pid": pid, "last": last}


def _loop_env() -> dict:
    """Environment for the spawned loop process.

    This process is launched through a chain (webui -> hidden PowerShell ->
    jobpilot.exe -> npx / claude), and PATH entries added to the
    interactive user shell (e.g. the npm global bin dir under %APPDATA%\\npm
    where npx.CMD lives, or %USERPROFILE%\\.local\\bin where claude.exe
    lives -- both observed missing here even though they resolve fine
    interactively, likely added via a shell profile script this chain never
    loads) don't reliably show up at the end of that chain. Make sure both
    are there explicitly so neither the local engine's npx dependency check
    nor the claude engine's Tier-3 check/spawn fail on a false "missing".
    """
    env = os.environ.copy()
    extra_dirs = [
        os.path.join(os.environ.get("APPDATA", ""), "npm"),
        os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin"),
    ]
    for d in extra_dirs:
        if d and os.path.isdir(d) and d not in env.get("PATH", ""):
            env["PATH"] = d + os.pathsep + env.get("PATH", "")
    return env


def _start_loop() -> None:
    cfg.ensure_dirs()
    if _loop_pid() is not None or _pipeline_pid() is not None:
        return
    # NOTE: deliberately no CREATE_NO_WINDOW here -- agent_loop.ps1 launches
    # child processes with -RedirectStandardOutput/-Error, which misbehaves
    # when the parent PowerShell process has no console at all. "-WindowStyle
    # Hidden" alone keeps a (hidden) console allocated and works reliably.
    proc = subprocess.Popen(
        ["powershell.exe", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
         "-File", str(AGENT_SCRIPT)],
        cwd=str(REPO_ROOT),
        env=_loop_env(),
        close_fds=True,
    )
    PID_PATH.write_text(str(proc.pid), encoding="utf-8")


def _stop_loop() -> None:
    pid = _loop_pid()
    if pid is None:
        return
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    PID_PATH.unlink(missing_ok=True)


def _set_live(live: bool) -> None:
    cfg.ensure_dirs()
    LIVE_FLAG_PATH.write_text("1" if live else "0", encoding="utf-8")


def _set_engine(engine: str) -> None:
    cfg.ensure_dirs()
    ENGINE_FLAG_PATH.write_text(engine if engine in ("claude", "local") else "claude", encoding="utf-8")


# ---------------------------------------------------------------------------
# Ad-hoc pipeline stage control (independent of the scheduled agent_loop)
# ---------------------------------------------------------------------------

def _pipeline_status() -> dict:
    pid = _pipeline_pid()
    tail = ""
    if PIPELINE_LOG_PATH.exists():
        lines = PIPELINE_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-40:])
    return {"running": pid is not None, "pid": pid, "log_tail": tail}


def _start_pipeline_run(stages: list[str], stream: bool = False, workers: int = 4) -> str | None:
    """Spawn a one-off `jobpilot run <stages>` in the background.

    Returns an error string if a run (loop or ad-hoc) is already active,
    else None on success. Kept separate from the scheduled loop so the UI
    can e.g. run just 'score' without touching the 4h cycle.
    """
    cfg.ensure_dirs()
    if _pipeline_pid() is not None:
        return "A pipeline run is already in progress."
    if _loop_pid() is not None:
        return "Stop the agent loop first -- it already runs the full pipeline on its own schedule."
    bad = [s for s in stages if s not in VALID_STAGES]
    if bad:
        return f"Invalid stage(s): {', '.join(bad)}"
    if not stages:
        return "No stages given."

    args = [str(VENV_JOBPILOT), "run", *stages, "--workers", str(workers), "--validation", "lenient"]
    if stream:
        args.append("--stream")

    header = f"\n=== pipeline run started: {' '.join(stages)}{' --stream' if stream else ''} ===\n"
    log_file = open(PIPELINE_LOG_PATH, "a", encoding="utf-8")
    log_file.write(header)
    log_file.flush()
    proc = subprocess.Popen(
        args, cwd=str(REPO_ROOT), env=_loop_env(),
        stdout=log_file, stderr=subprocess.STDOUT, close_fds=True,
    )
    log_file.close()  # child has its own inherited handle; safe to close ours
    PIPELINE_PID_PATH.write_text(str(proc.pid), encoding="utf-8")
    return None


def _stop_pipeline_run() -> None:
    pid = _pipeline_pid()
    if pid is None:
        return
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    PIPELINE_PID_PATH.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__)
    cfg.load_env()
    cfg.ensure_dirs()
    init_db()

    # -- pages --

    @app.get("/")
    def index():
        return PAGE_HTML

    # -- stats / jobs --

    @app.get("/api/stats")
    def api_stats():
        return jsonify(get_stats())

    @app.get("/api/jobs")
    def api_jobs():
        min_score = request.args.get("min_score", type=int)
        limit = request.args.get("limit", default=200, type=int)
        location = request.args.get("location", default="").strip()
        conn = get_connection()
        where = "1=1"
        params: list = []
        if min_score is not None:
            where += " AND (fit_score >= ? OR fit_score IS NULL)"
            params.append(min_score)
        if location:
            # Comma-separated OR match, e.g. "Dubai,UAE,Remote" -- same shape
            # as searches.yaml's location_accept list.
            terms = [t.strip() for t in location.split(",") if t.strip()]
            if terms:
                where += " AND (" + " OR ".join(["location LIKE ?"] * len(terms)) + ")"
                params.extend(f"%{t}%" for t in terms)
        rows = conn.execute(
            f"""SELECT url, title, site, location, fit_score, score_reasoning,
                       tailored_resume_path, cover_letter_path, applied_at,
                       apply_status, apply_error, application_url
                FROM jobs WHERE {where}
                ORDER BY fit_score DESC, discovered_at DESC LIMIT ?""",
            params + [limit],
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.post("/api/jobs/mark")
    def api_mark_job():
        data = request.get_json(force=True)
        url = data.get("url")
        status = data.get("status")
        if not url or status not in ("applied", "failed"):
            return jsonify({"error": "url and status(applied|failed) required"}), 400
        from jobpilot.apply.launcher import mark_job
        mark_job(url, status, reason=data.get("reason"))
        return jsonify({"ok": True})

    @app.get("/api/jobs/file")
    def api_job_file():
        from flask import send_file

        url = request.args.get("url", "")
        kind = request.args.get("kind", "")
        fmt = request.args.get("format", "txt")
        column = {"resume": "tailored_resume_path", "cover_letter": "cover_letter_path"}.get(kind)
        if not url or not column or fmt not in ("txt", "pdf", "docx"):
            return jsonify({"error": "url, kind(resume|cover_letter), format(txt|pdf|docx) required"}), 400
        conn = get_connection()
        row = conn.execute(f"SELECT {column} AS path FROM jobs WHERE url = ?", (url,)).fetchone()
        path = row["path"] if row else None
        if not path:
            return jsonify({"error": "no file for this job"}), 404
        resolved = Path(path).with_suffix(f".{fmt}").resolve()
        if cfg.APP_DIR.resolve() not in resolved.parents or not resolved.is_file():
            return jsonify({"error": "file not found"}), 404
        return send_file(resolved, as_attachment=False)

    # -- settings: LLM provider --

    @app.get("/api/settings/llm")
    def get_llm_settings():
        return jsonify({
            "provider": "local" if os.environ.get("LLM_URL") else (
                "gemini" if os.environ.get("GEMINI_API_KEY") else (
                    "openai" if os.environ.get("OPENAI_API_KEY") else "none"
                )
            ),
            "gemini_key_set": bool(os.environ.get("GEMINI_API_KEY")),
            "openai_key_set": bool(os.environ.get("OPENAI_API_KEY")),
            "llm_url": os.environ.get("LLM_URL", ""),
            "llm_model": os.environ.get("LLM_MODEL", ""),
            "llm_api_key_set": bool(os.environ.get("LLM_API_KEY")),
        })

    @app.post("/api/settings/llm")
    def set_llm_settings():
        data = request.get_json(force=True)
        provider = data.get("provider")
        updates: dict[str, str | None] = {}
        if provider == "local":
            updates["LLM_URL"] = data.get("llm_url") or "http://127.0.0.1:8080/v1"
            updates["LLM_MODEL"] = data.get("llm_model") or "local-model"
            if data.get("llm_api_key"):
                updates["LLM_API_KEY"] = data["llm_api_key"]
        elif provider == "gemini":
            updates["LLM_URL"] = None
            updates["LLM_MODEL"] = data.get("llm_model") or None
            if data.get("gemini_key"):
                updates["GEMINI_API_KEY"] = data["gemini_key"]
        elif provider == "openai":
            updates["LLM_URL"] = None
            updates["LLM_MODEL"] = data.get("llm_model") or None
            if data.get("openai_key"):
                updates["OPENAI_API_KEY"] = data["openai_key"]
        else:
            return jsonify({"error": "provider must be local|gemini|openai"}), 400
        _upsert_env(updates)
        cfg.load_env()
        return jsonify({"ok": True})

    # -- settings: profile / searches (raw edit) --

    @app.get("/api/settings/profile")
    def get_profile():
        if not cfg.PROFILE_PATH.exists():
            return jsonify({})
        return jsonify(json.loads(cfg.PROFILE_PATH.read_text(encoding="utf-8")))

    @app.post("/api/settings/profile")
    def set_profile():
        data = request.get_json(force=True)
        cfg.ensure_dirs()
        cfg.PROFILE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return jsonify({"ok": True})

    @app.get("/api/settings/searches")
    def get_searches():
        if cfg.SEARCH_CONFIG_PATH.exists():
            return jsonify({"yaml": cfg.SEARCH_CONFIG_PATH.read_text(encoding="utf-8")})
        return jsonify({"yaml": ""})

    @app.post("/api/settings/searches")
    def set_searches():
        data = request.get_json(force=True)
        cfg.ensure_dirs()
        cfg.SEARCH_CONFIG_PATH.write_text(data.get("yaml", ""), encoding="utf-8")
        return jsonify({"ok": True})

    @app.get("/api/settings/location")
    def get_location():
        parsed = _load_searches_yaml()
        return jsonify({
            "primary_location": parsed.get("defaults", {}).get("location", ""),
            "remote_enabled": any(l.get("remote") for l in parsed.get("locations", [])),
            "location_accept": parsed.get("location_accept", []),
        })

    @app.post("/api/settings/location")
    def set_location():
        data = request.get_json(force=True)
        primary = (data.get("primary_location") or "").strip()
        remote_enabled = bool(data.get("remote_enabled", True))
        accept = [a.strip() for a in data.get("location_accept", []) if a.strip()]

        parsed = _load_searches_yaml()
        parsed.setdefault("defaults", {})
        if primary:
            parsed["defaults"]["location"] = primary
        locations = []
        if primary:
            locations.append({"location": primary, "remote": False})
        if remote_enabled:
            locations.append({"location": "Remote", "remote": True})
        parsed["locations"] = locations
        parsed["location_accept"] = accept
        parsed.setdefault("location_reject_non_remote", [])

        cfg.ensure_dirs()
        # NOTE: rewrites the whole file via yaml.safe_dump -- any hand-added
        # comments in searches.yaml are lost on a structured save. The raw
        # YAML editor above remains available for comment-preserving edits.
        cfg.SEARCH_CONFIG_PATH.write_text(
            yaml.safe_dump(parsed, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        return jsonify({"ok": True, **get_location().get_json()})

    # -- profiles (multi-user) --

    @app.get("/api/profiles")
    def get_profiles():
        return jsonify({
            "profiles": cfg.list_profiles(),
            "active": cfg.get_active_profile_name(),
        })

    @app.post("/api/profiles/create")
    def create_profile():
        data = request.get_json(force=True)
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name required"}), 400
        try:
            cfg.create_profile(name)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "profiles": cfg.list_profiles()})

    @app.post("/api/profiles/switch")
    def switch_profile():
        data = request.get_json(force=True)
        name = (data.get("name") or "").strip()
        if name not in cfg.list_profiles():
            return jsonify({"error": f"unknown profile: {name}"}), 400
        cfg.set_active_profile(name)
        return jsonify({"ok": True, "active": name, "restart_required": True})

    @app.post("/api/restart")
    def restart_webui():
        # Relaunch this process in place so a newly-switched profile's
        # module-level paths (cfg.APP_DIR etc.) get recomputed on import.
        def _do_restart():
            import time
            time.sleep(0.3)
            # os.execv doesn't replace the process in-place on Windows (it
            # spawns a child and blocks the parent), leaving two servers
            # bound to the port. Spawn a detached replacement, then hard-exit.
            subprocess.Popen(
                [sys.executable, "-m", "jobpilot.cli", "web"],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
            os._exit(0)
        import threading
        threading.Thread(target=_do_restart, daemon=True).start()
        return jsonify({"ok": True})

    # -- agent loop control --

    @app.get("/api/loop/status")
    def loop_status():
        return jsonify(_loop_status())

    @app.post("/api/loop/start")
    def loop_start():
        _start_loop()
        return jsonify(_loop_status())

    @app.post("/api/loop/stop")
    def loop_stop():
        _stop_loop()
        return jsonify(_loop_status())

    @app.post("/api/loop/live")
    def loop_live():
        data = request.get_json(force=True)
        _set_live(bool(data.get("live")))
        return jsonify(_loop_status())

    @app.post("/api/loop/engine")
    def loop_engine():
        data = request.get_json(force=True)
        _set_engine(data.get("engine", "claude"))
        return jsonify(_loop_status())

    # -- ad-hoc pipeline stage control (independent of the loop) --

    @app.get("/api/pipeline/status")
    def pipeline_status():
        return jsonify(_pipeline_status())

    @app.post("/api/pipeline/run")
    def pipeline_run():
        data = request.get_json(force=True)
        stages = data.get("stages") or []
        stream = bool(data.get("stream", False))
        workers = int(data.get("workers", 4))
        err = _start_pipeline_run(stages, stream=stream, workers=workers)
        if err:
            return jsonify({"error": err}), 409
        return jsonify(_pipeline_status())

    @app.post("/api/pipeline/stop")
    def pipeline_stop():
        _stop_pipeline_run()
        return jsonify(_pipeline_status())


    # Enhanced dashboard APIs
    # ------------------------------------------------------------------

    @app.get("/api/queue")
    def api_queue():
        """Return jobs ready to apply (scored, tailored, cover letter done).

        Mirrors launcher.acquire_job()'s eligibility: not yet applied, not
        currently in progress or permanently done, and under the retry cap.
        (The jobs table has no failed_at column; failure is apply_status/attempts.)
        """
        conn = get_connection()
        rows = conn.execute(
            "SELECT url, title, site, fit_score, salary, location, "
            "tailored_resume_path, cover_letter_path, applied_at, apply_status, "
            "apply_attempts "
            "FROM jobs WHERE fit_score >= ? "
            "AND full_description IS NOT NULL "
            "AND tailored_resume_path IS NOT NULL "
            "AND cover_letter_path IS NOT NULL "
            "AND applied_at IS NULL "
            "AND (apply_status IS NULL OR apply_status = 'failed') "
            "AND (apply_attempts IS NULL OR apply_attempts < ?) "
            "ORDER BY fit_score DESC, discovered_at DESC",
            (cfg.DEFAULTS["min_score"], cfg.DEFAULTS["max_apply_attempts"])
        ).fetchall()
        jobs = []
        for row in rows:
            jobs.append({
                "url": row[0], "title": row[1], "site": row[2],
                "fit_score": row[3], "salary": row[4], "location": row[5],
                "has_resume": bool(row[6]), "has_cover": bool(row[7]),
                "applied": bool(row[8]), "failed": row[9] == "failed",
            })
        return jsonify({"count": len(jobs), "jobs": jobs})

    @app.get("/api/activity")
    def api_activity():
        """Return recent pipeline and apply activity from logs."""
        from pathlib import Path
        logs = []
        log_dir = cfg.APP_DIR / "logs"
        # Read agent_loop.log tail
        loop_log = log_dir / "agent_loop.log"
        if loop_log.exists():
            lines = loop_log.read_text(encoding="utf-8", errors="replace").splitlines()
            logs = lines[-50:]
        return jsonify({"logs": logs, "timestamp": __import__("time").time()})

    @app.get("/api/health")
    def api_health():
        """The same data `jobpilot health` prints.

        Served from jobpilot.health.collect() rather than recomputed here, so
        the CLI and the dashboard can never disagree about whether a loop is
        running or how deep the queue is.
        """
        from jobpilot.health import collect

        try:
            return jsonify(collect())
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    @app.get("/api/stage-progress")
    def api_stage_progress():
        """Return counts for each pipeline stage to show progress."""
        conn = get_connection()
        min_score = cfg.DEFAULTS["min_score"]
        counts = {
            "total": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            "discovered": conn.execute("SELECT COUNT(*) FROM jobs WHERE discovered_at IS NOT NULL").fetchone()[0],
            "enriched": conn.execute("SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NOT NULL").fetchone()[0],
            "scored": conn.execute("SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL").fetchone()[0],
            "high_fit": conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE fit_score >= ?", (min_score,)
            ).fetchone()[0],
            "tailored": conn.execute("SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL").fetchone()[0],
            "cover_done": conn.execute("SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL").fetchone()[0],
            "applied": conn.execute("SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL").fetchone()[0],
            "failed": conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE apply_status = 'failed'"
            ).fetchone()[0],
            # Mirrors api_queue()/launcher.acquire_job() eligibility. There is no
            # failed_at column -- failure lives in apply_status/apply_attempts.
            "ready_to_apply": conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE fit_score >= ? "
                "AND tailored_resume_path IS NOT NULL "
                "AND cover_letter_path IS NOT NULL "
                "AND applied_at IS NULL "
                "AND (apply_status IS NULL OR apply_status = 'failed') "
                "AND (apply_attempts IS NULL OR apply_attempts < ?)",
                (min_score, cfg.DEFAULTS["max_apply_attempts"])
            ).fetchone()[0],
        }
        return jsonify(counts)

    @app.post("/api/auto-apply/toggle")
    def api_auto_apply_toggle():
        """Toggle auto-apply continuous mode."""
        data = request.get_json(force=True)
        enabled = data.get("enabled", False)
        # Write to a flag file
        flag_path = cfg.APP_DIR / "auto_apply.flag"
        if enabled:
            flag_path.write_text("1", encoding="utf-8")
        else:
            flag_path.unlink(missing_ok=True)
        return jsonify({"enabled": enabled})

    @app.get("/api/auto-apply/status")
    def api_auto_apply_status():
        """Check if auto-apply is enabled."""
        flag_path = cfg.APP_DIR / "auto_apply.flag"
        return jsonify({"enabled": flag_path.exists()})


    return app


def run(port: int = 8765, open_browser: bool = True) -> None:
    app = create_app()
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


# ---------------------------------------------------------------------------
# Front end (single-page, vanilla JS, no build step)
# ---------------------------------------------------------------------------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JobPilot Control</title>
<style>
  /* "Paper Console" -- light, near-white, forest accent, high-contrast
     graphic register. Flat by design: no glow/gradient chrome, solid fills,
     hairline rules. Contrast-checked: --text-muted ~5.4:1, --text-faint
     ~4.6:1 on --bg (both clear the 4.5:1 floor for body/placeholder text). */
  :root {
    --bg: #ffffff; --bg-grad: none;
    --surface: #f7f7f5; --surface-2: #eeeeea; --border: #d8d8d2;
    --text: #111111; --text-muted: #6b6b66; --text-faint: #767570;
    --accent: #1a6b4f; --accent-soft: #1a6b4f1a; --accent-strong: #145038;
    --ok: #1a6b4f; --ok-bg: #e3f0ea; --warn: #a8720e; --warn-bg: #fbf0dc;
    --danger: #b3402c; --danger-bg: #fbe4df;
    --radius: 999px; --radius-lg: 12px; --radius-sm: 8px;
    --shadow: none;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg-grad), var(--bg); color: var(--text); min-height: 100vh;
    -webkit-font-smoothing: antialiased;
  }
  header {
    padding: 1.1rem 2rem; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
    position: sticky; top: 0; background: color-mix(in srgb, var(--bg) 88%, transparent);
    backdrop-filter: blur(10px); z-index: 10;
  }
  h1 { font-size: 1.15rem; font-weight: 700; letter-spacing: -0.01em; display: flex; align-items: center; gap: 0.5rem; }
  svg.icon { width: 15px; height: 15px; flex-shrink: 0; fill: currentColor; stroke: none; vertical-align: -3px; }
  nav button svg.icon, .panel h2 svg.icon { margin-right: 0.1rem; }
  nav { display: flex; gap: 0.3rem; background: var(--surface); padding: 0.3rem; border-radius: 999px; border: 1px solid var(--border); }
  nav button {
    background: transparent; border: none; color: var(--text-muted); padding: 0.45rem 1rem;
    border-radius: 999px; cursor: pointer; font-size: 0.82rem; font-weight: 500;
    transition: all 0.15s ease; white-space: nowrap;
    display: inline-flex; align-items: center; gap: 0.4rem;
  }
  nav button:hover:not(.active) { color: var(--text); background: #ffffff0a; }
  nav button.active { background: var(--accent); color: #fff; font-weight: 600; box-shadow: 0 2px 10px -2px var(--accent-soft); }
  main { padding: 2rem; max-width: 1140px; margin: 0 auto; }
  .tab { display: none; animation: fadeIn 0.25s ease; }
  .tab.active { display: block; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0.9rem; margin-bottom: 1.75rem; }
  .card {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
    padding: 1.15rem 1.25rem; transition: border-color 0.15s ease, transform 0.15s ease;
  }
  .card:hover { border-color: color-mix(in srgb, var(--accent) 40%, var(--border)); transform: translateY(-1px); }
  .card .num { font-size: 1.85rem; font-weight: 800; letter-spacing: -0.02em; line-height: 1; }
  .card .label { color: var(--text-muted); font-size: 0.75rem; margin-top: 0.4rem; font-weight: 500; text-transform: uppercase; letter-spacing: 0.03em; }
  .panel {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
    padding: 1.5rem; margin-bottom: 1.25rem; box-shadow: var(--shadow);
  }
  .panel h2 { font-size: 0.95rem; margin-bottom: 1rem; color: var(--text); font-weight: 700; display: flex; align-items: center; gap: 0.5rem; }
  .row { display: flex; gap: 0.75rem; align-items: center; margin-bottom: 0.75rem; flex-wrap: wrap; }
  label { font-size: 0.8rem; color: var(--text-muted); min-width: 90px; font-weight: 500; }
  input[type=text], input[type=password], input[type=number], select, textarea {
    background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 0.55rem 0.75rem;
    border-radius: var(--radius-sm); font-size: 0.85rem; flex: 1; min-width: 200px; font-family: inherit;
    transition: border-color 0.15s ease;
  }
  input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent); }
  textarea { width: 100%; min-height: 260px; font-family: ui-monospace, 'SF Mono', Consolas, monospace; font-size: 0.8rem; line-height: 1.5; }
  button.action {
    background: var(--accent); color: #fff; border: none; padding: 0.6rem 1.15rem;
    border-radius: var(--radius); font-weight: 600; cursor: pointer; font-size: 0.84rem;
    transition: all 0.15s ease; box-shadow: 0 1px 0 #ffffff22 inset;
  }
  button.action:hover:not(:disabled) { background: var(--accent-strong); transform: translateY(-1px); }
  button.action:active:not(:disabled) { transform: translateY(0); }
  button.action.danger { background: var(--danger); color: #1a0a0a; }
  button.action.danger:hover:not(:disabled) { background: #ef4444; }
  button.action.ghost { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); box-shadow: none; }
  button.action.ghost:hover:not(:disabled) { background: #ffffff0d; border-color: var(--text-faint); }
  button.action:disabled { opacity: 0.4; cursor: not-allowed; transform: none; }
  .badge {
    display: inline-flex; align-items: center; gap: 0.35rem; padding: 0.25rem 0.7rem;
    border-radius: 999px; font-size: 0.7rem; font-weight: 700; letter-spacing: 0.02em;
  }
  .badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
  .badge.on { background: var(--ok-bg); color: var(--ok); }
  .badge.off { background: var(--surface-2); color: var(--text-faint); }
  .badge.live { background: var(--danger-bg); color: var(--danger); }
  .badge.stalled { background: var(--danger-bg); color: var(--danger); animation: pulse 2s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
  table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  th, td { text-align: left; padding: 0.6rem 0.7rem; border-bottom: 1px solid var(--border); }
  th { color: var(--text-muted); font-weight: 600; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.03em; }
  tbody tr { transition: background 0.1s ease; }
  tbody tr:hover { background: #ffffff06; }
  .score { font-weight: 700; }
  .score-hi { color: var(--ok); } .score-mid { color: var(--warn); } .score-lo { color: var(--text-faint); }
  .status-needs-login { color: var(--warn); font-weight: 600; }
  .status-blocked { color: var(--text-faint); }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  pre.log {
    background: var(--bg); border: 1px solid var(--border); padding: 1rem; border-radius: var(--radius-sm);
    max-height: 400px; overflow: auto; font-size: 0.75rem; line-height: 1.5; white-space: pre-wrap;
    font-family: ui-monospace, 'SF Mono', Consolas, monospace;
  }
  .muted { color: var(--text-faint); font-size: 0.78rem; margin-top: 0.4rem; line-height: 1.5; }
  .filters { margin-bottom: 1rem; }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 999px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--text-faint); }
</style>
</head>
<body>
<header>
  <h1>JobPilot Control</h1>
  <nav>
    <button data-tab="overview" class="active"><svg class="icon" viewBox="0 0 24 24"><rect x="4" y="10" width="3" height="10" rx="0.5"/><rect x="10.5" y="4" width="3" height="16" rx="0.5"/><rect x="17" y="14" width="3" height="6" rx="0.5"/></svg>Overview</button>
    <button data-tab="jobs"><svg class="icon" viewBox="0 0 24 24"><rect x="3" y="8" width="18" height="12" rx="2"/><path d="M8 8V6a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>Jobs</button>
    <button data-tab="queue"><svg class="icon" viewBox="0 0 24 24"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>Queue</button>
    <button data-tab="pipeline"><svg class="icon" viewBox="0 0 24 24"><circle cx="5" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><circle cx="19" cy="6" r="2.5"/><path d="M7 7.5l3 8M17 7.5l-3 8" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>Pipeline</button>
    <button data-tab="agent"><svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="1.6" stroke-dasharray="3 3"/><circle cx="12" cy="12" r="3"/></svg>Agent Loop</button>
    <button data-tab="activity"><svg class="icon" viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-5 14H7v-2h7v2zm3-4H7v-2h10v2zm0-4H7V7h10v2z"/></svg>Activity</button>
    <button data-tab="settings"><svg class="icon" viewBox="0 0 24 24"><path d="M4 6h6M14 6h6M4 12h10M18 12h2M4 18h13M20 18h0" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="12" cy="6" r="2.2"/><circle cx="16" cy="12" r="2.2"/><circle cx="19" cy="18" r="2.2"/></svg>Settings</button>
  </nav>
</header>
<main>

  <section id="overview" class="tab active">
    <div class="panel">
      <h2><svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M7 12h3l2-4 2 8 2-4h1"/></svg>System health</h2>
      <div id="health-loops"></div>
      <div class="cards" id="health-counters" style="margin-top:0.8rem"></div>
      <div id="health-stages" style="margin-top:0.8rem"></div>
      <div id="health-matches" style="margin-top:0.8rem"></div>
    </div>
    <div class="cards" id="stat-cards"></div>
    <div class="panel">
      <h2><svg class="icon" viewBox="0 0 24 24"><path d="M3 20L12 6l9 14z"/></svg>Score Distribution</h2>
      <div id="score-dist"></div>
      <p class="muted" id="score-dist-note" style="margin-top:0.5rem"></p>
    </div>
  </section>

  <section id="jobs" class="tab">
    <div class="filters row">
      <label>Min score</label>
      <input type="number" id="job-min-score" value="0" style="max-width:80px">
      <label>Location</label>
      <input type="text" id="job-location" placeholder="e.g. Dubai, Remote (comma-separated, blank = all)" style="max-width:260px">
      <button class="action ghost" onclick="useTargetLocationFilter()">Use my target location</button>
      <button class="action ghost" onclick="loadJobs()">Refresh</button>
    </div>
    <div class="panel">
      <table>
        <thead><tr><th>Score</th><th>Title</th><th>Site</th><th>Status</th><th></th></tr></thead>
        <tbody id="jobs-body"></tbody>
      </table>
    </div>
  </section>

  
  <section id="queue" class="tab">
    <div class="panel">
      <h2><svg class="icon" viewBox="0 0 24 24"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>Apply Queue</h2>
      <p class="muted" id="queue-status">Jobs ready to apply: <span id="queue-count">0</span></p>
      <div id="queue-list" style="max-height:60vh;overflow:auto;"></div>
    </div>
  </section>

<section id="pipeline" class="tab">
    <div class="panel">
      <h2><svg class="icon" viewBox="0 0 24 24"><circle cx="5" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><circle cx="19" cy="6" r="2.5"/><path d="M7 7.5l3 8M17 7.5l-3 8" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>Ad-hoc pipeline run <span id="pipe-badge" class="badge off">stopped</span></h2>
      <p class="muted">Runs a single pass now, independent of the scheduled Agent Loop. Only one of (this run / the loop) can be active at a time.</p>
      <div class="row" style="margin-top:0.75rem">
        <button class="action ghost" onclick="runStage(['discover'])">Discover only</button>
        <button class="action ghost" onclick="runStage(['enrich'])">Enrich only</button>
        <button class="action ghost" onclick="runStage(['score'])">Score only</button>
        <button class="action ghost" onclick="runStage(['tailor'])">Tailor only</button>
        <button class="action ghost" onclick="runStage(['cover'])">Cover letters only</button>
      </div>
      <div class="row">
        <button class="action" onclick="runStage(['discover','enrich','score','tailor','cover','pdf'], true)">Full pipeline (streaming -- stages overlap)</button>
        <button class="action danger" onclick="stopPipeline()">Stop current run</button>
      </div>
      <p class="muted">Streaming mode lets scoring start on jobs as they're discovered instead of waiting for discovery to finish -- reduces wall-clock, doesn't increase LLM throughput (still bound by whichever provider is configured in Settings).</p>
      <h2 style="margin-top:1.5rem">Recent log</h2>
      <pre class="log" id="pipe-log">(no log yet)</pre>
    </div>
  </section>

  <section id="agent" class="tab">
    <div class="panel">
      <h2><svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="1.6" stroke-dasharray="3 3"/><circle cx="12" cy="12" r="3"/></svg>Loop status <span id="loop-badge" class="badge off">stopped</span> <span id="live-badge" class="badge off">dry-run</span> <span id="engine-badge" class="badge off">claude</span></h2>
      <div class="row">
        <button class="action" onclick="startLoop()">Start</button>
        <button class="action danger" onclick="stopLoop()">Stop</button>
        <button class="action ghost" id="live-toggle" onclick="toggleLive()">Enable live submit</button>
      </div>
      <p class="muted">Dry-run fills forms but never clicks submit. Live mode submits real applications. Toggle takes effect on the loop's next apply cycle.</p>
      <div class="row" style="margin-top:1rem">
        <label>Apply engine</label>
        <select id="engine-select">
          <option value="claude">Claude Code (spawns a Claude session per job, costs API usage)</option>
          <option value="local">Local LLM (drives the browser via the LLM configured in Settings, no Claude Code usage)</option>
        </select>
        <button class="action ghost" onclick="saveEngine()">Save</button>
      </div>
      <div class="row" style="margin-top:1rem;align-items:center;">
        <label style="min-width:auto;margin-right:0.5rem;">Auto-Apply Mode</label>
        <button id="auto-apply-btn" class="action" onclick="toggleAutoApply()" style="background:var(--text-faint);">Enable</button>
        <p class="muted" style="margin-left:0.5rem;flex:1;">Continuously applies to ready jobs without stopping</p>
      </div>
      <div class="row" style="margin-top:0.25rem">
        <p class="muted" id="auto-apply-status">Auto-apply is OFF. Jobs will queue until you manually apply.</p>
      </div>
      <h2 style="margin-top:1.5rem">Recent log</h2>
      <pre class="log" id="loop-log">(no log yet)</pre>
    </div>
  </section>

  
  <section id="activity" class="tab">
    <div class="panel">
      <h2><svg class="icon" viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-5 14H7v-2h7v2zm3-4H7v-2h10v2zm0-4H7V7h10v2z"/></svg>Live Activity</h2>
      <div class="row" style="justify-content:space-between;align-items:center;">
        <p class="muted">Real-time pipeline and apply activity</p>
        <span id="activity-status" class="badge off">paused</span>
      </div>
      <pre id="activity-feed" style="max-height:55vh;overflow:auto;font-size:0.78rem;background:var(--bg-alt);padding:0.75rem;border-radius:8px;"></pre>
    </div>
  </section>

<section id="settings" class="tab">
    <div class="panel">
      <h2><svg class="icon" viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.2"/><path d="M2 21c0-3.6 3.1-6.5 7-6.5s7 2.9 7 6.5"/><circle cx="18" cy="8" r="2.4" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M16 14.6c2.7.5 4.6 2.7 4.6 6.1" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>Profiles</h2>
      <p class="muted" style="margin-bottom:0.75rem">Each profile keeps its own resume, jobs, searches, and settings -- use one per person sharing this install.</p>
      <div class="row">
        <label>Active</label>
        <select id="profile-select"></select>
        <button class="action" onclick="switchProfile()">Switch</button>
      </div>
      <div class="row" style="margin-top:0.5rem">
        <label>New profile</label>
        <input type="text" id="new-profile-name" placeholder="e.g. jane">
        <button class="action ghost" onclick="createProfile()">Create</button>
      </div>
      <p class="muted" id="profile-status" style="margin-top:0.5rem"></p>
    </div>

    <div class="panel">
      <h2><svg class="icon" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>LLM Provider</h2>
      <div class="row">
        <label>Provider</label>
        <select id="llm-provider">
          <option value="gemini">Gemini (cloud)</option>
          <option value="openai">OpenAI (cloud)</option>
          <option value="local">Local (llama.cpp / Ollama)</option>
        </select>
      </div>
      <div class="row" id="row-local-url"><label>Local URL</label><input type="text" id="llm-url" placeholder="http://127.0.0.1:8080/v1"></div>
      <div class="row"><label>Model</label><input type="text" id="llm-model" placeholder="gemini-2.0-flash / local-model"></div>
      <div class="row" id="row-gemini-key"><label>Gemini key</label><input type="password" id="gemini-key" placeholder="(unchanged if blank)"></div>
      <div class="row" id="row-openai-key"><label>OpenAI key</label><input type="password" id="openai-key" placeholder="(unchanged if blank)"></div>
      <div class="row" id="row-local-key"><label>Local API key</label><input type="password" id="llm-api-key" placeholder="(optional, unchanged if blank)"></div>
      <button class="action" onclick="saveLlm()">Save</button>
      <p class="muted" id="llm-current"></p>
    </div>

    <div class="panel">
      <h2><svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-7 8-7s8 3 8 7z"/></svg>Personal Details</h2>
      <div class="row"><label>Full name</label><input type="text" id="p-full_name"></div>
      <div class="row"><label>Preferred name</label><input type="text" id="p-preferred_name"></div>
      <div class="row"><label>Email</label><input type="text" id="p-email"></div>
      <div class="row"><label>Phone</label><input type="text" id="p-phone"></div>
      <div class="row"><label>City</label><input type="text" id="p-city"></div>
      <div class="row"><label>Country</label><input type="text" id="p-country"></div>
      <div class="row"><label>LinkedIn</label><input type="text" id="p-linkedin_url"></div>
      <h2 style="margin-top:1.25rem;font-size:0.85rem">Work authorization</h2>
      <div class="row">
        <label>Authorized</label>
        <select id="p-legally_authorized_to_work"><option value="Yes">Yes</option><option value="No">No</option></select>
        <label>Needs sponsorship</label>
        <select id="p-require_sponsorship"><option value="No">No</option><option value="Yes">Yes</option></select>
      </div>
      <div class="row"><label>Permit type</label><input type="text" id="p-work_permit_type"></div>
      <h2 style="margin-top:1.25rem;font-size:0.85rem">Compensation</h2>
      <div class="row">
        <label>Currency</label><input type="text" id="p-salary_currency" style="max-width:90px">
        <label>Target</label><input type="text" id="p-salary_expectation" style="max-width:120px">
        <label>Min</label><input type="text" id="p-salary_range_min" style="max-width:120px">
        <label>Max</label><input type="text" id="p-salary_range_max" style="max-width:120px">
      </div>
      <h2 style="margin-top:1.25rem;font-size:0.85rem">Experience</h2>
      <div class="row"><label>Target role</label><input type="text" id="p-target_role"></div>
      <div class="row">
        <label>Years exp.</label><input type="text" id="p-years_of_experience_total" style="max-width:80px">
        <label>Education</label><input type="text" id="p-education_level">
      </div>
      <div class="row" style="margin-top:0.75rem">
        <button class="action" onclick="saveProfileForm()">Save profile</button>
        <button class="action ghost" onclick="toggleRawProfile()" id="raw-profile-toggle">Show raw JSON (advanced)</button>
      </div>
      <div id="raw-profile-wrap" style="display:none;margin-top:0.75rem">
        <p class="muted" style="margin-bottom:0.5rem">Full profile including skills, resume_facts, EEO. Editing here overrides the form fields above on save.</p>
        <textarea id="profile-json"></textarea>
        <div class="row" style="margin-top:0.75rem"><button class="action ghost" onclick="saveProfile()">Save raw JSON</button></div>
      </div>
    </div>

    <div class="panel">
      <h2><svg class="icon" viewBox="0 0 24 24"><path d="M12 21s7-6.5 7-12a7 7 0 1 0-14 0c0 5.5 7 12 7 12z"/></svg>Target Location</h2>
      <p class="muted" style="margin-bottom:0.75rem">Controls which jobs pass discovery's location filter. Non-remote postings only pass if their location matches one of the accepted cities/countries below.</p>
      <div class="row">
        <button class="action ghost" onclick="applyLocationPreset('dubai')">Dubai + Remote</button>
        <button class="action ghost" onclick="applyLocationPreset('remote')">Remote only</button>
      </div>
      <div class="row"><label>Primary city</label><input type="text" id="loc-primary" placeholder="e.g. Dubai, United Arab Emirates"></div>
      <div class="row">
        <label>Remote OK</label>
        <select id="loc-remote"><option value="true">Yes</option><option value="false">No</option></select>
      </div>
      <div class="row"><label>Accepted</label><input type="text" id="loc-accept-input" placeholder="Add a city or country, e.g. Abu Dhabi"><button class="action ghost" onclick="addAcceptedLocation()">Add</button></div>
      <div class="row" id="loc-accept-tags"></div>
      <div class="row" style="margin-top:0.5rem"><button class="action" onclick="saveLocation()">Save location</button></div>
    </div>

    <div class="panel">
      <h2><svg class="icon" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7" fill="none" stroke="currentColor" stroke-width="2.5"/><path d="M21 21l-4.3-4.3" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg>Search config (raw YAML)</h2>
      <p class="muted" style="margin-bottom:0.5rem">Queries, tiers, and everything else. Saving Target Location above regenerates this file's location fields (comments there are lost); edit here for anything else.</p>
      <textarea id="searches-yaml"></textarea>
      <div class="row" style="margin-top:0.75rem"><button class="action" onclick="saveSearches()">Save searches</button></div>
    </div>
  </section>

</main>
<script>
function $(id) { return document.getElementById(id); }

document.querySelectorAll('nav button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  $(b.dataset.tab).classList.add('active');
  if (b.dataset.tab === 'jobs') loadJobs();
  if (b.dataset.tab === 'pipeline') loadPipeline();
  if (b.dataset.tab === 'agent') loadLoop();
  if (b.dataset.tab === 'settings') loadSettings();
  loadQueue();
  loadActivity();
  loadStageProgress();
  loadAutoApplyStatus();
}));

async function loadPipeline() {
  const s = await (await fetch('/api/pipeline/status')).json();
  $('pipe-badge').textContent = s.running ? 'running' : 'stopped';
  $('pipe-badge').className = 'badge ' + (s.running ? 'on' : 'off');
  $('pipe-log').textContent = s.log_tail || '(no log yet)';
}
async function runStage(stages, stream) {
  const resp = await fetch('/api/pipeline/run', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({stages, stream: !!stream, workers: 4}),
  });
  if (!resp.ok) { const e = await resp.json(); alert(e.error || 'Failed to start'); }
  loadPipeline();
}
async function stopPipeline() { await fetch('/api/pipeline/stop', {method:'POST'}); loadPipeline(); }

async function loadHealth() {
  let h;
  try { h = await (await fetch('/api/health')).json(); }
  catch (e) { $('health-loops').innerHTML = '<p class="muted">health unavailable</p>'; return; }
  if (h.error) { $('health-loops').innerHTML = `<p class="muted">${h.error}</p>`; return; }

  const tone = { RUNNING:'on', 'LIKELY UP':'on', STALLED:'stalled', STOPPED:'off', UNKNOWN:'stalled' };
  $('health-loops').innerHTML = (h.loops || []).map(l => `
    <div style="display:flex;align-items:center;gap:0.7rem;margin-bottom:0.4rem">
      <span style="min-width:6rem;font-weight:600">${l.label}</span>
      <span class="badge ${tone[l.state] || 'off'}">${l.state}</span>
      <span class="muted" style="font-size:0.82rem">pid ${l.pid ?? '-'} &middot; ${l.note}</span>
    </div>`).join('');

  const c = h.counters || {};
  // acquirable is the blocked-site-aware count; the older "Ready to apply"
  // card below ignores that filter and reads high.
  const counters = [
    [c.new_last_hour, 'New last hour'],
    [c.unscored_backlog, 'Unscored backlog'],
    [c.acquirable, 'Acquirable to apply'],
    [c.manual, 'Retired as manual'],
  ];
  $('health-counters').innerHTML = counters.map(([v, label]) =>
    `<div class="card"><div class="num">${v ?? 0}</div><div class="label">${label}</div></div>`).join('');

  const rows = (h.stages || []).map(st => `
    <tr><td>${st.stage}</td>
        <td style="text-align:right">${st.total}</td>
        <td style="text-align:right">${st.last_24h}</td></tr>`).join('');
  $('health-stages').innerHTML = `
    <table style="width:100%;font-size:0.85rem">
      <thead><tr><th style="text-align:left">stage</th>
        <th style="text-align:right">total</th>
        <th style="text-align:right">last 24h</th></tr></thead>
      <tbody>${rows}</tbody></table>`;

  const m = h.matches || [];
  $('health-matches').innerHTML = m.length ? `
    <p class="muted" style="margin:0 0 0.4rem">Recent fast-lane matches (prepared, awaiting you)</p>
    ${m.map(j => `<div style="display:flex;gap:0.6rem;align-items:baseline;margin-bottom:0.25rem">
        <span class="badge on">${j.fit_score ?? '?'}</span>
        <a href="${j.application_url || j.url}" target="_blank" rel="noopener">${(j.title || '?')}</a>
      </div>`).join('')}`
    : '<p class="muted">No prepared fast-lane matches yet.</p>';
}

async function loadStats() {
  const s = await (await fetch('/api/stats')).json();
  const cards = [
    ['total', 'Discovered'], ['scored', 'Scored'], ['tailored', 'Tailored'],
    ['with_cover_letter', 'Cover letters'], ['ready_to_apply', 'Prepared (all sites)'], ['applied', 'Applied'],
  ];
  $('stat-cards').innerHTML = cards.map(([k, label]) =>
    `<div class="card"><div class="num">${s[k] ?? 0}</div><div class="label">${label}</div></div>`
  ).join('');
  const dist = s.score_distribution || [];
  const maxCount = Math.max(1, ...dist.map(([, c]) => c));
  const colorFor = (score) => score >= 7 ? 'var(--ok)' : score >= 5 ? 'var(--warn)' : 'var(--text-faint)';
  $('score-dist').innerHTML = dist.length ? dist.map(([score, count]) => `
    <div style="display:flex;align-items:center;gap:0.7rem;margin-bottom:0.5rem">
      <span style="width:1.2rem;font-weight:700;font-size:0.8rem;text-align:right">${score}</span>
      <div style="flex:1;height:10px;background:var(--surface-2);border-radius:999px;overflow:hidden">
        <div style="width:100%;height:100%;background:${colorFor(score)};border-radius:999px;transform:scaleX(${(count/maxCount).toFixed(3)});transform-origin:left;transition:transform 0.4s ease"></div>
      </div>
      <span class="muted" style="width:3.5rem">${count} jobs</span>
    </div>`).join('') : '<p class="muted">No scored jobs yet.</p>';
  // Deliberately-skipped jobs (e.g. deprioritized as off-target) are a
  // different axis from fit quality -- shown separately, never mixed into
  // the 1-10 distribution above, so one doesn't visually swamp the other.
  const skipped = s.skipped || 0;
  $('score-dist-note').textContent = skipped
    ? `+ ${skipped.toLocaleString()} deprioritized (off-target, not fit-scored) -- excluded above`
    : '';
}

async function useTargetLocationFilter() {
  const loc = await (await fetch('/api/settings/location')).json();
  const terms = [...(loc.location_accept || [])];
  if (loc.remote_enabled) terms.push('Remote');
  $('job-location').value = terms.join(', ');
  loadJobs();
}

async function loadJobs() {
  const minScore = $('job-min-score').value || 0;
  const location = encodeURIComponent($('job-location').value || '');
  const jobs = await (await fetch(`/api/jobs?min_score=${minScore}&location=${location}&limit=300`)).json();
  $('jobs-body').innerHTML = jobs.map(j => {
    const score = j.fit_score ?? '-';
    const cls = j.fit_score >= 6 ? 'score-hi' : (j.fit_score >= 5 ? 'score-mid' : 'score-lo');
    let status = 'discovered';
    let statusCls = '';
    if (j.applied_at) status = 'applied';
    else if (j.apply_status === 'failed') {
      // Show the real blocker, not just "failed" -- login_issue means a
      // human can go log in and it'll likely work next attempt; captcha/
      // unsafe_verification/expired are dead ends the loop can't fix itself.
      status = j.apply_error ? `failed: ${j.apply_error}` : 'failed';
      statusCls = (j.apply_error || '').includes('login') ? 'status-needs-login' : 'status-blocked';
    }
    else if (j.apply_status) status = j.apply_status;
    else if (j.cover_letter_path) status = 'cover letter ready';
    else if (j.tailored_resume_path) status = 'tailored';
    else if (j.fit_score != null) status = 'scored';
    const fileLink = (kind, label) => ['txt', 'pdf', 'docx'].map(fmt =>
      `<a class="action ghost" href="/api/jobs/file?kind=${kind}&format=${fmt}&url=${encodeURIComponent(j.url)}" target="_blank">${label} .${fmt}</a>`
    ).join('');
    const fileLinks = [
      j.tailored_resume_path ? fileLink('resume', 'Resume') : '',
      j.cover_letter_path ? fileLink('cover_letter', 'Cover letter') : '',
    ].join('');
    return `<tr>
      <td class="score ${cls}">${score}</td>
      <td><a href="${j.url}" target="_blank">${(j.title||'').slice(0,60)}</a></td>
      <td>${j.site||''}</td>
      <td class="${statusCls}">${status}</td>
      <td>
        ${fileLinks}
        <button class="action ghost" onclick="markJob('${j.url.replace(/'/g,"\\'")}','applied')">Mark applied</button>
        <button class="action ghost" onclick="markJob('${j.url.replace(/'/g,"\\'")}','failed')">Mark failed</button>
      </td>
    </tr>`;
    loadStageProgress();
  }).join('');
}

async function markJob(url, status) {
  await fetch('/api/jobs/mark', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url, status})});
  loadJobs();
}

async function loadLoop() {
  const s = await (await fetch('/api/loop/status')).json();
  $('loop-badge').textContent = s.stalled ? 'stalled' : (s.running ? 'running' : 'stopped');
  $('loop-badge').className = 'badge ' + (s.stalled ? 'stalled' : (s.running ? 'on' : 'off'));
  $('live-badge').textContent = s.live ? 'LIVE' : 'dry-run';
  $('live-badge').className = 'badge ' + (s.live ? 'live' : 'off');
  $('live-toggle').textContent = s.live ? 'Disable live submit' : 'Enable live submit';
  $('engine-badge').textContent = s.engine || 'claude';
  $('engine-badge').className = 'badge ' + (s.engine === 'local' ? 'on' : 'off');
  $('engine-select').value = s.engine || 'claude';
  const fl = s.fastlane || {};
  const flLine = 'fast lane: ' + (fl.state || 'unknown') + '  (pid ' + (fl.pid || '-') + ', last activity ' + (fl.last || '?') + ')';
  $('loop-log').textContent = flLine + '\n' + '-'.repeat(flLine.length) + '\n' + (s.log_tail || '(no log yet)');
}
async function startLoop() { await fetch('/api/loop/start', {method:'POST'}); loadLoop(); }
async function stopLoop() { await fetch('/api/loop/stop', {method:'POST'}); loadLoop(); }
async function toggleLive() {
  const badge = $('live-badge').textContent.trim();
  const live = badge !== 'LIVE';
  if (live && !confirm('Enable LIVE mode? The agent will submit real job applications.')) return;
  await fetch('/api/loop/live', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({live})});
  loadLoop();
}
async function saveEngine() {
  const engine = $('engine-select').value;
  await fetch('/api/loop/engine', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({engine})});
  loadLoop();
}

function updateLlmRows() {
  const p = $('llm-provider').value;
  $('row-local-url').style.display = p === 'local' ? 'flex' : 'none';
  $('row-local-key').style.display = p === 'local' ? 'flex' : 'none';
  $('row-gemini-key').style.display = p === 'gemini' ? 'flex' : 'none';
  $('row-openai-key').style.display = p === 'openai' ? 'flex' : 'none';
}
$('llm-provider').addEventListener('change', updateLlmRows);

let _profileData = {};

function _pget(path, def) {
  return path.split('.').reduce((o, k) => (o && o[k] !== undefined) ? o[k] : undefined, _profileData) ?? def;
}
function _pset(path, val) {
  const keys = path.split('.');
  let cur = _profileData;
  for (let i = 0; i < keys.length - 1; i++) { cur[keys[i]] = cur[keys[i]] || {}; cur = cur[keys[i]]; }
  cur[keys[keys.length - 1]] = val;
}

const PROFILE_FIELDS = [
  ['p-full_name', 'personal.full_name'], ['p-preferred_name', 'personal.preferred_name'],
  ['p-email', 'personal.email'], ['p-phone', 'personal.phone'],
  ['p-city', 'personal.city'], ['p-country', 'personal.country'],
  ['p-linkedin_url', 'personal.linkedin_url'],
  ['p-legally_authorized_to_work', 'work_authorization.legally_authorized_to_work'],
  ['p-require_sponsorship', 'work_authorization.require_sponsorship'],
  ['p-work_permit_type', 'work_authorization.work_permit_type'],
  ['p-salary_currency', 'compensation.salary_currency'],
  ['p-salary_expectation', 'compensation.salary_expectation'],
  ['p-salary_range_min', 'compensation.salary_range_min'],
  ['p-salary_range_max', 'compensation.salary_range_max'],
  ['p-target_role', 'experience.target_role'],
  ['p-years_of_experience_total', 'experience.years_of_experience_total'],
  ['p-education_level', 'experience.education_level'],
];

async function loadProfiles() {
  const p = await (await fetch('/api/profiles')).json();
  const sel = $('profile-select');
  sel.innerHTML = p.profiles.map(n => `<option value="${n}"${n === p.active ? ' selected' : ''}>${n}</option>`).join('');
  $('profile-status').textContent = `Active: ${p.active}`;
}

async function createProfile() {
  const name = $('new-profile-name').value.trim();
  if (!name) return;
  const r = await (await fetch('/api/profiles/create', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name})})).json();
  if (r.error) { $('profile-status').textContent = `Error: ${r.error}`; return; }
  $('new-profile-name').value = '';
  await loadProfiles();
}

async function switchProfile() {
  const name = $('profile-select').value;
  if (!name) return;
  if (!confirm(`Switch active profile to "${name}"? The web UI will restart -- this takes a few seconds.`)) return;
  await fetch('/api/profiles/switch', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name})});
  $('profile-status').textContent = 'Restarting...';
  await fetch('/api/restart', {method: 'POST'});
  setTimeout(() => location.reload(), 3000);
}

async function loadSettings() {
  await loadProfiles();
  const llm = await (await fetch('/api/settings/llm')).json();
  $('llm-provider').value = llm.provider === 'none' ? 'gemini' : llm.provider;
  $('llm-url').value = llm.llm_url || '';
  $('llm-model').value = llm.llm_model || '';
  updateLlmRows();
  $('llm-current').textContent = `Active: ${llm.provider} ${llm.llm_model ? '(' + llm.llm_model + ')' : ''}`;

  _profileData = await (await fetch('/api/settings/profile')).json();
  $('profile-json').value = JSON.stringify(_profileData, null, 2);
  for (const [id, path] of PROFILE_FIELDS) { $(id).value = _pget(path, ''); }

  const searches = await (await fetch('/api/settings/searches')).json();
  $('searches-yaml').value = searches.yaml || '';

  const loc = await (await fetch('/api/settings/location')).json();
  $('loc-primary').value = loc.primary_location || '';
  $('loc-remote').value = loc.remote_enabled ? 'true' : 'false';
  _acceptedLocations = loc.location_accept || [];
  renderAcceptedLocations();
}

async function saveLlm() {
  const body = {
    provider: $('llm-provider').value,
    llm_url: $('llm-url').value,
    llm_model: $('llm-model').value,
    llm_api_key: $('llm-api-key').value,
    gemini_key: $('gemini-key').value,
    openai_key: $('openai-key').value,
  };
  await fetch('/api/settings/llm', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  $('gemini-key').value = ''; $('openai-key').value = ''; $('llm-api-key').value = '';
  loadSettings();
  loadQueue();
  loadActivity();
  loadStageProgress();
  loadAutoApplyStatus();
}

async function saveProfile() {
  let data;
  try { data = JSON.parse($('profile-json').value); } catch (e) { alert('Invalid JSON: ' + e.message); return; }
  await fetch('/api/settings/profile', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data)});
  _profileData = data;
  alert('Profile saved.');
}

async function saveProfileForm() {
  for (const [id, path] of PROFILE_FIELDS) { _pset(path, $(id).value); }
  await fetch('/api/settings/profile', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(_profileData)});
  $('profile-json').value = JSON.stringify(_profileData, null, 2);
  alert('Profile saved.');
}

function toggleRawProfile() {
  const wrap = $('raw-profile-wrap');
  const showing = wrap.style.display !== 'none';
  wrap.style.display = showing ? 'none' : 'block';
  $('raw-profile-toggle').textContent = showing ? 'Show raw JSON (advanced)' : 'Hide raw JSON';
}

async function saveSearches() {
  await fetch('/api/settings/searches', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({yaml: $('searches-yaml').value})});
  alert('Search config saved.');
}

let _acceptedLocations = [];

function renderAcceptedLocations() {
  $('loc-accept-tags').innerHTML = _acceptedLocations.length ? _acceptedLocations.map((loc, i) =>
    `<span class="badge off" style="cursor:pointer" onclick="removeAcceptedLocation(${i})" title="Click to remove">${loc}<svg class="icon" style="width:9px;height:9px;margin-left:0.3rem" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg></span>`
  ).join('') : '<span class="muted">Remote-only (no cities/countries accepted for onsite roles)</span>';
}

function addAcceptedLocation() {
  const val = $('loc-accept-input').value.trim();
  if (val && !_acceptedLocations.includes(val)) { _acceptedLocations.push(val); renderAcceptedLocations(); }
  $('loc-accept-input').value = '';
}

function removeAcceptedLocation(i) {
  _acceptedLocations.splice(i, 1);
  renderAcceptedLocations();
}

function applyLocationPreset(preset) {
  if (preset === 'dubai') {
    $('loc-primary').value = 'Dubai, United Arab Emirates';
    $('loc-remote').value = 'true';
    _acceptedLocations = ['Dubai', 'UAE', 'United Arab Emirates'];
  } else if (preset === 'remote') {
    $('loc-primary').value = '';
    $('loc-remote').value = 'true';
    _acceptedLocations = [];
  }
  renderAcceptedLocations();
}

async function saveLocation() {
  const body = {
    primary_location: $('loc-primary').value,
    remote_enabled: $('loc-remote').value === 'true',
    location_accept: _acceptedLocations,
  };
  await fetch('/api/settings/location', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const searches = await (await fetch('/api/settings/searches')).json();
  $('searches-yaml').value = searches.yaml || '';
  alert('Location saved. Takes effect on the next discover run.');
}

loadStats();
loadHealth();
setInterval(loadStats, 15000);
setInterval(() => { if ($('overview').classList.contains('active')) loadHealth(); }, 10000);
setInterval(() => { if ($('agent').classList.contains('active')) loadLoop(); }, 5000);
setInterval(() => { if ($('pipeline').classList.contains('active')) loadPipeline(); }, 5000);

// ---------------------------------------------------------------------------
// Auto-refresh
// ---------------------------------------------------------------------------
let _refreshInterval = null;
function startAutoRefresh() {
  if (_refreshInterval) clearInterval(_refreshInterval);
  _refreshInterval = setInterval(() => {
    loadStats();
    loadQueue();
    loadActivity();
    loadStageProgress();
    loadLoop();
  }, 5000);
}
startAutoRefresh();

// ---------------------------------------------------------------------------
// Apply Queue
// ---------------------------------------------------------------------------
async function loadQueue() {
  try {
    const r = await fetch('/api/queue');
    const d = await r.json();
    $('queue-count').textContent = d.count;
    const list = $('queue-list');
    if (!d.jobs.length) {
      list.innerHTML = '<p class="muted">No jobs ready to apply yet. Run the pipeline first.</p>';
      return;
    }
    let html = '';
    for (const j of d.jobs.slice(0, 50)) {
      const scoreClass = j.fit_score >= 8 ? 'score-hi' : (j.fit_score >= 6 ? 'score-mid' : 'score-lo');
      html += `<div class="job-row" style="padding:0.5rem;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;">`;
      html += `<div style="flex:1;min-width:0;">`;
      html += `<div style="font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(j.title || 'Untitled')}</div>`;
      html += `<div class="muted" style="font-size:0.75rem;">${escapeHtml(j.site || '?')} | ${escapeHtml(j.location || '?')} | ${j.salary || 'Salary N/A'}</div>`;
      html += `</div>`;
      html += `<span class="score-pill ${scoreClass}" style="margin-left:0.5rem;">${j.fit_score}</span>`;
      html += `</div>`;
    }
    if (d.jobs.length > 50) {
      html += `<p class="muted" style="text-align:center;padding:0.5rem;">... and ${d.jobs.length - 50} more</p>`;
    }
    list.innerHTML = html;
  } catch (e) {
    console.error('loadQueue error:', e);
  }
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Activity Feed
// ---------------------------------------------------------------------------
let _lastActivityLog = '';
async function loadActivity() {
  try {
    const r = await fetch('/api/activity');
    const d = await r.json();
    const feed = $('activity-feed');
    const newLog = d.logs.join('\n');
    if (newLog !== _lastActivityLog) {
      _lastActivityLog = newLog;
      feed.textContent = newLog;
      feed.scrollTop = feed.scrollHeight;
      // Update status badge
      const status = $('activity-status');
      const hasActivity = d.logs.some(l => l.includes('---') || l.includes('LIVE') || l.includes('submit'));
      if (hasActivity) {
        status.textContent = 'active';
        status.className = 'badge on';
      } else {
        status.textContent = 'idle';
        status.className = 'badge off';
      }
    }
  } catch (e) {
    console.error('loadActivity error:', e);
  }
}

// ---------------------------------------------------------------------------
// Pipeline Stage Progress
// ---------------------------------------------------------------------------
async function loadStageProgress() {
  try {
    const r = await fetch('/api/stage-progress');
    const d = await r.json();
    // Update overview stat cards with progress bars
    const cards = $('stat-cards');
    if (!cards) return;

    const total = d.total || 1;
    const stages = [
      { key: 'discovered', label: 'Discovered', color: '#60a5fa' },
      { key: 'enriched', label: 'Enriched', color: '#34d399' },
      { key: 'scored', label: 'Scored', color: '#a78bfa' },
      { key: 'high_fit', label: 'High Fit', color: '#fbbf24' },
      { key: 'tailored', label: 'Tailored', color: '#f87171' },
      { key: 'cover_done', label: 'Cover', color: '#fb923c' },
      { key: 'applied', label: 'Applied', color: '#10b981' },
    ];

    let html = '';
    for (const s of stages) {
      const count = d[s.key] || 0;
      const pct = Math.round((count / total) * 100);
      html += `<div class="card">`;
      html += `<div class="num">${count}</div>`;
      html += `<div class="label">${s.label}</div>`;
      html += `<div style="width:100%;height:4px;background:var(--border);border-radius:2px;margin-top:0.5rem;">`;
      html += `<div style="width:${pct}%;height:100%;background:${s.color};border-radius:2px;transition:width 0.5s;"></div>`;
      html += `</div>`;
      html += `</div>`;
    }

    // Add ready-to-apply badge
    const ready = d.ready_to_apply || 0;
    html += `<div class="card" style="border:2px solid #10b981;">`;
    html += `<div class="num" style="color:#10b981;font-size:2.2rem;">${ready}</div>`;
    html += `<div class="label">Ready to Apply</div>`;
    html += `</div>`;

    cards.innerHTML = html;
  } catch (e) {
    console.error('loadStageProgress error:', e);
  }
}

// ---------------------------------------------------------------------------
// Auto-Apply Toggle
// ---------------------------------------------------------------------------
async function toggleAutoApply() {
  const btn = $('auto-apply-btn');
  const status = $('auto-apply-status');
  const currentlyEnabled = btn.textContent === 'Disable';
  const newEnabled = !currentlyEnabled;

  try {
    await fetch('/api/auto-apply/toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: newEnabled}),
    });

    if (newEnabled) {
      btn.textContent = 'Disable';
      btn.style.background = 'var(--ok)';
      status.textContent = 'Auto-apply is ON. Jobs will be applied to automatically as they become ready.';
      status.style.color = 'var(--ok)';
    } else {
      btn.textContent = 'Enable';
      btn.style.background = 'var(--text-faint)';
      status.textContent = 'Auto-apply is OFF. Jobs will queue until you manually apply.';
      status.style.color = '';
    }
  } catch (e) {
    console.error('toggleAutoApply error:', e);
  }
}

async function loadAutoApplyStatus() {
  try {
    const r = await fetch('/api/auto-apply/status');
    const d = await r.json();
    const btn = $('auto-apply-btn');
    const status = $('auto-apply-status');
    if (d.enabled) {
      btn.textContent = 'Disable';
      btn.style.background = 'var(--ok)';
      status.textContent = 'Auto-apply is ON. Jobs will be applied to automatically as they become ready.';
      status.style.color = 'var(--ok)';
    }
  } catch (e) {
    console.error('loadAutoApplyStatus error:', e);
  }
}

</script>
</body>
</html>
"""
    # ------------------------------------------------------------------

