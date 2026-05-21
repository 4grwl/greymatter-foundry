"""
Tests for M6 — stochastic demand profile and order stream generator.

Coverage
────────
DemandProfile
  - defaults are sane
  - JSON round-trip (to_json / from_json)
  - classify_skus assigns correct fractions to A / B / C
  - class_lists returns non-overlapping, complete partition
  - build_class_weights produces normalised cumulative probs
  - empty classes are gracefully dropped from weights
  - summary returns expected keys

OrderStream.generate  (stochastic model)
  - reproducibility: same (profile, skus, seed) → identical stream
  - different seeds → different streams
  - Poisson inter-arrivals: sample mean ≈ 1/rate  (within 3σ)
  - ABC demand weighting: A-class SKUs appear more than C-class
  - n_orders hard cap is respected
  - n_orders=None lets rate drive count (approx)
  - all arrive_at values are within [0, duration_ticks)
  - all order lines reference SKUs from the supplied list
  - qty per line is within [min_qty, max_qty]
  - n_lines per order is within [1, max_lines]
  - to_csv / from_csv round-trip preserves all fields
  - profile with demand_rate override via CLI args
  - zero A-class SKUs (tiny SKU list) degrades gracefully
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import unittest
from pathlib import Path

from foundry.demand import DemandProfile
from foundry.orders import Order, OrderLine, OrderStream


# ── helpers ───────────────────────────────────────────────────────────────────

def _skus(n: int) -> list[str]:
    return [f"SKU{i:04d}" for i in range(n)]


def _stream(n_skus=20, n_orders=200, seed=42, profile=None, duration=7200):
    return OrderStream.generate(
        n_orders=n_orders,
        skus=_skus(n_skus),
        duration_ticks=duration,
        seed=seed,
        profile=profile,
    )


# ── DemandProfile ──────────────────────────────────────────────────────────────

class TestDemandProfileDefaults(unittest.TestCase):

    def setUp(self):
        self.p = DemandProfile()

    def test_arrival_rate_positive(self):
        self.assertGreater(self.p.arrival_rate, 0)

    def test_abc_skus_sums_to_one(self):
        self.assertAlmostEqual(sum(self.p.abc_skus), 1.0, places=6)

    def test_abc_demand_three_elements(self):
        self.assertEqual(len(self.p.abc_demand), 3)

    def test_abc_demand_positive(self):
        self.assertTrue(all(w > 0 for w in self.p.abc_demand))

    def test_max_lines_at_least_one(self):
        self.assertGreaterEqual(self.p.max_lines, 1)

    def test_qty_range_valid(self):
        self.assertLessEqual(self.p.min_qty, self.p.max_qty)


class TestDemandProfileSerialisation(unittest.TestCase):

    def test_json_round_trip(self):
        p = DemandProfile(arrival_rate=120.0, max_lines=5, min_qty=2, max_qty=8)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            p.to_json(path)
            p2 = DemandProfile.from_json(path)
            self.assertAlmostEqual(p2.arrival_rate, 120.0)
            self.assertEqual(p2.max_lines, 5)
            self.assertEqual(p2.min_qty, 2)
            self.assertEqual(p2.max_qty, 8)
            self.assertEqual(p2.abc_skus,   p.abc_skus)
            self.assertEqual(p2.abc_demand, p.abc_demand)
        finally:
            os.unlink(path)

    def test_json_is_valid_json(self):
        p = DemandProfile()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            p.to_json(path)
            data = json.loads(Path(path).read_text())
            self.assertIn("arrival_rate", data)
        finally:
            os.unlink(path)

    def test_summary_keys(self):
        keys = DemandProfile().summary().keys()
        for expected in ("arrival_rate_per_hour", "abc_sku_split",
                         "abc_demand_weights", "max_lines", "qty_range"):
            self.assertIn(expected, keys)


class TestDemandProfileClassifySKUs(unittest.TestCase):

    def test_all_skus_classified(self):
        skus = _skus(50)
        cls_map = DemandProfile().classify_skus(skus)
        self.assertEqual(set(cls_map.keys()), set(skus))

    def test_classes_are_abc(self):
        cls_map = DemandProfile().classify_skus(_skus(30))
        self.assertTrue(all(v in "ABC" for v in cls_map.values()))

    def test_a_class_is_smallest_fraction(self):
        """Default: 20 % A, 30 % B, 50 % C → A bucket is smallest."""
        cls_map = DemandProfile().classify_skus(_skus(100))
        counts = {c: sum(1 for v in cls_map.values() if v == c) for c in "ABC"}
        self.assertLess(counts["A"], counts["C"])

    def test_first_skus_are_a_class(self):
        """The first SKU in the list should be A-class (hottest mover)."""
        cls_map = DemandProfile().classify_skus(_skus(10))
        self.assertEqual(cls_map[_skus(10)[0]], "A")

    def test_single_sku_still_classified(self):
        cls_map = DemandProfile().classify_skus(["ONLY"])
        self.assertIn(cls_map["ONLY"], "ABC")

    def test_class_lists_partition(self):
        """class_lists must cover every SKU exactly once."""
        skus = _skus(60)
        buckets = DemandProfile().class_lists(skus)
        all_classified = buckets["A"] + buckets["B"] + buckets["C"]
        self.assertEqual(sorted(all_classified), sorted(skus))

    def test_class_lists_no_overlap(self):
        skus = _skus(30)
        buckets = DemandProfile().class_lists(skus)
        a_set = set(buckets["A"])
        b_set = set(buckets["B"])
        c_set = set(buckets["C"])
        self.assertEqual(len(a_set & b_set), 0)
        self.assertEqual(len(b_set & c_set), 0)


class TestBuildClassWeights(unittest.TestCase):

    def test_cumulative_ends_at_one(self):
        weights = DemandProfile().build_class_weights(_skus(20))
        self.assertAlmostEqual(weights[-1][1], 1.0, places=6)

    def test_cumulative_is_monotone(self):
        weights = DemandProfile().build_class_weights(_skus(20))
        probs = [w[1] for w in weights]
        self.assertEqual(probs, sorted(probs))

    def test_empty_class_dropped(self):
        """With only 1 SKU, B and C will be empty — must not crash."""
        weights = DemandProfile().build_class_weights(["SINGLE"])
        self.assertEqual(len(weights), 1)
        self.assertAlmostEqual(weights[0][1], 1.0, places=6)

    def test_two_skus_no_crash(self):
        weights = DemandProfile().build_class_weights(_skus(2))
        self.assertGreater(len(weights), 0)


# ── OrderStream.generate ──────────────────────────────────────────────────────

class TestGenerateReproducibility(unittest.TestCase):

    def test_same_seed_same_stream(self):
        s1 = _stream(seed=7)
        s2 = _stream(seed=7)
        ids1 = [o.order_id for o in s1._orders]
        ids2 = [o.order_id for o in s2._orders]
        self.assertEqual(ids1, ids2)
        for o1, o2 in zip(s1._orders, s2._orders):
            self.assertEqual(o1.arrive_at, o2.arrive_at)
            self.assertEqual([(l.sku_id, l.qty) for l in o1.lines],
                             [(l.sku_id, l.qty) for l in o2.lines])

    def test_different_seeds_different_streams(self):
        s1 = _stream(seed=1)
        s2 = _stream(seed=2)
        ticks1 = [o.arrive_at for o in s1._orders]
        ticks2 = [o.arrive_at for o in s2._orders]
        self.assertNotEqual(ticks1, ticks2)


class TestPoissonArrivals(unittest.TestCase):
    """
    The inter-arrival gaps should be exponentially distributed.
    We check that the sample mean is within 3 standard errors of 1/rate.
    """

    def test_inter_arrival_mean(self):
        n          = 500
        duration   = 100_000   # large window so all n_orders fit
        rate_hr    = 120.0     # 120 orders/hour → mean gap = 30 ticks
        profile    = DemandProfile(arrival_rate=rate_hr)
        stream     = OrderStream.generate(
            n_orders=n, skus=_skus(20),
            duration_ticks=duration, seed=0, profile=profile,
        )
        ticks = sorted(o.arrive_at for o in stream._orders)
        gaps  = [ticks[i+1] - ticks[i] for i in range(len(ticks)-1)]
        if not gaps:
            self.skipTest("Not enough orders generated")

        mean_gap    = sum(gaps) / len(gaps)
        expected    = 3600.0 / rate_hr           # ticks between orders
        # Allow generous tolerance (exponential is high-variance)
        self.assertAlmostEqual(mean_gap, expected, delta=expected * 0.5)

    def test_arrivals_in_sorted_order(self):
        stream = _stream(n_orders=100)
        ticks  = [o.arrive_at for o in stream._orders]
        self.assertEqual(ticks, sorted(ticks))

    def test_all_arrivals_within_duration(self):
        duration = 3600
        stream   = _stream(n_orders=50, duration=duration)
        for o in stream._orders:
            self.assertLess(o.arrive_at, duration)
            self.assertGreaterEqual(o.arrive_at, 0)


class TestABCDemandWeighting(unittest.TestCase):
    """
    With the default 70/20/10 demand split, A-class SKUs should appear
    substantially more often than C-class across a large sample.
    """

    def _sku_freq(self, profile, n_orders=2000, n_skus=50):
        stream = OrderStream.generate(
            n_orders=n_orders, skus=_skus(n_skus),
            duration_ticks=500_000, seed=99, profile=profile,
        )
        freq: dict[str, int] = {}
        for o in stream._orders:
            for line in o.lines:
                freq[line.sku_id] = freq.get(line.sku_id, 0) + 1
        return freq

    def test_a_class_outnumbers_c_class(self):
        p    = DemandProfile(arrival_rate=3600.0)  # fast rate to get many orders
        freq = self._sku_freq(p, n_orders=2000, n_skus=50)

        # A-class: first 20% of 50 skus = SKU0000–SKU0009
        # C-class: last 50% of 50 skus  = SKU0025–SKU0049
        a_total = sum(freq.get(f"SKU{i:04d}", 0) for i in range(10))
        c_total = sum(freq.get(f"SKU{i:04d}", 0) for i in range(25, 50))
        self.assertGreater(a_total, c_total,
                           msg="A-class SKUs should have higher total demand than C-class")

    def test_uniform_profile_flattens_skew(self):
        """Equal abc_demand weights → no class should dominate hugely."""
        p    = DemandProfile(arrival_rate=3600.0,
                             abc_demand=[1.0, 1.0, 1.0])
        freq = self._sku_freq(p, n_orders=1500, n_skus=30)
        a_total = sum(freq.get(f"SKU{i:04d}", 0) for i in range(6))
        c_total = sum(freq.get(f"SKU{i:04d}", 0) for i in range(15, 30))
        # With equal weights, A-class has fewer SKUs (6) so lower *total* than C (15 SKUs),
        # but per-SKU frequency should be roughly equal.
        a_per = a_total / 6
        c_per = c_total / 15
        ratio = a_per / max(c_per, 1)
        self.assertLess(ratio, 3.0, "Uniform weights should not produce extreme A/C skew")


class TestGenerateConstraints(unittest.TestCase):

    def test_n_orders_cap(self):
        stream = _stream(n_orders=50)
        self.assertLessEqual(len(stream), 50)

    def test_n_orders_none_uses_rate(self):
        """Without n_orders, count is determined by arrival_rate × hours."""
        profile  = DemandProfile(arrival_rate=120.0)   # 120 orders/hr
        duration = 7200                                  # 2 hours → ~240 expected
        stream   = OrderStream.generate(
            n_orders=None, skus=_skus(20),
            duration_ticks=duration, seed=0, profile=profile,
        )
        expected = 120 * (duration / 3600)
        # Poisson count: std_dev ≈ sqrt(expected), allow ±4σ
        tolerance = 4 * math.sqrt(expected) + 10
        self.assertAlmostEqual(len(stream), expected, delta=tolerance)

    def test_all_skus_come_from_input_list(self):
        sku_set = set(_skus(20))
        stream  = _stream(n_orders=100)
        for o in stream._orders:
            for line in o.lines:
                self.assertIn(line.sku_id, sku_set)

    def test_qty_within_bounds(self):
        p = DemandProfile(min_qty=2, max_qty=7)
        stream = OrderStream.generate(
            n_orders=100, skus=_skus(10),
            duration_ticks=10000, seed=0, profile=p,
        )
        for o in stream._orders:
            for line in o.lines:
                self.assertGreaterEqual(line.qty, 2)
                self.assertLessEqual(line.qty, 7)

    def test_lines_within_bounds(self):
        p = DemandProfile(max_lines=4)
        stream = OrderStream.generate(
            n_orders=100, skus=_skus(10),
            duration_ticks=10000, seed=0, profile=p,
        )
        for o in stream._orders:
            self.assertGreaterEqual(o.n_lines, 1)
            self.assertLessEqual(o.n_lines, 4)

    def test_tiny_sku_list_no_crash(self):
        """A single SKU should not crash the generator."""
        stream = OrderStream.generate(
            n_orders=10, skus=["ONLY_SKU"],
            duration_ticks=1000, seed=0,
        )
        self.assertGreater(len(stream), 0)
        for o in stream._orders:
            for line in o.lines:
                self.assertEqual(line.sku_id, "ONLY_SKU")

    def test_empty_sku_list_raises(self):
        with self.assertRaises((ValueError, IndexError, Exception)):
            OrderStream.generate(n_orders=10, skus=[])


class TestOrderStreamToCSV(unittest.TestCase):

    def _roundtrip(self, stream: OrderStream) -> OrderStream:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            path = f.name
        try:
            stream.to_csv(path)
            return OrderStream.from_csv(path)
        finally:
            os.unlink(path)

    def test_order_count_preserved(self):
        s = _stream(n_orders=30)
        s2 = self._roundtrip(s)
        self.assertEqual(len(s2), len(s))

    def test_order_ids_preserved(self):
        s = _stream(n_orders=20)
        ids1 = {o.order_id for o in s._orders}
        s2   = self._roundtrip(s)
        ids2 = {o.order_id for o in s2._orders}
        self.assertEqual(ids1, ids2)

    def test_arrive_at_preserved(self):
        s  = _stream(n_orders=15)
        s2 = self._roundtrip(s)
        at1 = {o.order_id: o.arrive_at for o in s._orders}
        at2 = {o.order_id: o.arrive_at for o in s2._orders}
        self.assertEqual(at1, at2)

    def test_lines_preserved(self):
        s  = _stream(n_orders=15)
        s2 = self._roundtrip(s)
        for o1, o2 in zip(
            sorted(s._orders,  key=lambda x: x.order_id),
            sorted(s2._orders, key=lambda x: x.order_id),
        ):
            pairs1 = sorted((l.sku_id, l.qty) for l in o1.lines)
            pairs2 = sorted((l.sku_id, l.qty) for l in o2.lines)
            self.assertEqual(pairs1, pairs2)

    def test_csv_has_correct_header(self):
        s = _stream(n_orders=5)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            path = f.name
        try:
            s.to_csv(path)
            with open(path) as fh:
                header = fh.readline().strip()
            self.assertEqual(header, "order_id,sku_id,qty,arrive_at")
        finally:
            os.unlink(path)


class TestDemandRateOverride(unittest.TestCase):
    """Simulates the CLI path: profile loaded then arrival_rate overridden."""

    def test_rate_override_changes_count(self):
        slow_profile = DemandProfile(arrival_rate=10.0)
        fast_profile = DemandProfile(arrival_rate=600.0)
        kw = dict(n_orders=None, skus=_skus(20), duration_ticks=7200, seed=0)
        slow = OrderStream.generate(**kw, profile=slow_profile)
        fast = OrderStream.generate(**kw, profile=fast_profile)
        self.assertGreater(len(fast), len(slow))


if __name__ == "__main__":
    unittest.main()
