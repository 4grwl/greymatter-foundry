"""
Greymatter Foundry — Order model and stream generator.

OrderStream.generate() implements the M6 stochastic model:
  • Poisson inter-arrivals   (exponential gaps ~ Exp(λ/3600))
  • ABC-weighted SKU draw    (A-class SKUs dominate by demand fraction)
  • Reproducible             (seeded random.Random)

Backward-compatible: existing callers that pass (n_orders, skus, …) continue
to work; the new DemandProfile is optional.
"""
from __future__ import annotations

import bisect
import csv
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class OrderLine:
    sku_id: str
    qty: int


@dataclass
class Order:
    order_id: str
    lines: list[OrderLine]
    arrive_at: int          # sim tick when order becomes available
    assigned_at: Optional[int] = None
    completed_at: Optional[int] = None

    @property
    def cycle_time(self) -> Optional[int]:
        if self.completed_at is not None and self.arrive_at is not None:
            return self.completed_at - self.arrive_at
        return None

    @property
    def n_lines(self) -> int:
        return len(self.lines)


class OrderStream:
    """Ordered sequence of orders consumed tick-by-tick."""

    def __init__(self, orders: list[Order]) -> None:
        self._orders = sorted(orders, key=lambda o: o.arrive_at)
        self._idx    = 0

    def __len__(self) -> int:
        return len(self._orders)

    def arriving_at(self, tick: int) -> list[Order]:
        """Return (and consume) all orders with arrive_at ≤ tick."""
        result: list[Order] = []
        while (self._idx < len(self._orders)
               and self._orders[self._idx].arrive_at <= tick):
            result.append(self._orders[self._idx])
            self._idx += 1
        return result

    def reset(self) -> None:
        self._idx = 0

    # ── Serialisation ──────────────────────────────────────────────────────

    @classmethod
    def from_csv(cls, path: str | Path) -> OrderStream:
        """
        Load from CSV.  Columns: order_id, sku_id, qty, arrive_at
        Multiple rows with the same order_id are merged into one Order.
        """
        orders: dict[str, Order] = {}
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                oid = row["order_id"]
                if oid not in orders:
                    orders[oid] = Order(
                        order_id=oid,
                        lines=[],
                        arrive_at=int(row["arrive_at"]),
                    )
                orders[oid].lines.append(
                    OrderLine(sku_id=row["sku_id"], qty=int(row["qty"]))
                )
        return cls(list(orders.values()))

    def to_csv(self, path: str | Path) -> None:
        """
        Save this stream to a CSV compatible with :meth:`from_csv`.
        Useful for persisting generated streams for reproducible reruns.
        """
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["order_id", "sku_id", "qty", "arrive_at"])
            for order in self._orders:
                for line in order.lines:
                    writer.writerow(
                        [order.order_id, line.sku_id, line.qty, order.arrive_at]
                    )

    # ── Stochastic generator ───────────────────────────────────────────────

    @classmethod
    def generate(
        cls,
        n_orders:       int | None    = None,
        skus:           list[str]     | None = None,
        max_lines:      int           = 3,
        duration_ticks: int           = 28800,
        seed:           int           = 42,
        profile:        "DemandProfile | None" = None,  # type: ignore[name-defined]
    ) -> OrderStream:
        """
        Generate a reproducible stochastic order stream.

        Arrival model
        ~~~~~~~~~~~~~
        Orders arrive according to a Poisson process with rate
        ``profile.arrival_rate`` orders/hour.  Inter-arrival gaps are drawn
        from Exp(λ/3600) and accumulated until *duration_ticks* is exceeded
        or *n_orders* orders have been produced (whichever comes first).

        SKU selection
        ~~~~~~~~~~~~~
        Each order line draws a demand class (A / B / C) with probability
        proportional to ``profile.abc_demand``, then picks a SKU uniformly
        within that class.  This concentrates volume on A-class SKUs —
        matching typical warehouse Pareto distributions.

        Backward compatibility
        ~~~~~~~~~~~~~~~~~~~~~~
        The (n_orders, skus, max_lines, duration_ticks, seed) signature from
        before M6 is preserved.  Pass ``profile`` to use the full model;
        omitting it selects sensible defaults (arrival_rate derived from
        n_orders and duration_ticks, ABC weights 70/20/10).

        Parameters
        ----------
        n_orders :
            Hard cap on orders generated.  ``None`` → generate until
            *duration_ticks* based on arrival rate alone.
        skus :
            SKU ID list.  Pass sorted by historical demand for the best ABC
            class assignment (most popular first = A-class).
        max_lines :
            Max lines per order when no profile is supplied.  Ignored when
            ``profile`` is provided (use ``profile.max_lines`` instead).
        duration_ticks :
            Simulation window length.  Orders are never placed beyond this.
        seed :
            RNG seed for full reproducibility.
        profile :
            :class:`~foundry.demand.DemandProfile` instance.  If omitted a
            default profile is constructed whose arrival_rate is calibrated
            to produce approximately *n_orders* orders in *duration_ticks*.
        """
        from foundry.demand import DemandProfile

        if skus is None or len(skus) == 0:
            raise ValueError("skus must be a non-empty list of SKU IDs")

        # ── Build / calibrate profile ──────────────────────────────────
        if profile is None:
            cap = n_orders if n_orders is not None else 500
            # Calibrate rate so ~cap orders arrive within duration_ticks
            hours = duration_ticks / 3600.0
            rate  = cap / max(hours, 1e-6)
            profile = DemandProfile(
                arrival_rate=rate,
                max_lines=max_lines,
            )

        rng = random.Random(seed)

        # ── Pre-compute ABC class weights for fast sampling ────────────
        class_weights = profile.build_class_weights(skus)
        # class_weights: [(cls, cumulative_prob, sku_list), ...]
        cum_probs = [cw for _, cw, _ in class_weights]

        def draw_sku() -> str:
            r   = rng.random()
            idx = bisect.bisect_left(cum_probs, r)
            idx = min(idx, len(class_weights) - 1)
            return rng.choice(class_weights[idx][2])

        # ── Poisson inter-arrival generation ──────────────────────────
        rate_per_tick = profile.arrival_rate / 3600.0
        max_orders    = n_orders if n_orders is not None else 10 ** 9

        orders: list[Order] = []
        tick_f = 0.0
        i      = 0

        while i < max_orders:
            tick_f += rng.expovariate(rate_per_tick)
            if tick_f > duration_ticks:
                break

            arrive_at = int(tick_f)
            n_lines   = rng.randint(1, profile.max_lines)
            lines     = [
                OrderLine(sku_id=draw_sku(), qty=rng.randint(profile.min_qty, profile.max_qty))
                for _ in range(n_lines)
            ]
            orders.append(Order(order_id=f"ORD{i:06d}", lines=lines, arrive_at=arrive_at))
            i += 1

        return cls(orders)


# Resolve forward reference
from foundry.demand import DemandProfile  # noqa: E402  (must be after class def)
OrderStream.generate.__annotations__["profile"] = Optional[DemandProfile]
