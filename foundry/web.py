"""
Greymatter Foundry — browser-based warehouse GUI.

Architecture
─────────────
  Python side                    Browser side
  ───────────                    ────────────
  WarehouseWebServer
    └─ stdlib HTTPServer         GET /          → HTML page (SPA)
    └─ SimState (thread-safe)    GET /stream    → SSE tick updates
         ↑ background sim-loop   GET /grid      → JSON grid layout (one-time)
                                 GET /overlay   → JSON ABC class map
                                 GET /zones     → JSON named zones for labels
                                 POST /control  → {action: play|pause|stop|speed}
                                 POST /upload   → multipart (layout.json / orders.csv)
                                 POST /rebuild  → rebuild from current paths
                                 POST /chat     → AI assistant (Anthropic API)
                                 POST /mutate   → direct layout mutations (no AI)

Run standalone
──────────────
  python -m foundry --web [--web-port 5050] [other flags]

Or import and call:
  from foundry.web import launch
  launch(sim, port=5050, args=args)
"""
from __future__ import annotations

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
             gap: 8px; flex-shrink: 0; flex-wrap: wrap; border-bottom: 1px solid #3A3A50; }
  #toolbar h1  { color: #7C83FD; font-size: 13px; letter-spacing: 1px; margin-right: 4px; }
  #tick-lbl    { color: #90A4AE; font-size: 11px; flex: 1; min-width: 100px; }
  .btn { background: #2A2A3E; color: #ECEFF4; border: 1px solid #444; border-radius: 4px;
         padding: 4px 10px; cursor: pointer; font-family: inherit; font-size: 12px; white-space: nowrap; }
  .btn:hover  { background: #3A3A50; }
  .btn.active { color: #7C83FD; border-color: #7C83FD; }
  #speed-wrap { display: flex; align-items: center; gap: 5px; color: #90A4AE; }
  #speed-range { accent-color: #7C83FD; width: 100px; }

  /* ── main layout ── */
  #main { display: flex; flex: 1; overflow: hidden; }
  #canvas-wrap { flex: 1; display: flex; align-items: center; justify-content: center;
                 padding: 10px; overflow: hidden; position: relative; }
  canvas { image-rendering: pixelated; border-radius: 4px; }

  /* ── side panel ── */
  #side { width: 230px; background: #2A2A3E; overflow-y: auto; padding: 8px 10px;
          flex-shrink: 0; border-left: 1px solid #3A3A50; }
  .section-title { color: #7C83FD; font-size: 11px; font-weight: bold;
                   margin-top: 10px; margin-bottom: 2px; letter-spacing: .5px; }
  .divider { height: 1px; background: #3A3A50; margin-bottom: 5px; }
  .kpi-row { display: flex; justify-content: space-between; padding: 2px 0; color: #90A4AE; }
  .kpi-val { color: #ECEFF4; }
  .file-row { display: flex; align-items: center; gap: 4px; margin: 3px 0; }
  .file-lbl { color: #90A4AE; width: 50px; flex-shrink: 0; font-size: 11px; }
  .file-name { color: #ECEFF4; flex: 1; overflow: hidden; text-overflow: ellipsis;
               white-space: nowrap; font-size: 11px; }
  .file-btn  { background: #1E1E2E; color: #7C83FD; border: none; border-radius: 3px;
               padding: 2px 6px; cursor: pointer; font-family: inherit; font-size: 11px; }
  .file-btn:hover { background: #2e2e4e; }
  #status-lbl { font-size: 11px; min-height: 15px; margin: 3px 0; }
  .wide-btn { width: 100%; background: #1E1E2E; color: #ECEFF4; border: 1px solid #3A3A50;
              border-radius: 4px; padding: 4px 0; cursor: pointer; font-family: inherit;
              font-size: 11px; margin: 3px 0; }
  .wide-btn:hover { background: #2e2e4e; }
  #optimal-chk { accent-color: #7C83FD; margin-right: 5px; }
  .legend-row { display: flex; align-items: center; gap: 6px; padding: 2px 0; color: #90A4AE;
                font-size: 11px; }
  .swatch { width: 11px; height: 11px; border-radius: 50%; flex-shrink: 0; }
  .swatch.rect { border-radius: 2px; }
  #log { background: #1E1E2E; border-radius: 3px; padding: 4px; height: 130px;
         overflow-y: auto; font-size: 10px; color: #90A4AE; line-height: 1.5; }

  /* ── chat panel ── */
  #chat-panel {
    position: fixed; bottom: 0; right: 0; width: 390px;
    max-height: 0; background: #2A2A3E;
    border: 1px solid #7C83FD; border-bottom: none;
    border-radius: 10px 10px 0 0; overflow: hidden;
    transition: max-height .3s ease; z-index: 200;
    display: flex; flex-direction: column;
  }
  #chat-panel.open { max-height: 460px; }
  #chat-hdr {
    display: flex; align-items: center; gap: 8px; padding: 8px 12px;
    border-bottom: 1px solid #3A3A50; flex-shrink: 0; background: #1E1E2E;
  }
  #chat-hdr-title { color: #7C83FD; font-weight: bold; font-size: 12px; flex: 1; }
  #chat-model-badge { color: #666; font-size: 10px; }
  #chat-close { background: none; border: none; color: #90A4AE; cursor: pointer;
                font-size: 16px; line-height: 1; padding: 0 2px; }
  #chat-close:hover { color: #ECEFF4; }
  #chat-msgs {
    flex: 1; overflow-y: auto; padding: 10px 10px 6px; display: flex;
    flex-direction: column; gap: 8px; min-height: 0;
  }
  .chat-bubble {
    padding: 7px 10px; border-radius: 8px; max-width: 92%; font-size: 11px;
    line-height: 1.55; white-space: pre-wrap; word-break: break-word;
  }
  .chat-user { background: #3A3A6A; align-self: flex-end; color: #ECEFF4;
               border-radius: 8px 8px 2px 8px; }
  .chat-ai   { background: #1E1E2E; align-self: flex-start; color: #ECEFF4;
               border: 1px solid #3A3A50; border-radius: 8px 8px 8px 2px; }
  .chat-sys  { align-self: center; color: #43A047; font-size: 10px;
               background: rgba(67,160,71,.1); border-radius: 4px; padding: 3px 8px; }
  .chat-err  { align-self: center; color: #FB8C00; font-size: 10px; }
  .typing-dot { display: inline-block; animation: blink 1.1s infinite; }
  .typing-dot:nth-child(2) { animation-delay: .25s; }
  .typing-dot:nth-child(3) { animation-delay: .5s;  }
  @keyframes blink { 0%,100%{opacity:.2} 50%{opacity:1} }
  #chat-examples { padding: 0 10px 8px; }
  .example-chip {
    display: inline-block; background: #1E1E2E; border: 1px solid #3A3A50;
    border-radius: 12px; padding: 3px 9px; font-size: 10px; color: #90A4AE;
    cursor: pointer; margin: 2px 2px; white-space: nowrap;
  }
  .example-chip:hover { border-color: #7C83FD; color: #7C83FD; }
  #chat-foot {
    display: flex; gap: 6px; padding: 8px 10px; border-top: 1px solid #3A3A50;
    flex-shrink: 0;
  }
  #chat-in {
    flex: 1; background: #1E1E2E; color: #ECEFF4; border: 1px solid #3A3A50;
    border-radius: 6px; padding: 6px 9px; font-family: inherit; font-size: 12px;
    outline: none;
  }
  #chat-in:focus { border-color: #7C83FD; }
  #chat-send {
    background: #7C83FD; color: #fff; border: none; border-radius: 6px;
    padding: 6px 13px; cursor: pointer; font-family: inherit; font-size: 12px;
    white-space: nowrap;
  }
  #chat-send:hover   { background: #5C63DD; }
  #chat-send:disabled { background: #3A3A50; color: #666; cursor: not-allowed; }
</style>
</head>
<body>

<!-- toolbar -->
<div id="toolbar">
  <h1>GREYMATTER FOUNDRY</h1>
  <span id="tick-lbl">tick 0</span>
  <button class="btn active" id="btn-run"    onclick="ctrl('play')">▶ Run</button>
  <button class="btn"        id="btn-pause"  onclick="ctrl('pause')">⏸ Pause</button>
  <button class="btn"        id="btn-stop"   onclick="ctrl('stop')">■ Stop</button>
  <div id="speed-wrap">
    <span>Speed:</span>
    <input type="range" id="speed-range" min="0.5" max="500" step="0.5" value="10">
    <span id="speed-val">10</span> t/s
  </div>
  <button class="btn" id="btn-labels" onclick="toggleLabels()" title="Toggle rack/aisle/agent labels">🏷 Labels</button>
  <button class="btn" id="btn-chat"   onclick="toggleChat()"   title="Open AI warehouse assistant">💬 AI</button>
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
    <div class="section-title" style="margin-top:12px">Config</div><div class="divider"></div>
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
    <label style="cursor:pointer;display:flex;align-items:center;margin:4px 0;color:#ECEFF4;font-size:11px">
      <input type="checkbox" id="optimal-chk" onchange="toggleOptimal()">
      &nbsp;Show Optimal Layout
    </label>

    <!-- Legend -->
    <div class="section-title" style="margin-top:12px" id="legend-title">Agents</div>
    <div class="divider"></div>
    <div id="legend-body"></div>

    <!-- Events -->
    <div class="section-title" style="margin-top:12px">Events</div><div class="divider"></div>
    <div id="log"></div>
  </div>
</div>

<!-- AI Chat panel (fixed bottom-right, slides up) -->
<div id="chat-panel">
  <div id="chat-hdr">
    <span id="chat-hdr-title">🤖 AI Warehouse Assistant</span>
    <span id="chat-model-badge"></span>
    <button id="chat-close" onclick="toggleChat()" title="Close">×</button>
  </div>
  <div id="chat-msgs">
    <div class="chat-bubble chat-ai">
      👋 Hi! I can help you optimise this warehouse. Try asking me to extend a rack, add agents, or run a throughput forecast.
    </div>
  </div>
  <div id="chat-examples">
    <span class="example-chip" onclick="fillAndSend(this)">What are the current KPIs?</span>
    <span class="example-chip" onclick="fillAndSend(this)">Extend Rack 5 by 2 rows downward</span>
    <span class="example-chip" onclick="fillAndSend(this)">Add 2 more agents</span>
    <span class="example-chip" onclick="fillAndSend(this)">Forecast 1 hour with 8 agents</span>
    <span class="example-chip" onclick="fillAndSend(this)">Why is congestion high?</span>
  </div>
  <div id="chat-foot">
    <input id="chat-in" type="text"
           placeholder="e.g. 'extend rack 5 by 2 rows' or 'add 3 agents'"
           onkeydown="if(event.key==='Enter')sendChat()">
    <button id="chat-send" onclick="sendChat()">Send</button>
  </div>
</div>

<script>
// ── colour maps ───────────────────────────────────────────────────────────────
const CELL_COLOUR = {0:'#D8D8D8', 1:'#4A4A6A', 2:'#F2F2F2', 3:'#43A047', 4:'#FFB300'};
const AGENT_COLOUR = {
  IDLE:'#9E9E9E', MOVING_TO_PICK:'#1E88E5', PICKING:'#FB8C00',
  MOVING_TO_DOCK:'#00ACC1', DROPPING:'#7CB342',
  MOVING_TO_CHARGE:'#E53935', CHARGING:'#FDD835'
};
const ABC_COLOUR = {A:'#2E7D32', B:'#F57F17', C:'#1565C0'};

// ── global state ──────────────────────────────────────────────────────────────
let gridData    = null;
let overlayData = null;
let zonesData   = null;
let showOptimal = false;
let showLabels  = false;
let chatOpen    = false;
let chatBusy    = false;
let chatHistory = [];
let cellPx      = 10;
let latestAgents = [];

const canvas = document.getElementById('wh-canvas');
const ctx    = canvas.getContext('2d');

// ── fetch grid / overlay / zones ──────────────────────────────────────────────
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
async function fetchZones() {
  try {
    const r = await fetch('/zones');
    zonesData = await r.json();
  } catch(e) { console.warn('fetchZones:', e); }
}

// ── canvas sizing ─────────────────────────────────────────────────────────────
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

// ── drawing ───────────────────────────────────────────────────────────────────
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

function drawLabels() {
  if (!zonesData) return;
  ctx.save();
  const fs  = Math.max(7, Math.min(11, Math.floor(cellPx * 0.75)));
  const fs2 = Math.max(6, fs - 1);

  // ── rack zone labels ───────────────────────────────────────────────────────
  ctx.font         = `bold ${fs}px monospace`;
  ctx.textAlign    = 'center';
  ctx.textBaseline = 'middle';

  for (const z of (zonesData.zones || [])) {
    if (z.type !== 'RACK') continue;
    const cx = ((z.col_start + z.col_end + 1) / 2) * cellPx;
    const cy = ((z.row_start + z.row_end + 1) / 2) * cellPx;
    const label = z.name || '?';

    ctx.font = `bold ${fs}px monospace`;
    const tw = ctx.measureText(label).width;
    // semi-transparent badge
    ctx.fillStyle = 'rgba(10,10,30,0.62)';
    const bx = cx - tw/2 - 4, by = cy - fs/2 - 3;
    ctx.beginPath();
    if (ctx.roundRect) { ctx.roundRect(bx, by, tw + 8, fs + 6, 3); }
    else               { ctx.rect(bx, by, tw + 8, fs + 6); }
    ctx.fill();
    ctx.fillStyle = '#D0D0FF';
    ctx.fillText(label, cx, cy);
  }

  // ── dock + charger labels ──────────────────────────────────────────────────
  ctx.font = `bold ${fs2}px monospace`;
  for (const f of (zonesData.features || [])) {
    const cx = (f.c + 0.5) * cellPx;
    const cy = (f.r + 0.5) * cellPx;
    if (f.type === 'DOCK') {
      ctx.fillStyle = '#ECEFF4';
      ctx.fillText(f.short || f.name, cx, cy);
    } else if (f.type === 'CHARGING') {
      ctx.fillStyle = '#FFB300';
      ctx.fillText('⚡', cx, cy);
    }
  }

  // ── cross-aisle label ──────────────────────────────────────────────────────
  ctx.font      = `${Math.max(6, fs - 2)}px monospace`;
  ctx.textAlign = 'left';
  for (const ca of (zonesData.cross_aisles || [])) {
    ctx.fillStyle = 'rgba(140,210,255,0.75)';
    ctx.fillText('↔ Cross-Aisle', cellPx * 0.5, (ca.row + 0.5) * cellPx);
  }

  // ── vertical aisle labels ─────────────────────────────────────────────────
  ctx.font      = `${Math.max(6, fs - 2)}px monospace`;
  ctx.textAlign = 'center';
  for (const ai of (zonesData.aisles || [])) {
    ctx.fillStyle = 'rgba(200,200,255,0.5)';
    ctx.fillText(ai.short, (ai.col + 0.5) * cellPx, cellPx * 1.5);
  }

  // ── pick-position markers ──────────────────────────────────────────────────
  if (cellPx >= 8) {
    const r2 = Math.max(1.5, cellPx * 0.2);
    ctx.fillStyle = 'rgba(255,180,50,0.7)';
    for (const pp of (zonesData.pick_positions || [])) {
      ctx.beginPath();
      ctx.arc((pp.c + 0.5) * cellPx, (pp.r + 0.5) * cellPx, r2, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  ctx.restore();
}

function drawAgents(agents) {
  latestAgents = agents;
  const m = Math.max(1, Math.floor(cellPx / 5));
  for (const ag of agents) {
    const x = ag.c * cellPx + m, y = ag.r * cellPx + m;
    const d = cellPx - 2 * m;

    // Filled circle
    ctx.fillStyle   = AGENT_COLOUR[ag.state] || '#FFF';
    ctx.strokeStyle = '#111';
    ctx.lineWidth   = 0.8;
    ctx.beginPath();
    ctx.ellipse(x + d/2, y + d/2, d/2, d/2, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();

    // Agent ID label when labels are on (or always if cells are large enough)
    if (showLabels && cellPx >= 9) {
      const short = ag.id.replace('AMR-', '');
      ctx.fillStyle    = '#ECEFF4';
      ctx.font         = `bold ${Math.max(6, Math.floor(cellPx * 0.48))}px monospace`;
      ctx.textAlign    = 'center';
      ctx.textBaseline = 'top';
      ctx.fillText(short, x + d / 2, y + d + 1);
    }
  }
}

function redraw() {
  drawGrid();
  if (showLabels && zonesData) drawLabels();
  drawAgents(latestAgents);
}

// ── SSE stream ─────────────────────────────────────────────────────────────────
function startStream() {
  const es = new EventSource('/stream');
  es.onmessage = e => {
    const d = JSON.parse(e.data);
    document.getElementById('tick-lbl').textContent = 'tick ' + d.tick.toLocaleString();
    const k = d.kpis;
    document.getElementById('k-orders').textContent = k.orders_completed ?? '—';
    document.getElementById('k-oph').textContent    = k.orders_per_hour != null ? k.orders_per_hour.toFixed(1) : '—';
    document.getElementById('k-lph').textContent    = k.lines_per_hour  != null ? k.lines_per_hour.toFixed(1)  : '—';
    document.getElementById('k-cycle').textContent  = k.avg_cycle_time_s  != null ? k.avg_cycle_time_s.toFixed(0) : '—';
    document.getElementById('k-travel').textContent = k.total_travel_steps ?? '—';
    document.getElementById('k-cong').textContent   = k.congestion_events  ?? '—';
    document.getElementById('k-bat').textContent    = k.avg_battery_pct != null ? k.avg_battery_pct.toFixed(1) : '—';
    drawGrid();
    if (showLabels && zonesData) drawLabels();
    drawAgents(d.agents);
    if (d.events && d.events.length) appendEvents(d.events);
  };
  es.onerror = () => setTimeout(startStream, 2000);
}

// ── event log ─────────────────────────────────────────────────────────────────
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

// ── controls ──────────────────────────────────────────────────────────────────
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

// ── file upload ───────────────────────────────────────────────────────────────
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

// ── rebuild ───────────────────────────────────────────────────────────────────
async function rebuild() {
  setStatus('Rebuilding…', true);
  const r = await fetch('/rebuild', {method:'POST'});
  const d = await r.json();
  if (d.ok) {
    await fetchGrid(); await fetchOverlay(); await fetchZones();
    redraw();
    setStatus('Simulation rebuilt ✓', true);
  } else {
    setStatus('Rebuild failed: ' + d.error, false);
  }
}

// ── optimal layout overlay ────────────────────────────────────────────────────
async function toggleOptimal() {
  showOptimal = document.getElementById('optimal-chk').checked;
  if (showOptimal && !overlayData) await fetchOverlay();
  renderLegend();
  redraw();
}

// ── labels toggle ─────────────────────────────────────────────────────────────
async function toggleLabels() {
  showLabels = !showLabels;
  document.getElementById('btn-labels').classList.toggle('active', showLabels);
  if (showLabels && !zonesData) await fetchZones();
  redraw();
}

// ── chat panel ────────────────────────────────────────────────────────────────
async function toggleChat() {
  chatOpen = !chatOpen;
  document.getElementById('chat-panel').classList.toggle('open', chatOpen);
  document.getElementById('btn-chat').classList.toggle('active', chatOpen);
  if (chatOpen) {
    if (!zonesData) fetchZones();
    // Fetch model name for badge
    try {
      const r = await fetch('/chat-info');
      const d = await r.json();
      document.getElementById('chat-model-badge').textContent = d.model || '';
    } catch(e) {}
    setTimeout(() => document.getElementById('chat-in').focus(), 310);
  }
}

function fillAndSend(el) {
  document.getElementById('chat-in').value = el.textContent;
  sendChat();
}

async function sendChat() {
  if (chatBusy) return;
  const inp = document.getElementById('chat-in');
  const msg = inp.value.trim();
  if (!msg) return;
  inp.value = '';

  // Hide example chips after first message
  document.getElementById('chat-examples').style.display = 'none';

  addBubble(msg, 'user');
  const typingEl = addBubble(
    '<span class="typing-dot">●</span><span class="typing-dot"> ●</span><span class="typing-dot"> ●</span>',
    'ai', true
  );
  chatBusy = true;
  document.getElementById('chat-send').disabled = true;

  try {
    const resp = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg, history: chatHistory}),
    });
    const d = await resp.json();
    typingEl.remove();

    addBubble(d.reply || '(no response)', 'ai');
    chatHistory = d.history || chatHistory;

    if (d.changes_applied) {
      addBubble('✓ Simulation rebuilt with new layout', 'sys');
      await fetchGrid(); await fetchOverlay(); await fetchZones();
      redraw();
    }
    if (d.forecast) {
      const fc = d.forecast;
      if (!fc.error) {
        addBubble(
          `📊 Forecast (${(fc.ticks_simulated||0).toLocaleString()} ticks): ` +
          `${(fc.orders_per_hour||0).toFixed(1)} OPH | ` +
          `cycle ${(fc.avg_cycle_time_s||0).toFixed(0)}s | ` +
          `congestion ${fc.congestion_events||0}`,
          'sys'
        );
      }
    }
    if (d.error && !d.reply.startsWith('⚠️')) {
      addBubble('Error: ' + d.error, 'err');
    }
  } catch(e) {
    typingEl.remove();
    addBubble('Network error: ' + e.message, 'err');
  } finally {
    chatBusy = false;
    document.getElementById('chat-send').disabled = false;
  }
}

function addBubble(text, type, isHtml = false) {
  const msgs = document.getElementById('chat-msgs');
  const div = document.createElement('div');
  div.className = 'chat-bubble chat-' + type;
  if (isHtml) div.innerHTML = text;
  else        div.textContent = text;
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
  return div;
}

// ── legend ────────────────────────────────────────────────────────────────────
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

// ── status bar ────────────────────────────────────────────────────────────────
let _statusTimer = null;
function setStatus(msg, ok) {
  const el = document.getElementById('status-lbl');
  el.textContent = msg;
  el.style.color = ok ? '#43A047' : '#FB8C00';
  clearTimeout(_statusTimer);
  _statusTimer = setTimeout(() => { el.textContent = ''; }, 4000);
}

// ── boot ──────────────────────────────────────────────────────────────────────
renderLegend();
fetchGrid().then(() => {
  fetchZones();   // load zones for labels (non-blocking)
  startStream();
});
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

        self.running = False
        self.stopped = False
        self.speed   = 10.0   # ticks per second

        # File paths for rebuild (may be updated by /upload)
        self._layout_path: str | None = (
            args.layout if args and getattr(args, "layout", None) else None
        )
        self._orders_path: str | None = (
            args.orders if args and getattr(args, "orders", None) else None
        )

        # Mutable layout JSON (loaded once; mutated by /chat and /mutate)
        self._layout_json: dict | None = self._load_layout_json()

        # SSE subscribers
        self._subscribers: list[threading.Event] = []
        self._latest_frame: bytes = b""

    # ── SSE broadcast ──────────────────────────────────────────────────────────

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

    # ── Layout JSON helpers ────────────────────────────────────────────────────

    def _load_layout_json(self) -> "dict | None":
        if self._layout_path:
            try:
                return json.loads(Path(self._layout_path).read_text())
            except Exception:
                pass
        return None

    def _detect_cross_aisles(self) -> list[int]:
        """Return row numbers that lie inside the rack area but have no rack zone."""
        if not self._layout_json:
            return []
        rack_rows: set[int] = set()
        for z in self._layout_json.get("zones", []):
            if z.get("type", "").upper() != "RACK":
                continue
            for r in range(z.get("row_start", 0), z.get("row_end", 0) + 1):
                rack_rows.add(r)
        if not rack_rows:
            return []
        min_r, max_r = min(rack_rows), max(rack_rows)
        return [r for r in range(min_r, max_r + 1) if r not in rack_rows]

    def _detect_vertical_aisles(self) -> list[dict]:
        """Return col numbers inside the rack col range that have no rack zones."""
        if not self._layout_json:
            return []
        rack_cols: set[int] = set()
        for z in self._layout_json.get("zones", []):
            if z.get("type", "").upper() != "RACK":
                continue
            for c in range(z.get("col_start", 0), z.get("col_end", 0) + 1):
                rack_cols.add(c)
        if not rack_cols:
            return []
        min_c, max_c = min(rack_cols), max(rack_cols)
        result = []
        idx = 1
        for c in range(max(0, min_c - 1), max_c + 2):
            if c not in rack_cols:
                result.append({"col": c, "short": f"A{idx}"})
                idx += 1
        return result

    # ── Overlay ────────────────────────────────────────────────────────────────

    def compute_overlay(self) -> "dict[str, str]":
        """Return {"r,c": "A"|"B"|"C"} for accessible rack cells."""
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

    # ── Zones (for canvas labels) ──────────────────────────────────────────────

    def get_zones_payload(self) -> dict:
        """Return structured zone data for canvas label drawing."""
        zones    = []
        features = []
        lj = self._layout_json or {}
        rack_num = 1

        for z in lj.get("zones", []):
            t = z.get("type", "").upper()
            if t == "RACK":
                r0, r1 = z.get("row_start", 0), z.get("row_end", 0)
                c0, c1 = z.get("col_start", 0), z.get("col_end", 0)
                zones.append({
                    "name":      z.get("name", f"Rack {rack_num}"),
                    "type":      "RACK",
                    "row_start": r0, "row_end": r1,
                    "col_start": c0, "col_end": c1,
                })
                rack_num += 1
            elif t == "DOCK":
                for i, (r, c) in enumerate(z.get("cells", []), 1):
                    features.append({
                        "name":  f"Dock {i}",
                        "short": f"D{i}",
                        "type":  "DOCK",
                        "r": r, "c": c,
                    })
            elif t == "CHARGING":
                for i, (r, c) in enumerate(z.get("cells", []), 1):
                    features.append({
                        "name":  f"Charger {i}",
                        "short": "⚡",
                        "type":  "CHARGING",
                        "r": r, "c": c,
                    })

        # Pick positions from current inventory
        pick_positions = []
        try:
            with self.lock:
                for slot in self.sim.inventory._slots.values():
                    r, c = slot.pick_pos
                    pick_positions.append({"r": r, "c": c})
        except Exception:
            pass

        return {
            "zones":          zones,
            "features":       features,
            "cross_aisles":   [{"row": r} for r in self._detect_cross_aisles()],
            "aisles":         self._detect_vertical_aisles(),
            "pick_positions": pick_positions,
        }

    # ── AI-facing state summary ────────────────────────────────────────────────

    def get_state_summary(self) -> dict:
        """Return a structured snapshot for the AI assistant's context."""
        with self.lock:
            kpis   = self.sim.kpis.summary(self.sim.clock, self.sim.agents)
            agents = [
                {
                    "id":      a.agent_id,
                    "state":   a.state.name,
                    "battery": round(a.battery_pct, 1),
                    "orders":  a.orders_completed,
                    "congestion": a.congestion_events,
                }
                for a in self.sim.agents
            ]
            tick    = self.sim.clock
            pending = len(self.sim._pending)
            active  = len(self.sim._active)
            rows    = self.sim.grid.rows
            cols    = self.sim.grid.cols

        rack_zones = []
        if self._layout_json:
            for z in self._layout_json.get("zones", []):
                if z.get("type", "").upper() == "RACK" and "name" in z:
                    rack_zones.append({
                        "name":      z["name"],
                        "row_start": z.get("row_start"),
                        "row_end":   z.get("row_end"),
                        "col_start": z.get("col_start"),
                        "col_end":   z.get("col_end"),
                    })

        return {
            "tick":          tick,
            "grid":          {"rows": rows, "cols": cols},
            "agents":        agents,
            "n_agents":      len(agents),
            "pending_orders": pending,
            "active_orders":  active,
            "kpis":          kpis,
            "rack_zones":    rack_zones,
            "config": {
                "demand_rate": getattr(self.args, "demand_rate", None),
                "n_skus":      getattr(self.args, "n_skus", 50),
                "ticks":       getattr(self.args, "ticks", 28800),
                "seed":        getattr(self.args, "seed", 42),
            },
        }

    # ── Apply layout / config changes ──────────────────────────────────────────

    def apply_changes(self, changes: list[dict]) -> "tuple[bool, str]":
        """
        Apply a list of mutation dicts to _layout_json and args, then rebuild.
        Returns (success, message).
        """
        if not self._layout_json and not self.args:
            return False, "No layout loaded"

        lj       = copy.deepcopy(self._layout_json or {"rows": 30, "cols": 50, "zones": []})
        messages: list[str] = []

        def _find_rack(name: str) -> "dict | None":
            return next(
                (z for z in lj["zones"]
                 if z.get("name") == name and z.get("type", "").upper() == "RACK"),
                None,
            )

        for ch in changes:
            op = ch.get("op", "")
            try:
                if op == "set_agents":
                    n = int(ch["n"])
                    if self.args:
                        self.args.agents = n
                    messages.append(f"agents → {n}")

                elif op == "set_demand_rate":
                    rate = float(ch["rate"])
                    if self.args:
                        self.args.demand_rate = rate
                    messages.append(f"demand rate → {rate:.1f} orders/hr")

                elif op == "set_skus":
                    n = int(ch["n"])
                    if self.args:
                        self.args.n_skus = n
                    messages.append(f"SKUs → {n}")

                elif op == "modify_rack":
                    name = ch.get("name", "")
                    z = _find_rack(name)
                    if z is None:
                        return False, f"Rack '{name}' not found. " \
                                      f"Known racks: {[x.get('name') for x in lj['zones'] if x.get('type','').upper()=='RACK']}"
                    old_rs, old_re = z.get("row_start"), z.get("row_end")
                    for key in ("row_start", "row_end", "col_start", "col_end"):
                        if key in ch:
                            z[key] = int(ch[key])
                    messages.append(
                        f"{name} rows {old_rs}-{old_re} → {z.get('row_start')}-{z.get('row_end')}"
                    )

                elif op == "add_rack":
                    name = ch.get("name", f"Rack {len(lj['zones'])}")
                    lj["zones"].append({
                        "type":      "RACK",
                        "name":      name,
                        "row_start": int(ch["row_start"]),
                        "row_end":   int(ch["row_end"]),
                        "col_start": int(ch["col_start"]),
                        "col_end":   int(ch["col_end"]),
                    })
                    messages.append(f"added {name}")

                elif op == "remove_rack":
                    name = ch.get("name", "")
                    before = len(lj["zones"])
                    lj["zones"] = [
                        z for z in lj["zones"]
                        if not (z.get("name") == name and z.get("type", "").upper() == "RACK")
                    ]
                    if len(lj["zones"]) == before:
                        return False, f"Rack '{name}' not found"
                    messages.append(f"removed {name}")

                else:
                    return False, f"Unknown op: {op!r}"

            except (KeyError, ValueError, TypeError) as exc:
                return False, f"Error in op={op!r}: {exc}"

        # Write modified JSON to a temp file and rebuild
        fd, tmp = tempfile.mkstemp(suffix=".json")
        try:
            os.write(fd, json.dumps(lj).encode())
            os.close(fd)
            old_path = self._layout_path
            old_json = self._layout_json
            self._layout_path = tmp
            self._layout_json = lj
            err = self.rebuild()
            if err:
                self._layout_path = old_path
                self._layout_json = old_json
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return False, f"Rebuild failed: {err}"
            return True, "Applied: " + "; ".join(messages)
        except Exception as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False, str(exc)

    # ── Quick headless forecast ────────────────────────────────────────────────

    def quick_forecast(
        self,
        ticks: int,
        temp_changes: "list[dict] | None" = None,
    ) -> dict:
        """
        Build a fresh simulation (with optional temporary changes), run it
        headlessly for `ticks` ticks, and return KPI summary.
        Does NOT affect the live running simulation.
        """
        if not self.args:
            return {"error": "No args available for forecast"}

        try:
            from foundry.__main__ import build_simulation

            fc_args = copy.copy(self.args)
            fc_args.quiet = True

            tmp_path = None
            if temp_changes and self._layout_json:
                lj = copy.deepcopy(self._layout_json)
                for ch in temp_changes:
                    op = ch.get("op", "")
                    if op == "set_agents":
                        fc_args.agents = int(ch.get("n", fc_args.agents))
                    elif op == "set_demand_rate":
                        fc_args.demand_rate = float(ch.get("rate", 0))
                    elif op == "set_skus":
                        fc_args.n_skus = int(ch.get("n", fc_args.n_skus))
                    elif op == "modify_rack":
                        z = next(
                            (z for z in lj["zones"]
                             if z.get("name") == ch.get("name")
                             and z.get("type", "").upper() == "RACK"),
                            None,
                        )
                        if z:
                            for key in ("row_start", "row_end", "col_start", "col_end"):
                                if key in ch:
                                    z[key] = int(ch[key])
                    elif op == "add_rack":
                        lj["zones"].append({
                            "type": "RACK", "name": ch.get("name", "?"),
                            **{k: int(ch[k]) for k in ("row_start", "row_end", "col_start", "col_end") if k in ch},
                        })
                    elif op == "remove_rack":
                        name = ch.get("name", "")
                        lj["zones"] = [
                            z for z in lj["zones"]
                            if not (z.get("name") == name and z.get("type", "").upper() == "RACK")
                        ]
                fd2, tmp_path = tempfile.mkstemp(suffix=".json")
                os.write(fd2, json.dumps(lj).encode())
                os.close(fd2)
                fc_args.layout = tmp_path

            fc_sim = build_simulation(fc_args)
            for _ in range(ticks):
                fc_sim.step()
            result = fc_sim.kpis.summary(fc_sim.clock, fc_sim.agents)
            result["ticks_simulated"] = ticks
            result["forecast_agents"] = fc_args.agents

            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            return result

        except Exception as exc:
            return {"error": str(exc)}

    # ── Rebuild ────────────────────────────────────────────────────────────────

    def rebuild(self) -> "str | None":
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
    state: SimState   # injected before server start

    def log_message(self, fmt, *args):
        pass   # silence access log

    # ── GET ───────────────────────────────────────────────────────────────────

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
        elif path == "/zones":
            self._json(self.state.get_zones_payload())
        elif path == "/chat-info":
            import foundry.chat as _chat
            self._json({
                "model":   _chat._MODEL,
                "api_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
            })
        else:
            self.send_error(404)

    def _html(self):
        body = _HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type",   "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type",     "text/event-stream")
        self.send_header("Cache-Control",    "no-cache")
        self.send_header("X-Accel-Buffering","no")
        self.end_headers()
        ev = self.state.subscribe()
        try:
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
            grid  = self.state.sim.grid
            cells = [
                [int(grid[r, c]) for c in range(grid.cols)]
                for r in range(grid.rows)
            ]
        self._json({"rows": grid.rows, "cols": grid.cols, "cells": cells})

    # ── POST ──────────────────────────────────────────────────────────────────

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/control":
            self._control()
        elif path == "/upload":
            self._upload()
        elif path == "/rebuild":
            self._rebuild()
        elif path == "/chat":
            self._chat()
        elif path == "/mutate":
            self._mutate()
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
        ctype  = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length)

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
        msg      = email.message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
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
                self.state._layout_json = json.loads(filedata.decode())
            else:
                self.state._orders_path = tmp
            self._json({"ok": True, "filename": filename or Path(tmp).name})
        except Exception as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
            self._json({"ok": False, "error": str(exc)})

    def _rebuild(self):
        err = self.state.rebuild()
        if err:
            self._json({"ok": False, "error": err})
        else:
            self.state._layout_json = self.state._load_layout_json()
            self._json({"ok": True})

    def _chat(self):
        """Call the AI assistant and return a reply."""
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length) or b"{}")
        user_message = body.get("message", "").strip()
        history      = body.get("history", [])

        if not user_message:
            self._json({"reply": "", "history": history, "changes_applied": False,
                        "forecast": None, "error": "empty message"})
            return

        try:
            from foundry.chat import handle_message
            result = handle_message(self.state, user_message, history)
        except Exception as exc:
            result = {
                "reply":           f"⚠️ Internal error: {exc}",
                "history":         history,
                "changes_applied": False,
                "forecast":        None,
                "error":           str(exc),
            }
        self._json(result)

    def _mutate(self):
        """Apply layout / config mutations directly (without AI)."""
        length  = int(self.headers.get("Content-Length", 0))
        body    = json.loads(self.rfile.read(length) or b"{}")
        changes = body.get("changes", [])
        ok, msg = self.state.apply_changes(changes)
        self._json({"ok": ok, "message": msg})

    # ── helpers ───────────────────────────────────────────────────────────────

    def _json(self, data: dict):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── Simulation loop (background thread) ──────────────────────────────────────

def _sim_loop(state: SimState) -> None:
    while not state.stopped:
        if state.running:
            with state.lock:
                evts   = state.sim.step()
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
        _Handler.state = self.state
        with socketserver.ThreadingTCPServer(("", self.port), _Handler) as srv:
            srv.allow_reuse_address = True
            print(f"Foundry web GUI → http://localhost:{self.port}")
            print(f"AI assistant:    {'enabled ✓' if os.environ.get('ANTHROPIC_API_KEY') else 'set ANTHROPIC_API_KEY to enable'}")
            sim_thread = threading.Thread(target=_sim_loop, args=(self.state,), daemon=True)
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
