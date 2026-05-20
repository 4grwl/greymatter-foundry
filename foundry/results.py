"""
Results persistence for Greymatter Foundry simulation runs.

Writes two files per run into the output directory:
  run_<seed>_summary.csv  — one row of KPIs
  run_<seed>_events.csv   — full event log
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foundry.simulation import Simulation


def write_results(sim: Simulation, out_dir: str | Path, seed: int) -> dict[str, Path]:
    """
    Write summary and event CSVs. Returns dict with paths written.
    Creates out_dir if it does not exist.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    summary_path = out / f"run_{seed}_summary.csv"
    events_path  = out / f"run_{seed}_events.csv"

    _write_summary(sim, summary_path, seed)
    _write_events(sim, events_path)

    return {"summary": summary_path, "events": events_path}


def _write_summary(sim: Simulation, path: Path, seed: int) -> None:
    kpis = sim.kpis.summary(sim.clock, sim.agents)

    # Per-agent stats appended as extra columns
    agent_cols: dict[str, int | float] = {}
    for agent in sim.agents:
        prefix = agent.agent_id
        agent_cols[f"{prefix}_steps"]      = agent.steps_taken
        agent_cols[f"{prefix}_orders"]     = agent.orders_completed
        agent_cols[f"{prefix}_battery"]    = round(agent.battery_pct, 1)
        agent_cols[f"{prefix}_congestion"] = agent.congestion_events

    row = {"seed": seed, **kpis, **agent_cols}

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)


def _write_events(sim: Simulation, path: Path) -> None:
    if not sim.events:
        path.write_text("type,agent,tick\n")
        return

    # Collect all field names across all events
    fieldnames: list[str] = ["type", "agent", "tick"]
    seen: set[str] = set(fieldnames)
    for evt in sim.events:
        for k in evt:
            if k not in seen:
                fieldnames.append(k)
                seen.add(k)

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore",
                                restval="")
        writer.writeheader()
        writer.writerows(sim.events)


def print_summary(kpis: dict) -> None:
    """Print a compact KPI table to stdout (no third-party deps)."""
    width = 42
    print("=" * width)
    print(f"{'GREYMATTER FOUNDRY':^{width}}")
    print(f"{'Simulation Results':^{width}}")
    print("=" * width)
    rows = [
        ("Orders completed",    f"{kpis['orders_completed']}"),
        ("Lines completed",     f"{kpis['lines_completed']}"),
        ("Orders / hour",       f"{kpis['orders_per_hour']:.1f}"),
        ("Lines / hour",        f"{kpis['lines_per_hour']:.1f}"),
        ("Avg cycle time (s)",  f"{kpis['avg_cycle_time_s']:.0f}"),
        ("Total travel steps",  f"{kpis['total_travel_steps']}"),
        ("Congestion events",   f"{kpis['congestion_events']}"),
        ("Avg battery %",       f"{kpis['avg_battery_pct']:.1f}"),
        ("Elapsed ticks",       f"{kpis['elapsed_ticks']}"),
    ]
    for label, value in rows:
        print(f"  {label:<26}{value:>12}")
    print("=" * width)
