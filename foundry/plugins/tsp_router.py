"""
TSP Sequencer — nearest-neighbour pick-task ordering.

Reorders an agent's pick task list so the total travel distance is
minimised using a greedy nearest-neighbour heuristic.  Starting from
the agent's current position, the next pick is always the closest
unvisited task (by Manhattan distance).

This is a classic NN-TSP with O(n²) runtime — fast for the typical
1–5 lines per order, and a sensible baseline before applying 2-opt or
ML-based improvements.

Plugin contract
---------------
Expose a callable named ``sequencer`` with signature:
    sequencer(agent_pos, tasks) -> tasks

``tasks`` is a list of PickTask objects (have a ``.pick_pos`` attribute).
The function must return the same tasks in a (possibly different) order.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foundry.agent import PickTask


def sequencer(
    agent_pos: tuple[int, int],
    tasks: list,          # list[PickTask]
) -> list:
    """
    Nearest-neighbour TSP ordering of pick tasks.

    Args:
        agent_pos:  current (row, col) of the agent
        tasks:      list of PickTask objects to reorder

    Returns:
        The same tasks in greedy-nearest-neighbour order.
    """
    if len(tasks) <= 1:
        return tasks

    remaining = list(tasks)
    ordered: list = []
    current = agent_pos

    while remaining:
        nearest = min(
            remaining,
            key=lambda t: _manhattan(t.pick_pos, current),
        )
        ordered.append(nearest)
        current = nearest.pick_pos
        remaining.remove(nearest)

    return ordered


def _manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


# ── Analysis helpers (used in tests and benchmarks) ────────────────────────

def total_distance(agent_pos: tuple[int, int], tasks: list) -> int:
    """Total Manhattan travel distance for a given task ordering."""
    dist = 0
    current = agent_pos
    for t in tasks:
        dist += _manhattan(current, t.pick_pos)
        current = t.pick_pos
    return dist


def improvement_ratio(agent_pos: tuple[int, int], tasks: list) -> float:
    """
    Return (default_distance - tsp_distance) / default_distance.
    Positive means TSP ordering saves travel.  Zero or negative means
    the original order was already optimal (or TSP made it worse).
    """
    if len(tasks) <= 1:
        return 0.0
    original = total_distance(agent_pos, tasks)
    optimised = total_distance(agent_pos, sequencer(agent_pos, tasks))
    if original == 0:
        return 0.0
    return (original - optimised) / original
