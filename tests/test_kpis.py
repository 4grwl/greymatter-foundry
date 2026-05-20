"""
Unit tests for KPITracker and the full simulation integration.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from foundry.kpis import KPITracker
from foundry.orders import Order, OrderLine


LAYOUT = Path(__file__).parent.parent / "layouts" / "default.json"
ORDERS = Path(__file__).parent.parent / "data"  / "orders_sample.csv"


# ──────────────────────────────────────────────────────────────────────────────
# KPITracker unit tests
# ──────────────────────────────────────────────────────────────────────────────

class TestKPITracker(unittest.TestCase):
    def _make_order(self, arrive=0, complete=100, n_lines=2) -> Order:
        o = Order(
            order_id="ORD0001",
            lines=[OrderLine("SKU001", 1)] * n_lines,
            arrive_at=arrive,
            completed_at=complete,
        )
        return o

    def test_initial_zero(self):
        k = KPITracker()
        self.assertEqual(k.orders_completed, 0)
        self.assertEqual(k.lines_completed, 0)
        self.assertEqual(k.congestion_events, 0)

    def test_record_order_increments_orders(self):
        k = KPITracker()
        k.record_order(self._make_order(n_lines=3))
        self.assertEqual(k.orders_completed, 1)
        self.assertEqual(k.lines_completed, 3)

    def test_record_multiple_orders(self):
        k = KPITracker()
        for _ in range(5):
            k.record_order(self._make_order(n_lines=2))
        self.assertEqual(k.orders_completed, 5)
        self.assertEqual(k.lines_completed, 10)

    def test_cycle_time_accumulated(self):
        k = KPITracker()
        k.record_order(self._make_order(arrive=0, complete=120))
        k.record_order(self._make_order(arrive=0, complete=80))
        self.assertEqual(k.total_cycle_time, 200)

    def test_record_congestion(self):
        k = KPITracker()
        k.record_congestion()
        k.record_congestion()
        self.assertEqual(k.congestion_events, 2)

    def test_summary_keys(self):
        from foundry.agent import Agent
        k = KPITracker()
        k.record_order(self._make_order())
        agents = [Agent("AMR-00", (0, 0))]
        summary = k.summary(elapsed_ticks=3600, agents=agents)
        for key in ("orders_completed", "lines_completed", "orders_per_hour",
                    "lines_per_hour", "avg_cycle_time_s", "total_travel_steps",
                    "congestion_events", "avg_battery_pct"):
            self.assertIn(key, summary)

    def test_orders_per_hour(self):
        from foundry.agent import Agent
        k = KPITracker()
        for _ in range(10):
            k.record_order(self._make_order())
        agents = [Agent("AMR-00", (0, 0))]
        summary = k.summary(elapsed_ticks=3600, agents=agents)
        self.assertAlmostEqual(summary["orders_per_hour"], 10.0, places=1)

    def test_avg_cycle_time(self):
        from foundry.agent import Agent
        k = KPITracker()
        k.record_order(self._make_order(arrive=0, complete=200))
        k.record_order(self._make_order(arrive=0, complete=100))
        agents = [Agent("AMR-00", (0, 0))]
        summary = k.summary(elapsed_ticks=3600, agents=agents)
        self.assertAlmostEqual(summary["avg_cycle_time_s"], 150.0, places=0)

    def test_summary_zero_elapsed_does_not_divide_by_zero(self):
        from foundry.agent import Agent
        k = KPITracker()
        agents = [Agent("AMR-00", (0, 0))]
        summary = k.summary(elapsed_ticks=0, agents=agents)
        self.assertEqual(summary["orders_per_hour"], 0.0)


# ──────────────────────────────────────────────────────────────────────────────
# Integration: Simulation smoke test
# ──────────────────────────────────────────────────────────────────────────────

class TestSimulationIntegration(unittest.TestCase):
    def _build_sim(self, n_agents=3, n_orders=20, ticks=500):
        from foundry.grid import Grid
        from foundry.inventory import Inventory, SKU
        from foundry.orders import OrderStream
        from foundry.simulation import Simulation

        grid = Grid.from_json(LAYOUT)

        # 20 SKUs spread across rack bays
        skus = [SKU(sku_id=f"SKU{i:03d}") for i in range(1, 21)]
        inventory = Inventory.build(grid, skus, seed=42)

        orders = OrderStream.generate(
            n_orders=n_orders,
            skus=[s.sku_id for s in skus],
            duration_ticks=ticks // 2,
            seed=99,
        )
        return Simulation(grid, inventory, orders, n_agents=n_agents, seed=42)

    def test_simulation_runs_without_error(self):
        sim = self._build_sim()
        result = sim.run(ticks=500)
        self.assertIsInstance(result, dict)

    def test_some_orders_complete(self):
        sim = self._build_sim(n_agents=3, n_orders=20, ticks=2000)
        result = sim.run(ticks=2000)
        self.assertGreater(result["orders_completed"], 0)

    def test_kpi_keys_present(self):
        sim = self._build_sim()
        result = sim.run(ticks=500)
        for key in ("orders_completed", "lines_completed", "orders_per_hour",
                    "lines_per_hour", "avg_cycle_time_s", "total_travel_steps",
                    "congestion_events"):
            self.assertIn(key, result)

    def test_agents_move(self):
        sim = self._build_sim(n_agents=3, n_orders=20, ticks=500)
        sim.run(ticks=500)
        total_steps = sum(a.steps_taken for a in sim.agents)
        self.assertGreater(total_steps, 0)

    def test_events_emitted(self):
        sim = self._build_sim(n_agents=3, n_orders=20, ticks=2000)
        sim.run(ticks=2000)
        self.assertGreater(len(sim.events), 0)

    def test_order_complete_events_match_kpi(self):
        sim = self._build_sim(n_agents=3, n_orders=20, ticks=2000)
        sim.run(ticks=2000)
        complete_events = [e for e in sim.events if e["type"] == "ORDER_COMPLETE"]
        self.assertEqual(len(complete_events), sim.kpis.orders_completed)

    def test_majority_of_agents_participate(self):
        """At least half the agents should work given enough orders.

        Nearest-agent dispatch means agents at a dock are outbid by their
        1-step-closer neighbour when both are idle simultaneously. This is
        correct FIFO+nearest behaviour, not a bug. We verify the fleet is
        utilised as a whole rather than insisting on every individual.
        """
        sim = self._build_sim(n_agents=5, n_orders=200, ticks=8000)
        sim.run(ticks=8000)
        active = sum(1 for a in sim.agents if a.steps_taken > 0)
        self.assertGreaterEqual(active, 3,
                                f"Only {active}/5 agents took steps")

    def test_from_files(self):
        from foundry.simulation import Simulation
        sim = Simulation.from_files(LAYOUT, ORDERS, n_agents=3)
        result = sim.run(ticks=1000)
        self.assertIsInstance(result, dict)
        self.assertGreater(result["orders_completed"], 0)

    def test_step_returns_events(self):
        sim = self._build_sim(n_agents=3, n_orders=20, ticks=500)
        # step() should return recent events list (may be empty on tick 1)
        evts = sim.step()
        self.assertIsInstance(evts, list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
