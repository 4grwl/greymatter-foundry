"""
Greymatter Foundry CLI

Usage:
    python -m foundry [OPTIONS]

Run `python -m foundry --help` for full option list.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path


# ──────────────────────────────────────────────────────────────────────────────
# Argument parser
# ──────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m foundry",
        description="Greymatter Foundry — warehouse optimisation simulator",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--layout",   default="layouts/default.json",
                   help="Warehouse layout JSON file")
    p.add_argument("--orders",   default=None,
                   help="Orders CSV file (omit to use stochastic generator)")
    p.add_argument("--agents",   type=int,   default=5,
                   help="Number of AMR agents")
    p.add_argument("--seed",     type=int,   default=42,
                   help="RNG seed for reproducibility")
    p.add_argument("--ticks",    type=int,   default=28800,
                   help="Simulation duration in ticks (1 tick = 1 s; 28800 = 8 h)")
    p.add_argument("--realtime", type=float, default=None, metavar="MULTIPLIER",
                   help="Cap sim speed to MULTIPLIER × real time (e.g. 10 = 10× faster)")
    p.add_argument("--n-skus",   type=int,   default=50,   dest="n_skus",
                   help="SKUs to generate when using stochastic order mode")
    p.add_argument("--n-orders", type=int,   default=500,  dest="n_orders",
                   help="Orders to generate when using stochastic order mode")
    p.add_argument("--router",    default=None, metavar="PATH",
                   help="Router plugin .py file (must expose a `router` callable)")
    p.add_argument("--sequencer", default=None, metavar="PATH",
                   help="Sequencer plugin .py file (must expose a `sequencer` callable "
                        "with signature: sequencer(agent_pos, tasks) -> tasks)")
    p.add_argument("--slotter",  default=None, metavar="PATH",
                   help="Slotter plugin .py file (must expose a `slotter` callable)")
    p.add_argument("--dispatch", default=None, metavar="PATH",
                   help="Dispatcher plugin .py file (must expose a `dispatcher` callable)")
    p.add_argument("--gui",      action="store_true",
                   help="Launch live tkinter GUI (default when display available)")
    p.add_argument("--tui",      action="store_true",
                   help="Launch terminal dashboard (requires `rich`)")
    p.add_argument("--web",      action="store_true",
                   help="Launch browser-based web GUI (HTML5 canvas + SSE)")
    p.add_argument("--gui-speed", type=float, default=10.0, dest="gui_speed",
                   metavar="TICKS_PER_SEC",
                   help="Initial GUI/web simulation speed in ticks/second (default: 10)")
    p.add_argument("--web-port", type=int, default=5050, dest="web_port",
                   metavar="PORT",
                   help="Port for the web GUI server (default: 5050)")
    p.add_argument("--out",      default="results",
                   help="Output directory for results CSV files")
    p.add_argument("--quiet",    action="store_true",
                   help="Suppress progress output")
    return p


# ──────────────────────────────────────────────────────────────────────────────
# Plugin loader
# ──────────────────────────────────────────────────────────────────────────────

def load_fn(path: str, attr: str):
    """Import a .py file and return the named callable from it."""
    spec = importlib.util.spec_from_file_location("_plugin", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load plugin: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)              # type: ignore[union-attr]
    if not hasattr(mod, attr):
        raise AttributeError(f"Plugin {path} must expose a callable named '{attr}'")
    return getattr(mod, attr)


# ──────────────────────────────────────────────────────────────────────────────
# Simulation builders
# ──────────────────────────────────────────────────────────────────────────────

def build_simulation(args: argparse.Namespace):
    from foundry.grid import Grid
    from foundry.inventory import Inventory, SKU
    from foundry.orders import OrderStream
    from foundry.simulation import Simulation

    if not args.quiet:
        print(f"Loading layout: {args.layout}")
    grid = Grid.from_json(args.layout)

    # ── Orders ──────────────────────────────────────────────────────────
    if args.orders and Path(args.orders).exists():
        if not args.quiet:
            print(f"Loading orders: {args.orders}")
        orders = OrderStream.from_csv(args.orders)
        # Build inventory from SKUs referenced in the orders
        sku_ids: list[str] = []
        seen: set[str] = set()
        for o in orders._orders:
            for line in o.lines:
                if line.sku_id not in seen:
                    sku_ids.append(line.sku_id)
                    seen.add(line.sku_id)
        skus = [SKU(sku_id=sid) for sid in sku_ids[: args.n_skus]]
    else:
        if not args.quiet:
            print(f"Generating {args.n_orders} orders over {args.ticks} ticks "
                  f"({args.n_skus} SKUs, seed={args.seed})")
        skus = [SKU(sku_id=f"SKU{i:04d}") for i in range(args.n_skus)]
        orders = OrderStream.generate(
            n_orders=args.n_orders,
            skus=[s.sku_id for s in skus],
            duration_ticks=args.ticks,
            seed=args.seed,
        )

    # ── Slotter plugin ───────────────────────────────────────────────────
    if args.slotter:
        slotter_fn = load_fn(args.slotter, "slotter")
        from foundry.inventory import Slot
        from foundry.inventory import _find_pick_pos
        slot_map = slotter_fn(skus, grid)
        slots = [
            Slot(sku_id=sid, rack_pos=rack, pick_pos=_find_pick_pos(grid, rack))
            for sid, rack in slot_map.items()
            if _find_pick_pos(grid, rack) is not None
        ]
        inventory = Inventory(skus, slots)
    else:
        inventory = Inventory.build(grid, skus, seed=args.seed)

    if not args.quiet:
        print(f"Inventory: {inventory.slot_count()} SKUs slotted")

    # ── Dispatcher / sequencer plugins ──────────────────────────────────
    dispatcher_fn  = load_fn(args.dispatch,  "dispatcher") if getattr(args, "dispatch",   None) else None
    sequencer_fn   = load_fn(args.sequencer, "sequencer")  if getattr(args, "sequencer",  None) else None

    if not args.quiet:
        active = []
        if dispatcher_fn:  active.append("custom dispatcher")
        if sequencer_fn:   active.append("TSP sequencer")
        if active:
            print(f"Plugins active: {', '.join(active)}")

    return Simulation(
        grid=grid,
        inventory=inventory,
        orders=orders,
        n_agents=args.agents,
        seed=args.seed,
        dispatcher_fn=dispatcher_fn,
        task_sequencer=sequencer_fn,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Run modes
# ──────────────────────────────────────────────────────────────────────────────

def run_headless(sim, args: argparse.Namespace) -> None:
    """Run without a UI, optionally throttled to real-time."""
    ticks        = args.ticks
    realtime     = args.realtime
    quiet        = args.quiet
    report_every = max(1, ticks // 20)   # print progress ~20 times

    if not quiet:
        print(f"Running {ticks} ticks with {args.agents} agents "
              f"(seed={args.seed})...")

    wall_start = time.monotonic()
    tick_duration = (1.0 / realtime) if realtime else 0.0

    for t in range(1, ticks + 1):
        tick_wall = time.monotonic()
        sim.step()

        if realtime and tick_duration > 0:
            elapsed = time.monotonic() - tick_wall
            sleep_for = tick_duration - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

        if not quiet and t % report_every == 0:
            pct = t / ticks * 100
            kpis = sim.kpis
            print(f"  {pct:5.1f}%  tick={t:6d}  "
                  f"completed={kpis.orders_completed}  "
                  f"pending={len(sim._pending)}")

    if not quiet:
        wall_elapsed = time.monotonic() - wall_start
        print(f"Done in {wall_elapsed:.1f}s wall time.")


def run_with_web(sim, args: argparse.Namespace) -> None:
    """Run with the browser-based web GUI."""
    import importlib
    try:
        _web = importlib.import_module("foundry.web")
    except ImportError as e:
        print(f"Web GUI unavailable: {e}", file=sys.stderr)
        run_headless(sim, args)
        return
    port  = getattr(args, "web_port",  5050)
    speed = getattr(args, "gui_speed", 10.0)
    _web.launch(sim, port=port, args=args, speed=speed)


def run_with_gui(sim, args: argparse.Namespace) -> None:
    """Run with the tkinter GUI."""
    import importlib
    try:
        _gui = importlib.import_module("foundry.gui")
    except ImportError as e:
        print(f"GUI unavailable: {e}", file=sys.stderr)
        print("Falling back to headless mode.")
        run_headless(sim, args)
        return
    speed = getattr(args, "gui_speed", 10.0)
    _gui.launch(sim, speed=speed, args=args)


def run_with_tui(sim, args: argparse.Namespace) -> None:
    """Run with a live terminal dashboard using `rich`."""
    try:
        from rich.live import Live
        from rich.table import Table
        from rich.layout import Layout
        from rich.panel import Panel
        from rich.text import Text
    except ImportError:
        print("TUI requires `rich`. Install it with:  pip install rich", file=sys.stderr)
        print("Falling back to headless mode.")
        run_headless(sim, args)
        return

    ticks        = args.ticks
    realtime     = args.realtime
    tick_duration = (1.0 / realtime) if realtime else 0.0
    refresh_every = max(1, int(0.25 / tick_duration) if tick_duration > 0 else 250)

    def make_table() -> Table:
        kpis = sim.kpis.summary(sim.clock, sim.agents)
        t = Table(title=f"Tick {sim.clock}/{ticks}", expand=True)
        t.add_column("Metric", style="cyan")
        t.add_column("Value",  justify="right")
        t.add_row("Orders completed",   str(kpis["orders_completed"]))
        t.add_row("Pending",            str(len(sim._pending)))
        t.add_row("Orders / hour",      f"{kpis['orders_per_hour']:.1f}")
        t.add_row("Avg cycle time (s)", f"{kpis['avg_cycle_time_s']:.0f}")
        t.add_row("Travel steps",       str(kpis["total_travel_steps"]))
        t.add_row("Congestion events",  str(kpis["congestion_events"]))
        t.add_row("Avg battery %",      f"{kpis['avg_battery_pct']:.1f}")
        for agent in sim.agents:
            t.add_row(f"  {agent.agent_id} state",
                      agent.state.name.replace("_", " ").title())
        return t

    with Live(make_table(), refresh_per_second=4, screen=False) as live:
        for tick in range(1, ticks + 1):
            tick_wall = time.monotonic()
            sim.step()

            if tick % refresh_every == 0:
                live.update(make_table())

            if tick_duration > 0:
                elapsed = time.monotonic() - tick_wall
                sleep_for = tick_duration - elapsed
                if sleep_for > 0:
                    time.sleep(sleep_for)


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    sim = build_simulation(args)

    if getattr(args, "web", False):
        run_with_web(sim, args)
    elif getattr(args, "gui", False):
        run_with_gui(sim, args)
    elif args.tui:
        run_with_tui(sim, args)
    else:
        run_headless(sim, args)

    # Write results
    from foundry.results import write_results, print_summary
    paths = write_results(sim, args.out, args.seed)

    if not args.quiet:
        kpis = sim.kpis.summary(sim.clock, sim.agents)
        print_summary(kpis)
        print(f"\nResults written to:")
        for name, path in paths.items():
            print(f"  {name}: {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
