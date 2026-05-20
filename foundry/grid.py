"""
Grid world for the Greymatter Foundry warehouse simulator.

Cells are addressed as (row, col) — row 0 is the top of the warehouse.
Pure stdlib — no numpy, no yaml required.
"""

from __future__ import annotations

import heapq
import json
from collections import deque
from enum import IntEnum
from pathlib import Path
from typing import Iterator


class Cell(IntEnum):
    FLOOR     = 0   # open walkable space
    RACK      = 1   # storage rack — not traversable
    AISLE     = 2   # designated aisle — traversable, preferred path
    DOCK      = 3   # inbound / outbound dock door — traversable
    CHARGING  = 4   # AMR charging station — traversable

    @property
    def traversable(self) -> bool:
        return self != Cell.RACK


_GLYPH = {
    Cell.FLOOR:     ".",
    Cell.RACK:      "#",
    Cell.AISLE:     " ",
    Cell.DOCK:      "D",
    Cell.CHARGING:  "C",
}

# Cardinal movement only
_DIRS = [(-1, 0), (1, 0), (0, -1), (0, 1)]


class Grid:
    """
    2-D grid representing a warehouse floor.

    Internally a flat bytearray of Cell values, row-major order.
    All coordinates are (row, col) tuples.
    """

    def __init__(self, rows: int, cols: int, data: bytearray) -> None:
        assert len(data) == rows * cols, "data length must equal rows * cols"
        self._rows = rows
        self._cols = cols
        self._data = data

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def empty(cls, rows: int, cols: int, fill: Cell = Cell.FLOOR) -> Grid:
        return cls(rows, cols, bytearray([int(fill)] * (rows * cols)))

    @classmethod
    def from_json(cls, path: str | Path) -> Grid:
        """
        Load a warehouse layout from JSON.

        Expected structure:
        {
          "rows": 30,
          "cols": 50,
          "zones": [
            {"type": "RACK",  "row_start": 2, "row_end": 12,
                               "col_start": 2, "col_end": 8},
            {"type": "DOCK",  "cells": [[29, 10], [29, 20]]},
            {"type": "CHARGING", "cells": [[0, 0]]}
          ]
        }
        """
        raw = json.loads(Path(path).read_text())
        rows = int(raw["rows"])
        cols = int(raw["cols"])

        # Start all-AISLE so every cell is traversable; RACK overlaid on top.
        data = bytearray([int(Cell.AISLE)] * (rows * cols))

        for zone in raw.get("zones", []):
            cell_type = Cell[zone["type"].upper()]
            if "cells" in zone:
                for r, c in zone["cells"]:
                    data[r * cols + c] = int(cell_type)
            else:
                r0, r1 = int(zone["row_start"]), int(zone["row_end"]) + 1
                c0, c1 = int(zone["col_start"]), int(zone["col_end"]) + 1
                for r in range(r0, r1):
                    for c in range(c0, c1):
                        data[r * cols + c] = int(cell_type)

        grid = cls(rows, cols, data)
        grid.validate()
        return grid

    # ------------------------------------------------------------------
    # Basic accessors
    # ------------------------------------------------------------------

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def shape(self) -> tuple[int, int]:
        return (self._rows, self._cols)

    def _idx(self, r: int, c: int) -> int:
        return r * self._cols + c

    def __getitem__(self, pos: tuple[int, int]) -> Cell:
        r, c = pos
        return Cell(self._data[self._idx(r, c)])

    def __setitem__(self, pos: tuple[int, int], value: Cell) -> None:
        r, c = pos
        self._data[self._idx(r, c)] = int(value)

    def in_bounds(self, r: int, c: int) -> bool:
        return 0 <= r < self._rows and 0 <= c < self._cols

    def traversable(self, r: int, c: int) -> bool:
        return self.in_bounds(r, c) and Cell(self._data[self._idx(r, c)]).traversable

    # ------------------------------------------------------------------
    # Iteration helpers
    # ------------------------------------------------------------------

    def cells_of_type(self, cell_type: Cell) -> list[tuple[int, int]]:
        v = int(cell_type)
        result = []
        for i, b in enumerate(self._data):
            if b == v:
                result.append((i // self._cols, i % self._cols))
        return result

    def traversable_neighbours(self, r: int, c: int) -> Iterator[tuple[int, int]]:
        for dr, dc in _DIRS:
            nr, nc = r + dr, c + dc
            if self.traversable(nr, nc):
                yield (nr, nc)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> None:
        """
        Confirm every DOCK is reachable from every other DOCK via traversable
        cells, and that at least one DOCK and one RACK exist.
        Raises ValueError on failure.
        """
        docks = self.cells_of_type(Cell.DOCK)
        if not docks:
            raise ValueError("Layout must have at least one DOCK cell.")

        racks = self.cells_of_type(Cell.RACK)
        if not racks:
            raise ValueError("Layout must have at least one RACK cell.")

        reachable = self._bfs_reachable(docks[0])
        unreachable = [d for d in docks[1:] if d not in reachable]
        if unreachable:
            raise ValueError(
                f"Layout connectivity error: {len(unreachable)} dock(s) not reachable "
                f"from {docks[0]}: {unreachable[:5]}"
            )

    def _bfs_reachable(self, start: tuple[int, int]) -> set[tuple[int, int]]:
        visited: set[tuple[int, int]] = {start}
        queue: deque[tuple[int, int]] = deque([start])
        while queue:
            r, c = queue.popleft()
            for nb in self.traversable_neighbours(r, c):
                if nb not in visited:
                    visited.add(nb)
                    queue.append(nb)
        return visited

    # ------------------------------------------------------------------
    # A* pathfinding
    # ------------------------------------------------------------------

    def astar(
        self,
        start: tuple[int, int],
        goal: tuple[int, int],
        blocked: set[tuple[int, int]] | None = None,
    ) -> list[tuple[int, int]] | None:
        """
        Find the shortest traversable path from start to goal.

        Returns an ordered list of (row, col) cells including start and goal,
        or None if no path exists.

        `blocked` marks temporarily impassable cells (e.g. cells occupied by
        other agents). start and goal are never treated as blocked.
        """
        if not self.traversable(*start):
            raise ValueError(f"Start cell {start} is not traversable.")
        if not self.traversable(*goal):
            raise ValueError(f"Goal cell {goal} is not traversable.")

        if start == goal:
            return [start]

        blocked = (blocked or set()) - {start, goal}

        def h(r: int, c: int) -> int:
            return abs(r - goal[0]) + abs(c - goal[1])

        # heap: (f, g, (row, col))
        open_heap: list[tuple[int, int, tuple[int, int]]] = []
        heapq.heappush(open_heap, (h(*start), 0, start))

        came_from: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
        g_score: dict[tuple[int, int], int] = {start: 0}

        while open_heap:
            _, g, current = heapq.heappop(open_heap)

            if current == goal:
                return self._reconstruct(came_from, goal)

            if g > g_score.get(current, 10**9):
                continue  # stale entry

            cr, cc = current
            for dr, dc in _DIRS:
                nb = (cr + dr, cc + dc)
                if not self.traversable(*nb) or nb in blocked:
                    continue
                # Prefer AISLE cells (cost 1) over FLOOR/DOCK/CHARGING (cost 2)
                step_cost = 1 if Cell(self._data[self._idx(*nb)]) == Cell.AISLE else 2
                tentative_g = g + step_cost
                if tentative_g < g_score.get(nb, 10**9):
                    g_score[nb] = tentative_g
                    came_from[nb] = current
                    heapq.heappush(open_heap, (tentative_g + h(*nb), tentative_g, nb))

        return None

    @staticmethod
    def _reconstruct(
        came_from: dict[tuple[int, int], tuple[int, int] | None],
        goal: tuple[int, int],
    ) -> list[tuple[int, int]]:
        path: list[tuple[int, int]] = []
        node: tuple[int, int] | None = goal
        while node is not None:
            path.append(node)
            node = came_from[node]
        path.reverse()
        return path

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def __str__(self) -> str:
        lines = []
        for r in range(self._rows):
            row = ""
            for c in range(self._cols):
                row += _GLYPH[Cell(self._data[self._idx(r, c)])]
            lines.append(row)
        return "\n".join(lines)

    def render_path(self, path: list[tuple[int, int]]) -> str:
        """Return grid string with path cells marked as '*'."""
        path_set = set(path)
        lines = []
        for r in range(self._rows):
            row = ""
            for c in range(self._cols):
                if (r, c) in path_set:
                    row += "*"
                else:
                    row += _GLYPH[Cell(self._data[self._idx(r, c)])]
            lines.append(row)
        return "\n".join(lines)
