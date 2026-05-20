"""
Headless smoke tests for foundry.gui.

These tests exercise the WarehouseGUI class without ever opening a real
window by monkey-patching tkinter.Tk and the Canvas/Text/Label widgets
so no display is required.
"""
from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal simulation fixture
# ---------------------------------------------------------------------------

def _make_sim():
    """Return a real (tiny) Simulation so GUI logic runs against live objects."""
    from foundry.grid import Grid
    from foundry.inventory import Inventory, SKU
    from foundry.orders import OrderStream
    from foundry.simulation import Simulation

    layout = {
        "rows": 8, "cols": 10,
        "zones": [
            {"type": "RACK",     "row_start": 1, "row_end": 4, "col_start": 1, "col_end": 3},
            {"type": "DOCK",     "cells": [[7, 5]]},
            {"type": "CHARGING", "cells": [[0, 0]]},
        ],
    }
    import json, tempfile, os
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(layout, f)
        path = f.name
    try:
        grid = Grid.from_json(path)
    finally:
        os.unlink(path)

    skus = [SKU(sku_id=f"SKU{i:03d}") for i in range(5)]
    inventory = Inventory.build(grid, skus, seed=0)
    orders = OrderStream.generate(n_orders=10, skus=[s.sku_id for s in skus],
                                  duration_ticks=100, seed=0)
    return Simulation(grid, inventory, orders, n_agents=2, seed=0)


# ---------------------------------------------------------------------------
# Tkinter stub – keeps the module importable without a display
# ---------------------------------------------------------------------------

def _build_tk_stub():
    """Return a module-level stub that replaces tkinter."""
    stub = types.ModuleType("tkinter")

    class _Var:
        def __init__(self, value=None): self._v = value
        def get(self): return self._v
        def set(self, v): self._v = v

    class _Widget:
        def __init__(self, *_a, **_kw): pass
        def pack(self, **_kw): return self
        def config(self, **_kw): return self
        def pack_propagate(self, *_): return self
        def bind(self, *_a, **_kw): return self
        def protocol(self, *_a): return self
        def after(self, _ms, fn=None, *args):
            if fn is not None:
                fn(*args)
        def destroy(self): pass
        def mainloop(self): pass
        def winfo_width(self): return 400
        def winfo_height(self): return 300
        def delete(self, *_): pass
        def create_rectangle(self, *_a, **_kw): return 1
        def create_oval(self, *_a, **_kw): return 1
        def index(self, _): return "200.0"
        def insert(self, *_): pass
        def see(self, _): pass

    class Tk(_Widget):
        def configure(self, **_): pass
        def resizable(self, *_): pass
        def title(self, _): pass

    class Canvas(_Widget): pass
    class Frame(_Widget): pass
    class Label(_Widget): pass
    class Button(_Widget): pass
    class Scale(_Widget): pass
    class Text(_Widget): pass

    stub.Tk     = Tk
    stub.Canvas = Canvas
    stub.Frame  = Frame
    stub.Label  = Label
    stub.Button = Button
    stub.Scale  = Scale
    stub.Text   = Text
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

    return stub


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWarehouseGUIConstruction(unittest.TestCase):

    def setUp(self):
        self._orig_tk = sys.modules.get("tkinter")
        stub = _build_tk_stub()
        sys.modules["tkinter"] = stub
        # Remove cached gui module so it re-imports with stub
        sys.modules.pop("foundry.gui", None)

    def tearDown(self):
        sys.modules.pop("foundry.gui", None)
        if self._orig_tk is None:
            sys.modules.pop("tkinter", None)
        else:
            sys.modules["tkinter"] = self._orig_tk

    def _make_gui(self, speed=10.0):
        from foundry.gui import WarehouseGUI
        sim = _make_sim()
        return WarehouseGUI(sim, speed=speed)

    def test_construction_does_not_raise(self):
        gui = self._make_gui()
        self.assertIsNotNone(gui)

    def test_speed_stored(self):
        gui = self._make_gui(speed=42.5)
        self.assertAlmostEqual(gui.speed, 42.5)

    def test_initial_state(self):
        gui = self._make_gui()
        self.assertFalse(gui._running)
        self.assertFalse(gui._stopped)

    def test_kpi_vars_keys(self):
        gui = self._make_gui()
        expected = {
            "orders_completed", "orders_per_hour", "lines_per_hour",
            "avg_cycle_time_s", "total_travel_steps", "congestion_events",
            "avg_battery_pct",
        }
        self.assertEqual(set(gui._kpi_vars.keys()), expected)

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

    def test_redraw_does_not_raise(self):
        gui = self._make_gui()
        gui._redraw()   # should not throw

    def test_append_events_empty(self):
        gui = self._make_gui()
        gui._append_events([])  # should not throw

    def test_append_events_with_data(self):
        gui = self._make_gui()
        events = [
            {"type": "ORDER_COMPLETE", "agent": "AMR-00", "tick": 5},
            {"type": "PICKING",        "agent": "AMR-01", "tick": 7},
        ]
        gui._append_events(events)  # should not throw

    def test_cell_px_minimum(self):
        gui = self._make_gui()
        # winfo_width/height return 400/300 from stub
        px = gui._cell_px()
        self.assertGreaterEqual(px, gui.MIN_CELL_PX)
        self.assertLessEqual(px, gui.MAX_CELL_PX)


class TestLaunchFunction(unittest.TestCase):
    """Smoke-test the public launch() entry point (doesn't call mainloop)."""

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

    def test_launch_creates_gui(self):
        """launch() should build a WarehouseGUI without errors."""
        from foundry import gui as gui_mod
        sim = _make_sim()

        created = []
        _orig = gui_mod.WarehouseGUI

        class _PatchedGUI(_orig):
            def run(self):
                created.append(self)   # capture; don't actually run mainloop

        with patch.object(gui_mod, "WarehouseGUI", _PatchedGUI):
            gui_mod.launch(sim, speed=5.0)

        self.assertEqual(len(created), 1)
        self.assertAlmostEqual(created[0].speed, 5.0)


class TestMainGUIBranch(unittest.TestCase):
    """Verify that --gui flag routes to run_with_gui in __main__."""

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

    def test_run_with_gui_called_on_flag(self):
        import argparse
        from foundry import __main__ as m

        sim = _make_sim()
        called_with = []

        # Build a fake gui module and inject it so run_with_gui picks it up
        fake_gui = types.ModuleType("foundry.gui")
        def _fake_launch(s, speed=10.0):
            called_with.append((s, speed))
        fake_gui.launch = _fake_launch
        sys.modules["foundry.gui"] = fake_gui

        args = argparse.Namespace(gui=True, gui_speed=20.0)
        m.run_with_gui(sim, args)

        self.assertEqual(len(called_with), 1)
        self.assertIs(called_with[0][0], sim)
        self.assertAlmostEqual(called_with[0][1], 20.0)


if __name__ == "__main__":
    unittest.main()
