from __future__ import annotations

import csv
import random
from dataclasses import dataclass, field
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
    """Ordered sequence of orders that can be consumed tick-by-tick."""

    def __init__(self, orders: list[Order]) -> None:
        self._orders = sorted(orders, key=lambda o: o.arrive_at)
        self._idx = 0

    def __len__(self) -> int:
        return len(self._orders)

    def arriving_at(self, tick: int) -> list[Order]:
        """Return all orders whose arrive_at <= tick (consumes them)."""
        result: list[Order] = []
        while self._idx < len(self._orders) and self._orders[self._idx].arrive_at <= tick:
            result.append(self._orders[self._idx])
            self._idx += 1
        return result

    def reset(self) -> None:
        self._idx = 0

    @classmethod
    def from_csv(cls, path: str | Path) -> OrderStream:
        """
        CSV columns: order_id, sku_id, qty, arrive_at
        Multiple rows with the same order_id are merged into one order.
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

    @classmethod
    def generate(
        cls,
        n_orders: int,
        skus: list[str],
        max_lines: int = 3,
        duration_ticks: int = 28800,
        seed: int = 42,
    ) -> OrderStream:
        """Stochastic generator: Poisson-spaced arrivals, uniform SKU draw."""
        rng = random.Random(seed)
        orders: list[Order] = []
        for i in range(n_orders):
            arrive_at = rng.randint(0, duration_ticks)
            n_lines = rng.randint(1, max_lines)
            lines = [
                OrderLine(sku_id=rng.choice(skus), qty=rng.randint(1, 5))
                for _ in range(n_lines)
            ]
            orders.append(Order(order_id=f"ORD{i:04d}", lines=lines, arrive_at=arrive_at))
        return cls(orders)
