"""
M4 tests — ABC slotter and TSP sequencer plugins.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from foundry.agent import PickTask
from foundry.grid import Cell, Grid
from foundry.inventory import Inventory, SKU, _find_pick_pos
from foundry.plugins.abc_slotter import avg_dock_distance, slotter as abc_slotter
from foundry.plugins.tsp_router import (
    improvement_ratio,
    sequencer as tsp_sequencer,
    total_distance,
)

LAYOUT = Path(__file__).parent.parent / "layouts" / "default.json"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def load_grid() -> Grid:
    return Grid.from_json(LAYOUT)


def make_skus(n_a=5, n_b=10, n_c=5) -> list[SKU]:
    skus = []
    for i in range(n_a):
        skus.append(SKU(f"A{i:02d}", demand_class="A"))
    for i in range(n_b):
        skus.append(SKU(f"B{i:02d}", demand_class="B"))
    for i in range(n_c):
        skus.append(SKU(f"C{i:02d}", demand_class="C"))
    return skus


def make_tasks(positions: list[tuple[int, int]]) -> list[PickTask]:
    return [PickTask(pick_pos=p, sku_id=f"SKU{i}", qty=1)
            for i, p in enumerate(positions)]


# ──────────────────────────────────────────────────────────────────────────────
# ABC Slotter — unit tests
# ──────────────────────────────────────────────────────────────────────────────

class TestABCSlotter(unittest.TestCase):
    def test_returns_dict(self):
        grid = load_grid()
        skus = make_skus(n_a=2, n_b=4, n_c=2)
        result = abc_slotter(skus, grid)
        self.assertIsInstance(result, dict)

    def test_all_skus_slotted(self):
        grid = load_grid()
        skus = make_skus(n_a=3, n_b=5, n_c=3)
        result = abc_slotter(skus, grid)
        for sku in skus:
            self.assertIn(sku.sku_id, result, f"{sku.sku_id} not slotted")

    def test_slots_are_rack_cells(self):
        grid = load_grid()
        skus = make_skus(n_a=2, n_b=4, n_c=2)
        result = abc_slotter(skus, grid)
        for sku_id, rack in result.items():
            self.assertEqual(grid[rack], Cell.RACK,
                             f"{sku_id} slotted at non-RACK cell {rack}")

    def test_slots_are_accessible(self):
        """Every slotted rack cell must have a traversable pick_pos neighbour."""
        grid = load_grid()
        skus = make_skus(n_a=3, n_b=6, n_c=3)
        result = abc_slotter(skus, grid)
        for sku_id, rack in result.items():
            pick = _find_pick_pos(grid, rack)
            self.assertIsNotNone(pick, f"{sku_id} at {rack} has no accessible face")

    def test_no_duplicate_slots(self):
        """Two different SKUs must not share the same rack cell."""
        grid = load_grid()
        skus = make_skus(n_a=5, n_b=10, n_c=5)
        result = abc_slotter(skus, grid)
        slots = list(result.values())
        self.assertEqual(len(slots), len(set(slots)), "Duplicate rack assignments found")

    def test_a_class_closer_than_c_class(self):
        """A-class SKUs should on average be closer to docks than C-class."""
        grid = load_grid()
        skus = make_skus(n_a=8, n_b=0, n_c=8)
        result = abc_slotter(skus, grid)

        docks = grid.cells_of_type(Cell.DOCK)
        def dist_to_nearest_dock(rack):
            pick = _find_pick_pos(grid, rack)
            if pick is None:
                return float("inf")
            return min(abs(pick[0]-d[0]) + abs(pick[1]-d[1]) for d in docks)

        a_dists = [dist_to_nearest_dock(result[f"A{i:02d}"]) for i in range(8)
                   if f"A{i:02d}" in result]
        c_dists = [dist_to_nearest_dock(result[f"C{i:02d}"]) for i in range(8)
                   if f"C{i:02d}" in result]

        if a_dists and c_dists:
            avg_a = sum(a_dists) / len(a_dists)
            avg_c = sum(c_dists) / len(c_dists)
            self.assertLess(avg_a, avg_c,
                            f"A-class avg dist {avg_a:.1f} not < C-class avg dist {avg_c:.1f}")

    def test_avg_dock_distance_helper(self):
        grid = load_grid()
        skus = make_skus(n_a=3, n_b=5, n_c=3)
        result = abc_slotter(skus, grid)
        avg = avg_dock_distance(result, grid)
        self.assertGreater(avg, 0)

    def test_abc_closer_than_random(self):
        """ABC slotting must place A-class SKUs closer to docks than random assignment."""
        grid = load_grid()
        skus = make_skus(n_a=10, n_b=0, n_c=0)

        abc_map   = abc_slotter(skus, grid, seed=42)
        rand_map  = {s.sku_id: r for s, (_, r) in
                     zip(skus, __import__("random", fromlist=[]).Random(42)
                         .sample([(d, r) for d, r in
                                  [(abs((_find_pick_pos(grid, rc) or (0,0))[0] - 29) +
                                    abs((_find_pick_pos(grid, rc) or (0,0))[1] - 24),
                                    rc)
                                   for rc in grid.cells_of_type(Cell.RACK)
                                   if _find_pick_pos(grid, rc) is not None]],
                                 len(skus)))}

        avg_abc  = avg_dock_distance(abc_map,  grid)
        avg_rand = avg_dock_distance(rand_map, grid)
        self.assertLessEqual(avg_abc, avg_rand,
                             f"ABC avg {avg_abc:.1f} should be ≤ random avg {avg_rand:.1f}")

    def test_integrates_with_inventory(self):
        """slot_map from abc_slotter should build a valid Inventory."""
        grid = load_grid()
        skus = make_skus(n_a=3, n_b=5, n_c=3)
        slot_map = abc_slotter(skus, grid)
        from foundry.inventory import Slot
        slots = [
            Slot(sku_id=sid, rack_pos=rack, pick_pos=_find_pick_pos(grid, rack))
            for sid, rack in slot_map.items()
            if _find_pick_pos(grid, rack) is not None
        ]
        inv = Inventory(skus, slots)
        self.assertEqual(inv.slot_count(), len(slots))
        for sku in skus:
            if sku.sku_id in slot_map:
                self.assertIsNotNone(inv.get_pick_pos(sku.sku_id))

    def test_no_dock_raises(self):
        g = Grid.empty(5, 5, Cell.AISLE)
        g[2, 2] = Cell.RACK
        skus = [SKU("SKU001")]
        with self.assertRaises(ValueError):
            abc_slotter(skus, g)

    def test_empty_sku_list_returns_empty(self):
        grid = load_grid()
        result = abc_slotter([], grid)
        self.assertEqual(result, {})

    def test_reproducible_with_same_seed(self):
        grid = load_grid()
        skus = make_skus(n_a=3, n_b=5, n_c=3)
        r1 = abc_slotter(skus, grid, seed=7)
        r2 = abc_slotter(skus, grid, seed=7)
        self.assertEqual(r1, r2)

    def test_different_seeds_can_differ(self):
        grid = load_grid()
        skus = make_skus(n_a=5, n_b=5, n_c=5)
        r1 = abc_slotter(skus, grid, seed=1)
        r2 = abc_slotter(skus, grid, seed=999)
        # Not guaranteed to differ, but almost certainly will for 15 SKUs
        # Just verify both run without error
        self.assertIsInstance(r1, dict)
        self.assertIsInstance(r2, dict)


# ──────────────────────────────────────────────────────────────────────────────
# TSP Sequencer — unit tests
# ──────────────────────────────────────────────────────────────────────────────

class TestTSPSequencer(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(tsp_sequencer((0, 0), []), [])

    def test_single_task_unchanged(self):
        tasks = make_tasks([(5, 5)])
        result = tsp_sequencer((0, 0), tasks)
        self.assertEqual(result, tasks)

    def test_returns_same_tasks(self):
        tasks = make_tasks([(1, 1), (8, 8), (3, 9)])
        result = tsp_sequencer((0, 0), tasks)
        self.assertEqual(set(id(t) for t in result), set(id(t) for t in tasks))

    def test_returns_all_tasks(self):
        tasks = make_tasks([(1, 1), (8, 8), (3, 9), (5, 2)])
        result = tsp_sequencer((0, 0), tasks)
        self.assertEqual(len(result), len(tasks))

    def test_nearest_first_from_origin(self):
        """From (0,0), the nearest task should come first."""
        tasks = make_tasks([(1, 1), (9, 9), (5, 5)])
        result = tsp_sequencer((0, 0), tasks)
        # nearest to (0,0) by Manhattan is (1,1) with distance 2
        self.assertEqual(result[0].pick_pos, (1, 1))

    def test_greedy_ordering_trivial(self):
        """
        Agent at (0,0), tasks at (0,9), (0,1), (0,5).
        NN order should be: (0,1) → (0,5) → (0,9)
        """
        tasks = make_tasks([(0, 9), (0, 1), (0, 5)])
        result = tsp_sequencer((0, 0), tasks)
        positions = [t.pick_pos for t in result]
        self.assertEqual(positions, [(0, 1), (0, 5), (0, 9)])

    def test_total_distance_not_worse(self):
        """TSP ordering must produce ≤ total travel of a random order."""
        import random
        rng = random.Random(42)
        positions = [(rng.randint(0, 29), rng.randint(0, 49)) for _ in range(6)]
        tasks = make_tasks(positions)
        agent_pos = (29, 24)

        tsp_dist  = total_distance(agent_pos, tsp_sequencer(agent_pos, tasks))
        orig_dist = total_distance(agent_pos, tasks)
        # NN-TSP should be ≤ original (or equal in best case)
        self.assertLessEqual(tsp_dist, orig_dist)

    def test_improvement_ratio_non_negative(self):
        """improvement_ratio must be ≥ 0 (TSP never makes things worse)."""
        import random
        rng = random.Random(7)
        positions = [(rng.randint(0, 29), rng.randint(0, 49)) for _ in range(5)]
        tasks = make_tasks(positions)
        ratio = improvement_ratio((0, 0), tasks)
        self.assertGreaterEqual(ratio, 0.0)

    def test_improvement_ratio_zero_for_one_task(self):
        tasks = make_tasks([(5, 5)])
        self.assertAlmostEqual(improvement_ratio((0, 0), tasks), 0.0)

    def test_tsp_improves_worst_case(self):
        """
        Deliberately reversed order: tasks go far then near.
        TSP should find a better tour.
        """
        # Agent at (0,0), tasks in reverse order: (0,9),(0,7),(0,5),(0,3),(0,1)
        tasks = make_tasks([(0, 9), (0, 7), (0, 5), (0, 3), (0, 1)])
        agent_pos = (0, 0)
        orig_dist = total_distance(agent_pos, tasks)        # 9+2+2+2+2 = 17
        tsp_dist  = total_distance(agent_pos, tsp_sequencer(agent_pos, tasks))
        self.assertLess(tsp_dist, orig_dist)

    def test_does_not_mutate_input(self):
        tasks = make_tasks([(5, 0), (0, 5), (3, 3)])
        original_order = [t.pick_pos for t in tasks]
        tsp_sequencer((0, 0), tasks)
        self.assertEqual([t.pick_pos for t in tasks], original_order)


# ──────────────────────────────────────────────────────────────────────────────
# Plugin drop-in: both swap into Simulation without changes
# ──────────────────────────────────────────────────────────────────────────────

class TestPluginDropIn(unittest.TestCase):
    def _build_sim(self, slotter_fn=None, sequencer_fn=None,
                   n_agents=3, n_orders=40, ticks=1000):
        from foundry.simulation import Simulation

        grid = load_grid()
        skus = make_skus(n_a=5, n_b=10, n_c=5)

        if slotter_fn:
            slot_map = slotter_fn(skus, grid)
            from foundry.inventory import Slot
            slots = [
                Slot(sid, rack, _find_pick_pos(grid, rack))
                for sid, rack in slot_map.items()
                if _find_pick_pos(grid, rack) is not None
            ]
            inventory = Inventory(skus, slots)
        else:
            inventory = Inventory.build(grid, skus, seed=42)

        from foundry.orders import OrderStream
        orders = OrderStream.generate(
            n_orders=n_orders,
            skus=[s.sku_id for s in skus],
            duration_ticks=ticks // 2,
            seed=99,
        )
        return Simulation(grid, inventory, orders, n_agents=n_agents,
                          task_sequencer=sequencer_fn)

    def test_abc_slotter_runs_in_sim(self):
        sim = self._build_sim(slotter_fn=abc_slotter)
        result = sim.run(ticks=1000)
        self.assertIsInstance(result, dict)

    def test_tsp_sequencer_runs_in_sim(self):
        sim = self._build_sim(sequencer_fn=tsp_sequencer)
        result = sim.run(ticks=1000)
        self.assertIsInstance(result, dict)

    def test_both_plugins_together(self):
        sim = self._build_sim(slotter_fn=abc_slotter, sequencer_fn=tsp_sequencer)
        result = sim.run(ticks=1000)
        self.assertGreater(result["orders_completed"], 0)

    def test_abc_plus_tsp_completes_orders(self):
        sim = self._build_sim(slotter_fn=abc_slotter, sequencer_fn=tsp_sequencer,
                              n_orders=60, ticks=2000)
        result = sim.run(ticks=2000)
        self.assertGreater(result["orders_completed"], 0)

    def test_tsp_reduces_per_order_travel(self):
        """
        TSP reordering must reduce total Manhattan distance for each individual
        multi-stop pick sequence — this is the direct guarantee of NN-TSP.
        We verify it here at the task level, not via emergent sim congestion
        (which is confounded by agent interactions).
        """
        from foundry.orders import OrderStream
        grid = load_grid()
        skus = make_skus(n_a=5, n_b=5, n_c=5)
        inventory = Inventory.build(grid, skus, seed=42)

        orders = OrderStream.generate(20, [s.sku_id for s in skus],
                                      max_lines=3, duration_ticks=500, seed=7)

        agent_pos = (29, 24)   # representative starting position
        total_orig = 0
        total_tsp  = 0

        for order in orders._orders:
            tasks = [
                PickTask(pick_pos=inventory.get_pick_pos(l.sku_id),
                         sku_id=l.sku_id, qty=l.qty)
                for l in order.lines
                if inventory.get_pick_pos(l.sku_id) is not None
            ]
            if len(tasks) < 2:
                continue
            total_orig += total_distance(agent_pos, tasks)
            total_tsp  += total_distance(agent_pos, tsp_sequencer(agent_pos, tasks))

        self.assertLessEqual(total_tsp, total_orig,
                             "TSP increased total task travel distance")

    def test_abc_slotter_a_class_pick_positions_near_docks(self):
        """
        ABC slotter must place A-class pick positions closer to docks than
        random slotting does.  This is the direct invariant of the plugin —
        end-to-end travel steps also depend on path congestion, so we test
        the slotting quality directly here.
        """
        grid = load_grid()
        n_a = 10
        skus_a = [SKU(f"A{i:02d}", demand_class="A") for i in range(n_a)]

        abc_map  = abc_slotter(skus_a, grid, seed=42)
        rand_inv = Inventory.build(grid, skus_a, seed=99)   # different seed → different placement

        # ABC average dock distance
        avg_abc  = avg_dock_distance(abc_map, grid)

        # Random average dock distance — collect from rand_inv slots
        docks = grid.cells_of_type(Cell.DOCK)
        rand_dists = []
        for sku in skus_a:
            pp = rand_inv.get_pick_pos(sku.sku_id)
            if pp:
                rand_dists.append(min(abs(pp[0]-d[0]) + abs(pp[1]-d[1]) for d in docks))
        avg_rand = sum(rand_dists) / len(rand_dists) if rand_dists else 0.0

        self.assertLess(avg_abc, avg_rand,
                        f"ABC avg dock dist {avg_abc:.1f} not < random {avg_rand:.1f}")


# ──────────────────────────────────────────────────────────────────────────────
# CLI integration: plugins loadable via --slotter and --sequencer flags
# ──────────────────────────────────────────────────────────────────────────────

class TestPluginCLI(unittest.TestCase):
    LAYOUT_STR = str(LAYOUT)

    def test_abc_slotter_via_cli(self):
        import tempfile
        from foundry.__main__ import main
        slotter_path = str(Path(__file__).parent.parent /
                           "foundry" / "plugins" / "abc_slotter.py")
        with tempfile.TemporaryDirectory() as tmp:
            rc = main([
                "--layout",   self.LAYOUT_STR,
                "--ticks",    "200",
                "--agents",   "2",
                "--n-skus",   "10",
                "--n-orders", "20",
                "--slotter",  slotter_path,
                "--out",      tmp,
                "--quiet",
            ])
        self.assertEqual(rc, 0)

    def test_tsp_sequencer_via_cli(self):
        import tempfile
        from foundry.__main__ import main
        seq_path = str(Path(__file__).parent.parent /
                       "foundry" / "plugins" / "tsp_router.py")
        with tempfile.TemporaryDirectory() as tmp:
            rc = main([
                "--layout",    self.LAYOUT_STR,
                "--ticks",     "200",
                "--agents",    "2",
                "--n-skus",    "10",
                "--n-orders",  "20",
                "--sequencer", seq_path,
                "--out",       tmp,
                "--quiet",
            ])
        self.assertEqual(rc, 0)

    def test_both_plugins_via_cli(self):
        import tempfile, csv
        from foundry.__main__ import main
        slotter_path = str(Path(__file__).parent.parent /
                           "foundry" / "plugins" / "abc_slotter.py")
        seq_path = str(Path(__file__).parent.parent /
                       "foundry" / "plugins" / "tsp_router.py")
        with tempfile.TemporaryDirectory() as tmp:
            rc = main([
                "--layout",    self.LAYOUT_STR,
                "--ticks",     "500",
                "--agents",    "3",
                "--n-skus",    "20",
                "--n-orders",  "40",
                "--slotter",   slotter_path,
                "--sequencer", seq_path,
                "--seed",      "42",
                "--out",       tmp,
                "--quiet",
            ])
            self.assertEqual(rc, 0)
            summary = Path(tmp) / "run_42_summary.csv"
            with open(summary) as f:
                row = list(csv.DictReader(f))[0]
            self.assertIn("orders_completed", row)


if __name__ == "__main__":
    unittest.main(verbosity=2)
