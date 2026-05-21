"""
Greymatter Foundry — browser-based warehouse GUI.

Architecture
─────────────
  Python side                    Browser side
  ───────────                    ────────────
  WarehouseWebServer
    └─ stdlib HTTPServer         GET /          → HTML page
    └─ SimState (thread-safe)    GET /stream    → SSE  (tick updates)
         ↑ background sim-loop   GET /grid      → JSON (grid layout, one-time)
                                 GET /overlay   → JSON (ABC class map)
                                 POST /control  → JSON {action: play|pause|stop}
                                 POST /upload   → multipart (layout.json / orders.csv)
                                 POST /rebuild  → JSON (rebuild from current paths)

Run standalone
──────────────
  python -m foundry --web [--web-port 5050] [other flags]

Or import and call:
  from foundry.web import launch
  launch(sim, port=5050, args=args)
"""
from __future__ import annotations

import cgi
import copy
import http.server
import json
import os
import socketserver
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    import argparse
    from foundry.simulation import Simulation

# ── Embedded single-page app ──────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Greymatter Foundry</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body   { background: #1E1E2E; color: #ECEFF4; font-family: 'Menlo','Courier New',monospace;
           font-size: 12px; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

  /* ── toolbar ── */
  #toolbar { background: #2A2A3E; padding: 8px 14px; display: flex; align-items: center;
             gap: 10px; flex-shrink: 0; flex-wrap: wrap; }
  #toolbar h1  { color: #7C83FD; font-size: 13px; letter-spacing: 1px; margin-right: 6px; }
  #tick-lbl    { color: #90A4AE; font-size: 11px; flex: 1; }
  .btn { background: #2A2A3E; color: #ECEFF4; border: 1px solid #444; border-radius: 4px;
         padding: 4px 12px; cursor: pointer; font-family: inherit; font-size: 12px; }
  .btn:hover   { background: #3A3A50; }
  .btn.active  { color: #7C83FD; border-color: #7C83FD; }
  #speed-wrap  { display: flex; align-items: center; gap: 6px; color: #90A4AE; }
  #speed-range { accent-color: #7C83FD; width: 120px; }

  /* ── main layout ── */
  #main { display: flex; flex: 1; overflow: hidden; gap: 0; }
  #canvas-wrap { flex: 1; display: flex; align-items: center; justify-content: center;
                 padding: 10px; overflow: hidden; }
  canvas { image-rendering: pixelated; border-radius: 4px; }

  /* ── side panel ── */
  #side { width: 240px; background: #2A2A3E; overflow-y: auto; padding: 8px 10px; flex-shrink: 0; }
  .section-title { color: #7C83FD; font-size: 12px; font-weight: bold; margin-top: 10px; margin-bottom: 2px; }
  .divider { height: 1px; background: #7C83FD; margin-bottom: 6px; }
  .kpi-row { display: flex; justify-content: space-between; padding: 1px 0; color: #90A4AE; }
  .kpi-val { color: #ECEFF4; }

  /* config panel */
  .file-row { display: flex; align-items: center; gap: 4px; margin: 3px 0; }
  .file-lbl { color: #90A4AE; width: 54px; flex-shrink: 0; }
  .file-name { color: #ECEFF4; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .file-btn  { background: #1E1E2E; color: #7C83FD; border: none; border-radius: 3px;
               padding: 2px 7px; cursor: pointer; font-family: inherit; font-size: 11px; }
  .file-btn:hover { background: #2e2e4e; }
  #status-lbl { font-size: 11px; min-height: 16px; margin: 3px 0; }
  .wide-btn { width: 100%; background: #1E1E2E; color: #ECEFF4; border: 1px solid #444;
              border-radius: 4px; padding: 4px 0; cursor: pointer; font-family: inherit;
              font-size: 11px; margin: 3px 0; }
  .wide-btn:hover { background: #2e2e4e; }
  #optimal-chk { accent-color: #7C83FD; margin-right: 5px; }

  /* legend */
  .legend-row { display: flex; align-items: center; gap: 6px; padding: 2px 0; color: #90A4AE; }
  .swatch { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }
  .swatch.rect { border-radius: 2px; }

  /* event log */
  #log { background: #1E1E2E; border-radius: 3px; padding: 4px; height: 140px;
         overflow-y: auto; font-size: 10px; color: #90A4AE; line-height: 1.5; }
</style>
</head>
<body>

<!-- toolbar -->
<div id="toolbar">
  <h1>GREYMATTER FOUNDRY</h1>
  <span id="tick-lbl">tick 0</span>
  <button class="btn active" id="btn-run"   onclick="ctrl('play')">▶ Run</button>
  <button class="btn"        id="btn-pause" onclick="ctrl('pause')">⏸ Pause</button>
  <button class="btn"        id="btn-stop"  onclick="ctrl('stop')">■ Stop</button>
  <div id="speed-wrap">
    <span>Speed:</span>
    <input type="range" id="speed-range" min="0.5" max="500" step="0.5" value="10">
    <span id="speed-val">10</span> ticks/s
  </div>
</div>

<!-- main -->
<div id="main">
  <div id="canvas-wrap"><canvas id="wh-canvas"></canvas></div>

  <div id="side">
    <!-- KPIs -->
    <div class="section-title">KPIs</div><div class="divider"></div>
    <div class="kpi-row"><span>Orders done</span>  <span class="kpi-val" id="k-orders">—</span></div>
    <div class="kpi-row"><span>OPH</span>          <span class="kpi-val" id="k-oph">—</span></div>
    <div class="kpi-row"><span>LPH</span>          <span class="kpi-val" id="k-lph">—</span></div>
    <div class="kpi-row"><span>Cycle (s)</span>    <span class="kpi-val" id="k-cycle">—</span></div>
    <div class="kpi-row"><span>Travel steps</span> <span class="kpi-val" id="k-travel">—</span></div>
    <div class="kpi-row"><span>Congestion</span>   <span class="kpi-val" id="k-cong">—</span></div>
    <div class="kpi-row"><span>Avg battery %</span><span class="kpi-val" id="k-bat">—</span></div>

    <!-- Config -->
    <div class="section-title" style="margin-top:14px">Config</div><div class="divider"></div>
    <div class="file-row">
      <span class="file-lbl">Layout:</span>
      <span class="file-name" id="layout-name">default</span>
      <label class="file-btn">…<input type="file" id="layout-file" accept=".json"
             style="display:none" onchange="uploadFile('layout',this)"></label>
    </div>
    <div class="file-row">
      <span class="file-lbl">Orders:</span>
      <span class="file-name" id="orders-name">generated</span>
      <label class="file-btn">…<input type="file" id="orders-file" accept=".csv"
             style="display:none" onchange="uploadFile('orders',this)"></label>
    </div>
    <div id="status-lbl"></div>
    <button class="wide-btn" onclick="rebuild()">↺ Rebuild Simulation</button>
    <label style="cursor:pointer;display:flex;align-items:center;margin:4px 0;color:#ECEFF4">
      <input type="checkbox" id="optimal-chk" onchange="toggleOptimal()">
      Show Optimal Layout
    </label>

    <!-- Legend -->
    <div class="section-title" style="margin-top:14px" id="legend-title">Agents</div>
    <div class="divider"></div>
    <div id="legend-body"></div>

    <!-- Events -->
    <div class="section-title" style="margin-top:14px">Events</div><div class="divider"></div>
    <div id="log"></div>
  </div>
</div>

<script>
// ── colour maps ───────────────────────────────────────────────────────────────
const CELL_COLOUR = {0:'#E8E8E8', 1:'#4A4A6A', 2:'#F8F8F8', 3:'#43A047', 4:'#FFB300'};
const AGENT_COLOUR = {
  IDLE:'#9E9E9E', MOVING_TO_PICK:'#1E88E5', PICKING:'#FB8C00',
  MOVING_TO_DOCK:'#00ACC1', DROPPING:'#7CB342',
  MOVING_TO_CHARGE:'#E53935', CHARGING:'#FDD835'
};
const ABC_COLOUR = {A:'#2E7D32', B:'#F57F17', C:'#1565C0'};

// ── state ──────────────────────────────────────────────────────────────────────
let gridData = null;        // {rows, cols, cells}
let overlayData = null;     // {"r,c": "A"|"B"|"C"}
let showOptimal = false;
let cellPx = 10;
const canvas = document.getElementById('wh-canvas');
const ctx    = canvas.getContext('2d');
let latestAgents = [];

// ── fetch grid once ────────────────────────────────────────────────────────────
async function fetchGrid() {
  const r = await fetch('/grid');
  gridData = await r.json();
  resizeCanvas();
  drawGrid();
}

async function fetchOverlay() {
  const r = await fetch('/overlay');
  overlayData = await r.json();
}

// ── canvas sizing ──────────────────────────────────────────────────────────────
function resizeCanvas() {
  if (!gridData) return;
  const wrap = document.getElementById('canvas-wrap');
  const maxW = wrap.clientWidth  - 20;
  const maxH = wrap.clientHeight - 20;
  cellPx = Math.max(6, Math.min(24,
    Math.min(Math.floor(maxW / gridData.cols), Math.floor(maxH / gridData.rows))
  ));
  canvas.width  = gridData.cols * cellPx;
  canvas.height = gridData.rows * cellPx;
}
window.addEventListener('resize', () => { resizeCanvas(); redraw(); });

// ── drawing ────────────────────────────────────────────────────────────────────
function drawGrid() {
  if (!gridData) return;
  const rows = gridData.rows, cols = gridData.cols;
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const key = r + ',' + c;
      let colour;
      if (showOptimal && overlayData && overlayData[key]) {
        colour = ABC_COLOUR[overlayData[key]];
      } else {
        colour = CELL_COLOUR[gridData.cells[r][c]] || '#888';
      }
      ctx.fillStyle = colour;
      ctx.fillRect(c * cellPx, r * cellPx, cellPx, cellPx);
    }
  }
}

function drawAgents(agents) {
  latestAgents = agents;
  const m = Math.max(1, Math.floor(cellPx / 5));
  for (const ag of agents) {
    const x = ag.c * cellPx + m, y = ag.r * cellPx + m;
    const d = cellPx - 2 * m;
    ctx.fillStyle   = AGENT_COLOUR[ag.state] || '#FFF';
    ctx.strokeStyle = '#212121';
    ctx.lineWidth   = 1;
    ctx.beginPath();
    ctx.ellipse(x + d/2, y + d/2, d/2, d/2, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
  }
}

function redraw() {
  drawGrid();
  drawAgents(latestAgents);
}

// ── SSE stream ─────────────────────────────────────────────────────────────────
function startStream() {
  const es = new EventSource('/stream');
  es.onmessage = e => {
    const d = JSON.parse(e.data);
    // tick
    document.getElementById('tick-lbl').textContent = 'tick ' + d.tick.toLocaleString();
    // KPIs
    const k = d.kpis;
    document.getElementById('k-orders').textContent = k.orders_completed ?? '—';
    document.getElementById('k-oph').textContent    = k.orders_per_hour != null ? k.orders_per_hour.toFixed(1) : '—';
    document.getElementById('k-lph').textContent    = k.lines_per_hour  != null ? k.lines_per_hour.toFixed(1)  : '—';
    document.getElementById('k-cycle').textContent  = k.avg_cycle_time_s  != null ? k.avg_cycle_time_s.toFixed(0) : '—';
    document.getElementById('k-travel').textContent = k.total_travel_steps ?? '—';
    document.getElementById('k-cong').textContent   = k.congestion_events  ?? '—';
    document.getElementById('k-bat').textContent    = k.avg_battery_pct != null ? k.avg_battery_pct.toFixed(1) : '—';
    // canvas
    drawGrid();
    drawAgents(d.agents);
    // events
    if (d.events && d.events.length) appendEvents(d.events);
  };
  es.onerror = () => setTimeout(startStream, 2000);
}

// ── event log ──────────────────────────────────────────────────────────────────
function appendEvents(evts) {
  const log = document.getElementById('log');
  for (const e of evts) {
    const line = document.createElement('div');
    line.textContent = `[${String(e.tick || '').padStart(5)}] ${e.agent || ''} ${e.type || ''}`;
    log.appendChild(line);
  }
  while (log.children.length > 200) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

// ── controls ───────────────────────────────────────────────────────────────────
async function ctrl(action) {
  await fetch('/control', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action})});
  document.getElementById('btn-run').classList.toggle('active',   action === 'play');
  document.getElementById('btn-pause').classList.toggle('active', action === 'pause');
}

document.getElementById('speed-range').addEventListener('input', function() {
  document.getElementById('speed-val').textContent = this.value;
  fetch('/control', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action:'speed', value: parseFloat(this.value)})});
});

// ── file upload ────────────────────────────────────────────────────────────────
async function uploadFile(kind, input) {
  if (!input.files.length) return;
  const fd = new FormData();
  fd.append('file', input.files[0]);
  fd.append('kind', kind);
  const r = await fetch('/upload', {method:'POST', body: fd});
  const d = await r.json();
  if (d.ok) {
    document.getElementById(kind + '-name').textContent = d.filename;
    setStatus(kind + ' loaded: ' + d.filename, true);
  } else {
    setStatus('Upload failed: ' + d.error, false);
  }
}

// ── rebuild ────────────────────────────────────────────────────────────────────
async function rebuild() {
  setStatus('Rebuilding…', true);
  const r = await fetch('/rebuild', {method:'POST'});
  const d = await r.json();
  if (d.ok) {
    await fetchGrid();
    await fetchOverlay();
    redraw();
    setStatus('Simulation rebuilt ✓', true);
  } else {
    setStatus('Rebuild failed: ' + d.error, false);
  }
}

// ── optimal layout ─────────────────────────────────────────────────────────────
async function toggleOptimal() {
  showOptimal = document.getElementById('optimal-chk').checked;
  if (showOptimal && !overlayData) await fetchOverlay();
  renderLegend();
  redraw();
}

function renderLegend() {
  const body  = document.getElementById('legend-body');
  const title = document.getElementById('legend-title');
  body.innerHTML = '';
  if (showOptimal) {
    title.textContent = 'Rack zones';
    [['A', ABC_COLOUR.A, 'Nearest docks (hot)'],
     ['B', ABC_COLOUR.B, 'Mid-range'],
     ['C', ABC_COLOUR.C, 'Farthest (cold)']].forEach(([lbl, col, desc]) => {
      body.insertAdjacentHTML('beforeend',
        `<div class="legend-row"><div class="swatch rect" style="background:${col}"></div>
         <span>${lbl}-class — ${desc}</span></div>`);
    });
  } else {
    title.textContent = 'Agents';
    Object.entries(AGENT_COLOUR).forEach(([state, col]) => {
      const label = state.replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
      body.insertAdjacentHTML('beforeend',
        `<div class="legend-row"><div class="swatch" style="background:${col}"></div>
         <span>${label}</span></div>`);
    });
  }
}

// ── status ─────────────────────────────────────────────────────────────────────
let _statusTimer = null;
function setStatus(msg, ok) {
  const el = document.getElementById('status-lbl');
  el.textContent = msg;
  el.style.color = ok ? '#43A047' : '#FB8C00';
  clearTimeout(_statusTimer);
  _statusTimer = setTimeout(() => { el.textContent = ''; }, 4000);
}

// ── boot ───────────────────────────────────────────────────────────────────────
renderLegend();
fetchGrid().then(() => startStream());
</script>
</body>
</html>
"""


# ── Simulation state (thread-safe) ────────────────────────────────────────────

class SimState:
    """Shared mutable state accessed by both the HTTP handler and sim thread."""

    def __init__(self, sim: "Simulation", args: "argparse.Namespace | None") -> None:
        self.sim   = sim
        self.args  = args
        self.lock  = threading.Lock()

        self.running  = False
        self.stopped  = False
        self.speed    = 10.0   # ticks per second

        # File paths for rebuild (may be updated by /upload)
        self._layout_path: str | None = (
            args.layout if args and getattr(args, "layout", None) else None
        )
        self._orders_path: str | None = (
            args.orders if args and getattr(args, "orders", None) else None
        )

        # SSE subscribers: list of (queue) writable sockets
        self._subscribers: list[threading.Event] = []
        self._latest_frame: bytes = b""

    # ── SSE broadcast ──────────────────────────────────────────────────────

    def push_frame(self, data: dict) -> None:
        frame = ("data: " + json.dumps(data) + "\n\n").encode()
        self._latest_frame = frame
        for ev in list(self._subscribers):
            ev.set()

    def subscribe(self) -> threading.Event:
        ev = threading.Event()
        self._subscribers.append(ev)
        return ev

    def unsubscribe(self, ev: threading.Event) -> None:
        try:
            self._subscribers.remove(ev)
        except ValueError:
            pass

    # ── Overlay ────────────────────────────────────────────────────────────

    def compute_overlay(self) -> dict[str, str]:
        """Return {\"r,c\": \"A\"|\"B\"|\"C\"} for accessible rack cells."""
        from foundry.grid import Cell
        from foundry.inventory import _find_pick_pos

        grid  = self.sim.grid
        docks = grid.cells_of_type(Cell.DOCK)
        if not docks:
            return {}

        accessible = [
            pos for pos in grid.cells_of_type(Cell.RACK)
            if _find_pick_pos(grid, pos) is not None
        ]
        if not accessible:
            return {}

        def dock_dist(pos: tuple) -> int:
            return min(abs(pos[0]-d[0]) + abs(pos[1]-d[1]) for d in docks)

        accessible.sort(key=dock_dist)
        n = len(accessible)
        result = {}
        for i, (r, c) in enumerate(accessible):
            t = i / n
            result[f"{r},{c}"] = "A" if t < 1/3 else ("B" if t < 2/3 else "C")
        return result

    # ── Rebuild ────────────────────────────────────────────────────────────

    def rebuild(self) -> str | None:
        """Rebuild simulation from stored paths. Returns error string or None."""
        if self.args is None:
            return "No args available"
        try:
            from foundry.__main__ import build_simulation
            new_args = copy.copy(self.args)
            if self._layout_path:
                new_args.layout = self._layout_path
            if self._orders_path:
                new_args.orders = self._orders_path
            new_args.quiet = True
            new_sim = build_simulation(new_args)
            with self.lock:
                self.sim = new_sim
            return None
        except Exception as exc:
            return str(exc)


# ── HTTP handler ──────────────────────────────────────────────────────────────

class _Handler(http.server.BaseHTTPRequestHandler):
    # Injected before server starts
    state: SimState

    def log_message(self, fmt, *args):  # silence access log
        pass

    # ── GET ───────────────────────────────────────────────────────────────

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._html()
        elif path == "/stream":
            self._sse()
        elif path == "/grid":
            self._grid()
        elif path == "/overlay":
            self._json(self.state.compute_overlay())
        else:
            self.send_error(404)

    def _html(self):
        body = _HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        ev = self.state.subscribe()
        try:
            # Send the latest frame immediately on connect
            if self.state._latest_frame:
                self.wfile.write(self.state._latest_frame)
                self.wfile.flush()
            while not self.state.stopped:
                if ev.wait(timeout=1.0):
                    ev.clear()
                    self.wfile.write(self.state._latest_frame)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.state.unsubscribe(ev)

    def _grid(self):
        with self.state.lock:
            grid = self.state.sim.grid
            cells = [
                [int(grid[r, c]) for c in range(grid.cols)]
                for r in range(grid.rows)
            ]
        self._json({"rows": grid.rows, "cols": grid.cols, "cells": cells})

    # ── POST ──────────────────────────────────────────────────────────────

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/control":
            self._control()
        elif path == "/upload":
            self._upload()
        elif path == "/rebuild":
            self._rebuild()
        else:
            self.send_error(404)

    def _control(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length) or b"{}")
        action = body.get("action", "")
        if action == "play":
            self.state.running = True
        elif action == "pause":
            self.state.running = False
        elif action == "stop":
            self.state.running = False
            self.state.stopped = True
        elif action == "speed":
            self.state.speed = max(0.01, float(body.get("value", 10)))
        self._json({"ok": True})

    def _upload(self):
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0))
        raw  = self.rfile.read(length)

        # Parse multipart
        boundary = None
        for part in ctype.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[len("boundary="):]
                break
        if not boundary:
            self._json({"ok": False, "error": "no boundary"})
            return

        import email
        msg = email.message_from_bytes(
            b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw
        )
        kind = filename = filedata = None
        for part in msg.get_payload():
            cd = part.get("Content-Disposition", "")
            name_match = None
            for seg in cd.split(";"):
                seg = seg.strip()
                if seg.startswith("name="):
                    name_match = seg[5:].strip('"')
                elif seg.startswith("filename="):
                    filename = seg[9:].strip('"')
            if name_match == "kind":
                kind = part.get_payload().strip()
            elif name_match == "file":
                filedata = part.get_payload(decode=True)

        if not kind or filedata is None:
            self._json({"ok": False, "error": "missing fields"})
            return

        suffix = ".json" if kind == "layout" else ".csv"
        fd, tmp = tempfile.mkstemp(suffix=suffix)
        try:
            os.write(fd, filedata)
            os.close(fd)
            if kind == "layout":
                self.state._layout_path = tmp
                self.state._layout_name = filename or Path(tmp).name
            else:
                self.state._orders_path = tmp
                self.state._orders_name = filename or Path(tmp).name
            self._json({"ok": True, "filename": filename or Path(tmp).name})
        except Exception as exc:
            os.close(fd)
            os.unlink(tmp)
            self._json({"ok": False, "error": str(exc)})

    def _rebuild(self):
        err = self.state.rebuild()
        if err:
            self._json({"ok": False, "error": err})
        else:
            self._json({"ok": True})

    # ── helpers ───────────────────────────────────────────────────────────

    def _json(self, data: dict):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── Simulation loop (background thread) ──────────────────────────────────────

def _sim_loop(state: SimState) -> None:
    while not state.stopped:
        if state.running:
            with state.lock:
                evts = state.sim.step()
                tick   = state.sim.clock
                agents = [
                    {"id": a.agent_id, "r": a.position[0], "c": a.position[1],
                     "state": a.state.name}
                    for a in state.sim.agents
                ]
                kpis = state.sim.kpis.summary(tick, state.sim.agents)

            frame = {
                "tick":   tick,
                "agents": agents,
                "kpis":   kpis,
                "events": evts[-8:],
            }
            state.push_frame(frame)
            time.sleep(1.0 / state.speed)
        else:
            time.sleep(0.05)


# ── Public entry point ────────────────────────────────────────────────────────

class WarehouseWebServer:
    """Wraps the HTTP server and sim thread."""

    def __init__(
        self,
        sim: "Simulation",
        port: int = 5050,
        args: "argparse.Namespace | None" = None,
        speed: float = 10.0,
    ) -> None:
        self.port  = port
        self.state = SimState(sim, args)
        self.state.speed = speed

    def run(self) -> None:
        # Inject state into handler class (socketserver uses class-level attrs)
        _Handler.state = self.state

        with socketserver.ThreadingTCPServer(("", self.port), _Handler) as srv:
            srv.allow_reuse_address = True
            print(f"Foundry web GUI → http://localhost:{self.port}")

            sim_thread = threading.Thread(
                target=_sim_loop, args=(self.state,), daemon=True
            )
            sim_thread.start()

            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                self.state.stopped = True


def launch(
    sim: "Simulation",
    port: int = 5050,
    args: "argparse.Namespace | None" = None,
    speed: float = 10.0,
) -> None:
    """Start the warehouse web server (blocks until Ctrl-C)."""
    WarehouseWebServer(sim, port=port, args=args, speed=speed).run()
