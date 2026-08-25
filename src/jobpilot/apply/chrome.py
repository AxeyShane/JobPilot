"""Chrome lifecycle management for apply workers.

Handles launching an isolated Chrome instance with remote debugging,
worker profile setup/cloning, and cross-platform process cleanup.
"""

import json
import logging
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path
import os
import sqlite3

from jobpilot import config

logger = logging.getLogger(__name__)

# CDP port base — each worker uses BASE_CDP_PORT + worker_id
BASE_CDP_PORT = 9222

# Track Chrome processes per worker for cleanup
_chrome_procs: dict[int, subprocess.Popen] = {}
_chrome_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Cross-platform process helpers
# ---------------------------------------------------------------------------

def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children.

    On Windows, Chrome spawns 10+ child processes (GPU, renderer, etc.),
    so taskkill /T is needed to kill the entire tree. On Unix, os.killpg
    handles the process group.
    """
    import signal as _signal

    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            # Unix: kill entire process group
            import os
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Process already gone or owned by another user
                try:
                    os.kill(pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception:
        logger.debug("Failed to kill process tree for PID %d", pid, exc_info=True)


def _kill_on_port(port: int) -> None:
    """Kill any process listening on a specific port (zombie cleanup).

    Uses netstat on Windows, lsof on macOS/Linux.
    """
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    if pid.isdigit():
                        _kill_process_tree(int(pid))
        else:
            # macOS / Linux
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=10,
            )
            for pid_str in result.stdout.strip().splitlines():
                pid_str = pid_str.strip()
                if pid_str.isdigit():
                    _kill_process_tree(int(pid_str))
    except FileNotFoundError:
        logger.debug("Port-kill tool not found (netstat/lsof) for port %d", port)
    except Exception:
        logger.debug("Failed to kill process on port %d", port, exc_info=True)


# ---------------------------------------------------
# ---------------------------------------------------------------------------
# Job-site session profile handling (logged-in Indeed/LinkedIn sessions)
# ---------------------------------------------------------------------------

def _find_job_profile_dir() -> str:
    """Locate the user's REAL Chrome profile whose cookies contain job-site
    logins (linkedin/indeed/workday/smartrecruiters/greenhouse/lever).

    The apply workers launch an isolated Chrome with --profile-directory=<this>
    so they reuse the user's existing logged-in sessions instead of hitting
    login walls. Override with JOBPILOT_CHROME_PROFILE (e.g. 'Profile 3').
    Falls back to 'Default'.
    """
    override = os.environ.get("JOBPILOT_CHROME_PROFILE", "").strip()
    if override:
        return override
    ud = config.get_chrome_user_data()
    best, best_count = "Default", 0
    for folder in ("Default", "Profile 1", "Profile 2", "Profile 3", "Profile 4"):
        ck = Path(ud) / folder / "Network" / "Cookies"
        if not ck.exists():
            ck = Path(ud) / folder / "Cookies"
        if not ck.exists():
            continue
        try:
            tmp = f"/tmp/ap_profile_{folder.replace(' ', '_')}.db"
            shutil.copy2(str(ck), tmp)
            conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%linkedin%'"
                " OR host_key LIKE '%indeed%' OR host_key LIKE '%workday%'"
                " OR host_key LIKE '%smartrecruiters%' OR host_key LIKE '%successfactors%'"
                " OR host_key LIKE '%greenhouse%' OR host_key LIKE '%lever%'"
            )
            n = int(cur.fetchone()[0])
            conn.close()
            if n > best_count:
                best_count, best = n, folder
        except Exception:
            logger.debug("Could not inspect profile %s cookies", folder, exc_info=True)
    logger.info("Job-site Chrome profile selected: %s (%d session cookies)", best, best_count)
    return best


def _refresh_profile_session(profile_dir: Path, profile_dir_name: str) -> None:
    """Re-sync the user's logged-in session (cookies + localStorage) from their
    real Chrome profile into a worker's isolated profile, so job boards
    recognise existing logins. Best with Chrome closed; tolerates a locked
    (live) Chrome by copying the WAL/journal alongside the main DB."""
    src_root = Path(config.get_chrome_user_data()) / profile_dir_name
    dst_root = Path(profile_dir) / profile_dir_name
    if not src_root.exists() or not dst_root.exists():
        return
    # Copy cookie WAL/journal files first (live Chrome keeps recent writes here)
    for cookie_part in ("Cookies-journal", "Cookies-wal", "Cookies-shm"):
        s = src_root / "Network" / cookie_part
        if s.exists():
            try:
                shutil.copy2(str(s), str(dst_root / "Network" / cookie_part))
            except Exception:
                pass
    # Copy the main cookie DB last
    try:
        shutil.copy2(str(src_root / "Network" / "Cookies"), str(dst_root / "Network" / "Cookies"))
    except Exception:
        logger.warning("Could not refresh worker session cookies from %s (Chrome may be open)",
                       profile_dir_name, exc_info=True)
    # Copy localStorage (some sites track login state here)
    src_ls = src_root / "Local Storage" / "leveldb"
    dst_ls = dst_root / "Local Storage" / "leveldb"
    if src_ls.exists():
        try:
            dst_ls.mkdir(parents=True, exist_ok=True)
            for item in src_ls.iterdir():
                if item.is_file():
                    try:
                        shutil.copy2(str(item), str(dst_ls / item.name))
                    except Exception:
                        pass
        except Exception:
            pass

# ---------------------------------------------------------------------------

# Worker profile management
# ---------------------------------------------------------------------------

def setup_worker_profile(worker_id: int) -> Path:
    """Create an isolated Chrome profile for a worker.

    On first run, clones from an existing worker profile (preferred, since
    it already has session cookies) or from the user's real Chrome profile.
    Subsequent runs reuse the existing worker profile.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the worker's Chrome user-data directory.
    """
    profile_dir = config.CHROME_WORKER_DIR / f"worker-{worker_id}"
    if (profile_dir / "Default").exists():
        return profile_dir  # Already initialized

    # Find a source: prefer existing worker (has session cookies), else user profile
    source: Path | None = None
    for wid in range(10):
        if wid == worker_id:
            continue
        candidate = config.CHROME_WORKER_DIR / f"worker-{wid}"
        if (candidate / "Default").exists():
            source = candidate
            break
    if source is None:
        source = config.get_chrome_user_data()

    logger.info("[worker-%d] Copying Chrome profile from %s (first time setup)...",
                worker_id, source.name)
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Copy essential profile dirs -- skip caches and heavy transient data
    skip = {
        "ShaderCache", "GrShaderCache", "Service Worker", "Cache",
        "Code Cache", "GPUCache", "CacheStorage", "Crashpad",
        "BrowserMetrics", "SafeBrowsing", "Crowd Deny",
        "MEIPreload", "SSLErrorAssistant", "recovery", "Temp",
        "SingletonLock", "SingletonSocket", "SingletonCookie",
    }

    for item in source.iterdir():
        if item.name in skip:
            continue
        dst = profile_dir / item.name
        try:
            if item.is_dir():
                shutil.copytree(
                    str(item), str(dst), dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(
                        "Cache", "Code Cache", "GPUCache", "Service Worker",
                    ),
                )
            else:
                shutil.copy2(str(item), str(dst))
        except (PermissionError, OSError):
            pass  # skip locked files

    return profile_dir


def _suppress_restore_nag(profile_dir: Path) -> None:
    """Clear Chrome's 'restore pages' nag by fixing Preferences.

    Chrome writes exit_type=Crashed when killed, which triggers a
    'Restore pages?' prompt on next launch. This patches it out.
    """
    prefs_file = profile_dir / "Default" / "Preferences"
    if not prefs_file.exists():
        return

    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        prefs.setdefault("session", {})["restore_on_startup"] = 4  # 4 = open blank
        prefs.setdefault("session", {}).pop("startup_urls", None)
        prefs["credentials_enable_service"] = False
        prefs.setdefault("password_manager", {})["saving_enabled"] = False
        prefs.setdefault("autofill", {})["profile_enabled"] = False
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
    except Exception:
        logger.debug("Could not patch Chrome preferences", exc_info=True)


# ---------------------------------------------------------------------------
# Chrome launch / kill
# ---------------------------------------------------------------------------

def launch_chrome(worker_id: int, port: int | None = None,
                  headless: bool = False) -> subprocess.Popen:
    """Launch a Chrome instance with remote debugging for a worker.

    Args:
        worker_id: Numeric worker identifier.
        port: CDP port. Defaults to BASE_CDP_PORT + worker_id.
        headless: Run Chrome in headless mode (no visible window).

    Returns:
        subprocess.Popen handle for the Chrome process.
    """
    if port is None:
        port = BASE_CDP_PORT + worker_id

    profile_dir = setup_worker_profile(worker_id)
    job_profile = _find_job_profile_dir()
    _refresh_profile_session(profile_dir, job_profile)

    # Kill any zombie Chrome from a previous run on this port
    _kill_on_port(port)

    # Patch preferences to suppress restore nag
    _suppress_restore_nag(profile_dir)

    chrome_exe = config.get_chrome_path()

    cmd = [
        chrome_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        f"--profile-directory={job_profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1024,768",
        "--disable-session-crashed-bubble",
        "--disable-features=InfiniteSessionRestore,PasswordManagerOnboarding",
        "--hide-crash-restore-bubble",
        "--noerrdialogs",
    ]
    if headless:
        # Headless mode is inherently more detectable; keep the quiet flags that
        # prevent unwanted dialogs, but avoid the loudest automation fingerprints.
        cmd += [
            "--headless=new",
            "--disable-popup-blocking",
            "--deny-permission-prompts",
            "--disable-notifications",
        ]
    else:
        # Headed mode using the user's real logged-in profile: keep it as clean
        # and human-like as possible so real sites (Indeed/LinkedIn behind
        # Cloudflare) don't trigger bot detection.
        cmd += [
            "--start-maximized",
            "--noerrdialogs",
        ]

    # On Unix, start in a new process group so we can kill the whole tree
    kwargs: dict = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if platform.system() != "Windows":
        import os
        kwargs["preexec_fn"] = os.setsid

    proc = subprocess.Popen(cmd, **kwargs)
    with _chrome_lock:
        _chrome_procs[worker_id] = proc

    # Give Chrome time to start and open the debug port
    time.sleep(3)
    logger.info("[worker-%d] Chrome started on port %d (pid %d)",
                worker_id, port, proc.pid)
    return proc


def cleanup_worker(worker_id: int, process: subprocess.Popen | None) -> None:
    """Kill a worker's Chrome instance and remove it from tracking.

    Args:
        worker_id: Numeric worker identifier.
        process: The Popen handle returned by launch_chrome.
    """
    if process and process.poll() is None:
        _kill_process_tree(process.pid)
    with _chrome_lock:
        _chrome_procs.pop(worker_id, None)
    logger.info("[worker-%d] Chrome cleaned up", worker_id)


def kill_all_chrome() -> None:
    """Kill all Chrome instances and any port zombies.

    Called during graceful shutdown to ensure no orphan Chrome processes.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)

    # Sweep base port in case of zombies
    _kill_on_port(BASE_CDP_PORT)


def reset_worker_dir(worker_id: int) -> Path:
    """Wipe and recreate a worker's isolated working directory.

    Each job gets a fresh working directory so that file conflicts
    (resume PDFs, MCP configs) don't bleed between jobs.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the clean worker directory.
    """
    worker_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
    if worker_dir.exists():
        shutil.rmtree(str(worker_dir), ignore_errors=True)
    worker_dir.mkdir(parents=True, exist_ok=True)
    return worker_dir


def cleanup_on_exit() -> None:
    """Atexit handler: kill all Chrome processes and sweep CDP ports.

    Register this with atexit.register() at application startup.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)

    # Sweep base port for any orphan
    _kill_on_port(BASE_CDP_PORT)
