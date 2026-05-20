"""
M1 unit tests — Grid world and A* pathfinding.
No third-party dependencies — stdlib only.
"""

import sys
import unittest
from pathlib import Path

# Make sure the project root is on the path when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from foundry.grid import Cell, Grid

LAYOUT = Path(__file__).parent.parent / "layouts" / "default.json"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def small_grid() -> Grid:
    """
    5×10 grid:
      col:   0 1 2 3 4 5 6 7 8 9
      row 0: A A A A A A A A A A   (AISLE)
      row 1: A R R R R R R R R A   (RACK block)
      row 2: A R R R R R R R R A
      row 3: A R R R R R R R R A
      row 4: A A A A D A A A A A   (AISLE + 1 DOCK at col 4)

    CHARGING at (0, 0).
    """
    g = Grid.empty(5, 10, Cell.AISLE)
    for r in range(1, 4):
        for c in range(1, 9):
            g[r, c] = Cell.RACK
    g[4, 4] = Cell.DOCK
    g[0, 0] = Cell.CHARGING
    return g


# ──────────────────────────────────────────────────────────────────────────────
# Cell enum
# ──────────────────────────────────────────────────────────────────────────────

class TestCell(unittest.TestCase):
    def test_traversable_cells(self):
        for c in (Cell.FLOOR, Cell.AISLE, Cell.DOCK, Cell.CHARGING):
            self.assertTrue(c.traversable)

    def test_rack_not_traversable(self):
        self.assertFalse(Cell.RACK.traversable)


# ──────────────────────────────────────────────────────────────────────────────
# Grid construction
# ──────────────────────────────────────────────────────────────────────────────

class TestGridConstruction(unittest.TestCase):
    def test_empty_shape(self):
        g = Grid.empty(10, 20, Cell.AISLE)
        self.assertEqual(g.rows, 10)
        self.assertEqual(g.cols, 20)
        self.assertEqual(g.shape, (10, 20))

    def test_empty_fill(self):
        g = Grid.empty(5, 5, Cell.FLOOR)
        self.assertEqual(g[2, 2], Cell.FLOOR)

    def test_setitem(self):
        g = Grid.empty(5, 5, Cell.AISLE)
        g[2, 3] = Cell.RACK
        self.assertEqual(g[2, 3], Cell.RACK)

    def test_in_bounds(self):
        g = Grid.empty(5, 10)
        self.assertTrue(g.in_bounds(0, 0))
        self.assertTrue(g.in_bounds(4, 9))
        self.assertFalse(g.in_bounds(-1, 0))
        self.assertFalse(g.in_bounds(5, 0))
        self.assertFalse(g.in_bounds(0, 10))

    def test_cells_of_type(self):
        g = small_grid()
        docks = g.cells_of_type(Cell.DOCK)
        self.assertEqual(docks, [(4, 4)])
        racks = g.cells_of_type(Cell.RACK)
        self.assertEqual(len(racks), 3 * 8)

    def test_traversable_cell(self):
        g = small_grid()
        self.assertTrue(g.traversable(0, 0))    # CHARGING
        self.assertTrue(g.traversable(4, 4))    # DOCK
        self.assertFalse(g.traversable(2, 2))   # RACK
        self.assertFalse(g.traversable(10, 10)) # out of bounds


# ──────────────────────────────────────────────────────────────────────────────
# JSON loading
# ──────────────────────────────────────────────────────────────────────────────

class TestJSONLoading(unittest.TestCase):
    def test_load_default_layout(self):
        g = Grid.from_json(LAYOUT)
        self.assertEqual(g.rows, 30)
        self.assertEqual(g.cols, 50)

    def test_default_has_three_docks(self):
        g = Grid.from_json(LAYOUT)
        self.assertEqual(len(g.cells_of_type(Cell.DOCK)), 3)

    def test_default_has_two_charging(self):
        g = Grid.from_json(LAYOUT)
        self.assertEqual(len(g.cells_of_type(Cell.CHARGING)), 2)

    def test_default_has_racks(self):
        g = Grid.from_json(LAYOUT)
        self.assertGreater(len(g.cells_of_type(Cell.RACK)), 0)

    def test_no_dock_raises(self):
        import json, tempfile, os
        data = {"rows": 5, "cols": 5, "zones": [
            {"type": "RACK", "row_start": 1, "row_end": 3,
             "col_start": 1, "col_end": 3}
        ]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            fname = f.name
        try:
            with self.assertRaises(ValueError) as ctx:
                Grid.from_json(fname)
            self.assertIn("DOCK", str(ctx.exception))
        finally:
            os.unlink(fname)

    def test_disconnected_dock_raises(self):
        import json, tempfile, os
        # Full column of racks splits left and right halves
        data = {"rows": 5, "cols": 5, "zones": [
            {"type": "RACK", "row_start": 0, "row_end": 4,
             "col_start": 2, "col_end": 2},
            {"type": "DOCK", "cells": [[0, 0], [0, 4]]}
        ]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            fname = f.name
        try:
            with self.assertRaises(ValueError) as ctx:
                Grid.from_json(fname)
            self.assertIn("connectivity", str(ctx.exception))
        finally:
            os.unlink(fname)


# ──────────────────────────────────────────────────────────────────────────────
# Neighbours
# ──────────────────────────────────────────────────────────────────────────────

class TestNeighbours(unittest.TestCase):
    def test_corner_has_two_neighbours(self):
        g = Grid.empty(5, 5, Cell.AISLE)
        nbs = set(g.traversable_neighbours(0, 0))
        self.assertEqual(nbs, {(0, 1), (1, 0)})

    def test_rack_excluded_from_neighbours(self):
        g = small_grid()
        nbs = list(g.traversable_neighbours(0, 1))
        self.assertNotIn((1, 1), nbs)

    def test_centre_has_four_neighbours(self):
        g = Grid.empty(5, 5, Cell.AISLE)
        nbs = list(g.traversable_neighbours(2, 2))
        self.assertEqual(len(nbs), 4)


# ──────────────────────────────────────────────────────────────────────────────
# A* pathfinding
# ──────────────────────────────────────────────────────────────────────────────

class TestAstar(unittest.TestCase):
    def test_same_cell(self):
        g = small_grid()
        path = g.astar((0, 0), (0, 0))
        self.assertEqual(path, [(0, 0)])

    def test_straight_line(self):
        g = Grid.empty(1, 5, Cell.AISLE)
        g[0, 0] = Cell.DOCK
        g[0, 4] = Cell.DOCK
        path = g.astar((0, 0), (0, 4))
        self.assertIsNotNone(path)
        self.assertEqual(path[0], (0, 0))
        self.assertEqual(path[-1], (0, 4))
        self.assertEqual(len(path), 5)

    def test_path_around_rack(self):
        g = small_grid()
        path = g.astar((0, 0), (0, 9))
        self.assertIsNotNone(path)
        self.assertEqual(path[0], (0, 0))
        self.assertEqual(path[-1], (0, 9))
        for r, c in path:
            self.assertTrue(g.traversable(r, c), f"Non-traversable: ({r},{c})")

    def test_top_to_dock(self):
        g = small_grid()
        path = g.astar((0, 5), (4, 4))
        self.assertIsNotNone(path)
        self.assertEqual(path[-1], (4, 4))
        for r, c in path:
            self.assertTrue(g.traversable(r, c))

    def test_no_path_returns_none(self):
        g = Grid.empty(5, 5, Cell.AISLE)
        # Surround (2,2) with racks
        for pos in [(1, 2), (3, 2), (2, 1), (2, 3)]:
            g[pos] = Cell.RACK
        g[0, 0] = Cell.DOCK
        g[4, 4] = Cell.DOCK
        path = g.astar((0, 0), (2, 2))
        self.assertIsNone(path)

    def test_impassable_start_raises(self):
        g = small_grid()
        with self.assertRaises(ValueError) as ctx:
            g.astar((2, 2), (0, 0))
        self.assertIn("not traversable", str(ctx.exception))

    def test_impassable_goal_raises(self):
        g = small_grid()
        with self.assertRaises(ValueError) as ctx:
            g.astar((0, 0), (2, 5))
        self.assertIn("not traversable", str(ctx.exception))

    def test_blocked_cells_detour(self):
        g = Grid.empty(3, 5, Cell.AISLE)
        g[0, 0] = Cell.DOCK
        g[0, 4] = Cell.DOCK
        blocked = {(0, 1), (0, 2), (0, 3)}
        path = g.astar((0, 0), (0, 4), blocked=blocked)
        self.assertIsNotNone(path)
        self.assertEqual(path[-1], (0, 4))
        for cell in blocked:
            self.assertNotIn(cell, path)

    def test_path_is_connected(self):
        g = small_grid()
        path = g.astar((0, 0), (4, 9))
        self.assertIsNotNone(path)
        for (r1, c1), (r2, c2) in zip(path, path[1:]):
            self.assertEqual(abs(r1 - r2) + abs(c1 - c2), 1,
                             f"Non-adjacent step: ({r1},{c1})->({r2},{c2})")

    def test_astar_on_default_layout(self):
        g = Grid.from_json(LAYOUT)
        path = g.astar((0, 0), (0, 49))
        self.assertIsNotNone(path)
        self.assertEqual(path[0], (0, 0))
        self.assertEqual(path[-1], (0, 49))
        for r, c in path:
            self.assertTrue(g.traversable(r, c))

    def test_dock_to_dock(self):
        g = Grid.from_json(LAYOUT)
        docks = g.cells_of_type(Cell.DOCK)
        path = g.astar(docks[0], docks[-1])
        self.assertIsNotNone(path)
        self.assertEqual(path[0], docks[0])
        self.assertEqual(path[-1], docks[-1])
        for r, c in path:
            self.assertTrue(g.traversable(r, c))

    def test_aisle_preferred_over_floor(self):
        """A* must prefer AISLE (cost 1) over FLOOR (cost 2)."""
        # 3-row grid: row 0 and row 2 are AISLE, row 1 is FLOOR.
        # Straight path along row 0 should be chosen over dipping into row 1.
        g = Grid.empty(3, 7, Cell.AISLE)
        for c in range(7):
            g[1, c] = Cell.FLOOR
        g[0, 0] = Cell.DOCK
        g[0, 6] = Cell.DOCK
        path = g.astar((0, 0), (0, 6))
        self.assertIsNotNone(path)
        for r, c in path:
            self.assertEqual(r, 0, f"Path left AISLE row: ({r},{c})")


# ──────────────────────────────────────────────────────────────────────────────
# Display
# ──────────────────────────────────────────────────────────────────────────────

class TestDisplay(unittest.TestCase):
    def test_str_row_count(self):
        g = small_grid()
        lines = str(g).split("\n")
        self.assertEqual(len(lines), g.rows)

    def test_str_col_count(self):
        g = small_grid()
        for line in str(g).split("\n"):
            self.assertEqual(len(line), g.cols)

    def test_render_path_marks_cells(self):
        g = Grid.empty(1, 5, Cell.AISLE)
        g[0, 0] = Cell.DOCK
        g[0, 4] = Cell.DOCK
        path = g.astar((0, 0), (0, 4))
        rendered = g.render_path(path)
        self.assertIn("*", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
