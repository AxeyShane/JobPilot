"""Desktop and remote notifications for JobPilot.

The fast lane (`jobpilot watch`) exists so you hear about a fresh, high-fit
job within minutes instead of days. That only works if something actually
pokes you, so this module is deliberately dependency-free and never raises:
a broken notifier must never take down a pipeline run.

Backends:
  1. Windows toast via PowerShell + Windows.UI.Notifications (native, no deps)
     or balloon tip via System.Windows.Forms.NotifyIcon (older shells)
  2. ntfy via plain HTTP POST (urllib, dependency-free) to ntfy.sh or custom server
  3. Apprise (optional multi-channel push, only if installed and configured)

Configuration via environment variables:
  - JOBPILOT_NOTIFY: "0", "false", "no", "off" disables all notifications.
  - JOBPILOT_NTFY_TOPIC: Topic name for ntfy.sh (e.g. "jobpilot-myalerts").
  - JOBPILOT_NTFY_URL: Custom ntfy server URL (default: "https://ntfy.sh").
  - JOBPILOT_APPRISE_URL: Apprise target URL (e.g. "tgram://...", "pushover://...").
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as _sax_escape

log = logging.getLogger(__name__)

# PowerShell's registered AppUserModelID. Reusing it means toasts appear
# without having to install a Start Menu shortcut for JobPilot first.
_PS_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_TOAST_TIMEOUT = 20  # seconds; a hung PowerShell must not stall the pipeline
_HTTP_TIMEOUT = 10.0  # seconds; remote HTTP push timeout


def _xml_escape(text: str) -> str:
    """Escape for both XML text and attribute values.

    saxutils.escape handles & < > but NOT quotes, and these strings go into
    attributes (launch=, arguments=). An unescaped double quote there ends the
    attribute early and produces malformed toast XML -- job URLs carry
    arbitrary query strings, so this is reachable, not theoretical.
    """
    return _sax_escape(str(text), {'"': "&quot;", "'": "&apos;"})


def enabled() -> bool:
    """False when the user has switched notifications off."""
    return os.environ.get("JOBPILOT_NOTIFY", "1").strip().lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# Windows Desktop Toast / Balloon Backends
# ---------------------------------------------------------------------------

def _powershell() -> str | None:
    """Locate a PowerShell interpreter, preferring Windows PowerShell."""
    for exe in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        found = shutil.which(exe)
        if found:
            return found
    return None


def _run_ps(script: str) -> bool:
    """Run a PowerShell script from a temp file. Returns True on exit code 0."""
    ps = _powershell()
    if not ps:
        return False

    tmp = None
    try:
        # -File avoids the quoting minefield of -Command with embedded XML.
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8-sig") as fh:
            fh.write(script)
            tmp = fh.name

        proc = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", tmp],
            capture_output=True,
            text=True,
            timeout=_TOAST_TIMEOUT,
            check=False,
        )
        if proc.returncode != 0:
            log.debug("Notification backend failed (rc=%s): %s", proc.returncode, (proc.stderr or "").strip()[:300])
            return False
        return True
    except subprocess.TimeoutExpired:
        log.debug("Notification backend timed out after %ss", _TOAST_TIMEOUT)
        return False
    except Exception:
        log.debug("Notification backend raised", exc_info=True)
        return False
    finally:
        if tmp:
            try:
                Path(tmp).unlink()
            except OSError:
                pass


def _toast_windows(title: str, message: str, subtitle: str = "", launch: str = "") -> bool:
    """Native Windows 10/11 toast via the WinRT notification manager."""
    lines = "".join(f"<text>{_xml_escape(t)}</text>" for t in (title, message, subtitle) if t)
    # Only http(s) is safe to hand to protocol activation from here.
    launch_uri = launch if launch.startswith(("http://", "https://")) else ""
    launch_attr = f' launch="{_xml_escape(launch_uri)}"' if launch_uri else ""
    actions = ""
    if launch_uri:
        actions = (
            "<actions>"
            f'<action content="Open job" activationType="protocol" arguments="{_xml_escape(launch_uri)}"/>'
            "</actions>"
        )
    script = f"""
$ErrorActionPreference = "Stop"
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null

$xml = @"
<toast activationType="protocol"{launch_attr}>
  <visual><binding template="ToastGeneric">{lines}</binding></visual>
  {actions}
  <audio src="ms-winsoundevent:Notification.Default"/>
</toast>
"@

$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$toast = New-Object Windows.UI.Notifications.ToastNotification $doc
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{_PS_APP_ID}').Show($toast)
"""
    return _run_ps(script)


def _balloon_windows(title: str, message: str) -> bool:
    """Fallback for shells where the WinRT toast manager is unavailable."""
    def ps_str(v: str) -> str:
        return "'" + v.replace("'", "''") + "'"

    script = f"""
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
$icon = New-Object System.Windows.Forms.NotifyIcon
$icon.Icon = [System.Drawing.SystemIcons]::Information
$icon.BalloonTipTitle = {ps_str(title)}
$icon.BalloonTipText = {ps_str(message)}
$icon.Visible = $true
$icon.ShowBalloonTip(10000)
Start-Sleep -Seconds 10
$icon.Dispose()
"""
    return _run_ps(script)


def notify(title: str, message: str, subtitle: str = "", launch: str = "") -> bool:
    """Send a desktop notification. Never raises.

    Args:
        title: Toast title / header.
        message: Main notification body.
        subtitle: Secondary detail line.
        launch: http(s) URL opened when the toast is clicked.

    Returns True if a backend accepted it, False if it only got logged.
    """
    banner = f"{title} — {message}" + (f" ({subtitle})" if subtitle else "")

    if not enabled():
        log.info("NOTIFY (suppressed): %s", banner)
        return False

    if platform.system() == "Windows":
        if _toast_windows(title, message, subtitle, launch=launch):
            log.info("NOTIFY: %s", banner)
            return True
        if _balloon_windows(title, f"{message} {subtitle}".strip()):
            log.info("NOTIFY (balloon): %s", banner)
            return True

    log.info("NOTIFY (log only): %s", banner)
    return False


# ---------------------------------------------------------------------------
# Remote / Mobile Backends (ntfy & Apprise)
# ---------------------------------------------------------------------------

def send_ntfy(
    title: str,
    message: str,
    topic: str | None = None,
    base_url: str | None = None,
    priority: int | str = 3,
    tags: list[str] | str | None = None,
    click: str = "",
    timeout: float = _HTTP_TIMEOUT,
) -> bool:
    """Send notification via ntfy HTTP POST. Never raises.

    Configured via env:
        JOBPILOT_NTFY_TOPIC: topic name (required to send)
        JOBPILOT_NTFY_URL: base ntfy URL (default: https://ntfy.sh)
    """
    if not enabled():
        return False

    resolved_topic = (topic if topic is not None else os.environ.get("JOBPILOT_NTFY_TOPIC", "")).strip()
    if not resolved_topic:
        return False

    resolved_url = (base_url if base_url is not None else os.environ.get("JOBPILOT_NTFY_URL", "https://ntfy.sh")).strip()
    if not resolved_url:
        resolved_url = "https://ntfy.sh"

    target = f"{resolved_url.rstrip('/')}/{resolved_topic}"

    tag_list: list[str] = []
    if isinstance(tags, list):
        tag_list = [str(t) for t in tags if t]
    elif isinstance(tags, str) and tags.strip():
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    payload: dict[str, Any] = {
        "topic": resolved_topic,
        "title": str(title),
        "message": str(message),
    }
    if priority is not None:
        payload["priority"] = priority
    if tag_list:
        payload["tags"] = tag_list
    if click and str(click).startswith(("http://", "https://")):
        payload["click"] = str(click)

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            target,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", getattr(resp, "code", 200))
            if 200 <= status < 300:
                log.info("NTFY [%s]: %s — %s", resolved_topic, title, message)
                return True
            log.debug("NTFY returned non-2xx status: %s", status)
            return False
    except Exception as e:
        log.debug("NTFY send failed: %s", e)
        return False


def send_apprise(
    title: str,
    message: str,
    apprise_url: str | None = None,
    url: str = "",
) -> bool:
    """Send notification via Apprise if installed and configured. Never raises.

    Configured via env:
        JOBPILOT_APPRISE_URL: Apprise target URL(s) (e.g. tgram://..., pushover://...)
    """
    if not enabled():
        return False

    resolved_url = (apprise_url if apprise_url is not None else os.environ.get("JOBPILOT_APPRISE_URL", "")).strip()
    if not resolved_url:
        return False

    try:
        import apprise  # lazy import
    except ImportError:
        log.debug("Apprise is not installed; skipping")
        return False
    except Exception as e:
        log.debug("Failed to import apprise: %s", e)
        return False

    try:
        ap = apprise.Apprise()
        ap.add(resolved_url)
        body = f"{message}\n{url}".strip() if url and url.startswith(("http://", "https://")) else message
        res = ap.notify(title=title, body=body)
        if res:
            log.info("APPRISE: %s — %s", title, message)
            return True
        log.debug("Apprise notify returned False")
        return False
    except Exception as e:
        log.debug("Apprise notify raised: %s", e)
        return False


# ---------------------------------------------------------------------------
# Event Formatting & Unified Entrypoint
# ---------------------------------------------------------------------------

def format_event(kind: str, **fields: Any) -> dict[str, Any]:
    """Format an event kind and field dictionary into notification attributes.

    Returns dict with keys: title, message, subtitle, launch, priority, tags.
    """
    if kind == "new_match":
        job = fields.get("job") if isinstance(fields.get("job"), dict) else {}
        count = fields.get("count")
        if count is not None and int(count) > 1 and not job:
            n = int(count)
            scored = fields.get("scored")
            title = f"{n} fresh jobs matched"
            if scored is not None and scored != n:
                message = f"Found {n} high-fit matches out of {scored} scored jobs"
            else:
                message = f"Found {n} fresh high-fit job matches"
            best_title = fields.get("best_title")
            best_score = fields.get("best_score")
            if best_title:
                message += f" · Top: {best_title}" + (f" ({best_score}/10)" if best_score is not None else "")
            subtitle = ""
            launch = str(fields.get("url") or fields.get("application_url") or "")
        else:
            score = fields.get("fit_score") or fields.get("score") or job.get("fit_score") or job.get("score")
            title_text = fields.get("title") or job.get("title") or "Untitled role"
            site = fields.get("company") or fields.get("site") or job.get("company") or job.get("site") or "?"
            location = fields.get("location") or job.get("location") or ""
            prepped = bool(fields.get("prepped", False))

            title = f"Fit {score}/10 — {site}" if score is not None else f"New job — {site}"
            message = title_text[:90]
            subtitle = location[:60]
            if prepped:
                subtitle = (subtitle + " · resume + cover letter ready").strip(" ·")
            launch = str(
                fields.get("application_url") or fields.get("url") or job.get("application_url") or job.get("url") or ""
            )

        return {
            "title": title,
            "message": message,
            "subtitle": subtitle,
            "launch": launch,
            "priority": 4,  # high
            "tags": ["briefcase", "star"],
        }

    if kind == "applied":
        job = fields.get("job") if isinstance(fields.get("job"), dict) else {}
        title_text = fields.get("title") or job.get("title") or "Job application"
        company = fields.get("company") or job.get("company") or job.get("site") or ""
        method = fields.get("method") or ""
        launch = str(
            fields.get("url") or fields.get("application_url") or job.get("url") or job.get("application_url") or ""
        )

        title = f"Applied: {company}" if company else f"Applied: {title_text[:40]}"
        message = f"Application submitted for {title_text}" + (f" at {company}" if company else "")
        if method:
            message += f" via {method}"
        return {
            "title": title,
            "message": message,
            "subtitle": company,
            "launch": launch,
            "priority": 3,  # default
            "tags": ["white_check_mark", "memo"],
        }

    if kind == "apply_failed":
        job = fields.get("job") if isinstance(fields.get("job"), dict) else {}
        title_text = fields.get("title") or job.get("title") or "Job application"
        company = fields.get("company") or job.get("company") or job.get("site") or ""
        error = fields.get("error") or fields.get("reason") or "Application attempt failed"
        launch = str(
            fields.get("url") or fields.get("application_url") or job.get("url") or job.get("application_url") or ""
        )

        title = f"Apply Failed: {company or title_text[:30]}"
        message = f"Failed applying to {title_text}" + (f" at {company}" if company else "") + f": {error}"
        return {
            "title": title,
            "message": message,
            "subtitle": str(error)[:60],
            "launch": launch,
            "priority": 4,  # high
            "tags": ["warning", "x"],
        }

    if kind == "scam_blocked":
        job = fields.get("job") if isinstance(fields.get("job"), dict) else {}
        title_text = fields.get("title") or job.get("title") or "Job posting"
        company = fields.get("company") or job.get("company") or job.get("site") or ""
        reason = fields.get("reason") or fields.get("rule") or fields.get("category") or "High scam risk detected"
        launch = str(
            fields.get("url") or fields.get("application_url") or job.get("url") or job.get("application_url") or ""
        )

        title = f"Scam Blocked: {company or title_text[:30]}"
        message = f"Blocked posting '{title_text}'" + (f" ({company})" if company else "") + f" — {reason}"
        return {
            "title": title,
            "message": message,
            "subtitle": str(reason)[:60],
            "launch": launch,
            "priority": 2,  # low
            "tags": ["shield", "no_entry"],
        }

    if kind == "run_summary":
        elapsed = float(fields.get("elapsed", 0.0))
        scored = fields.get("scored", 0)
        tailored = fields.get("tailored", 0)
        ready = fields.get("ready") if fields.get("ready") is not None else fields.get("ready_to_apply", 0)
        applied = fields.get("applied", 0)
        errors = fields.get("errors")
        new_matches = fields.get("new_matches") or fields.get("new_match_count")

        title = "JobPilot Run Complete"
        parts = [f"Elapsed: {elapsed:.1f}s"]
        if scored:
            parts.append(f"Scored: {scored}")
        if new_matches:
            parts.append(f"Matches: {new_matches}")
        if tailored:
            parts.append(f"Tailored: {tailored}")
        if ready:
            parts.append(f"Ready: {ready}")
        if applied:
            parts.append(f"Applied: {applied}")
        if errors:
            err_count = len(errors) if isinstance(errors, (dict, list)) else errors
            parts.append(f"Errors: {err_count}")

        message = " · ".join(parts) if parts else "Pipeline run finished."
        return {
            "title": title,
            "message": message,
            "subtitle": f"{elapsed:.1f}s",
            "launch": "",
            "priority": 2,  # low
            "tags": ["bar_chart", "robot"],
        }

    # Generic / custom event fallback
    title = f"JobPilot: {kind.replace('_', ' ').title()}"
    message = ", ".join(f"{k}={v}" for k, v in fields.items()) if fields else f"Event: {kind}"
    return {
        "title": title,
        "message": message,
        "subtitle": "",
        "launch": str(fields.get("url") or fields.get("application_url") or ""),
        "priority": 3,
        "tags": ["bell"],
    }


def notify_event(kind: str, **fields: Any) -> dict[str, bool]:
    """Single entrypoint to format an event and fan out across all backends.

    Kinds:
        'new_match': Fresh high-fit job match (single job or batch count)
        'applied': Job application submitted
        'apply_failed': Job application failed
        'scam_blocked': Scam posting detected and blocked
        'run_summary': Pipeline execution summary

    Fans out to:
        - Desktop toast (Windows PowerShell / balloon / log)
        - ntfy (if JOBPILOT_NTFY_TOPIC is set)
        - Apprise (if JOBPILOT_APPRISE_URL is set and apprise is installed)

    Never raises -- failures log and fall through.
    """
    try:
        formatted = format_event(kind, **fields)
    except Exception as e:
        log.debug("format_event raised for %s: %s", kind, e)
        formatted = {
            "title": f"JobPilot: {kind}",
            "message": str(fields),
            "subtitle": "",
            "launch": "",
            "priority": 3,
            "tags": ["bell"],
        }

    title = formatted.get("title", f"JobPilot: {kind}")
    message = formatted.get("message", "")
    subtitle = formatted.get("subtitle", "")
    launch = formatted.get("launch", "")
    priority = formatted.get("priority", 3)
    tags = formatted.get("tags", [])

    results = {
        "desktop": False,
        "ntfy": False,
        "apprise": False,
    }

    if not enabled():
        log.info("NOTIFY (suppressed): [%s] %s — %s", kind, title, message)
        return results

    # 1. Desktop toast
    try:
        results["desktop"] = notify(title, message, subtitle=subtitle, launch=launch)
    except Exception as e:
        log.debug("Desktop notify failed: %s", e)

    # 2. ntfy
    try:
        results["ntfy"] = send_ntfy(
            title=title,
            message=message,
            priority=priority,
            tags=tags,
            click=launch,
        )
    except Exception as e:
        log.debug("ntfy notify failed: %s", e)

    # 3. Apprise
    try:
        results["apprise"] = send_apprise(
            title=title,
            message=message,
            url=launch,
        )
    except Exception as e:
        log.debug("apprise notify failed: %s", e)

    return results


# ---------------------------------------------------------------------------
# JobPilot-specific helpers (backward compatible)
# ---------------------------------------------------------------------------

def notify_fresh_job(job: dict, prepped: bool = False) -> bool:
    """Alert about a single fresh high-fit job across configured backends."""
    results = notify_event("new_match", job=job, prepped=prepped)
    return any(results.values())


def notify_batch(jobs: list[dict], prepped: bool = False) -> bool:
    """Alert about a batch. One toast per job up to 3, then a summary."""
    if not jobs:
        return False

    if len(jobs) <= 3:
        return any(notify_fresh_job(j, prepped=prepped) for j in jobs)

    best = max(jobs, key=lambda j: j.get("fit_score") or 0)
    results = notify_event(
        "new_match",
        count=len(jobs),
        best_title=best.get("title") or "?",
        best_score=best.get("fit_score") or 0,
        url=best.get("application_url") or best.get("url") or "",
        prepped=prepped,
    )
    return any(results.values())
