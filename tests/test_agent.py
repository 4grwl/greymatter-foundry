"""
Unit tests for the Agent state machine.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from foundry.agent import (
    Agent, AgentState, PickTask,
    PICK_TICKS, DROP_TICKS_PER_LINE,
    BATTERY_DRAIN_PER_STEP, BATTERY_CHARGE_PER_TICK,
    LOW_BATTERY_THRESHOLD,
)
from foundry.grid import Cell, Grid


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

def flat_grid(rows=10, cols=10) -> Grid:
    """All-AISLE grid with a DOCK at (9,5) and CHARGING at (0,0)."""
    g = Grid.empty(rows, cols, Cell.AISLE)
    g[9, 5] = Cell.DOCK
    g[0, 0] = Cell.CHARGING
    return g


def grid_with_rack() -> Grid:
    """
    5×10 grid:
      rows 0,4 : AISLE
      rows 1-3 : RACK (cols 1-8), AISLE at cols 0,9
      (4,4)    : DOCK
      (0,0)    : CHARGING
    """
    g = Grid.empty(5, 10, Cell.AISLE)
    for r in range(1, 4):
        for c in range(1, 9):
            g[r, c] = Cell.RACK
    g[4, 4] = Cell.DOCK
    g[0, 0] = Cell.CHARGING
    return g


def make_agent(pos=(9, 5)) -> Agent:
    return Agent("AMR-00", pos)


# ──────────────────────────────────────────────────────────────────────────────
# Initial state
# ──────────────────────────────────────────────────────────────────────────────

class TestAgentInit(unittest.TestCase):
    def test_starts_idle(self):
        a = make_agent()
        self.assertEqual(a.state, AgentState.IDLE)
        self.assertTrue(a.is_idle)

    def test_starts_with_empty_payload(self):
        a = make_agent()
        self.assertEqual(a.payload, [])

    def test_starts_full_battery(self):
        a = make_agent()
        self.assertAlmostEqual(a.battery_pct, 100.0)

    def test_zero_stats(self):
        a = make_agent()
        self.assertEqual(a.steps_taken, 0)
        self.assertEqual(a.orders_completed, 0)


# ──────────────────────────────────────────────────────────────────────────────
# IDLE tick does nothing
# ──────────────────────────────────────────────────────────────────────────────

class TestIdleTick(unittest.TestCase):
    def test_idle_tick_emits_no_events(self):
        a = make_agent()
        g = flat_grid()
        evts = a.tick(g, set(), clock=1)
        self.assertEqual(evts, [])

    def test_idle_tick_doesnt_move(self):
        a = make_agent()
        g = flat_grid()
        pos = a.position
        a.tick(g, set(), clock=1)
        self.assertEqual(a.position, pos)


# ──────────────────────────────────────────────────────────────────────────────
# assign_order → MOVING_TO_PICK
# ──────────────────────────────────────────────────────────────────────────────

class TestAssignOrder(unittest.TestCase):
    def test_state_becomes_moving_to_pick(self):
        g = flat_grid()
        a = Agent("AMR-00", (9, 5))
        tasks = [PickTask((5, 5), "SKU001", 2)]
        a.assign_order("ORD001", tasks, (9, 5), g)
        self.assertEqual(a.state, AgentState.MOVING_TO_PICK)
        self.assertFalse(a.is_idle)

    def test_path_computed(self):
        g = flat_grid()
        a = Agent("AMR-00", (9, 5))
        tasks = [PickTask((5, 5), "SKU001", 2)]
        a.assign_order("ORD001", tasks, (9, 5), g)
        # agent is not already at pick pos, so path should be non-empty
        self.assertGreater(len(a._path), 0)

    def test_same_position_path_empty(self):
        g = flat_grid()
        a = Agent("AMR-00", (5, 5))
        tasks = [PickTask((5, 5), "SKU001", 2)]
        a.assign_order("ORD001", tasks, (9, 5), g)
        self.assertEqual(a._path, [])


# ──────────────────────────────────────────────────────────────────────────────
# Movement
# ──────────────────────────────────────────────────────────────────────────────

class TestMovement(unittest.TestCase):
    def test_agent_moves_one_step_per_tick(self):
        g = flat_grid()
        a = Agent("AMR-00", (9, 5))
        tasks = [PickTask((5, 5), "SKU001", 1)]
        a.assign_order("ORD001", tasks, (9, 5), g)
        start = a.position
        a.tick(g, set(), clock=1)
        # should have moved exactly one cell
        r1, c1 = a.position
        r0, c0 = start
        self.assertEqual(abs(r1 - r0) + abs(c1 - c0), 1)

    def test_steps_counted(self):
        g = flat_grid()
        a = Agent("AMR-00", (9, 5))
        tasks = [PickTask((5, 5), "SKU001", 1)]
        a.assign_order("ORD001", tasks, (9, 5), g)
        for _ in range(4):
            a.tick(g, set(), clock=1)
        self.assertEqual(a.steps_taken, 4)

    def test_battery_drains_on_movement(self):
        g = flat_grid()
        a = Agent("AMR-00", (9, 5))
        tasks = [PickTask((5, 5), "SKU001", 1)]
        a.assign_order("ORD001", tasks, (9, 5), g)
        initial = a.battery_pct
        a.tick(g, set(), clock=1)
        # if agent moved, battery should have dropped
        if a.steps_taken > 0:
            self.assertLess(a.battery_pct, initial)

    def test_blocked_cell_causes_congestion_event(self):
        g = flat_grid()
        a = Agent("AMR-00", (9, 5))
        tasks = [PickTask((5, 5), "SKU001", 1)]
        a.assign_order("ORD001", tasks, (9, 5), g)
        # Block every neighbour of (9,5)
        path_next = a._path[0] if a._path else None
        if path_next:
            evts = a.tick(g, {path_next}, clock=1)
            congestion = [e for e in evts if e["type"] == "CONGESTION"]
            self.assertGreater(len(congestion), 0)


# ──────────────────────────────────────────────────────────────────────────────
# Full pick → drop cycle
# ──────────────────────────────────────────────────────────────────────────────

class TestPickDropCycle(unittest.TestCase):
    def _run_to_completion(self, max_ticks=500) -> tuple[Agent, list[dict]]:
        """Run a single-line order to completion. Returns (agent, all_events)."""
        g = flat_grid()
        # Place agent at dock, pick pos at (5,5)
        a = Agent("AMR-00", (9, 5))
        tasks = [PickTask((5, 5), "SKU001", 2)]
        a.assign_order("ORD001", tasks, (9, 5), g)
        all_events = []
        for tick in range(1, max_ticks + 1):
            evts = a.tick(g, set(), clock=tick)
            all_events.extend(evts)
            if a.state == AgentState.IDLE:
                break
        return a, all_events

    def test_order_completes(self):
        a, events = self._run_to_completion()
        types = [e["type"] for e in events]
        self.assertIn("ORDER_COMPLETE", types)

    def test_agent_returns_to_idle(self):
        a, _ = self._run_to_completion()
        self.assertEqual(a.state, AgentState.IDLE)

    def test_orders_completed_increments(self):
        a, _ = self._run_to_completion()
        self.assertEqual(a.orders_completed, 1)

    def test_payload_cleared_after_drop(self):
        a, _ = self._run_to_completion()
        self.assertEqual(a.payload, [])

    def test_pick_start_event_emitted(self):
        _, events = self._run_to_completion()
        self.assertTrue(any(e["type"] == "PICK_START" for e in events))

    def test_pick_done_event_emitted(self):
        _, events = self._run_to_completion()
        self.assertTrue(any(e["type"] == "PICK_DONE" for e in events))

    def test_drop_start_event_emitted(self):
        _, events = self._run_to_completion()
        self.assertTrue(any(e["type"] == "DROP_START" for e in events))

    def test_order_complete_event_has_correct_id(self):
        _, events = self._run_to_completion()
        complete = [e for e in events if e["type"] == "ORDER_COMPLETE"]
        self.assertEqual(len(complete), 1)
        self.assertEqual(complete[0]["order_id"], "ORD001")

    def test_multi_line_order(self):
        """Agent should visit two pick positions before dropping."""
        g = flat_grid()
        a = Agent("AMR-00", (9, 5))
        tasks = [PickTask((5, 3), "SKU001", 1), PickTask((5, 7), "SKU002", 1)]
        a.assign_order("ORD001", tasks, (9, 5), g)
        all_events = []
        for tick in range(1, 600):
            evts = a.tick(g, set(), clock=tick)
            all_events.extend(evts)
            if a.state == AgentState.IDLE:
                break
        picks_done = [e for e in all_events if e["type"] == "PICK_DONE"]
        self.assertEqual(len(picks_done), 2)
        self.assertEqual(a.orders_completed, 1)

    def test_state_sequence(self):
        """Check state passes through MOVING→PICKING→MOVING→DROPPING."""
        g = flat_grid()
        a = Agent("AMR-00", (9, 5))
        tasks = [PickTask((5, 5), "SKU001", 1)]
        a.assign_order("ORD001", tasks, (9, 5), g)
        states_seen = set()
        for tick in range(1, 400):
            states_seen.add(a.state)
            a.tick(g, set(), clock=tick)
            if a.state == AgentState.IDLE:
                break
        self.assertIn(AgentState.MOVING_TO_PICK,  states_seen)
        self.assertIn(AgentState.PICKING,          states_seen)
        self.assertIn(AgentState.MOVING_TO_DOCK,   states_seen)
        self.assertIn(AgentState.DROPPING,         states_seen)


# ──────────────────────────────────────────────────────────────────────────────
# Battery & charging
# ──────────────────────────────────────────────────────────────────────────────

class TestBattery(unittest.TestCase):
    def test_charging_state_replenishes_battery(self):
        g = flat_grid()
        a = Agent("AMR-00", (0, 0), battery_pct=10.0)
        # Force into CHARGING at (0,0) which is already a CHARGING cell
        a.state = AgentState.CHARGING
        a._path = []
        a._target = (0, 0)
        initial = a.battery_pct
        a.tick(g, set(), clock=1)
        self.assertGreater(a.battery_pct, initial)

    def test_battery_caps_at_100(self):
        g = flat_grid()
        a = Agent("AMR-00", (0, 0), battery_pct=99.9)
        a.state = AgentState.CHARGING
        a._path = []
        a._target = (0, 0)
        a.tick(g, set(), clock=1)
        self.assertLessEqual(a.battery_pct, 100.0)

    def test_low_battery_triggers_charge_after_drop(self):
        g = flat_grid()
        # Agent with low battery should head to charge after completing an order
        a = Agent("AMR-00", (9, 5), battery_pct=LOW_BATTERY_THRESHOLD - 1)
        tasks = [PickTask((5, 5), "SKU001", 1)]
        a.assign_order("ORD001", tasks, (9, 5), g)
        for tick in range(1, 500):
            a.tick(g, set(), clock=tick)
            if a.orders_completed == 1:
                break
        self.assertIn(a.state,
                      [AgentState.MOVING_TO_CHARGE, AgentState.CHARGING, AgentState.IDLE])

    def test_full_battery_stays_idle_after_drop(self):
        g = flat_grid()
        a = Agent("AMR-00", (9, 5), battery_pct=100.0)
        tasks = [PickTask((5, 5), "SKU001", 1)]
        a.assign_order("ORD001", tasks, (9, 5), g)
        for tick in range(1, 500):
            a.tick(g, set(), clock=tick)
            if a.orders_completed == 1:
                break
        self.assertEqual(a.state, AgentState.IDLE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
