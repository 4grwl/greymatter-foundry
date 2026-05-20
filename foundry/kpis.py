from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foundry.agent import Agent
    from foundry.orders import Order


class KPITracker:
    """Accumulates warehouse performance metrics during a simulation run."""

    def __init__(self) -> None:
        self.orders_completed:  int   = 0
        self.lines_completed:   int   = 0
        self.total_cycle_time:  int   = 0   # sum of (complete_tick - arrive_tick)
        self.congestion_events: int   = 0

    def record_order(self, order: Order) -> None:
        self.orders_completed += 1
        self.lines_completed  += order.n_lines
        if order.cycle_time is not None:
            self.total_cycle_time += order.cycle_time

    def record_congestion(self) -> None:
        self.congestion_events += 1

    def summary(self, elapsed_ticks: int, agents: list[Agent]) -> dict:
        hours = elapsed_ticks / 3600.0
        total_steps = sum(a.steps_taken for a in agents)
        avg_battery = sum(a.battery_pct for a in agents) / len(agents) if agents else 0.0
        avg_cycle   = (
            self.total_cycle_time / self.orders_completed
            if self.orders_completed > 0 else 0.0
        )
        return {
            "orders_completed":    self.orders_completed,
            "lines_completed":     self.lines_completed,
            "orders_per_hour":     round(self.orders_completed / hours, 2) if hours > 0 else 0.0,
            "lines_per_hour":      round(self.lines_completed  / hours, 2) if hours > 0 else 0.0,
            "avg_cycle_time_s":    round(avg_cycle, 1),
            "total_travel_steps":  total_steps,
            "congestion_events":   self.congestion_events,
            "avg_battery_pct":     round(avg_battery, 1),
            "elapsed_ticks":       elapsed_ticks,
        }
