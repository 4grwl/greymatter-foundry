# Greymatter Foundry — Lo-Fi Local Edition
### Warehouse Optimisation Simulator · Spec v0.1

---

## 1. Purpose

A self-contained, locally-runnable simulation of warehouse operations for rapid prototyping
of optimisation algorithms. No cloud dependency, no real hardware, no high-fidelity physics.
Everything runs in a single process on a laptop.

The goal is to get ideas from whiteboard to measurable result in a day, not a sprint.

---

## 2. Scope & Non-Goals

**In scope**
- 2D grid-based warehouse layout (no 3D, no CAD)
- Discrete-event simulation of pick, place, and replenishment tasks
- Agent-based robots (AMRs) with simple collision avoidance
- Pluggable optimisation hooks (routing, slotting, scheduling)
- CLI + optional terminal-UI dashboard
- Deterministic replay from seed for reproducible benchmarks

**Out of scope**
- Real-time 3D rendering
- Physics engine or continuous kinematics
- WMS / ERP integration
- Multi-warehouse federation
- Any network communication

---

## 3. Core Concepts

### 3.1 Grid World

The warehouse is a 2D integer grid `W × H` (default 50 × 30 cells).  
Each cell is one of: `FLOOR | RACK | AISLE | DOCK | CHARGING`.

A YAML layout file defines zones, rack rows, dock doors, and charging stations.
The simulator validates connectivity (every dock reachable from every rack via AISLE) on load.

### 3.2 Inventory

Items have:
- `sku_id` — unique string
- `weight_kg` — float
- `volume_m3` — float
- `demand_class` — A / B / C (pareto tier, affects slotting)
- `current_slot` — grid cell

Stock is stored in a flat CSV or SQLite file. No live sync.

### 3.3 Orders

An order is a list of `(sku_id, qty)` lines with an `arrive_at` timestamp (sim-time seconds).
Orders are loaded from CSV at startup. A stochastic generator can also produce them from a
demand profile (Poisson arrivals, ABC-weighted SKU draw).

### 3.4 Agents (AMRs)

Each robot has:
- `position` — (row, col)
- `state` — IDLE | MOVING | PICKING | DROPPING | CHARGING
- `battery_pct` — 0–100, drains per step, recharges at CHARGING cells
- `payload` — list of (sku_id, qty) currently carried (up to `capacity` weight/volume)

Pathfinding: A* on the grid, with occupied cells treated as temporarily blocked.
No true multi-agent path planning (MAPF) — just re-route on collision detection.
A MAPF plugin slot is reserved for later.

### 3.5 Simulation Clock

Discrete-event, step-based. One tick = 1 simulated second.
The main loop:
1. Advance clock by one tick.
2. Dispatch new orders that arrived at this tick.
3. Tick each agent (move one cell, or complete action).
4. Emit events to the event log.
5. Recompute KPIs.

Speed: run as fast as CPU allows (no wall-clock throttle by default).
A `--realtime 10x` flag caps speed for visual demos.

---

## 4. Optimisation Hooks

The simulator exposes three plugin points. Each is a Python callable with a defined signature.
Swap in a new function to test a different algorithm without touching the sim core.

| Hook | Signature | Default |
|---|---|---|
| **Router** | `route(agent, target, grid) -> list[cell]` | A* shortest path |
| **Slotter** | `slot(sku_list, grid) -> dict[sku_id, cell]` | Random assignment |
| **Dispatcher** | `dispatch(orders, agents) -> list[(order, agent)]` | FIFO nearest-agent |

Example: drop in a TSP-based router, a demand-class slotter, or a priority-queue dispatcher
without changing any other code.

Plugins live in `plugins/` and are loaded by name at startup:
```
python -m foundry --router plugins/tsp_router.py --slotter plugins/abc_slotter.py
```

---

## 5. KPIs Tracked

| Metric | Definition |
|---|---|
| Orders per hour (OPH) | completed orders / sim-hours elapsed |
| Lines per hour (LPH) | completed order lines / sim-hours elapsed |
| Avg cycle time | mean (order complete - order arrive) seconds |
| Travel distance | total cell-steps across all agents |
| Battery utilisation | % time agents are moving vs idle vs charging |
| Congestion events | count of re-routes triggered by blocked paths |

All KPIs are written to `results/run_<seed>.csv` at end of simulation.

---

## 6. Interfaces

### 6.1 CLI

```
python -m foundry [OPTIONS]

Options:
  --layout   PATH     Warehouse YAML layout          [default: layouts/default.yaml]
  --orders   PATH     Orders CSV                     [default: data/orders.csv]
  --agents   INT      Number of AMR agents           [default: 5]
  --seed     INT      RNG seed for reproducibility   [default: 42]
  --ticks    INT      Simulation duration in ticks   [default: 28800  (8h)]
  --realtime FLOAT    Wall-clock speed multiplier    [optional]
  --router   PATH     Router plugin file             [optional]
  --slotter  PATH     Slotter plugin file            [optional]
  --dispatch PATH     Dispatcher plugin file         [optional]
  --tui               Launch terminal dashboard      [flag]
  --out      DIR      Results output directory       [default: results/]
```

### 6.2 Terminal UI (optional, `--tui`)

Built with `rich` or `textual`. Three panels:
- **Grid view** — ASCII/Unicode warehouse map, agents shown as coloured `@` symbols,
  racks as `█`, docks as `D`, charging as `⚡`
- **KPI panel** — live OPH, LPH, cycle time, battery
- **Event log** — last 20 events (order dispatched, pick complete, battery low, etc.)

Refresh rate: 4 Hz (sufficient, no flicker, low CPU).

### 6.3 Python API (for notebooks / scripts)

```python
from foundry import Simulation, Layout, OrderStream

sim = Simulation(
    layout=Layout.from_yaml("layouts/default.yaml"),
    orders=OrderStream.from_csv("data/orders.csv"),
    n_agents=5,
    seed=42,
)
sim.run(ticks=28800)
print(sim.kpis())
```

---

## 7. File Structure

```
foundry/
├── foundry/
│   ├── __main__.py          # CLI entry point
│   ├── simulation.py        # Main loop, clock, event bus
│   ├── grid.py              # Grid world, pathfinding (A*)
│   ├── agent.py             # AMR agent model
│   ├── inventory.py         # SKU / slot / stock management
│   ├── orders.py            # Order model, stochastic generator
│   ├── kpis.py              # KPI accumulators
│   ├── tui.py               # Terminal UI (optional dep)
│   └── plugins/
│       ├── base.py          # Plugin interfaces / type hints
│       ├── abc_slotter.py   # Demand-class ABC slotter
│       └── tsp_router.py    # Nearest-neighbour TSP router stub
├── layouts/
│   └── default.yaml         # 50×30 reference layout
├── data/
│   └── orders_sample.csv    # 500-order sample for smoke tests
├── results/                 # Auto-created on first run
├── tests/
│   ├── test_grid.py
│   ├── test_agent.py
│   └── test_kpis.py
├── requirements.txt         # numpy, pyyaml, rich (or textual), pytest
└── README.md
```

---

## 8. Dependencies

| Package | Purpose | Version floor |
|---|---|---|
| `numpy` | Grid ops, distance matrices | 1.24 |
| `pyyaml` | Layout file parsing | 6.0 |
| `rich` | Terminal UI + progress | 13.0 |
| `textual` | Full TUI (optional) | 0.50 |
| `pytest` | Test runner | 7.0 |

No ML frameworks. No simulation engines. Pure Python + numpy.

---

## 9. Performance Targets

| Parameter | Target |
|---|---|
| Grid size | up to 200 × 100 cells |
| Agents | up to 50 simultaneous |
| Simulation speed | ≥ 1000 ticks/sec on a 2020 laptop (no TUI) |
| TUI overhead | < 20% slowdown at 4 Hz refresh |
| Memory | < 200 MB resident for default scenario |

---

## 10. Milestones

| # | Deliverable | Done when |
|---|---|---|
| M1 | Grid + pathfinding | A* finds shortest path, unit tested |
| M2 | Agent loop | 5 agents pick and deliver orders, KPIs emit |
| M3 | CLI + results CSV | `python -m foundry` runs full 8h sim, writes results |
| M4 | Plugin system | ABC slotter and TSP router swap in without sim changes |
| M5 | Terminal UI | `--tui` shows live grid and KPI panel |
| M6 | Stochastic generator | Demand profile produces reproducible order streams |

---

## 11. Open Questions

1. **Collision model** — re-route on next tick (simplest) vs. reservation table vs. priority
   yield. Start with re-route; measure congestion events to decide if upgrade needed.
2. **Battery model** — linear drain good enough, or model acceleration/deceleration?
   Linear for now.
3. **Multi-SKU picks** — should one agent batch multiple order lines in one trip?
   Yes, up to payload capacity. Batching logic lives in the dispatcher hook.
4. **Layout editor** — out of scope for v0.1, but a `foundry-edit` CLI that prints a grid
   and lets you toggle cell types via coordinates would be cheap to add in M5.
