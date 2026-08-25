"""Shared, normalized result parsing for the apply engines.

Single source of truth for turning an agent's RESULT:... output into a clean,
canonical status string, and for classifying whether a failure is permanent.
Previously launcher.py and local_agent.py each had their own copy of this
logic and drifted: markdown artifacts ("page_error**") leaked through, and
near-identical reasons ("cloudflare_block", "cloudflare_blocked",
"cloudflare_ip_blocked", "site_blocked_by_cloudflare", ...) were recorded as
separate buckets, so the database hid how often the pipeline actually hit
antibot walls. This module collapses those into canonical values.
"""

from __future__ import annotations

import re

# Simple RESULT codes that are always promotions, not failures.
_SIMPLE = ("APPLIED", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE")

# Canonical reason -> canonical parent reason (collapses near-identical words).
_VARIANTS: dict[str, str] = {
    # cloudflare / antibot family -> blocked_by_cloudflare
    "cloudflare_blocked": "blocked_by_cloudflare",
    "cloudflare_block": "blocked_by_cloudflare",
    "cloudflare_ip_blocked": "blocked_by_cloudflare",
    "cloudflare_ip_block": "blocked_by_cloudflare",
    "cloudflare_waf_block": "blocked_by_cloudflare",
    "cloudflare_blocking_access": "blocked_by_cloudflare",
    "site_blocked_by_cloudflare": "blocked_by_cloudflare",
    "blocked_by_cloudflare": "blocked_by_cloudflare",
    "cloudflare_waf": "blocked_by_cloudflare",
    # generic access / bot / security walls
    "access_blocked": "blocked_by_security",
    "blocked_by_security": "blocked_by_security",
    "bot_detection_ip_blocked": "blocked_by_security",
    "site_blocked": "blocked_by_security",
    "geo_blocked": "blocked_by_security",
    "blocked_by_site": "blocked_by_security",
    "blocked_by_indeed": "blocked_by_indeed",
    # rate limiting
    "rate_limited": "rate_limited",
    "rate_limited_30_mins": "rate_limited",
    # browser-tool bring-up / availability failures
    "browser_tools_unavailable": "browser_tools_unavailable",
    "browser_automation_unavailable": "browser_tools_unavailable",
    "missing_browser_tools": "browser_tools_unavailable",
    "no_browser_tools": "browser_tools_unavailable",
    "tools_unavailable": "browser_tools_unavailable",
    "unavailable_tools": "browser_tools_unavailable",
    "browser_unavailable": "browser_tools_unavailable",
    # page-level problems
    "page_error": "page_error",
    "page_blocked": "page_error",
    "page_inaccessible": "page_error",
    "network_blocked": "page_error",
    "page_ip_blocked": "page_error",
    # login / account family
    "login_issue": "login_issue",
    "account_required": "account_required",
    "sso_required": "sso_required",
    # verification family
    "verification_failure": "verification_required",
    "verification_lockout": "verification_required",
    "verification_code_invalid": "verification_required",
    "verification_codes_invalid": "verification_required",
    "verification_codes_rejected": "verification_required",
    "verification_system_broken": "verification_required",
    "email_verification_required": "verification_required",
    # profile/eligibility family
    "not_eligible_location": "not_eligible_location",
    "not_eligible_work_auth": "not_eligible_work_auth",
    "not_eligible_salary": "not_eligible_salary",
    # safety family
    "unsafe_permissions": "unsafe_permissions",
    "unsafe_verification": "unsafe_verification",
    # object-level
    "already_applied": "already_applied",
    "not_a_job_application": "not_a_job_application",
    "expired": "expired",
    "captcha": "captcha",
    "stuck": "stuck",
    "account_recovery_needed": "login_issue",
}

# Upper-bound: any reason starting with these stems is folded to that reason.
_PREFIX_MAP = {
    "blocked_by_cloudflare": "blocked_by_cloudflare",
    "cloudflare": "blocked_by_cloudflare",
    "blocked_by_security": "blocked_by_security",
    "blocked_by_indeed": "blocked_by_indeed",
    "browser_unavailable": "browser_tools_unavailable",
}

# Failures that will never clear by retrying the same job / session.
_PERMANENT: frozenset[str] = frozenset({
    "expired",
    "captcha",
    "login_issue",
    "account_required",
    "sso_required",
    "verification_required",
    "not_eligible_location",
    "not_eligible_work_auth",
    "not_eligible_salary",
    "already_applied",
    "not_a_job_application",
    "unsafe_permissions",
    "unsafe_verification",
    "blocked_by_cloudflare",
    "blocked_by_security",
    "blocked_by_indeed",
})


# characters stripped from reason edges: whitespace, markdown emphasis/backticks,
# quote chars, brackets, and sentence punctuation (models leak all of these).
_TAIL_TRIM = " \t\n\r*`\"'#[].,;:!?"
_HEAD_TRIM = " \t\n\r*`\"'#["  # keep leading = / - (reason fragments)


def _clean(raw: str) -> str:
    """Strip surrounding markdown/whitespace and collapse inner whitespace."""
    s = raw.strip()
    s = s.rstrip(_TAIL_TRIM).lstrip(_HEAD_TRIM)
    s = s.replace("**", "").replace("``", "").replace("`", "")
    s = " ".join(s.split()).lower()
    return s

def normalize_reason(raw: str) -> str:
    """Collapse an arbitrary failure reason into a canonical one.

    Handles asterisk/backtick leaks from LLM output, case, and the many
    spelling variants for the same underlying wall.
    """
    if not raw:
        return "unknown"
    cleaned = _clean(raw)
    if not cleaned:
        return "unknown"
    # exact canonical / variant match
    if cleaned in _VARIANTS:
        return _VARIANTS[cleaned]
    # prefix match (e.g. cloudflare_<something else>, browser_unavailable:cdp_timeout)
    for stem, reason in _PREFIX_MAP.items():
        if cleaned.startswith(stem):
            return reason
    return cleaned


def extract_result(text: str) -> str | None:
    """Parse an agent transcript into a normalized status string.

    Mirrors the original local_agent._extract_result contract:
      "applied" / "expired" / "captcha" / "login_issue"
      "failed:<canonical_reason>"
      None if no RESULT marker is present.
    """
    if not text:
        return None
    for code in _SIMPLE:
        if f"RESULT:{code}" in text:
            return code.lower()
    if "RESULT:FAILED" in text:
        fallback = None
        for line in text.split("\n"):
            if "RESULT:FAILED" not in line:
                continue
            idx = line.index("FAILED")
            tail = line[idx + 6:]                      # after "FAILED"
            # RESULT:FAILED:<reason>  or  RESULT:FAILED
            if ":" in tail:
                reason = tail.split(":", 1)[1].strip()
            else:
                reason = line[idx + 6:].strip()
            reason = normalize_reason(reason)
            if reason in ("captcha", "expired", "login_issue"):
                return reason
            fallback = f"failed:{reason}"
        return fallback or "failed:unknown"
    return None


def is_permanent_failure(result: str) -> bool:
    """Whether a status string should never be retried.

    Accepts either a bare canonical reason or a "failed:<reason>" status.
    """
    reason = result.split(":", 1)[-1] if ":" in result else result
    return normalize_reason(reason) in _PERMANENT
