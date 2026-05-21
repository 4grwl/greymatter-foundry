"""
Greymatter Foundry — tkinter GUI

Layout
------
┌──────────────────────────────────────────────────────────────┐
│  toolbar: [▶ Run] [⏸ Pause] [■ Stop]  Speed: [──○──]         │
├───────────────────────────────────┬──────────────────────────┤
│                                   │  KPI panel               │
│   warehouse canvas                │  Config (load / rebuild) │
│   (Grid + agents                  │  Optimal Layout toggle   │
│    + ABC overlay)                 │  Agent / class legend    │
│                                   │  Event log               │
└───────────────────────────────────┴──────────────────────────┘

Colour scheme
─────────────
Cell types (normal view):
  AISLE    #F8F8F8  (near-white)
  FLOOR    #E8E8E8  (light grey)
  RACK     #4A4A6A  (dark blue-grey)
  DOCK     #43A047  (green)
  CHARGING #FFB300  (amber)

Rack cells in Optimal Layout view (ABC demand class):
  A-class  #2E7D32  (dark green  — nearest to docks, hottest SKUs)
  B-class  #F57F17  (dark amber  — mid-range)
  C-class  #1565C0  (dark blue   — farthest, slowest movers)

Agent states:
  IDLE              #9E9E9E  (grey)
  MOVING_TO_PICK    #1E88E5  (blue)
  PICKING           #FB8C00  (orange)
  MOVING_TO_DOCK    #00ACC1  (light blue)
  DROPPING          #7CB342  (lime)
  MOVING_TO_CHARGE  #E53935  (deep orange)
  CHARGING          #FDD835  (yellow)
"""
from __future__ import annotations

import copy
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from tkinter import font as tkfont
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
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

# ABC demand-class colours used in the optimal layout overlay
ABC_COLOUR = {
    "A": "#2E7D32",   # dark green  — hot zone
    "B": "#F57F17",   # dark amber  — warm zone
    "C": "#1565C0",   # dark blue   — cold zone
}

AGENT_OUTLINE = "#212121"
BG_DARK       = "#1E1E2E"
PANEL_BG      = "#2A2A3E"
TEXT_FG       = "#ECEFF4"
TEXT_DIM      = "#90A4AE"
ACCENT        = "#7C83FD"
SUCCESS       = "#43A047"
WARN          = "#FB8C00"


class WarehouseGUI:
    """
    Self-contained tkinter window that visualises a running Simulation.

    Supports loading layout JSON / orders CSV files at runtime and
    rebuilding the simulation without restarting the process.
    An optional Optimal Layout overlay colours rack cells by ABC demand
    class (proximity to dock) so users can judge slot placement at a glance.

    Usage
    -----
    gui = WarehouseGUI(sim, speed=10.0, args=args)
    gui.run()   # blocks until window closed
    """

    MIN_CELL_PX = 6
    MAX_CELL_PX = 24

    def __init__(
        self,
        sim: "Simulation",
        speed: float = 1.0,
        args: "argparse.Namespace | None" = None,
    ) -> None:
        self.sim   = sim
        self.speed = speed
        self._args = args   # kept so we can rebuild with new file paths

        self._running  = False
        self._stopped  = False
        self._thread: threading.Thread | None = None
        self._lock     = threading.Lock()

        # Optimal layout overlay state
        self._show_optimal   = False
        self._optimal_overlay: dict[tuple[int, int], str] = {}   # pos → hex colour

        self._build_window()
        self._compute_optimal_overlay()

    # ── Window construction ────────────────────────────────────────────────────

    def _build_window(self) -> None:
        self.root = tk.Tk()
        self.root.title("Greymatter Foundry")
        self.root.configure(bg=BG_DARK)
        self.root.resizable(True, True)

        # ── Fonts ──
        try:
            mono    = tkfont.Font(family="Menlo", size=11)
            mono_sm = tkfont.Font(family="Menlo", size=9)
            heading = tkfont.Font(family="Menlo", size=13, weight="bold")
        except Exception:
            mono    = tkfont.Font(family="Courier", size=11)
            mono_sm = tkfont.Font(family="Courier", size=9)
            heading = tkfont.Font(family="Courier", size=13, weight="bold")

        self._fonts = {"mono": mono, "mono_sm": mono_sm, "heading": heading}

        # ── Toolbar ──
        toolbar = tk.Frame(self.root, bg=PANEL_BG, pady=6)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        tk.Label(toolbar, text="GREYMATTER FOUNDRY",
                 bg=PANEL_BG, fg=ACCENT, font=heading).pack(side=tk.LEFT, padx=12)

        self._tick_lbl = tk.Label(toolbar, text="tick 0",
                                  bg=PANEL_BG, fg=TEXT_DIM, font=mono_sm)
        self._tick_lbl.pack(side=tk.LEFT, padx=8)

        # Speed slider  (0.5 – 500 ticks/s)
        tk.Label(toolbar, text="Speed:", bg=PANEL_BG, fg=TEXT_FG,
                 font=mono_sm).pack(side=tk.RIGHT, padx=(0, 4))
        self._speed_var = tk.DoubleVar(value=self.speed)
        tk.Scale(
            toolbar, from_=0.5, to=500, resolution=0.5,
            orient=tk.HORIZONTAL, length=160,
            variable=self._speed_var, bg=PANEL_BG, fg=TEXT_FG,
            troughcolor=BG_DARK, highlightthickness=0,
            command=self._on_speed_change,
        ).pack(side=tk.RIGHT, padx=8)
        tk.Label(toolbar, text="ticks/s", bg=PANEL_BG, fg=TEXT_DIM,
                 font=mono_sm).pack(side=tk.RIGHT)

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

        self._canvas = tk.Canvas(content, bg=BG_DARK, highlightthickness=0)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)

        # Side panel (fixed width)
        side = tk.Frame(content, bg=PANEL_BG, width=230)
        side.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=8)
        side.pack_propagate(False)

        self._build_kpi_panel(side)
        self._build_config_panel(side)
        self._build_legend(side)
        self._build_event_log(side)

        self._canvas.bind("<Configure>", lambda e: self._redraw())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Side-panel sections ───────────────────────────────────────────────────

    def _build_kpi_panel(self, parent: tk.Frame) -> None:
        frame = tk.Frame(parent, bg=PANEL_BG, pady=6)
        frame.pack(fill=tk.X, padx=8)

        tk.Label(frame, text="KPIs", bg=PANEL_BG, fg=ACCENT,
                 font=self._fonts["heading"]).pack(anchor=tk.W)
        tk.Frame(frame, bg=ACCENT, height=1).pack(fill=tk.X, pady=3)

        self._kpi_vars: dict[str, tk.StringVar] = {}
        rows = [
            ("orders_completed",   "Orders done"),
            ("orders_per_hour",    "OPH"),
            ("lines_per_hour",     "LPH"),
            ("avg_cycle_time_s",   "Cycle (s)"),
            ("total_travel_steps", "Travel steps"),
            ("congestion_events",  "Congestion"),
            ("avg_battery_pct",    "Avg battery %"),
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

    def _build_config_panel(self, parent: tk.Frame) -> None:
        """File-loader + rebuild + optimal-layout toggle."""
        frame = tk.Frame(parent, bg=PANEL_BG, pady=6)
        frame.pack(fill=tk.X, padx=8)

        tk.Label(frame, text="Config", bg=PANEL_BG, fg=ACCENT,
                 font=self._fonts["heading"]).pack(anchor=tk.W)
        tk.Frame(frame, bg=ACCENT, height=1).pack(fill=tk.X, pady=3)

        # ── Layout file row ──
        lrow = tk.Frame(frame, bg=PANEL_BG)
        lrow.pack(fill=tk.X, pady=1)
        tk.Label(lrow, text="Layout:", bg=PANEL_BG, fg=TEXT_DIM,
                 font=self._fonts["mono_sm"], width=7,
                 anchor=tk.W).pack(side=tk.LEFT)
        layout_name = (
            Path(self._args.layout).name
            if self._args and getattr(self._args, "layout", None)
            else "default"
        )
        self._layout_var = tk.StringVar(value=layout_name)
        tk.Label(lrow, textvariable=self._layout_var, bg=PANEL_BG, fg=TEXT_FG,
                 font=self._fonts["mono_sm"],
                 anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(lrow, text="…", bg=PANEL_BG, fg=ACCENT, cursor="hand2",
                  font=self._fonts["mono_sm"], relief=tk.FLAT, padx=4,
                  command=self._load_layout).pack(side=tk.RIGHT)

        # ── Orders file row ──
        orow = tk.Frame(frame, bg=PANEL_BG)
        orow.pack(fill=tk.X, pady=1)
        tk.Label(orow, text="Orders:", bg=PANEL_BG, fg=TEXT_DIM,
                 font=self._fonts["mono_sm"], width=7,
                 anchor=tk.W).pack(side=tk.LEFT)
        orders_name = (
            Path(self._args.orders).name
            if self._args and getattr(self._args, "orders", None)
            else "generated"
        )
        self._orders_var = tk.StringVar(value=orders_name)
        tk.Label(orow, textvariable=self._orders_var, bg=PANEL_BG, fg=TEXT_FG,
                 font=self._fonts["mono_sm"],
                 anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(orow, text="…", bg=PANEL_BG, fg=ACCENT, cursor="hand2",
                  font=self._fonts["mono_sm"], relief=tk.FLAT, padx=4,
                  command=self._load_orders).pack(side=tk.RIGHT)

        # ── Status label (shows rebuild result) ──
        self._status_var = tk.StringVar(value="")
        self._status_lbl = tk.Label(frame, textvariable=self._status_var,
                                    bg=PANEL_BG, fg=SUCCESS,
                                    font=self._fonts["mono_sm"],
                                    anchor=tk.W, wraplength=210)
        self._status_lbl.pack(fill=tk.X, pady=(2, 0))

        # ── Rebuild button ──
        tk.Button(frame, text="↺  Rebuild Simulation",
                  bg=PANEL_BG, fg=TEXT_FG, cursor="hand2",
                  font=self._fonts["mono_sm"], relief=tk.FLAT,
                  command=self._rebuild_simulation).pack(fill=tk.X, pady=(4, 2))

        # ── Optimal layout toggle ──
        self._optimal_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            frame, text="Show Optimal Layout",
            bg=PANEL_BG, fg=TEXT_FG, selectcolor=BG_DARK,
            activebackground=PANEL_BG, activeforeground=ACCENT,
            font=self._fonts["mono_sm"],
            variable=self._optimal_var,
            command=self._on_toggle_optimal,
        ).pack(anchor=tk.W, pady=2)

    def _build_legend(self, parent: tk.Frame) -> None:
        frame = tk.Frame(parent, bg=PANEL_BG, pady=6)
        frame.pack(fill=tk.X, padx=8)

        self._legend_frame = frame   # kept so we can refresh it

        self._legend_title_var = tk.StringVar(value="Agents")
        tk.Label(frame, textvariable=self._legend_title_var,
                 bg=PANEL_BG, fg=ACCENT,
                 font=self._fonts["heading"]).pack(anchor=tk.W)
        tk.Frame(frame, bg=ACCENT, height=1).pack(fill=tk.X, pady=3)

        self._legend_body = tk.Frame(frame, bg=PANEL_BG)
        self._legend_body.pack(fill=tk.X)
        self._render_agent_legend()

    def _render_agent_legend(self) -> None:
        for w in self._legend_body.winfo_children():
            w.destroy()
        short = {
            "IDLE": "Idle", "MOVING_TO_PICK": "→ Pick",
            "PICKING": "Picking", "MOVING_TO_DOCK": "→ Dock",
            "DROPPING": "Dropping", "MOVING_TO_CHARGE": "→ Charge",
            "CHARGING": "Charging",
        }
        for state, label in short.items():
            row = tk.Frame(self._legend_body, bg=PANEL_BG)
            row.pack(fill=tk.X, pady=1)
            dot = tk.Canvas(row, width=12, height=12, bg=PANEL_BG,
                            highlightthickness=0)
            dot.create_oval(2, 2, 10, 10, fill=AGENT_COLOUR[state], outline="")
            dot.pack(side=tk.LEFT, padx=(0, 4))
            tk.Label(row, text=label, bg=PANEL_BG, fg=TEXT_DIM,
                     font=self._fonts["mono_sm"]).pack(side=tk.LEFT)

    def _render_abc_legend(self) -> None:
        for w in self._legend_body.winfo_children():
            w.destroy()
        items = [
            ("A-class", ABC_COLOUR["A"], "Nearest docks (hot)"),
            ("B-class", ABC_COLOUR["B"], "Mid-range"),
            ("C-class", ABC_COLOUR["C"], "Farthest (cold)"),
        ]
        for label, colour, desc in items:
            row = tk.Frame(self._legend_body, bg=PANEL_BG)
            row.pack(fill=tk.X, pady=2)
            swatch = tk.Canvas(row, width=12, height=12, bg=PANEL_BG,
                               highlightthickness=0)
            swatch.create_rectangle(1, 1, 11, 11, fill=colour, outline="")
            swatch.pack(side=tk.LEFT, padx=(0, 4))
            tk.Label(row, text=f"{label}  {desc}",
                     bg=PANEL_BG, fg=TEXT_DIM,
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

    # ── Optimal layout overlay ────────────────────────────────────────────────

    def _compute_optimal_overlay(self) -> None:
        """
        Build a colour map from rack-cell position → ABC class colour.

        Strategy: rank every accessible rack cell (one that has a traversable
        pick neighbour) by its Manhattan distance to the nearest DOCK.
        The nearest third is A-class (prime slotting zone), the middle third
        is B-class, and the farthest third is C-class.
        """
        from foundry.grid import Cell
        from foundry.inventory import _find_pick_pos

        grid  = self.sim.grid
        docks = grid.cells_of_type(Cell.DOCK)
        if not docks:
            self._optimal_overlay = {}
            return

        accessible = [
            pos for pos in grid.cells_of_type(Cell.RACK)
            if _find_pick_pos(grid, pos) is not None
        ]
        if not accessible:
            self._optimal_overlay = {}
            return

        def dock_dist(pos: tuple[int, int]) -> int:
            return min(abs(pos[0] - d[0]) + abs(pos[1] - d[1]) for d in docks)

        accessible.sort(key=dock_dist)
        n = len(accessible)
        overlay: dict[tuple[int, int], str] = {}
        for i, pos in enumerate(accessible):
            t = i / n
            if t < 1 / 3:
                overlay[pos] = ABC_COLOUR["A"]
            elif t < 2 / 3:
                overlay[pos] = ABC_COLOUR["B"]
            else:
                overlay[pos] = ABC_COLOUR["C"]

        self._optimal_overlay = overlay

    # ── Cell size ─────────────────────────────────────────────────────────────

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
        use_abc = self._show_optimal and bool(self._optimal_overlay)

        for r in range(grid.rows):
            for c in range(grid.cols):
                pos    = (r, c)
                ctype  = int(grid[r, c])
                if use_abc and pos in self._optimal_overlay:
                    colour = self._optimal_overlay[pos]
                else:
                    colour = CELL_COLOUR.get(ctype, "#888888")
                self._canvas.create_rectangle(
                    c * cell_px, r * cell_px,
                    c * cell_px + cell_px, r * cell_px + cell_px,
                    fill=colour, outline="", tags="cell",
                )

        margin = max(1, cell_px // 5)
        with self._lock:
            agents = list(self.sim.agents)

        for agent in agents:
            r, c   = agent.position
            colour = AGENT_COLOUR.get(agent.state.name, "#FFFFFF")
            x0 = c * cell_px + margin
            y0 = r * cell_px + margin
            self._canvas.create_oval(
                x0, y0,
                x0 + cell_px - 2 * margin,
                y0 + cell_px - 2 * margin,
                fill=colour, outline=AGENT_OUTLINE, width=1, tags="agent",
            )

        self._tick_lbl.config(text=f"tick {self.sim.clock:,}")

        with self._lock:
            kpis = self.sim.kpis.summary(self.sim.clock, self.sim.agents)
        for key, var in self._kpi_vars.items():
            val = kpis.get(key, "—")
            var.set(f"{val:.1f}" if isinstance(val, float) else str(val))

    def _append_events(self, events: list[dict]) -> None:
        if not events:
            return
        lines = [
            f"[{e.get('tick', ''):>5}] {e.get('agent', '')} {e.get('type', '?')}\n"
            for e in events[-8:]
        ]
        self._log_text.config(state=tk.NORMAL)
        for line in lines:
            self._log_text.insert(tk.END, line)
        line_count = int(self._log_text.index(tk.END).split(".")[0])
        if line_count > 200:
            self._log_text.delete("1.0", f"{line_count - 200}.0")
        self._log_text.see(tk.END)
        self._log_text.config(state=tk.DISABLED)

    # ── Simulation thread ──────────────────────────────────────────────────────

    def _sim_loop(self) -> None:
        while not self._stopped:
            if self._running:
                with self._lock:
                    evts = self.sim.step()
                self.root.after(0, self._redraw)
                self.root.after(0, lambda e=evts: self._append_events(e))
                time.sleep(1.0 / max(0.01, self._speed_var.get()))
            else:
                time.sleep(0.05)

    # ── File loading ──────────────────────────────────────────────────────────

    def _load_layout(self) -> None:
        """Open a file picker for a warehouse layout JSON."""
        initial = str(Path(self._args.layout).parent) if (
            self._args and getattr(self._args, "layout", None)
        ) else "layouts"
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Load warehouse layout",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialdir=initial,
        )
        if not path:
            return
        if self._args is None:
            import argparse as _ap
            self._args = _ap.Namespace()
        self._args.layout = path
        self._layout_var.set(Path(path).name)
        self._set_status(f"Layout: {Path(path).name}", ok=True)

    def _load_orders(self) -> None:
        """Open a file picker for an orders CSV."""
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Load orders CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialdir="data",
        )
        if not path:
            return
        if self._args is None:
            import argparse as _ap
            self._args = _ap.Namespace()
        self._args.orders = path
        self._orders_var.set(Path(path).name)
        self._set_status(f"Orders: {Path(path).name}", ok=True)

    # ── Rebuild ───────────────────────────────────────────────────────────────

    def _rebuild_simulation(self) -> None:
        """Stop the running sim, build a fresh one from current file paths,
        recompute the ABC overlay, and resume if we were running."""
        if self._args is None:
            self._set_status("No config loaded yet.", ok=False)
            return

        was_running = self._running
        self._running = False
        time.sleep(0.12)   # give _sim_loop one sleep cycle to idle

        try:
            from foundry.__main__ import build_simulation

            # Build on a copy so partial failures don't corrupt self._args
            new_args = copy.copy(self._args)
            # Ensure quiet so rebuild output doesn't clutter terminal
            new_args.quiet = False

            new_sim = build_simulation(new_args)

            with self._lock:
                self.sim = new_sim

            self._compute_optimal_overlay()
            self.root.after(0, self._redraw)
            self._set_status("Simulation rebuilt ✓", ok=True)

        except Exception as exc:
            self._set_status(f"Rebuild failed: {exc}", ok=False)
            self._running = was_running
            return

        if was_running:
            self._running = True

    # ── Optimal layout toggle ─────────────────────────────────────────────────

    def _on_toggle_optimal(self) -> None:
        self._show_optimal = self._optimal_var.get()
        if self._show_optimal:
            self._legend_title_var.set("Rack zones")
            self._render_abc_legend()
        else:
            self._legend_title_var.set("Agents")
            self._render_agent_legend()
        self._redraw()

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
        pass   # read directly from _speed_var in _sim_loop

    def _on_close(self) -> None:
        self._on_stop()
        self.root.destroy()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, msg: str, *, ok: bool) -> None:
        self._status_var.set(msg)
        self._status_lbl.config(fg=SUCCESS if ok else WARN)
        # Auto-clear after 4 s
        self.root.after(4000, lambda: self._status_var.set(""))

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the GUI — blocks until the window is closed."""
        self.root.after(100, self._redraw)
        self._thread = threading.Thread(target=self._sim_loop, daemon=True)
        self._thread.start()
        self.root.mainloop()


# ── Standalone entry point ────────────────────────────────────────────────────

def launch(
    sim: "Simulation",
    speed: float = 10.0,
    args: "argparse.Namespace | None" = None,
) -> None:
    """Create and run the GUI for the given simulation."""
    WarehouseGUI(sim, speed=speed, args=args).run()
