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
                       apply_status, apply_error, application_url,
                       scam_verdict, scam_reasons, scam_checked_at
                FROM jobs WHERE {where}
                ORDER BY fit_score DESC, discovered_at DESC LIMIT ?""",
            params + [limit],
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.get("/api/jobs/flagged")
    @app.get("/api/scam/flagged")
    def api_flagged_jobs():
        limit = request.args.get("limit", default=100, type=int)
        conn = get_connection()
        rows = conn.execute(
            """SELECT url, title, site, location, fit_score, score_reasoning,
                      tailored_resume_path, cover_letter_path, applied_at,
                      apply_status, apply_error, application_url,
                      scam_verdict, scam_reasons, scam_checked_at
               FROM jobs WHERE scam_verdict = 'blocked'
               ORDER BY scam_checked_at DESC, discovered_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.post("/api/jobs/report-scam")
    @app.post("/api/scam/report")
    def api_report_scam():
        data = request.get_json(force=True) or {}
        url = data.get("url")
        note = data.get("note", "")
        if not url:
            return jsonify({"error": "url is required"}), 400
        try:
            from jobpilot.scam_report import record_report
            res = record_report(url, note=note)
            return jsonify({"ok": True, "report": res})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/api/jobs/clear-scam")
    @app.post("/api/scam/clear")
    def api_clear_scam():
        data = request.get_json(force=True) or {}
        url = data.get("url")
        if not url:
            return jsonify({"error": "url is required"}), 400
        from datetime import datetime, timezone
        conn = get_connection()
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE jobs SET scam_verdict = 'clear', scam_checked_at = ? WHERE url = ?",
            (now_iso, url),
        )
        conn.commit()
        return jsonify({"ok": True})

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

    def _resolve_openrouter() -> str:
        """Detect OpenRouter routing: simple OPENROUTER_API_KEY form, OR the
        per-stage OpenRouter setup JobPilot actually ships with (stage URLs
        pointing at openrouter.ai + a gateway key in TAILOR_LLM_API_KEY)."""
        if os.environ.get("OPENROUTER_API_KEY"):
            return "openrouter"
        for k in ("SCORE_LLM_URL", "TAILOR_LLM_URL", "COVER_LLM_URL", "APPLY_LLM_URL", "ENRICH_LLM_URL"):
            if "openrouter.ai" in (os.environ.get(k, "") or ""):
                if os.environ.get("TAILOR_LLM_API_KEY") or os.environ.get("SCORE_LLM_API_KEY") or os.environ.get("APPLY_LLM_API_KEY"):
                    return "openrouter"
        return ""

    @app.get("/api/settings/llm")
    def get_llm_settings():
        or_route = _resolve_openrouter()
        # Prefer OpenRouter when it's configured for any stage: score/tailor/
        # cover/apply all route through it here, even if LLM_URL (the default
        # stage) also points at a local endpoint.
        provider = ("openrouter" if or_route else (
            "gemini" if os.environ.get("GEMINI_API_KEY") else (
                "openai" if os.environ.get("OPENAI_API_KEY") else (
                    "local" if os.environ.get("LLM_URL") else "none"
                )
            )
        ))
        eff_model = (os.environ.get("SCORE_LLM_MODEL")
                     or os.environ.get("TAILOR_LLM_MODEL")
                     or os.environ.get("COVER_LLM_MODEL")
                     or os.environ.get("LLM_MODEL") or "")
        return jsonify({
            "provider": provider,
            "gemini_key_set": bool(os.environ.get("GEMINI_API_KEY")),
            "openai_key_set": bool(os.environ.get("OPENAI_API_KEY")),
            "openrouter": or_route,
            "openrouter_key_set": bool(os.environ.get("OPENROUTER_API_KEY")
                                      or os.environ.get("TAILOR_LLM_API_KEY")
                                      or os.environ.get("SCORE_LLM_API_KEY")),
            "openrouter_model": eff_model,
            "llm_url": os.environ.get("LLM_URL", ""),
            "llm_model": os.environ.get("LLM_MODEL", ""),
            "llm_api_key_set": bool(os.environ.get("LLM_API_KEY")),
        })

    @app.post("/api/settings/llm")
    def set_llm_settings():
        data = request.get_json(force=True)
        provider = data.get("provider")
        updates: dict[str, str | None] = {}
        if provider == "openrouter":
            updates["SCORE_LLM_URL"] = "https://openrouter.ai/api/v1"
            updates["TAILOR_LLM_URL"] = "https://openrouter.ai/api/v1"
            updates["COVER_LLM_URL"] = "https://openrouter.ai/api/v1"
            if data.get("openrouter_key"):
                updates["TAILOR_LLM_API_KEY"] = data["openrouter_key"]
        elif provider == "local":
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
            "scam_blocked": conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE scam_verdict = 'blocked'"
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
<title>JobPilot - AI Job Search Assistant</title>
<style>
  :root {
    --bg: #f8fafc;
    --card: #ffffff;
    --border: #e2e8f0;
    --border-subtle: #f1f5f9;
    --text: #0f172a;
    --text-muted: #64748b;
    --text-faint: #94a3b8;
    --primary: #2563eb;
    --primary-hover: #1d4ed8;
    --primary-light: #eff6ff;
    --success: #16a34a;
    --success-light: #f0fdf4;
    --warning: #d97706;
    --warning-light: #fffbeb;
    --danger: #dc2626;
    --danger-hover: #b91c1c;
    --danger-light: #fef2f2;
    --radius-sm: 6px;
    --radius-md: 10px;
    --radius-lg: 14px;
    --radius-full: 9999px;
    --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
    --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.07), 0 2px 4px -2px rgba(0, 0, 0, 0.05);
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  header {
    background: #ffffff;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    z-index: 100;
    box-shadow: var(--shadow-sm);
  }
  .header-inner {
    max-width: 1160px;
    margin: 0 auto;
    padding: 0.85rem 1.5rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    text-decoration: none;
    color: var(--text);
  }
  .brand-logo {
    width: 36px;
    height: 36px;
    background: var(--primary);
    color: #ffffff;
    border-radius: 9px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 800;
    font-size: 1.15rem;
    box-shadow: 0 2px 6px rgba(37,99,235,0.3);
  }
  .brand-title {
    font-size: 1.25rem;
    font-weight: 800;
    letter-spacing: -0.02em;
    color: var(--text);
  }
  .brand-sub {
    font-size: 0.76rem;
    color: var(--text-muted);
    font-weight: 500;
  }
  nav {
    display: flex;
    background: #f1f5f9;
    padding: 0.25rem;
    border-radius: var(--radius-full);
    gap: 0.25rem;
  }
  nav button {
    background: transparent;
    border: none;
    color: var(--text-muted);
    padding: 0.55rem 1.25rem;
    border-radius: var(--radius-full);
    cursor: pointer;
    font-size: 0.92rem;
    font-weight: 600;
    transition: all 0.15s ease;
    display: flex;
    align-items: center;
    gap: 0.45rem;
  }
  nav button:hover:not(.active) {
    color: var(--text);
    background: rgba(255,255,255,0.7);
  }
  nav button.active {
    background: #ffffff;
    color: var(--primary);
    box-shadow: var(--shadow-sm);
  }
  .header-actions {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }
  .status-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    padding: 0.4rem 0.9rem;
    border-radius: var(--radius-full);
    font-size: 0.82rem;
    font-weight: 600;
    background: #f1f5f9;
    color: var(--text-muted);
  }
  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text-faint);
  }
  .status-badge.active {
    background: var(--success-light);
    color: var(--success);
  }
  .status-badge.active .status-dot {
    background: var(--success);
    animation: pulse 1.8s infinite;
  }
  .status-badge.stalled {
    background: var(--warning-light);
    color: var(--warning);
  }
  .status-badge.stalled .status-dot {
    background: var(--warning);
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.4; transform: scale(0.85); }
  }
  .btn-emergency {
    background: var(--danger);
    color: #ffffff;
    border: none;
    padding: 0.5rem 1.05rem;
    border-radius: var(--radius-md);
    font-size: 0.84rem;
    font-weight: 700;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    transition: background 0.15s ease, transform 0.1s ease;
  }
  .btn-emergency:hover {
    background: var(--danger-hover);
    transform: translateY(-1px);
  }
  .btn-emergency:active {
    transform: translateY(0);
  }

  main {
    max-width: 1160px;
    margin: 0 auto;
    padding: 1.75rem 1.5rem 3.5rem 1.5rem;
  }
  .tab-pane {
    display: none;
  }
  .tab-pane.active {
    display: block;
    animation: fadeIn 0.2s ease;
  }
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(3px); }
    to { opacity: 1; transform: translateY(0); }
  }

  /* Cards & Components */
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: 1.5rem;
    box-shadow: var(--shadow-sm);
    margin-bottom: 1.25rem;
  }
  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1rem;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .card-title {
    font-size: 1.15rem;
    font-weight: 700;
    color: var(--text);
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .card-sub {
    font-size: 0.85rem;
    color: var(--text-muted);
    margin-top: -0.5rem;
    margin-bottom: 1rem;
  }

  /* Hero & Status Card */
  .hero-card {
    background: linear-gradient(135deg, #ffffff 0%, #eff6ff 100%);
    border: 1px solid #bfdbfe;
    border-radius: var(--radius-lg);
    padding: 1.75rem;
    margin-bottom: 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
    box-shadow: 0 4px 12px rgba(37,99,235,0.06);
  }
  .hero-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1.25rem;
    flex-wrap: wrap;
  }
  .hero-info h2 {
    font-size: 1.45rem;
    font-weight: 800;
    color: var(--text);
    letter-spacing: -0.02em;
  }
  .hero-info p {
    font-size: 0.92rem;
    color: var(--text-muted);
    margin-top: 0.25rem;
    max-width: 580px;
  }
  .hero-controls {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
  }

  /* Buttons */
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    font-family: inherit;
    font-size: 0.92rem;
    font-weight: 600;
    padding: 0.65rem 1.25rem;
    border-radius: var(--radius-md);
    border: 1px solid transparent;
    cursor: pointer;
    transition: all 0.15s ease;
    text-decoration: none;
    line-height: 1.2;
  }
  .btn:active { transform: translateY(1px); }
  .btn-lg {
    padding: 0.85rem 1.65rem;
    font-size: 1.05rem;
    font-weight: 700;
    border-radius: var(--radius-md);
  }
  .btn-primary {
    background: var(--primary);
    color: #ffffff;
  }
  .btn-primary:hover {
    background: var(--primary-hover);
    box-shadow: 0 4px 12px rgba(37, 99, 235, 0.25);
  }
  .btn-secondary {
    background: #ffffff;
    color: var(--text);
    border-color: var(--border);
  }
  .btn-secondary:hover {
    background: #f8fafc;
    border-color: var(--text-faint);
  }
  .btn-success {
    background: var(--success);
    color: #ffffff;
  }
  .btn-success:hover {
    background: #15803d;
  }
  .btn-danger-outline {
    background: #ffffff;
    color: var(--danger);
    border-color: #fecaca;
  }
  .btn-danger-outline:hover {
    background: var(--danger-light);
    border-color: var(--danger);
  }
  .btn-sm {
    padding: 0.35rem 0.75rem;
    font-size: 0.82rem;
    border-radius: var(--radius-sm);
  }

  /* Control Switches Grid */
  .switches-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 0.9rem;
  }
  .switch-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.85rem 1.15rem;
    background: #ffffff;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    gap: 1rem;
  }
  .switch-label-group {
    display: flex;
    flex-direction: column;
  }
  .switch-title {
    font-size: 0.9rem;
    font-weight: 700;
    color: var(--text);
  }
  .switch-desc {
    font-size: 0.78rem;
    color: var(--text-muted);
    margin-top: 0.15rem;
  }
  .toggle-btn {
    background: #e2e8f0;
    border: none;
    color: var(--text-muted);
    font-weight: 700;
    padding: 0.45rem 1rem;
    border-radius: var(--radius-full);
    cursor: pointer;
    font-size: 0.82rem;
    transition: all 0.2s ease;
    white-space: nowrap;
  }
  .toggle-btn.on {
    background: var(--success);
    color: #ffffff;
  }

  /* Big Friendly Numbers Grid */
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 1rem;
    margin-bottom: 1.5rem;
  }
  .stat-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: 1.3rem 1.5rem;
    box-shadow: var(--shadow-sm);
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    transition: transform 0.15s ease, box-shadow 0.15s ease;
  }
  .stat-card:hover {
    transform: translateY(-2px);
    box-shadow: var(--shadow-md);
  }
  .stat-card-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .stat-icon {
    font-size: 1.6rem;
    line-height: 1;
  }
  .stat-num {
    font-size: 2.25rem;
    font-weight: 800;
    color: var(--text);
    letter-spacing: -0.03em;
    line-height: 1.1;
    margin-top: 0.35rem;
  }
  .stat-label {
    font-size: 0.95rem;
    font-weight: 700;
    color: var(--text);
    margin-top: 0.25rem;
  }
  .stat-desc {
    font-size: 0.78rem;
    color: var(--text-muted);
    margin-top: 0.25rem;
  }

  /* Step-by-step Visual Progress */
  .progress-steps {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 0.75rem;
    margin-top: 0.5rem;
  }
  .step-box {
    background: #f8fafc;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 0.9rem 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .step-box-num {
    font-size: 1.35rem;
    font-weight: 800;
    color: var(--primary);
  }
  .step-box-name {
    font-size: 0.85rem;
    font-weight: 700;
    color: var(--text);
  }
  .step-box-desc {
    font-size: 0.74rem;
    color: var(--text-muted);
  }

  /* Match Bar Chart */
  .match-bars {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }
  .match-bar-row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    font-size: 0.84rem;
  }
  .match-bar-label {
    width: 65px;
    font-weight: 600;
    color: var(--text-muted);
    text-align: right;
  }
  .match-bar-track {
    flex: 1;
    height: 11px;
    background: #f1f5f9;
    border-radius: var(--radius-full);
    overflow: hidden;
  }
  .match-bar-fill {
    height: 100%;
    border-radius: var(--radius-full);
    transition: width 0.4s ease;
  }
  .match-bar-val {
    width: 60px;
    font-size: 0.8rem;
    color: var(--text-muted);
  }

  /* Search & Filter Bar */
  .filter-bar {
    display: flex;
    gap: 0.75rem;
    align-items: center;
    flex-wrap: wrap;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: 1rem 1.25rem;
    margin-bottom: 1.25rem;
    box-shadow: var(--shadow-sm);
  }
  .filter-input {
    flex: 1;
    min-width: 200px;
    background: #ffffff;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 0.6rem 0.9rem;
    font-size: 0.9rem;
    color: var(--text);
    font-family: inherit;
  }
  .filter-input:focus {
    outline: none;
    border-color: var(--primary);
  }
  .filter-select {
    background: #ffffff;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 0.6rem 0.9rem;
    font-size: 0.88rem;
    color: var(--text);
    font-family: inherit;
    cursor: pointer;
  }
  .filter-select:focus {
    outline: none;
    border-color: var(--primary);
  }

  /* Job Cards List */
  .job-list {
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
  }
  .job-item {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: 1.3rem 1.5rem;
    box-shadow: var(--shadow-sm);
    display: flex;
    flex-direction: column;
    gap: 0.8rem;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }
  .job-item:hover {
    border-color: #cbd5e1;
    box-shadow: var(--shadow-md);
  }
  .job-top {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
  }
  .job-title-link {
    font-size: 1.15rem;
    font-weight: 700;
    color: var(--primary);
    text-decoration: none;
    line-height: 1.3;
  }
  .job-title-link:hover {
    text-decoration: underline;
  }
  .job-meta {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    font-size: 0.84rem;
    color: var(--text-muted);
    margin-top: 0.25rem;
    flex-wrap: wrap;
  }
  .job-pill {
    background: #f1f5f9;
    padding: 0.2rem 0.65rem;
    border-radius: var(--radius-full);
    font-weight: 600;
    color: var(--text-muted);
    font-size: 0.75rem;
  }
  .match-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.35rem 0.85rem;
    border-radius: var(--radius-full);
    font-size: 0.84rem;
    font-weight: 700;
    white-space: nowrap;
  }
  .match-great {
    background: var(--success-light);
    color: var(--success);
    border: 1px solid #bbf7d0;
  }
  .match-good {
    background: var(--warning-light);
    color: var(--warning);
    border: 1px solid #fde68a;
  }
  .match-fair {
    background: #f1f5f9;
    color: var(--text-muted);
    border: 1px solid var(--border);
  }
  .job-reason {
    font-size: 0.84rem;
    color: var(--text-muted);
    background: #f8fafc;
    border-left: 3px solid var(--primary);
    padding: 0.55rem 0.85rem;
    border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
    line-height: 1.45;
  }
  .job-bottom {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    border-top: 1px solid #f1f5f9;
    padding-top: 0.75rem;
    flex-wrap: wrap;
  }
  .job-docs {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
    font-size: 0.8rem;
  }
  .doc-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    background: #f8fafc;
    border: 1px solid var(--border);
    padding: 0.3rem 0.65rem;
    border-radius: var(--radius-sm);
    color: var(--text);
    text-decoration: none;
    font-weight: 500;
    font-size: 0.78rem;
  }
  .doc-btn:hover {
    background: #f1f5f9;
    border-color: var(--text-faint);
  }
  .job-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  /* Safety & Scam Warnings */
  .scam-warning-box {
    background: #fef2f2;
    border: 1px solid #fecaca;
    border-left: 4px solid var(--danger);
    border-radius: var(--radius-sm);
    padding: 0.65rem 0.9rem;
    font-size: 0.84rem;
    color: #991b1b;
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }
  .scam-warning-title {
    font-weight: 700;
    display: flex;
    align-items: center;
    gap: 0.35rem;
  }
  .scam-warning-reason {
    font-weight: 500;
  }
  .scam-warning-detail {
    font-size: 0.8rem;
    color: #7f1d1d;
    background: #ffffff;
    border: 1px solid #fee2e2;
    padding: 0.4rem 0.6rem;
    border-radius: 4px;
    margin-top: 0.2rem;
    line-height: 1.4;
  }
  .scam-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    background: #fee2e2;
    color: #991b1b;
    border: 1px solid #fca5a5;
    padding: 0.2rem 0.6rem;
    border-radius: var(--radius-full);
    font-size: 0.78rem;
    font-weight: 700;
  }
  .nav-count-badge {
    background: var(--danger);
    color: #ffffff;
    font-size: 0.7rem;
    font-weight: 700;
    padding: 0.1rem 0.45rem;
    border-radius: 10px;
    margin-left: 0.25rem;
  }

  /* Empty States */
  .empty-state {
    text-align: center;
    padding: 3.5rem 1.5rem;
    background: var(--card);
    border: 1px dashed var(--border);
    border-radius: var(--radius-lg);
  }
  .empty-icon {
    font-size: 2.8rem;
    margin-bottom: 0.75rem;
  }
  .empty-title {
    font-size: 1.25rem;
    font-weight: 700;
    color: var(--text);
    margin-bottom: 0.4rem;
  }
  .empty-desc {
    font-size: 0.92rem;
    color: var(--text-muted);
    max-width: 480px;
    margin: 0 auto 1.5rem auto;
    line-height: 1.5;
  }

  /* Form & Settings Elements */
  .form-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 1rem;
    margin-bottom: 1rem;
  }
  .form-group {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }
  .form-group label {
    font-size: 0.84rem;
    font-weight: 600;
    color: var(--text);
  }
  .form-group input, .form-group select, .form-group textarea {
    background: #ffffff;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 0.65rem 0.9rem;
    font-size: 0.9rem;
    color: var(--text);
    font-family: inherit;
    transition: border-color 0.15s ease;
  }
  .form-group input:focus, .form-group select:focus, .form-group textarea:focus {
    outline: none;
    border-color: var(--primary);
  }
  .form-group textarea {
    min-height: 180px;
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-size: 0.82rem;
    line-height: 1.45;
  }
  .tag-container {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin-top: 0.5rem;
  }
  .tag {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    background: #f1f5f9;
    border: 1px solid var(--border);
    padding: 0.3rem 0.75rem;
    border-radius: var(--radius-full);
    font-size: 0.82rem;
    font-weight: 500;
    color: var(--text);
  }
  .tag-close {
    cursor: pointer;
    font-weight: 700;
    color: var(--text-muted);
    font-size: 1rem;
    line-height: 1;
  }
  .tag-close:hover {
    color: var(--danger);
  }

  /* Toast Notification */
  .toast {
    position: fixed;
    bottom: 2rem;
    right: 2rem;
    background: #1e293b;
    color: #ffffff;
    padding: 0.85rem 1.4rem;
    border-radius: var(--radius-md);
    font-size: 0.9rem;
    font-weight: 600;
    box-shadow: var(--shadow-md);
    opacity: 0;
    transform: translateY(10px);
    transition: all 0.25s ease;
    pointer-events: none;
    z-index: 1000;
  }
  .toast.show {
    opacity: 1;
    transform: translateY(0);
  }

  /* Activity Feed */
  .activity-box {
    background: #0f172a;
    color: #e2e8f0;
    border-radius: var(--radius-md);
    padding: 1.1rem;
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-size: 0.8rem;
    line-height: 1.5;
    max-height: 240px;
    overflow-y: auto;
    white-space: pre-wrap;
  }

  /* Collapsible Accordion */
  .accordion-toggle {
    background: transparent;
    border: none;
    color: var(--primary);
    font-size: 0.88rem;
    font-weight: 600;
    cursor: pointer;
    padding: 0.4rem 0;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  .accordion-toggle:hover {
    text-decoration: underline;
  }

  @media (max-width: 768px) {
    .header-inner { padding: 0.75rem 1rem; }
    main { padding: 1rem; }
    .hero-top { flex-direction: column; align-items: flex-start; }
    .job-top { flex-direction: column; }
    .job-bottom { flex-direction: column; align-items: flex-start; }
    .stats-grid { grid-template-columns: 1fr 1fr; }
  }
  @media (max-width: 480px) {
    .stats-grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>

<header>
  <div class="header-inner">
    <a href="#" class="brand" onclick="switchTab('home'); return false;">
      <div class="brand-logo">JP</div>
      <div>
        <div class="brand-title">JobPilot</div>
        <div class="brand-sub">Your AI Job Search Assistant</div>
      </div>
    </a>

    <nav>
      <button data-tab="home" class="active" onclick="switchTab('home')">🏠 Home</button>
      <button data-tab="jobs" onclick="switchTab('jobs')">💼 My Jobs</button>
      <button data-tab="safety" onclick="switchTab('safety')">🛡️ Safety</button>
      <button data-tab="settings" onclick="switchTab('settings')">⚙️ Settings</button>
    </nav>

    <div class="header-actions">
      <div id="header-status" class="status-badge">
        <span class="status-dot"></span>
        <span id="header-status-text">Ready</span>
      </div>
      <button class="btn-emergency" onclick="emergencyStop()" title="Immediately stops all searches and applications">
        🛑 Stop All
      </button>
    </div>
  </div>
</header>

<main>

  <!-- ================= TAB 1: HOME ================= -->
  <section id="tab-home" class="tab-pane active">

    <!-- Welcome & Control Hero -->
    <div class="hero-card">
      <div class="hero-top">
        <div class="hero-info">
          <h2>Job Search Assistant</h2>
          <p>JobPilot automatically discovers job openings, matches them to your background, prepares customized resumes, and helps you apply effortlessly.</p>
        </div>
        <div class="hero-controls">
          <button id="btn-main-search" class="btn btn-primary btn-lg" onclick="toggleMainSearch()">
            🚀 Start Finding Jobs
          </button>
          <button id="btn-quick-search" class="btn btn-secondary btn-lg" onclick="runQuickSearch()" title="Run a single search pass now">
            ⚡ Quick Search
          </button>
        </div>
      </div>

      <!-- Controls & Toggles -->
      <div class="switches-grid">
        <div class="switch-row">
          <div class="switch-label-group">
            <span class="switch-title">Automatic Submissions</span>
            <span class="switch-desc" id="auto-apply-desc">OFF — Prepares applications for your review in My Jobs</span>
          </div>
          <button id="auto-apply-toggle-btn" class="toggle-btn" onclick="toggleAutoApplyMode()">Enable</button>
        </div>

        <div class="switch-row">
          <div class="switch-label-group">
            <span class="switch-title">Application Mode</span>
            <span class="switch-desc" id="live-mode-desc">Test Drafts (Dry-run) — No real submissions</span>
          </div>
          <button id="live-mode-toggle-btn" class="btn btn-secondary btn-sm" onclick="toggleLiveMode()">Switch to Live</button>
        </div>
      </div>
    </div>

    <!-- 4 Big Friendly Numbers -->
    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-card-top">
          <span class="stat-label">Jobs Found</span>
          <span class="stat-icon">💼</span>
        </div>
        <div class="stat-num" id="stat-found">0</div>
        <div class="stat-desc">Total job postings discovered</div>
      </div>

      <div class="stat-card">
        <div class="stat-card-top">
          <span class="stat-label">Top Matches</span>
          <span class="stat-icon">⭐</span>
        </div>
        <div class="stat-num" id="stat-matches" style="color:var(--success);">0</div>
        <div class="stat-desc">Highly rated for your profile</div>
      </div>

      <div class="stat-card">
        <div class="stat-card-top">
          <span class="stat-label">Ready to Apply</span>
          <span class="stat-icon">📝</span>
        </div>
        <div class="stat-num" id="stat-ready" style="color:var(--primary);">0</div>
        <div class="stat-desc">Resume & letter customized</div>
      </div>

      <div class="stat-card">
        <div class="stat-card-top">
          <span class="stat-label">Applications Sent</span>
          <span class="stat-icon">🚀</span>
        </div>
        <div class="stat-num" id="stat-applied" style="color:#059669;">0</div>
        <div class="stat-desc">Successfully submitted</div>
      </div>
    </div>

    <!-- Step-by-Step Progress -->
    <div class="card">
      <div class="card-header">
        <span class="card-title">📊 How Your Search Is Progressing</span>
      </div>
      <div class="progress-steps">
        <div class="step-box">
          <span class="step-box-num" id="step-discovered">0</span>
          <span class="step-box-name">1. Found Listings</span>
          <span class="step-box-desc">Searched across job sites</span>
        </div>
        <div class="step-box">
          <span class="step-box-num" id="step-enriched">0</span>
          <span class="step-box-name">2. Details Gathered</span>
          <span class="step-box-desc">Full job descriptions loaded</span>
        </div>
        <div class="step-box">
          <span class="step-box-num" id="step-scored">0</span>
          <span class="step-box-name">3. Match Evaluated</span>
          <span class="step-box-desc">Rated against your background</span>
        </div>
        <div class="step-box">
          <span class="step-box-num" id="step-tailored">0</span>
          <span class="step-box-name">4. Resumes Prepared</span>
          <span class="step-box-desc">Custom resume & letter ready</span>
        </div>
        <div class="step-box">
          <span class="step-box-num" id="step-applied">0</span>
          <span class="step-box-name">5. Applications Sent</span>
          <span class="step-box-desc">Completed job submissions</span>
        </div>
      </div>
    </div>

    <!-- Match Quality Breakdown -->
    <div class="card">
      <div class="card-header">
        <span class="card-title">🎯 Match Quality Breakdown</span>
      </div>
      <p class="card-sub">Distribution of how closely discovered jobs match your target role and skills.</p>
      <div id="match-bars-container" class="match-bars">
        <p style="color:var(--text-muted);font-size:0.88rem;">No rated jobs yet. Click 'Start Finding Jobs' to begin.</p>
      </div>
      <p id="match-dist-note" style="font-size:0.78rem;color:var(--text-muted);margin-top:0.75rem;"></p>
    </div>

    <!-- Recent Updates (Activity Log) -->
    <div class="card">
      <div class="card-header">
        <span class="card-title">📋 Recent Updates</span>
        <span id="activity-status-badge" class="status-badge">Idle</span>
      </div>
      <div id="activity-log-box" class="activity-box">Starting assistant logs...</div>
    </div>

  </section>


  <!-- ================= TAB 2: MY JOBS ================= -->
  <section id="tab-jobs" class="tab-pane">

    <!-- Search & Filter Controls -->
    <div class="filter-bar">
      <input type="text" id="job-search-input" class="filter-input" placeholder="Search by job title or keyword..." oninput="filterJobsClientSide()">

      <select id="job-score-filter" class="filter-select" onchange="loadJobs()">
        <option value="0">All Match Ratings</option>
        <option value="8">Top Matches Only (8-10)</option>
        <option value="6" selected>Good Matches (6-10)</option>
        <option value="4">Moderate & Above (4-10)</option>
      </select>

      <input type="text" id="job-location-input" class="filter-input" placeholder="Location (e.g. your city, Remote)" style="max-width:240px;">

      <button class="btn btn-secondary btn-sm" onclick="useTargetLocationFilter()">
        📍 Use Saved Location
      </button>

      <button class="btn btn-primary btn-sm" onclick="loadJobs()">
        🔄 Refresh
      </button>
    </div>

    <!-- Job Count Summary -->
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:0.75rem;padding:0 0.25rem;">
      <span id="jobs-count-text" style="font-size:0.9rem;font-weight:600;color:var(--text-muted);">Loading jobs...</span>
    </div>

    <!-- Jobs List Container -->
    <div id="jobs-container" class="job-list">
      <!-- Jobs will be rendered here dynamically -->
    </div>

  </section>


  <!-- ================= TAB 3: SAFETY / FLAGGED JOBS ================= -->
  <section id="tab-safety" class="tab-pane">

    <!-- Safety Intro Card -->
    <div class="card" style="border-left: 4px solid var(--danger);">
      <div class="card-header">
        <span class="card-title">🛡️ Scam Screening & Flagged Jobs</span>
        <button class="btn btn-secondary btn-sm" onclick="loadSafetyJobs()">
          🔄 Refresh
        </button>
      </div>
      <p class="card-sub" style="margin-bottom:0.25rem;">
        JobPilot screens job listings for suspicious fee demands, fake recruiter patterns, and off-platform pivots. Flagged jobs are blocked from automatic applications.
      </p>
    </div>

    <!-- Safety Job Count Summary -->
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:0.75rem;padding:0 0.25rem;">
      <span id="safety-count-text" style="font-size:0.9rem;font-weight:600;color:var(--text-muted);">Loading flagged jobs...</span>
    </div>

    <!-- Flagged Jobs List Container -->
    <div id="safety-jobs-container" class="job-list">
      <!-- Flagged jobs will be rendered here dynamically -->
    </div>

  </section>


  <!-- ================= TAB 4: SETTINGS ================= -->
  <section id="tab-settings" class="tab-pane">

    <!-- Multi-User Profiles -->
    <div class="card">
      <div class="card-header">
        <span class="card-title">👤 User Profiles</span>
      </div>
      <p class="card-sub">Switch between different user profiles or create a new one for someone else.</p>
      <div class="form-grid">
        <div class="form-group">
          <label>Active Profile</label>
          <div style="display:flex;gap:0.5rem;">
            <select id="profile-select" style="flex:1;"></select>
            <button class="btn btn-secondary btn-sm" onclick="switchProfile()">Switch</button>
          </div>
        </div>
        <div class="form-group">
          <label>Create New Profile</label>
          <div style="display:flex;gap:0.5rem;">
            <input type="text" id="new-profile-name" placeholder="e.g. Jane">
            <button class="btn btn-primary btn-sm" onclick="createProfile()">Create</button>
          </div>
        </div>
      </div>
      <p id="profile-status" style="font-size:0.82rem;color:var(--text-muted);"></p>
    </div>

    <!-- Personal Profile & Resume Info -->
    <div class="card">
      <div class="card-header">
        <span class="card-title">📄 Profile & Resume Details</span>
      </div>
      <p class="card-sub">JobPilot uses this information to match jobs and customize your resume for applications.</p>

      <div class="form-grid">
        <div class="form-group">
          <label>Full Name</label>
          <input type="text" id="p-full_name" placeholder="e.g. Jane Doe">
        </div>
        <div class="form-group">
          <label>Preferred Name</label>
          <input type="text" id="p-preferred_name" placeholder="e.g. Jane">
        </div>
        <div class="form-group">
          <label>Email Address</label>
          <input type="text" id="p-email" placeholder="jane@example.com">
        </div>
        <div class="form-group">
          <label>Phone Number</label>
          <input type="text" id="p-phone" placeholder="+1 (555) 000-0000">
        </div>
        <div class="form-group">
          <label>City</label>
          <input type="text" id="p-city" placeholder="e.g. your city">
        </div>
        <div class="form-group">
          <label>Country</label>
          <input type="text" id="p-country" placeholder="e.g. your country">
        </div>
        <div class="form-group">
          <label>LinkedIn Profile</label>
          <input type="text" id="p-linkedin_url" placeholder="https://linkedin.com/in/username">
        </div>
      </div>

      <div class="card-title" style="font-size:1rem;margin:1.25rem 0 0.5rem 0;">💼 Career & Work Authorization</div>
      <div class="form-grid">
        <div class="form-group">
          <label>Target Job Title / Role</label>
          <input type="text" id="p-target_role" placeholder="e.g. Senior Software Engineer">
        </div>
        <div class="form-group">
          <label>Total Years of Experience</label>
          <input type="text" id="p-years_of_experience_total" placeholder="e.g. 5">
        </div>
        <div class="form-group">
          <label>Education Level</label>
          <input type="text" id="p-education_level" placeholder="e.g. Bachelor's in Computer Science">
        </div>
        <div class="form-group">
          <label>Legally Authorized to Work?</label>
          <select id="p-legally_authorized_to_work">
            <option value="Yes">Yes</option>
            <option value="No">No</option>
          </select>
        </div>
        <div class="form-group">
          <label>Require Visa Sponsorship?</label>
          <select id="p-require_sponsorship">
            <option value="No">No</option>
            <option value="Yes">Yes</option>
          </select>
        </div>
        <div class="form-group">
          <label>Work Permit Type</label>
          <input type="text" id="p-work_permit_type" placeholder="e.g. Citizen, Permanent Resident, etc.">
        </div>
      </div>

      <div class="card-title" style="font-size:1rem;margin:1.25rem 0 0.5rem 0;">💰 Compensation Expectations</div>
      <div class="form-grid">
        <div class="form-group">
          <label>Currency</label>
          <input type="text" id="p-salary_currency" placeholder="e.g. AED or USD">
        </div>
        <div class="form-group">
          <label>Target Salary</label>
          <input type="text" id="p-salary_expectation" placeholder="e.g. 120000">
        </div>
        <div class="form-group">
          <label>Minimum Acceptable</label>
          <input type="text" id="p-salary_range_min" placeholder="e.g. 100000">
        </div>
        <div class="form-group">
          <label>Maximum Range</label>
          <input type="text" id="p-salary_range_max" placeholder="e.g. 150000">
        </div>
      </div>

      <div style="margin-top:1.25rem;display:flex;align-items:center;gap:1rem;flex-wrap:wrap;">
        <button class="btn btn-primary" onclick="saveProfileForm()">
          💾 Save Profile Information
        </button>
        <button class="accordion-toggle" onclick="toggleRawProfile()" id="raw-profile-toggle">
          ⚙️ Advanced: View / Edit Full Profile Data (JSON)
        </button>
      </div>

      <div id="raw-profile-wrap" style="display:none;margin-top:1rem;">
        <p style="font-size:0.8rem;color:var(--text-muted);margin-bottom:0.5rem;">
          Full profile including resume history, skills, and facts. Editing here overrides form fields on save.
        </p>
        <textarea id="profile-json" class="form-group" style="width:100%;height:220px;"></textarea>
        <div style="margin-top:0.5rem;">
          <button class="btn btn-secondary btn-sm" onclick="saveProfile()">Save Raw Profile JSON</button>
        </div>
      </div>
    </div>

    <!-- Location & Search Preferences -->
    <div class="card">
      <div class="card-header">
        <span class="card-title">📍 Location & Search Preferences</span>
      </div>
      <p class="card-sub">Choose where you want to find jobs.</p>

      <div style="display:flex;gap:0.6rem;margin-bottom:1rem;flex-wrap:wrap;">
        <button class="btn btn-secondary btn-sm" onclick="applyLocationPreset('mycity')">🏠 My City + Remote</button>
        <button class="btn btn-secondary btn-sm" onclick="applyLocationPreset('remote')">🌍 Remote Only</button>
      </div>

      <div class="form-grid">
        <div class="form-group">
          <label>Primary Location</label>
          <input type="text" id="loc-primary" placeholder="e.g. your city, your country">
        </div>
        <div class="form-group">
          <label>Include Remote Jobs?</label>
          <select id="loc-remote">
            <option value="true">Yes — Include Remote Positions</option>
            <option value="false">No — Onsite / Hybrid Only</option>
          </select>
        </div>
      </div>

      <div class="form-group" style="margin-bottom:1rem;">
        <label>Accepted Cities & Countries</label>
        <div style="display:flex;gap:0.5rem;">
          <input type="text" id="loc-accept-input" placeholder="Add a city or country, e.g. Abu Dhabi" style="flex:1;">
          <button class="btn btn-secondary btn-sm" onclick="addAcceptedLocation()">+ Add Location</button>
        </div>
        <div id="loc-accept-tags" class="tag-container"></div>
      </div>

      <div style="display:flex;align-items:center;gap:1rem;flex-wrap:wrap;">
        <button class="btn btn-primary" onclick="saveLocation()">
          💾 Save Location Preferences
        </button>
        <button class="accordion-toggle" onclick="toggleRawSearches()" id="raw-searches-toggle">
          ⚙️ Advanced: Search Queries & Keywords (YAML)
        </button>
      </div>

      <div id="raw-searches-wrap" style="display:none;margin-top:1rem;">
        <p style="font-size:0.8rem;color:var(--text-muted);margin-bottom:0.5rem;">
          Configure specific search queries, websites, and search filters.
        </p>
        <textarea id="searches-yaml" class="form-group" style="width:100%;height:220px;"></textarea>
        <div style="margin-top:0.5rem;">
          <button class="btn btn-secondary btn-sm" onclick="saveSearches()">Save Search Config</button>
        </div>
      </div>
    </div>

    <!-- AI Assistant Setup -->
    <div class="card">
      <div class="card-header">
        <span class="card-title">🤖 AI Assistant Settings</span>
      </div>
      <p class="card-sub">Choose which AI service analyzes jobs and customizes resumes. Keys are stored locally on your computer.</p>

      <div class="form-grid">
        <div class="form-group">
          <label>AI Provider</label>
          <select id="llm-provider" onchange="updateLlmRows()">
            <option value="openrouter">OpenRouter (Cloud — one key, any model)</option>
            <option value="gemini">Google Gemini (Cloud)</option>
            <option value="openai">OpenAI (Cloud)</option>
            <option value="local">Local AI (Ollama / llama.cpp)</option>
          </select>
        </div>
        <div class="form-group" id="row-gemini-key">
          <label>Google Gemini API Key</label>
          <input type="password" id="gemini-key" placeholder="(Keep blank to leave unchanged)">
        </div>
        <div class="form-group" id="row-openrouter-key" style="display:none;">
          <label>OpenRouter API Key</label>
          <input type="password" id="openrouter-key" placeholder="sk-or-v1-... (keep blank to leave unchanged)">
          <p class="card-sub">Best choice — one key works with all models.</p>
        </div>
        <div class="form-group" id="row-openai-key" style="display:none;">
          <label>OpenAI API Key</label>
          <input type="password" id="openai-key" placeholder="(Keep blank to leave unchanged)">
        </div>
        <div class="form-group" id="row-local-url" style="display:none;">
          <label>Local AI Server Address</label>
          <input type="text" id="llm-url" placeholder="http://127.0.0.1:8080/v1">
        </div>
        <div class="form-group" id="row-local-key" style="display:none;">
          <label>Local AI API Key (Optional)</label>
          <input type="password" id="llm-api-key" placeholder="(Optional, leave blank if none)">
        </div>
        <div class="form-group">
          <label>AI Model Name</label>
          <input type="text" id="llm-model" placeholder="e.g. gemini-2.0-flash">
        </div>
      </div>

      <div style="display:flex;align-items:center;justify-content:space-between;margin-top:0.5rem;flex-wrap:wrap;gap:0.75rem;">
        <button class="btn btn-primary" onclick="saveLlm()">
          💾 Save AI Settings
        </button>
        <span id="llm-current" style="font-size:0.82rem;color:var(--text-muted);"></span>
      </div>
    </div>

  </section>

</main>

<!-- Toast Notification -->
<div id="toast-msg" class="toast">✓ Saved successfully</div>

<script>
function $(id) { return document.getElementById(id); }

function showToast(msg) {
  const t = $('toast-msg');
  t.textContent = msg || '✓ Saved successfully';
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}

// Tab Switching
function switchTab(tabName) {
  document.querySelectorAll('nav button').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tabName);
  });
  document.querySelectorAll('.tab-pane').forEach(p => {
    p.classList.toggle('active', p.id === 'tab-' + tabName);
  });
  if (tabName === 'home') loadHome();
  if (tabName === 'jobs') loadJobs();
  if (tabName === 'safety') loadSafetyJobs();
  if (tabName === 'settings') loadSettings();
}

// ---------------------------------------------------------------------------
// Home Tab Logic & Assistant Controls
// ---------------------------------------------------------------------------
let _isAssistantRunning = false;

async function loadHome() {
  try {
    const [stats, stageProg, loopStatus, autoApplyStatus, health] = await Promise.all([
      fetch('/api/stats').then(r => r.json()).catch(() => ({})),
      fetch('/api/stage-progress').then(r => r.json()).catch(() => ({})),
      fetch('/api/loop/status').then(r => r.json()).catch(() => ({})),
      fetch('/api/auto-apply/status').then(r => r.json()).catch(() => ({})),
      fetch('/api/health').then(r => r.json()).catch(() => ({})),
    ]);

    // Update Big Stat Numbers
    const totalFound = stageProg.discovered ?? stats.total ?? 0;
    const topMatches = stageProg.high_fit ?? stats.scored ?? 0;
    const readyToApply = stageProg.ready_to_apply ?? stats.ready_to_apply ?? 0;
    const appliedCount = stageProg.applied ?? stats.applied ?? 0;

    $('stat-found').textContent = totalFound.toLocaleString();
    $('stat-matches').textContent = topMatches.toLocaleString();
    $('stat-ready').textContent = readyToApply.toLocaleString();
    $('stat-applied').textContent = appliedCount.toLocaleString();

    // Step by step progress
    $('step-discovered').textContent = (stageProg.discovered ?? stats.total ?? 0).toLocaleString();
    $('step-enriched').textContent = (stageProg.enriched ?? stats.with_description ?? 0).toLocaleString();
    $('step-scored').textContent = (stageProg.scored ?? stats.scored ?? 0).toLocaleString();
    $('step-tailored').textContent = (stageProg.tailored ?? stats.tailored ?? 0).toLocaleString();
    $('step-applied').textContent = (stageProg.applied ?? stats.applied ?? 0).toLocaleString();

    // Loop & Assistant Status
    _isAssistantRunning = !!(loopStatus.running || (health.loops && health.loops.some(l => l.state === 'RUNNING')));
    const isStalled = !!loopStatus.stalled;

    const headerBadge = $('header-status');
    const headerText = $('header-status-text');
    const mainBtn = $('btn-main-search');

    if (isStalled) {
      headerBadge.className = 'status-badge stalled';
      headerText.textContent = 'Attention Needed';
      mainBtn.textContent = '⏸️ Stop Assistant';
      mainBtn.className = 'btn btn-danger-outline btn-lg';
    } else if (_isAssistantRunning) {
      headerBadge.className = 'status-badge active';
      headerText.textContent = 'Searching & Preparing';
      mainBtn.textContent = '⏸️ Pause Assistant';
      mainBtn.className = 'btn btn-danger-outline btn-lg';
    } else {
      headerBadge.className = 'status-badge';
      headerText.textContent = 'Ready';
      mainBtn.textContent = '🚀 Start Finding Jobs';
      mainBtn.className = 'btn btn-primary btn-lg';
    }

    // Auto-Apply Toggle state
    const autoBtn = $('auto-apply-toggle-btn');
    const autoDesc = $('auto-apply-desc');
    if (autoApplyStatus.enabled) {
      autoBtn.textContent = 'Disable';
      autoBtn.className = 'toggle-btn on';
      autoDesc.textContent = 'ON — Prepares and applies automatically to top matching jobs';
    } else {
      autoBtn.textContent = 'Enable';
      autoBtn.className = 'toggle-btn';
      autoDesc.textContent = 'OFF — Prepares applications for your review in My Jobs';
    }

    // Live vs Dry-run mode
    const isLive = !!loopStatus.live;
    const liveBtn = $('live-mode-toggle-btn');
    const liveDesc = $('live-mode-desc');
    if (isLive) {
      liveBtn.textContent = 'Switch to Test Mode';
      liveBtn.className = 'btn btn-danger-outline btn-sm';
      liveDesc.textContent = 'Real Applications (LIVE) — Submits applications to real job sites';
    } else {
      liveBtn.textContent = 'Switch to Live';
      liveBtn.className = 'btn btn-secondary btn-sm';
      liveDesc.textContent = 'Test Drafts (Dry-run) — Fills out forms without submitting';
    }

    // Match Distribution Chart
    renderMatchDistribution(stats.score_distribution || [], stats.skipped || 0);

    // Update Safety tab badge if any jobs blocked
    const safetyBtn = document.querySelector('nav button[data-tab="safety"]');
    if (safetyBtn) {
      const blockedCount = stageProg.scam_blocked || 0;
      safetyBtn.innerHTML = blockedCount > 0
        ? `🛡️ Safety <span class="nav-count-badge">${blockedCount}</span>`
        : `🛡️ Safety`;
    }

  } catch (err) {
    console.error('loadHome error:', err);
  }
}

function renderMatchDistribution(dist, skipped) {
  const container = $('match-bars-container');
  if (!dist.length) {
    container.innerHTML = '<p style="color:var(--text-muted);font-size:0.88rem;">No rated jobs yet. Click "Start Finding Jobs" above to begin.</p>';
    $('match-dist-note').textContent = '';
    return;
  }

  const maxCount = Math.max(1, ...dist.map(([, c]) => c));
  container.innerHTML = dist.map(([score, count]) => {
    const color = score >= 8 ? 'var(--success)' : (score >= 6 ? 'var(--warning)' : '#94a3b8');
    const pct = Math.min(100, Math.round((count / maxCount) * 100));
    return `
      <div class="match-bar-row">
        <span class="match-bar-label">${score} / 10</span>
        <div class="match-bar-track">
          <div class="match-bar-fill" style="width:${pct}%;background:${color};"></div>
        </div>
        <span class="match-bar-val">${count} ${count === 1 ? 'job' : 'jobs'}</span>
      </div>
    `;
  }).join('');

  if (skipped > 0) {
    $('match-dist-note').textContent = `+ ${skipped.toLocaleString()} off-target job postings were filtered out automatically.`;
  } else {
    $('match-dist-note').textContent = '';
  }
}

async function toggleMainSearch() {
  if (_isAssistantRunning) {
    await fetch('/api/loop/stop', { method: 'POST' });
    await fetch('/api/pipeline/stop', { method: 'POST' });
    showToast('Assistant paused.');
  } else {
    await fetch('/api/loop/start', { method: 'POST' });
    showToast('Assistant started searching!');
  }
  await loadHome();
}

async function runQuickSearch() {
  showToast('Starting a quick search & match pass...');
  const res = await fetch('/api/pipeline/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      stages: ['discover', 'enrich', 'score', 'tailor', 'cover', 'pdf'],
      stream: true,
      workers: 4
    })
  });
  if (!res.ok) {
    const err = await res.json();
    alert(err.error || 'Could not start search.');
  } else {
    showToast('Quick search is running!');
  }
  await loadHome();
}

async function emergencyStop() {
  await Promise.all([
    fetch('/api/loop/stop', { method: 'POST' }).catch(() => {}),
    fetch('/api/pipeline/stop', { method: 'POST' }).catch(() => {})
  ]);
  showToast('🛑 All searches and applications stopped immediately.');
  await loadHome();
}

async function toggleAutoApplyMode() {
  const btn = $('auto-apply-toggle-btn');
  const isCurrentlyOn = btn.classList.contains('on');
  const newEnabled = !isCurrentlyOn;
  try {
    await fetch('/api/auto-apply/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: newEnabled })
    });
    showToast(newEnabled ? 'Automatic applications enabled.' : 'Automatic applications disabled.');
    await loadHome();
  } catch (e) {
    console.error('toggleAutoApply error:', e);
  }
}

async function toggleLiveMode() {
  const status = await (await fetch('/api/loop/status')).json();
  const willBeLive = !status.live;
  if (willBeLive) {
    const confirmed = confirm('Turn on LIVE application submissions? JobPilot will submit real job applications to employer websites on your behalf.');
    if (!confirmed) return;
  }
  await fetch('/api/loop/live', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ live: willBeLive })
  });
  showToast(willBeLive ? 'Switched to LIVE submissions.' : 'Switched to Test Drafts (Dry-run).');
  await loadHome();
}

// ---------------------------------------------------------------------------
// Activity Feed
// ---------------------------------------------------------------------------
let _lastActivityLog = '';
async function loadActivity() {
  try {
    const d = await (await fetch('/api/activity')).json();
    const feed = $('activity-log-box');
    const logs = d.logs || [];
    const newLog = logs.join('\n');
    if (newLog !== _lastActivityLog) {
      _lastActivityLog = newLog;
      feed.textContent = newLog || 'Assistant is ready for new tasks.';
      feed.scrollTop = feed.scrollHeight;

      const hasActivity = logs.some(l => l.includes('---') || l.includes('LIVE') || l.includes('submit') || l.includes('stage'));
      const statusBadge = $('activity-status-badge');
      if (statusBadge) {
        if (hasActivity) {
          statusBadge.textContent = 'Active';
          statusBadge.className = 'status-badge active';
        } else {
          statusBadge.textContent = 'Idle';
          statusBadge.className = 'status-badge';
        }
      }
    }
  } catch (e) {
    console.error('loadActivity error:', e);
  }
}

// ---------------------------------------------------------------------------
// Tab 2: My Jobs Logic
// ---------------------------------------------------------------------------
let _allLoadedJobs = [];

async function useTargetLocationFilter() {
  try {
    const loc = await (await fetch('/api/settings/location')).json();
    const terms = [...(loc.location_accept || [])];
    if (loc.remote_enabled) terms.push('Remote');
    $('job-location-input').value = terms.join(', ');
    await loadJobs();
  } catch (e) {
    console.error('useTargetLocationFilter error:', e);
  }
}

async function loadJobs() {
  const countText = $('jobs-count-text');
  countText.textContent = 'Loading jobs...';

  const minScore = $('job-score-filter').value || 0;
  const location = encodeURIComponent($('job-location-input').value || '');

  try {
    const jobs = await (await fetch(`/api/jobs?min_score=${minScore}&location=${location}&limit=200`)).json();
    _allLoadedJobs = jobs || [];
    filterJobsClientSide();
  } catch (e) {
    console.error('loadJobs error:', e);
    $('jobs-container').innerHTML = '<div class="empty-state"><div class="empty-title">Could not load jobs</div><div class="empty-desc">Check that the assistant is connected.</div></div>';
  }
}

function parseScamReasons(scamReasons) {
  if (!scamReasons) return [];
  if (Array.isArray(scamReasons)) return scamReasons;
  try {
    const parsed = JSON.parse(scamReasons);
    return Array.isArray(parsed) ? parsed : [];
  } catch (e) {
    return [];
  }
}

function getFriendlyReasonText(reasonObj) {
  if (!reasonObj) return 'Suspicious posting details detected';
  if (reasonObj.note) return reasonObj.note;
  const category = reasonObj.category || '';
  const quotes = reasonObj.quoted ? ` ("${reasonObj.quoted.length > 90 ? reasonObj.quoted.substring(0, 87) + '...' : reasonObj.quoted}")` : '';

  if (category === 'payment_fee') {
    return 'Requests upfront payment, application fees, or equipment purchases' + quotes;
  }
  if (category === 'bank_account_processing') {
    return 'Requests personal bank account for payment processing' + quotes;
  }
  if (category === 'premature_sensitive_info') {
    return 'Requests sensitive ID or bank details before interview' + quotes;
  }
  if (category === 'off_platform_pivot') {
    return 'Directs conversation to unverified off-platform messaging' + quotes;
  }
  if (category === 'too_good_to_be_true') {
    return 'Unrealistic hiring guarantees or vague employer details' + quotes;
  }
  if (category === 'reported_signature_match') {
    return 'Matches a previously reported scam posting' + quotes;
  }
  if (category === 'user-reported') {
    return (reasonObj.note || 'Reported by user as a scam') + quotes;
  }
  if (category === 'llm_suspicious') {
    return (reasonObj.reason || reasonObj.quoted || 'Flagged as suspicious during safety review') + quotes;
  }
  return (reasonObj.reason || reasonObj.quoted || 'Suspicious posting details detected') + quotes;
}

function filterJobsClientSide() {
  const query = ($('job-search-input').value || '').toLowerCase().trim();
  const container = $('jobs-container');
  const countText = $('jobs-count-text');

  let filtered = _allLoadedJobs;
  if (query) {
    filtered = filtered.filter(j =>
      (j.title || '').toLowerCase().includes(query) ||
      (j.site || '').toLowerCase().includes(query) ||
      (j.location || '').toLowerCase().includes(query) ||
      (j.score_reasoning || '').toLowerCase().includes(query)
    );
  }

  countText.textContent = `Showing ${filtered.length} ${filtered.length === 1 ? 'job' : 'jobs'}`;

  if (!filtered.length) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">🔍</div>
        <div class="empty-title">No jobs found</div>
        <div class="empty-desc">${_allLoadedJobs.length === 0 ? "You haven't searched for jobs yet. Click 'Start Finding Jobs' on the Home tab to begin discovering matches." : "No jobs match your current search words or filter."}</div>
        ${_allLoadedJobs.length === 0 ? `<button class="btn btn-primary" onclick="switchTab('home'); toggleMainSearch();">🚀 Start Finding Jobs</button>` : `<button class="btn btn-secondary btn-sm" onclick="$('job-search-input').value=''; filterJobsClientSide();">Clear Search</button>`}
      </div>
    `;
    return;
  }

  container.innerHTML = filtered.map(j => {
    const score = j.fit_score != null ? j.fit_score : null;
    let matchBadgeHtml = '<span class="match-badge match-fair">Unrated</span>';
    if (score != null) {
      if (score >= 8) matchBadgeHtml = `<span class="match-badge match-great">⭐ ${score}/10 Great Match</span>`;
      else if (score >= 6) matchBadgeHtml = `<span class="match-badge match-good">👍 ${score}/10 Good Match</span>`;
      else matchBadgeHtml = `<span class="match-badge match-fair">${score}/10 Fair Match</span>`;
    }

    const isBlocked = j.scam_verdict === 'blocked';
    const reasons = parseScamReasons(j.scam_reasons);
    const firstReasonText = reasons.length > 0 ? getFriendlyReasonText(reasons[0]) : 'Flagged as suspicious during screening';

    let statusHtml = '<span class="job-pill">Discovered</span>';
    if (isBlocked) {
      statusHtml = '<span class="job-pill" style="background:#fee2e2;color:#991b1b;font-weight:700;">⚠️ Blocked (Scam Warning)</span>';
    } else if (j.applied_at) {
      statusHtml = '<span class="job-pill" style="background:var(--success-light);color:var(--success);">✅ Applied</span>';
    } else if (j.apply_status === 'failed') {
      const isLogin = (j.apply_error || '').includes('login');
      statusHtml = `<span class="job-pill" style="background:var(--danger-light);color:var(--danger);">${isLogin ? '⚠️ Requires Login' : '⚠️ Application Issue'}</span>`;
    } else if (j.tailored_resume_path && j.cover_letter_path) {
      statusHtml = '<span class="job-pill" style="background:var(--primary-light);color:var(--primary);">📝 Ready to Apply</span>';
    }

    // Scam warning banner on card
    const scamWarningBanner = isBlocked ? `
      <div class="scam-warning-box">
        <div class="scam-warning-title">⚠️ Possible scam: <span class="scam-warning-reason">${escapeHtml(firstReasonText)}</span></div>
      </div>
    ` : '';

    // Documents download
    const encUrl = encodeURIComponent(j.url);
    const resumeLinks = j.tailored_resume_path ? `
      <span>Resume:</span>
      <a class="doc-btn" href="/api/jobs/file?kind=resume&format=pdf&url=${encUrl}" target="_blank">PDF</a>
      <a class="doc-btn" href="/api/jobs/file?kind=resume&format=docx&url=${encUrl}" target="_blank">DOCX</a>
      <a class="doc-btn" href="/api/jobs/file?kind=resume&format=txt&url=${encUrl}" target="_blank">TXT</a>
    ` : '';

    const coverLinks = j.cover_letter_path ? `
      <span style="margin-left:0.4rem;">Letter:</span>
      <a class="doc-btn" href="/api/jobs/file?kind=cover_letter&format=pdf&url=${encUrl}" target="_blank">PDF</a>
      <a class="doc-btn" href="/api/jobs/file?kind=cover_letter&format=docx&url=${encUrl}" target="_blank">DOCX</a>
      <a class="doc-btn" href="/api/jobs/file?kind=cover_letter&format=txt&url=${encUrl}" target="_blank">TXT</a>
    ` : '';

    const jobUrl = j.application_url || j.url;
    const safeUrl = j.url.replace(/'/g, "\\'");
    const safeTitle = (j.title || 'Untitled Job').replace(/'/g, "\\'");

    // Actions
    let actionButtons = '';
    if (isBlocked) {
      actionButtons = `
        <button class="btn btn-secondary btn-sm" style="color:var(--success);font-weight:600;" onclick="clearScamFlag('${safeUrl}')">
          ✅ Clear Flag
        </button>
        <button class="btn btn-secondary btn-sm" style="color:var(--danger);" onclick="reportScamPrompt('${safeUrl}', '${escapeHtml(safeTitle)}')">
          🚩 Report as Scam
        </button>
      `;
    } else {
      actionButtons = `
        <button class="btn btn-secondary btn-sm" onclick="markJob('${safeUrl}', 'applied')">
          ✅ Mark Applied
        </button>
        <button class="btn btn-secondary btn-sm" style="color:var(--text-muted);" onclick="markJob('${safeUrl}', 'failed')">
          ❌ Not Interested
        </button>
        <button class="btn btn-secondary btn-sm" style="color:var(--text-muted);" title="Report suspicious posting" onclick="reportScamPrompt('${safeUrl}', '${escapeHtml(safeTitle)}')">
          🚩 Report Scam
        </button>
      `;
    }

    return `
      <div class="job-item"${isBlocked ? ' style="border-color:#fecaca;"' : ''}>
        <div class="job-top">
          <div style="flex:1;min-width:0;">
            <a href="${jobUrl}" target="_blank" rel="noopener" class="job-title-link"${isBlocked ? ' style="color:#b91c1c;"' : ''}>${escapeHtml(j.title || 'Untitled Job')}</a>
            <div class="job-meta">
              <span class="job-pill">${escapeHtml(j.site || 'Direct')}</span>
              <span>📍 ${escapeHtml(j.location || 'Location Not Specified')}</span>
              ${statusHtml}
            </div>
          </div>
          <div>${matchBadgeHtml}</div>
        </div>

        ${scamWarningBanner}

        ${j.score_reasoning ? `<div class="job-reason">${escapeHtml(j.score_reasoning)}</div>` : ''}

        <div class="job-bottom">
          <div class="job-docs">
            ${resumeLinks || coverLinks ? resumeLinks + coverLinks : '<span style="color:var(--text-faint);">No custom documents prepared yet</span>'}
          </div>
          <div class="job-actions">
            ${actionButtons}
          </div>
        </div>
      </div>
    `;
  }).join('');
}

async function markJob(url, status) {
  try {
    await fetch('/api/jobs/mark', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, status })
    });
    showToast(status === 'applied' ? 'Marked as applied!' : 'Job dismissed.');
    await loadJobs();
  } catch (e) {
    console.error('markJob error:', e);
  }
}

async function reportScamPrompt(url, title) {
  const note = prompt(`Report "${title || 'this job'}" as a scam?\n\nThis will immediately block the job and remember its signature.\n\nOptional: Add a brief note (e.g. asked for fee, bank account, fake recruiter):`, '');
  if (note === null) return;
  try {
    const res = await fetch('/api/jobs/report-scam', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, note: note.trim() })
    });
    const data = await res.json();
    if (res.ok) {
      showToast('Reported as scam and blocked.');
      await loadJobs();
      if ($('tab-safety') && $('tab-safety').classList.contains('active')) {
        await loadSafetyJobs();
      }
      loadHome();
    } else {
      alert('Could not report job: ' + (data.error || 'Unknown error'));
    }
  } catch (e) {
    console.error('reportScam error:', e);
    alert('Failed to report scam.');
  }
}

async function clearScamFlag(url) {
  if (!confirm('Mark this job posting as safe and clear the scam flag? It will become eligible for applications if match criteria are met.')) {
    return;
  }
  try {
    const res = await fetch('/api/jobs/clear-scam', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const data = await res.json();
    if (res.ok) {
      showToast('Flag cleared. Job marked as safe.');
      await loadJobs();
      if ($('tab-safety') && $('tab-safety').classList.contains('active')) {
        await loadSafetyJobs();
      }
      loadHome();
    } else {
      alert('Could not clear flag: ' + (data.error || 'Unknown error'));
    }
  } catch (e) {
    console.error('clearScamFlag error:', e);
    alert('Failed to clear flag.');
  }
}

let _allSafetyJobs = [];

async function loadSafetyJobs() {
  const countText = $('safety-count-text');
  if (countText) countText.textContent = 'Loading flagged jobs...';
  const container = $('safety-jobs-container');
  if (!container) return;

  try {
    const res = await fetch('/api/jobs/flagged');
    const jobs = await res.json();
    _allSafetyJobs = jobs || [];
    renderSafetyJobs();
  } catch (e) {
    console.error('loadSafetyJobs error:', e);
    if (container) {
      container.innerHTML = '<div class="empty-state"><div class="empty-title">Could not load flagged jobs</div><div class="empty-desc">Check that the assistant is connected.</div></div>';
    }
  }
}

function renderSafetyJobs() {
  const container = $('safety-jobs-container');
  const countText = $('safety-count-text');
  if (!container) return;

  const jobs = _allSafetyJobs;
  if (countText) {
    countText.textContent = `Found ${jobs.length} flagged ${jobs.length === 1 ? 'posting' : 'postings'}`;
  }

  if (!jobs.length) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">🛡️</div>
        <div class="empty-title">No flagged postings</div>
        <div class="empty-desc">All discovered job postings look safe and passed scam screening. When suspicious postings or fee demands are detected, they will appear here for review.</div>
      </div>
    `;
    return;
  }

  container.innerHTML = jobs.map(j => {
    const reasons = parseScamReasons(j.scam_reasons);
    const firstReasonText = reasons.length > 0 ? getFriendlyReasonText(reasons[0]) : 'Flagged as suspicious during screening';

    const reasonsListHtml = reasons.map(r => {
      const friendlyCat = getFriendlyReasonText(r);
      const quoteHtml = r.quoted ? `<div style="margin-top:0.25rem;font-style:italic;color:#881337;">Excerpt: "${escapeHtml(r.quoted)}"</div>` : '';
      return `<div class="scam-warning-detail"><strong>${escapeHtml(friendlyCat)}</strong>${quoteHtml}</div>`;
    }).join('');

    const score = j.fit_score != null ? j.fit_score : null;
    let matchBadgeHtml = '';
    if (score != null) {
      matchBadgeHtml = `<span class="match-badge match-fair">Match: ${score}/10</span>`;
    }

    const jobUrl = j.application_url || j.url;
    const safeUrl = j.url.replace(/'/g, "\\'");
    const safeTitle = (j.title || 'Untitled Job').replace(/'/g, "\\'");

    return `
      <div class="job-item" style="border-color:#fecaca;">
        <div class="job-top">
          <div style="flex:1;min-width:0;">
            <a href="${jobUrl}" target="_blank" rel="noopener" class="job-title-link" style="color:#b91c1c;">${escapeHtml(j.title || 'Untitled Job')}</a>
            <div class="job-meta">
              <span class="job-pill" style="background:#fee2e2;color:#991b1b;font-weight:700;">⚠️ Blocked</span>
              <span class="job-pill">${escapeHtml(j.site || 'Direct')}</span>
              <span>📍 ${escapeHtml(j.location || 'Location Not Specified')}</span>
              ${j.scam_checked_at ? `<span>Checked: ${escapeHtml(j.scam_checked_at.slice(0, 10))}</span>` : ''}
            </div>
          </div>
          <div>${matchBadgeHtml}</div>
        </div>

        <div class="scam-warning-box">
          <div class="scam-warning-title">⚠️ Possible scam: <span class="scam-warning-reason">${escapeHtml(firstReasonText)}</span></div>
          ${reasonsListHtml}
        </div>

        ${j.score_reasoning ? `<div class="job-reason">${escapeHtml(j.score_reasoning)}</div>` : ''}

        <div class="job-bottom">
          <div style="font-size:0.8rem;color:var(--text-muted);">
            Blocked from auto-apply. Review details or clear flag if this posting is legitimate.
          </div>
          <div class="job-actions">
            <button class="btn btn-secondary btn-sm" style="color:var(--danger);" onclick="reportScamPrompt('${safeUrl}', '${escapeHtml(safeTitle)}')">
              🚩 Report as Scam
            </button>
            <button class="btn btn-secondary btn-sm" style="color:var(--success);font-weight:600;" onclick="clearScamFlag('${safeUrl}')">
              ✅ Clear Flag (Mark Safe)
            </button>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

function escapeHtml(text) {
  if (!text) return '';
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Tab 3: Settings Logic
// ---------------------------------------------------------------------------
let _profileData = {};
let _acceptedLocations = [];

const PROFILE_FIELDS = [
  ['p-full_name', 'personal.full_name'],
  ['p-preferred_name', 'personal.preferred_name'],
  ['p-email', 'personal.email'],
  ['p-phone', 'personal.phone'],
  ['p-city', 'personal.city'],
  ['p-country', 'personal.country'],
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

function _pget(path, def) {
  return path.split('.').reduce((o, k) => (o && o[k] !== undefined) ? o[k] : undefined, _profileData) ?? def;
}

function _pset(path, val) {
  const keys = path.split('.');
  let cur = _profileData;
  for (let i = 0; i < keys.length - 1; i++) {
    cur[keys[i]] = cur[keys[i]] || {};
    cur = cur[keys[i]];
  }
  cur[keys[keys.length - 1]] = val;
}

async function loadSettings() {
  await loadProfiles();

  // LLM Settings
  try {
    const llm = await (await fetch('/api/settings/llm')).json();
    let prov = llm.provider === 'none' ? 'gemini' : llm.provider;
    if (prov === 'none' && llm.openrouter) prov = 'openrouter';
    $('llm-provider').value = prov;
    $('llm-url').value = llm.llm_url || '';
    $('llm-model').value = llm.llm_model || '';
    updateLlmRows();
    const shown = prov === 'openrouter' ? (llm.openrouter_model || 'OpenRouter') : llm.llm_model;
    $('llm-current').textContent = `Current AI: ${prov === 'openrouter' ? 'OpenRouter' : prov} ${shown ? '(' + shown + ')' : ''}`;
  } catch (e) {
    console.error('loadSettings LLM error:', e);
  }

  // Profile data
  try {
    _profileData = await (await fetch('/api/settings/profile')).json();
    $('profile-json').value = JSON.stringify(_profileData, null, 2);
    for (const [id, path] of PROFILE_FIELDS) {
      const el = $(id);
      if (el) el.value = _pget(path, '');
    }
  } catch (e) {
    console.error('loadSettings profile error:', e);
  }

  // Searches YAML
  try {
    const searches = await (await fetch('/api/settings/searches')).json();
    $('searches-yaml').value = searches.yaml || '';
  } catch (e) {
    console.error('loadSettings searches error:', e);
  }

  // Location settings
  try {
    const loc = await (await fetch('/api/settings/location')).json();
    $('loc-primary').value = loc.primary_location || '';
    $('loc-remote').value = loc.remote_enabled ? 'true' : 'false';
    _acceptedLocations = loc.location_accept || [];
    renderAcceptedLocations();
  } catch (e) {
    console.error('loadSettings location error:', e);
  }
}

function updateLlmRows() {
  const p = $('llm-provider').value;
  $('row-openrouter-key').style.display = p === 'openrouter' ? 'flex' : 'none';
  $('row-gemini-key').style.display = p === 'gemini' ? 'flex' : 'none';
  $('row-openai-key').style.display = p === 'openai' ? 'flex' : 'none';
  $('row-local-url').style.display = p === 'local' ? 'flex' : 'none';
  $('row-local-key').style.display = p === 'local' ? 'flex' : 'none';
}

async function saveLlm() {
  const body = {
    provider: $('llm-provider').value,
    llm_url: $('llm-url').value,
    llm_model: $('llm-model').value,
    llm_api_key: $('llm-api-key').value,
    gemini_key: $('gemini-key').value,
    openai_key: $('openai-key').value,
    openrouter_key: $('openrouter-key').value,
  };
  await fetch('/api/settings/llm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  $('gemini-key').value = '';
  $('openai-key').value = '';
  $('openrouter-key').value = '';
  $('llm-api-key').value = '';
  showToast('✓ AI Assistant settings saved!');
  await loadSettings();
}

async function saveProfileForm() {
  for (const [id, path] of PROFILE_FIELDS) {
    const el = $(id);
    if (el) _pset(path, el.value);
  }
  await fetch('/api/settings/profile', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(_profileData)
  });
  $('profile-json').value = JSON.stringify(_profileData, null, 2);
  showToast('✓ Profile details saved!');
}

async function saveProfile() {
  let data;
  try {
    data = JSON.parse($('profile-json').value);
  } catch (e) {
    alert('Invalid JSON format: ' + e.message);
    return;
  }
  await fetch('/api/settings/profile', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data)
  });
  _profileData = data;
  for (const [id, path] of PROFILE_FIELDS) {
    const el = $(id);
    if (el) el.value = _pget(path, '');
  }
  showToast('✓ Raw profile JSON saved!');
}

function toggleRawProfile() {
  const wrap = $('raw-profile-wrap');
  const isHidden = wrap.style.display === 'none';
  wrap.style.display = isHidden ? 'block' : 'none';
  $('raw-profile-toggle').textContent = isHidden ? '▲ Hide Raw Profile JSON' : '⚙️ Advanced: View / Edit Full Profile Data (JSON)';
}

function toggleRawSearches() {
  const wrap = $('raw-searches-wrap');
  const isHidden = wrap.style.display === 'none';
  wrap.style.display = isHidden ? 'block' : 'none';
  $('raw-searches-toggle').textContent = isHidden ? '▲ Hide Raw Search Config' : '⚙️ Advanced: Search Queries & Keywords (YAML)';
}

async function saveSearches() {
  await fetch('/api/settings/searches', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ yaml: $('searches-yaml').value })
  });
  showToast('✓ Search configuration saved!');
}

function renderAcceptedLocations() {
  const container = $('loc-accept-tags');
  if (!_acceptedLocations.length) {
    container.innerHTML = '<span style="font-size:0.8rem;color:var(--text-muted);">Remote-only (no specific onsite cities added)</span>';
    return;
  }
  container.innerHTML = _acceptedLocations.map((loc, idx) => `
    <span class="tag">
      ${escapeHtml(loc)}
      <span class="tag-close" onclick="removeAcceptedLocation(${idx})" title="Remove location">&times;</span>
    </span>
  `).join('');
}

function addAcceptedLocation() {
  const input = $('loc-accept-input');
  const val = input.value.trim();
  if (val && !_acceptedLocations.includes(val)) {
    _acceptedLocations.push(val);
    renderAcceptedLocations();
  }
  input.value = '';
}

function removeAcceptedLocation(idx) {
  _acceptedLocations.splice(idx, 1);
  renderAcceptedLocations();
}

function applyLocationPreset(preset) {
  if (preset === 'mycity') {
    var _city = ($('p-city') && $('p-city').value.trim()) || '';
    var _country = ($('p-country') && $('p-country').value.trim()) || '';
    if (!_city) { showToast('Add your city in Profile & Resume first, then try this preset again.'); return; }
    $('loc-primary').value = _country ? (_city + ', ' + _country) : _city;
    $('loc-remote').value = 'true';
    _acceptedLocations = [_city, _country].filter(Boolean);
  } else if (preset === 'remote') {
    $('loc-primary').value = '';
    $('loc-remote').value = 'true';
    _acceptedLocations = [];
  }
  renderAcceptedLocations();
  showToast('Preset applied! Click "Save Location Preferences" to keep.');
}

async function saveLocation() {
  const body = {
    primary_location: $('loc-primary').value,
    remote_enabled: $('loc-remote').value === 'true',
    location_accept: _acceptedLocations,
  };
  await fetch('/api/settings/location', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  const searches = await (await fetch('/api/settings/searches')).json();
  $('searches-yaml').value = searches.yaml || '';
  showToast('✓ Location preferences saved!');
}

// Multi-User Profile Switcher
async function loadProfiles() {
  try {
    const p = await (await fetch('/api/profiles')).json();
    const sel = $('profile-select');
    sel.innerHTML = (p.profiles || []).map(n => `<option value="${n}"${n === p.active ? ' selected' : ''}>${n}</option>`).join('');
    $('profile-status').textContent = `Currently active: ${p.active}`;
  } catch (e) {
    console.error('loadProfiles error:', e);
  }
}

async function createProfile() {
  const name = $('new-profile-name').value.trim();
  if (!name) return;
  const res = await (await fetch('/api/profiles/create', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name })
  })).json();
  if (res.error) {
    alert('Error: ' + res.error);
    return;
  }
  $('new-profile-name').value = '';
  showToast(`Profile "${name}" created!`);
  await loadProfiles();
}

async function switchProfile() {
  const name = $('profile-select').value;
  if (!name) return;
  if (!confirm(`Switch active user to "${name}"? The assistant will restart in a moment.`)) return;
  await fetch('/api/profiles/switch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name })
  });
  $('profile-status').textContent = 'Restarting assistant...';
  await fetch('/api/restart', { method: 'POST' });
  setTimeout(() => location.reload(), 3000);
}

// ---------------------------------------------------------------------------
// Auto-Refresh Loop
// ---------------------------------------------------------------------------
loadHome();
loadActivity();

setInterval(() => {
  const activeTab = document.querySelector('.tab-pane.active');
  if (activeTab && activeTab.id === 'tab-home') {
    loadHome();
    loadActivity();
  }
}, 4000);

</script>
</body>
</html>
"""
