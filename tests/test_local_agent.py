"""Tests for apply/local_agent.py: dry-run submit-detection heuristic,
technical dry-run enforcement, safe stdio stderr handling, and Windows MCP bring-up.

Run:  cd <repo> && python3 tests/test_local_agent.py

Requires the same third-party deps as the real apply engine (httpx, mcp,
rich, python-dotenv, pyyaml -- see pyproject.toml); these are only needed
because importing jobpilot.apply.local_agent pulls in jobpilot.apply.dashboard
and jobpilot.llm at module load time, not because this file talks to a real
LLM, browser, or remote MCP server -- everything below is mocked / in-process.
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from jobpilot.apply.dashboard import init_worker
from jobpilot.apply.local_agent import (
    _looks_like_submit,
    _mcp_tools_to_openai,
    _npx_env,
    _run_agent_turns,
    _run_async,
    _safe_errlog,
)

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
        status, _transcript = await _run_agent_turns(
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
        status, _transcript = await _run_agent_turns(
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


# ---------------------------------------------------------------------------
# 4. _safe_errlog & _run_async unit tests
# ---------------------------------------------------------------------------
check("_safe_errlog handles normal stderr without crash",
      _safe_errlog() is not None or sys.stderr is None)

_orig_stderr = sys.stderr
try:
    sys.stderr = io.StringIO("captured logs")
    check("_safe_errlog returns None or fileno-capable stream when sys.stderr is StringIO",
          _safe_errlog() is None or hasattr(_safe_errlog(), "fileno"))
finally:
    sys.stderr = _orig_stderr


async def _dummy_coro():
    await asyncio.sleep(0.01)
    return 42


check("_run_async runs coroutine and returns result",
      _run_async(_dummy_coro()) == 42)


# ---------------------------------------------------------------------------
# 5. _npx_env thread-safety & consistency
# ---------------------------------------------------------------------------
def _query_env():
    return _npx_env()


with ThreadPoolExecutor(max_workers=4) as ex:
    env_results = list(ex.map(lambda _: _query_env(), range(8)))

check("concurrent _npx_env calls return identical dicts",
      all(e == env_results[0] for e in env_results))
check("_npx_env contains PATH",
      "PATH" in env_results[0])


# ---------------------------------------------------------------------------
# 6. Mock MCP bring-up & tool listing with _safe_errlog & _run_async
# ---------------------------------------------------------------------------
MOCK_MCP_SERVER_CODE = r"""
import sys
import json

while True:
    line = sys.stdin.readline()
    if not line:
        break
    try:
        req = json.loads(line)
        req_id = req.get("id")
        method = req.get("method")
        if method == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mock-mcp", "version": "1.0.0"}
                }
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "browser_click",
                            "description": "Click an element",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"element": {"type": "string"}, "ref": {"type": "string"}}
                            }
                        },
                        {
                            "name": "browser_snapshot",
                            "description": "Capture a snapshot",
                            "inputSchema": {"type": "object", "properties": {}}
                        }
                    ]
                }
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Executed {name} successfully"}],
                    "isError": False
                }
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.stderr.flush()
"""

with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
    tf.write(MOCK_MCP_SERVER_CODE)
    mock_server_script = tf.name

try:
    async def _test_mock_mcp_bringup():
        params = StdioServerParameters(
            command=sys.executable,
            args=[mock_server_script],
            env=_npx_env(),
        )
        safe_err = _safe_errlog()
        async with (
            stdio_client(params, errlog=safe_err) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            openai_tools = _mcp_tools_to_openai(listed.tools)
            return [t["function"]["name"] for t in openai_tools]

    tools_found = _run_async(_test_mock_mcp_bringup())
    check("mock MCP brings up and lists filtered allowed tools",
          "browser_click" in tools_found and "browser_snapshot" in tools_found)

    # ---------------------------------------------------------------------------
    # 7. Concurrent MCP bring-up across parallel worker threads on Windows
    # ---------------------------------------------------------------------------
    def _worker_thread_mcp(worker_id):
        _orig = sys.stderr
        try:
            sys.stderr = io.StringIO()
            return _run_async(_test_mock_mcp_bringup())
        finally:
            sys.stderr = _orig

    with ThreadPoolExecutor(max_workers=3) as ex:
        parallel_results = list(ex.map(_worker_thread_mcp, range(3)))

    check("parallel worker threads bring up MCP without fileno crashes",
          len(parallel_results) == 3 and all("browser_click" in r for r in parallel_results))

finally:
    if os.path.exists(mock_server_script):
        try:
            os.unlink(mock_server_script)
        except OSError:
            pass


if __name__ == "__main__":
    passed = sum(1 for x in RESULT if x)
    print("\n%d/%d checks passed" % (passed, len(RESULT)))
    raise SystemExit(0 if passed == len(RESULT) else 1)
