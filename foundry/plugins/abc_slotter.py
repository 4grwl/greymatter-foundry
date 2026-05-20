"""
ABC Slotter — demand-class aware rack assignment.

Assigns high-demand (class A) SKUs to the rack slots closest to dock doors
so agents travel the shortest distance for the most frequent picks.

    A-class: nearest third of accessible rack faces  (hot zone)
    B-class: middle third                             (warm zone)
    C-class: farthest third                           (cold zone)

Slot assignment within each zone is random (shuffled by seed).

Plugin contract
---------------
Expose a callable named ``slotter`` with signature:
    slotter(skus, grid, seed=42) -> dict[sku_id, rack_cell]
"""
from __future__ import annotations

import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foundry.grid import Grid


def slotter(skus, grid: Grid, seed: int = 42) -> dict[str, tuple[int, int]]:
    """
    Assign SKUs to rack cells ranked by Manhattan distance to the nearest dock.
    A-class goes closest, C-class goes farthest.

    Args:
        skus:  list of SKU objects (must have .sku_id and .demand_class attributes)
        grid:  warehouse Grid
        seed:  RNG seed for within-zone shuffle

    Returns:
        dict mapping sku_id -> rack_cell (the cell itself, not the pick_pos)
    """
    from foundry.grid import Cell
    from foundry.inventory import _find_pick_pos

    docks = grid.cells_of_type(Cell.DOCK)
    if not docks:
        raise ValueError("Grid has no DOCK cells — cannot rank slots by dock distance.")

    # Build list of (dock_distance, rack_cell) for all accessible rack faces
    ranked: list[tuple[int, tuple[int, int]]] = []
    for rack in grid.cells_of_type(Cell.RACK):
        pick = _find_pick_pos(grid, rack)
        if pick is None:
            continue
        dist = min(abs(pick[0] - d[0]) + abs(pick[1] - d[1]) for d in docks)
        ranked.append((dist, rack))

    ranked.sort(key=lambda x: x[0])   # closest first
    accessible = [rack for _, rack in ranked]

    if not accessible:
        return {}

    # Partition rack slots into three zones
    n = len(accessible)
    zone_a = accessible[: n // 3]
    zone_b = accessible[n // 3 : 2 * n // 3]
    zone_c = accessible[2 * n // 3 :]

    # Partition SKUs by demand class
    a_skus = [s for s in skus if getattr(s, "demand_class", "B") == "A"]
    b_skus = [s for s in skus if getattr(s, "demand_class", "B") == "B"]
    c_skus = [s for s in skus if getattr(s, "demand_class", "B") not in ("A", "B")]

    rng = random.Random(seed)
    rng.shuffle(a_skus)
    rng.shuffle(b_skus)
    rng.shuffle(c_skus)

    result: dict[str, tuple[int, int]] = {}

    def _assign(sku_list, slot_list) -> None:
        for i, sku in enumerate(sku_list):
            if i < len(slot_list):
                result[sku.sku_id] = slot_list[i]

    _assign(a_skus, zone_a)
    _assign(b_skus, zone_b)
    _assign(c_skus, zone_c)

    # Any SKUs left unassigned (overflow) fall into whatever slots remain
    assigned_slots = set(result.values())
    overflow_skus  = [s for s in skus if s.sku_id not in result]
    overflow_slots = [r for r in accessible if r not in assigned_slots]
    rng.shuffle(overflow_slots)
    for i, sku in enumerate(overflow_skus):
        if i < len(overflow_slots):
            result[sku.sku_id] = overflow_slots[i]

    return result


# ── Convenience: avg dock distance for a slotting (used in tests / benchmarks)

def avg_dock_distance(slot_map: dict[str, tuple[int, int]], grid: Grid) -> float:
    """Return mean Manhattan distance from each slotted pick_pos to nearest dock."""
    from foundry.grid import Cell
    from foundry.inventory import _find_pick_pos

    docks = grid.cells_of_type(Cell.DOCK)
    distances = []
    for rack in slot_map.values():
        pick = _find_pick_pos(grid, rack)
        if pick:
            distances.append(min(abs(pick[0] - d[0]) + abs(pick[1] - d[1]) for d in docks))
    return sum(distances) / len(distances) if distances else 0.0
