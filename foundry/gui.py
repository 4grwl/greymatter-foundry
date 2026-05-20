"""
Greymatter Foundry — tkinter GUI

Layout
------
┌─────────────────────────────────────────────────────┐
│  toolbar: [▶ Run] [⏸ Pause] [■ Stop]  Speed: [──○──] │
├──────────────────────────────────┬──────────────────┤
│                                  │  KPI panel       │
│   warehouse canvas               │  Agent legend    │
│   (Grid + agents)                │  Event log       │
│                                  │                  │
└──────────────────────────────────┴──────────────────┘

Colour scheme
─────────────
Cell types:
  AISLE    #F5F5F5  (near-white)
  FLOOR    #E0E0E0  (light grey)
  RACK     #5C5C7A  (dark blue-grey)
  DOCK     #4CAF50  (green)
  CHARGING #FFC107  (amber)

Agent states:
  IDLE              #9E9E9E  (grey)
  MOVING_TO_PICK    #2196F3  (blue)
  PICKING           #FF9800  (orange)
  MOVING_TO_DOCK    #03A9F4  (light blue)
  DROPPING          #8BC34A  (lime)
  MOVING_TO_CHARGE  #FF5722  (deep orange)
  CHARGING          #FFEB3B  (yellow)
"""
from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foundry.simulation import Simulation

# ── Colour maps ────────────────────────────────────────────────────────────────

CELL_COLOUR = {
    0: "#E8E8E8",   # FLOOR
    1: "#4A4A6A",   # RACK
    2: "#F8F8F8",   # AISLE
    3: "#43A047",   # DOCK
    4: "#FFB300",   # CHARGING
}

AGENT_COLOUR = {
    "IDLE":             "#9E9E9E",
    "MOVING_TO_PICK":   "#1E88E5",
    "PICKING":          "#FB8C00",
    "MOVING_TO_DOCK":   "#00ACC1",
    "DROPPING":         "#7CB342",
    "MOVING_TO_CHARGE": "#E53935",
    "CHARGING":         "#FDD835",
}

AGENT_OUTLINE = "#212121"
BG_DARK       = "#1E1E2E"
PANEL_BG      = "#2A2A3E"
TEXT_FG       = "#ECEFF4"
TEXT_DIM      = "#90A4AE"
ACCENT        = "#7C83FD"


class WarehouseGUI:
    """
    Self-contained tkinter window that visualises a running Simulation.

    Usage
    -----
    gui = WarehouseGUI(sim)
    gui.run()           # blocks until window closed
    """

    # Minimum cell size in pixels; auto-scales to fill available space
    MIN_CELL_PX = 6
    MAX_CELL_PX = 24

    def __init__(self, sim: Simulation, speed: float = 1.0) -> None:
        self.sim    = sim
        self.speed  = speed   # ticks per second (real time)

        self._running  = False
        self._stopped  = False
        self._thread: threading.Thread | None = None
        self._lock     = threading.Lock()

        self._build_window()

    # ── Window construction ────────────────────────────────────────────────────

    def _build_window(self) -> None:
        self.root = tk.Tk()
        self.root.title("Greymatter Foundry")
        self.root.configure(bg=BG_DARK)
        self.root.resizable(True, True)

        # ── Fonts ──
        try:
            mono = tkfont.Font(family="Menlo", size=11)
            mono_sm = tkfont.Font(family="Menlo", size=9)
            heading = tkfont.Font(family="Menlo", size=13, weight="bold")
        except Exception:
            mono = tkfont.Font(family="Courier", size=11)
            mono_sm = tkfont.Font(family="Courier", size=9)
            heading = tkfont.Font(family="Courier", size=13, weight="bold")

        self._fonts = {"mono": mono, "mono_sm": mono_sm, "heading": heading}

        # ── Toolbar ──
        toolbar = tk.Frame(self.root, bg=PANEL_BG, pady=6)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        title_lbl = tk.Label(toolbar, text="GREYMATTER FOUNDRY",
                             bg=PANEL_BG, fg=ACCENT, font=heading)
        title_lbl.pack(side=tk.LEFT, padx=12)

        self._tick_lbl = tk.Label(toolbar, text="tick 0",
                                  bg=PANEL_BG, fg=TEXT_DIM, font=mono_sm)
        self._tick_lbl.pack(side=tk.LEFT, padx=8)

        # Speed slider  (0.5× – 500×)
        tk.Label(toolbar, text="Speed:", bg=PANEL_BG, fg=TEXT_FG,
                 font=mono_sm).pack(side=tk.RIGHT, padx=(0, 4))
        self._speed_var = tk.DoubleVar(value=self.speed)
        speed_scale = tk.Scale(
            toolbar, from_=0.5, to=500, resolution=0.5,
            orient=tk.HORIZONTAL, length=160,
            variable=self._speed_var, bg=PANEL_BG, fg=TEXT_FG,
            troughcolor=BG_DARK, highlightthickness=0,
            command=self._on_speed_change,
        )
        speed_scale.pack(side=tk.RIGHT, padx=8)
        tk.Label(toolbar, text="ticks/s", bg=PANEL_BG, fg=TEXT_DIM,
                 font=mono_sm).pack(side=tk.RIGHT)

        # Control buttons
        btn_kw = dict(bg=PANEL_BG, font=mono,
                      relief=tk.FLAT, padx=10, pady=3, cursor="hand2")
        self._stop_btn  = tk.Button(toolbar, text="■  Stop",  fg=TEXT_FG, **btn_kw,
                                    command=self._on_stop)
        self._stop_btn.pack(side=tk.RIGHT, padx=4)
        self._pause_btn = tk.Button(toolbar, text="⏸  Pause", fg=TEXT_FG, **btn_kw,
                                    command=self._on_pause)
        self._pause_btn.pack(side=tk.RIGHT, padx=4)
        self._play_btn  = tk.Button(toolbar, text="▶  Run",   fg=ACCENT, **btn_kw,
                                    command=self._on_play)
        self._play_btn.pack(side=tk.RIGHT, padx=4)

        # ── Main content ──
        content = tk.Frame(self.root, bg=BG_DARK)
        content.pack(fill=tk.BOTH, expand=True)

        # Canvas (left, expands)
        self._canvas = tk.Canvas(content, bg=BG_DARK, highlightthickness=0)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)

        # Side panel (right, fixed width)
        side = tk.Frame(content, bg=PANEL_BG, width=220)
        side.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=8)
        side.pack_propagate(False)

        self._build_kpi_panel(side)
        self._build_legend(side)
        self._build_event_log(side)

        # Bind resize so canvas redraws when window resizes
        self._canvas.bind("<Configure>", lambda e: self._redraw())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_kpi_panel(self, parent: tk.Frame) -> None:
        frame = tk.Frame(parent, bg=PANEL_BG, pady=6)
        frame.pack(fill=tk.X, padx=8)

        tk.Label(frame, text="KPIs", bg=PANEL_BG, fg=ACCENT,
                 font=self._fonts["heading"]).pack(anchor=tk.W)
        tk.Frame(frame, bg=ACCENT, height=1).pack(fill=tk.X, pady=3)

        self._kpi_vars: dict[str, tk.StringVar] = {}
        rows = [
            ("orders_completed",  "Orders done"),
            ("orders_per_hour",   "OPH"),
            ("lines_per_hour",    "LPH"),
            ("avg_cycle_time_s",  "Cycle (s)"),
            ("total_travel_steps","Travel steps"),
            ("congestion_events", "Congestion"),
            ("avg_battery_pct",   "Avg battery %"),
        ]
        for key, label in rows:
            row_f = tk.Frame(frame, bg=PANEL_BG)
            row_f.pack(fill=tk.X, pady=1)
            tk.Label(row_f, text=f"{label}:", bg=PANEL_BG,
                     fg=TEXT_DIM, font=self._fonts["mono_sm"],
                     anchor=tk.W, width=14).pack(side=tk.LEFT)
            var = tk.StringVar(value="—")
            tk.Label(row_f, textvariable=var, bg=PANEL_BG,
                     fg=TEXT_FG, font=self._fonts["mono_sm"],
                     anchor=tk.E).pack(side=tk.RIGHT)
            self._kpi_vars[key] = var

    def _build_legend(self, parent: tk.Frame) -> None:
        frame = tk.Frame(parent, bg=PANEL_BG, pady=6)
        frame.pack(fill=tk.X, padx=8)

        tk.Label(frame, text="Agents", bg=PANEL_BG, fg=ACCENT,
                 font=self._fonts["heading"]).pack(anchor=tk.W)
        tk.Frame(frame, bg=ACCENT, height=1).pack(fill=tk.X, pady=3)

        short = {
            "IDLE": "Idle", "MOVING_TO_PICK": "→ Pick",
            "PICKING": "Picking", "MOVING_TO_DOCK": "→ Dock",
            "DROPPING": "Dropping", "MOVING_TO_CHARGE": "→ Charge",
            "CHARGING": "Charging",
        }
        for state, label in short.items():
            row = tk.Frame(frame, bg=PANEL_BG)
            row.pack(fill=tk.X, pady=1)
            dot = tk.Canvas(row, width=12, height=12, bg=PANEL_BG,
                            highlightthickness=0)
            dot.create_oval(2, 2, 10, 10, fill=AGENT_COLOUR[state], outline="")
            dot.pack(side=tk.LEFT, padx=(0, 4))
            tk.Label(row, text=label, bg=PANEL_BG, fg=TEXT_DIM,
                     font=self._fonts["mono_sm"]).pack(side=tk.LEFT)

    def _build_event_log(self, parent: tk.Frame) -> None:
        frame = tk.Frame(parent, bg=PANEL_BG, pady=6)
        frame.pack(fill=tk.BOTH, expand=True, padx=8)

        tk.Label(frame, text="Events", bg=PANEL_BG, fg=ACCENT,
                 font=self._fonts["heading"]).pack(anchor=tk.W)
        tk.Frame(frame, bg=ACCENT, height=1).pack(fill=tk.X, pady=3)

        self._log_text = tk.Text(
            frame, bg=BG_DARK, fg=TEXT_DIM, font=self._fonts["mono_sm"],
            relief=tk.FLAT, state=tk.DISABLED, wrap=tk.WORD,
        )
        self._log_text.pack(fill=tk.BOTH, expand=True)

    # ── Cell size computation ──────────────────────────────────────────────────

    def _cell_px(self) -> int:
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        if cw < 2 or ch < 2:
            return self.MIN_CELL_PX
        px = min(cw // self.sim.grid.cols, ch // self.sim.grid.rows)
        return max(self.MIN_CELL_PX, min(self.MAX_CELL_PX, px))

    # ── Drawing ───────────────────────────────────────────────────────────────

    def _redraw(self) -> None:
        self._canvas.delete("all")
        cell_px = self._cell_px()
        grid    = self.sim.grid

        # Draw grid cells
        for r in range(grid.rows):
            for c in range(grid.cols):
                x0 = c * cell_px
                y0 = r * cell_px
                colour = CELL_COLOUR.get(int(grid[r, c]), "#888888")
                self._canvas.create_rectangle(
                    x0, y0, x0 + cell_px, y0 + cell_px,
                    fill=colour, outline="", tags="cell",
                )

        # Draw agents
        margin = max(1, cell_px // 5)
        with self._lock:
            agents = list(self.sim.agents)

        for agent in agents:
            r, c     = agent.position
            state    = agent.state.name
            colour   = AGENT_COLOUR.get(state, "#FFFFFF")
            x0 = c * cell_px + margin
            y0 = r * cell_px + margin
            x1 = x0 + cell_px - 2 * margin
            y1 = y0 + cell_px - 2 * margin
            self._canvas.create_oval(
                x0, y0, x1, y1,
                fill=colour, outline=AGENT_OUTLINE, width=1,
                tags="agent",
            )

        # Update tick label
        self._tick_lbl.config(text=f"tick {self.sim.clock:,}")

        # Update KPIs
        with self._lock:
            kpis = self.sim.kpis.summary(self.sim.clock, self.sim.agents)
        for key, var in self._kpi_vars.items():
            val = kpis.get(key, "—")
            if isinstance(val, float):
                var.set(f"{val:.1f}")
            else:
                var.set(str(val))

    def _append_events(self, events: list[dict]) -> None:
        if not events:
            return
        lines = []
        for e in events[-8:]:
            t    = e.get("type", "?")
            a    = e.get("agent", "")
            tick = e.get("tick", "")
            lines.append(f"[{tick:>5}] {a} {t}\n")

        self._log_text.config(state=tk.NORMAL)
        for line in lines:
            self._log_text.insert(tk.END, line)
        # Keep last 200 lines
        line_count = int(self._log_text.index(tk.END).split(".")[0])
        if line_count > 200:
            self._log_text.delete("1.0", f"{line_count - 200}.0")
        self._log_text.see(tk.END)
        self._log_text.config(state=tk.DISABLED)

    # ── Simulation thread ──────────────────────────────────────────────────────

    def _sim_loop(self) -> None:
        """Runs in a background thread; calls step() in a loop."""
        while not self._stopped:
            if self._running:
                with self._lock:
                    evts = self.sim.step()

                # Schedule UI update on main thread
                self.root.after(0, self._redraw)
                self.root.after(0, lambda e=evts: self._append_events(e))

                # Throttle to requested speed (ticks per second)
                speed = max(0.01, self._speed_var.get())
                time.sleep(1.0 / speed)
            else:
                time.sleep(0.05)

    # ── Controls ──────────────────────────────────────────────────────────────

    def _on_play(self) -> None:
        self._running = True
        self._play_btn.config(fg=ACCENT)
        self._pause_btn.config(fg=TEXT_FG)

    def _on_pause(self) -> None:
        self._running = False
        self._pause_btn.config(fg=ACCENT)
        self._play_btn.config(fg=TEXT_FG)

    def _on_stop(self) -> None:
        self._running = False
        self._stopped = True

    def _on_speed_change(self, _val) -> None:
        pass  # slider variable is read directly in _sim_loop

    def _on_close(self) -> None:
        self._on_stop()
        self.root.destroy()

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the GUI (blocks until the window is closed)."""
        # Initial draw (window may not have final size yet — schedule for later)
        self.root.after(100, self._redraw)

        # Start sim thread
        self._thread = threading.Thread(target=self._sim_loop, daemon=True)
        self._thread.start()

        self.root.mainloop()


# ── Standalone entry point (for `python -m foundry.gui`) ──────────────────────

def launch(sim: Simulation, speed: float = 10.0) -> None:
    """Create and run the GUI for the given simulation."""
    WarehouseGUI(sim, speed=speed).run()
