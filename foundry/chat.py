"""
Greymatter Foundry — AI warehouse assistant.

Supports both Anthropic (Claude) and OpenAI (GPT) APIs.
Set exactly one of:
    ANTHROPIC_API_KEY=sk-ant-...   → uses Claude
    OPENAI_API_KEY=sk-...          → uses GPT

Optional overrides:
    FOUNDRY_AI_MODEL               → override the default model
    FOUNDRY_AI_PROVIDER            → force "anthropic" or "openai"

Usage (from web.py)
-------------------
    from foundry.chat import handle_message
    result = handle_message(state, user_message, history)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foundry.web import SimState

# ── Provider detection ────────────────────────────────────────────────────────

def _detect_provider() -> str:
    """Return 'anthropic' or 'openai' based on env vars."""
    forced = os.environ.get("FOUNDRY_AI_PROVIDER", "").lower()
    if forced in ("anthropic", "openai"):
        return forced
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "anthropic"   # default (will fail gracefully with helpful message)

_PROVIDER = _detect_provider()

_DEFAULT_MODELS = {
    "anthropic": "claude-3-5-haiku-20241022",
    "openai":    "gpt-4o-mini",
}
_MODEL = os.environ.get("FOUNDRY_AI_MODEL", _DEFAULT_MODELS[_PROVIDER])

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_OPENAI_URL    = "https://api.openai.com/v1/chat/completions"
_ANT_VERSION   = "2023-06-01"

# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM = """\
You are an expert AI assistant for the Greymatter Foundry warehouse simulator.
Your job is to help users understand and improve AMR (autonomous mobile robot) throughput.

Capabilities:
  • Explain current KPIs and identify bottlenecks
  • Resize, add, or remove rack zones
  • Adjust simulation parameters (agents, demand rate, SKU count)
  • Run before/after forecasts to quantify the impact of changes

Warehouse coordinate system (row, col) with (0, 0) at top-left:
  Rows increase DOWNWARD. Cols increase RIGHTWARD.
  row_start/row_end and col_start/col_end are INCLUSIVE.

Default layout facts:
  • Grid: 30 rows × 50 cols
  • Racks numbered 1-12 (odd = upper half rows 2-13, even = lower half rows 15-27)
  • Six rack columns A–F, each split into upper/lower by a cross-aisle at row 14
  • Three docks at row 29 (cols 9, 24, 40) — agents drop off orders here
  • Open front corridor at row 28 connects all aisles
  • Charging stations at (0,0) and (0,49)

When a user says "extend rack N by X rows/blocks downward" → increase row_end by X.
When a user says "extend rack N upward" → decrease row_start by X.
When a user says "add X agents" → new total = current + X.
Always call get_simulation_state first to read actual coordinates before modifying.
After a change, briefly confirm what changed and run a short forecast if useful.
"""

# ── Tool definitions ──────────────────────────────────────────────────────────

# Canonical tool specs (provider-agnostic input_schema / parameters)
_TOOL_SPECS = [
    {
        "name": "get_simulation_state",
        "description": (
            "Return the current simulation's layout zones (with names and coordinates), "
            "live KPIs, agent states, grid dimensions, and config. "
            "Always call this before making any changes so you work with accurate data."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "apply_changes",
        "description": (
            "Apply one or more mutations to the warehouse layout or simulation parameters. "
            "The simulation rebuilds immediately after. "
            "Rack zone row/col bounds are INCLUSIVE. "
            "Row 0 is top-left; rows increase downward, cols increase rightward."
        ),
        "parameters": {
            "type": "object",
            "required": ["changes"],
            "properties": {
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["op"],
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": [
                                    "modify_rack",
                                    "add_rack",
                                    "remove_rack",
                                    "set_agents",
                                    "set_demand_rate",
                                    "set_skus",
                                ],
                                "description": "Mutation operation.",
                            },
                            "name":      {"type": "string",  "description": "Zone name (rack ops)"},
                            "row_start": {"type": "integer", "description": "New inclusive row_start"},
                            "row_end":   {"type": "integer", "description": "New inclusive row_end"},
                            "col_start": {"type": "integer", "description": "New inclusive col_start"},
                            "col_end":   {"type": "integer", "description": "New inclusive col_end"},
                            "n":         {"type": "integer", "description": "Count (set_agents / set_skus)"},
                            "rate":      {"type": "number",  "description": "Orders/hr (set_demand_rate)"},
                        },
                    },
                }
            },
        },
    },
    {
        "name": "run_forecast",
        "description": (
            "Run a quick headless simulation to preview throughput. "
            "Does NOT affect the live running simulation. "
            "Optionally supply 'changes' to test hypothetical mutations. "
            "Returns KPI metrics for a before/after comparison."
        ),
        "parameters": {
            "type": "object",
            "required": ["ticks"],
            "properties": {
                "ticks": {
                    "type": "integer",
                    "description": "Ticks to simulate. 3600 = 1 sim-hour, 28800 = 8 h.",
                },
                "changes": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Optional temporary changes (same format as apply_changes.changes).",
                },
            },
        },
    },
]


def _anthropic_tools() -> list[dict]:
    return [
        {
            "name":         t["name"],
            "description":  t["description"],
            "input_schema": t["parameters"],
        }
        for t in _TOOL_SPECS
    ]


def _openai_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name":        t["name"],
                "description": t["description"],
                "parameters":  t["parameters"],
            },
        }
        for t in _TOOL_SPECS
    ]


# ── Low-level API calls ───────────────────────────────────────────────────────

def _post_json(url: str, headers: dict, payload: dict) -> dict:
    """POST JSON payload; return parsed response or {"error": str}."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read().decode())
            # Anthropic wraps in {"error": {"message": ...}}
            # OpenAI wraps in {"error": {"message": ...}} too
            msg = err.get("error", {})
            if isinstance(msg, dict):
                msg = msg.get("message", str(exc))
            return {"error": str(msg)}
        except Exception:
            return {"error": f"HTTP {exc.code}: {exc.reason}"}
    except Exception as exc:
        return {"error": str(exc)}


def _call_anthropic(messages: list[dict]) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY is not set. Export it: export ANTHROPIC_API_KEY=sk-ant-..."}
    return _post_json(
        _ANTHROPIC_URL,
        headers={
            "x-api-key":         api_key,
            "anthropic-version": _ANT_VERSION,
        },
        payload={
            "model":      _MODEL,
            "max_tokens": 2048,
            "system":     _SYSTEM,
            "messages":   messages,
            "tools":      _anthropic_tools(),
        },
    )


def _call_openai(messages: list[dict]) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return {"error": "OPENAI_API_KEY is not set. Export it: export OPENAI_API_KEY=sk-..."}
    # Prepend system message for OpenAI (it lives in the messages array)
    full_messages = [{"role": "system", "content": _SYSTEM}] + messages
    return _post_json(
        _OPENAI_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        payload={
            "model":    _MODEL,
            "messages": full_messages,
            "tools":    _openai_tools(),
        },
    )


# ── Response normalisation ────────────────────────────────────────────────────
# Both providers are normalised to the same internal format:
#   {
#     "stop_reason": "tool_use" | "end_turn",
#     "content": [
#       {"type": "text",     "text": "..."},
#       {"type": "tool_use", "id": "...", "name": "...", "input": {...}},
#     ]
#   }

def _normalise_anthropic(resp: dict) -> dict:
    """Anthropic response is already in the canonical format."""
    return resp   # stop_reason + content[] already match


def _normalise_openai(resp: dict) -> dict:
    """Convert OpenAI response to canonical format."""
    if "error" in resp:
        return resp
    try:
        choice  = resp["choices"][0]
        message = choice["message"]
        finish  = choice.get("finish_reason", "stop")

        content: list[dict] = []
        if message.get("content"):
            content.append({"type": "text", "text": message["content"]})
        for tc in message.get("tool_calls") or []:
            try:
                inp = json.loads(tc["function"]["arguments"])
            except Exception:
                inp = {}
            content.append({
                "type":  "tool_use",
                "id":    tc["id"],
                "name":  tc["function"]["name"],
                "input": inp,
            })

        stop_reason = "tool_use" if finish == "tool_calls" else "end_turn"
        return {"stop_reason": stop_reason, "content": content}
    except (KeyError, IndexError) as exc:
        return {"error": f"Unexpected OpenAI response shape: {exc} — {resp}"}


def _call_api(messages: list[dict]) -> dict:
    """Call the configured provider and return a normalised response."""
    if _PROVIDER == "openai":
        raw = _call_openai(messages)
        return _normalise_openai(raw)
    raw = _call_anthropic(messages)
    return _normalise_anthropic(raw)


# ── History helpers ───────────────────────────────────────────────────────────
# Anthropic expects tool results as role="user" content blocks.
# OpenAI expects them as separate role="tool" messages.
# We store history in Anthropic format internally and convert on the fly for OpenAI.

def _history_for_api(messages: list[dict]) -> list[dict]:
    """Convert internal history to the format expected by the active provider."""
    if _PROVIDER != "openai":
        return messages

    converted: list[dict] = []
    for msg in messages:
        if msg["role"] == "assistant":
            # Rebuild OpenAI assistant message with tool_calls array
            text_parts   = [b["text"] for b in msg["content"] if b.get("type") == "text"]
            tool_calls   = []
            for b in msg["content"]:
                if b.get("type") == "tool_use":
                    tool_calls.append({
                        "id":       b["id"],
                        "type":     "function",
                        "function": {
                            "name":      b["name"],
                            "arguments": json.dumps(b.get("input", {})),
                        },
                    })
            oai_msg: dict = {"role": "assistant", "content": " ".join(text_parts) or None}
            if tool_calls:
                oai_msg["tool_calls"] = tool_calls
            converted.append(oai_msg)

        elif msg["role"] == "user":
            # Check if this is a tool_result batch (Anthropic style)
            content = msg["content"]
            if isinstance(content, list) and content and content[0].get("type") == "tool_result":
                # Explode into individual role="tool" messages
                for block in content:
                    converted.append({
                        "role":         "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content":      block["content"],
                    })
            else:
                converted.append(msg)
        else:
            converted.append(msg)
    return converted


# ── Agentic message handler ───────────────────────────────────────────────────

def handle_message(
    state: "SimState",
    user_message: str,
    history: list[dict],
) -> dict:
    """
    Drive one user turn through the tool-use loop.

    Parameters
    ----------
    state        : SimState (from web.py) — exposes get_state_summary(),
                   apply_changes(), quick_forecast()
    user_message : the new user message string
    history      : previous messages list (accumulates across turns)

    Returns
    -------
    {
        "reply":           str,
        "history":         list[dict],
        "changes_applied": bool,
        "forecast":        dict | None,
        "error":           str | None,
    }
    """
    messages        = list(history) + [{"role": "user", "content": user_message}]
    changes_applied = False
    forecast        = None

    for _step in range(8):          # cap tool-use iterations
        resp = _call_api(_history_for_api(messages))

        if "error" in resp:
            return {
                "reply":           f"⚠️ {resp['error']}",
                "history":         history,
                "changes_applied": False,
                "forecast":        None,
                "error":           resp["error"],
            }

        content     = resp.get("content", [])
        stop_reason = resp.get("stop_reason", "end_turn")

        # Store in internal (Anthropic-style) format
        messages.append({"role": "assistant", "content": content})

        if stop_reason != "tool_use":
            reply = " ".join(
                b.get("text", "") for b in content if b.get("type") == "text"
            ).strip()
            return {
                "reply":           reply or "(no response)",
                "history":         messages[-24:],
                "changes_applied": changes_applied,
                "forecast":        forecast,
                "error":           None,
            }

        # Execute every tool_use block before next round-trip
        tool_results: list[dict] = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            name = block["name"]
            inp  = block.get("input", {})
            tid  = block["id"]

            if name == "get_simulation_state":
                result = state.get_state_summary()
            elif name == "apply_changes":
                ok, msg = state.apply_changes(inp.get("changes", []))
                changes_applied = changes_applied or ok
                result = {"success": ok, "message": msg}
            elif name == "run_forecast":
                fc = state.quick_forecast(
                    ticks=int(inp.get("ticks", 3600)),
                    temp_changes=inp.get("changes") or [],
                )
                forecast = fc
                result   = fc
            else:
                result = {"error": f"Unknown tool: {name}"}

            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": tid,
                "content":     json.dumps(result, default=str),
            })

        messages.append({"role": "user", "content": tool_results})

    return {
        "reply":           "I couldn't complete that in the allowed number of steps.",
        "history":         messages[-24:],
        "changes_applied": changes_applied,
        "forecast":        forecast,
        "error":           None,
    }
