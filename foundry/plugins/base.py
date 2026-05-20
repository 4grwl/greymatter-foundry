"""
Plugin interfaces for the three optimisation hooks.

Each hook is a plain callable. Swap in a new function to test a different
algorithm without touching the simulation core.

    Router:     route(agent, target, grid)  -> list[cell]
    Slotter:    slot(skus, grid)            -> dict[sku_id, rack_cell]
    Dispatcher: dispatch(orders, agents)    -> list[(order, agent)]
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foundry.agent import Agent
    from foundry.grid import Grid
    from foundry.orders import Order


# ── Router ─────────────────────────────────────────────────────────────────

def default_router(
    agent: Agent,
    target: tuple[int, int],
    grid: Grid,
    blocked: set[tuple[int, int]] | None = None,
) -> list[tuple[int, int]]:
    """A* shortest path. Falls back to unblocked search if blocked path fails."""
    path = grid.astar(agent.position, target, blocked)
    if path is None:
        path = grid.astar(agent.position, target)
    return path or []


# ── Dispatcher ─────────────────────────────────────────────────────────────

def default_dispatcher(
    pending: list[Order],
    idle_agents: list[Agent],
    pick_pos_fn,          # callable: sku_id -> (row,col) | None
) -> list[tuple[Order, Agent]]:
    """
    FIFO order queue, nearest-idle-agent assignment.
    Returns (order, agent) pairs to dispatch this tick.
    """
    assignments: list[tuple[Order, Agent]] = []
    remaining_idle = list(idle_agents)

    for order in pending:
        if not remaining_idle:
            break
        first_sku = order.lines[0].sku_id
        pos = pick_pos_fn(first_sku)
        if pos is None:
            continue
        agent = min(
            remaining_idle,
            key=lambda a: abs(a.position[0] - pos[0]) + abs(a.position[1] - pos[1]),
        )
        assignments.append((order, agent))
        remaining_idle.remove(agent)

    return assignments


# ── Slotter ────────────────────────────────────────────────────────────────

def default_slotter(skus, grid: Grid, seed: int = 42) -> dict:
    """Random slot assignment (baseline). Returns {sku_id: rack_cell}."""
    import random
    from foundry.grid import Cell

    rack_cells = grid.cells_of_type(Cell.RACK)
    rng = random.Random(seed)
    rng.shuffle(rack_cells)
    return {sku.sku_id: rack_cells[i] for i, sku in enumerate(skus) if i < len(rack_cells)}
