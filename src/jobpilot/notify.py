"""Desktop notifications for JobPilot.

The fast lane (`jobpilot watch`) exists so you hear about a fresh, high-fit
job within minutes instead of days. That only works if something actually
pokes you, so this module is deliberately dependency-free and never raises:
a broken notifier must never take down a pipeline run.

Backends, in order of preference:
  1. Windows toast via PowerShell + Windows.UI.Notifications (native, no deps)
  2. Windows balloon tip via System.Windows.Forms.NotifyIcon (older shells)
  3. Log line only (any other OS, or when notifications are disabled)

Disable entirely with JOBPILOT_NOTIFY=0.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape as _sax_escape


def _xml_escape(text: str) -> str:
    """Escape for both XML text and attribute values.

    saxutils.escape handles & < > but NOT quotes, and these strings go into
    attributes (launch=, arguments=). An unescaped double quote there ends the
    attribute early and produces malformed toast XML -- job URLs carry
    arbitrary query strings, so this is reachable, not theoretical.
    """
    return _sax_escape(str(text), {'"': "&quot;", "'": "&apos;"})

log = logging.getLogger(__name__)

# PowerShell's registered AppUserModelID. Reusing it means toasts appear
# without having to install a Start Menu shortcut for JobPilot first.
_PS_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_TOAST_TIMEOUT = 20  # seconds; a hung PowerShell must not stall the pipeline


def enabled() -> bool:
    """False when the user has switched notifications off."""
    return os.environ.get("JOBPILOT_NOTIFY", "1").strip().lower() not in ("0", "false", "no", "off")


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
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                         encoding="utf-8-sig") as fh:
            fh.write(script)
            tmp = fh.name

        proc = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", tmp],
            capture_output=True, text=True, timeout=_TOAST_TIMEOUT,
        )
        if proc.returncode != 0:
            log.debug("Notification backend failed (rc=%s): %s",
                      proc.returncode, (proc.stderr or "").strip()[:300])
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


def _toast_windows(title: str, message: str, subtitle: str = "",
                   launch: str = "") -> bool:
    """Native Windows 10/11 toast via the WinRT notification manager.

    `launch` is the URI opened when the toast body is clicked. It must be a
    real URI: protocol activation with an empty launch string produces a toast
    that appears normally and does nothing at all when clicked, which is
    exactly the wrong behaviour for an alert whose entire purpose is to get
    you onto the job page before someone else applies.
    """
    lines = "".join(
        f"<text>{_xml_escape(t)}</text>"
        for t in (title, message, subtitle) if t
    )
    # Only http(s) is safe to hand to protocol activation from here.
    launch_uri = launch if launch.startswith(("http://", "https://")) else ""
    launch_attr = f' launch="{_xml_escape(launch_uri)}"' if launch_uri else ""
    actions = ""
    if launch_uri:
        actions = (
            "<actions>"
            f'<action content="Open job" activationType="protocol" '
            f'arguments="{_xml_escape(launch_uri)}"/>'
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
        launch: http(s) URL opened when the toast (or its "Open job" button)
            is clicked. Omit for informational toasts.

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
# JobPilot-specific helpers
# ---------------------------------------------------------------------------

def notify_fresh_job(job: dict, prepped: bool = False) -> bool:
    """Alert about a single fresh high-fit job."""
    score = job.get("fit_score")
    title_text = job.get("title") or "Untitled role"
    site = job.get("site") or "?"
    location = job.get("location") or ""

    head = f"Fit {score}/10 — {site}" if score is not None else f"New job — {site}"
    body = title_text[:90]
    tail = location[:60]
    if prepped:
        tail = (tail + " · resume + cover letter ready").strip(" ·")

    # The apply URL when enrichment found one, else the listing. Clicking the
    # toast is the whole point now that applications are submitted by hand.
    target = job.get("application_url") or job.get("url") or ""
    return notify(head, body, tail, launch=target)


def notify_batch(jobs: list[dict], prepped: bool = False) -> bool:
    """Alert about a batch. One toast per job up to 3, then a summary."""
    if not jobs:
        return False

    if len(jobs) <= 3:
        return any([notify_fresh_job(j, prepped=prepped) for j in jobs])

    best = max(jobs, key=lambda j: j.get("fit_score") or 0)
    return notify(
        f"{len(jobs)} fresh jobs matched",
        f"Top: {(best.get('title') or '?')[:70]} (fit {best.get('fit_score')}/10)",
        "Click to open the top match" + (" -- all prepped" if prepped else ""),
        launch=best.get("application_url") or best.get("url") or "",
    )
