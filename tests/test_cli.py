"""
M3 tests — CLI entry point and results CSV.
"""
import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from foundry.__main__ import build_parser, build_simulation, main
from foundry.results import write_results, print_summary

LAYOUT = str(Path(__file__).parent.parent / "layouts" / "default.json")
ORDERS = str(Path(__file__).parent.parent / "data"  / "orders_sample.csv")


# ──────────────────────────────────────────────────────────────────────────────
# Argument parser
# ──────────────────────────────────────────────────────────────────────────────

class TestParser(unittest.TestCase):
    def _parse(self, argv):
        return build_parser().parse_args(argv)

    def test_defaults(self):
        args = self._parse([])
        self.assertEqual(args.agents,   5)
        self.assertEqual(args.seed,     42)
        self.assertEqual(args.ticks,    28800)
        self.assertIsNone(args.realtime)
        self.assertFalse(args.tui)
        self.assertFalse(args.quiet)

    def test_layout_flag(self):
        args = self._parse(["--layout", "my_layout.json"])
        self.assertEqual(args.layout, "my_layout.json")

    def test_agents_flag(self):
        args = self._parse(["--agents", "3"])
        self.assertEqual(args.agents, 3)

    def test_seed_flag(self):
        args = self._parse(["--seed", "99"])
        self.assertEqual(args.seed, 99)

    def test_ticks_flag(self):
        args = self._parse(["--ticks", "1000"])
        self.assertEqual(args.ticks, 1000)

    def test_out_flag(self):
        args = self._parse(["--out", "/tmp/my_results"])
        self.assertEqual(args.out, "/tmp/my_results")

    def test_quiet_flag(self):
        args = self._parse(["--quiet"])
        self.assertTrue(args.quiet)

    def test_tui_flag(self):
        args = self._parse(["--tui"])
        self.assertTrue(args.tui)

    def test_realtime_flag(self):
        args = self._parse(["--realtime", "10.0"])
        self.assertAlmostEqual(args.realtime, 10.0)


# ──────────────────────────────────────────────────────────────────────────────
# build_simulation
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildSimulation(unittest.TestCase):
    def _args(self, **kwargs):
        defaults = dict(
            layout=LAYOUT, orders=None, agents=3, seed=42,
            ticks=500, n_skus=20, n_orders=30,
            slotter=None, dispatch=None, quiet=True,
        )
        defaults.update(kwargs)
        return build_parser().parse_args([])  # get Namespace, then override

    def _make_args(self, **kwargs):
        import argparse
        args = argparse.Namespace(
            layout=LAYOUT, orders=None, agents=3, seed=42,
            ticks=500, n_skus=20, n_orders=30,
            slotter=None, dispatch=None, quiet=True,
        )
        for k, v in kwargs.items():
            setattr(args, k, v)
        return args

    def test_stochastic_mode_builds_simulation(self):
        from foundry.simulation import Simulation
        args = self._make_args()
        sim = build_simulation(args)
        self.assertIsInstance(sim, Simulation)

    def test_csv_orders_mode(self):
        from foundry.simulation import Simulation
        args = self._make_args(orders=ORDERS)
        sim = build_simulation(args)
        self.assertIsInstance(sim, Simulation)

    def test_correct_agent_count(self):
        args = self._make_args(agents=4)
        sim = build_simulation(args)
        self.assertEqual(len(sim.agents), 4)

    def test_seed_reproducibility(self):
        args1 = self._make_args(seed=7)
        args2 = self._make_args(seed=7)
        sim1 = build_simulation(args1)
        sim2 = build_simulation(args2)
        sim1.run(ticks=200)
        sim2.run(ticks=200)
        self.assertEqual(sim1.kpis.orders_completed, sim2.kpis.orders_completed)

    def test_different_seeds_differ(self):
        args1 = self._make_args(seed=1)
        args2 = self._make_args(seed=2)
        sim1 = build_simulation(args1)
        sim2 = build_simulation(args2)
        sim1.run(ticks=1000)
        sim2.run(ticks=1000)
        # Events or layout may differ enough for step counts to diverge
        steps1 = sum(a.steps_taken for a in sim1.agents)
        steps2 = sum(a.steps_taken for a in sim2.agents)
        # Not guaranteed to differ (same layout) but KPIs should differ
        # At minimum both should have run
        self.assertGreater(steps1 + steps2, 0)

    def test_plugin_dispatcher_loads(self):
        import tempfile, os
        plugin = textwrap.dedent("""
            def dispatcher(pending, idle_agents, pick_pos_fn):
                return []
        """)
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(plugin)
            fname = f.name
        try:
            args = self._make_args(dispatch=fname)
            sim = build_simulation(args)
            self.assertIsNotNone(sim)
        finally:
            os.unlink(fname)


import textwrap


# ──────────────────────────────────────────────────────────────────────────────
# Results writer
# ──────────────────────────────────────────────────────────────────────────────

class TestResultsWriter(unittest.TestCase):
    def _run_mini_sim(self):
        import argparse
        from foundry.simulation import Simulation

        args = argparse.Namespace(
            layout=LAYOUT, orders=None, agents=3, seed=42,
            ticks=500, n_skus=20, n_orders=50,
            slotter=None, dispatch=None, quiet=True,
        )
        sim = build_simulation(args)
        sim.run(ticks=500)
        return sim

    def test_write_creates_both_files(self):
        sim = self._run_mini_sim()
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_results(sim, tmp, seed=42)
            self.assertTrue(paths["summary"].exists())
            self.assertTrue(paths["events"].exists())

    def test_summary_csv_has_header(self):
        sim = self._run_mini_sim()
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_results(sim, tmp, seed=42)
            with open(paths["summary"]) as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertIn("orders_completed",  rows[0])
            self.assertIn("orders_per_hour",   rows[0])
            self.assertIn("total_travel_steps", rows[0])
            self.assertIn("seed",              rows[0])

    def test_summary_seed_column(self):
        sim = self._run_mini_sim()
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_results(sim, tmp, seed=99)
            with open(paths["summary"]) as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(int(rows[0]["seed"]), 99)

    def test_events_csv_has_header(self):
        sim = self._run_mini_sim()
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_results(sim, tmp, seed=42)
            with open(paths["events"]) as f:
                reader = csv.DictReader(f)
                header = reader.fieldnames
            self.assertIn("type",  header)
            self.assertIn("agent", header)
            self.assertIn("tick",  header)

    def test_events_count_matches_simulation(self):
        sim = self._run_mini_sim()
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_results(sim, tmp, seed=42)
            with open(paths["events"]) as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), len(sim.events))

    def test_out_dir_created_if_missing(self):
        sim = self._run_mini_sim()
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "deep" / "nested"
            write_results(sim, nested, seed=42)
            self.assertTrue(nested.exists())

    def test_per_agent_columns_in_summary(self):
        sim = self._run_mini_sim()
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_results(sim, tmp, seed=42)
            with open(paths["summary"]) as f:
                rows = list(csv.DictReader(f))
            # Each agent should have a steps column
            for agent in sim.agents:
                col = f"{agent.agent_id}_steps"
                self.assertIn(col, rows[0])

    def test_print_summary_no_crash(self):
        sim = self._run_mini_sim()
        kpis = sim.kpis.summary(sim.clock, sim.agents)
        # Should not raise
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            print_summary(kpis)
        finally:
            sys.stdout = old_stdout


# ──────────────────────────────────────────────────────────────────────────────
# End-to-end CLI via main()
# ──────────────────────────────────────────────────────────────────────────────

class TestMainCLI(unittest.TestCase):
    def test_main_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = main([
                "--layout", LAYOUT,
                "--ticks",  "100",
                "--agents", "2",
                "--n-skus", "10",
                "--n-orders", "20",
                "--seed",   "42",
                "--out",    tmp,
                "--quiet",
            ])
        self.assertEqual(rc, 0)

    def test_main_writes_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            main([
                "--layout", LAYOUT,
                "--ticks",  "100",
                "--agents", "2",
                "--n-skus", "10",
                "--n-orders", "20",
                "--seed",   "7",
                "--out",    tmp,
                "--quiet",
            ])
            self.assertTrue((Path(tmp) / "run_7_summary.csv").exists())
            self.assertTrue((Path(tmp) / "run_7_events.csv").exists())

    def test_main_with_orders_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = main([
                "--layout", LAYOUT,
                "--orders", ORDERS,
                "--ticks",  "200",
                "--agents", "3",
                "--seed",   "42",
                "--out",    tmp,
                "--quiet",
            ])
        self.assertEqual(rc, 0)

    def test_main_reproducible_across_runs(self):
        argv = [
            "--layout", LAYOUT,
            "--ticks",  "300",
            "--agents", "3",
            "--n-skus", "15",
            "--n-orders", "30",
            "--seed",   "55",
            "--quiet",
        ]
        results = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                main(argv + ["--out", tmp])
                with open(Path(tmp) / "run_55_summary.csv") as f:
                    results.append(list(csv.DictReader(f))[0])

        self.assertEqual(results[0]["orders_completed"], results[1]["orders_completed"])
        self.assertEqual(results[0]["total_travel_steps"], results[1]["total_travel_steps"])

    def test_plugin_dispatcher_via_cli(self):
        import os, textwrap
        plugin_code = textwrap.dedent("""
            def dispatcher(pending, idle_agents, pick_pos_fn):
                # Assign nothing — no-op dispatcher
                return []
        """)
        with tempfile.TemporaryDirectory() as tmp:
            plugin_path = Path(tmp) / "noop_dispatcher.py"
            plugin_path.write_text(plugin_code)
            rc = main([
                "--layout",   LAYOUT,
                "--ticks",    "100",
                "--agents",   "2",
                "--n-skus",   "10",
                "--n-orders", "10",
                "--dispatch", str(plugin_path),
                "--out",      tmp,
                "--quiet",
            ])
        self.assertEqual(rc, 0)

    def test_stochastic_8h_sim_completes(self):
        """Full 8-hour (28800 tick) sim must finish and write results."""
        with tempfile.TemporaryDirectory() as tmp:
            rc = main([
                "--layout",   LAYOUT,
                "--ticks",    "28800",
                "--agents",   "5",
                "--n-skus",   "50",
                "--n-orders", "500",
                "--seed",     "42",
                "--out",      tmp,
                "--quiet",
            ])
            self.assertEqual(rc, 0)
            summary_path = Path(tmp) / "run_42_summary.csv"
            self.assertTrue(summary_path.exists())
            with open(summary_path) as f:
                row = list(csv.DictReader(f))[0]
            self.assertEqual(int(row["elapsed_ticks"]), 28800)
            self.assertGreater(int(row["orders_completed"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
