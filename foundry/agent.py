"""
AMR (Autonomous Mobile Robot) agent model.

State machine:
    IDLE → MOVING_TO_PICK → PICKING → MOVING_TO_PICK (repeat per line)
         → MOVING_TO_DOCK → DROPPING → IDLE | MOVING_TO_CHARGE
    MOVING_TO_CHARGE → CHARGING → IDLE
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from foundry.grid import Cell, Grid


class AgentState(Enum):
    IDLE             = auto()
    MOVING_TO_PICK   = auto()
    PICKING          = auto()
    MOVING_TO_DOCK   = auto()
    DROPPING         = auto()
    MOVING_TO_CHARGE = auto()
    CHARGING         = auto()


PICK_TICKS             = 3      # ticks to complete one pick action
DROP_TICKS_PER_LINE    = 2      # ticks per payload line at drop-off
BATTERY_DRAIN_PER_STEP = 0.5    # % battery lost per movement step
BATTERY_CHARGE_PER_TICK= 2.0    # % battery gained per tick at charger
LOW_BATTERY_THRESHOLD  = 20.0   # go charge after drop if below this

# Congestion back-off: when a cell is blocked, an agent waits this many ticks
# before attempting to move again (staggered by priority so agents don't all
# retry simultaneously, which would just reproduce the same deadlock).
_CONGESTION_WAIT_BASE  = 2      # ticks to wait per congestion event
_CONGESTION_WAIT_JITTER = 3     # modulus for per-agent stagger (0, 1, 2 extra)


@dataclass
class PickTask:
    pick_pos: tuple[int, int]
    sku_id: str
    qty: int


class Agent:
    def __init__(
        self,
        agent_id: str,
        position: tuple[int, int],
        capacity_kg: float = 50.0,
        battery_pct: float = 100.0,
    ) -> None:
        self.agent_id     = agent_id
        self.position     = position
        self.capacity_kg  = capacity_kg
        self.battery_pct  = battery_pct

        self.state   = AgentState.IDLE
        self.payload: list[tuple[str, int]] = []   # [(sku_id, qty), ...]

        # Navigation
        self._path:   list[tuple[int, int]] = []
        self._target: Optional[tuple[int, int]] = None

        # Current order
        self._order_id:   Optional[str]       = None
        self._pick_tasks: list[PickTask]       = []
        self._dock_pos:   Optional[tuple[int, int]] = None

        # Action countdown (PICKING / DROPPING)
        self._action_ticks: int = 0

        # Congestion back-off counter: agent won't attempt movement while > 0
        self._congestion_wait: int = 0
        # Priority derived from agent ID number (AMR-00 → 0, AMR-01 → 1, …)
        try:
            self._priority: int = int(agent_id.split("-")[-1])
        except (ValueError, IndexError):
            self._priority = 0

        # Cumulative stats
        self.steps_taken:       int = 0
        self.orders_completed:  int = 0
        self.congestion_events: int = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def is_idle(self) -> bool:
        return self.state == AgentState.IDLE

    def assign_order(
        self,
        order_id: str,
        pick_tasks: list[PickTask],
        dock_pos: tuple[int, int],
        grid: Grid,
    ) -> None:
        self._order_id   = order_id
        self._pick_tasks = list(pick_tasks)
        self._dock_pos   = dock_pos
        self._begin_next_pick(grid, blocked=set())

    def tick(
        self,
        grid: Grid,
        occupied: set[tuple[int, int]],
        clock: int,
    ) -> list[dict]:
        """Advance agent one sim tick. Returns emitted events."""
        events: list[dict] = []

        if self.state == AgentState.IDLE:
            pass

        elif self.state == AgentState.MOVING_TO_PICK:
            self._move_step(grid, occupied, events)
            if self.position == self._target:
                self._action_ticks = PICK_TICKS
                self.state = AgentState.PICKING
                events.append(_ev("PICK_START", self, clock, pos=self.position,
                                  sku=self._pick_tasks[0].sku_id))
            elif not self._path:
                # Path exhausted but not arrived — replan on next tick
                self._navigate_to(self._target, grid, occupied)

        elif self.state == AgentState.PICKING:
            self._action_ticks -= 1
            if self._action_ticks <= 0:
                task = self._pick_tasks.pop(0)
                self.payload.append((task.sku_id, task.qty))
                events.append(_ev("PICK_DONE", self, clock, sku=task.sku_id))
                if self._pick_tasks:
                    self._begin_next_pick(grid, occupied)
                else:
                    self._begin_to_dock(grid, occupied)

        elif self.state == AgentState.MOVING_TO_DOCK:
            self._move_step(grid, occupied, events)
            if self.position == self._target:
                n_lines = len(self.payload)
                self._action_ticks = max(DROP_TICKS_PER_LINE * n_lines, 1)
                self.state = AgentState.DROPPING
                events.append(_ev("DROP_START", self, clock))
            elif not self._path:
                self._navigate_to(self._target, grid, occupied)

        elif self.state == AgentState.DROPPING:
            self._action_ticks -= 1
            if self._action_ticks <= 0:
                oid = self._order_id
                self.payload.clear()
                self._order_id = None
                self.orders_completed += 1
                events.append(_ev("ORDER_COMPLETE", self, clock, order_id=oid))
                if self.battery_pct < LOW_BATTERY_THRESHOLD:
                    self._begin_to_charge(grid, occupied)
                else:
                    self.state = AgentState.IDLE

        elif self.state == AgentState.MOVING_TO_CHARGE:
            self._move_step(grid, occupied, events)
            if self.position == self._target:
                self.state = AgentState.CHARGING
            elif not self._path:
                self._navigate_to(self._target, grid, occupied)

        elif self.state == AgentState.CHARGING:
            self.battery_pct = min(100.0, self.battery_pct + BATTERY_CHARGE_PER_TICK)
            if self.battery_pct >= 100.0:
                self.state = AgentState.IDLE
                events.append(_ev("CHARGED", self, clock))

        return events

    # ------------------------------------------------------------------
    # Navigation helpers
    # ------------------------------------------------------------------

    def _begin_next_pick(
        self, grid: Grid, blocked: set[tuple[int, int]]
    ) -> None:
        task = self._pick_tasks[0]
        self._navigate_to(task.pick_pos, grid, blocked)
        self.state = AgentState.MOVING_TO_PICK

    def _begin_to_dock(
        self, grid: Grid, blocked: set[tuple[int, int]]
    ) -> None:
        self._navigate_to(self._dock_pos, grid, blocked)
        self.state = AgentState.MOVING_TO_DOCK

    def _begin_to_charge(
        self, grid: Grid, blocked: set[tuple[int, int]]
    ) -> None:
        charger = _nearest_charger(grid, self.position)
        if charger is None:
            self.state = AgentState.IDLE
            return
        self._navigate_to(charger, grid, blocked)
        self.state = AgentState.MOVING_TO_CHARGE

    def _navigate_to(
        self,
        target: tuple[int, int],
        grid: Grid,
        blocked: set[tuple[int, int]],
    ) -> None:
        self._target = target
        if self.position == target:
            self._path = []
            return
        path = grid.astar(self.position, target, blocked - {self.position, target})
        if path is None:
            path = grid.astar(self.position, target)
        self._path = path[1:] if path else []

    def _move_step(
        self,
        grid: Grid,
        occupied: set[tuple[int, int]],
        events: list[dict],
    ) -> None:
        if not self._path:
            return

        # Back-off: agent yielding after a congestion event — just count down.
        if self._congestion_wait > 0:
            self._congestion_wait -= 1
            return

        next_cell = self._path[0]
        if next_cell not in occupied:
            self._path.pop(0)
            self.position = next_cell
            self.steps_taken += 1
            self.battery_pct = max(0.0, self.battery_pct - BATTERY_DRAIN_PER_STEP)
        else:
            # Cell blocked — record event, set a staggered wait, then let the
            # state machine's "path exhausted" branch replan after the wait.
            # Priority stagger ensures agents don't all retry on the same tick,
            # which breaks head-on and convoy deadlocks.
            self._congestion_wait = (
                _CONGESTION_WAIT_BASE + self._priority % _CONGESTION_WAIT_JITTER
            )
            self.congestion_events += 1
            events.append({"type": "CONGESTION", "agent": self.agent_id,
                           "pos": self.position, "blocked": next_cell})
            # Clear path so the state machine calls _navigate_to after the wait,
            # using the then-current occupied set rather than today's snapshot.
            self._path = []


# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------

def _nearest_charger(
    grid: Grid, pos: tuple[int, int]
) -> Optional[tuple[int, int]]:
    chargers = grid.cells_of_type(Cell.CHARGING)
    if not chargers:
        return None
    return min(chargers, key=lambda p: abs(p[0] - pos[0]) + abs(p[1] - pos[1]))


def _ev(event_type: str, agent: Agent, clock: int, **kwargs) -> dict:
    return {"type": event_type, "agent": agent.agent_id,
            "tick": clock, **kwargs}
