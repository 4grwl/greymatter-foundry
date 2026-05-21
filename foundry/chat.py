"""
Greymatter Foundry — AI warehouse assistant.

Requires the ANTHROPIC_API_KEY environment variable.
Uses Claude with tool_use to interpret natural-language requests
and translate them into concrete layout / config mutations.

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

_MODEL   = os.environ.get("FOUNDRY_AI_MODEL", "claude-3-5-haiku-20241022")
_API_URL = "https://api.anthropic.com/v1/messages"
_VERSION = "2023-06-01"

# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "get_simulation_state",
        "description": (
            "Return the current simulation's layout zones (with names and coordinates), "
            "live KPIs, agent states, grid dimensions, and config. "
            "Always call this before making any changes so you work with accurate data."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "apply_changes",
        "description": (
            "Apply one or more mutations to the warehouse layout or simulation parameters. "
            "The simulation rebuilds immediately after. "
            "Rack zone row/col bounds are INCLUSIVE. "
            "Row 0 is top-left; rows increase downward, cols increase rightward."
        ),
        "input_schema": {
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
                                    "modify_rack",   # change bounds of an existing rack zone
                                    "add_rack",      # add a new rack zone
                                    "remove_rack",   # delete a rack zone by name
                                    "set_agents",    # change number of AMR agents
                                    "set_demand_rate", # change orders/hr arrival rate
                                    "set_skus",      # change number of SKUs to simulate
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
        "input_schema": {
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


# ── Low-level API call ────────────────────────────────────────────────────────

def _call_anthropic(messages: list[dict]) -> dict:
    """POST to Anthropic messages API. Returns response dict or {"error": str}."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {
            "error": (
                "ANTHROPIC_API_KEY is not set. "
                "Export it in your shell:  export ANTHROPIC_API_KEY=sk-ant-..."
            )
        }
    payload = {
        "model":      _MODEL,
        "max_tokens": 2048,
        "system":     _SYSTEM,
        "messages":   messages,
        "tools":      TOOLS,
    }
    req = urllib.request.Request(
        _API_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         api_key,
            "anthropic-version": _VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read().decode())
            return {"error": err.get("error", {}).get("message", str(exc))}
        except Exception:
            return {"error": f"HTTP {exc.code}: {exc.reason}"}
    except Exception as exc:
        return {"error": str(exc)}


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
        "reply":           str,         # text to display to user
        "history":         list[dict],  # updated history for next call
        "changes_applied": bool,
        "forecast":        dict | None,
        "error":           str | None,
    }
    """
    messages        = list(history) + [{"role": "user", "content": user_message}]
    changes_applied = False
    forecast        = None

    for _step in range(8):          # cap tool-use iterations
        resp = _call_anthropic(messages)

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

        messages.append({"role": "assistant", "content": content})

        if stop_reason != "tool_use":
            reply = " ".join(
                b.get("text", "") for b in content if b.get("type") == "text"
            ).strip()
            return {
                "reply":           reply or "(no response)",
                "history":         messages[-24:],   # keep ~12 turns
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
