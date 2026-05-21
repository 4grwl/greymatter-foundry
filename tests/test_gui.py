"""
Headless smoke tests for foundry.gui.

All tests use a tkinter stub so no display is required.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal real simulation fixture
# ---------------------------------------------------------------------------

def _make_layout_file() -> str:
    layout = {
        "rows": 8, "cols": 10,
        "zones": [
            {"type": "RACK",     "row_start": 1, "row_end": 4, "col_start": 1, "col_end": 3},
            {"type": "DOCK",     "cells": [[7, 5]]},
            {"type": "CHARGING", "cells": [[0, 0]]},
        ],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(layout, f)
        return f.name


def _make_sim(layout_path: str | None = None):
    from foundry.grid import Grid
    from foundry.inventory import Inventory, SKU
    from foundry.orders import OrderStream
    from foundry.simulation import Simulation

    path = layout_path or _make_layout_file()
    try:
        grid = Grid.from_json(path)
    finally:
        if layout_path is None:
            os.unlink(path)

    skus      = [SKU(sku_id=f"SKU{i:03d}") for i in range(5)]
    inventory = Inventory.build(grid, skus, seed=0)
    orders    = OrderStream.generate(
        n_orders=10, skus=[s.sku_id for s in skus],
        duration_ticks=100, seed=0,
    )
    return Simulation(grid, inventory, orders, n_agents=2, seed=0)


def _make_args(layout_path: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        layout=layout_path or "layouts/default.json",
        orders=None,
        agents=2,
        seed=0,
        ticks=100,
        n_skus=5,
        n_orders=10,
        slotter=None,
        dispatch=None,
        sequencer=None,
        quiet=True,
    )


# ---------------------------------------------------------------------------
# Tkinter stub
# ---------------------------------------------------------------------------

def _build_tk_stub():
    stub = types.ModuleType("tkinter")

    class _Var:
        def __init__(self, value=None): self._v = value
        def get(self): return self._v
        def set(self, v): self._v = v

    class _Widget:
        def __init__(self, *_a, **_kw): pass
        def pack(self, **_kw): return self
        def config(self, **_kw): return self
        def configure(self, **_kw): return self
        def pack_propagate(self, *_): return self
        def bind(self, *_a, **_kw): return self
        def protocol(self, *_a): return self
        def after(self, ms, fn=None, *args):
            # Only execute immediately for delay=0 (UI update callbacks).
            # Positive delays (e.g. the 4 s status auto-clear) are no-ops
            # in the stub so timed state changes don't race in tests.
            if fn is not None and ms == 0:
                fn(*args)
        def destroy(self): pass
        def mainloop(self): pass
        def winfo_width(self): return 400
        def winfo_height(self): return 300
        def winfo_children(self): return []
        def delete(self, *_): pass
        def create_rectangle(self, *_a, **_kw): return 1
        def create_oval(self, *_a, **_kw): return 1
        def index(self, _): return "200.0"
        def insert(self, *_): pass
        def see(self, _): pass
        def resizable(self, *_): pass
        def title(self, _): pass

    class Tk(_Widget): pass
    class Canvas(_Widget): pass
    class Frame(_Widget): pass
    class Label(_Widget): pass
    class Button(_Widget): pass
    class Scale(_Widget): pass
    class Text(_Widget): pass
    class Checkbutton(_Widget): pass

    stub.Tk         = Tk
    stub.Canvas     = Canvas
    stub.Frame      = Frame
    stub.Label      = Label
    stub.Button     = Button
    stub.Scale      = Scale
    stub.Text       = Text
    stub.Checkbutton = Checkbutton
    stub.DoubleVar  = _Var
    stub.StringVar  = _Var
    stub.BooleanVar = _Var

    for c in ("TOP", "BOTTOM", "LEFT", "RIGHT", "BOTH", "X", "Y",
              "W", "E", "N", "S", "END", "NORMAL", "DISABLED",
              "FLAT", "HORIZONTAL", "WORD"):
        setattr(stub, c, c)

    font_stub = types.ModuleType("tkinter.font")
    class _Font:
        def __init__(self, **_kw): pass
    font_stub.Font = _Font
    stub.font = font_stub
    sys.modules["tkinter.font"] = font_stub

    # filedialog stub
    fd_stub = types.ModuleType("tkinter.filedialog")
    fd_stub.askopenfilename = lambda **_kw: ""
    stub.filedialog = fd_stub
    sys.modules["tkinter.filedialog"] = fd_stub

    return stub


# ---------------------------------------------------------------------------
# Base test class — installs / tears down the tkinter stub
# ---------------------------------------------------------------------------

class _GUITestBase(unittest.TestCase):
    def setUp(self):
        self._orig_tk = sys.modules.get("tkinter")
        stub = _build_tk_stub()
        sys.modules["tkinter"] = stub
        sys.modules.pop("foundry.gui", None)

    def tearDown(self):
        sys.modules.pop("foundry.gui", None)
        if self._orig_tk is None:
            sys.modules.pop("tkinter", None)
        else:
            sys.modules["tkinter"] = self._orig_tk

    def _make_gui(self, speed=10.0, args=None):
        from foundry.gui import WarehouseGUI
        sim = _make_sim()
        return WarehouseGUI(sim, speed=speed, args=args)


# ---------------------------------------------------------------------------
# Construction & basic state
# ---------------------------------------------------------------------------

class TestWarehouseGUIConstruction(_GUITestBase):

    def test_construction_does_not_raise(self):
        self.assertIsNotNone(self._make_gui())

    def test_speed_stored(self):
        gui = self._make_gui(speed=42.5)
        self.assertAlmostEqual(gui.speed, 42.5)

    def test_initial_state(self):
        gui = self._make_gui()
        self.assertFalse(gui._running)
        self.assertFalse(gui._stopped)

    def test_args_stored(self):
        args = _make_args()
        gui  = self._make_gui(args=args)
        self.assertIs(gui._args, args)

    def test_kpi_vars_keys(self):
        gui = self._make_gui()
        expected = {
            "orders_completed", "orders_per_hour", "lines_per_hour",
            "avg_cycle_time_s", "total_travel_steps", "congestion_events",
            "avg_battery_pct",
        }
        self.assertEqual(set(gui._kpi_vars.keys()), expected)

    def test_optimal_overlay_populated_after_init(self):
        """ABC overlay should be non-empty after construction (grid has racks)."""
        gui = self._make_gui()
        self.assertGreater(len(gui._optimal_overlay), 0)

    def test_optimal_overlay_values_are_valid_colours(self):
        gui = self._make_gui()
        from foundry.gui import ABC_COLOUR
        valid = set(ABC_COLOUR.values())
        for colour in gui._optimal_overlay.values():
            self.assertIn(colour, valid)

    def test_show_optimal_starts_false(self):
        gui = self._make_gui()
        self.assertFalse(gui._show_optimal)


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

class TestControls(_GUITestBase):

    def test_on_play_sets_running(self):
        gui = self._make_gui()
        gui._on_play()
        self.assertTrue(gui._running)

    def test_on_pause_clears_running(self):
        gui = self._make_gui()
        gui._on_play()
        gui._on_pause()
        self.assertFalse(gui._running)

    def test_on_stop_sets_stopped(self):
        gui = self._make_gui()
        gui._on_play()
        gui._on_stop()
        self.assertFalse(gui._running)
        self.assertTrue(gui._stopped)


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

class TestDrawing(_GUITestBase):

    def test_redraw_normal_mode_does_not_raise(self):
        gui = self._make_gui()
        gui._redraw()

    def test_redraw_optimal_mode_does_not_raise(self):
        gui = self._make_gui()
        gui._show_optimal = True
        gui._redraw()

    def test_cell_px_within_bounds(self):
        gui = self._make_gui()
        px = gui._cell_px()
        self.assertGreaterEqual(px, gui.MIN_CELL_PX)
        self.assertLessEqual(px, gui.MAX_CELL_PX)

    def test_append_events_empty(self):
        gui = self._make_gui()
        gui._append_events([])

    def test_append_events_with_data(self):
        gui = self._make_gui()
        gui._append_events([
            {"type": "ORDER_COMPLETE", "agent": "AMR-00", "tick": 5},
            {"type": "PICKING",        "agent": "AMR-01", "tick": 7},
        ])


# ---------------------------------------------------------------------------
# Optimal layout overlay
# ---------------------------------------------------------------------------

class TestOptimalOverlay(_GUITestBase):

    def test_overlay_covers_all_accessible_racks(self):
        """Every accessible rack cell should appear in the overlay."""
        from foundry.grid import Cell
        from foundry.inventory import _find_pick_pos

        gui  = self._make_gui()
        grid = gui.sim.grid
        accessible = {
            pos for pos in grid.cells_of_type(Cell.RACK)
            if _find_pick_pos(grid, pos) is not None
        }
        self.assertEqual(set(gui._optimal_overlay.keys()), accessible)

    def test_nearest_cells_are_a_class(self):
        """The cell closest to the dock must be coloured A-class."""
        from foundry.grid import Cell
        from foundry.gui import ABC_COLOUR

        gui   = self._make_gui()
        grid  = gui.sim.grid
        docks = grid.cells_of_type(Cell.DOCK)

        def dock_dist(pos):
            return min(abs(pos[0] - d[0]) + abs(pos[1] - d[1]) for d in docks)

        nearest = min(gui._optimal_overlay.keys(), key=dock_dist)
        self.assertEqual(gui._optimal_overlay[nearest], ABC_COLOUR["A"])

    def test_farthest_cells_are_c_class(self):
        """The cell farthest from the dock must be coloured C-class."""
        from foundry.grid import Cell
        from foundry.gui import ABC_COLOUR

        gui   = self._make_gui()
        grid  = gui.sim.grid
        docks = grid.cells_of_type(Cell.DOCK)

        def dock_dist(pos):
            return min(abs(pos[0] - d[0]) + abs(pos[1] - d[1]) for d in docks)

        farthest = max(gui._optimal_overlay.keys(), key=dock_dist)
        self.assertEqual(gui._optimal_overlay[farthest], ABC_COLOUR["C"])

    def test_toggle_flips_show_optimal(self):
        gui = self._make_gui()
        self.assertFalse(gui._show_optimal)
        gui._optimal_var.set(True)
        gui._on_toggle_optimal()
        self.assertTrue(gui._show_optimal)
        gui._optimal_var.set(False)
        gui._on_toggle_optimal()
        self.assertFalse(gui._show_optimal)

    def test_toggle_redraw_does_not_raise(self):
        gui = self._make_gui()
        gui._optimal_var.set(True)
        gui._on_toggle_optimal()
        gui._redraw()
        gui._optimal_var.set(False)
        gui._on_toggle_optimal()
        gui._redraw()


# ---------------------------------------------------------------------------
# Config panel / file loading
# ---------------------------------------------------------------------------

class TestConfigPanel(_GUITestBase):

    def test_load_layout_updates_var_on_valid_path(self):
        layout_path = _make_layout_file()
        try:
            gui = self._make_gui()
            # Patch filedialog to return our layout file
            sys.modules["tkinter.filedialog"].askopenfilename = lambda **_: layout_path
            gui._load_layout()
            self.assertEqual(gui._args.layout, layout_path)
            import os
            self.assertEqual(gui._layout_var.get(), os.path.basename(layout_path))
        finally:
            os.unlink(layout_path)

    def test_load_layout_no_op_on_cancel(self):
        gui = self._make_gui(args=_make_args())
        original = gui._args.layout
        sys.modules["tkinter.filedialog"].askopenfilename = lambda **_: ""
        gui._load_layout()
        self.assertEqual(gui._args.layout, original)

    def test_load_orders_updates_var(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(b"order_id,sku_id,qty,arrive_at\n")
            csv_path = f.name
        try:
            gui = self._make_gui()
            sys.modules["tkinter.filedialog"].askopenfilename = lambda **_: csv_path
            gui._load_orders()
            self.assertEqual(gui._args.orders, csv_path)
        finally:
            os.unlink(csv_path)

    def test_load_orders_no_op_on_cancel(self):
        gui = self._make_gui(args=_make_args())
        sys.modules["tkinter.filedialog"].askopenfilename = lambda **_: ""
        gui._load_orders()
        self.assertIsNone(gui._args.orders)   # unchanged


# ---------------------------------------------------------------------------
# Rebuild simulation
# ---------------------------------------------------------------------------

class TestRebuildSimulation(_GUITestBase):

    def test_rebuild_replaces_sim(self):
        layout_path = _make_layout_file()
        try:
            gui = self._make_gui(args=_make_args(layout_path))
            old_sim = gui.sim
            gui._rebuild_simulation()
            # A new Simulation object must have been installed
            self.assertIsNot(gui.sim, old_sim)
        finally:
            os.unlink(layout_path)

    def test_rebuild_recomputes_overlay(self):
        layout_path = _make_layout_file()
        try:
            gui = self._make_gui(args=_make_args(layout_path))
            gui._optimal_overlay = {}   # clear it
            gui._rebuild_simulation()
            self.assertGreater(len(gui._optimal_overlay), 0)
        finally:
            os.unlink(layout_path)

    def test_rebuild_without_args_sets_error_status(self):
        gui = self._make_gui(args=None)
        gui._rebuild_simulation()
        # Status message should be set (non-empty)
        self.assertNotEqual(gui._status_var.get(), "")

    def test_rebuild_preserves_running_state(self):
        layout_path = _make_layout_file()
        try:
            gui = self._make_gui(args=_make_args(layout_path))
            gui._running = True
            gui._rebuild_simulation()
            self.assertTrue(gui._running)
        finally:
            os.unlink(layout_path)


# ---------------------------------------------------------------------------
# launch() entry point
# ---------------------------------------------------------------------------

class TestLaunchFunction(_GUITestBase):

    def test_launch_passes_args(self):
        from foundry import gui as gui_mod
        sim  = _make_sim()
        args = _make_args()

        created = []
        _orig = gui_mod.WarehouseGUI

        class _Patched(_orig):
            def run(self):
                created.append(self)

        with patch.object(gui_mod, "WarehouseGUI", _Patched):
            gui_mod.launch(sim, speed=5.0, args=args)

        self.assertEqual(len(created), 1)
        self.assertAlmostEqual(created[0].speed, 5.0)
        self.assertIs(created[0]._args, args)


# ---------------------------------------------------------------------------
# CLI routing
# ---------------------------------------------------------------------------

class TestMainGUIBranch(unittest.TestCase):

    def setUp(self):
        self._orig_tk = sys.modules.get("tkinter")
        stub = _build_tk_stub()
        sys.modules["tkinter"] = stub
        sys.modules.pop("foundry.gui", None)

    def tearDown(self):
        sys.modules.pop("foundry.gui", None)
        if self._orig_tk is None:
            sys.modules.pop("tkinter", None)
        else:
            sys.modules["tkinter"] = self._orig_tk

    def test_run_with_gui_passes_args(self):
        from foundry import __main__ as m

        sim        = _make_sim()
        called     = []
        fake_gui   = types.ModuleType("foundry.gui")

        def _fake_launch(s, speed=10.0, args=None):
            called.append({"sim": s, "speed": speed, "args": args})

        fake_gui.launch = _fake_launch
        sys.modules["foundry.gui"] = fake_gui

        ns = argparse.Namespace(gui=True, gui_speed=20.0)
        m.run_with_gui(sim, ns)

        self.assertEqual(len(called), 1)
        self.assertIs(called[0]["sim"], sim)
        self.assertAlmostEqual(called[0]["speed"], 20.0)
        self.assertIs(called[0]["args"], ns)


if __name__ == "__main__":
    unittest.main()
