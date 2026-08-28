"""Local-LLM auto-apply engine.

An alternative to launcher.run_job() that drives the same Playwright MCP
server (@playwright/mcp) directly via tool-calling, using whichever LLM
provider is configured in .env (GEMINI_API_KEY / OPENAI_API_KEY / LLM_URL)
instead of spawning a real `claude` CLI session.

Exists so auto-apply can run without spending Claude Code API usage per
job -- point LLM_URL at a local llama.cpp/Ollama server and every
application is driven by that model instead.

Requires the configured endpoint to support OpenAI-style function calling
(`tools` / `tool_calls` in the chat completions payload). For llama-server,
start it with --jinja so the chat template emits tool calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from jobpilot import config
from jobpilot.apply import prompt as prompt_mod
from jobpilot.apply.result import extract_result as _extract_result
from jobpilot.apply.dashboard import add_event, get_state, update_state
from jobpilot.llm import resolve_apply_provider

logger = logging.getLogger(__name__)


def _find_node_dir() -> str | None:
    """Locate node.exe's directory: PATH first, then common install locations.

    npx needs its own directory AND node.exe's directory on PATH to run.
    Bare 'npx'/'cmd' resolution via shutil.which() can silently pick up a
    non-Windows shim on a dev box with msys64/git-bash also on PATH
    (observed: shutil.which('cmd') resolving to a msys shell script instead
    of System32\\cmd.exe -> WinError 193 "not a valid Win32 application").
    Explicit candidate search avoids depending on ambient PATH ordering.
    """
    node_exe = shutil.which("node") or shutil.which("node.exe")
    if node_exe:
        return str(Path(node_exe).parent)
    for candidate in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs",
        Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "node",
        Path(os.environ.get("APPDATA", "")) / "npm",
    ):
        if (candidate / "node.exe").exists() or (candidate / "npx.CMD").exists():
            return str(candidate)
    return None


def _npx_env() -> dict:
    """Environment for the Playwright MCP subprocess, with node's directory
    guaranteed to be first on PATH regardless of the caller's PATH state."""
    env = os.environ.copy()
    node_dir = _find_node_dir()
    if node_dir:
        env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
        os.environ["PATH"] = env["PATH"]  # also fixes this process's own npx resolution
    return env


# A real ATS form (Eightfold, Workday, Oracle) takes more than 40 turns to
# fill honestly: observed runs spent 20+ clicks and 4 fill_form calls and were
# still making progress on the last field when the budget ran out. Turns are
# cheap now that apply runs on a flash-class cloud model rather than the
# Claude Code CLI, so the old ceiling costs completed applications for nothing.
MAX_TURNS = int(os.environ.get("APPLY_MAX_TURNS", "90"))

# If the agent repeats the same tool call with the same arguments this many
# times in a row, it is looping rather than progressing -- one observed run
# burned 38 of its 40 turns on identical browser_click calls with a single
# snapshot, never re-reading the page to find out why the click failed.
_REPEAT_LIMIT = 3
# Bounded termination for dry-run enforcement: after this many blocked
# submit-like attempts, stop trusting the model to wrap up on its own and
# force a clean dry-run completion. Without this, a model that ignores the
# "conclude now" instruction fed back on a block could burn most of the
# MAX_TURNS budget retrying the same blocked action before the existing
# max-turns fallback eventually catches it.
_DRY_RUN_BLOCK_LIMIT = int(os.environ.get("APPLY_DRY_RUN_BLOCK_LIMIT", "3"))
# P1 bring-up escalation: Chrome CDP readiness + MCP spawn retries. The 60-failure
# "unhandled errors in a TaskGroup (Connection closed)" class was the MCP stdio
# server dying at startup -- because Chrome's CDP port wasn't listening yet and/or
# the stdio connection dropped on the first try. We now (a) wait for CDP before
# spawning the server, and (b) re-spawn the whole MCP server with backoff.
CDP_WAIT_TIMEOUT = 20.0   # seconds to wait for http://localhost:<port>/json/version
CDP_WAIT_INTERVAL = 0.5
MAX_MCP_ATTEMPTS = 3
_MCP_BACKOFF_BASE = 2.0
# How much of each tool result goes back into the conversation. 3000 was sized
# for a 9B model on a local llama.cpp server; a browser_snapshot of a real ATS
# form is far larger than that, so the agent saw only the top of the page and
# burned its whole turn budget re-snapshotting without ever reaching the form
# fields. Apply now runs on a large-context cloud model (see APPLY_LLM_*), so
# the old ceiling is pure loss. Override with APPLY_TOOL_RESULT_CHARS when
# pointing apply back at a small local model.
_TOOL_RESULT_CHARS = int(os.environ.get("APPLY_TOOL_RESULT_CHARS", "30000"))

_SYSTEM_PREAMBLE = (
    "You are an autonomous job-application agent. Use the provided browser "
    "tools to navigate the page, read it, fill in fields, upload the resume "
    "and cover letter, and submit the application. Take one tool call at a "
    "time and look at its result before deciding the next step. "
    "If a tool call fails, do NOT immediately retry it with the same or "
    "slightly different arguments -- call browser_snapshot first and re-read "
    "the page, because the element reference you are using is probably stale "
    "or wrong. Never repeat an identical failing call more than twice. "
    "When -- and only when -- the task is fully finished (or you have "
    "determined it cannot be completed), reply with plain text (no tool "
    "call) whose LAST line is exactly one of:\n"
    "RESULT:APPLIED\nRESULT:EXPIRED\nRESULT:CAPTCHA\nRESULT:LOGIN_ISSUE\n"
    "RESULT:FAILED:<short_reason>\n"
    "Never output RESULT:APPLIED unless the form was actually submitted "
    "(or, in dry-run mode, was fully filled and ready to submit)."
)



# Full Playwright MCP tool set is ~24 tools; llama.cpp compiles a grammar
# covering every tool's parameter schema up front, and observed behavior
# with all 24 attached was either a fast 400 or the request never completing
# within a generous timeout. Trimmed to what's actually needed to read and
# fill out a job application form -- cuts grammar size and complexity, and
# drops browser_run_code_unsafe unconditionally regardless of size: it's
# explicitly unsandboxed arbitrary JS execution, not appropriate to hand to
# an autonomous agent submitting real applications.
_ALLOWED_TOOLS = {
    "browser_navigate", "browser_navigate_back", "browser_snapshot",
    "browser_click", "browser_type", "browser_fill_form",
    "browser_select_option", "browser_file_upload", "browser_press_key",
    "browser_wait_for", "browser_find", "browser_tabs", "browser_close",
}


# --- Dry-run enforcement -----------------------------------------------
#
# prompt.py's dry-run mode is a sentence in the prompt ("do NOT click
# Submit"). That is not a technical guarantee -- nothing stopped the actual
# click from reaching the browser if the model ignored it, and the rest of
# the same prompt tells the agent to "Act decisively... Submit the
# application" across up to MAX_TURNS turns. This is the real backstop:
# when dry_run is on, a tool call that looks like the final submit action
# is intercepted in _run_agent_turns BEFORE session.call_tool() is ever
# invoked, so the real MCP call -- and therefore the real browser click --
# never fires, regardless of what the model decides to do.
#
# This only covers engine=local (this file's own tool-calling loop).
# engine=claude (apply/launcher.py) spawns the Claude Code CLI as a
# subprocess that owns its own internal tool loop and is not interceptable
# this way -- dry-run there remains prompt-only. Per CLAUDE.md, engine=claude
# is already discouraged (275 weekly-limit failures, 141 Playwright-load
# failures logged) and every real scheduled run (scripts/agent_loop.ps1, via
# --fast) auto-forces engine=local regardless of the configured engine flag,
# so this covers the path that actually matters in practice.

# Keys that can submit a focused form. Playwright MCP's browser_press_key
# schema gives no element context, so this is intentionally coarse: block
# every Enter/Return press in dry-run mode rather than trying to guess
# what's focused.
_SUBMIT_KEYS = {"Enter", "Return"}

# Strong signal: these phrases essentially never appear on a mid-wizard
# progression control, only on the terminal action.
_STRONG_SUBMIT_RE = re.compile(
    r"\bsubmit\b|\bsend\s+application\b|\bfinish\s+application\b|"
    r"\bcomplete\s+application\b",
    re.IGNORECASE,
)
# Navigational / non-terminal controls in a multi-step wizard. Checked
# before the weak pattern below so "Next" / "Continue to step 2" / "Save
# draft" don't block real ATS forms -- these are documented to take 58-73
# distinct clicks across multiple pages before reaching a real submit.
_NAV_RE = re.compile(
    r"\b(next|continue|back|previous|save\s+draft|search|filters?|browse|"
    r"add\s+(?:another|attachment)|step\s*\d+|page\s*\d+)\b",
    re.IGNORECASE,
)
# Weak signal: bare "apply" is ambiguous elsewhere in job-board UIs (e.g.
# "Apply filters"), so it only blocks when the nav pattern above didn't
# already clear it.
_WEAK_SUBMIT_RE = re.compile(r"\bapply(?:\s*now)?\b", re.IGNORECASE)


def _looks_like_submit(tool_name: str, args: dict) -> bool:
    """Heuristic: would this tool call actually submit the application?

    Deliberately conservative and best-effort -- a defense-in-depth
    backstop on top of (not a replacement for) the prompt-level dry-run
    instruction in prompt.py. It can both under-block (a final action
    worded as bare "Confirm"/"Done" won't be caught) and over-block (a
    genuinely mid-wizard "Submit references" step would be), so it is not
    a proof of safety, only a substantial technical narrowing of the gap.
    """
    if tool_name == "browser_press_key":
        return str(args.get("key", "")) in _SUBMIT_KEYS

    if tool_name != "browser_click":
        return False

    element = str(args.get("element", "") or "")
    if not element:
        return False
    if _STRONG_SUBMIT_RE.search(element):
        return True
    if _NAV_RE.search(element):
        return False
    return bool(_WEAK_SUBMIT_RE.search(element))


def _mcp_tools_to_openai(tools) -> list[dict]:
    result = []
    for t in tools:
        if t.name not in _ALLOWED_TOOLS:
            continue
        params = dict(t.input_schema) if t.input_schema else {"type": "object", "properties": {}}
        params.pop("$schema", None)  # suspected incompatible with llama.cpp's grammar converter
        result.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": params,
            },
        })
    return result


def _mcp_command() -> str:
    """Resolved npx executable for the Playwright MCP server subprocess.

    On Windows, npx is actually npx.cmd; handing bare "npx" to the MCP
    stdio transport (create_subprocess_exec, which cannot run .cmd/.bat on
    Windows without a shell) silently fails to start the server -> the
    session reports "Connection closed" at initialize(). Resolving the real
    path via find_npx() (which prefers the fully-qualified npx.cmd) removes
    that class of failure.
    """
    resolved = config.find_npx()
    if resolved:
        return resolved
    return "npx.cmd" if os.name == "nt" else "npx"


async def _wait_cdp(port: int, timeout: float = CDP_WAIT_TIMEOUT,
                    interval: float = CDP_WAIT_INTERVAL) -> bool:
    """Poll Chrome's CDP /json/version endpoint until it answers.

    Chrome opens the remote-debugging port asynchronously; if the Playwright
    MCP server connects before it is listening, the CDP connection dies and
    the server exits -> "Connection closed" at session.initialize(). Waiting
    here first removes that race. Usually returns on the first poll (Chrome
    was already started by the launcher); only blocks when Chrome is actually
    slow or failed to launch.
    """
    url = f"http://localhost:{port}/json/version"
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=2) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(interval)
    return False


async def _run_agent_turns(session, openai_tools: list[dict], messages: list[dict],
                           worker_id: int, base_url: str, model: str,
                           headers: dict, dry_run: bool = False) -> tuple[str, str]:
    """Drive one job application to completion via LLM->tool-calling turns.

    Shared mutable `messages` keeps full history, so a caller that re-spawns
    the MCP server on a transport drop can resume from where the agent left
    off instead of restarting the application.

    Returns (status, transcript). Transport/browser exceptions propagate so
    the retry ladder in _drive_agent can re-spawn the MCP server.
    """
    # Loop detection across turns: (tool name, arguments) of the last call.
    last_sig: str | None = None
    repeats = 0
    # Count of submit-like tool calls blocked by dry-run enforcement (see
    # _looks_like_submit above).
    dry_run_blocks = 0

    async with httpx.AsyncClient(timeout=1200) as http:
        for turn in range(MAX_TURNS):
            payload = {
                "model": model,
                "messages": messages,
                "tools": openai_tools,
                "tool_choice": "auto",
                "temperature": 0.2,
                "max_tokens": 2048,
            }
            # Windows-only: transient ConnectError observed right after
            # spawning the npx/Chrome subprocess under load (rapid
            # worker restarts) -- one retry clears it.
            for attempt in range(3):
                try:
                    resp = await http.post(
                        f"{base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                    break
                except httpx.ConnectError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(2 * (attempt + 1))
            if resp.status_code >= 400:
                raise RuntimeError(f"LLM endpoint {resp.status_code}: {resp.text[:2000]}")
            msg = resp.json()["choices"][0]["message"]
            messages.append(msg)

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                text = msg.get("content") or ""
                status = _extract_result(text)
                if status:
                    return status, text
                update_state(worker_id, last_action=f"turn {turn}: no result yet")
                messages.append({
                    "role": "user",
                    "content": "Continue the task with a tool call, or if finished reply with RESULT:...",
                })
                continue

            for tc in tool_calls:
                name = tc["function"]["name"]
                raw_args = tc["function"].get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    args = {}

                if dry_run and _looks_like_submit(name, args):
                    # Technical enforcement: the real MCP call is never made,
                    # so the real browser click never fires, regardless of
                    # what the model does next. Not counted as a repeat --
                    # an intentional, expected block is not the model looping.
                    detail = args.get("element") or args.get("key") or ""
                    add_event(
                        f"[W{worker_id}] dry-run: blocked submit-like action "
                        f"({name}: {detail!r})"
                    )
                    update_state(worker_id, last_action=f"dry-run blocked: {name}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", name),
                        "content": (
                            "BLOCKED (dry run): submit action intercepted and not "
                            "sent to the browser. This is expected in dry-run mode "
                            "- the form has been reviewed and is ready. Conclude "
                            "now with RESULT:APPLIED and a note that this was a "
                            "dry run."
                        ),
                    })
                    dry_run_blocks += 1
                    if dry_run_blocks >= _DRY_RUN_BLOCK_LIMIT:
                        add_event(
                            f"[W{worker_id}] dry-run: block limit reached, "
                            "forcing completion"
                        )
                        return "dry_run:applied", (
                            "Dry run: submit blocked repeatedly; treating as "
                            "completed."
                        )
                    continue

                sig = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
                repeats = repeats + 1 if sig == last_sig else 1
                last_sig = sig

                try:
                    result = await session.call_tool(name, args)
                    parts = [
                        getattr(c, "text", "") for c in getattr(result, "content", [])
                    ]
                    content = "\n".join(p for p in parts if p) or "(no output)"
                    if getattr(result, "is_error", False):
                        content = f"ERROR: {content}"
                except Exception as e:  # noqa: BLE001 -- feed the error back to the model
                    content = f"ERROR: {e}"

                ws = get_state(worker_id)
                cur_actions = ws.actions if ws else 0
                update_state(worker_id, actions=cur_actions + 1,
                             last_action=f"{name}"[:35])
                add_event(f"[W{worker_id}] {name}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", name),
                    "content": content[:_TOOL_RESULT_CHARS],
                })

                if repeats >= _REPEAT_LIMIT:
                    # Break the spiral explicitly. Left alone the model will
                    # keep tweaking one argument and re-firing the same call
                    # until the turn budget is gone.
                    logger.info("[worker %s] breaking repeat loop on %s (x%d)",
                                worker_id, name, repeats)
                    messages.append({
                        "role": "user",
                        "content": (
                            f"You have called {name} with the same arguments "
                            f"{repeats} times and it is not working. Stop "
                            "retrying it. Call browser_snapshot now, re-read "
                            "the page, and choose a different element or a "
                            "different approach. If the page genuinely cannot "
                            "be progressed, finish with a RESULT: line."
                        ),
                    })
                    repeats = 0
                    last_sig = None

    # Returning an empty transcript here made the single most common failure
    # completely undiagnosable -- the worker log got a header and nothing else.
    # Dump what the agent actually did so the turn budget can be reasoned about.
    tool_names = [
        c["function"]["name"]
        for m in messages
        for c in (m.get("tool_calls") or [])
        if isinstance(m, dict)
    ]
    last_text = ""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("content"):
            last_text = str(m["content"])[:800]
            break
    counts: dict[str, int] = {}
    for n in tool_names:
        counts[n] = counts.get(n, 0) + 1
    summary = (
        f"MAX_TURNS ({MAX_TURNS}) exhausted after {len(tool_names)} tool call(s).\n"
        f"Tool usage: {counts or '(none)'}\n"
        f"Call order: {' -> '.join(tool_names[-25:]) or '(none)'}\n"
        f"Last assistant message:\n{last_text or '(none)'}"
    )
    logger.warning("[worker %s] %s", worker_id, summary.replace("\n", " | "))
    return "failed:local_agent_max_turns", summary


async def _drive_agent(job: dict, port: int, worker_id: int, dry_run: bool) -> tuple[str, str]:
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    task_prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text, dry_run=dry_run)

    base_url, model, api_key = resolve_apply_provider()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    server_params = StdioServerParameters(
        command=_mcp_command(),
        args=[
            "@playwright/mcp@latest",
            f"--cdp-endpoint=http://localhost:{port}",
            f"--viewport-size={config.DEFAULTS['viewport']}",
        ],
        env=_npx_env(),
    )

    # P1 ladder (1/3): Chrome's CDP endpoint must be listening before the MCP
    # server connects, otherwise the server dies at startup with a "Connection
    # closed" and the agent never gets browser tools (the dominant ~60-failure
    # class). Cheap when Chrome is already up; only blocks when it is slow.
    if not await _wait_cdp(port, timeout=CDP_WAIT_TIMEOUT):
        reason = "browser_unavailable:cdp_timeout"
        add_event(f"[W{worker_id}] {reason} (port {port})")
        update_state(worker_id, status="failed", last_action=reason)
        return reason, (
            f"Chrome DevTools endpoint http://localhost:{port}/json/version never "
            f"answered within {CDP_WAIT_TIMEOUT}s. Chrome likely failed to launch "
            f"or is wedged; the job stays retryable on the next acquire pass."
        )

    messages: list[dict] = [
        {"role": "system", "content": _SYSTEM_PREAMBLE},
        {"role": "user", "content": task_prompt},
    ]

    # P1 ladder (2/3 + 3/3): re-spawn the whole MCP server (stdio transport +
    # CDP session) with backoff. `messages` history persists across attempts, so
    # a transport drop mid-job resumes from where the agent left off instead of
    # failing with a raw TaskGroup traceback; a final clean reason replaces it.
    last_err: Exception | None = None
    for attempt in range(1, MAX_MCP_ATTEMPTS + 1):
        try:
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    openai_tools = _mcp_tools_to_openai(listed.tools)
                    if not openai_tools:
                        raise RuntimeError("no allowed Playwright tools listed by MCP server")
                    return await _run_agent_turns(
                        session, openai_tools, messages, worker_id,
                        base_url, model, headers, dry_run=dry_run,
                    )
        except Exception as exc:  # noqa: BLE001 -- re-spawn and retry the server
            last_err = exc
            logger.warning("[W%d] MCP bring-up attempt %d/%d failed: %s",
                           worker_id, attempt, MAX_MCP_ATTEMPTS, exc)
            if attempt < MAX_MCP_ATTEMPTS:
                await asyncio.sleep(_MCP_BACKOFF_BASE * attempt)

    reason = "browser_unavailable:mcp_init_failed"
    add_event(f"[W{worker_id}] {reason}")
    update_state(worker_id, status="failed", last_action=reason)
    return reason, (
        f"Playwright MCP server could not initialize after {MAX_MCP_ATTEMPTS} "
        f"attempts. Last error: {last_err}"
    )


def run_job_local(job: dict, port: int, worker_id: int = 0, dry_run: bool = False) -> tuple[str, int]:
    """Local-LLM equivalent of launcher.run_job(). Same return contract."""
    start = time.time()
    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=start, actions=0, last_action="starting (local agent)")
    add_event(f"[W{worker_id}] Starting (local): {job['title'][:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = time.strftime("%Y-%m-%d %H:%M:%S")
    header = (
        f"\n{'=' * 60}\n[{ts_header}] (local agent) {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\nScore: {job.get('fit_score', 'N/A')}/10\n{'=' * 60}\n"
    )
    with open(worker_log, "a", encoding="utf-8") as lf:
        lf.write(header)

        try:
            status, transcript = asyncio.run(_drive_agent(job, port, worker_id, dry_run))
            lf.write(transcript + "\n")
        except Exception as e:  # noqa: BLE001
            import traceback
            duration_ms = int((time.time() - start) * 1000)
            lf.write(f"ERROR: {e}\n{traceback.format_exc()}\n")
            add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
            update_state(worker_id, status="failed", last_action=f"ERROR: {str(e)[:25]}")
            return f"failed:{str(e)[:100]}", duration_ms

    duration_ms = int((time.time() - start) * 1000)
    elapsed = int(time.time() - start)
    add_event(f"[W{worker_id}] {status.upper()} ({elapsed}s): {job['title'][:30]}")
    update_state(worker_id, status=status.split(":")[0], last_action=f"{status} ({elapsed}s)")
    return status, duration_ms
