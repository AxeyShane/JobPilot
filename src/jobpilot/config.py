"""JobPilot configuration: paths, platform detection, user data."""

import os
import platform
import shutil
from pathlib import Path

# Root of all JobPilot data on this machine. Multiple people sharing a
# machine (or one person running multiple resumes/searches side by side)
# each get their own subdirectory under PROFILES_ROOT -- see
# get_active_profile_name()/list_profiles()/create_profile() below.
_USER_ROOT = Path.home() / ".jobpilot"
PROFILES_ROOT = _USER_ROOT / "profiles"
_ACTIVE_PROFILE_MARKER = _USER_ROOT / "active_profile.txt"
DEFAULT_PROFILE_NAME = "default"


def get_active_profile_name() -> str:
    """Which profile subdirectory is currently active.

    Priority: JOBPILOT_PROFILE env var (explicit, e.g. for a per-terminal
    override) > the marker file the web UI's profile switcher writes >
    "default". Read fresh each call rather than cached, so a switch takes
    effect on the next process that starts (module-level path constants
    below are still fixed for the lifetime of an already-running process --
    switching profiles requires restarting the web UI / agent loop, same as
    changing most other config here).
    """
    env = os.environ.get("JOBPILOT_PROFILE", "").strip()
    if env:
        return env
    if _ACTIVE_PROFILE_MARKER.exists():
        val = _ACTIVE_PROFILE_MARKER.read_text(encoding="utf-8").strip()
        if val:
            return val
    return DEFAULT_PROFILE_NAME


def list_profiles() -> list[str]:
    """List existing profile names, migrating legacy flat-layout data first."""
    _migrate_legacy_layout()
    if not PROFILES_ROOT.exists():
        return [DEFAULT_PROFILE_NAME]
    names = sorted(p.name for p in PROFILES_ROOT.iterdir() if p.is_dir())
    return names or [DEFAULT_PROFILE_NAME]


def create_profile(name: str) -> None:
    """Create a new, empty profile directory. Does not switch to it."""
    name = name.strip()
    if not name or any(c in name for c in r'\/:*?"<>|'):
        raise ValueError(f"Invalid profile name: {name!r}")
    (PROFILES_ROOT / name).mkdir(parents=True, exist_ok=True)


def set_active_profile(name: str) -> None:
    """Switch the active profile. Takes effect for processes started after this."""
    _USER_ROOT.mkdir(parents=True, exist_ok=True)
    _ACTIVE_PROFILE_MARKER.write_text(name.strip(), encoding="utf-8")


def _migrate_legacy_layout() -> None:
    """One-time move: pre-profiles installs kept everything directly under
    ~/.jobpilot/. If that flat layout is still present and no "default"
    profile exists yet, move it into profiles/default/ so existing data
    (resume, DB, tailored resumes...) isn't silently orphaned."""
    legacy_marker = _USER_ROOT / "profile.json"
    default_dir = PROFILES_ROOT / DEFAULT_PROFILE_NAME
    if not legacy_marker.exists() or (default_dir / "profile.json").exists():
        return
    default_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "jobpilot.db", "jobpilot.db-wal", "jobpilot.db-shm",
        "profile.json", "resume.txt", "resume.pdf",
        "searches.yaml", ".env", "tailored_resumes", "cover_letters",
        "logs", "chrome-workers", "apply-workers",
    ):
        src = _USER_ROOT / name
        if src.exists():
            src.rename(default_dir / name)


# User data directory for the ACTIVE profile -- all user-specific files live
# here. JOBPILOT_DIR remains a full override (bypasses the profile system
# entirely) for power users/testing who want one fixed, explicit path.
if os.environ.get("JOBPILOT_DIR"):
    APP_DIR = Path(os.environ["JOBPILOT_DIR"])
else:
    _migrate_legacy_layout()
    APP_DIR = PROFILES_ROOT / get_active_profile_name()

# Core paths
DB_PATH = APP_DIR / "jobpilot.db"
PROFILE_PATH = APP_DIR / "profile.json"
RESUME_PATH = APP_DIR / "resume.txt"
RESUME_PDF_PATH = APP_DIR / "resume.pdf"
SEARCH_CONFIG_PATH = APP_DIR / "searches.yaml"
ENV_PATH = APP_DIR / ".env"

# Generated output
TAILORED_DIR = APP_DIR / "tailored_resumes"
COVER_LETTER_DIR = APP_DIR / "cover_letters"
LOG_DIR = APP_DIR / "logs"

# Chrome worker isolation
CHROME_WORKER_DIR = APP_DIR / "chrome-workers"
APPLY_WORKER_DIR = APP_DIR / "apply-workers"

# Package-shipped config (YAML registries)
PACKAGE_DIR = Path(__file__).parent
CONFIG_DIR = PACKAGE_DIR / "config"


def get_chrome_path() -> str:
    """Auto-detect Chrome/Chromium executable path, cross-platform.

    Override with CHROME_PATH environment variable.
    """
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    system = platform.system()

    if system == "Windows":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    else:  # Linux
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    for c in candidates:
        if c and c.exists():
            return str(c)

    # Fall back to PATH search
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    raise FileNotFoundError(
        "Chrome/Chromium not found. Install Chrome or set CHROME_PATH environment variable."
    )


def find_claude_cli() -> str | None:
    """Locate the Claude Code CLI: PATH first, then common install locations.

    shutil.which("claude") alone can miss it when this process was spawned
    through a chain that skips the shell profile script which normally adds
    it to PATH (observed: %USERPROFILE%\\.local\\bin\\claude.exe resolves
    fine in an interactive shell but not through a background/service spawn
    chain). Mirrors get_chrome_path()'s candidate-search pattern.
    """
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (
        Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "claude.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "claude" / "claude.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return None


def find_npx() -> str | None:
    """Locate npx's fully-resolved path: PATH first, then common Node install dirs.

    On Windows, npx is a .CMD script. Handing bare "npx" to a spawner that
    resolves it via PATH search can fail or silently hang depending on the
    spawner's own resolution logic (observed: Claude Code CLI's internal MCP
    server spawn never completing with bare "npx" in the MCP config, even
    with npx's directory correctly on PATH for this process). An absolute,
    fully-qualified path sidesteps whatever that resolution mechanism is,
    for any spawner reading the config, not just our own subprocess calls.
    """
    found = shutil.which("npx") or shutil.which("npx.cmd")
    if found:
        return found
    for candidate in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "npx.cmd",
        Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "node" / "npx.cmd",
        Path(os.environ.get("APPDATA", "")) / "npm" / "npx.cmd",
    ):
        if candidate.exists():
            return str(candidate)
    return None


def get_chrome_user_data() -> Path:
    """Default Chrome user data directory, cross-platform."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        return Path.home() / ".config" / "google-chrome"


def ensure_dirs():
    """Create all required directories."""
    for d in [APP_DIR, TAILORED_DIR, COVER_LETTER_DIR, LOG_DIR, CHROME_WORKER_DIR, APPLY_WORKER_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_profile() -> dict:
    """Load user profile from ~/.jobpilot/profile.json."""
    import json
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"Profile not found at {PROFILE_PATH}. Run `jobpilot init` first."
        )
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def load_search_config() -> dict:
    """Load search configuration from ~/.jobpilot/searches.yaml."""
    import yaml
    if not SEARCH_CONFIG_PATH.exists():
        # Fall back to package-shipped example
        example = CONFIG_DIR / "searches.example.yaml"
        if example.exists():
            return yaml.safe_load(example.read_text(encoding="utf-8"))
        return {}
    return yaml.safe_load(SEARCH_CONFIG_PATH.read_text(encoding="utf-8"))


def load_sites_config() -> dict:
    """Load sites.yaml configuration (sites list, manual_ats, blocked, etc.)."""
    import yaml
    path = CONFIG_DIR / "sites.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def is_manual_ats(url: str | None) -> bool:
    """Check if a URL routes through an ATS that requires manual application."""
    if not url:
        return False
    sites_cfg = load_sites_config()
    domains = sites_cfg.get("manual_ats", [])
    url_lower = url.lower()
    return any(domain in url_lower for domain in domains)


def load_blocked_sites() -> tuple[set[str], list[str]]:
    """Load blocked sites and URL patterns from sites.yaml.

    Returns:
        (blocked_site_names, blocked_url_patterns)
    """
    cfg = load_sites_config()
    blocked = cfg.get("blocked", {})
    sites = set(blocked.get("sites", []))
    patterns = blocked.get("url_patterns", [])
    return sites, patterns


def load_blocked_sso() -> list[str]:
    """Load blocked SSO domains from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("blocked_sso", [])


def load_base_urls() -> dict[str, str | None]:
    """Load site base URLs for URL resolution from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("base_urls", {})


# ---------------------------------------------------------------------------
# Default values — referenced across modules instead of magic numbers
# ---------------------------------------------------------------------------

DEFAULTS = {
    "min_score": 6,
    "max_apply_attempts": 10,
    "max_tailor_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 300,
    "viewport": "1280x900",
}


def load_env():
    """Load environment variables from ~/.jobpilot/.env if it exists."""
    from dotenv import load_dotenv
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    # Also try CWD .env as fallback
    load_dotenv()


# ---------------------------------------------------------------------------
# Tier system — feature gating by installed dependencies
# ---------------------------------------------------------------------------

TIER_LABELS = {
    1: "Discovery",
    2: "AI Scoring & Tailoring",
    3: "Full Auto-Apply",
}

TIER_COMMANDS: dict[int, list[str]] = {
    1: ["init", "run discover", "run enrich", "status", "dashboard"],
    2: ["run score", "run tailor", "run cover", "run pdf", "run"],
    3: ["apply"],
}


def get_tier() -> int:
    """Detect the current tier based on available dependencies.

    Tier 1 (Discovery):            Python + pip
    Tier 2 (AI Scoring & Tailoring): + LLM API key
    Tier 3 (Full Auto-Apply):       + Claude Code CLI + Chrome
    """
    load_env()

    has_llm = any(os.environ.get(k) for k in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL"))
    if not has_llm:
        return 1

    has_claude = find_claude_cli() is not None
    try:
        get_chrome_path()
        has_chrome = True
    except FileNotFoundError:
        has_chrome = False

    if has_claude and has_chrome:
        return 3

    return 2


def check_tier(required: int, feature: str) -> None:
    """Raise SystemExit with a clear message if the current tier is too low.

    Args:
        required: Minimum tier needed (1, 2, or 3).
        feature: Human-readable description of the feature being gated.
    """
    current = get_tier()
    if current >= required:
        return

    from rich.console import Console
    _console = Console(stderr=True)

    missing: list[str] = []
    if required >= 2 and not any(os.environ.get(k) for k in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL")):
        missing.append("LLM API key — run [bold]jobpilot init[/bold] or set OPENROUTER_API_KEY")
    if required >= 3:
        if not find_claude_cli():
            missing.append("Claude Code CLI — install from [bold]https://claude.ai/code[/bold]")
        try:
            get_chrome_path()
        except FileNotFoundError:
            missing.append("Chrome/Chromium — install or set CHROME_PATH")

    _console.print(
        f"\n[red]'{feature}' requires {TIER_LABELS.get(required, f'Tier {required}')} (Tier {required}).[/red]\n"
        f"Current tier: {TIER_LABELS.get(current, f'Tier {current}')} (Tier {current})."
    )
    if missing:
        _console.print("\n[yellow]Missing:[/yellow]")
        for m in missing:
            _console.print(f"  - {m}")
    _console.print()
    raise SystemExit(1)
