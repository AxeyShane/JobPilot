"""Unit and integration tests for JobPilot notifications (desktop, ntfy, apprise).

Script-style test suite following repo convention (like test_scam_gate.py).
Zero external network calls; all HTTP/OS backends are mocked.
"""

from __future__ import annotations

import io
import json
import os
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure src is importable
repo_root = Path(__file__).resolve().parent.parent
src_dir = repo_root / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from jobpilot.notify import (
    enabled,
    format_event,
    notify,
    notify_batch,
    notify_event,
    notify_fresh_job,
    send_apprise,
    send_ntfy,
)

RESULT: list[bool] = []


def check(name: str, cond: bool) -> None:
    RESULT.append(bool(cond))
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}")
    if not cond:
        print(f"  FAILED: {name}")


# ---------------------------------------------------------------------------
# 1. Environment & Global Enabled Flag
# ---------------------------------------------------------------------------

def test_enabled_flag():
    orig = os.environ.get("JOBPILOT_NOTIFY")
    try:
        os.environ.pop("JOBPILOT_NOTIFY", None)
        check("enabled: default is True", enabled() is True)

        os.environ["JOBPILOT_NOTIFY"] = "1"
        check("enabled: '1' is True", enabled() is True)

        os.environ["JOBPILOT_NOTIFY"] = "true"
        check("enabled: 'true' is True", enabled() is True)

        os.environ["JOBPILOT_NOTIFY"] = "0"
        check("enabled: '0' is False", enabled() is False)

        os.environ["JOBPILOT_NOTIFY"] = "false"
        check("enabled: 'false' is False", enabled() is False)

        os.environ["JOBPILOT_NOTIFY"] = "no"
        check("enabled: 'no' is False", enabled() is False)

        os.environ["JOBPILOT_NOTIFY"] = "off"
        check("enabled: 'off' is False", enabled() is False)
    finally:
        if orig is not None:
            os.environ["JOBPILOT_NOTIFY"] = orig
        else:
            os.environ.pop("JOBPILOT_NOTIFY", None)


test_enabled_flag()


# ---------------------------------------------------------------------------
# 2. Event Formatting Unit Tests
# ---------------------------------------------------------------------------

def test_formatting_new_match():
    # Single job with score
    job_single = {
        "title": "Senior Python Backend Engineer",
        "company": "Anthropic",
        "fit_score": 9,
        "location": "San Francisco, CA",
        "application_url": "https://anthropic.com/careers/123",
    }
    fmt1 = format_event("new_match", job=job_single, prepped=True)
    check("new_match single: title contains score and company", "Fit 9/10 — Anthropic" in fmt1["title"])
    check("new_match single: message contains role title", "Senior Python Backend Engineer" in fmt1["message"])
    check("new_match single: subtitle has location + prepped", "San Francisco, CA" in fmt1["subtitle"] and "ready" in fmt1["subtitle"])
    check("new_match single: launch URL preserved", fmt1["launch"] == "https://anthropic.com/careers/123")
    check("new_match single: high priority", fmt1["priority"] == 4)
    check("new_match single: tags present", "briefcase" in fmt1["tags"])

    # Batch count
    fmt2 = format_event("new_match", count=5, scored=12, best_title="Lead AI Engineer", best_score=10)
    check("new_match batch: title contains count", "5 fresh jobs matched" in fmt2["title"])
    check("new_match batch: message has count and best job", "5 high-fit matches" in fmt2["message"] and "Lead AI Engineer" in fmt2["message"])
    check("new_match batch: priority is 4", fmt2["priority"] == 4)


test_formatting_new_match()


def test_formatting_applied():
    fmt = format_event(
        "applied",
        title="ML Platform Engineer",
        company="OpenAI",
        method="workday",
        url="https://openai.com/jobs/456",
    )
    check("applied: title contains company or role", "Applied: OpenAI" in fmt["title"])
    check("applied: message contains role, company, method", "ML Platform Engineer" in fmt["message"] and "OpenAI" in fmt["message"] and "workday" in fmt["message"])
    check("applied: launch URL", fmt["launch"] == "https://openai.com/jobs/456")
    check("applied: priority is 3", fmt["priority"] == 3)
    check("applied: tags contain checkmark", "white_check_mark" in fmt["tags"])


test_formatting_applied()


def test_formatting_apply_failed():
    fmt = format_event(
        "apply_failed",
        title="Distributed Systems Lead",
        company="Meta",
        error="CAPTCHA challenge blocked automation",
        url="https://meta.com/jobs/789",
    )
    check("apply_failed: title contains Apply Failed", "Apply Failed" in fmt["title"])
    check("apply_failed: message contains error description", "CAPTCHA challenge blocked automation" in fmt["message"])
    check("apply_failed: priority is 4", fmt["priority"] == 4)
    check("apply_failed: tags contain warning", "warning" in fmt["tags"])


test_formatting_apply_failed()


def test_formatting_scam_blocked():
    fmt = format_event(
        "scam_blocked",
        title="Remote Virtual Assistant",
        company="Suspicious LLC",
        reason="Upfront equipment fee required",
    )
    check("scam_blocked: title contains Scam Blocked", "Scam Blocked" in fmt["title"])
    check("scam_blocked: message contains reason", "Upfront equipment fee required" in fmt["message"])
    check("scam_blocked: priority is 2", fmt["priority"] == 2)
    check("scam_blocked: tags contain shield", "shield" in fmt["tags"])


test_formatting_scam_blocked()


def test_formatting_run_summary():
    fmt = format_event(
        "run_summary",
        elapsed=15.4,
        total=150,
        scored=25,
        new_matches=4,
        tailored=3,
        ready=3,
        applied=2,
        errors={"enrich": "rate limit"},
    )
    check("run_summary: title is JobPilot Run Complete", fmt["title"] == "JobPilot Run Complete")
    check("run_summary: message contains elapsed time", "15.4s" in fmt["message"])
    check("run_summary: message contains scored and ready stats", "Scored: 25" in fmt["message"] and "Ready: 3" in fmt["message"])
    check("run_summary: message contains error count", "Errors: 1" in fmt["message"])
    check("run_summary: priority is 2", fmt["priority"] == 2)
    check("run_summary: tags contain robot or chart", "robot" in fmt["tags"] or "bar_chart" in fmt["tags"])


test_formatting_run_summary()


def test_formatting_fallback():
    fmt = format_event("unknown_custom_kind", key1="val1", key2=123)
    check("fallback: formats kind title", "Unknown Custom Kind" in fmt["title"])
    check("fallback: contains field key1", "val1" in fmt["message"])
    check("fallback: priority is 3", fmt["priority"] == 3)


test_formatting_fallback()


# ---------------------------------------------------------------------------
# 3. ntfy Backend Unit Tests (Mocked HTTP, Zero Network)
# ---------------------------------------------------------------------------

class MockHttpResponse:
    def __init__(self, status: int = 200, body: bytes = b'{"ok": true}'):
        self.status = status
        self.code = status
        self._body = io.BytesIO(body)

    def read(self, *args, **kwargs):
        return self._body.read(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_ntfy_send_success():
    orig_topic = os.environ.get("JOBPILOT_NTFY_TOPIC")
    orig_url = os.environ.get("JOBPILOT_NTFY_URL")
    orig_notify = os.environ.get("JOBPILOT_NOTIFY")

    captured_requests: list[urllib.request.Request] = []

    def mock_urlopen(req, timeout=None):
        captured_requests.append(req)
        return MockHttpResponse(status=200)

    try:
        os.environ["JOBPILOT_NOTIFY"] = "1"
        os.environ["JOBPILOT_NTFY_TOPIC"] = "jobpilot-test-alerts"
        os.environ["JOBPILOT_NTFY_URL"] = "https://ntfy.sh"

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ok = send_ntfy(
                title="Fresh Match",
                message="Staff Engineer at Google",
                priority=4,
                tags=["briefcase", "star"],
                click="https://careers.google.com/123",
            )

        check("ntfy: send_ntfy returned True on 200", ok is True)
        check("ntfy: HTTP request was captured", len(captured_requests) == 1)

        req = captured_requests[0]
        check("ntfy: target URL is correct endpoint", req.full_url == "https://ntfy.sh/jobpilot-test-alerts")
        check("ntfy: content-type header is JSON", "application/json" in req.headers.get("Content-type", ""))

        body = json.loads(req.data.decode("utf-8"))
        check("ntfy payload: topic", body.get("topic") == "jobpilot-test-alerts")
        check("ntfy payload: title", body.get("title") == "Fresh Match")
        check("ntfy payload: message", body.get("message") == "Staff Engineer at Google")
        check("ntfy payload: priority", body.get("priority") == 4)
        check("ntfy payload: tags", body.get("tags") == ["briefcase", "star"])
        check("ntfy payload: click URL", body.get("click") == "https://careers.google.com/123")
    finally:
        if orig_topic is not None:
            os.environ["JOBPILOT_NTFY_TOPIC"] = orig_topic
        else:
            os.environ.pop("JOBPILOT_NTFY_TOPIC", None)
        if orig_url is not None:
            os.environ["JOBPILOT_NTFY_URL"] = orig_url
        else:
            os.environ.pop("JOBPILOT_NTFY_URL", None)
        if orig_notify is not None:
            os.environ["JOBPILOT_NOTIFY"] = orig_notify
        else:
            os.environ.pop("JOBPILOT_NOTIFY", None)


test_ntfy_send_success()


def test_ntfy_custom_url():
    captured_requests: list[urllib.request.Request] = []

    def mock_urlopen(req, timeout=None):
        captured_requests.append(req)
        return MockHttpResponse(status=200)

    orig_topic = os.environ.get("JOBPILOT_NTFY_TOPIC")
    orig_url = os.environ.get("JOBPILOT_NTFY_URL")
    try:
        os.environ["JOBPILOT_NTFY_TOPIC"] = "internal-ops"
        os.environ["JOBPILOT_NTFY_URL"] = "https://custom-ntfy.corp.internal/"

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ok = send_ntfy(title="Test", message="Ping")

        check("ntfy custom URL: returned True", ok is True)
        check("ntfy custom URL: resolved endpoint without double slash", captured_requests[0].full_url == "https://custom-ntfy.corp.internal/internal-ops")
    finally:
        if orig_topic is not None:
            os.environ["JOBPILOT_NTFY_TOPIC"] = orig_topic
        else:
            os.environ.pop("JOBPILOT_NTFY_TOPIC", None)
        if orig_url is not None:
            os.environ["JOBPILOT_NTFY_URL"] = orig_url
        else:
            os.environ.pop("JOBPILOT_NTFY_URL", None)


test_ntfy_custom_url()


def test_ntfy_skipped_when_topic_unset():
    orig_topic = os.environ.get("JOBPILOT_NTFY_TOPIC")
    try:
        os.environ.pop("JOBPILOT_NTFY_TOPIC", None)
        with patch("urllib.request.urlopen") as mock_url:
            ok = send_ntfy(title="Test", message="Should not send")
            check("ntfy: skipped when topic unset", ok is False)
            check("ntfy: urlopen was not called", not mock_url.called)
    finally:
        if orig_topic is not None:
            os.environ["JOBPILOT_NTFY_TOPIC"] = orig_topic


test_ntfy_skipped_when_topic_unset()


def test_ntfy_error_never_raises():
    orig_topic = os.environ.get("JOBPILOT_NTFY_TOPIC")
    try:
        os.environ["JOBPILOT_NTFY_TOPIC"] = "test-topic"

        # 1. URLError network failure
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            ok1 = send_ntfy(title="Test", message="Network down")
            check("ntfy: URLError returns False without raising", ok1 is False)

        # 2. Timeout error
        with patch("urllib.request.urlopen", side_effect=TimeoutError("Request timed out")):
            ok2 = send_ntfy(title="Test", message="Timeout")
            check("ntfy: TimeoutError returns False without raising", ok2 is False)

        # 3. HTTP 500 error status
        with patch("urllib.request.urlopen", return_value=MockHttpResponse(status=500)):
            ok3 = send_ntfy(title="Test", message="Server 500")
            check("ntfy: HTTP 500 returns False without raising", ok3 is False)
    finally:
        if orig_topic is not None:
            os.environ["JOBPILOT_NTFY_TOPIC"] = orig_topic


test_ntfy_error_never_raises()


# ---------------------------------------------------------------------------
# 4. Apprise Backend Unit Tests
# ---------------------------------------------------------------------------

def test_apprise_skipped_when_url_unset():
    orig_url = os.environ.get("JOBPILOT_APPRISE_URL")
    try:
        os.environ.pop("JOBPILOT_APPRISE_URL", None)
        ok = send_apprise(title="Test", message="Should skip")
        check("apprise: skipped when URL unset", ok is False)
    finally:
        if orig_url is not None:
            os.environ["JOBPILOT_APPRISE_URL"] = orig_url


test_apprise_skipped_when_url_unset()


def test_apprise_absent_silent_skip():
    orig_url = os.environ.get("JOBPILOT_APPRISE_URL")
    try:
        os.environ["JOBPILOT_APPRISE_URL"] = "tgram://123456:ABC-DEF/123456789"
        with patch.dict(sys.modules, {"apprise": None}):
            ok = send_apprise(title="Test", message="Import fails")
            check("apprise: absent module skips silently", ok is False)
    finally:
        if orig_url is not None:
            os.environ["JOBPILOT_APPRISE_URL"] = orig_url


test_apprise_absent_silent_skip()


def test_apprise_mock_success():
    orig_url = os.environ.get("JOBPILOT_APPRISE_URL")
    try:
        os.environ["JOBPILOT_APPRISE_URL"] = "pushover://user@token"

        mock_apprise_mod = types.ModuleType("apprise")
        mock_apprise_inst = MagicMock()
        mock_apprise_inst.notify.return_value = True
        mock_apprise_cls = MagicMock(return_value=mock_apprise_inst)
        mock_apprise_mod.Apprise = mock_apprise_cls

        with patch.dict(sys.modules, {"apprise": mock_apprise_mod}):
            ok = send_apprise(
                title="Job Alert",
                message="New lead found",
                url="https://example.com/job/1",
            )

        check("apprise: mock send returned True", ok is True)
        mock_apprise_inst.add.assert_called_once_with("pushover://user@token")
        mock_apprise_inst.notify.assert_called_once()
        _, kwargs = mock_apprise_inst.notify.call_args
        check("apprise notify kwargs: title", kwargs.get("title") == "Job Alert")
        check("apprise notify kwargs: body contains URL", "https://example.com/job/1" in kwargs.get("body", ""))
    finally:
        if orig_url is not None:
            os.environ["JOBPILOT_APPRISE_URL"] = orig_url


test_apprise_mock_success()


def test_apprise_exception_never_raises():
    orig_url = os.environ.get("JOBPILOT_APPRISE_URL")
    try:
        os.environ["JOBPILOT_APPRISE_URL"] = "slack://token-a/token-b/token-c"

        mock_apprise_mod = types.ModuleType("apprise")
        mock_apprise_inst = MagicMock()
        mock_apprise_inst.notify.side_effect = RuntimeError("Slack API token expired")
        mock_apprise_cls = MagicMock(return_value=mock_apprise_inst)
        mock_apprise_mod.Apprise = mock_apprise_cls

        with patch.dict(sys.modules, {"apprise": mock_apprise_mod}):
            ok = send_apprise(title="Test", message="Will fail")
            check("apprise: exception in notify returns False without raising", ok is False)
    finally:
        if orig_url is not None:
            os.environ["JOBPILOT_APPRISE_URL"] = orig_url


test_apprise_exception_never_raises()


# ---------------------------------------------------------------------------
# 5. notify_event Fanout & Helpers Unit Tests
# ---------------------------------------------------------------------------

def test_desktop_notify_direct():
    with patch("jobpilot.notify.platform.system", return_value="Windows"),          patch("jobpilot.notify._toast_windows", return_value=True) as mock_toast:
        ok = notify("Header", "Body", "Sub", "https://example.com")
        check("desktop notify direct: returns True", ok is True)
        mock_toast.assert_called_once_with("Header", "Body", "Sub", launch="https://example.com")


test_desktop_notify_direct()


def test_notify_event_fanout():
    with patch("jobpilot.notify.notify", return_value=True) as mock_desk,          patch("jobpilot.notify.send_ntfy", return_value=True) as mock_ntfy,          patch("jobpilot.notify.send_apprise", return_value=True) as mock_app:

        res = notify_event(
            "applied",
            title="Backend Architect",
            company="Stripe",
            method="easy-apply",
            url="https://stripe.com/jobs/1",
        )

        check("notify_event: desktop called", mock_desk.called)
        check("notify_event: ntfy called", mock_ntfy.called)
        check("notify_event: apprise called", mock_app.called)
        check("notify_event: result dict values", res == {"desktop": True, "ntfy": True, "apprise": True})


test_notify_event_fanout()


def test_notify_disabled_skips_all():
    orig_notify = os.environ.get("JOBPILOT_NOTIFY")
    try:
        os.environ["JOBPILOT_NOTIFY"] = "0"
        with patch("jobpilot.notify.notify") as mock_desk,              patch("jobpilot.notify.send_ntfy") as mock_ntfy,              patch("jobpilot.notify.send_apprise") as mock_app:

            res = notify_event("new_match", count=3)
            check("notify_event suppressed: desktop not called", not mock_desk.called)
            check("notify_event suppressed: ntfy not called", not mock_ntfy.called)
            check("notify_event suppressed: apprise not called", not mock_app.called)
            check("notify_event suppressed: all False", res == {"desktop": False, "ntfy": False, "apprise": False})
    finally:
        if orig_notify is not None:
            os.environ["JOBPILOT_NOTIFY"] = orig_notify
        else:
            os.environ.pop("JOBPILOT_NOTIFY", None)


test_notify_disabled_skips_all()


def test_notify_fresh_job_and_batch_helpers():
    with patch("jobpilot.notify.notify_event", return_value={"desktop": True, "ntfy": False, "apprise": False}) as mock_ne:
        job = {"title": "Software Engineer", "company": "Apple", "fit_score": 8}
        ok1 = notify_fresh_job(job, prepped=True)
        check("notify_fresh_job: returns True", ok1 is True)
        check("notify_fresh_job: called notify_event", mock_ne.called)

    with patch("jobpilot.notify.notify_event", return_value={"desktop": True, "ntfy": False, "apprise": False}) as mock_ne:
        jobs = [
            {"title": "Job 1", "fit_score": 8},
            {"title": "Job 2", "fit_score": 9},
            {"title": "Job 3", "fit_score": 7},
            {"title": "Job 4", "fit_score": 9},
        ]
        ok2 = notify_batch(jobs)
        check("notify_batch (count > 3): returns True", ok2 is True)
        check("notify_batch: called notify_event with count", mock_ne.called)


test_notify_fresh_job_and_batch_helpers()


# ---------------------------------------------------------------------------
# 6. Pipeline Hook Integration Tests
# ---------------------------------------------------------------------------

def test_pipeline_score_hook():
    import jobpilot.pipeline as pipe_mod

    mock_score_result = {
        "scored": 4,
        "errors": 0,
        "unscored": 0,
        "elapsed": 2.5,
        "distribution": [(9, 2), (7, 1), (4, 1)],
    }

    with patch("jobpilot.scoring.scorer.run_scoring", return_value=mock_score_result),          patch("jobpilot.notify.notify_event") as mock_notify_event:

        res = pipe_mod._run_score(workers=1)

        check("pipeline _run_score: status is ok", res.get("status") == "ok")
        check("pipeline _run_score: notify_event was called", mock_notify_event.called)

        args, kwargs = mock_notify_event.call_args
        check("pipeline score hook: kind is new_match", args[0] == "new_match" if args else kwargs.get("kind") == "new_match")
        # 2 + 1 = 3 high fit matches (>= 7)
        check("pipeline score hook: count of high fit matches", kwargs.get("count") == 3)
        check("pipeline score hook: total scored count", kwargs.get("scored") == 4)


test_pipeline_score_hook()


def test_pipeline_run_summary_hook():
    import jobpilot.pipeline as pipe_mod

    mock_db_stats = {
        "total": 120,
        "with_description": 100,
        "scored": 50,
        "tailored": 10,
        "with_cover_letter": 10,
        "ready_to_apply": 10,
        "applied": 5,
        "pending_detail": 20,
    }

    mock_seq_result = {
        "stages": [{"stage": "score", "status": "ok", "elapsed": 3.0}],
        "errors": {},
        "elapsed": 3.0,
    }

    with patch("jobpilot.pipeline.load_env"),          patch("jobpilot.pipeline.ensure_dirs"),          patch("jobpilot.pipeline.init_db"),          patch("jobpilot.pipeline.get_stats", return_value=mock_db_stats),          patch("jobpilot.pipeline._run_sequential", return_value=mock_seq_result),          patch("jobpilot.notify.notify_event") as mock_notify_event:

        res = pipe_mod.run_pipeline(stages=["score"], dry_run=False)

        check("pipeline run_pipeline: returns result dict", "stages" in res)
        check("pipeline run_pipeline: notify_event called for run_summary", mock_notify_event.called)

        args, kwargs = mock_notify_event.call_args
        check("pipeline run_summary hook: kind is run_summary", args[0] == "run_summary" if args else kwargs.get("kind") == "run_summary")
        check("pipeline run_summary hook: total jobs in kwargs", kwargs.get("total") == 120)
        check("pipeline run_summary hook: scored in kwargs", kwargs.get("scored") == 50)
        check("pipeline run_summary hook: tailored in kwargs", kwargs.get("tailored") == 10)
        check("pipeline run_summary hook: ready in kwargs", kwargs.get("ready") == 10)
        check("pipeline run_summary hook: applied in kwargs", kwargs.get("applied") == 5)


test_pipeline_run_summary_hook()


# ---------------------------------------------------------------------------
# Test Summary
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    passed = sum(1 for x in RESULT if x)
    total = len(RESULT)
    print(f"\n{passed}/{total} checks passed")
    raise SystemExit(0 if passed == total else 1)
