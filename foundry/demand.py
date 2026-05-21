"""
Greymatter Foundry — Stochastic Demand Profile (M6)

Model
─────
SKU classes
    The SKU list is split into three demand tiers by position:
    • A-class  top    abc_skus[0] fraction  (default 20 %) — hottest movers
    • B-class  next   abc_skus[1] fraction  (default 30 %) — regular movers
    • C-class  bottom abc_skus[2] fraction  (default 50 %) — slow movers

    This mirrors the Pareto 80/20 rule: a small fraction of SKUs drives
    most order volume.

Arrival process
    Orders arrive according to a Poisson process with rate λ (orders/hour).
    Inter-arrival gaps are i.i.d. Exp(λ/3600) ticks, giving the classic
    memoryless property and realistic burstiness.

SKU draw
    Each order line picks a demand class with probability proportional to
    abc_demand weights (default 70 / 20 / 10 %).  Within the chosen class
    a SKU is picked uniformly at random.

Reproducibility
    All randomness flows through a single seeded `random.Random` instance
    so the same (profile, skus, seed) triple always produces the same stream.

Usage
─────
    from foundry.demand import DemandProfile
    profile = DemandProfile(arrival_rate=120.0)   # 120 orders / hour
    profile.to_json("profiles/high_volume.json")  # save
    p2 = DemandProfile.from_json("profiles/high_volume.json")  # load
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class DemandProfile:
    """Configures the stochastic order stream generator.

    Parameters
    ----------
    arrival_rate : float
        Average number of orders arriving per *hour* (Poisson λ).
        Default 60 → one order every simulated minute on average.
    abc_skus : list[float]
        Fraction of the SKU catalogue assigned to classes A, B, C.
        Must sum to ≤ 1.0; remainder is absorbed into C.
        Default [0.20, 0.30, 0.50].
    abc_demand : list[float]
        Relative demand weight for classes A, B, C.
        Normalised internally so they need not sum to 1.
        Default [0.70, 0.20, 0.10].
    max_lines : int
        Maximum number of distinct SKU lines per order.  Actual count
        is drawn uniformly from [1, max_lines].
    min_qty, max_qty : int
        Quantity range per order line (uniform draw).
    """

    arrival_rate: float       = 60.0
    abc_skus:     list[float] = field(default_factory=lambda: [0.20, 0.30, 0.50])
    abc_demand:   list[float] = field(default_factory=lambda: [0.70, 0.20, 0.10])
    max_lines:    int         = 3
    min_qty:      int         = 1
    max_qty:      int         = 5

    # ── Serialisation ─────────────────────────────────────────────────────

    def to_json(self, path: str | Path) -> None:
        """Write the profile to a JSON file."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def from_json(cls, path: str | Path) -> DemandProfile:
        """Load a profile from a JSON file written by :meth:`to_json`."""
        data = json.loads(Path(path).read_text())
        return cls(**data)

    # ── SKU classification ────────────────────────────────────────────────

    def classify_skus(self, skus: list[str]) -> dict[str, str]:
        """Return ``{sku_id: 'A'|'B'|'C'}`` for every SKU in *skus*.

        SKUs are split in the order they appear in the list, so passing a
        list pre-sorted by historical demand (most popular first) gives the
        most meaningful class assignment.
        """
        n     = len(skus)
        a_end = max(1, round(n * self.abc_skus[0]))
        b_end = max(a_end + 1, round(n * (self.abc_skus[0] + self.abc_skus[1])))
        b_end = min(b_end, n)
        result: dict[str, str] = {}
        for i, sku in enumerate(skus):
            if i < a_end:
                result[sku] = "A"
            elif i < b_end:
                result[sku] = "B"
            else:
                result[sku] = "C"
        return result

    def class_lists(self, skus: list[str]) -> dict[str, list[str]]:
        """Return ``{'A': [...], 'B': [...], 'C': [...]}``."""
        buckets: dict[str, list[str]] = {"A": [], "B": [], "C": []}
        for sku, cls in self.classify_skus(skus).items():
            buckets[cls].append(sku)
        return buckets

    # ── Weighted class sampler ────────────────────────────────────────────

    def build_class_weights(
        self, skus: list[str]
    ) -> list[tuple[str, float, list[str]]]:
        """Return ``[(class, cumulative_weight, sku_list), ...]`` for sampling.

        Empty classes are silently dropped and weights are renormalised so
        the profile degrades gracefully when the SKU list is very short.
        """
        buckets = self.class_lists(skus)
        pairs   = list(zip("ABC", self.abc_demand))
        active  = [(cls, w, buckets[cls]) for cls, w in pairs if buckets[cls]]
        total   = sum(w for _, w, _ in active)
        if total == 0:
            # Fallback: uniform
            n = len(active)
            active = [(cls, 1.0 / n, lst) for cls, _, lst in active]
            total  = 1.0

        # Convert to cumulative for fast bisect-style sampling
        cumulative: list[tuple[str, float, list[str]]] = []
        acc = 0.0
        for cls, w, lst in active:
            acc += w / total
            cumulative.append((cls, acc, lst))
        return cumulative

    # ── Summary ───────────────────────────────────────────────────────────

    def summary(self) -> dict:
        """Return a plain-dict summary suitable for logging or display."""
        return {
            "arrival_rate_per_hour": self.arrival_rate,
            "abc_sku_split":         self.abc_skus,
            "abc_demand_weights":    self.abc_demand,
            "max_lines":             self.max_lines,
            "qty_range":             [self.min_qty, self.max_qty],
        }
