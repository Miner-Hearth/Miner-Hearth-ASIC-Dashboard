#!/usr/bin/env python3
"""Local web dashboard for Miner Hearth (formerly miner-autotune), stdlib only.

Served by the same process as the tuner. Binds 127.0.0.1 by default; the
optional LAN listener (Settings > Phone / LAN access) adds PIN and read-only
protection for non-local clients (see miner_hearth.AccessGuard).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from autotune import (
    APP_VERSION,
    DISPLAY_FIELD_DEFS,
    GLOBAL_TUNER_KEYS,
    compute_j_per_th,
    default_display_fields,
    default_global_tuner,
    iso_now,
    normalize_display_fields,
    normalize_global_tuner,
)

import nk_ui
import nk_ui19
import nk_ui110
import nk_ui113
import nk_ui114
import nk_ui115
import nk_ui1155
import nk_ui116
import nk_ui1162
import nk_ui117
import nk_ui1171
from miner_hearth import is_loopback

LOG = logging.getLogger("autotune.dashboard")
MAX_BODY_BYTES = 12 * 1024 * 1024

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Miner Autotune</title>
<style>
  :root {
    --bg: #101317;
    --bg-card: #1a1f27;
    --bg-soft: #232a35;
    --border: #2d3643;
    --text: #e8edf4;
    --muted: #9aa6b6;
    --accent: #3ee0b4;
    --warn: #f0c14a;
    --danger: #ff6b6b;
    --ok: #3ee0b4;
    --info: #6cb8ff;
    --paused: #7f8ea3;
    --shadow: 0 8px 24px rgba(0,0,0,.35);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
  }
  header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
    padding: 18px 24px 14px;
    border-bottom: 1px solid var(--border);
  }
  h1 { font-size: 1.25rem; margin: 0; font-weight: 650; letter-spacing: .02em; display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
  .ver-pill {
    font-size: .72rem; font-weight: 700; letter-spacing: .04em;
    padding: 2px 8px; border-radius: 999px;
    background: rgba(62, 224, 180, .12); color: var(--accent);
    border: 1px solid #1e6b57;
  }
  .sub { color: var(--muted); font-size: .88rem; }
  .spacer { flex: 1; }
  .badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border-radius: 999px;
    font-size: .72rem; font-weight: 650; letter-spacing: .04em;
    text-transform: uppercase; line-height: 1.2;
    background: var(--bg-soft); border: 1px solid var(--border);
    white-space: nowrap; max-width: 100%;
  }
  .badge.paused { color: var(--muted); border-color: var(--border); }
  .badge.live { color: var(--ok); border-color: #1e6b57; }
  .badge.dry { color: var(--warn); border-color: #7a5d12; }
  .badge.mon { color: var(--info); border-color: #295680; }
  .toggle {
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    background: var(--bg-soft); border: 1px solid var(--border);
    color: var(--text); border-radius: 8px; padding: 7px 12px;
    cursor: pointer; font: inherit; font-size: .86rem; line-height: 1.2;
    white-space: nowrap; min-height: 34px;
  }
  .toggle:hover { border-color: var(--accent); }
  .toggle:disabled { opacity: .55; cursor: default; }
  .fleet-bar {
    margin: 14px 24px 0;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 12px 18px;
    box-shadow: var(--shadow);
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(145px, 1fr));
    gap: 10px 16px;
  }
  .fleet-stat label {
    display: block; color: var(--muted); font-size: .68rem;
    text-transform: uppercase; letter-spacing: .06em; margin-bottom: 2px;
  }
  .fleet-stat .val { font-size: 1.05rem; font-weight: 680; font-variant-numeric: tabular-nums; }
  .fleet-stat .tiny { font-size: .74rem; color: var(--muted); }
  .badge.fb { color: var(--warn); border-color: #7a5d12; background: rgba(240,193,74,.12); }
  .bar-track {
    height: 6px; border-radius: 999px; background: var(--bg); border: 1px solid var(--border);
    overflow: hidden; margin-top: 4px;
  }
  .bar-fill { height: 100%; background: var(--accent); border-radius: 999px; }
  .bar-fill.warn { background: var(--warn); }
  .bar-fill.info { background: var(--info); }
  .domains-row {
    display: flex; align-items: flex-end; gap: 3px; height: 26px; margin-top: 4px;
    padding: 3px 4px; background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
  }
  .dom-bar { flex: 1; min-width: 6px; background: var(--accent); border-radius: 2px 2px 0 0; opacity: .88; }
  .fan-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 10px; margin-top: 4px; }
  .fan-item { font-size: .75rem; color: var(--muted); }
  .fan-item b { color: var(--text); }
  .board-table { width: 100%; border-collapse: collapse; font-size: .73rem; margin-top: 4px; }
  .board-table th { text-align: left; color: var(--muted); font-weight: 600; padding: 3px 4px; border-bottom: 1px solid var(--border); }
  .board-table td { padding: 4px 4px; border-bottom: 1px solid rgba(45,54,67,.5); font-variant-numeric: tabular-nums; }
  .pool-list { max-height: 130px; overflow-y: auto; margin-top: 6px; display: grid; gap: 4px; }
  .pool-row {
    font-size: .74rem; padding: 4px 7px; border-radius: 6px;
    background: var(--bg); border: 1px solid var(--border);
    display: flex; justify-content: space-between; gap: 8px; align-items: center;
  }
  .pool-row.active { border-color: var(--ok); background: rgba(62,224,180,.06); }
  .spark-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 8px; }
  .spark-box { background: var(--bg-soft); border: 1px solid var(--border); border-radius: 8px; padding: 6px 8px; }
  .spark-head { display: flex; justify-content: space-between; font-size: .68rem; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; margin-bottom: 3px; }
  .spark-box svg { width: 100%; height: 34px; display: block; }
  .scan-panel {
    margin: 16px 24px 0;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 14px 18px;
    box-shadow: var(--shadow);
  }
  .scan-bar {
    display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
  }
  .scan-bar input {
    background: var(--bg-soft); border: 1px solid var(--border);
    color: var(--text); border-radius: 8px; padding: 7px 10px;
    font: inherit; min-width: 190px;
  }
  .scan-list {
    margin-top: 12px; display: grid; gap: 8px;
  }
  .scan-row {
    display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px;
    background: var(--bg-soft); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px;
  }
  .scan-row input.add-name {
    background: var(--bg-card); border: 1px solid var(--border); color: var(--text);
    border-radius: 6px; padding: 5px 8px; font: inherit; width: 170px;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(340px, 100%), 1fr));
    gap: 16px;
    padding: 20px 24px;
  }
  .card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px 18px 14px;
    box-shadow: var(--shadow);
    display: flex;
    flex-direction: column;
  }
  .card.offline { opacity: .82; }
  .card.unparsed { border-color: var(--warn); }
  .parse-err { color: var(--warn); font-size: .82rem; margin: 8px 0; background: rgba(240,193,74,.08); border-radius: 8px; padding: 8px 10px; }
  .card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
  .name-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .name { font-size: 1.05rem; font-weight: 650; }
  .icon-btn {
    background: transparent; border: 1px solid var(--border); color: var(--muted);
    border-radius: 6px; padding: 2px 7px; cursor: pointer; font-size: .75rem;
  }
  .icon-btn:hover { color: var(--text); border-color: var(--accent); }
  .rename-form { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .rename-form input {
    background: var(--bg-soft); border: 1px solid var(--accent); color: var(--text);
    border-radius: 6px; padding: 4px 8px; font: inherit; width: 160px;
  }
  .ip { color: var(--muted); font-size: .84rem; margin-top: 2px; word-break: break-word; }
  .dot {
    width: 9px; height: 9px; border-radius: 50%; display: inline-block;
    background: var(--paused); margin-right: 4px;
  }
  .dot.on { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
  .dot.off { background: var(--danger); }
  .metrics {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px 14px;
    margin: 14px 0 10px;
  }
  .metric.full { grid-column: 1 / -1; }
  .metric label { display: block; color: var(--muted); font-size: .72rem; text-transform: uppercase; letter-spacing: .06em; line-height: 1.25; overflow-wrap: anywhere; }
  .metric .val { font-size: 1.12rem; font-weight: 650; font-variant-numeric: tabular-nums; }
  .metric .val small { font-size: .76rem; font-weight: 500; color: var(--muted); }
  .temp.ok, .err-ok { color: var(--ok); }
  .temp.warn, .err-warn { color: var(--warn); }
  .temp.hot, .err-hot { color: var(--danger); }
  .chips { color: var(--muted); font-size: .78rem; margin-top: 2px; word-break: break-word; }
  .chips b { color: var(--text); font-weight: 650; }
  .vreason {
    font-size: .8rem; color: var(--muted);
    background: rgba(62, 224, 180, .06); border: 1px solid var(--border);
    border-radius: 8px; padding: 6px 10px; margin-bottom: 8px;
  }
  .vreason strong { color: var(--text); font-size: .72rem; text-transform: uppercase; letter-spacing: .05em; }
  .panel-box {
    background: var(--bg-soft); border: 1px solid var(--border);
    border-radius: 9px; padding: 9px 10px; margin-bottom: 8px;
  }
  .panel-head {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    font-size: .74rem; text-transform: uppercase; letter-spacing: .05em;
    color: var(--muted); font-weight: 650; margin-bottom: 6px;
  }
  .manual-grid {
    display: grid; grid-template-columns: 1fr 1fr auto; gap: 8px; align-items: end;
  }
  .field label {
    display: block; font-size: .7rem; color: var(--muted); margin-bottom: 3px;
    line-height: 1.25; overflow-wrap: anywhere;
  }
  .field input {
    width: 100%; background: var(--bg-card); border: 1px solid var(--border);
    color: var(--text); border-radius: 6px; padding: 5px 8px; font: inherit; font-size: .86rem;
  }
  .field input:disabled { opacity: .5; cursor: not-allowed; }
  .pool-grid {
    display: grid; grid-template-columns: 2fr 1fr; gap: 6px 8px; margin-top: 6px;
  }
  .pool-grid .full { grid-column: 1 / -1; }
  .mini-note { font-size: .74rem; color: var(--muted); margin-top: 5px; line-height: 1.35; }
  .mini-note.warn { color: var(--warn); }
  .mini-note.err { color: var(--danger); }
  .mini-note.ok { color: var(--ok); }
  .action {
    font-size: .84rem; color: var(--muted);
    background: var(--bg-soft); border-radius: 8px; padding: 8px 10px;
    min-height: 3.1em;
    margin-top: auto;
  }
  .action strong { color: var(--text); text-transform: uppercase; font-size: .75rem; letter-spacing: .04em; }
  .row-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 12px; }
  button.btn {
    flex: 1; border: 0; border-radius: 8px; padding: 9px 12px;
    font: inherit; font-weight: 650; cursor: pointer;
  }
  button.start { background: #1e6b57; color: #dffaf1; }
  button.start:hover:not(:disabled) { background: #248066; }
  button.stop { background: #6b2a2a; color: #ffd6d6; }
  button.stop:hover:not(:disabled) { background: #7c3333; }
  button.add-btn { background: #21597a; color: #e2f4ff; border: 0; border-radius: 6px; padding: 6px 12px; font: inherit; font-weight: 650; cursor: pointer; }
  button.add-btn:hover:not(:disabled) { background: #2a6e96; }
  button:disabled { opacity: .45; cursor: not-allowed; }
  .logs {
    margin: 0 24px 24px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 14px 16px;
  }
  .logs h2 { margin: 0 0 8px; font-size: .85rem; color: var(--muted); font-weight: 650; letter-spacing: .06em; text-transform: uppercase; }
  pre {
    margin: 0; max-height: 240px; overflow: auto;
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: .78rem; line-height: 1.45; color: #c5d0dc; white-space: pre-wrap;
  }
  .settings-panel {
    margin: 14px 24px 0;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px 18px;
    box-shadow: var(--shadow);
  }
  .settings-panel[hidden] { display: none; }
  .settings-cols {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr));
    gap: 16px;
    margin-top: 12px;
    align-items: start;
  }
  .settings-box {
    background: var(--bg-soft);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px;
  }
  .settings-box h3 {
    margin: 0 0 8px; font-size: .8rem; text-transform: uppercase;
    letter-spacing: .05em; color: var(--accent);
  }
  .field-checks {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(185px, 1fr)); gap: 6px 10px;
  }
  .field-check {
    display: flex; align-items: center; gap: 6px; font-size: .8rem; color: var(--text); cursor: pointer;
  }
  .field-check input { accent-color: var(--accent); cursor: pointer; }
  .caps-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 8px 10px;
  }
  .order-row {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 6px 8px; background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 7px; margin-bottom: 6px; font-size: .84rem;
  }
  .card.drag-over { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(62,224,180,.25); }
  .bos-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 6px 8px; margin-top: 6px;
  }
  .bos-grid .full { grid-column: 1 / -1; }
  .err { color: var(--danger); padding: 12px 24px; }
  footer { color: var(--muted); font-size: .75rem; padding: 0 24px 20px; }
</style>
</head>
<body>
  <header>
    <div>
      <h1>Miner Autotune <span class="ver-pill" id="appVersion">v__APP_VERSION__</span></h1>
      <div class="sub">Bitaxe Gamma 601 &middot; NerdOCTAxe &middot; Antminer S19 Pro &mdash; local dashboard</div>
    </div>
    <div class="spacer"></div>
    <button class="toggle" id="settingsOpenBtn" type="button">Settings</button>
    <button class="toggle" id="scanOpenBtn" type="button">Scan network</button>
    <span id="autoBadge" class="badge paused">Autotune: OFF</span>
    <button class="toggle" id="autoToggle" type="button">Turn autotune ON</button>
    <span id="modeBadge" class="badge">…</span>
    <button class="toggle" id="dryToggle" type="button">Toggle dry-run</button>
    <span class="sub" id="updated">Starting…</span>
  </header>

  <section class="fleet-bar" id="fleetSummary">
    <div class="fleet-stat"><label>Machines Online</label><div class="val">—</div></div>
    <div class="fleet-stat"><label>Total Hashrate</label><div class="val">—</div></div>
    <div class="fleet-stat"><label>Total Power</label><div class="val">—</div></div>
    <div class="fleet-stat"><label>Fleet Efficiency</label><div class="val">—</div></div>
    <div class="fleet-stat"><label>Hottest Chip</label><div class="val">—</div></div>
    <div class="fleet-stat"><label>Hottest VR</label><div class="val">—</div></div>
    <div class="fleet-stat"><label>Best Share Ever</label><div class="val">—</div></div>
  </section>

  <section class="scan-panel" id="scanPanel" hidden>
    <div class="scan-bar">
      <strong>LAN Discovery</strong>
      <input id="scanSubnet" type="text" placeholder="Auto-detect local /24 (e.g. 192.168.1.0/24)">
      <button class="toggle" id="runScanBtn" type="button">Run scan</button>
      <button class="icon-btn" id="closeScanBtn" type="button">Hide</button>
      <span class="sub" id="scanStatus">Probes AxeOS / ESP-Miner (:80) and Antminer CGMiner (:4028). Newly added miners start with autotune OFF.</span>
    </div>
    <div class="scan-list" id="scanList"></div>
  </section>

  <section class="settings-panel" id="settingsPanel" hidden>
    <div class="scan-bar">
      <strong>Dashboard &amp; Autotune Settings</strong>
      <span class="sub" id="settingsStatus">Saved automatically in <code>userdata/devices.json</code>.</span>
      <div class="spacer"></div>
      <button class="icon-btn" id="closeSettingsBtn" type="button">Hide</button>
    </div>
    <div class="settings-cols">
      <div class="settings-box">
        <h3>Visible Data Fields</h3>
        <div class="row-actions" style="margin:0 0 8px">
          <button class="icon-btn" type="button" onclick="setAllFieldsVisible(true)">Show all</button>
          <button class="icon-btn" type="button" onclick="setAllFieldsVisible(false)">Hide all</button>
        </div>
        <div class="field-checks" id="settingsFieldChecks"></div>
      </div>
      <div class="settings-box">
        <h3>Global Caps &amp; Targets (Tunable Machines)</h3>
        <form id="globalTunerForm" onsubmit="return saveGlobalTuner(event)">
          <div class="caps-grid">
            <div class="field"><label>Max Freq Cap (MHz)</label><input id="gt_max_freq" type="number" min="400" max="850" step="25"></div>
            <div class="field"><label>Emergency Freq Drop (MHz)</label><input id="gt_emergency_step_mhz" type="number" min="5" max="200" step="5"></div>
            <div class="field"><label>Min Voltage (mV)</label><input id="gt_min_voltage_mv" type="number" min="1000" max="1250" step="5"></div>
            <div class="field"><label>Max Voltage (mV)</label><input id="gt_max_voltage_mv" type="number" min="1000" max="1250" step="5"></div>
            <div class="field"><label>Emergency Voltage Reset (mV)</label><input id="gt_emergency_voltage_mv" type="number" min="1000" max="1250" step="5"></div>
            <div class="field"><label>ASIC Target (°C)</label><input id="gt_asic_target_c" type="number" min="35" max="85" step="0.5"></div>
            <div class="field"><label>ASIC Emergency (°C)</label><input id="gt_asic_max_c" type="number" min="45" max="90" step="0.5"></div>
            <div class="field"><label>VR Target (°C)</label><input id="gt_vr_target_c" type="number" min="35" max="90" step="0.5"></div>
            <div class="field"><label>VR Emergency (°C)</label><input id="gt_vr_max_c" type="number" min="45" max="95" step="0.5"></div>
            <div class="field"><label>Error Target (%)</label><input id="gt_error_target_pct" type="number" min="0" max="10" step="0.1"></div>
            <div class="field"><label>Error Low / Trim (%)</label><input id="gt_error_low_pct" type="number" min="0" max="10" step="0.1"></div>
            <div class="field"><label>Error Acceptable Max (%)</label><input id="gt_error_high_pct" type="number" min="0.1" max="20" step="0.1"></div>
          </div>
          <div class="mini-note">Per-machine overrides on individual cards take priority over these globals.</div>
          <div id="globalTunerNotice" class="mini-note"></div>
          <div class="row-actions" style="margin-top:8px">
            <button class="add-btn" type="submit">Save Global Caps &amp; Targets</button>
          </div>
        </form>
      </div>
      <div class="settings-box">
        <h3>Machine Card Order</h3>
        <div class="mini-note" style="margin-top:0;margin-bottom:8px">Reorder cards here or with ◀ / ▶ buttons or drag-and-drop on card headers.</div>
        <div id="settingsCardOrder"></div>
      </div>
    </div>
  </section>

  <div id="error" class="err" hidden></div>
  <div class="grid" id="cards"></div>
  <section class="logs" id="logsSection">
    <h2>Recent log</h2>
    <pre id="log">Waiting for data…</pre>
  </section>
  <footer>Refreshes every 3 seconds from this PC only. Stopping autotune keeps polling and unlocks manual frequency/voltage.</footer>
<script>
const $ = (id) => document.getElementById(id);
let editingDevice = null;
let editingDraft = "";
let openPoolDevice = null;
let openOverrideDevice = null;
let openBosDevice = null;
let cardNotice = {};
let scanPanelOpen = false;
let settingsPanelOpen = false;
let dragSourceDevice = null;
let lastStatus = null;

function isFieldVisible(key) {
  const df = lastStatus && lastStatus.settings && lastStatus.settings.display_fields;
  if (!df || !(key in df)) return true;
  return df[key] !== false;
}

function isEditingCards() {
  if (editingDevice !== null || openPoolDevice !== null || openOverrideDevice !== null || openBosDevice !== null) return true;
  const active = document.activeElement;
  const cardsEl = $("cards");
  if (active && cardsEl && cardsEl.contains(active)) {
    const tag = (active.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  }
  return false;
}
function domKey(name) {
  return encodeURIComponent(name).replace(/%/g, "_");
}
function fmt(v, digits, unit) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return "—";
  return n.toFixed(digits) + (unit ? " " + unit : "");
}
function fmtHashrate(ghs) {
  if (ghs === null || ghs === undefined || ghs === "") return "—";
  const n = Number(ghs);
  if (Number.isNaN(n)) return "—";
  if (n >= 1000) return (n / 1000).toFixed(2) + " TH/s";
  return n.toFixed(1) + " GH/s";
}
function fmtDiff(v, disp) {
  if (disp) return String(disp);
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return "—";
  if (n >= 1e12) return (n / 1e12).toFixed(2) + "T";
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "G";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 10000) return (n / 1e3).toFixed(1) + "K";
  return Math.round(n).toLocaleString();
}
function fmtAgo(sec) {
  if (sec === null || sec === undefined || sec === "") return "—";
  const s = Math.max(0, Math.round(Number(sec)));
  if (Number.isNaN(s)) return "—";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s ago`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h ${m}m ago`;
}
function fmtLastShare(d) {
  if (d.last_share_waiting) return "waiting for share…";
  const diff = d.last_submitted_diff;
  const sec = d.last_share_seconds;
  if (diff == null && sec == null) return "—";
  const parts = [];
  if (diff != null) {
    const prefix = d.last_share_estimated ? "≥ " : "";
    parts.push(prefix + fmtDiff(diff));
  }
  if (sec != null) parts.push(fmtAgo(sec));
  return parts.join(" · ");
}
function fmtLoss(frac) {
  if (frac === null || frac === undefined || frac === "") return "";
  const n = Number(frac);
  if (Number.isNaN(n)) return "";
  const pct = n <= 1 ? n * 100 : n;
  return pct.toFixed(pct < 10 ? 2 : 1) + "%";
}
function fmtUptime(sec) {
  if (sec === null || sec === undefined) return "—";
  let s = Math.max(0, Math.floor(Number(sec)));
  const d = Math.floor(s / 86400); s %= 86400;
  const h = Math.floor(s / 3600); s %= 3600;
  const m = Math.floor(s / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}
function tempClass(v, target, max) {
  if (v === null || v === undefined) return "";
  if (max != null && v >= max) return "hot";
  if (target != null && v > target) return "warn";
  return "ok";
}
function errClass(v, low, high) {
  if (v === null || v === undefined) return "";
  if (high != null && v > high) return "err-hot";
  if (low != null && v < low) return "err-ok";
  return "err-warn";
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
async function post(url, body) {
  const opt = {method: "POST", headers: {"Content-Type": "application/json"}};
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(url, opt);
  const txt = await r.text();
  let parsed = null;
  try { parsed = JSON.parse(txt); } catch (_) {}
  if (!r.ok) throw new Error((parsed && parsed.error) || txt || r.status);
  return parsed || {};
}

function renderSparklineSvg(points, idx, color) {
  const vals = (points || []).map(p => p[idx]).filter(v => v != null && !Number.isNaN(Number(v))).map(Number);
  if (vals.length < 2) {
    return `<div style="color:var(--muted);font-size:.71rem;padding:8px 0">Collecting 24h samples…</div>`;
  }
  const w = 160, h = 34, pad = 2;
  let mn = Math.min(...vals), mx = Math.max(...vals);
  if (mx - mn < 1e-6) { mn -= 1; mx += 1; }
  const coords = vals.map((v, i) => {
    const x = pad + (i / (vals.length - 1)) * (w - 2 * pad);
    const y = h - pad - ((v - mn) / (mx - mn)) * (h - 2 * pad);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <polyline fill="none" stroke="${color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" points="${coords.join(" ")}"/>
  </svg>`;
}

function renderSparklines(d) {
  const hist = d.history || [];
  const hrVals = hist.map(p => p[1]).filter(v => v != null);
  const tVals = hist.map(p => p[2]).filter(v => v != null);
  const hrRange = hrVals.length >= 2 ? `${fmtHashrate(Math.min(...hrVals))}–${fmtHashrate(Math.max(...hrVals))}` : "";
  const tRange = tVals.length >= 2 ? `${Math.min(...tVals).toFixed(0)}–${Math.max(...tVals).toFixed(0)}°C` : "";
  return `<div class="spark-grid">
    <div class="spark-box">
      <div class="spark-head"><span>24h Hashrate</span><span>${esc(hrRange)}</span></div>
      ${renderSparklineSvg(hist, 1, "#3ee0b4")}
    </div>
    <div class="spark-box">
      <div class="spark-head"><span>24h Chip Temp</span><span>${esc(tRange)}</span></div>
      ${renderSparklineSvg(hist, 2, "#f0c14a")}
    </div>
  </div>`;
}

function renderFleetSummary(fleet) {
  const f = fleet || {};
  const onlineStr = `${f.online_count ?? 0} / ${f.total_count ?? 0}`;
  const chipStr = f.hottest_chip_c != null ? `${Number(f.hottest_chip_c).toFixed(1)}°C` : "—";
  const vrStr = f.hottest_vr_c != null ? `${Number(f.hottest_vr_c).toFixed(1)}°C` : "—";
  const bestStr = f.best_share != null ? fmtDiff(f.best_share, f.best_share_display) : "—";
  $("fleetSummary").innerHTML = `
    <div class="fleet-stat"><label>Machines Online</label><div class="val">${esc(onlineStr)}</div></div>
    <div class="fleet-stat"><label>Total Hashrate</label><div class="val">${esc(fmtHashrate(f.total_hashrate_ghs))}</div></div>
    <div class="fleet-stat"><label>Total Power</label><div class="val">${esc(fmt(f.total_power_w, 0, "W"))}</div></div>
    <div class="fleet-stat"><label>Fleet Efficiency</label><div class="val">${esc(fmt(f.fleet_j_per_th, 1, "J/TH"))}</div></div>
    <div class="fleet-stat"><label>Hottest Chip</label><div class="val temp ${tempClass(f.hottest_chip_c, 65, 80)}">${esc(chipStr)}</div><div class="tiny">${esc(f.hottest_chip_device || "")}</div></div>
    <div class="fleet-stat"><label>Hottest VR</label><div class="val temp ${tempClass(f.hottest_vr_c, 65, 70)}">${esc(vrStr)}</div><div class="tiny">${esc(f.hottest_vr_device || "")}</div></div>
    <div class="fleet-stat"><label>Best Share Ever</label><div class="val">${esc(bestStr)}</div><div class="tiny">${esc(f.best_share_device || "")}</div></div>
  `;
}

function renderPoolSection(d) {
  const key = domKey(d.name);
  const isOpen = openPoolDevice === d.name;
  const pSummary = d.pool_url
    ? `${d.pool_url}${d.pool_port ? ":" + d.pool_port : ""}${d.pool_user ? " (" + d.pool_user + ")" : ""}`
    : "—";
  const fbSummary = d.fallback_pool_url
    ? `${d.fallback_pool_url}${d.fallback_pool_port ? ":" + d.fallback_pool_port : ""}${d.fallback_pool_user ? " (" + d.fallback_pool_user + ")" : ""}`
    : "";
  const fbBadge = d.using_fallback_stratum ? ` <span class="badge fb">FALLBACK</span>` : "";
  const notice = cardNotice["pool:" + d.name];
  const noticeHtml = notice ? `<div class="mini-note ${notice.cls}">${esc(notice.text)}</div>` : "";
  const isAnt = d.type === "antminer";
  const helpText = isAnt
    ? "Stock Bitmain firmware disables remote CGMiner pool writes by default (requires --api-allow W:<PC-IP>, e.g. Braiins OS / VNish). Saving tests privileged access and reports if denied."
    : "Updates stratum pool via PATCH /api/system (leave password blank to keep existing). ESP-Miner requires a restart for pool changes to apply.";

  let allPoolsHtml = "";
  if (isAnt && Array.isArray(d.pools) && d.pools.length) {
    allPoolsHtml = `<div class="pool-list">${d.pools.map(p => {
      const act = !!p.stratum_active;
      const cls = act ? "pool-row active" : "pool-row";
      const tag = act ? `<span class="badge live">ACTIVE</span>` : `<span class="badge">P${esc(p.priority ?? p.pool)} · ${esc(p.status || "—")}</span>`;
      const ls = (p.last_share_diff != null || p.last_share_seconds != null)
        ? ` · Last ${fmtDiff(p.last_share_diff)} (${fmtAgo(p.last_share_seconds)})` : "";
      return `<div class="${cls}">
        <div style="min-width:0;overflow:hidden;text-overflow:ellipsis"><b>${esc(p.url)}</b><div style="font-size:.7rem;color:var(--muted)">${esc(p.user || "")}${esc(ls)}</div></div>
        ${tag}
      </div>`;
    }).join("")}</div>`;
  }

  if (!isOpen) {
    return `<div class="panel-box">
      <div class="panel-head">
        <span>Pool${fbBadge}</span>
        <button class="icon-btn" type="button" onclick="togglePoolEditor('${esc(d.name)}')">${isAnt ? "Pool / Edit" : "Edit pool"}</button>
      </div>
      <div class="chips"><b>Primary:</b> ${esc(pSummary)}${fbSummary ? `<br><b>Fallback:</b> ${esc(fbSummary)}` : ""}</div>
      ${allPoolsHtml}${noticeHtml}
    </div>`;
  }

  return `<div class="panel-box">
    <div class="panel-head">
      <span>Pool settings</span>
      <button class="icon-btn" type="button" onclick="togglePoolEditor('${esc(d.name)}')">Close</button>
    </div>
    <form onsubmit="return submitPool(event, '${esc(d.name)}', ${isAnt ? "true" : "false"})">
      <div class="pool-grid">
        <div class="field"><label>Primary Stratum URL</label><input id="pUrl_${key}" type="text" value="${esc(d.pool_url || "")}" placeholder="stratum.pool.com" required></div>
        <div class="field"><label>Port</label><input id="pPort_${key}" type="number" min="1" max="65535" value="${esc(d.pool_port || 3333)}" required></div>
        <div class="field"><label>Worker / User</label><input id="pUser_${key}" type="text" value="${esc(d.pool_user || "")}" placeholder="wallet.worker" required></div>
        <div class="field"><label>Password (never shown)</label><input id="pPass_${key}" type="password" value="" placeholder="Keep existing" autocomplete="new-password"></div>
        <div class="field"><label>Fallback URL (optional)</label><input id="fbUrl_${key}" type="text" value="${esc(d.fallback_pool_url || "")}" placeholder="backup.pool.com"></div>
        <div class="field"><label>Fallback Port</label><input id="fbPort_${key}" type="number" min="1" max="65535" value="${esc(d.fallback_pool_port || "")}" placeholder="3333"></div>
        <div class="field"><label>Fallback Worker</label><input id="fbUser_${key}" type="text" value="${esc(d.fallback_pool_user || "")}" placeholder="wallet.worker"></div>
        <div class="field"><label>Fallback Password</label><input id="fbPass_${key}" type="password" value="" placeholder="Keep existing" autocomplete="new-password"></div>
      </div>
      <div class="mini-note">${esc(helpText)}</div>
      ${noticeHtml}
      <div class="row-actions" style="margin-top:8px">
        <button class="add-btn" type="submit">${isAnt ? "Try CGMiner pool update" : "Save &amp; Restart miner"}</button>
        <button class="icon-btn" type="button" onclick="togglePoolEditor('${esc(d.name)}')">Cancel</button>
      </div>
    </form>
  </div>`;
}

function renderCard(d, dry) {
  const key = domKey(d.name);
  const unparsed = !!d.unparsed || d.last_action === "unparsed";
  const online = !!d.online || unparsed;
  const enabled = !!d.enabled;
  const tunable = d.tunable !== false && d.type !== "antminer";
  const isEditing = editingDevice === d.name;
  const parseErr = (unparsed || (online && d.error && !d.asic_temp_c && !d.frequency_mhz && !d.hashrate_ghs))
    ? `<div class="parse-err"><strong>Online but unparsed.</strong> ${esc(d.error || d.last_reason || "The miner answered, but telemetry could not be read.")}</div>`
    : "";

  const order = (lastStatus && lastStatus.devices) ? lastStatus.devices.map(x => x.name) : [];
  const idx = order.indexOf(d.name);
  const moveBtns = order.length > 1
    ? `<button class="icon-btn" type="button" title="Move card left/up" ${idx <= 0 ? "disabled" : ""} onclick="moveDevice('${esc(d.name)}', -1)">&#9664;</button>`
      + `<button class="icon-btn" type="button" title="Move card right/down" ${idx < 0 || idx >= order.length - 1 ? "disabled" : ""} onclick="moveDevice('${esc(d.name)}', 1)">&#9654;</button>`
    : "";

  const titleHtml = isEditing
    ? `<form class="rename-form" onsubmit="return submitRename(event, '${esc(d.name)}')">
         <input id="renameInput" type="text" value="${esc(editingDraft)}" maxlength="64" autofocus>
         <button class="icon-btn" type="submit">Save</button>
         <button class="icon-btn" type="button" onclick="cancelRename()">Cancel</button>
       </form>`
    : `<div class="name-row">
         <div class="name"><span class="dot ${online ? "on" : "off"}"></span>${esc(d.name)}</div>
         <button class="icon-btn" type="button" title="Rename device" onclick="startRename('${esc(d.name)}')">Rename</button>
         <button class="icon-btn danger" type="button" title="Remove this machine from dashboard" onclick="removeDevice('${esc(d.name)}', ${online ? "true" : "false"})">Remove</button>
         ${moveBtns}
       </div>`;

  const globalAuto = !!(lastStatus && lastStatus.autotune_enabled);
  const badgeHtml = !tunable
    ? `<span class="badge mon" title="Monitor-only device">${esc(d.firmware || "Monitor only")}</span>`
    : (!enabled
        ? `<span class="badge paused">autotune off</span>`
        : (!globalAuto
            ? `<span class="badge paused" title="Per-machine autotune is ON, but global autotune is OFF">paused (global off)</span>`
            : `<span class="badge ${dry ? "dry" : "live"}">${dry ? "watching (dry-run)" : "autotune on"}</span>`));

  const subtitle = `${esc(d.ip)} · ${esc(d.type)}${d.model ? " · " + esc(d.model) : ""}${d.version ? " · " + esc(d.version) : ""}`;
  const action = d.last_action ? d.last_action : "—";
  const when = d.last_action_at ? d.last_action_at : "";

  if (!tunable) {
    const hrSub = [
      d.hashrate_1m_ghs != null ? `1m ${fmtHashrate(d.hashrate_1m_ghs)}` : "",
      d.hashrate_5m_ghs != null ? `5m ${fmtHashrate(d.hashrate_5m_ghs)}` : "",
      d.hashrate_15m_ghs != null ? `15m ${fmtHashrate(d.hashrate_15m_ghs)}` : "",
      d.hashrate_24h_ghs != null ? `24h ${fmtHashrate(d.hashrate_24h_ghs)}` : "",
      d.hashrate_avg_ghs != null ? `avg ${fmtHashrate(d.hashrate_avg_ghs)}` : "",
    ].filter(Boolean).join(" · ");

    const pwrStr = d.power_w != null
      ? (d.power_limit_w ? `${Math.round(d.power_w)} / ${Math.round(d.power_limit_w)} W` : fmt(d.power_w, 0, "W"))
      : "—";
    const pwrPct = (d.power_w && d.power_limit_w) ? Math.min(100, (d.power_w / d.power_limit_w) * 100) : null;
    const pwrBar = pwrPct != null ? `<div class="bar-track"><div class="bar-fill ${pwrPct > 95 ? "warn" : ""}" style="width:${pwrPct.toFixed(0)}%"></div></div>` : "";

    let boardsHtml = "";
    if (Array.isArray(d.boards) && d.boards.length) {
      boardsHtml = `<div class="metric full"><label>Hashboards (${d.boards.length})</label>
        <table class="board-table">
          <thead><tr><th>#</th><th>5s / 15m / Avg</th><th>Nom.</th><th>Chip / Board</th><th>HW Err</th><th>Status</th></tr></thead>
          <tbody>${d.boards.map(b => `<tr>
            <td><b>B${esc(b.id)}</b></td>
            <td>${esc(fmtHashrate(b.hashrate_5s_ghs))} / ${esc(fmtHashrate(b.hashrate_15m_ghs))} / ${esc(fmtHashrate(b.hashrate_av_ghs))}</td>
            <td>${esc(fmtHashrate(b.nominal_ghs))}</td>
            <td><span class="temp ${tempClass(b.chip_temp_c, 75, 85)}">${esc(fmt(b.chip_temp_c, 0, "°"))}</span> / ${esc(fmt(b.board_temp_c, 0, "°"))}</td>
            <td>${esc(b.hw_errors ?? 0)}</td>
            <td>${esc(b.tuner_status || b.status || "—")}</td>
          </tr>`).join("")}</tbody>
        </table>
      </div>`;
    } else {
      const boardStr = (d.board_temps && d.board_temps.length) ? d.board_temps.map(t => Number(t).toFixed(0) + "°").join(" · ") : "—";
      const chipStr = (d.chip_temps && d.chip_temps.length) ? d.chip_temps.map(t => Number(t).toFixed(0) + "°").join(" · ") : "—";
      boardsHtml = `<div class="metric full"><label>Board / Chip temps</label><div class="chips">Board: <b>${esc(boardStr)}</b> &nbsp;|&nbsp; Chip: <b>${esc(chipStr)}</b></div></div>`;
    }

    let fansHtml = `<div class="chips">—</div>`;
    if (Array.isArray(d.fans) && d.fans.length) {
      fansHtml = `<div class="fan-grid">${d.fans.map((f, idx) => {
        const pct = f.speed_pct != null ? Math.max(0, Math.min(100, Number(f.speed_pct))) : null;
        const rpmTxt = f.no_tach ? "speed % only (no RPM sensor)" : (f.rpm != null ? f.rpm + " RPM" : "—");
        return `<div class="fan-item">F${esc((f.id ?? idx) + 1)}: <b>${esc(rpmTxt)}</b>${pct != null ? ` (${pct.toFixed(0)}%)` : ""}
          ${pct != null ? `<div class="bar-track"><div class="bar-fill info" style="width:${pct.toFixed(0)}%"></div></div>` : ""}
        </div>`;
      }).join("")}</div>`;
    } else if (d.fan_rpms && d.fan_rpms.length) {
      fansHtml = `<div class="chips"><b>${esc(d.fan_rpms.join(" · ") + " RPM")}</b></div>`;
    }

    const shareParts = [
      d.shares_accepted != null ? `Acc: <b>${Number(d.shares_accepted).toLocaleString()}</b>` : "",
      d.shares_rejected != null ? `Rej: <b>${Number(d.shares_rejected).toLocaleString()}</b>${d.pool_rejected_pct != null ? ` (${Number(d.pool_rejected_pct).toFixed(2)}%)` : ""}` : "",
      d.shares_stale != null ? `Stale: <b>${Number(d.shares_stale).toLocaleString()}</b>` : "",
      d.hw_errors != null ? `HW Err: <b>${Number(d.hw_errors).toLocaleString()}</b>` : "",
    ].filter(Boolean).join(" &middot; ");

    const bosSection = isFieldVisible("s19_tuner") ? renderBraiinsPowerSection(d) : "";
    const metricsAnt = [
      isFieldVisible("hashrate") ? `<div class="metric"><label>Hashrate (5s)</label><div class="val">${fmtHashrate(d.hashrate_ghs)}</div>${hrSub ? `<div class="chips">${esc(hrSub)}</div>` : ""}</div>` : "",
      isFieldVisible("asic_temp") ? `<div class="metric"><label>Max chip temp</label><div class="val temp ${tempClass(d.asic_temp_c, 75, 85)}">${fmt(d.asic_temp_c, 1, "°C")}</div>${isFieldVisible("wifi_system") ? `<div class="chips">Uptime: <b>${esc(fmtUptime(d.uptime_sec))}</b></div>` : ""}</div>` : "",
      isFieldVisible("power_efficiency") ? `<div class="metric"><label>Power &amp; Limit</label><div class="val">${esc(pwrStr)}</div>${pwrBar}${isFieldVisible("s19_tuner") && d.tuner_mode ? `<div class="chips">Tuner: <b>${esc(d.tuner_mode)}</b></div>` : ""}</div>` : "",
      isFieldVisible("power_efficiency") ? `<div class="metric"><label>Efficiency</label><div class="val">${fmt(d.j_per_th, 1, "J/TH")}</div>${isFieldVisible("shares_best_diff") ? `<div class="chips">Best share: <b>${esc(fmtDiff(d.best_diff, d.best_diff_display))}</b></div>` : ""}</div>` : "",
      isFieldVisible("s19_boards") ? boardsHtml : "",
      isFieldVisible("fans") ? `<div class="metric full"><label>Fans</label>${fansHtml}</div>` : "",
      isFieldVisible("last_share") ? `<div class="metric"><label>Last share</label><div class="val" style="font-size:.95rem">${esc(fmtLastShare(d))}</div></div>` : "",
      isFieldVisible("shares_best_diff") ? `<div class="metric"><label>Shares</label><div class="chips">${shareParts || "—"}</div></div>` : "",
    ].filter(Boolean).join("");
    return `<article class="card${online ? (unparsed ? " unparsed" : "") : " offline"}" data-device="${esc(d.name)}" ondragover="onCardDragOver(event)" ondragleave="onCardDragLeave(event)" ondrop="onCardDrop(event, '${esc(d.name)}')">
      <div class="card-head" draggable="true" ondragstart="onCardDragStart(event, '${esc(d.name)}')" ondragend="onCardDragEnd(event)">
        <div>${titleHtml}<div class="ip">${subtitle}</div></div>
        ${badgeHtml}
      </div>
      <div class="metrics">${metricsAnt}</div>
      ${parseErr}${isFieldVisible("sparklines") ? renderSparklines(d) : ""}${bosSection}${isFieldVisible("pool_info") ? renderPoolSection(d) : ""}
      ${isFieldVisible("action_log") ? `<div class="action"><strong>${esc(action)}</strong> ${esc(when)}<br>${esc(d.firmware_note || d.last_reason || "Monitor only")}</div>` : ""}
      <div class="row-actions">
        <button class="btn start" disabled title="Monitor-only: Antminer autotune is disabled in this app">Start autotune (monitor only)</button>
      </div>
    </article>`;
  }

  const asicCls = tempClass(d.asic_temp_c, d.asic_target_c, d.asic_max_c);
  const vrCls = tempClass(d.vr_temp_c, d.vr_target_c, d.vr_max_c);
  let chips = "";
  if (d.type === "nerdoctaxe" && d.asic_temps && d.asic_temps.length) {
    const parts = d.asic_temps.map(t => {
      const hot = d.hottest_asic_temp_c != null && Number(t) === Number(d.hottest_asic_temp_c);
      return hot ? `<b>${Number(t).toFixed(1)}°</b>` : `${Number(t).toFixed(1)}°`;
    });
    chips = `<div class="chips">Chips: ${parts.join(" · ")}</div>`;
  } else if (d.board_temps && d.board_temps.length) {
    chips = `<div class="chips">Board: <b>${Number(d.board_temps[0]).toFixed(1)}°C</b></div>`;
  }

  const expPct = (d.hashrate_ghs != null && d.expected_hashrate_ghs)
    ? ` <small>(${((d.hashrate_ghs / d.expected_hashrate_ghs) * 100).toFixed(0)}% of ${fmtHashrate(d.expected_hashrate_ghs)})</small>`
    : "";
  const hrSubs = [
    d.hashrate_1m_ghs != null ? `1m ${fmtHashrate(d.hashrate_1m_ghs)}` : "",
    d.hashrate_10m_ghs != null ? `10m ${fmtHashrate(d.hashrate_10m_ghs)}` : "",
    d.hashrate_1h_ghs != null ? `1h ${fmtHashrate(d.hashrate_1h_ghs)}` : "",
    d.hashrate_24h_ghs != null ? `1d ${fmtHashrate(d.hashrate_24h_ghs)}` : "",
  ].filter(Boolean).join(" · ");

  const setV = fmt(d.core_voltage_mv, 0, "mV");
  const actV = fmt(d.core_voltage_actual_mv, 0, "mV");
  const volt = (d.core_voltage_actual_mv && d.core_voltage_mv && Number(d.core_voltage_actual_mv) !== Number(d.core_voltage_mv))
    ? `${esc(setV)} <small>meas. ${esc(actV)}</small>`
    : esc(actV !== "—" ? (setV !== "—" ? setV + (actV !== setV ? ` <small>meas. ${actV}</small>` : "") : actV) : setV);

  const rollErr = d.rolling_error_pct;
  const instErr = d.error_pct;
  const eCls = errClass(rollErr, d.error_low_pct, d.error_high_pct);
  const errMain = rollErr != null ? `${Number(rollErr).toFixed(2)}%` : (instErr != null ? `${Number(instErr).toFixed(2)}%` : "—");
  const errSub = d.error_window_size ? `${d.error_window_count || 0}/${d.error_window_size} polls` : "";
  const instSub = (instErr != null && rollErr != null) ? `last ${Number(instErr).toFixed(2)}%` : "";
  const srcSub = d.error_source ? (d.error_source === "errorPercentage" ? "errorPercentage" : `${d.error_source} (fallback)`) : "";
  const errNote = [instSub, errSub, srcSub].filter(Boolean).join(" · ");

  let fanBarHtml = `<div class="chips">—</div>`;
  if (Array.isArray(d.fans) && d.fans.length) {
    fanBarHtml = d.fans.map(f => {
      const pct = f.speed_pct != null ? Math.max(0, Math.min(100, Number(f.speed_pct))) : null;
      const rpmTxt = f.no_tach
        ? `${pct != null ? pct.toFixed(0) + "%" : "—"} · speed % only (no RPM sensor)`
        : `${f.rpm != null ? f.rpm + " RPM" : "—"}${pct != null ? " · " + pct.toFixed(0) + "%" : ""}`;
      return `<div class="chips"><b>${esc(rpmTxt)}</b></div>
        ${pct != null ? `<div class="bar-track"><div class="bar-fill info" style="width:${pct.toFixed(0)}%"></div></div>` : ""}`;
    }).join("");
  } else if (d.fan_rpms && d.fan_rpms.length) {
    fanBarHtml = `<div class="chips"><b>${esc(d.fan_rpms.join(" · "))} RPM</b></div>`;
  }

  let domainsHtml = "";
  if (Array.isArray(d.asic_domains) && d.asic_domains.length) {
    const blocks = d.asic_domains.map(a => {
      const doms = a.domains || [];
      if (!doms.length) return "";
      const mx = Math.max(...doms, 1);
      const bars = doms.map((v, i) => {
        const h = Math.max(12, Math.round((Number(v) / mx) * 100));
        return `<div class="dom-bar" style="height:${h}%" title="Domain ${i}: ${Number(v).toFixed(1)} GH/s"></div>`;
      }).join("");
      const errBadge = a.error_count != null ? ` <small>(err ${a.error_count})</small>` : "";
      return `<div class="chips">ASIC #${a.id}${errBadge}: ${doms.map(v => Number(v).toFixed(0)).join(" / ")} GH/s</div><div class="domains-row">${bars}</div>`;
    }).filter(Boolean).join("");
    if (blocks) {
      domainsHtml = `<div class="metric full"><label>ASIC Hashrate Domains</label>${blocks}</div>`;
    }
  }

  const shareText = (d.shares_accepted != null || d.shares_rejected != null)
    ? `${Number(d.shares_accepted ?? 0).toLocaleString()} acc / ${Number(d.shares_rejected ?? 0).toLocaleString()} rej`
    : "—";
  const rejList = (d.rejected_reasons || []).map(r => `${r.message}${r.count != null ? " (" + r.count + ")" : ""}`).join(", ");
  const bestLine = [
    d.best_session_diff != null ? `Sess: <b>${esc(fmtDiff(d.best_session_diff, d.best_session_diff_display))}</b>` : "",
    d.best_diff != null ? `Best: <b>${esc(fmtDiff(d.best_diff, d.best_diff_display))}</b>` : "",
  ].filter(Boolean).join(" · ");

  const wifiStr = d.wifi_rssi != null ? `${d.wifi_rssi} dBm (${d.wifi_quality || ""})` : "—";
  const discParts = [];
  if (d.stratum_disconnects != null) discParts.push(`stratum disc: ${d.stratum_disconnects}`);
  if (d.wifi_disconnects != null) discParts.push(`wifi disc: ${d.wifi_disconnects}`);
  const pingParts = [];
  if (d.ping_rtt_ms != null) pingParts.push(`ping ${Number(d.ping_rtt_ms).toFixed(1)} ms`);
  if (d.ping_loss != null) pingParts.push(`loss ${fmtLoss(d.ping_loss)}`);
  const netExtras = [
    d.uptime_sec != null ? `up ${fmtUptime(d.uptime_sec)}` : "",
    pingParts.join(" · "),
    ...discParts,
    d.block_height != null ? `blk #${Number(d.block_height).toLocaleString()}` : "",
  ].filter(Boolean).join(" · ");

  const safetyNotes = [];
  if (d.last_action === "fault_hold") safetyNotes.push("Safety fault-hold active (step-up paused)");
  if (d.emergency_lockout_remaining_sec > 0) safetyNotes.push(`Post-emergency lockout: ${Math.ceil(d.emergency_lockout_remaining_sec / 60)}m left${d.rollback_freq ? " (rollback " + d.rollback_freq + " MHz)" : ""}`);
  if (d.sensor_suspect) safetyNotes.push("Suspect temp sensor guard active");
  const safetyBanner = safetyNotes.length ? `<div class="vreason" style="border-left:3px solid var(--warn)"><strong>Safety Guard:</strong> ${esc(safetyNotes.join(" · "))}</div>` : "";
  const vReasonHtml = safetyBanner + (d.voltage_control
    ? `<div class="vreason"><strong>Voltage (${d.min_voltage_mv || 1150}-${d.max_voltage_mv || 1250} mV${d.learned_min_voltage_mv ? `, learned floor ${d.learned_min_voltage_mv} mV` : ""}):</strong> ${esc(d.voltage_reason || "waiting for samples")}</div>`
    : `<div class="vreason"><strong>Autotune Voltage:</strong> frequency-only (${d.type === "nerdoctaxe" ? "manual voltage allowed when stopped" : "enable Auto voltage control in Overrides if supported"}, max 1250 mV)</div>`);

  const freqVal = d.frequency_mhz != null ? Math.round(Number(d.frequency_mhz)) : (d.manual_frequency_mhz != null ? Math.round(Number(d.manual_frequency_mhz)) : "");
  const voltVal = d.core_voltage_mv != null ? Math.round(Number(d.core_voltage_mv)) : (d.manual_voltage_mv != null ? Math.round(Number(d.manual_voltage_mv)) : "");
  const freqMax = Math.min(850, Number(d.max_freq || 850));
  const voltMax = Math.min(1250, Number(d.max_voltage_mv || 1250));
  const manualHint = enabled
    ? "Stop autotune to edit manual frequency or core voltage."
    : (d.type === "nerdoctaxe"
        ? `Allowed ${d.min_freq || 400}-${freqMax} MHz · ${d.min_voltage_mv || 1050}-${voltMax} mV (1250 mV cap; autotune won't change OCTAxe voltage).`
        : `Allowed ${d.min_freq || 400}-${freqMax} MHz · ${d.min_voltage_mv || 1150}-${voltMax} mV (1250 mV hard cap).`);
  const mNotice = cardNotice["manual:" + d.name];
  const mNoticeHtml = mNotice ? `<div class="mini-note ${mNotice.cls}">${esc(mNotice.text)}</div>` : "";

  const manualHtml = `<div class="panel-box">
    <div class="panel-head"><span>Manual Control</span><span>${enabled ? "Locked (Autotune ON)" : "Editable (Autotune OFF)"}</span></div>
    <form class="manual-grid" onsubmit="return submitManual(event, '${esc(d.name)}')">
      <div class="field">
        <label>Frequency (MHz)</label>
        <input id="manFreq_${key}" type="number" min="${esc(d.min_freq || 400)}" max="${freqMax}" step="1" value="${esc(freqVal)}" ${enabled ? "disabled" : ""}>
      </div>
      <div class="field">
        <label>Core Voltage (mV)</label>
        <input id="manVolt_${key}" type="number" min="${esc(d.min_voltage_mv || 1050)}" max="${voltMax}" step="5" value="${esc(voltVal)}" ${enabled ? "disabled" : ""}>
      </div>
      <button class="add-btn" type="submit" ${enabled ? "disabled" : ""}>Apply</button>
    </form>
    <div class="mini-note">${esc(manualHint)}</div>
    ${mNoticeHtml}
  </div>`;

  const metricsTunable = [
    isFieldVisible("hashrate") ? `<div class="metric full"><label>Hashrate</label><div class="val">${fmtHashrate(d.hashrate_ghs)}${expPct}</div>${hrSubs ? `<div class="chips">${esc(hrSubs)}</div>` : ""}</div>` : "",
    isFieldVisible("asic_temp") ? `<div class="metric"><label>ASIC temp</label><div class="val temp ${asicCls}">${fmt(d.asic_temp_c, 1, "°C")}</div>${chips}</div>` : "",
    (isFieldVisible("vr_temp") || isFieldVisible("fans")) ? `<div class="metric"><label>${isFieldVisible("vr_temp") ? "VR temp" : "Fans"}</label>${isFieldVisible("vr_temp") ? `<div class="val temp ${vrCls}">${fmt(d.vr_temp_c, 1, "°C")}</div>` : ""}${isFieldVisible("fans") ? fanBarHtml : ""}</div>` : "",
    isFieldVisible("frequency") ? `<div class="metric"><label>Frequency</label><div class="val">${fmt(d.frequency_mhz, 0, "MHz")}${d.actual_frequency_mhz != null ? ` <small>actual ${fmt(d.actual_frequency_mhz, 1)}</small>` : ""}</div></div>` : "",
    isFieldVisible("core_voltage") ? `<div class="metric"><label>Core voltage</label><div class="val">${volt}</div></div>` : "",
    isFieldVisible("power_efficiency") ? `<div class="metric"><label>Power &amp; Efficiency</label><div class="val">${fmt(d.power_w, 1, "W")}${d.j_per_th != null ? ` <small>${fmt(d.j_per_th, 1, "J/TH")}</small>` : ""}</div></div>` : "",
    isFieldVisible("error_rate") ? `<div class="metric"><label>HW error rate</label><div class="val ${eCls}">${esc(errMain)}${errNote ? ` <small>${esc(errNote)}</small>` : ""}</div></div>` : "",
    isFieldVisible("last_share") ? `<div class="metric"><label>Last share</label><div class="val" style="font-size:.95rem">${esc(fmtLastShare(d))}</div></div>` : "",
    isFieldVisible("shares_best_diff") ? `<div class="metric"><label>Shares &amp; Best diff</label><div class="chips"><b>${esc(shareText)}</b>${bestLine ? `<br>${bestLine}` : ""}${rejList ? `<br>Rej: ${esc(rejList)}` : ""}</div></div>` : "",
    isFieldVisible("wifi_system") ? `<div class="metric full"><label>WiFi &amp; System</label><div class="chips">WiFi: <b>${esc(wifiStr)}</b>${netExtras ? ` · ${esc(netExtras)}` : ""}</div></div>` : "",
    isFieldVisible("asic_domains") ? domainsHtml : "",
  ].filter(Boolean).join("");

  return `<article class="card${online ? (unparsed ? " unparsed" : "") : " offline"}" data-device="${esc(d.name)}" ondragover="onCardDragOver(event)" ondragleave="onCardDragLeave(event)" ondrop="onCardDrop(event, '${esc(d.name)}')">
    <div class="card-head" draggable="true" ondragstart="onCardDragStart(event, '${esc(d.name)}')" ondragend="onCardDragEnd(event)">
      <div>${titleHtml}<div class="ip">${subtitle}</div></div>
      ${badgeHtml}
    </div>
    <div class="metrics">${metricsTunable}</div>
    ${parseErr}${isFieldVisible("sparklines") ? renderSparklines(d) : ""}${isFieldVisible("voltage_reason") ? vReasonHtml : ""}${isFieldVisible("manual_control") ? manualHtml : ""}${renderOverridesSection(d)}${isFieldVisible("pool_info") ? renderPoolSection(d) : ""}
    ${isFieldVisible("action_log") ? `<div class="action"><strong>${esc(action)}</strong> ${esc(when)}<br>${esc(d.last_reason || "")}</div>` : ""}
    <div class="row-actions">
      <button class="btn start" ${enabled ? "disabled" : ""} onclick="setDevice('${esc(d.name)}', true)">Start autotune</button>
      <button class="btn stop" ${enabled ? "" : "disabled"} onclick="setDevice('${esc(d.name)}', false)">Stop autotune</button>
    </div>
  </article>`;
}

function renderOverridesSection(d) {
  const key = domKey(d.name);
  const ov = d.overrides || {};
  const ovCount = Object.keys(ov).length;
  const isOpen = openOverrideDevice === d.name;
  const notice = cardNotice["ov:" + d.name];
  const noticeHtml = notice ? `<div class="mini-note ${notice.cls}">${esc(notice.text)}</div>` : "";
  const toggleBtn = `<button class="icon-btn" type="button" onclick="toggleOverrideEditor('${esc(d.name)}')">${isOpen ? "Hide overrides" : (ovCount ? `Overrides (${ovCount})` : "Overrides")}</button>`;
  if (!isOpen) {
    return `<div class="panel-box"><div class="panel-head"><span>Per-Machine Caps &amp; Targets</span>${toggleBtn}</div><div class="chips">${ovCount ? `Custom overrides active: <b>${esc(Object.keys(ov).join(", "))}</b>` : `Using global caps &amp; targets (max ${esc(d.max_freq)} MHz · ASIC ${esc(d.asic_max_c)}°C · VR ${esc(d.vr_max_c)}°C)`}</div>${noticeHtml}</div>`;
  }
  const ph = (fallback) => `Global (${fallback ?? ""})`;
  const gt = (lastStatus && lastStatus.settings && lastStatus.settings.global_tuner) || {};
  const isNerd = d.type === "nerdoctaxe";
  const ocOn = !!d.overclock_opt_in;
  const maxFreqCap = isNerd ? (ocOn ? 850 : 800) : 850;
  const globalFreqPh = isNerd ? Math.min(maxFreqCap, Number(gt.max_freq ?? 800)) : (gt.max_freq ?? 850);
  const nerdOptHtml = isNerd ? `<div style="margin-bottom:8px;padding:6px 8px;border:1px solid var(--warn);border-radius:8px;font-size:.78rem">
      <label class="field-check"><input id="ov_overclock_opt_in_${key}" type="checkbox" ${ocOn ? "checked" : ""} onchange="const el=$('ov_max_freq_${key}'); if(el){el.max=this.checked?850:800;}"><span><b>Allow &gt;800 MHz overclock (up to 850 MHz)</b></span></label>
      <div class="mini-note" style="color:var(--warn)">Warning: NerdOCTAxe firmware rates 525–800 MHz as normal operation. 825–850 MHz significantly raises VR/MOSFET heat and power draw. Only enable with upgraded VR cooling.</div>
    </div>` : "";
  const vctrlHtml = !isNerd ? `<div style="margin-bottom:8px;padding:6px 8px;border:1px solid var(--border);border-radius:8px;font-size:.78rem">
      <label class="field-check"><input id="ov_voltage_control_${key}" type="checkbox" ${d.voltage_control ? "checked" : ""}><span><b>Auto voltage control</b> (raise core voltage when HW errors &gt; Error Max, clamped to Max Volt / 1250 mV)</span></label>
    </div>` : "";
  return `<div class="panel-box">
    <div class="panel-head"><span>Per-Machine Caps &amp; Targets</span>${toggleBtn}</div>
    <form onsubmit="return submitOverrides(event, '${esc(d.name)}')">
      ${nerdOptHtml}${vctrlHtml}
      <div class="caps-grid">
        <div class="field"><label>Max Freq (MHz)</label><input id="ov_max_freq_${key}" type="number" min="400" max="${maxFreqCap}" step="25" value="${esc(ov.max_freq ?? "")}" placeholder="${esc(ph(globalFreqPh))}"></div>
        <div class="field"><label>Emerg Drop (MHz)</label><input id="ov_emergency_step_mhz_${key}" type="number" min="5" max="200" step="5" value="${esc(ov.emergency_step_mhz ?? "")}" placeholder="${esc(ph(gt.emergency_step_mhz ?? 50))}"></div>
        <div class="field"><label>Min Volt (mV)</label><input id="ov_min_voltage_mv_${key}" type="number" min="1000" max="1250" step="5" value="${esc(ov.min_voltage_mv ?? "")}" placeholder="${esc(ph(gt.min_voltage_mv ?? 1150))}"></div>
        <div class="field"><label>Max Volt (mV)</label><input id="ov_max_voltage_mv_${key}" type="number" min="1000" max="1250" step="5" value="${esc(ov.max_voltage_mv ?? "")}" placeholder="${esc(ph(gt.max_voltage_mv ?? 1250))}"></div>
        <div class="field"><label>Emerg Reset (mV)</label><input id="ov_emergency_voltage_mv_${key}" type="number" min="1000" max="1250" step="5" value="${esc(ov.emergency_voltage_mv ?? "")}" placeholder="${esc(ph(gt.emergency_voltage_mv ?? 1210))}"></div>
        <div class="field"><label>ASIC Target (°C)</label><input id="ov_asic_target_c_${key}" type="number" min="35" max="85" step="0.5" value="${esc(ov.asic_target_c ?? "")}" placeholder="${esc(ph(gt.asic_target_c ?? 59))}"></div>
        <div class="field"><label>ASIC Emerg (°C)</label><input id="ov_asic_max_c_${key}" type="number" min="45" max="90" step="0.5" value="${esc(ov.asic_max_c ?? "")}" placeholder="${esc(ph(gt.asic_max_c ?? 62.5))}"></div>
        <div class="field"><label>VR Target (°C)</label><input id="ov_vr_target_c_${key}" type="number" min="35" max="90" step="0.5" value="${esc(ov.vr_target_c ?? "")}" placeholder="${esc(ph(gt.vr_target_c ?? 62))}"></div>
        <div class="field"><label>VR Emerg (°C)</label><input id="ov_vr_max_c_${key}" type="number" min="45" max="95" step="0.5" value="${esc(ov.vr_max_c ?? "")}" placeholder="${esc(ph(gt.vr_max_c ?? 65))}"></div>
        <div class="field"><label>Error Low (%)</label><input id="ov_error_low_pct_${key}" type="number" min="0" max="10" step="0.1" value="${esc(ov.error_low_pct ?? "")}" placeholder="${esc(ph(gt.error_low_pct ?? 0.5))}"></div>
        <div class="field"><label>Error Max (%)</label><input id="ov_error_high_pct_${key}" type="number" min="0.1" max="20" step="0.1" value="${esc(ov.error_high_pct ?? "")}" placeholder="${esc(ph(gt.error_high_pct ?? 1.0))}"></div>
      </div>
      <div class="mini-note">Leave a box blank to inherit the Global setting.</div>
      ${noticeHtml}
      <div class="row-actions" style="margin-top:8px">
        <button class="add-btn" type="submit">Save Overrides</button>
        <button class="icon-btn" type="button" onclick="resetOverrides('${esc(d.name)}')">Reset to Global</button>
      </div>
    </form>
  </div>`;
}

function renderBraiinsPowerSection(d) {
  const key = domKey(d.name);
  const isOpen = openBosDevice === d.name;
  const enabled = !!d.bos_power_mgmt;
  const notice = cardNotice["bos:" + d.name];
  const noticeHtml = notice ? `<div class="mini-note ${notice.cls}">${esc(notice.text)}</div>` : "";
  const toggleBtn = `<button class="icon-btn" type="button" onclick="toggleBosEditor('${esc(d.name)}')">${isOpen ? "Hide" : "Configure"}</button>`;
  const summaryLine = `Auto power-target: <b>${enabled ? "ON" : "OFF"}</b> (${esc(d.bos_min_watts ?? 2000)}–${esc(d.bos_max_watts ?? 3500)} W, step ${esc(d.bos_step_watts ?? 50)} W, cooldown ${esc(d.bos_cooldown_sec ?? 300)}s)`;
  const lastLine = d.bos_last_reason ? `<div class="mini-note">${esc(d.bos_last_reason)}</div>` : "";
  if (!isOpen) {
    return `<div class="panel-box">
      <div class="panel-head"><span>Braiins OS+ Power Target</span><div style="display:flex;gap:6px"><button class="icon-btn" type="button" onclick="testBraiinsConnection('${esc(d.name)}', false)">Test connection</button>${toggleBtn}</div></div>
      <div class="chips">${summaryLine}</div>
      ${lastLine}${noticeHtml}
    </div>`;
  }
  return `<div class="panel-box">
    <div class="panel-head"><span>Braiins OS+ Power Target</span>${toggleBtn}</div>
    <form onsubmit="return submitBraiinsPower(event, '${esc(d.name)}')">
      <label class="field-check" style="margin-bottom:8px">
        <input id="bos_en_${key}" type="checkbox" ${enabled ? "checked" : ""}>
        <span>Enable gentle Braiins OS+ power-target nudging (default OFF)</span>
      </label>
      <div class="bos-grid">
        <div class="field"><label>Min Watts</label><input id="bos_min_${key}" type="number" min="500" max="6000" step="25" value="${esc(d.bos_min_watts ?? 2000)}"></div>
        <div class="field"><label>Max Watts</label><input id="bos_max_${key}" type="number" min="500" max="6000" step="25" value="${esc(d.bos_max_watts ?? 3500)}"></div>
        <div class="field"><label>Step Watts</label><input id="bos_step_${key}" type="number" min="10" max="500" step="10" value="${esc(d.bos_step_watts ?? 50)}"></div>
        <div class="field"><label>Cooldown (sec)</label><input id="bos_cd_${key}" type="number" min="30" max="3600" step="15" value="${esc(d.bos_cooldown_sec ?? 300)}"></div>
        <div class="field"><label>Chip Target (°C)</label><input id="bos_tgt_${key}" type="number" min="40" max="95" step="0.5" value="${esc(d.bos_chip_target_c ?? 75)}"></div>
        <div class="field"><label>Chip Hot (°C)</label><input id="bos_hot_${key}" type="number" min="45" max="105" step="0.5" value="${esc(d.bos_chip_hot_c ?? 82)}"></div>
        <div class="field"><label>Username</label><input id="bos_user_${key}" type="text" value="${esc(d.bos_username || "root")}"></div>
        <div class="field"><label>gRPC Port</label><input id="bos_port_${key}" type="number" min="1" max="65535" value="${esc(d.bos_grpc_port ?? 50051)}"></div>
        <div class="field full"><label>Password</label><input id="bos_pass_${key}" type="password" value="" placeholder="${d.bos_has_password ? "Saved (leave blank to keep)" : "Blank if default root has no password"}" autocomplete="new-password"></div>
      </div>
      <div class="mini-note">Works with Braiins' own autotuner: lowers watts when chips run hot, raises slowly with headroom, rate-limited by cooldown.</div>
      ${noticeHtml}
      <div class="row-actions" style="margin-top:8px">
        <button class="add-btn" type="submit">Save</button>
        <button class="icon-btn" type="button" onclick="testBraiinsConnection('${esc(d.name)}', true)">Test connection (read-only)</button>
      </div>
    </form>
  </div>`;
}

function renderSettingsPanel(data) {
  const s = data.settings || {};
  const df = s.display_fields || {};
  const defs = s.display_field_defs || [];
  const checksEl = $("settingsFieldChecks");
  if (checksEl && defs.length) {
    checksEl.innerHTML = defs.map(item => {
      const checked = df[item.key] !== false;
      return `<label class="field-check"><input type="checkbox" ${checked ? "checked" : ""} onchange="toggleDisplayField('${esc(item.key)}', this.checked)"><span>${esc(item.label)}</span></label>`;
    }).join("");
  }
  const gt = s.global_tuner || {};
  const active = document.activeElement;
  const formEl = $("globalTunerForm");
  const editingGlobal = !!(active && formEl && formEl.contains(active));
  if (!editingGlobal) {
    for (const [k, val] of Object.entries(gt)) {
      const inp = $("gt_" + k);
      if (inp && val !== undefined && val !== null) inp.value = val;
    }
  }
  const orderEl = $("settingsCardOrder");
  if (orderEl) {
    const devs = data.devices || [];
    if (!devs.length) {
      orderEl.innerHTML = "<div class='sub'>No machines configured yet.</div>";
    } else {
      orderEl.innerHTML = devs.map((d, idx) => `<div class="order-row">
        <span>${idx + 1}. <b>${esc(d.name)}</b> <small class="sub">(${esc(d.type)})</small></span>
        <span style="display:flex;gap:4px">
          <button class="icon-btn" type="button" ${idx === 0 ? "disabled" : ""} onclick="moveDevice('${esc(d.name)}', -1)">&#9650;</button>
          <button class="icon-btn" type="button" ${idx === devs.length - 1 ? "disabled" : ""} onclick="moveDevice('${esc(d.name)}', 1)">&#9660;</button>
        </span>
      </div>`).join("");
    }
  }
}

function render(data) {
  lastStatus = data;
  $("error").hidden = true;
  if (data.version) {
    $("appVersion").textContent = "v" + data.version;
    document.title = "Miner Autotune v" + data.version;
  }
  const autoOn = !!data.autotune_enabled;
  const ab = $("autoBadge");
  if (ab) {
    ab.textContent = autoOn ? "Autotune: ON" : "Autotune: OFF";
    ab.className = "badge " + (autoOn ? "live" : "paused");
  }
  if ($("autoToggle")) {
    $("autoToggle").textContent = autoOn ? "Turn autotune OFF" : "Turn autotune ON";
  }
  const dry = !!data.dry_run;
  const badge = $("modeBadge");
  badge.textContent = dry ? "Dry-run" : "Live";
  badge.className = "badge " + (dry ? "dry" : "live");
  $("dryToggle").textContent = dry ? "Switch to LIVE" : "Switch to dry-run";
  $("updated").textContent = "Updated " + (data.now || "");
  if (!$("scanSubnet").value && data.scan_subnet) {
    $("scanSubnet").placeholder = data.scan_subnet;
  }
  $("fleetSummary").hidden = !isFieldVisible("fleet_summary");
  $("logsSection").hidden = !isFieldVisible("recent_logs");
  renderFleetSummary(data.fleet);
  renderSettingsPanel(data);

  if (!isEditingCards()) {
    const cards = $("cards");
    const emptyMsg = "<p class='sub' style='padding:12px'>No miners configured yet. Click <strong>Scan network</strong> above to discover and add miners on your LAN (saved in <code>userdata/devices.json</code>).</p>";
    cards.innerHTML = (data.devices || []).map(d => renderCard(d, dry)).join("") || emptyMsg;
  }
  $("log").textContent = (data.logs && data.logs.length) ? data.logs.join("\n") : "No log lines yet.";
  $("log").scrollTop = $("log").scrollHeight;
}

function renderScanResults(res) {
  const list = $("scanList");
  if (!res) {
    list.innerHTML = "";
    return;
  }
  const found = res.found || [];
  $("scanStatus").textContent = `Scanned ${res.hosts_scanned || 0} host(s) in ${res.subnet || "?"} — found ${found.length} miner(s).`;
  if (!found.length) {
    list.innerHTML = "<div class='sub'>No supported miners found on that subnet.</div>";
    return;
  }
  list.innerHTML = found.map((item, idx) => {
    const stats = [
      item.model || item.type,
      item.firmware || "",
      item.hashrate_ghs != null ? fmtHashrate(item.hashrate_ghs) : "",
      item.asic_temp_c != null ? `${Number(item.asic_temp_c).toFixed(1)}°C` : "",
      item.detection || ""
    ].filter(Boolean).join(" · ");
    if (item.already_configured) {
      return `<div class="scan-row">
        <div><strong>${esc(item.ip)}</strong> <span class="badge">${esc(item.type)}</span><div class="sub">${esc(stats)}</div></div>
        <span class="badge live">Already configured</span>
      </div>`;
    }
    return `<div class="scan-row">
      <div><strong>${esc(item.ip)}</strong> <span class="badge">${esc(item.type)}</span><div class="sub">${esc(stats)}</div></div>
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <input class="add-name" id="addName_${idx}" type="text" value="${esc(item.suggested_name || item.ip)}">
        <button class="add-btn" type="button" onclick="addFound(${idx})">Add (autotune OFF)</button>
      </div>
    </div>`;
  }).join("");
  window._lastScanFound = found;
}

async function addFound(idx) {
  const item = (window._lastScanFound || [])[idx];
  if (!item) return;
  const nameInput = $("addName_" + idx);
  const chosenName = (nameInput ? nameInput.value : item.suggested_name || "").trim();
  if (!chosenName) { alert("Please enter a device name."); return; }
  try {
    await post("/api/devices/add", {
      name: chosenName,
      ip: item.ip,
      type: item.type,
      family: item.family || "",
      algorithm: item.algorithm || "",
      port: item.port || 80,
      cgminer_port: item.cgminer_port || 4028
    });
    item.already_configured = true;
    renderScanResults({subnet: $("scanSubnet").value || "LAN", hosts_scanned: (window._lastScanHosts || 0), found: window._lastScanFound});
    await refresh();
  } catch (e) {
    alert("Could not add device: " + e.message);
  }
}

function startRename(name) {
  editingDevice = name;
  editingDraft = name;
  if (lastStatus) {
    $("cards").innerHTML = (lastStatus.devices || []).map(d => renderCard(d, !!lastStatus.dry_run)).join("");
    const inp = $("renameInput");
    if (inp) {
      inp.focus();
      inp.select();
      inp.addEventListener("input", () => { editingDraft = inp.value; });
    }
  }
}
function cancelRename() {
  editingDevice = null;
  editingDraft = "";
  if (lastStatus) render(lastStatus);
}
async function submitRename(ev, oldName) {
  ev.preventDefault();
  const inp = $("renameInput");
  const newName = (inp ? inp.value : editingDraft).trim();
  if (!newName) { alert("Name cannot be empty."); return false; }
  try {
    await post("/api/device/" + encodeURIComponent(oldName) + "/rename", {name: newName});
    editingDevice = null;
    editingDraft = "";
    await refresh();
  } catch (e) {
    alert("Could not rename device: " + e.message);
  }
  return false;
}

async function removeDevice(name, isOnline) {
  const msg = isOnline
    ? `Remove "${name}" from Miner Hearth?\n\nThis machine is currently online. Removing it deletes it from config/userdata and clears its saved overrides, state, and history references (the miner itself is not touched).`
    : `Remove "${name}" from Miner Hearth?\n\nThis deletes it from config/userdata and clears its saved overrides, state, and history references.`;
  if (!window.confirm(msg)) return;
  try {
    await post("/api/device/" + encodeURIComponent(name) + "/remove", {});
    if (editingDevice === name) { editingDevice = null; editingDraft = ""; }
    if (openPoolDevice === name) openPoolDevice = null;
    if (openOverrideDevice === name) openOverrideDevice = null;
    if (openBosDevice === name) openBosDevice = null;
    delete cardNotice["manual:" + name];
    delete cardNotice["pool:" + name];
    delete cardNotice["ov:" + name];
    delete cardNotice["bos:" + name];
    delete cardNotice["adp:" + name];
    await refresh();
  } catch (e) {
    alert("Could not remove device: " + e.message);
  }
}

async function submitManual(ev, name) {
  ev.preventDefault();
  const key = domKey(name);
  const fEl = $("manFreq_" + key);
  const vEl = $("manVolt_" + key);
  const payload = {};
  if (fEl && fEl.value !== "") payload.frequency_mhz = Number(fEl.value);
  if (vEl && vEl.value !== "") payload.core_voltage_mv = Number(vEl.value);
  try {
    const res = await post("/api/device/" + encodeURIComponent(name) + "/manual", payload);
    const msg = res.warning
      ? `Applied (${res.frequency_mhz ?? "—"} MHz, ${res.core_voltage_mv ?? "—"} mV). Note: ${res.warning}`
      : `Applied ${res.frequency_mhz ?? "—"} MHz / ${res.core_voltage_mv ?? "—"} mV.`;
    cardNotice["manual:" + name] = {cls: res.warning ? "warn" : "ok", text: msg};
    if (document.activeElement) document.activeElement.blur();
    await refresh();
  } catch (e) {
    cardNotice["manual:" + name] = {cls: "err", text: "Manual apply failed: " + e.message};
    if (document.activeElement) document.activeElement.blur();
    if (lastStatus) render(lastStatus);
  }
  return false;
}

function togglePoolEditor(name) {
  openPoolDevice = (openPoolDevice === name) ? null : name;
  if (lastStatus) {
    $("cards").innerHTML = (lastStatus.devices || []).map(d => renderCard(d, !!lastStatus.dry_run)).join("");
  }
}

async function submitPool(ev, name, isAntminer) {
  ev.preventDefault();
  const key = domKey(name);
  const url = ($("pUrl_" + key)?.value || "").trim();
  const port = Number($("pPort_" + key)?.value || 3333);
  const user = ($("pUser_" + key)?.value || "").trim();
  const pass = $("pPass_" + key)?.value || "";
  const fbUrl = ($("fbUrl_" + key)?.value || "").trim();
  const fbPortRaw = ($("fbPort_" + key)?.value || "").trim();
  const fbUser = ($("fbUser_" + key)?.value || "").trim();
  const fbPass = $("fbPass_" + key)?.value || "";

  if (!isAntminer) {
    const ok = window.confirm(
      `Apply pool changes to "${name}" (${url}:${port}) and restart the miner?\n\n` +
      `ESP-Miner / AxeOS requires a restart for stratum changes to take effect.`
    );
    if (!ok) return false;
  }

  try {
    const res = await post("/api/device/" + encodeURIComponent(name) + "/pool", {
      stratum_url: url,
      stratum_port: port,
      stratum_user: user,
      stratum_password: pass,
      fallback_url: fbUrl,
      fallback_port: fbPortRaw ? Number(fbPortRaw) : null,
      fallback_user: fbUser,
      fallback_password: fbPass,
      restart: !isAntminer
    });
    cardNotice["pool:" + name] = {cls: "ok", text: res.result || "Pool updated."};
    openPoolDevice = null;
    await refresh();
  } catch (e) {
    cardNotice["pool:" + name] = {cls: "err", text: e.message};
    if (lastStatus) {
      $("cards").innerHTML = (lastStatus.devices || []).map(d => renderCard(d, !!lastStatus.dry_run)).join("");
    }
  }
  return false;
}

async function refresh() {
  try {
    const r = await fetch("/api/status");
    if (!r.ok) throw new Error("HTTP " + r.status);
    render(await r.json());
  } catch (e) {
    $("error").hidden = false;
    $("error").textContent = "Cannot reach the dashboard server: " + e + ". If you closed run.bat or the scheduled task is not running, start it again.";
  }
}
async function setDevice(name, start) {
  try {
    await post("/api/device/" + encodeURIComponent(name) + "/" + (start ? "start" : "stop"));
    delete cardNotice["manual:" + name];
    await refresh();
  } catch (e) { alert("Could not change autotune: " + e.message); }
}

if ($("autoToggle")) {
  $("autoToggle").onclick = async () => {
    try {
      const r = await fetch("/api/status");
      const data = await r.json();
      await post("/api/autotune", {enabled: !data.autotune_enabled});
      await refresh();
    } catch (e) { alert("Could not change global autotune: " + e.message); }
  };
}
$("dryToggle").onclick = async () => {
  try {
    const r = await fetch("/api/status");
    const data = await r.json();
    await post("/api/dry-run", {dry_run: !data.dry_run});
    await refresh();
  } catch (e) { alert("Could not change dry-run: " + e.message); }
};
async function moveDevice(name, delta) {
  if (!lastStatus || !Array.isArray(lastStatus.devices)) return;
  const order = lastStatus.devices.map(d => d.name);
  const idx = order.indexOf(name);
  const target = idx + delta;
  if (idx < 0 || target < 0 || target >= order.length) return;
  const tmp = order[idx];
  order[idx] = order[target];
  order[target] = tmp;
  try {
    await post("/api/devices/reorder", {order});
    await refresh();
  } catch (e) {
    alert("Could not reorder cards: " + e.message);
  }
}

function onCardDragStart(ev, name) {
  dragSourceDevice = name;
  if (ev.dataTransfer) {
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", name);
  }
}
function onCardDragEnd() {
  dragSourceDevice = null;
  document.querySelectorAll(".card.drag-over").forEach(el => el.classList.remove("drag-over"));
}
function onCardDragOver(ev) {
  if (!dragSourceDevice) return;
  ev.preventDefault();
  if (ev.currentTarget) ev.currentTarget.classList.add("drag-over");
}
function onCardDragLeave(ev) {
  if (ev.currentTarget) ev.currentTarget.classList.remove("drag-over");
}
async function onCardDrop(ev, targetName) {
  ev.preventDefault();
  if (ev.currentTarget) ev.currentTarget.classList.remove("drag-over");
  const srcName = dragSourceDevice || (ev.dataTransfer ? ev.dataTransfer.getData("text/plain") : "");
  dragSourceDevice = null;
  if (!srcName || srcName === targetName || !lastStatus || !Array.isArray(lastStatus.devices)) return;
  const order = lastStatus.devices.map(d => d.name);
  const fromIdx = order.indexOf(srcName);
  const toIdx = order.indexOf(targetName);
  if (fromIdx < 0 || toIdx < 0) return;
  order.splice(fromIdx, 1);
  order.splice(toIdx, 0, srcName);
  try {
    await post("/api/devices/reorder", {order});
    await refresh();
  } catch (e) {
    alert("Could not reorder cards: " + e.message);
  }
}

async function toggleDisplayField(key, visible) {
  try {
    await post("/api/settings", {display_fields: {[key]: !!visible}});
    await refresh();
  } catch (e) {
    alert("Could not update field visibility: " + e.message);
  }
}

async function setAllFieldsVisible(visible) {
  const defs = (lastStatus && lastStatus.settings && lastStatus.settings.display_field_defs) || [];
  const df = {};
  for (const item of defs) df[item.key] = !!visible;
  try {
    await post("/api/settings", {display_fields: df});
    await refresh();
  } catch (e) {
    alert("Could not update display fields: " + e.message);
  }
}

async function saveGlobalTuner(ev) {
  ev.preventDefault();
  const payload = {};
  const keys = ["max_freq","emergency_step_mhz","min_voltage_mv","max_voltage_mv","emergency_voltage_mv","asic_target_c","asic_max_c","vr_target_c","vr_max_c","error_target_pct","error_low_pct","error_high_pct"];
  for (const k of keys) {
    const inp = $("gt_" + k);
    if (inp && inp.value !== "") payload[k] = Number(inp.value);
  }
  const noticeEl = $("globalTunerNotice");
  try {
    await post("/api/settings", {global_tuner: payload});
    if (noticeEl) { noticeEl.className = "mini-note ok"; noticeEl.textContent = "Saved global caps & targets."; }
    if (document.activeElement) document.activeElement.blur();
    await refresh();
  } catch (e) {
    if (noticeEl) { noticeEl.className = "mini-note err"; noticeEl.textContent = "Save failed: " + e.message; }
  }
  return false;
}

function toggleOverrideEditor(name) {
  openOverrideDevice = (openOverrideDevice === name) ? null : name;
  if (lastStatus) $("cards").innerHTML = (lastStatus.devices || []).map(d => renderCard(d, !!lastStatus.dry_run)).join("");
}

async function submitOverrides(ev, name) {
  ev.preventDefault();
  const key = domKey(name);
  const keys = ["max_freq","emergency_step_mhz","min_voltage_mv","max_voltage_mv","emergency_voltage_mv","asic_target_c","asic_max_c","vr_target_c","vr_max_c","error_low_pct","error_high_pct"];
  const ov = {};
  for (const k of keys) {
    const el = $("ov_" + k + "_" + key);
    if (el) ov[k] = (el.value === "") ? null : Number(el.value);
  }
  const ocEl = $("ov_overclock_opt_in_" + key);
  if (ocEl) ov.overclock_opt_in = !!ocEl.checked;
  const vcEl = $("ov_voltage_control_" + key);
  if (vcEl) ov.voltage_control = !!vcEl.checked;
  try {
    await post("/api/device/" + encodeURIComponent(name) + "/overrides", {overrides: ov});
    cardNotice["ov:" + name] = {cls: "ok", text: "Per-machine overrides saved."};
    openOverrideDevice = null;
    await refresh();
  } catch (e) {
    cardNotice["ov:" + name] = {cls: "err", text: e.message};
    if (lastStatus) $("cards").innerHTML = (lastStatus.devices || []).map(d => renderCard(d, !!lastStatus.dry_run)).join("");
  }
  return false;
}

async function resetOverrides(name) {
  try {
    await post("/api/device/" + encodeURIComponent(name) + "/overrides", {reset: true});
    cardNotice["ov:" + name] = {cls: "ok", text: "Cleared overrides; using global defaults."};
    openOverrideDevice = null;
    await refresh();
  } catch (e) {
    cardNotice["ov:" + name] = {cls: "err", text: e.message};
  }
}

function toggleBosEditor(name) {
  openBosDevice = (openBosDevice === name) ? null : name;
  if (lastStatus) $("cards").innerHTML = (lastStatus.devices || []).map(d => renderCard(d, !!lastStatus.dry_run)).join("");
}

async function submitBraiinsPower(ev, name) {
  ev.preventDefault();
  const key = domKey(name);
  const payload = {
    bos_power_mgmt: !!$("bos_en_" + key)?.checked,
    bos_min_watts: Number($("bos_min_" + key)?.value || 2000),
    bos_max_watts: Number($("bos_max_" + key)?.value || 3500),
    bos_step_watts: Number($("bos_step_" + key)?.value || 50),
    bos_cooldown_sec: Number($("bos_cd_" + key)?.value || 300),
    bos_chip_target_c: Number($("bos_tgt_" + key)?.value || 75),
    bos_chip_hot_c: Number($("bos_hot_" + key)?.value || 82),
    bos_username: ($("bos_user_" + key)?.value || "root").trim(),
    bos_grpc_port: Number($("bos_port_" + key)?.value || 50051),
    bos_password: $("bos_pass_" + key)?.value || ""
  };
  try {
    await post("/api/device/" + encodeURIComponent(name) + "/braiins-power", payload);
    cardNotice["bos:" + name] = {cls: "ok", text: `Saved (auto power-target ${payload.bos_power_mgmt ? "ON" : "OFF"}).`};
    openBosDevice = null;
    await refresh();
  } catch (e) {
    cardNotice["bos:" + name] = {cls: "err", text: e.message};
    if (lastStatus) $("cards").innerHTML = (lastStatus.devices || []).map(d => renderCard(d, !!lastStatus.dry_run)).join("");
  }
  return false;
}

async function testBraiinsConnection(name, fromForm) {
  const key = domKey(name);
  const payload = {};
  if (fromForm) {
    payload.bos_username = ($("bos_user_" + key)?.value || "root").trim();
    payload.bos_grpc_port = Number($("bos_port_" + key)?.value || 50051);
    const pw = $("bos_pass_" + key)?.value || "";
    if (pw) payload.bos_password = pw;
  }
  cardNotice["bos:" + name] = {cls: "warn", text: "Testing Braiins OS+ connection (read-only)..."};
  if (lastStatus) $("cards").innerHTML = (lastStatus.devices || []).map(d => renderCard(d, !!lastStatus.dry_run)).join("");
  try {
    const res = await post("/api/device/" + encodeURIComponent(name) + "/braiins-test", payload);
    const writes = res.writes_supported ? "Power-target writes: SUPPORTED" : "Power-target writes: NOT supported / tuner disabled";
    cardNotice["bos:" + name] = {
      cls: res.connected ? (res.writes_supported ? "ok" : "warn") : "err",
      text: `${res.message || "Check finished"} — ${writes}`
    };
  } catch (e) {
    cardNotice["bos:" + name] = {cls: "err", text: "Test failed: " + e.message};
  }
  if (lastStatus) $("cards").innerHTML = (lastStatus.devices || []).map(d => renderCard(d, !!lastStatus.dry_run)).join("");
}

$("settingsOpenBtn").onclick = () => {
  settingsPanelOpen = !settingsPanelOpen;
  $("settingsPanel").hidden = !settingsPanelOpen;
};
$("closeSettingsBtn").onclick = () => {
  settingsPanelOpen = false;
  $("settingsPanel").hidden = true;
};
$("scanOpenBtn").onclick = () => {
  scanPanelOpen = !scanPanelOpen;
  $("scanPanel").hidden = !scanPanelOpen;
};
$("closeScanBtn").onclick = () => {
  scanPanelOpen = false;
  $("scanPanel").hidden = true;
};
$("runScanBtn").onclick = async () => {
  const btn = $("runScanBtn");
  btn.disabled = true;
  $("scanStatus").textContent = "Scanning local network… this usually takes 2–5 seconds.";
  try {
    const subnet = $("scanSubnet").value.trim();
    const res = await post("/api/scan", subnet ? {subnet} : {});
    window._lastScanHosts = res.hosts_scanned || 0;
    renderScanResults(res);
  } catch (e) {
    $("scanStatus").textContent = "Scan failed: " + e.message;
  } finally {
    btn.disabled = false;
  }
};
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""
DASHBOARD_HTML = nk_ui1171.inject1171(nk_ui117.inject117(nk_ui1162.inject1162(nk_ui116.inject116(nk_ui1155.inject1155(nk_ui115.inject115(nk_ui114.inject114(nk_ui113.inject113(nk_ui110.inject110(nk_ui19.inject19(nk_ui.inject(DASHBOARD_HTML))))))))))).replace("__APP_VERSION__", APP_VERSION)


class MemoryLogHandler(logging.Handler):
    """Keeps a short tail of log lines for the dashboard."""

    def __init__(self, lines: deque, lock: threading.RLock, level: int = logging.INFO):
        super().__init__(level)
        self.lines = lines
        self._lock = lock
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        with self._lock:
            self.lines.append(msg)


def _compute_fleet_summary(devices: list[dict[str, Any]]) -> dict[str, Any]:
    online_count = 0
    total_hr = 0.0
    total_w = 0.0
    hottest_chip: Optional[float] = None
    hottest_chip_dev = ""
    hottest_vr: Optional[float] = None
    hottest_vr_dev = ""
    best_share: Optional[float] = None
    best_share_disp = ""
    best_share_dev = ""

    for d in devices:
        name = str(d.get("name") or "")
        if bool(d.get("online")):
            online_count += 1
            hr = d.get("hashrate_ghs")
            if isinstance(hr, (int, float)) and hr > 0:
                total_hr += float(hr)
            pw = d.get("power_w")
            if isinstance(pw, (int, float)) and pw > 0:
                total_w += float(pw)
            ct = d.get("hottest_asic_temp_c")
            if ct is None:
                ct = d.get("asic_temp_c")
            if isinstance(ct, (int, float)) and ct > 0:
                if hottest_chip is None or float(ct) > hottest_chip:
                    hottest_chip = float(ct)
                    hottest_chip_dev = name
            vt = d.get("vr_temp_c")
            if isinstance(vt, (int, float)) and vt > 0:
                if hottest_vr is None or float(vt) > hottest_vr:
                    hottest_vr = float(vt)
                    hottest_vr_dev = name
        bs = d.get("best_diff")
        if isinstance(bs, (int, float)) and bs > 0:
            if best_share is None or float(bs) > best_share:
                best_share = float(bs)
                best_share_disp = str(d.get("best_diff_display") or "")
                best_share_dev = name

    return {
        "online_count": online_count,
        "total_count": len(devices),
        "total_hashrate_ghs": round(total_hr, 2) if total_hr > 0 else 0.0,
        "total_power_w": round(total_w, 2) if total_w > 0 else 0.0,
        "fleet_j_per_th": compute_j_per_th(total_w, total_hr),
        "hottest_chip_c": hottest_chip,
        "hottest_chip_device": hottest_chip_dev,
        "hottest_vr_c": hottest_vr,
        "hottest_vr_device": hottest_vr_dev,
        "best_share": best_share,
        "best_share_display": best_share_disp,
        "best_share_device": best_share_dev,
    }


class SharedState:
    """Thread-safe live view of the tuner, plus persisted start/stop flags."""

    def __init__(
        self,
        device_names: list[str],
        state_path: Path,
        dry_run: bool = False,
        device_defaults: Optional[dict[str, bool]] = None,
        scan_subnet: str = "",
        autotune_enabled: Optional[bool] = None,
    ):
        self.lock = threading.RLock()
        self.state_path = Path(state_path)
        self.dry_run = bool(dry_run)
        defaults = dict(device_defaults or {})
        self.enabled = {name: bool(defaults.get(name, False)) for name in device_names}
        self._autotune_explicit = autotune_enabled is not None
        self.autotune_enabled = bool(autotune_enabled) if autotune_enabled is not None else any(self.enabled.values())
        self.device_order: list[str] = list(device_names)
        self.display_fields: dict[str, bool] = default_display_fields()
        self.global_tuner: dict[str, Any] = default_global_tuner()
        self.snapshots: dict[str, dict[str, Any]] = {name: {} for name in device_names}
        self.logs: deque = deque(maxlen=200)
        self.started_at = iso_now()
        self.scan_subnet = scan_subnet
        self.last_scan: Optional[dict[str, Any]] = None
        self.tuner: Any = None
        self.hub: Any = None
        self._load()
        if dry_run:
            # CLI --dry-run always starts this session in dry-run.
            self.dry_run = True

    def attach_tuner(self, tuner: Any) -> None:
        with self.lock:
            self.tuner = tuner
            if self.hub is not None and getattr(tuner, "hub", None) is None:
                tuner.hub = self.hub

    def _load(self) -> None:
        path = self.state_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("Could not read state file %s: %s", path, exc)
            return
        if not isinstance(data, dict):
            return
        if "dry_run" in data and not self.dry_run:
            self.dry_run = bool(data.get("dry_run"))
        devices = data.get("devices") or {}
        if isinstance(devices, dict):
            for name in self.enabled:
                entry = devices.get(name)
                if isinstance(entry, dict) and "enabled" in entry:
                    self.enabled[name] = bool(entry["enabled"])
                elif isinstance(entry, bool):
                    self.enabled[name] = entry
        if "autotune_enabled" in data:
            self.autotune_enabled = bool(data.get("autotune_enabled"))
            self._autotune_explicit = True
        elif not self._autotune_explicit:
            self.autotune_enabled = any(self.enabled.values())

    def save(self) -> None:
        payload = {
            "autotune_enabled": bool(self.autotune_enabled),
            "dry_run": self.dry_run,
            "devices": {name: {"enabled": bool(self.enabled[name])} for name in self.enabled},
        }
        path = self.state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            LOG.warning("Could not write state file %s: %s", path, exc)

    def is_autotune_enabled(self) -> bool:
        with self.lock:
            return bool(self.autotune_enabled)

    def set_autotune_enabled(self, enabled: bool) -> None:
        with self.lock:
            self.autotune_enabled = bool(enabled)
            self._autotune_explicit = True
            self.save()
            tuner = self.tuner
        if tuner is not None:
            try:
                if hasattr(tuner, "config"):
                    tuner.config.autotune_enabled = bool(enabled)
                tuner._persist_userdata()
                if enabled and hasattr(tuner, "wake"):
                    tuner.wake()
            except Exception:
                pass

    def is_enabled(self, name: str) -> bool:
        with self.lock:
            snap = self.snapshots.get(name) or {}
            if snap.get("tunable") is False:
                return False
            return bool(self.enabled.get(name, False))

    def is_dry_run(self) -> bool:
        with self.lock:
            return bool(self.dry_run)

    def set_enabled(self, name: str, enabled: bool) -> tuple[bool, str]:
        with self.lock:
            if name not in self.enabled:
                return False, f"unknown device: {name}"
            snap = self.snapshots.get(name) or {}
            if enabled and snap.get("tunable") is False:
                return False, f"{name} is monitor-only; autotune cannot be enabled"
            self.enabled[name] = bool(enabled)
            if enabled and not self.autotune_enabled:
                self.autotune_enabled = True
                self._autotune_explicit = True
            self.save()
            return True, ""

    def set_dry_run(self, dry_run: bool) -> None:
        with self.lock:
            self.dry_run = bool(dry_run)
            self.save()
            tuner = self.tuner
        if tuner is not None:
            try:
                tuner._persist_userdata()
            except Exception:
                pass

    def rename_device(self, old_name: str, new_name: str) -> bool:
        with self.lock:
            if old_name not in self.enabled or new_name in self.enabled:
                return False
            new_enabled: dict[str, bool] = {}
            for k, v in self.enabled.items():
                new_enabled[new_name if k == old_name else k] = v
            self.enabled = new_enabled

            new_snaps: dict[str, dict[str, Any]] = {}
            for k, v in self.snapshots.items():
                if k == old_name:
                    snap = dict(v)
                    snap["name"] = new_name
                    new_snaps[new_name] = snap
                else:
                    new_snaps[k] = v
            self.snapshots = new_snaps
            self.device_order = [new_name if n == old_name else n for n in self.device_order]
            self.save()
            return True

    def add_device(self, name: str, enabled: bool = False, initial: Optional[dict[str, Any]] = None) -> bool:
        with self.lock:
            if name in self.enabled:
                return False
            self.enabled[name] = bool(enabled)
            if name not in self.device_order:
                self.device_order.append(name)
            snap = dict(initial or {"name": name})
            snap.setdefault("name", name)
            self.snapshots[name] = snap
            self.save()
            return True

    def remove_device(self, name: str) -> bool:
        with self.lock:
            existed = (name in self.enabled) or (name in self.snapshots) or (name in self.device_order)
            self.enabled.pop(name, None)
            self.snapshots.pop(name, None)
            self.device_order = [n for n in self.device_order if n != name]
            if existed:
                self.save()
            return existed

    def set_device_order(self, order: list[str]) -> list[str]:
        with self.lock:
            known = list(self.snapshots.keys())
            new_order: list[str] = []
            used: set[str] = set()
            for raw_n in order:
                nm = str(raw_n or "").strip()
                if nm in self.snapshots and nm not in used:
                    new_order.append(nm)
                    used.add(nm)
            for nm in self.device_order + known:
                if nm in self.snapshots and nm not in used:
                    new_order.append(nm)
                    used.add(nm)
            self.device_order = new_order
            return list(self.device_order)

    def set_settings(
        self,
        display_fields: Optional[dict[str, bool]] = None,
        global_tuner: Optional[dict[str, Any]] = None,
        device_order: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        with self.lock:
            if display_fields is not None:
                self.display_fields = normalize_display_fields(display_fields)
            if global_tuner is not None:
                self.global_tuner = normalize_global_tuner(global_tuner)
            if device_order is not None:
                self.set_device_order(device_order)
            return self.get_settings()

    def get_settings(self) -> dict[str, Any]:
        with self.lock:
            return {
                "display_fields": dict(self.display_fields),
                "display_field_defs": list(DISPLAY_FIELD_DEFS),
                "global_tuner": dict(self.global_tuner),
                "device_order": list(self.device_order),
            }

    def update_device(self, name: str, fields: dict[str, Any]) -> None:
        with self.lock:
            snap = self.snapshots.setdefault(name, {})
            snap.update(fields)
            snap["updated_at"] = iso_now()

    def configured_ips(self) -> set[str]:
        with self.lock:
            ips: set[str] = set()
            for snap in self.snapshots.values():
                ip = str(snap.get("ip") or "").strip()
                if ip:
                    ips.add(ip)
            return ips

    def device_names(self) -> set[str]:
        with self.lock:
            return set(self.enabled.keys())

    def status(self) -> dict[str, Any]:
        with self.lock:
            ordered_names: list[str] = []
            seen: set[str] = set()
            for nm in self.device_order:
                if nm in self.snapshots and nm not in seen:
                    ordered_names.append(nm)
                    seen.add(nm)
            for nm in self.snapshots:
                if nm not in seen:
                    ordered_names.append(nm)
                    seen.add(nm)
            devices = []
            for name in ordered_names:
                snap = self.snapshots[name]
                row = dict(snap)
                row.setdefault("name", name)
                if row.get("tunable") is False:
                    row["enabled"] = False
                else:
                    row["enabled"] = bool(self.enabled.get(name, False))
                devices.append(row)
            return {
                "ok": True,
                "version": APP_VERSION,
                "now": iso_now(),
                "started_at": self.started_at,
                "autotune_enabled": bool(self.autotune_enabled),
                "dry_run": bool(self.dry_run),
                "scan_subnet": self.scan_subnet,
                "settings": {
                    "display_fields": dict(self.display_fields),
                    "display_field_defs": list(DISPLAY_FIELD_DEFS),
                    "global_tuner": dict(self.global_tuner),
                    "device_order": list(ordered_names),
                },
                "fleet": _compute_fleet_summary(devices),
                "devices": devices,
                "logs": list(self.logs),
                **({"nk": self.hub.status_payload(devices)} if self.hub is not None else {}),
            }

    def replace_devices(
        self,
        names: list[str],
        enabled: dict[str, bool],
        initials: Optional[dict[str, dict[str, Any]]] = None,
    ) -> None:
        """Swap the whole device set (used by settings restore)."""
        initials = initials or {}
        with self.lock:
            self.enabled = {n: bool(enabled.get(n, False)) for n in names}
            self.snapshots = {n: dict(initials.get(n) or {}) for n in names}
            self.device_order = list(names)
            self.save()

    def log_handler(self) -> MemoryLogHandler:
        return MemoryLogHandler(self.logs, self.lock)


_BG_CSS: list = []


def _background_css() -> Optional[bytes]:
    """v1.17.1: the dashboard background stylesheet built from assets/dashboard-bg.jpg (cached)."""
    if not _BG_CSS:
        import autotune as _at
        _BG_CSS.append(nk_ui1171.bundled_background_css(_at.BUNDLE_DIR, _at.SCRIPT_DIR, Path(__file__).resolve().parent))
    return _BG_CSS[0]


def _json_bytes(data: Any, status: int = 200) -> tuple[int, bytes, str]:
    body = json.dumps(data, default=str).encode("utf-8")
    return status, body, "application/json"


def handle_dashboard_request(
    method: str,
    path: str,
    body: bytes,
    state: SharedState,
    client_ip: Optional[str] = None,
    headers: Any = None,
) -> tuple[int, bytes, str]:
    """Pure request handler used by the HTTP server and by tests.

    client_ip/headers come from the HTTP server; None means an in-process call
    (tests), which is treated like this PC (loopback).
    """
    parsed = urlparse(path)
    route = parsed.path
    method = method.upper()
    hub = getattr(state, "hub", None)
    remote = not is_loopback(client_ip)

    if hub is not None:
        denied = hub.guard.check(method, route, client_ip, headers, hub.settings.section("lan"))
        if denied is not None:
            return denied

    if method in {"GET", "HEAD"} and route in {"/", "/index.html"}:
        return 200, DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8"

    if method in {"GET", "HEAD"} and route == "/mh/background.css":
        css = _background_css()
        if css is None:
            return 404, b"/* no bundled background; a live update may provide ui/mh-background.css */", "text/css; charset=utf-8"
        return 200, css, "text/css; charset=utf-8"

    if method in {"GET", "HEAD"} and route == "/api/status":
        data = state.status()
        if hub is not None:
            if not remote:
                hub.last_client_seen = time.time()
            else:
                data["nk"] = hub.status_payload(data.get("devices") or [], remote=True)
            lan = hub.settings.section("lan")
            data["access"] = {"remote": remote, "read_only": bool(remote and lan.get("read_only", True))}
        else:
            data["access"] = {"remote": remote, "read_only": False}
        return _json_bytes(data)

    if hub is not None:
        handled = hub.handle(method, route, parsed.query, body, remote)
        if handled is not None:
            return handled

    if method in {"GET", "HEAD"} and route == "/api/settings":
        return _json_bytes({"ok": True, **state.get_settings()})

    if method == "POST" and route == "/api/settings":
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return _json_bytes({"ok": False, "error": "invalid JSON"}, 400)
        if not isinstance(payload, dict):
            return _json_bytes({"ok": False, "error": "expected JSON object"}, 400)
        tuner = state.tuner
        if tuner is not None:
            ok, err, out = tuner.update_settings(payload)
            if not ok:
                return _json_bytes({"ok": False, "error": err}, 400)
            return _json_bytes({"ok": True, **out})
        out = state.set_settings(
            display_fields=payload.get("display_fields"),
            global_tuner=payload.get("global_tuner"),
            device_order=payload.get("device_order"),
        )
        return _json_bytes({"ok": True, **out})

    if method == "POST" and route == "/api/devices/reorder":
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return _json_bytes({"ok": False, "error": "invalid JSON"}, 400)
        order = payload.get("order") if isinstance(payload, dict) else None
        if not isinstance(order, list):
            return _json_bytes({"ok": False, "error": "order must be a list"}, 400)
        tuner = state.tuner
        if tuner is not None:
            ok, err, new_order = tuner.reorder_devices(order)
            if not ok:
                return _json_bytes({"ok": False, "error": err}, 400)
            return _json_bytes({"ok": True, "order": new_order})
        new_order = state.set_device_order(order)
        return _json_bytes({"ok": True, "order": new_order})

    if method == "POST" and route == "/api/autotune":
        flag = True
        if body:
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return _json_bytes({"ok": False, "error": "invalid JSON"}, 400)
            if isinstance(payload, dict) and "enabled" in payload:
                flag = bool(payload["enabled"])
        tuner = state.tuner
        if tuner is not None and hasattr(tuner, "set_autotune_enabled"):
            tuner.set_autotune_enabled(flag)
        else:
            state.set_autotune_enabled(flag)
        LOG.info("Dashboard: global autotune %s", "ON" if state.is_autotune_enabled() else "OFF")
        return _json_bytes({"ok": True, "autotune_enabled": state.is_autotune_enabled()})

    if method == "POST" and route == "/api/dry-run":
        flag = True
        if body:
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return _json_bytes({"ok": False, "error": "invalid JSON"}, 400)
            if isinstance(payload, dict) and "dry_run" in payload:
                flag = bool(payload["dry_run"])
        state.set_dry_run(flag)
        LOG.info("Dashboard: dry-run %s", "ON" if flag else "OFF")
        return _json_bytes({"ok": True, "dry_run": state.is_dry_run()})

    if method == "POST" and route == "/api/scan":
        subnet = state.scan_subnet
        if body:
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return _json_bytes({"ok": False, "error": "invalid JSON"}, 400)
            if isinstance(payload, dict) and payload.get("subnet"):
                subnet = str(payload["subnet"]).strip()
        try:
            from scanner import scan_network
            result = scan_network(
                subnet=subnet,
                configured_ips=state.configured_ips(),
                existing_names=state.device_names(),
            )
        except ValueError as exc:
            return _json_bytes({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            return _json_bytes({"ok": False, "error": f"scan failed: {exc}"}, 500)
        with state.lock:
            state.last_scan = result
        LOG.info(
            "Dashboard: network scan (%s, %d hosts) found %d miner(s)",
            result.get("subnet"),
            result.get("hosts_scanned", 0),
            len(result.get("found") or []),
        )
        return _json_bytes({"ok": True, **result})

    if method == "POST" and route == "/api/devices/add":
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return _json_bytes({"ok": False, "error": "invalid JSON"}, 400)
        if not isinstance(payload, dict):
            return _json_bytes({"ok": False, "error": "expected JSON object"}, 400)
        # Newly added miners ALWAYS start with autotune OFF.
        spec = dict(payload)
        spec["enabled"] = False
        tuner = state.tuner
        if tuner is not None:
            ok, err, entry = tuner.add_device(spec)
            if not ok:
                code = 409 if "already" in err else 400
                return _json_bytes({"ok": False, "error": err}, code)
            name = str(entry["name"]) if entry else str(spec.get("name") or "")
            # Poll once asynchronously so telemetry renders immediately without blocking HTTP.
            if name and name in tuner.states:
                st_obj = tuner.states[name]
                threading.Thread(target=tuner.poll_device, args=(st_obj,), daemon=True).start()
            return _json_bytes({"ok": True, "device": entry, "enabled": False}, 201)
        name = str(spec.get("name") or "").strip()
        ip = str(spec.get("ip") or "").strip()
        dtype = str(spec.get("type") or "").strip().lower()
        if not name or not ip or not dtype:
            return _json_bytes({"ok": False, "error": "name, ip, and type are required"}, 400)
        if not state.add_device(name, enabled=False, initial={"name": name, "ip": ip, "type": dtype, "tunable": dtype != "antminer"}):
            return _json_bytes({"ok": False, "error": f"device '{name}' already exists"}, 409)
        return _json_bytes({"ok": True, "device": {"name": name, "ip": ip, "type": dtype, "enabled": False}, "enabled": False}, 201)

    if method == "POST" and route.startswith("/api/device/"):
        parts = [p for p in route.split("/") if p]
        # api, device, <name>, start|stop|rename|manual|pool
        if len(parts) != 4 or parts[0] != "api" or parts[1] != "device":
            return _json_bytes({"ok": False, "error": "not found"}, 404)
        name = unquote(parts[2])
        action = parts[3]
        if action in {"start", "stop"}:
            enabled = action == "start"
            tuner = state.tuner
            if tuner is not None:
                ok, err = tuner.set_device_enabled(name, enabled)
            else:
                ok, err = state.set_enabled(name, enabled)
            if not ok:
                code = 404 if err.startswith("unknown") else 400
                return _json_bytes({"ok": False, "error": err}, code)
            LOG.info("Dashboard: autotune %s for %s", "STARTED" if enabled else "STOPPED", name)
            return _json_bytes({"ok": True, "name": name, "enabled": enabled})
        if action == "rename":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return _json_bytes({"ok": False, "error": "invalid JSON"}, 400)
            new_name = str(payload.get("name") or "").strip() if isinstance(payload, dict) else ""
            if not new_name:
                return _json_bytes({"ok": False, "error": "new name cannot be empty"}, 400)
            tuner = state.tuner
            if tuner is not None:
                ok, err = tuner.rename_device(name, new_name)
                if not ok:
                    code = 404 if err.startswith("Unknown") else (409 if "already exists" in err else 400)
                    return _json_bytes({"ok": False, "error": err}, code)
            else:
                if name not in state.enabled:
                    return _json_bytes({"ok": False, "error": f"unknown device: {name}"}, 404)
                if new_name != name and new_name in state.enabled:
                    return _json_bytes({"ok": False, "error": f"A device named '{new_name}' already exists"}, 409)
                if new_name != name:
                    state.rename_device(name, new_name)
            return _json_bytes({"ok": True, "old_name": name, "name": new_name})
        if action in {"remove", "delete"}:
            tuner = state.tuner
            if tuner is not None:
                ok, err = tuner.remove_device(name)
                if not ok:
                    code = 404 if err.startswith("Unknown") else 400
                    return _json_bytes({"ok": False, "error": err}, code)
            else:
                if not state.remove_device(name):
                    return _json_bytes({"ok": False, "error": f"unknown device: {name}"}, 404)
            return _json_bytes({"ok": True, "name": name, "removed": True})
        if action == "manual":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return _json_bytes({"ok": False, "error": "invalid JSON"}, 400)
            if not isinstance(payload, dict):
                return _json_bytes({"ok": False, "error": "expected JSON object"}, 400)
            tuner = state.tuner
            if tuner is None:
                return _json_bytes({"ok": False, "error": "tuner not attached"}, 503)
            ok, msg, info = tuner.apply_manual_settings(
                name,
                frequency_mhz=payload.get("frequency_mhz"),
                core_voltage_mv=payload.get("core_voltage_mv"),
            )
            if not ok:
                code = 404 if msg.startswith("unknown") else 400
                return _json_bytes({"ok": False, "error": msg}, code)
            return _json_bytes({"ok": True, "name": name, **info})
        if action == "pool":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return _json_bytes({"ok": False, "error": "invalid JSON"}, 400)
            if not isinstance(payload, dict):
                return _json_bytes({"ok": False, "error": "expected JSON object"}, 400)
            tuner = state.tuner
            if tuner is None:
                return _json_bytes({"ok": False, "error": "tuner not attached"}, 503)
            ok, err, info = tuner.apply_pool_change(name, payload)
            if not ok:
                code = 404 if err.startswith("unknown") else 400
                return _json_bytes({"ok": False, "error": err}, code)
            return _json_bytes({"ok": True, "name": name, **info})
        if action == "overrides":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return _json_bytes({"ok": False, "error": "invalid JSON"}, 400)
            if not isinstance(payload, dict):
                return _json_bytes({"ok": False, "error": "expected JSON object"}, 400)
            tuner = state.tuner
            if tuner is None:
                return _json_bytes({"ok": False, "error": "tuner not attached"}, 503)
            ok, err, info = tuner.update_device_overrides(name, payload)
            if not ok:
                code = 404 if err.startswith("unknown") else 400
                return _json_bytes({"ok": False, "error": err}, code)
            return _json_bytes({"ok": True, "name": name, **info})
        if action == "braiins-power":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return _json_bytes({"ok": False, "error": "invalid JSON"}, 400)
            if not isinstance(payload, dict):
                return _json_bytes({"ok": False, "error": "expected JSON object"}, 400)
            tuner = state.tuner
            if tuner is None:
                return _json_bytes({"ok": False, "error": "tuner not attached"}, 503)
            ok, err, info = tuner.update_braiins_power(name, payload)
            if not ok:
                code = 404 if err.startswith("unknown") else 400
                return _json_bytes({"ok": False, "error": err}, code)
            return _json_bytes({"ok": True, "name": name, **info})
        if action == "adapter":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return _json_bytes({"ok": False, "error": "invalid JSON"}, 400)
            if not isinstance(payload, dict):
                return _json_bytes({"ok": False, "error": "expected JSON object"}, 400)
            tuner = state.tuner
            if tuner is None:
                return _json_bytes({"ok": False, "error": "tuner not attached"}, 503)
            ok, err, info = tuner.update_device_adapter(name, payload)
            if not ok:
                code = 404 if err.startswith("unknown") else 400
                return _json_bytes({"ok": False, "error": err}, code)
            return _json_bytes({"ok": True, "name": name, **info})
        if action == "adapter-action":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return _json_bytes({"ok": False, "error": "invalid JSON"}, 400)
            if not isinstance(payload, dict):
                return _json_bytes({"ok": False, "error": "expected JSON object"}, 400)
            tuner = state.tuner
            if tuner is None:
                return _json_bytes({"ok": False, "error": "tuner not attached"}, 503)
            act = str(payload.get("action") or "").strip()
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            ok, msg = tuner.apply_adapter_action(name, act, params)
            info = {}
            if not ok:
                code = 404 if msg.startswith("unknown") else (403 if ("monitor-only" in msg or "opt-in" in msg or "disabled" in msg) else 400)
                return _json_bytes({"ok": False, "error": msg}, code)
            return _json_bytes({"ok": True, "name": name, "message": msg, **info})
        if action == "braiins-test":
            payload: dict[str, Any] = {}
            if body:
                try:
                    parsed_body = json.loads(body.decode("utf-8") or "{}")
                    if isinstance(parsed_body, dict):
                        payload = parsed_body
                except json.JSONDecodeError:
                    return _json_bytes({"ok": False, "error": "invalid JSON"}, 400)
            tuner = state.tuner
            if tuner is None:
                return _json_bytes({"ok": False, "error": "tuner not attached"}, 503)
            ok, err, info = tuner.test_braiins_connection(name, payload)
            if not ok:
                code = 404 if err.startswith("unknown") else 400
                return _json_bytes({"ok": False, "error": err}, code)
            connected = bool(info.get("ok"))
            return _json_bytes({"ok": True, "connected": connected, "name": name, **{k: v for k, v in info.items() if k != "ok"}})
        return _json_bytes({"ok": False, "error": "unknown action"}, 404)

    if method == "DELETE" and route.startswith("/api/device/"):
        parts = [p for p in route.split("/") if p]
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "device":
            name = unquote(parts[2])
            tuner = state.tuner
            if tuner is not None:
                ok, err = tuner.remove_device(name)
                if not ok:
                    return _json_bytes({"ok": False, "error": err}, 404 if err.startswith("Unknown") else 400)
            else:
                if not state.remove_device(name):
                    return _json_bytes({"ok": False, "error": f"unknown device: {name}"}, 404)
            return _json_bytes({"ok": True, "name": name, "removed": True})

    if method == "OPTIONS":
        return 204, b"", "text/plain"

    return _json_bytes({"ok": False, "error": "not found"}, 404)


def make_handler(state: SharedState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _read_body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return b""
            if length > MAX_BODY_BYTES:
                self.rfile.read(min(length, MAX_BODY_BYTES))
                return b""
            return self.rfile.read(length)

        def _client(self) -> str:
            try:
                return str(self.client_address[0])
            except (AttributeError, IndexError, TypeError):
                return ""

        def _respond(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:
            status, body, ctype = handle_dashboard_request(self.command, self.path, b"", state, self._client(), self.headers)
            self._respond(status, body, ctype)

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_POST(self) -> None:
            status, body, ctype = handle_dashboard_request(
                self.command, self.path, self._read_body(), state, self._client(), self.headers
            )
            self._respond(status, body, ctype)

        def do_DELETE(self) -> None:
            status, body, ctype = handle_dashboard_request(
                self.command, self.path, self._read_body(), state, self._client(), self.headers
            )
            self._respond(status, body, ctype)

        def do_OPTIONS(self) -> None:
            status, body, ctype = handle_dashboard_request(self.command, self.path, b"", state, self._client(), self.headers)
            self._respond(status, body, ctype)

    return Handler


class DashboardServer:
    def __init__(self, state: SharedState, host: str = "127.0.0.1", port: int = 8080):
        self.state = state
        self.host = host
        self.port = int(port)
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        display = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
        actual = self.port
        if self.server:
            actual = int(self.server.server_address[1])
        return f"http://{display}:{actual}"

    def start(self) -> "DashboardServer":
        self.server = ThreadingHTTPServer((self.host, self.port), make_handler(self.state))
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, name="dashboard", daemon=True)
        self.thread.start()
        LOG.info("Dashboard listening on %s (bound to %s)", self.url, self.host)
        return self

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=2)
