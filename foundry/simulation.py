"""
Greymatter Foundry — discrete-event simulation core.

One tick = 1 simulated second.
Main loop: spawn orders → dispatch → tick agents → record KPIs.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Callable, Optional

from foundry.agent import Agent, PickTask
from foundry.grid import Cell, Grid
from foundry.inventory import Inventory
from foundry.kpis import KPITracker
from foundry.orders import Order, OrderStream
from foundry.plugins.base import default_dispatcher


class Simulation:
    def __init__(
        self,
        grid: Grid,
        inventory: Inventory,
        orders: OrderStream,
        n_agents: int = 5,
        seed: int = 42,
        dispatcher_fn: Optional[Callable] = None,
        task_sequencer: Optional[Callable] = None,
    ) -> None:
        self.grid      = grid
        self.inventory = inventory
        self.orders    = orders
        self.clock     = 0
        self.kpis      = KPITracker()
        self.events:   list[dict] = []

        self._dispatcher_fn   = dispatcher_fn or default_dispatcher
        self._task_sequencer  = task_sequencer   # optional: (agent_pos, tasks) -> tasks

        # Place agents near dock positions with unique starting cells
        docks = grid.cells_of_type(Cell.DOCK)
        if not docks:
            raise ValueError("Grid has no DOCK cells — cannot place agents.")
        start_positions = _spread_start_positions(grid, docks, n_agents)
        self.agents: list[Agent] = [
            Agent(f"AMR-{i:02d}", start_positions[i])
            for i in range(n_agents)
        ]

        self._pending:  deque[Order]       = deque()
        self._active:   dict[str, Order]   = {}
        self._skipped:  int                = 0   # orders with no slotted SKUs

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self, ticks: int) -> dict:
        """Run for `ticks` simulation steps. Returns final KPI summary."""
        for _ in range(ticks):
            self._tick()
        return self.kpis.summary(self.clock, self.agents)

    def step(self) -> list[dict]:
        """Advance by one tick. Returns events emitted this tick."""
        self._tick()
        return self.events[-50:]   # last 50 events for TUI use

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        self.clock += 1

        # 1. Spawn arriving orders
        for order in self.orders.arriving_at(self.clock):
            self._pending.append(order)

        # 2. Dispatch pending orders to idle agents
        self._dispatch()

        # 3. Tick each agent.
        # `occupied` is updated after every move so later agents in the loop
        # see the real current positions — this dissolves convoy pile-ups where
        # a trailing agent can only move once the one ahead of it has stepped
        # forward.  Combined with the per-agent back-off in Agent._move_step,
        # this also breaks head-on deadlocks without expensive cooperative
        # path planning.
        occupied = {a.position for a in self.agents}
        for agent in self.agents:
            old_pos = agent.position
            others  = occupied - {old_pos}
            evts    = agent.tick(self.grid, others, self.clock)
            # Keep occupied consistent for agents processed later this tick.
            if agent.position != old_pos:
                occupied.discard(old_pos)
                occupied.add(agent.position)
            self.events.extend(evts)
            for evt in evts:
                self._handle_event(evt)

    def _dispatch(self) -> None:
        if not self._pending:
            return
        idle = [a for a in self.agents if a.is_idle]
        if not idle:
            return

        # Filter to orders that have at least one slotted SKU
        dispatchable = [
            o for o in self._pending
            if self.inventory.get_pick_pos(o.lines[0].sku_id) is not None
        ]
        assignments = self._dispatcher_fn(
            dispatchable, idle, self.inventory.get_pick_pos
        )

        dispatched_orders: set[str] = set()
        for order, agent in assignments:
            if order.order_id in dispatched_orders:
                continue

            tasks = self._build_pick_tasks(order)
            if not tasks:
                self._skipped += 1
                continue

            # Optional TSP / sequencer hook reorders tasks before dispatch
            if self._task_sequencer is not None:
                tasks = self._task_sequencer(agent.position, tasks)

            dock = _nearest_dock(self.grid, agent.position)
            order.assigned_at = self.clock
            self._active[order.order_id] = order
            dispatched_orders.add(order.order_id)
            agent.assign_order(order.order_id, tasks, dock, self.grid)

        # Remove dispatched (and unskippable) orders from pending queue
        skip_ids = {
            o.order_id for o in self._pending
            if self.inventory.get_pick_pos(o.lines[0].sku_id) is None
        }
        self._pending = deque(
            o for o in self._pending
            if o.order_id not in dispatched_orders and o.order_id not in skip_ids
        )
        self._skipped += len(skip_ids)

    def _build_pick_tasks(self, order: Order) -> list[PickTask]:
        tasks: list[PickTask] = []
        for line in order.lines:
            pos = self.inventory.get_pick_pos(line.sku_id)
            if pos is not None:
                tasks.append(PickTask(pick_pos=pos, sku_id=line.sku_id, qty=line.qty))
                self.inventory.deduct(line.sku_id, line.qty)
        return tasks

    def _handle_event(self, evt: dict) -> None:
        if evt["type"] == "ORDER_COMPLETE":
            order = self._active.pop(evt["order_id"], None)
            if order:
                order.completed_at = self.clock
                self.kpis.record_order(order)
        elif evt["type"] == "CONGESTION":
            self.kpis.record_congestion()

    # ------------------------------------------------------------------
    # Convenience constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_files(
        cls,
        layout_path: str | Path,
        orders_path: str | Path,
        n_agents: int = 5,
        n_skus: int = 50,
        seed: int = 42,
    ) -> Simulation:
        from foundry.inventory import SKU

        grid = Grid.from_json(layout_path)
        orders = OrderStream.from_csv(orders_path)

        # Build a simple inventory from the SKU IDs referenced in the orders
        sku_ids_seen: list[str] = []
        seen: set[str] = set()
        for o in orders._orders:
            for line in o.lines:
                if line.sku_id not in seen:
                    sku_ids_seen.append(line.sku_id)
                    seen.add(line.sku_id)

        skus = [SKU(sku_id=sid) for sid in sku_ids_seen[:n_skus]]
        inventory = Inventory.build(grid, skus, seed=seed)
        return cls(grid, inventory, orders, n_agents=n_agents, seed=seed)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _nearest_dock(grid: Grid, pos: tuple[int, int]) -> tuple[int, int]:
    docks = grid.cells_of_type(Cell.DOCK)
    return min(docks, key=lambda p: abs(p[0] - pos[0]) + abs(p[1] - pos[1]))


def _spread_start_positions(
    grid: Grid,
    docks: list[tuple[int, int]],
    n_agents: int,
) -> list[tuple[int, int]]:
    """
    Return n_agents unique traversable starting cells spread around the docks.

    Agents should NOT start on dock cells — those are reserved as drop-off
    points and an agent parked on a dock would immediately block returning
    agents from completing their orders.  We skip dock cells during BFS
    expansion, preferring the open aisle cells directly above them.
    """
    from collections import deque as _deque

    dock_set = set(docks)
    positions: list[tuple[int, int]] = []
    used: set[tuple[int, int]] = set()

    # BFS frontier from all docks simultaneously
    queue: _deque[tuple[int, int]] = _deque()
    visited: set[tuple[int, int]] = set()
    for d in docks:
        queue.append(d)
        visited.add(d)

    while queue and len(positions) < n_agents:
        cell = queue.popleft()
        # Skip dock cells: agents start one step away so docks stay clear.
        if cell not in used and grid.traversable(*cell) and cell not in dock_set:
            positions.append(cell)
            used.add(cell)
        for nb in grid.traversable_neighbours(*cell):
            if nb not in visited:
                visited.add(nb)
                queue.append(nb)

    # Fallback: use dock positions only if the grid is too small
    while len(positions) < n_agents:
        positions.append(docks[len(positions) % len(docks)])

    return positions[:n_agents]
