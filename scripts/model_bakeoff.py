"""Compare OpenRouter models on the exact failure mode auto-apply hits.

Not a general benchmark. It replays one scenario, shaped like a real
application run: read the page, click a button whose tool schema requires TWO
parameters, recover when the first attempt is rejected, fill a form, finish.

That middle step is the whole point. The observed production failure was 38
consecutive browser_click calls that omitted the required `element` parameter,
with the model narrating "I will try to click ... using its `ref` directly,
without the `element` parameter". A model that cannot self-correct there will
burn its entire turn budget on any real ATS form, no matter how cheap it is.

Usage:
    .venv\\Scripts\\python.exe scripts\\model_bakeoff.py
    .venv\\Scripts\\python.exe scripts\\model_bakeoff.py --models a,b,c
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jobpilot.config import load_env  # noqa: E402

DEFAULT_MODELS = [
    "deepseek/deepseek-v4-flash-0731",
    "google/gemini-3.5-flash-lite",
    "google/gemini-3.6-flash",
    "z-ai/glm-5.3",
]

# Mirrors Playwright MCP: browser_click needs BOTH element and ref.
TOOLS = [
    {"type": "function", "function": {
        "name": "browser_snapshot",
        "description": "Capture an accessibility snapshot of the current page.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "browser_click",
        "description": "Click an element on the page.",
        "parameters": {
            "type": "object",
            "properties": {
                "element": {"type": "string", "description": "Human-readable element description."},
                "ref": {"type": "string", "description": "Exact ref from the page snapshot."},
            },
            "required": ["element", "ref"],
        },
    }},
    {"type": "function", "function": {
        "name": "browser_fill_form",
        "description": "Fill multiple form fields.",
        "parameters": {
            "type": "object",
            "properties": {
                "fields": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "ref": {"type": "string"},
                        "type": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["name", "ref", "type", "value"],
                }},
            },
            "required": ["fields"],
        },
    }},
]

SNAPSHOT = """- generic [ref=e1]:
  - heading "Test Development Engineer - Mechanical" [ref=e2]
  - link "Apply Now" [ref=e42]
- form [ref=e50]:
  - textbox "Full name" [ref=e51]
  - textbox "Email" [ref=e52]
  - combobox "Source" [ref=e53]
  - button "Submit application" [ref=e60]
"""

SYSTEM = (
    "You are an autonomous job-application agent. Use the browser tools to read "
    "the page, click through to the form, fill it in, and submit. One tool call "
    "at a time; look at each result before the next step. If a call fails, do "
    "NOT retry it unchanged -- re-read the page and fix your arguments. "
    "When finished reply with plain text whose LAST line is exactly RESULT:APPLIED."
)

MAX_TURNS = 14


def simulate(name: str, args: dict, state: dict) -> tuple[str, bool]:
    """Fake browser. Returns (tool_output, was_malformed)."""
    if name == "browser_snapshot":
        return SNAPSHOT, False

    if name == "browser_click":
        if "element" not in args or "ref" not in args:
            return ("ERROR: browser_click requires BOTH 'element' (a human-readable "
                    "description) and 'ref' (from the snapshot). You supplied: "
                    f"{sorted(args)}"), True
        if args.get("ref") == "e42" and not state.get("clicked_apply"):
            state["clicked_apply"] = True
            return "Clicked 'Apply Now'. The application form is now visible.", False
        if args.get("ref") == "e60":
            if state.get("filled"):
                state["submitted"] = True
                return "Application submitted successfully.", False
            return "ERROR: form is incomplete; fill the fields first.", False
        return f"Clicked {args.get('element')}.", False

    if name == "browser_fill_form":
        fields = args.get("fields")
        if not isinstance(fields, list) or not fields:
            return "ERROR: browser_fill_form requires a non-empty 'fields' array.", True
        state["filled"] = True
        return f"Filled {len(fields)} field(s).", False

    return f"ERROR: unknown tool {name}", True


def run(model: str, key: str) -> dict:
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "Apply to this job. Start by reading the page."}]
    state: dict = {}
    malformed = recovered = turns = 0
    in_tok = out_tok = 0
    seen_malformed = False
    t0 = time.time()

    with httpx.Client(timeout=180) as http:
        for turns in range(1, MAX_TURNS + 1):
            r = http.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": msgs, "tools": TOOLS,
                      "tool_choice": "auto", "temperature": 0.2, "max_tokens": 1024},
            )
            if r.status_code >= 400:
                return {"model": model, "error": f"HTTP {r.status_code}: {r.text[:160]}"}
            body = r.json()
            usage = body.get("usage") or {}
            in_tok += usage.get("prompt_tokens", 0)
            out_tok += usage.get("completion_tokens", 0)
            msg = body["choices"][0]["message"]
            msgs.append(msg)

            calls = msg.get("tool_calls") or []
            if not calls:
                if "RESULT:APPLIED" in (msg.get("content") or ""):
                    break
                msgs.append({"role": "user", "content": "Continue with a tool call."})
                continue

            for tc in calls:
                fname = tc["function"]["name"]
                try:
                    fargs = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    fargs = {}
                out, bad = simulate(fname, fargs, state)
                if bad:
                    malformed += 1
                    seen_malformed = True
                elif seen_malformed and fname == "browser_click":
                    recovered += 1
                    seen_malformed = False
                msgs.append({"role": "tool", "tool_call_id": tc.get("id", fname),
                             "content": out})
            if state.get("submitted"):
                break

    return {"model": model, "done": bool(state.get("submitted")), "turns": turns,
            "malformed": malformed, "recovered": recovered,
            "in": in_tok, "out": out_tok, "secs": round(time.time() - t0, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", help="comma-separated model ids")
    args = ap.parse_args()

    load_env()
    key = os.environ.get("APPLY_LLM_API_KEY") or os.environ.get("TAILOR_LLM_API_KEY")
    if not key:
        print("No OpenRouter key found (APPLY_LLM_API_KEY / TAILOR_LLM_API_KEY).")
        raise SystemExit(1)

    models = args.models.split(",") if args.models else DEFAULT_MODELS
    print(f"{'model':<34} {'done':>5} {'turns':>6} {'malformed':>10} {'recovered':>10} {'in':>8} {'out':>7} {'secs':>6}")
    print("-" * 92)
    for m in models:
        try:
            r = run(m.strip(), key)
        except Exception as e:  # noqa: BLE001
            print(f"{m:<34} EXCEPTION {type(e).__name__}: {e}")
            continue
        if r.get("error"):
            print(f"{m:<34} {r['error']}")
            continue
        print(f"{r['model']:<34} {'YES' if r['done'] else 'no':>5} {r['turns']:>6} "
              f"{r['malformed']:>10} {r['recovered']:>10} {r['in']:>8} {r['out']:>7} {r['secs']:>6}")
    print()
    print("done=YES means it reached submit. malformed = calls missing required params.")
    print("recovered = times it fixed itself after a rejection. Lower malformed + ")
    print("higher recovered is what survives a real 90-turn ATS form.")


if __name__ == "__main__":
    main()
