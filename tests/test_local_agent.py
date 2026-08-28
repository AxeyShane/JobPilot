"""Tests for apply/local_agent.py: dry-run submit-detection heuristic and the
technical dry-run enforcement in _run_agent_turns.

Run:  cd <repo> && python3 tests/test_local_agent.py

Requires the same third-party deps as the real apply engine (httpx, mcp,
rich, python-dotenv, pyyaml -- see pyproject.toml); these are only needed
because importing jobpilot.apply.local_agent pulls in jobpilot.apply.dashboard
and jobpilot.llm at module load time, not because this file talks to a real
LLM, browser, or MCP server -- everything below is mocked.
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from jobpilot.apply.local_agent import _looks_like_submit, _run_agent_turns
from jobpilot.apply.dashboard import init_worker

RESULT = []


def check(name, cond):
    RESULT.append(bool(cond))
    print("[PASS] " + name if cond else "[FAIL] " + name)
    if not cond:
        raise SystemExit("TEST FAILED: " + name)


# ---------------------------------------------------------------------------
# 1. _looks_like_submit -- positive cases (must block in dry-run)
# ---------------------------------------------------------------------------
check("'Submit Application' blocks",
      _looks_like_submit("browser_click", {"element": "Submit Application", "ref": "e1"}))
check("'Apply Now' blocks",
      _looks_like_submit("browser_click", {"element": "Apply Now", "ref": "e2"}))
check("'Submit' blocks",
      _looks_like_submit("browser_click", {"element": "Submit", "ref": "e3"}))
check("'Send Application' blocks",
      _looks_like_submit("browser_click", {"element": "Send Application", "ref": "e4"}))
check("'Save and Submit' blocks (strong pattern beats nav veto)",
      _looks_like_submit("browser_click", {"element": "Save and Submit", "ref": "e5"}))
check("Enter key blocks",
      _looks_like_submit("browser_press_key", {"key": "Enter"}))
check("Return key blocks",
      _looks_like_submit("browser_press_key", {"key": "Return"}))

# ---------------------------------------------------------------------------
# 2. _looks_like_submit -- negative cases (must NOT block; real ATS forms
#    take 58-73 clicks across multiple pages, dry-run must be able to
#    exercise all of them)
# ---------------------------------------------------------------------------
check("'Next' does not block",
      not _looks_like_submit("browser_click", {"element": "Next", "ref": "e6"}))
check("'Continue to step 2' does not block",
      not _looks_like_submit("browser_click", {"element": "Continue to step 2", "ref": "e7"}))
check("'Save draft' does not block",
      not _looks_like_submit("browser_click", {"element": "Save draft", "ref": "e8"}))
check("'Search jobs' does not block",
      not _looks_like_submit("browser_click", {"element": "Search jobs", "ref": "e9"}))
check("'Apply filters' does not block (bare 'apply' is nav-vetoable)",
      not _looks_like_submit("browser_click", {"element": "Apply filters", "ref": "e10"}))
check("'Upload resume' does not block",
      not _looks_like_submit("browser_click", {"element": "Upload resume", "ref": "e11"}))
check("empty element does not block",
      not _looks_like_submit("browser_click", {"element": "", "ref": "e12"}))
check("browser_navigate does not block (wrong tool entirely)",
      not _looks_like_submit("browser_navigate", {"url": "https://example.com/apply"}))
check("browser_fill_form does not block",
      not _looks_like_submit("browser_fill_form", {"fields": []}))
check("Tab key does not block",
      not _looks_like_submit("browser_press_key", {"key": "Tab"}))
check("ArrowDown key does not block",
      not _looks_like_submit("browser_press_key", {"key": "ArrowDown"}))


# ---------------------------------------------------------------------------
# 3. _run_agent_turns -- the real MCP call must never fire when dry_run=True
#    and the model tries to click Submit.
# ---------------------------------------------------------------------------

class _FakeSession:
    """Stub MCP ClientSession. call_tool must never be awaited in this test."""

    def __init__(self):
        self.call_tool = AsyncMock(side_effect=AssertionError(
            "session.call_tool() was invoked -- dry-run enforcement failed "
            "to intercept a submit-like tool call before dispatch"
        ))


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


def _assistant_tool_call_response(name, arguments):
    return _FakeResponse({
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "function": {"name": name, "arguments": arguments},
                }],
            }
        }]
    })


def _assistant_final_response(text):
    return _FakeResponse({
        "choices": [{"message": {"role": "assistant", "content": text}}]
    })


async def _run_blocked_submit_case():
    init_worker(99)
    session = _FakeSession()
    messages = [{"role": "user", "content": "apply to this job"}]

    responses = [
        _assistant_tool_call_response(
            "browser_click", '{"element": "Submit Application", "ref": "e1"}'
        ),
        _assistant_final_response("Reviewed and ready.\nRESULT:APPLIED"),
    ]

    async def fake_post(*args, **kwargs):
        return responses.pop(0)

    with patch("httpx.AsyncClient.post", new=fake_post):
        status, transcript = await _run_agent_turns(
            session, openai_tools=[], messages=messages, worker_id=99,
            base_url="http://fake", model="fake-model", headers={},
            dry_run=True,
        )
    return status, messages, session


status, messages, session = asyncio.run(_run_blocked_submit_case())

check("real session.call_tool was never awaited",
      session.call_tool.await_count == 0)
check("loop concluded via RESULT:APPLIED, not MAX_TURNS exhaustion",
      status == "applied")
check("blocked tool message actually present in conversation history",
      any(
          isinstance(m, dict) and m.get("role") == "tool"
          and str(m.get("content", "")).startswith("BLOCKED (dry run)")
          for m in messages
      ))


async def _run_non_dry_run_case_unaffected():
    """Regression: dry_run=False must behave exactly as before -- the real
    tool call fires normally, nothing is blocked."""
    init_worker(97)
    session = _FakeSession()
    session.call_tool = AsyncMock(return_value=type(
        "Result", (), {"content": [], "is_error": False}
    )())
    messages = [{"role": "user", "content": "apply to this job"}]

    responses = [
        _assistant_tool_call_response(
            "browser_click", '{"element": "Submit Application", "ref": "e1"}'
        ),
        _assistant_final_response("Submitted.\nRESULT:APPLIED"),
    ]

    async def fake_post(*args, **kwargs):
        return responses.pop(0)

    with patch("httpx.AsyncClient.post", new=fake_post):
        status, transcript = await _run_agent_turns(
            session, openai_tools=[], messages=messages, worker_id=97,
            base_url="http://fake", model="fake-model", headers={},
            dry_run=False,
        )
    return status, session


status3, session3 = asyncio.run(_run_non_dry_run_case_unaffected())
check("dry_run=False: real submit click IS dispatched (no behavior change)",
      session3.call_tool.await_count == 1)
check("dry_run=False: still concludes RESULT:APPLIED normally",
      status3 == "applied")


if __name__ == "__main__":
    passed = sum(1 for x in RESULT if x)
    print("\n%d/%d checks passed" % (passed, len(RESULT)))
    raise SystemExit(0 if passed == len(RESULT) else 1)
