"""Miner Hearth v1.12 dashboard UI: fault badge + explainer, Clear/Reset, log capture,
redacted diagnostics, fault history, and the per-card Details panel (all component readings)."""

from __future__ import annotations

import re

NK110_CSS = r"""
  /* ---- v1.10 faults ---- */
  .nk-faultbox { display: block !important; margin: 6px 0 2px; }
  .nk-fault { border: 1px solid var(--border); border-left: 4px solid var(--warn); border-radius: 8px; background: var(--bg-soft); padding: 6px 10px; margin: 4px 0; font-size: .84rem; }
  .nk-fault.problem { border-left-color: var(--danger); }
  .nk-fault.info { border-left-color: var(--info); }
  .nk-fault summary { cursor: pointer; font-weight: 600; list-style: revert; }
  .nk-fault .nk-ftext { color: var(--muted); font-weight: 400; margin-left: 4px; }
  .nk-fault ul { margin: 4px 0 4px 18px; padding: 0; }
  .nk-fault .nk-um { font-size: .7rem; border: 1px solid var(--muted); color: var(--muted); border-radius: 6px; padding: 0 5px; margin-left: 6px; }
  .nk-fault-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; align-items: center; }
  .nk-fault-ok { font-size: .78rem; color: var(--ok); }
  .nk-badge.fault { border-color: var(--danger); color: var(--danger); }
  .nk-badge.faultw { border-color: var(--warn); color: var(--warn); }
  /* ---- v1.10 details modal ---- */
  .nk110-modal { position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 9000; display: flex; align-items: flex-start; justify-content: center; padding: 3vh 2vw; }
  .nk110-modal[hidden] { display: none; }
  .nk110-dlg { background: var(--bg-card); color: var(--text); border: 1px solid var(--border); border-radius: 12px; box-shadow: var(--shadow); width: min(1100px, 96vw); max-height: 94vh; display: flex; flex-direction: column; }
  .nk110-head { display: flex; align-items: center; gap: 10px; padding: 10px 14px; border-bottom: 1px solid var(--border); }
  .nk110-head h2 { margin: 0; font-size: 1.05rem; flex: 1; }
  .nk110-tabs { display: flex; gap: 4px; padding: 6px 14px 0; flex-wrap: wrap; }
  .nk110-tabs button { border: 1px solid var(--border); background: var(--bg-soft); color: var(--text); border-radius: 8px 8px 0 0; padding: 5px 12px; cursor: pointer; font: inherit; font-size: .85rem; }
  .nk110-tabs button.on { background: var(--accent); color: #000; border-color: var(--accent); }
  .nk110-body { overflow: auto; padding: 10px 14px 14px; }
  .nk110-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 10px; }
  .nk110-box { border: 1px solid var(--border); border-radius: 10px; padding: 8px 10px; background: var(--bg-soft); }
  .nk110-box h4 { margin: 0 0 6px; font-size: .9rem; }
  .nk110-kv { display: grid; grid-template-columns: minmax(120px, 1fr) auto; gap: 2px 10px; font-size: .82rem; font-variant-numeric: tabular-nums; }
  .nk110-kv span:nth-child(odd) { color: var(--muted); overflow-wrap: anywhere; }
  .nk110-kv span:nth-child(even) { text-align: right; overflow-wrap: anywhere; }
  .nk110-raw td:first-child { font-family: ui-monospace, Consolas, monospace; font-size: .78rem; }
  .nk110-filter { width: 100%; margin: 0 0 8px; }
  .nk110-hist .ev-appeared { color: var(--danger); } .nk110-hist .ev-cleared { color: var(--ok); } .nk110-hist .ev-user_reset, .nk110-hist .ev-auto_restart { color: var(--info); }
"""

NK110_PANEL_HTML = r"""
  <section class="nk-panel" id="nkFaultPanel" hidden>
    <div class="nk-panel-head"><h2>Fault history</h2>
      <select id="nkFaultDev" onchange="nk110RenderHistory()"><option value="">All machines</option></select>
      <span class="nk-tiny">Faults the miners reported (appeared / cleared), plus Clear/Reset actions and automatic restarts.</span></div>
    <div class="nk-table-wrap"><table class="nk-table nk110-hist" id="nkFaultTable"></table></div>
  </section>
"""

NK110_MODAL_HTML = r"""
<div class="nk110-modal" id="nk110Modal" hidden onclick="if (event.target === this) nk110Close()">
  <div class="nk110-dlg" role="dialog" aria-modal="true" aria-labelledby="nk110Title">
    <div class="nk110-head"><h2 id="nk110Title">Details</h2><span class="nk-tiny" id="nk110Upd"></span><button class="icon-btn" type="button" onclick="nk110Close()">Close</button></div>
    <div class="nk110-tabs" id="nk110Tabs"></div>
    <div class="nk110-body" id="nk110Body"></div>
  </div>
</div>
"""

NK110_SETTINGS_HTML = r"""
      <div class="settings-box nk-set" id="nkSetFaults">
        <h3>Faults &amp; Clear/Reset</h3>
        <label class="nk-row"><input type="checkbox" id="nk_f_en"> Show miner fault flags and explanations</label>
        <label class="nk-row"><input type="checkbox" id="nk_f_probe"> Read NerdQAxe+ fault flags and the Braiins error list every 2 min (Braiins needs its saved login)</label>
        <label class="nk-row"><input type="checkbox" id="nk_f_alert"> In-app alert (sound / toast) when a fault appears</label>
        <div class="nk-row"><label>Clear/Reset only when chip below</label><input type="number" id="nk_f_cool_a" step="1" style="width:70px"><span class="nk110-unit"></span>
          <label>and VR below</label><input type="number" id="nk_f_cool_v" step="1" style="width:70px"><span class="nk110-unit"></span></div>
        <div class="nk-row"><label>At most one reset every</label><input type="number" id="nk_f_gap" min="5" step="1" style="width:70px"> min,
          <label>and</label><input type="number" id="nk_f_max" min="1" max="12" step="1" style="width:60px"><label>per day per machine</label></div>
        <div class="nk-row"><label>Post-emergency autotune lockout</label><input type="number" id="nk_f_lockout" min="0" max="1440" step="15" style="width:80px"><label>min (default 180 min = 3h; 0 disables)</label></div>
        <div class="nk-row"><label>Pre-fault high-res capture window</label><input type="number" id="nk_f_rec_hours" min="1" max="72" step="1" style="width:70px"><label>hours before any fault (saved to userdata/fault_records/, kept until deleted)</label></div>
        <div class="row-actions"><button class="add-btn" type="button" onclick="nk110SaveSettings()">Save</button></div>
        <div class="mini-note" id="nkFaultSetNote">Nothing is ever cleared automatically. Each Clear/Reset asks first, is blocked while the miner is hot or offline, and is logged. Webhooks for faults are off until you tick "Hardware fault" under Webhooks.</div>
      </div>
"""

NK110_JS = r"""
/* ================= Miner Hearth v1.10 ================= */
const NK110 = {open: "", tab: "faults", filter: "", filled: false, busy: false, diag: {}, diagLoading: {}, recView: {}, recLoading: {}};
if (typeof NK19_WEBHOOK_LABELS === "object") NK19_WEBHOOK_LABELS.hardware_fault = "Hardware fault (miner error flags)";
if (typeof NK19_SECTION_IDS === "object") NK19_SECTION_IDS.fault_history = "nkFaultPanel";

function nk110Ex(name) { const nk = lastStatus && lastStatus.nk; return nk && nk.devices ? (nk.devices[name] || {}) : {}; }
function nk110Dev(name) { return ((lastStatus && lastStatus.devices) || []).find(x => x.name === name) || null; }
function nk110Sev(f) { return f.severity === "problem" ? "problem" : (f.severity === "info" ? "info" : "warn"); }

function nk110FaultHtml(f, open) {
  const checks = (f.checks || []).map(c => `<li>${esc(c)}</li>`).join("");
  const compLine = f.component ? `<div><b>Component:</b> ${esc(f.component)}</div>` : "";
  const tsCause = f.cause_summary ? `<div><b>Recorded cause${f.cause_iso ? " (" + esc(f.cause_iso) + ")" : ""}:</b> ${esc(f.cause_summary)}</div>` : "";
  const logLine = f.log_line ? `<div><b>Firmware log:</b> <code>${esc(f.log_line)}</code></div>` : "";
  return `<details class="nk-fault ${nk110Sev(f)}" ${open ? "open" : ""}><summary>${esc(f.title)}${f.mapped ? "" : '<span class="nk-um">unmapped</span>'}<span class="nk-ftext">${esc(f.text)}</span></summary>
    ${compLine}${tsCause}${logLine}
    <div><b>What it means:</b> ${esc(f.meaning || "")}</div>
    ${f.cause ? `<div><b>Likely cause:</b> ${esc(f.cause)}</div>` : ""}
    ${checks ? `<div><b>First checks:</b><ul>${checks}</ul></div>` : ""}
    <div class="nk-tiny">Source: ${esc(f.source || "")}${f.code ? " · code " + esc(f.code) : ""}${f.first_seen_iso ? " · first seen " + esc(f.first_seen_iso) : ""}${f.guide_url ? ` · <a href="${esc(f.guide_url)}" target="_blank" rel="noopener">${esc(f.guide_label || "Guide")}</a>` : ""}</div></details>`;
}

function nk110ActionsHtml(d, fx) {
  const r = fx.reset || {}; const n = esc(d.name).replace(/'/g, "\\'");
  const parts = [];
  if (r.available) parts.push(`<button class="icon-btn" type="button" ${r.enabled ? "" : "disabled"} title="${esc(r.enabled ? (r.what || "") : r.reason)}" onclick="nk110Clear('${n}', '${esc(r.action)}')">${esc(r.label)}</button>${r.enabled ? "" : `<span class="nk-tiny">${esc(r.reason)}</span>`}`);
  if (fx.family !== "nerd" && fx.family !== "other") parts.push(`<button class="icon-btn" type="button" onclick="nk110Log('${n}')">Capture log</button>`);
  parts.push(`<button class="icon-btn nk-ro-ok" type="button" onclick="nk110Diag('${n}')">Diagnostics</button>`);
  return `<div class="nk-fault-actions">${parts.join("")}<span class="mini-note" id="nk110Note_${esc(d.name).replace(/[^\w-]/g, "_")}"></span></div>`;
}

function nk110Card(d) {
  if (!isFieldVisible("faults")) return "";
  const fx = nk110Ex(d.name).faults; if (!fx) return "";
  const act = (fx.active || []).filter(f => f.severity !== "info");
  if (!act.length) return "";
  const worst = act.some(f => f.severity === "problem");
  const n = esc(d.name).replace(/'/g, "\\'");
  return `<div class="nk-badges nk-faultbox"><span class="nk-badge ${worst ? "fault" : "faultw"}" style="cursor:pointer" onclick="nk110OpenDetails('${n}', 'faults')">${worst ? "⚠ " : ""}${act.length} fault${act.length > 1 ? "s" : ""}: ${esc(act[0].title)}</span>
    ${act.slice(0, 2).map(f => nk110FaultHtml(f, false)).join("")}${act.length > 2 ? `<div class="nk-tiny">+${act.length - 2} more in Details</div>` : ""}
    ${nk110ActionsHtml(d, fx)}</div>`;
}
function nk110DetailsBtn(d) {
  if (!isFieldVisible("details_button")) return "";
  const n = esc(d.name).replace(/'/g, "\\'");
  return `<button class="btn nk-ro-ok" type="button" onclick="nk110OpenDetails('${n}')" title="All component readings this machine reports">Details</button>`;
}

async function nk110Clear(name, action) {
  const d = nk110Dev(name); const fx = (nk110Ex(name).faults || {}); const r = fx.reset || {};
  const dry = lastStatus && lastStatus.dry_run;
  const msg = `${r.label} on ${name}?\n\nThis sends: ${r.what || action}${dry ? "\n\nDry-run is ON: nothing will be sent, it will only be logged." : ""}\n\nOnly do this after fixing the cause (see the checks).`;
  if (!confirm(msg)) return;
  const id = "nk110Note_" + name.replace(/[^\w-]/g, "_");
  try {
    const res = await post("/api/nk/clear-fault", {device: name, action, confirm: true});
    nkNote(id, (res.dry_run ? "Dry-run: " : "") + (res.result || "done"), "ok");
  } catch (e) { nkNote(id, "Not done: " + e.message, "err"); }
  refresh();
}
async function nk110Log(name) {
  const id = "nk110Note_" + name.replace(/[^\w-]/g, "_");
  nkNote(id, "Reading the miner's log…", "");
  try {
    const res = await post("/api/nk/log-snapshot", {device: name});
    nkNote(id, `Log saved (${Math.round((res.bytes || 0) / 1024)} KB): ${(res.findings || []).length} known issue(s) found.`, "ok");
    if (NK110.open === name) { NK110.tab = "faults"; nk110RenderModal(); }
  } catch (e) { nkNote(id, "Log not available: " + e.message, "err"); }
}
function nk110Diag(name) {
  nk110OpenDetails(name, "diagnostics");
}
async function nk110FetchDiag(name, force) {
  if (!name) return;
  if (NK110.diagLoading[name]) return;
  if (!force && NK110.diag[name] && (Date.now() - (NK110.diag[name]._fetchedAt || 0) < 10000)) return;
  NK110.diagLoading[name] = true;
  try {
    const r = await fetch("/api/nk/diagnostics?device=" + encodeURIComponent(name), {cache: "no-store"});
    if (r.ok) {
      const data = await r.json();
      data._fetchedAt = Date.now();
      NK110.diag[name] = data;
    }
  } catch (_) {}
  finally { NK110.diagLoading[name] = false; }
  if (NK110.open === name && NK110.tab === "diagnostics") nk110RenderModal();
}

async function nk110ViewRec(id, name) {
  if (!id) return;
  if (NK110.recView[id]) { delete NK110.recView[id]; nk110RenderModal(); return; }
  NK110.recLoading[id] = true;
  nk110RenderModal();
  try {
    const r = await fetch("/api/nk/fault-records/view?id=" + encodeURIComponent(id), {cache: "no-store"});
    if (r.ok) {
      const res = await r.json();
      if (res && res.record) NK110.recView[id] = res.record;
    }
  } catch (_) {}
  finally { NK110.recLoading[id] = false; }
  if (NK110.open === name && NK110.tab === "diagnostics") nk110RenderModal();
}
async function nk110ExportRec(id) {
  if (!id) return;
  const outName = id.replace(/\.json\.gz$/i, "") + "-redacted.json";
  const res = await nkDownload("/api/nk/fault-records/export?id=" + encodeURIComponent(id), outName);
  const el = $("nk110DiagNote");
  if (el && res) nkNote("nk110DiagNote", "Fault record " + res + " (personal data removed).", "ok");
}
async function nk110DeleteRec(id, name) {
  if (!id) return;
  if (!confirm(`Delete saved fault record ${id}?\n\nThis cannot be undone.`)) return;
  try {
    await post("/api/nk/fault-records/delete", {id});
    delete NK110.recView[id];
    nkNote("nk110DiagNote", "Deleted " + id, "ok");
    await nk110FetchDiag(name, true);
  } catch (e) { nkNote("nk110DiagNote", "Delete failed: " + e.message, "err"); }
}

async function nk110DownloadDiag(name) {
  const stamp = new Date().toISOString().slice(0, 10);
  const safe = String(name || "all").replace(/[^\w-]+/g, "_");
  const res = await nkDownload("/api/nk/diagnostics" + (name ? "?device=" + encodeURIComponent(name) : ""), `miner-hearth-diagnostics-${safe}-${stamp}.json`);
  const id = "nk110DiagNote";
  if ($(id) && res) nkNote(id, "Diagnostics " + res + " (personal data removed).", "ok");
}

/* ---- Details modal ---- */
function nk110OpenDetails(name, tab) { NK110.open = name; NK110.tab = tab || "components"; NK110.filter = ""; $("nk110Modal").hidden = false; if (NK110.tab === "diagnostics") nk110FetchDiag(name, true); nk110RenderModal(); }
function nk110Close() { NK110.open = ""; $("nk110Modal").hidden = true; }
document.addEventListener("keydown", e => { if (e.key === "Escape" && NK110.open) nk110Close(); });
function nk110Tab(t) { NK110.tab = t; if (t === "diagnostics" && NK110.open) nk110FetchDiag(NK110.open, true); nk110RenderModal(); }

function nk110Kv(rows) {
  const r = rows.filter(x => x && x[1] !== null && x[1] !== undefined && x[1] !== "" && x[1] !== "—");
  if (!r.length) return '<div class="nk-tiny">Not reported by this machine.</div>';
  return `<div class="nk110-kv">${r.map(([k, v]) => `<span>${esc(k)}</span><span>${esc(String(v))}</span>`).join("")}</div>`;
}
function nk110Box(title, rows) { return `<div class="nk110-box"><h4>${esc(title)}</h4>${typeof rows === "string" ? rows : nk110Kv(rows)}</div>`; }
function nk110N(v, d, unit) { if (v === null || v === undefined || v === "" || isNaN(Number(v))) return null; return Number(v).toFixed(d) + (unit ? " " + unit : ""); }
function nk110Volt(v) { if (v == null || isNaN(Number(v))) return null; const n = Number(v); return n > 100 ? (n / 1000).toFixed(2) + " V" : n.toFixed(2) + " V"; }
function nk110Up(s) { if (s == null || isNaN(s)) return null; s = Number(s); const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return (d ? d + "d " : "") + h + "h " + m + "m"; }

const NK110_CATS = [
  ["Temperatures", /temp|therm|_c$|overheat|ambient|inlet|outlet|pcb|chiptemp/i],
  ["Voltages & currents", /volt|vin|vout|vcore|_mv$|current|amp|iout|iin/i],
  ["Power & PSU", /power|watt|psu|_w$|energy/i],
  ["Fans", /fan|rpm/i],
  ["Frequency", /freq|mhz|clock/i],
  ["Hashrate & shares", /hash|ghs|mhs|ths|share|diff|accept|reject|stale|error/i],
  ["Chips & boards", /chip|asic|chain|board|domain|acn|acs/i],
  ["Network & Wi-Fi", /rssi|wifi|ping|stratum|pool|net|rtt/i],
  ["System", /uptime|elapsed|reset|version|firmware|model|heap|boot|build|idf|board/i],
];
function nk110Cat(k) { for (const [c, rx] of NK110_CATS) if (rx.test(k)) return c; return "Other"; }
function nk110RawVal(k, v) {
  const isT = /temp|therm|overheat|ambient|inlet|outlet|pcb/i.test(k) && !/time|count|mode|enabled|fault/i.test(k);
  const conv = x => (typeof x === "number" && isT) ? nkTn(x, 1) : x;
  if (Array.isArray(v)) return v.map(conv).join(", ") + (isT ? (nkUnitF() ? " °F" : " °C") : "");
  if (typeof v === "number") return isT ? nkT(v, 1) : String(Math.abs(v) >= 1e6 ? v.toExponential(4) : (Number.isInteger(v) ? v : Number(v.toFixed(4))));
  if (typeof v === "string" && isT && /^\s*-?\d+(\.\d+)?(\s*-\s*-?\d+(\.\d+)?)+\s*$/.test(v)) return v.split("-").map(x => nkTn(Number(x), 0)).join("-") + (nkUnitF() ? " °F" : " °C");
  if (v === null) return "null";
  return String(v);
}

function nk110Components(d, ex) {
  const fr = d.fault_raw || {};
  const temps = [
    ["Hottest chip / ASIC", d.hottest_asic_temp_c != null ? nkT(d.hottest_asic_temp_c, 1) : null],
    ["ASIC (reported)", d.asic_temp_c != null ? nkT(d.asic_temp_c, 1) : null],
    ...(d.asic_temps || []).map((t, i) => [`ASIC ${i}`, nkT(t, 1)]),
    ["VR / VRM", d.vr_temp_c != null ? nkT(d.vr_temp_c, 1) : null],
    ...(d.board_temps || []).map((t, i) => [`Board / PCB ${i + 1}`, nkT(t, 1)]),
    ...(d.type === "antminer" ? (d.chip_temps || []).map((t, i) => [`Chips, board ${i + 1}`, nkT(t, 1)]) : []),
    ["Ambient / intake", ex.ambient ? nkT(ex.ambient.c, 1) + " (" + (ex.ambient.source || "miner") + ")" : null],
    ["Chip target / max", d.asic_target_c != null ? nkTR(d.asic_target_c, d.asic_max_c) : null],
    ["VR target / max", d.vr_target_c != null && d.type !== "antminer" ? nkTR(d.vr_target_c, d.vr_max_c) : null],
  ];
  const power = [
    ["Power", nk110N(d.power_w, 1, "W")], ["Power limit", nk110N(d.power_limit_w, 0, "W")], ["Efficiency", nk110N(d.j_per_th, 1, "J/TH")],
    ["Input voltage", nk110Volt(d.input_voltage)], ["Input current", nk110N(d.input_current, 2, "A")],
    ["Core voltage (set)", nk110N(d.core_voltage_mv, 0, "mV")], ["Core voltage (measured)", nk110N(d.core_voltage_actual_mv, 0, "mV")],
    ["Nominal input voltage", fr.nominalVoltage != null ? nk110Volt(fr.nominalVoltage) : null],
  ];
  const fanSrc = Array.isArray(fr.fans) && fr.fans.length ? fr.fans.map((r, i) => ({id: i, rpm: r})) : (d.fans && d.fans.length ? d.fans : (d.fan_rpms || []).map((r, i) => ({id: i, rpm: r})));
  const fans = fanSrc.map(f =>
    [`Fan ${(f.id ?? 0) + 1}${f.label ? " (" + f.label + ")" : ""}`, [f.no_tach ? "speed % only (no RPM sensor)" : (f.rpm != null ? f.rpm + " RPM" : null), f.speed_pct != null ? Math.round(f.speed_pct) + " %" : null].filter(Boolean).join(" · ")]);
  const freq = [["Frequency (set)", nk110N(d.frequency_mhz, 0, "MHz")], ["Frequency (actual)", nk110N(d.actual_frequency_mhz, 0, "MHz")],
                ["Hashrate", d.hashrate_ghs != null ? fmtHashrate(d.hashrate_ghs) : null], ["Expected", d.expected_hashrate_ghs != null ? fmtHashrate(d.expected_hashrate_ghs) : null],
                ["HW error rate", nk110N(d.rolling_error_pct ?? d.error_pct, 2, "%")], ["HW errors", d.hw_errors]];
  const sys = [["Model", d.model], ["Firmware", [d.firmware, d.version].filter(Boolean).join(" ")], ["Uptime", nk110Up(d.uptime_sec)],
               ["Last reset reason", fr.resetReason || fr.lastResetReason || null], ["Wi-Fi RSSI", d.wifi_rssi != null ? d.wifi_rssi + " dBm (" + (d.wifi_quality || "") + ")" : null],
               ["Pool ping", nk110N(d.ping_rtt_ms, 0, "ms")], ["Stratum disconnects", d.stratum_disconnects], ["Wi-Fi disconnects", d.wifi_disconnects],
               ["Block height", d.block_height]];
  let chips = "";
  if ((d.asic_domains || []).length) {
    chips += `<table class="nk-table"><tr><th>Chip</th><th>Hashrate</th><th>Errors</th><th>Domains</th></tr>${d.asic_domains.map(a =>
      `<tr><td>ASIC ${esc(a.id)}</td><td>${esc(a.total != null ? fmtHashrate(a.total) : "—")}</td><td>${esc(a.error_count ?? "—")}</td><td>${esc((a.domains || []).map(x => Math.round(x)).join(" / "))}</td></tr>`).join("")}</table>`;
  }
  if ((d.boards || []).length) {
    chips += `<table class="nk-table"><tr><th>Board</th><th>Hashrate</th><th>Nominal</th><th>Board</th><th>Chip</th><th>HW err</th><th>Status</th></tr>${d.boards.map(b =>
      `<tr><td>${esc(b.id)}</td><td>${esc(b.hashrate_5s_ghs != null ? fmtHashrate(b.hashrate_5s_ghs) : "—")}</td><td>${esc(b.nominal_ghs != null ? fmtHashrate(b.nominal_ghs) : "—")}</td>
       <td>${esc(nkT(b.board_temp_c, 0))}</td><td>${esc(nkT(b.chip_temp_c, 0))}</td><td>${esc(b.hw_errors ?? "—")}</td><td>${esc(b.status || b.tuner_status || "")}</td></tr>`).join("")}</table>`;
  }
  if ((fr.chains || []).length) {
    chips += `<table class="nk-table"><tr><th>Chain</th><th>Chips</th><th>'x' (error)</th><th>'-' (missing)</th></tr>${fr.chains.map(c =>
      `<tr><td>${esc(c.idx)}</td><td>${esc(c.acn ?? "—")}</td><td>${esc((c.acs || "").split("x").length - 1)}</td><td>${esc((c.acs || "").split("-").length - 1)}</td></tr>`).join("")}</table>`;
  }
  return `<div class="nk110-grid">${nk110Box("Temperatures", temps)}${nk110Box("Power, voltage & current", power)}${nk110Box("Fans", fans)}
    ${nk110Box("Frequency & hashrate", freq)}${nk110Box("System", sys)}</div>
    ${chips ? `<div class="nk110-box" style="margin-top:10px"><h4>Chips &amp; boards</h4><div class="nk-table-wrap">${chips}</div></div>` : ""}
    <div class="nk-tiny" style="margin-top:8px">Read-only. Everything the miner's API returns is under "All fields"; values the firmware does not report are left out.</div>`;
}

function nk110Raw(d) {
  const raw = d.raw_fields || {}; const keys = Object.keys(raw);
  if (!keys.length) return '<div class="nk-tiny">No raw fields yet (the miner has not answered a poll).</div>';
  const f = (NK110.filter || "").toLowerCase();
  const groups = {};
  for (const k of keys) { if (f && !k.toLowerCase().includes(f)) continue; const c = nk110Cat(k.split(".").slice(1).join(".") || k); (groups[c] = groups[c] || []).push(k); }
  const order = [...NK110_CATS.map(x => x[0]), "Other"];
  const body = order.filter(c => groups[c]).map(c => `<div class="nk110-box" style="margin-bottom:8px"><h4>${esc(c)} <span class="nk-tiny">(${groups[c].length})</span></h4>
    <table class="nk-table nk110-raw">${groups[c].map(k => `<tr><td>${esc(k)}</td><td>${esc(nk110RawVal(k, raw[k]))}</td></tr>`).join("")}</table></div>`).join("");
  return `<input type="search" class="nk110-filter" placeholder="Filter by API field name (e.g. temp, fan, volt)" value="${esc(NK110.filter)}" oninput="NK110.filter=this.value;nk110RenderModal(true)">
    <div class="nk-tiny" style="margin-bottom:6px">${keys.length} fields exactly as the API names them (pool users, wallets, Wi-Fi names, MAC addresses and passwords are left out). Temperatures shown in ${nkUnitF() ? "°F" : "°C"}.</div>${body || '<div class="nk-tiny">No field matches.</div>'}`;
}

function nk110FaultsTab(d, ex) {
  const fx = ex.faults;
  if (!fx) return '<div class="nk-tiny">Fault display is turned off in Settings → Faults &amp; Clear/Reset.</div>';
  const act = fx.active || [];
  let h = act.length ? act.map(f => nk110FaultHtml(f, true)).join("") : `<div class="nk-fault-ok">No fault flags reported by this miner right now.</div>`;
  h += nk110ActionsHtml(d, fx);
  if (fx.probe_error) h += `<div class="nk-tiny nk-warn">Extra fault check failed: ${esc(fx.probe_error)}</div>`;
  const lg = fx.log;
  if (lg) {
    h += `<div class="nk110-box" style="margin-top:10px"><h4>Captured log <span class="nk-tiny">${esc(new Date(lg.ts * 1000).toLocaleString())} · ${esc(lg.source)} · ${Math.round(lg.bytes / 1024)} KB</span></h4>
      ${(lg.findings || []).length ? lg.findings.map(f => nk110FaultHtml(Object.assign({}, f, {text: `${f.count}× e.g. "${(f.examples || [""])[0]}"`}), false)).join("") : '<div class="nk-tiny">No known error lines found in the log.</div>'}</div>`;
  }
  if (d.type === "antminer" && fx.family !== "braiins") {
    h += `<div class="nk110-box" style="margin-top:10px"><h4>Bitmain web login (optional)</h4>
      <div class="nk-tiny">Needed only to capture the stock Bitmain log or reboot it from here (HTTP Digest, same login as the miner's web page). Stored in your userdata, never preset, left out of backups unless you include passwords. Host PC only.</div>
      <div class="nk-row" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px"><input type="text" id="nk110_wu" placeholder="user (usually root)" value="${esc(d.web_username || "")}" style="width:150px">
      <input type="password" id="nk110_wp" placeholder="${d.web_has_password ? "saved (leave blank to keep)" : "password"}" style="width:170px" autocomplete="off">
      <button class="icon-btn" type="button" onclick="nk110SaveWeb('${esc(d.name).replace(/'/g, "\\'")}', false)">Save</button>
      ${d.web_has_password ? `<button class="icon-btn" type="button" onclick="nk110SaveWeb('${esc(d.name).replace(/'/g, "\\'")}', true)">Forget password</button>` : ""}</div>
      <div class="mini-note" id="nk110WebNote"></div></div>`;
  }
  const rec = fx.recent || [];
  if (rec.length) h += `<div class="nk110-box" style="margin-top:10px"><h4>Recent fault history</h4><table class="nk-table nk110-hist">${rec.map(nk110HistRow).join("")}</table></div>`;
  return h;
}
async function nk110SaveWeb(name, clear) {
  try {
    const body = {device: name, web_username: $("nk110_wu").value.trim()};
    if (clear) body.clear_password = true; else if ($("nk110_wp").value) body.web_password = $("nk110_wp").value;
    await post("/api/nk/web-login", body); $("nk110_wp").value = "";
    nkNote("nk110WebNote", clear ? "Password removed." : "Saved.", "ok"); await refresh(); nk110RenderModal();
  } catch (e) { nkNote("nk110WebNote", "Could not save: " + e.message, "err"); }
}



function nk110RecordsHtml(devName, recs) {
  if (!recs || !recs.length) return '<div class="nk-tiny">No saved fault records yet. When a fault appears, the prior 12 hours (configurable) of high-resolution polls are automatically saved to userdata/fault_records/.</div>';
  const escDev = esc(devName).replace(/'/g, "\\'");
  return recs.map(r => {
    const rid = r.id || r.filename || "";
    const escId = esc(rid).replace(/'/g, "\\'");
    const trigs = (r.triggers || []).map(t => t.title || t.key).filter(Boolean);
    const trigTxt = trigs.length ? trigs.slice(-3).join(", ") : "Fault";
    const kb = r.size_bytes ? Math.max(1, Math.round(r.size_bytes / 1024)) + " KB" : "";
    const loaded = NK110.recView[rid];
    const loading = NK110.recLoading[rid];
    let detail = "";
    if (loading) detail = '<div class="nk-tiny" style="margin-top:6px">Loading fault record…</div>';
    else if (loaded) {
      const sm = loaded.summary || {};
      const sampRows = (loaded.samples || []).slice(-25).reverse().map(s =>
        `<tr><td>${esc(s.iso || "")}</td><td>${s.hashrate_ghs != null ? fmtHashrate(s.hashrate_ghs) : "—"}</td><td>${s.asic_temp_c != null ? nkT(s.asic_temp_c, 1) : "—"}</td><td>${s.vr_temp_c != null ? nkT(s.vr_temp_c, 1) : "—"}</td><td>${s.power_w != null ? Number(s.power_w).toFixed(1) + " W" : "—"}</td><td>${s.frequency_mhz ?? "—"}</td><td>${s.core_voltage_mv ?? "—"}/${s.core_voltage_actual_mv ?? "—"}</td></tr>`
      ).join("");
      detail = `<div style="margin-top:8px;padding:8px;border:1px solid var(--border);border-radius:8px;background:var(--bg)">
        <div class="nk-tiny">Window: ${esc(loaded.window_start_iso || "")} → ${esc(loaded.window_end_iso || "")} · ${loaded.total_samples ?? 0} total sample(s) (redacted)</div>
        <div class="nk-tiny">ASIC: ${sm.asic_min_c != null ? nkT(sm.asic_min_c, 1) : "—"}–${sm.asic_max_c != null ? nkT(sm.asic_max_c, 1) : "—"} · VR: ${sm.vr_min_c != null ? nkT(sm.vr_min_c, 1) : "—"}–${sm.vr_max_c != null ? nkT(sm.vr_max_c, 1) : "—"} · Power: ${sm.power_min_w != null ? Number(sm.power_min_w).toFixed(1) : "—"}–${sm.power_max_w != null ? Number(sm.power_max_w).toFixed(1) + " W" : "—"}</div>
        <div class="nk-table-wrap" style="margin-top:6px;max-height:220px;overflow:auto"><table class="nk-table">
          <tr><th>Time</th><th>Hashrate</th><th>ASIC</th><th>VR</th><th>Power</th><th>MHz</th><th>mV (set/act)</th></tr>${sampRows}
        </table></div>
      </div>`;
    }
    return `<div style="padding:6px 0;border-bottom:1px solid var(--border)">
      <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap">
        <div><b>${esc(trigTxt)}</b> · <span class="nk-tiny">${esc(r.updated_iso || r.created_iso || "")} · ${r.sample_count ?? 0} samples${kb ? " · " + kb : ""}</span><div class="nk-tiny">${esc(rid)}</div></div>
        <div class="nk-fault-actions">
          <button class="icon-btn nk-ro-ok" type="button" onclick="nk110ViewRec('${escId}', '${escDev}')">${loaded ? "Hide" : "View"}</button>
          <button class="icon-btn nk-ro-ok" type="button" onclick="nk110ExportRec('${escId}')">Export JSON</button>
          <button class="icon-btn" type="button" onclick="nk110DeleteRec('${escId}', '${escDev}')">Delete</button>
        </div>
      </div>${detail}
    </div>`;
  }).join("");
}

function nk110DiagnosticsTab(d, ex) {
  const bundle = NK110.diag[d.name];
  const devEntry = bundle && Array.isArray(bundle.devices) ? (bundle.devices[0] || {}) : {};
  const fx = ex.faults || {};
  const act = devEntry.faults || fx.active || [];
  const pre = devEntry.pre_fault_snapshot || fx.pre_fault_snapshot || null;
  const lastVr = devEntry.last_vr_overheat || fx.last_vr_overheat || null;
  const safety = devEntry.safety || {};
  const payout = devEntry.payout_check || ex.payout_check || {};
  const hist = devEntry.fault_history || fx.recent || [];
  const tlog = devEntry.tuning_log || [];
  const escName = esc(d.name).replace(/'/g, "\\'");

  const guardRows = [
    ["Autotune action", d.last_action || "—"],
    ["Last stable freq", (d.last_stable_freq ?? safety.last_stable_freq) != null ? (d.last_stable_freq ?? safety.last_stable_freq) + " MHz" : "—"],
    ["Rollback freq", (d.rollback_freq ?? safety.rollback_freq) != null ? (d.rollback_freq ?? safety.rollback_freq) + " MHz" : "—"],
    ["Emergency lockout", d.emergency_lockout_remaining_sec > 0 ? Math.ceil(d.emergency_lockout_remaining_sec / 60) + " min remaining" : "None"],
    ["Remembered VR overheat", lastVr && lastVr.temp_c != null ? `${Number(lastVr.temp_c).toFixed(1)} °C (${lastVr.sensor || "vrTemp"}) at ${lastVr.iso || ""}` : "None"],
    ["Overclock (>800 MHz)", d.type === "nerdoctaxe" ? (d.overclock_opt_in ? "Opted in (max 850 MHz)" : "Capped at 800 MHz") : "N/A"],
    ["Block payout check", payout.source ? `${payout.source}${payout.note ? " — " + payout.note : ""}` : "—"],
    ["Sensor guard", d.sensor_suspect ? (d.sensor_suspect_reason || "Suspect") : "OK"],
  ];

  let snapHtml = '<div class="nk-tiny">No pre-fault snapshot captured yet (saved automatically when a fault or 0 GH/s appears after healthy mining).</div>';
  if (pre && pre.snapshot) {
    const s = pre.snapshot;
    const rows = [
      ["Captured before", `${pre.trigger || "fault"} (${pre.captured_iso || ""})`],
      ["Healthy at", pre.snapshot.iso || "—"],
      ["Hashrate", (s.hashrate_ghs != null ? fmtHashrate(s.hashrate_ghs) : "—") + " → now " + (d.hashrate_ghs != null ? fmtHashrate(d.hashrate_ghs) : "—")],
      ["Power", (s.power_w != null ? Number(s.power_w).toFixed(1) + " W" : "—") + " → now " + (d.power_w != null ? Number(d.power_w).toFixed(1) + " W" : "—")],
      ["ASIC temp", (s.asic_temp_c != null ? nkT(s.asic_temp_c, 1) : "—") + " → now " + (d.asic_temp_c != null ? nkT(d.asic_temp_c, 1) : "—")],
      ["VR temp", (s.vr_temp_c != null ? nkT(s.vr_temp_c, 1) : "—") + " → now " + (d.vr_temp_c != null ? nkT(d.vr_temp_c, 1) : "—")],
      ["Frequency", (s.frequency_mhz != null ? s.frequency_mhz + " MHz" : "—") + " → now " + (d.frequency_mhz != null ? d.frequency_mhz + " MHz" : "—")],
      ["Core V (set/actual)", `${s.core_voltage_mv ?? "—"}/${s.core_voltage_actual_mv ?? "—"} mV → ${d.core_voltage_mv ?? "—"}/${d.core_voltage_actual_mv ?? "—"} mV`],
    ];
    snapHtml = nk110Kv(rows);
  }

  const faultHtml = act.length
    ? act.map(f => nk110FaultHtml(f, true)).join("")
    : '<div class="nk-fault-ok">No active fault flags right now.</div>';

  const histHtml = hist.length
    ? `<table class="nk-table nk110-hist">${hist.slice(0, 15).map(nk110HistRow).join("")}</table>`
    : '<div class="nk-tiny">No fault history recorded for this machine.</div>';

  const tuneHtml = tlog.length
    ? `<table class="nk-table">${tlog.slice(0, 12).map(e => `<tr><td>${esc(new Date((e.ts || 0) * 1000).toLocaleString())}</td><td>${esc(e.action || e.kind || "")}</td><td>${esc((e.old ?? "") + " → " + (e.new ?? ""))}</td><td style="text-align:left;white-space:normal">${esc(e.reason || "")}</td></tr>`).join("")}</table>`
    : '<div class="nk-tiny">No tuning events recorded yet.</div>';

  const jsonPreview = bundle
    ? `<details style="margin-top:10px"><summary style="cursor:pointer">Redacted diagnostics JSON (personal data removed)</summary><pre style="max-height:260px;overflow:auto;font-size:.75rem;background:var(--bg);padding:8px;border-radius:8px">${esc(JSON.stringify(bundle, null, 2))}</pre></details>`
    : '<div class="nk-tiny" style="margin-top:8px">Loading full redacted diagnostics bundle…</div>';

  return `<div class="nk-fault-actions" style="margin-bottom:8px">
      <button class="icon-btn nk-ro-ok" type="button" onclick="nk110FetchDiag('${escName}', true)">Refresh</button>
      <button class="icon-btn nk-ro-ok" type="button" onclick="nk110DownloadDiag('${escName}')">Download redacted JSON</button>
      <span class="mini-note" id="nk110DiagNote"></span>
    </div>
    <div class="nk110-grid">
      ${nk110Box("Safety & Guard State", guardRows)}
      ${nk110Box("Pre-Fault Snapshot vs Current", snapHtml)}
    </div>
    <div class="nk110-box" style="margin-top:10px"><h4>Active Faults, Components &amp; Timestamped Causes</h4>${faultHtml}</div>
    <div class="nk110-box" style="margin-top:10px"><h4>Saved Pre-Fault Records (${(devEntry.fault_records || []).length})</h4>${nk110RecordsHtml(d.name, devEntry.fault_records || [])}</div>
    <div class="nk110-box" style="margin-top:10px"><h4>Recent Fault History</h4><div class="nk-table-wrap">${histHtml}</div></div>
    <div class="nk110-box" style="margin-top:10px"><h4>Recent Tuning Log</h4><div class="nk-table-wrap">${tuneHtml}</div></div>
    ${jsonPreview}`;
}

function nk110RenderModal(keepFocus) {
  if (!NK110.open) return;
  const d = nk110Dev(NK110.open);
  if (!d) { $("nk110Body").innerHTML = '<div class="nk-tiny">This machine is no longer in the list.</div>'; return; }
  const ex = nk110Ex(d.name);
  $("nk110Title").textContent = d.name + " — " + (d.model || d.type || "") + (d.online ? "" : " (offline)");
  $("nk110Upd").textContent = d.updated_at ? "updated " + new Date(d.updated_at).toLocaleTimeString() : "";
  const nf = ((ex.faults || {}).active || []).filter(f => f.severity !== "info").length;
  const tabs = [["faults", "Faults" + (nf ? " (" + nf + ")" : "")], ["diagnostics", "Diagnostics"], ["components", "Components"], ["raw", "All fields (" + Object.keys(d.raw_fields || {}).length + ")"]];
  $("nk110Tabs").innerHTML = tabs.map(([k, l]) => `<button type="button" class="${NK110.tab === k ? "on" : ""}" onclick="nk110Tab('${k}')">${esc(l)}</button>`).join("");
  const body = $("nk110Body");
  const active = document.activeElement; const fid = active && active.id; const pos = active && active.selectionStart;
  if (!keepFocus && body.contains(active) && active.tagName === "INPUT") return;  // don't clobber typing on refresh
  body.innerHTML = NK110.tab === "raw" ? nk110Raw(d) : (NK110.tab === "faults" ? nk110FaultsTab(d, ex) : (NK110.tab === "diagnostics" ? nk110DiagnosticsTab(d, ex) : nk110Components(d, ex)));
  if (keepFocus) { const el = body.querySelector(".nk110-filter"); if (el) { el.focus(); try { el.setSelectionRange(pos, pos); } catch (_) {} } }
}

/* ---- fault history panel ---- */
const NK110_EV = {appeared: "Appeared", cleared: "Cleared", user_reset: "Clear/Reset", auto_restart: "Auto restart", log_snapshot: "Log captured"};
function nk110HistRow(e) {
  const when = new Date(e.ts * 1000).toLocaleString();
  const what = e.event === "user_reset" ? `${e.action || ""}${e.dry_run ? " (dry-run)" : ""}${e.ok === false ? " — failed" : ""}: ${e.text || ""}`
    : (e.event === "cleared" && e.duration_sec != null ? `${e.title || ""} (after ${nk110Up(e.duration_sec)})` : (e.title ? e.title + (e.text ? " — " + e.text : "") : (e.text || e.kind || "")));
  return `<tr><td>${esc(when)}</td><td>${esc(e.device || "")}</td><td class="ev-${esc(e.event)}">${esc(NK110_EV[e.event] || e.event)}</td><td style="text-align:left;white-space:normal">${esc(what)}</td></tr>`;
}
function nk110RenderHistory() {
  const el = $("nkFaultPanel"); if (!el) return;
  const nk = NK.last || {};
  if (!isFieldVisible("fault_history") || !nk.fault_history) { el.hidden = true; return; }
  el.hidden = false;
  const sel = $("nkFaultDev"); const cur = sel.value;
  const names = ((lastStatus && lastStatus.devices) || []).map(d => d.name);
  const want = '<option value="">All machines</option>' + names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  if (sel.dataset.k !== names.join("|")) { sel.innerHTML = want; sel.value = cur; sel.dataset.k = names.join("|"); }
  const rows = (nk.fault_history || []).filter(e => !sel.value || e.device === sel.value);
  $("nkFaultTable").innerHTML = rows.length ? `<tr><th>When</th><th>Machine</th><th>Event</th><th style="text-align:left">Details</th></tr>` + rows.map(nk110HistRow).join("")
    : '<tr><td class="nk-tiny">No faults recorded yet.</td></tr>';
}

/* ---- settings ---- */
function nk110Fill(nk) {
  const s = nk.settings || {}; const f = s.faults; if (!f) return;
  const toU = c => nkUnitF() ? Math.round(c * 9 / 5 + 32) : c;
  $("nk_f_en").checked = !!f.enabled; $("nk_f_probe").checked = !!f.probe_enabled;
  $("nk_f_alert").checked = !!((s.alerts || {}).hardware_fault || {}).enabled;
  $("nk_f_cool_a").value = toU(f.cool_asic_c); $("nk_f_cool_v").value = toU(f.cool_vr_c);
  $("nk_f_gap").value = f.min_gap_min; $("nk_f_max").value = f.max_per_day;
  if ($("nk_f_lockout")) $("nk_f_lockout").value = (s.tuner && s.tuner.emergency_lockout_min != null) ? s.tuner.emergency_lockout_min : 180;
  if ($("nk_f_rec_hours")) $("nk_f_rec_hours").value = (f.record_hours != null) ? f.record_hours : 12;
  document.querySelectorAll(".nk110-unit").forEach(x => { x.textContent = nkUnitF() ? "°F" : "°C"; });
  NK110.filled = true; NK110.unit = NK19.unit;
}
function nk110SaveSettings() {
  const toC = v => nkUnitF() ? (Number(v) - 32) * 5 / 9 : Number(v);
  const lockMin = $("nk_f_lockout") ? nkN("nk_f_lockout") : 180;
  nkSave({faults: {enabled: $("nk_f_en").checked, probe_enabled: $("nk_f_probe").checked, cool_asic_c: toC($("nk_f_cool_a").value), cool_vr_c: toC($("nk_f_cool_v").value),
                   min_gap_min: nkN("nk_f_gap"), max_per_day: nkN("nk_f_max"), record_hours: $("nk_f_rec_hours") ? nkN("nk_f_rec_hours") : 12},
          tuner: {emergency_lockout_min: lockMin},
          alerts: {hardware_fault: {enabled: $("nk_f_alert").checked}}}, "nkFaultSetNote");
  NK110.filled = false;
}

function nk110Render(data) {
  const nk = data.nk; if (!nk) return;
  if (!NK110.filled || NK110.unit !== NK19.unit) nk110Fill(nk);
  nk110RenderHistory();
  if (NK110.open && NK110.tab !== "raw") nk110RenderModal();
  else if (NK110.open) { const a = document.activeElement; if (!(a && a.classList && a.classList.contains("nk110-filter"))) nk110RenderModal(); }
}
"""


def inject110(html: str) -> str:
    """Apply the v1.10 UI on top of the v1.9 dashboard HTML."""
    def rep(old: str, new: str, count: int = 1) -> None:
        nonlocal html
        if old not in html:
            raise RuntimeError(f"v1.10 dashboard injection point missing: {old[:70]!r}")
        html = html.replace(old, new, count)

    def resub(pattern: str, repl: str, want: int) -> None:
        nonlocal html
        html, n = re.subn(pattern, repl, html)
        if n != want:
            raise RuntimeError(f"v1.10 dashboard pattern matched {n} times (want {want}): {pattern[:70]!r}")

    rep("</style>", NK110_CSS + "</style>")
    rep('  <section class="nk-panel" id="nkTuningPanel"', NK110_PANEL_HTML + '  <section class="nk-panel" id="nkTuningPanel"')
    rep('      <div class="settings-box nk-set" id="nkSetAlerts">', NK110_SETTINGS_HTML + '      <div class="settings-box nk-set" id="nkSetAlerts">')
    rep("</body>", NK110_MODAL_HTML + "</body>")
    # Fault box right under the card header (both card types); Details button first in the action row.
    resub(r'(<div class="metrics">\$\{metrics(?:Ant|Tunable)\})', r'${typeof nk110Card === "function" ? nk110Card(d) : ""}\n      \1', 2)
    resub(r'(<div class="row-actions">)(\s*)(<button class="btn start")', r'\1\2${typeof nk110DetailsBtn === "function" ? nk110DetailsBtn(d) : ""}\3', 2)
    rep('  if (typeof nk19Render === "function") nk19Render(data);', '  if (typeof nk19Render === "function") nk19Render(data);\n  if (typeof nk110Render === "function") nk110Render(data);')
    rep("\nrefresh();\nsetInterval(refresh, 3000);\n</script>", NK110_JS + "\nrefresh();\nsetInterval(refresh, 3000);\n</script>")
    return html
