"""Miner Hearth v1.12 dashboard UI (themes, °F, layout, coins, pools, comparison, new settings)."""

from __future__ import annotations

import re

NK19_CSS = r"""
  .nk-set .nk-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; margin: 6px 0; font-size: .82rem; color: var(--text); }
  .nk-set .nk-row > label { color: var(--muted); display: inline-flex; align-items: center; gap: 6px; }
  .nk-set .nk-row .nk-tiny { flex-basis: 100%; }
  .nk-set .nk-row textarea { min-height: 34px; }
  .nk-set .nk-coinlbl { flex-basis: 100%; color: var(--muted); }
  /* ---- v1.9 themes ---- */
  body.theme-light {
    --bg: #f3f5f8; --bg-card: #ffffff; --bg-soft: #eef1f5; --border: #cfd6e0; --text: #18202b; --muted: #5b6878;
    --accent: #0a9f7a; --warn: #a66d00; --danger: #c62828; --ok: #0a9f7a; --info: #1f6fd1; --paused: #6b7889;
    --shadow: 0 4px 14px rgba(20,30,45,.10);
  }
  body.theme-contrast {
    --bg: #000; --bg-card: #000; --bg-soft: #111; --border: #fff; --text: #fff; --muted: #e0e0e0;
    --accent: #00ffd0; --warn: #ffe600; --danger: #ff5050; --ok: #00ff7f; --info: #66ccff; --paused: #cccccc; --shadow: none;
  }
  body.theme-contrast .card, body.theme-contrast .fleet-bar, body.theme-contrast .nk-panel { border-width: 2px; }
  body.theme-contrast a { color: #66ccff; text-decoration: underline; }
  body.theme-light .nk-netbar, body.theme-light .nk-econ { color: var(--text); }
  /* ---- v1.9 layout (section + field order) ---- */
  body.nk-flex { display: flex; flex-direction: column; min-height: 100vh; }
  body.nk-flex > header { order: 0; } body.nk-flex > footer { order: 999; }
  .card.nk-cardflex { display: flex; flex-direction: column; }
  .card.nk-cardflex > .row-actions { order: 1000; }
  .nk-lay-list { display: flex; flex-direction: column; gap: 3px; max-height: 260px; overflow: auto; border: 1px solid var(--border); border-radius: 8px; padding: 4px; }
  .nk-lay-row { display: flex; align-items: center; gap: 6px; padding: 3px 6px; border-radius: 6px; background: var(--bg-soft); cursor: grab; font-size: .84rem; }
  .nk-lay-row.off { opacity: .5; }
  .nk-lay-row.drag-over { outline: 2px dashed var(--accent); }
  .nk-lay-row span.l { flex: 1; }
  .nk-lay-cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
  /* ---- v1.9 panels ---- */
  .nk-table { width: 100%; border-collapse: collapse; font-size: .84rem; font-variant-numeric: tabular-nums; }
  .nk-table th, .nk-table td { padding: 5px 8px; border-bottom: 1px solid var(--border); text-align: right; white-space: nowrap; }
  .nk-table th:first-child, .nk-table td:first-child { text-align: left; }
  .nk-table th { color: var(--muted); font-weight: 600; cursor: pointer; user-select: none; position: sticky; top: 0; background: var(--bg-card); }
  .nk-table th.sorted::after { content: " \25BE"; } .nk-table th.sorted.asc::after { content: " \25B4"; }
  .nk-table-wrap { overflow-x: auto; }
  .nk-coins { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 10px; }
  .nk-coin { background: var(--bg-soft); border: 1px solid var(--border); border-radius: 10px; padding: 8px 12px; }
  .nk-coin h4 { margin: 0 0 4px; font-size: .95rem; display: flex; justify-content: space-between; gap: 6px; }
  .nk-coin .big { font-size: 1.05rem; font-weight: 700; }
  .nk-coin .tiny, .nk-tiny { color: var(--muted); font-size: .74rem; }
  .nk-badge.coin { border-color: var(--info); color: var(--info); }
  .nk-badge.quiet { border-color: #8e7cff; color: #b3a8ff; }
  .nk-badge.wear { border-color: var(--warn); color: var(--warn); }
  .nk-badge.unknown { border-color: var(--muted); color: var(--muted); }
  .nk-warn { color: var(--warn); } .nk-bad { color: var(--danger); } .nk-good { color: var(--ok); }
  .nk-avg-label { color: #f5c542; }
  @media (max-width: 720px) { .nk-table { font-size: .78rem; } .nk-table th, .nk-table td { padding: 4px 5px; } }
"""

NK19_PANELS_HTML = r"""
  <section class="nk-panel" id="nkCoins" hidden>
    <div class="nk-panel-head"><h2>Block odds per coin</h2><span class="nk-tiny" id="nkCoinsNote">Each machine's coin is detected from its stratum job, payout address and pool name. Change it in Settings &rarr; Coins.</span></div>
    <div class="nk-coins" id="nkCoinsBody"></div>
  </section>
  <section class="nk-panel" id="nkCompare" hidden>
    <div class="nk-panel-head"><h2>Fleet comparison</h2><span class="nk-tiny">Click a column to sort.</span></div>
    <div class="nk-table-wrap"><table class="nk-table" id="nkCompareTable"></table></div>
  </section>
  <section class="nk-panel" id="nkPools" hidden>
    <div class="nk-panel-head"><h2>Pool comparison</h2><span class="nk-tiny">Shares from your machines; latency / uptime measured from this PC every 2 min (miner ping where the firmware reports it).</span></div>
    <div class="nk-table-wrap"><table class="nk-table" id="nkPoolsTable"></table></div>
  </section>
"""

NK19_SETTINGS_HTML = r"""
      <div class="settings-box nk-set" id="nkSetAppearance">
        <h3>Appearance</h3>
        <div class="nk-row"><label>Theme <select id="nk_ui_theme" onchange="nk19SaveUi()"><option value="dark">Dark</option><option value="light">Light</option><option value="contrast">High contrast</option></select></label>
          <label>Temperatures <select id="nk_ui_unit" onchange="nk19SaveUi()"><option value="C">Celsius (°C)</option><option value="F">Fahrenheit (°F)</option></select></label></div>
        <div class="mini-note">Display only: limits, targets and the tuner always work in °C, so settings fields stay in °C.</div>
      </div>
      <div class="settings-box nk-set" id="nkSetLayout">
        <h3>Layout</h3>
        <div class="mini-note" style="margin-top:0">Drag rows or use ▲ / ▼. Hidden fields (unticked in Visible Data Fields) are greyed out but keep their place.</div>
        <div class="nk-lay-cols">
          <div><b>Dashboard sections</b><div class="nk-lay-list" id="nkLaySections"></div></div>
          <div><b>Summary bar</b><div class="nk-lay-list" id="nkLaySummary"></div></div>
          <div><b>Machine card fields</b><div class="nk-lay-list" id="nkLayCard"></div></div>
        </div>
        <div class="row-actions" style="margin-top:8px"><button class="icon-btn" type="button" onclick="nk19ResetLayout()">Reset layout to default</button></div>
        <div class="mini-note" id="nkLayoutNote"></div>
      </div>
      <div class="settings-box nk-set" id="nkSetCoins">
        <h3>Coins &amp; Per-Coin Payout Addresses</h3>
        <div id="nkCoinMachines"></div>
        <div class="mini-note">Auto uses the miner's stratum job (block height / difficulty vs. each chain's tip), the payout address format and the pool name. Unsure = "unknown".</div>
        <div id="nkCoinAddrs"></div>
        <div class="mini-note">Bitcoin payout addresses are set in Block-Found Confirmation above. A block is only celebrated when that coin's explorer shows a new block whose coinbase pays one of these addresses (solo mining).</div>
        <label class="nk-row"><input type="checkbox" id="nk_c_fetch"> Look up other chains when a machine may be mining them</label>
        <div class="row-actions"><button class="add-btn" type="button" onclick="nk19SaveCoins()">Save coins</button></div>
        <div class="mini-note" id="nkCoinsSetNote"></div>
      </div>
      <div class="settings-box nk-set" id="nkSetHealth">
        <h3>Pool &amp; Chip Health</h3>
        <label class="nk-row"><input type="checkbox" id="nk_pr_en"> Alert when a pool rejects shares: at <input type="number" id="nk_pr_pct" min="0.5" max="50" step="0.5" style="width:64px">% rejected+stale over <input type="number" id="nk_pr_win" min="5" max="720" style="width:64px"> min (min. <input type="number" id="nk_pr_min" min="1" max="100000" style="width:70px"> shares)</label>
        <label class="nk-row"><input type="checkbox" id="nk_pp_en"> Measure pool latency &amp; uptime from this PC</label>
        <label class="nk-row"><input type="checkbox" id="nk_ch_en"> Track chip health: stable after <input type="number" id="nk_ch_stable" min="5" max="720" style="width:64px"> min; wear warning at +<input type="number" id="nk_ch_mv" min="5" max="200" style="width:64px"> mV over <input type="number" id="nk_ch_days" min="1" max="365" style="width:60px"> days</label>
        <label class="nk-row"><input type="checkbox" id="nk_cw_en"> Notify on chip wear</label>
        <div class="row-actions"><button class="add-btn" type="button" onclick="nk19SaveHealth()">Save</button></div>
        <div class="mini-note" id="nkHealthNote"></div>
      </div>
      <div class="settings-box nk-set" id="nkSetQuiet">
        <h3>Quiet Mode (fan cap)</h3>
        <label class="nk-row"><input type="checkbox" id="nk_q_enabled"> Enable quiet windows</label>
        <div id="nkQuietWins"></div>
        <div class="row-actions"><button class="icon-btn" type="button" onclick="nk19AddQuiet()">Add quiet window</button><button class="add-btn" type="button" onclick="nk19SaveQuiet()">Save quiet mode</button></div>
        <div class="mini-note" id="nkQuietNote">AxeOS miners with autotune ON only. The fan is capped, the tuner lowers frequency to keep temperatures on target, and at emergency temperature the fan goes straight back to automatic. Closing the app also restores automatic fan.</div>
      </div>
      <div class="settings-box nk-set" id="nkSetRestarts">
        <h3>Maintenance Restarts</h3>
        <label class="nk-row"><input type="checkbox" id="nk_r_week"> Weekly restart on <select id="nk_r_day"></select> at <input type="time" id="nk_r_time"></label>
        <label class="nk-row"><input type="checkbox" id="nk_r_hung"> Restart a hung miner (hashrate 0 but API answering) after <input type="number" id="nk_r_hung_min" min="5" max="1440" style="width:64px"> min</label>
        <label class="nk-row">At most <input type="number" id="nk_r_max" min="1" max="6" style="width:52px"> per 24 h, at least <input type="number" id="nk_r_gap" min="15" max="1440" style="width:64px"> min apart</label>
        <label class="nk-row">Machines <input type="text" id="nk_r_devs" placeholder="Leave blank for all machines" style="flex:1;min-width:140px"></label>
        <div class="row-actions"><button class="add-btn" type="button" onclick="nk19SaveRestarts()">Save restarts</button></div>
        <div class="mini-note" id="nkRestartNote">Off by default. Dry-run only logs. Antminers need Braiins OS+ with the login set on the card.</div>
      </div>
      <div class="settings-box nk-set" id="nkSetHooks">
        <h3>Webhook Alerts</h3>
        <label class="nk-row"><input type="checkbox" id="nk_w_en"> Send alerts to webhooks</label>
        <label class="nk-row">Discord webhook URL <input type="text" id="nk_w_discord" autocomplete="off" placeholder="https://discord.com/api/webhooks/..." style="flex:1;min-width:160px"></label>
        <label class="nk-row">Telegram bot token <input type="password" id="nk_w_tg_token" autocomplete="off" style="flex:1;min-width:140px"> chat id <input type="text" id="nk_w_tg_chat" style="width:120px"></label>
        <label class="nk-row">ntfy topic URL <input type="text" id="nk_w_ntfy" autocomplete="off" placeholder="https://ntfy.sh/your-secret-topic" style="flex:1;min-width:160px"> token <input type="password" id="nk_w_ntfy_tok" autocomplete="off" style="width:110px"></label>
        <div class="field-checks" id="nkWebhookTypes"></div>
        <div class="row-actions"><button class="add-btn" type="button" onclick="nk19SaveHooks()">Save webhooks</button><button class="icon-btn" type="button" onclick="nk19TestHooks()">Send test</button></div>
        <div class="mini-note" id="nkHooksNote">Off by default. URLs and tokens are secrets: hidden from phones/LAN and left out of backups unless you tick "include passwords".</div>
      </div>
"""

NK19_JS = r"""
/* ================= Miner Hearth v1.9 ================= */
const NK19 = {unit: "C", sort: {key: "name", asc: true}, quiet: [], layoutDrag: null, lastLayoutKey: ""};
const NK19_WEBHOOK_LABELS = {block_confirmed: "Block Confirmed (on the blockchain)", block_possible: "Block Found", offline: "Machine offline", hashrate_drop: "Hashrate drop",
  fallback_pool: "Fallback pool", no_shares: "No shares", pool_rejects: "Pool rejecting shares", chip_wear: "Chip wear", restart: "Maintenance restart", recovered: "Back online"};
const NK19_SUMMARY_LABELS = {online: "Machines online", hashrate: "Total hashrate", power: "Total power", efficiency: "Fleet efficiency", hottest_chip: "Hottest chip", hottest_vr: "Hottest VR", best_share: "Best share ever"};
const NK19_SECTION_IDS = {summary: "fleetSummary", econ: "nkEcon", coins: "nkCoins", charts: "nkHistoryPanel", compare: "nkCompare", pools: "nkPools", cards: "cards", tuning_log: "nkTuningPanel", logs: "logsSection"};

/* ---- temperatures (display only; logic is always °C) ---- */
function nkUnitF() { return NK19.unit === "F"; }
function nkTn(c, d) { if (c === null || c === undefined || c === "" || isNaN(c)) return "—"; const v = nkUnitF() ? Number(c) * 9 / 5 + 32 : Number(c); return v.toFixed(d ?? 1); }
function nkT(c, d) { const s = nkTn(c, d); return s === "—" ? s : s + (nkUnitF() ? " °F" : " °C"); }
function nkTR(lo, hi) { return nkTn(lo, 0) + "–" + nkTn(hi, 0) + (nkUnitF() ? " °F" : " °C"); }
const _nkFmtC = fmt;
fmt = function (v, digits, unit) {
  if ((unit === "°C" || unit === "°") && v !== null && v !== undefined && v !== "" && !isNaN(Number(v))) return nkTn(v, digits) + (unit === "°" ? "°" : (nkUnitF() ? " °F" : " °C"));
  return _nkFmtC(v, digits, unit);
};

/* ---- layout ---- */
function nkTag(html, key) {
  if (!html) return html;
  return String(html).replace(/^(\s*<[a-zA-Z]+)/, `$1 data-nkf="${key}"`);
}
function nk19Layout() { const s = NK.last && NK.last.settings; return (s && s.layout) || {card_fields: [], summary_fields: [], sections: []}; }
function nk19OrderChildren(container, order, fallback) {
  const seen = {};
  for (const el of container.children) {
    const k = el.dataset ? el.dataset.nkf : null;
    if (!k) { if (fallback) el.style.order = fallback(el); continue; }
    const i = order.indexOf(k);
    seen[k] = (seen[k] || 0) + 1;
    el.style.order = String((i < 0 ? 400 : i) * 10 + seen[k]);
  }
}
function nk19ApplyLayout() {
  const lay = nk19Layout();
  document.body.classList.add("nk-flex");
  const fixed = {nkNetBar: 1, nkTouBar: 2, nkBanners: 3, settingsPanel: 4, scanPanel: 5, error: 6};
  for (const [id, o] of Object.entries(fixed)) { const el = $(id); if (el) el.style.order = o; }
  (lay.sections || []).forEach((k, i) => { const el = $(NK19_SECTION_IDS[k]); if (el) el.style.order = String(10 + i); });
  const fs = $("fleetSummary"); if (fs) nk19OrderChildren(fs, lay.summary_fields || [], null);
  for (const card of document.querySelectorAll("#cards .card")) {
    card.classList.add("nk-cardflex");
    const m = card.querySelector(".metrics"); if (m) nk19OrderChildren(m, lay.card_fields || [], () => "4000");
    nk19OrderChildren(card, lay.card_fields || [], el => {
      if (el.classList.contains("card-head")) return "-20";
      if (el.classList.contains("nk-badges")) return "-10";
      if (el.classList.contains("metrics")) return "-5";
      if (el.classList.contains("parse-err")) return "-4";
      if (el.classList.contains("row-actions")) return "1000";
      return "4000";
    });
  }
}
function nk19FieldLabel(k) {
  const defs = (lastStatus && lastStatus.settings && lastStatus.settings.display_field_defs) || [];
  const d = defs.find(x => x.key === k); return d ? d.label : k;
}
function nk19RenderLayoutLists() {
  const nk = NK.last; if (!nk || !nk.settings) return;
  const lay = nk.settings.layout || {};
  const key = JSON.stringify(lay) + JSON.stringify((lastStatus && lastStatus.settings && lastStatus.settings.display_fields) || {});
  if (key === NK19.lastLayoutKey) return; NK19.lastLayoutKey = key;
  const labels = (nk.layout_keys && nk.layout_keys.section_labels) || {};
  const secVis = {summary: "fleet_summary", coins: "coin_odds", charts: "long_history", compare: "fleet_compare", pools: "pool_compare", tuning_log: "tuning_log", logs: "recent_logs"};
  const list = (elId, kind, items, lab, vis) => {
    $(elId).innerHTML = items.map((k, i) => `<div class="nk-lay-row ${vis && vis(k) === false ? "off" : ""}" draggable="true" data-kind="${kind}" data-i="${i}"
        ondragstart="nk19DragStart(event,'${kind}',${i})" ondragover="event.preventDefault();this.classList.add('drag-over')" ondragleave="this.classList.remove('drag-over')" ondrop="nk19Drop(event,'${kind}',${i})">
      <span class="l">${esc(lab(k))}</span>
      <button class="icon-btn" type="button" ${i === 0 ? "disabled" : ""} onclick="nk19Move('${kind}',${i},-1)">&#9650;</button>
      <button class="icon-btn" type="button" ${i === items.length - 1 ? "disabled" : ""} onclick="nk19Move('${kind}',${i},1)">&#9660;</button></div>`).join("");
  };
  list("nkLaySections", "sections", lay.sections || [], k => labels[k] || k, k => secVis[k] ? isFieldVisible(secVis[k]) : (k === "econ" ? (isFieldVisible("profit") || isFieldVisible("block_odds")) : true));
  list("nkLaySummary", "summary_fields", lay.summary_fields || [], k => NK19_SUMMARY_LABELS[k] || k, null);
  list("nkLayCard", "card_fields", lay.card_fields || [], nk19FieldLabel, k => isFieldVisible(k));
}
async function nk19SaveLayout(kind, arr) {
  const patch = {layout: {}}; patch.layout[kind] = arr;
  NK19.lastLayoutKey = "";
  if (NK.last && NK.last.settings) { NK.last.settings.layout[kind] = arr; nk19ApplyLayout(); nk19RenderLayoutLists(); }
  await nkSave(patch, "nkLayoutNote");
}
function nk19Move(kind, i, delta) {
  const arr = [...(nk19Layout()[kind] || [])]; const j = i + delta; if (j < 0 || j >= arr.length) return;
  [arr[i], arr[j]] = [arr[j], arr[i]]; nk19SaveLayout(kind, arr);
}
function nk19DragStart(ev, kind, i) { NK19.layoutDrag = {kind, i}; ev.dataTransfer.effectAllowed = "move"; try { ev.dataTransfer.setData("text/plain", kind + ":" + i); } catch (_) {} }
function nk19Drop(ev, kind, j) {
  ev.preventDefault(); ev.currentTarget.classList.remove("drag-over");
  const d = NK19.layoutDrag; NK19.layoutDrag = null; if (!d || d.kind !== kind || d.i === j) return;
  const arr = [...(nk19Layout()[kind] || [])]; const [x] = arr.splice(d.i, 1); arr.splice(j, 0, x); nk19SaveLayout(kind, arr);
}
async function nk19ResetLayout() {
  try { await post("/api/nk/reset-layout", {}); NK19.lastLayoutKey = ""; NK.settingsFilled = false; nkNote("nkLayoutNote", "Layout reset to default.", "ok"); await refresh(); }
  catch (e) { nkNote("nkLayoutNote", "Could not reset: " + e.message, "err"); }
}

/* ---- card tiles & badges ---- */
function nkCardExtras(d) {
  const nk = lastStatus && lastStatus.nk; if (!nk) return "";
  const ex = (nk.devices || {})[d.name]; if (!ex) return "";
  const badges = [];
  const ci = ex.coin || {};
  if (isFieldVisible("coin_odds") || isFieldVisible("block_odds")) {
    badges.push(ci.coin && ci.coin !== "unknown"
      ? `<span class="nk-badge coin" title="${esc(ci.reason || "")}">${esc(ci.coin)}${ci.confidence === "manual" ? " (manual)" : (ci.confidence === "medium" ? " ?" : "")}</span>`
      : `<span class="nk-badge unknown" title="${esc(ci.reason || "")}">coin: unknown</span>`);
  }
  if (isFieldVisible("firmware_badge") && ex.firmware && ex.firmware.update_available) {
    badges.push(`<a class="nk-badge fw" href="${esc(ex.firmware.url)}" target="_blank" rel="noopener" title="${esc(ex.firmware.source_label)}: installed ${esc(ex.firmware.installed)}">Firmware update: ${esc(ex.firmware.latest)}</a>`);
  }
  if (ex.schedule) {
    const s = ex.schedule; const bits = [];
    if (s.max_freq) bits.push("cap " + s.max_freq + " MHz");
    if (s.bos_power_target_w) bits.push("cap " + s.bos_power_target_w + " W");
    const until = s.until ? (" until " + s.until) : "";
    const paused = (!d.enabled && d.tunable) ? " · autotune paused" : "";
    badges.push(`<span class="nk-badge tou" title="Time-of-use window active">TOU: ${esc((s.windows || []).join(", "))}${bits.length ? " · " + esc(bits.join(", ")) : ""}${esc(until)}${esc(paused)}</span>`);
  } else if (nk.schedule_status && nk.schedule_status.enabled && nk.schedule_status.state === "off_peak") {
    badges.push(`<span class="nk-badge tou offpeak" title="${esc(nk.schedule_status.label || "TOU: off-peak")}">TOU: off-peak</span>`);
  }
  if (ex.quiet) badges.push(`<span class="nk-badge quiet" title="Quiet mode window active">Quiet: fan ≤ ${esc(ex.quiet.fan_max_pct ?? "—")}%${ex.quiet.max_freq ? " · " + esc(ex.quiet.max_freq) + " MHz cap" : ""}</span>`);
  if (isFieldVisible("chip_health") && ex.chip_health && ex.chip_health.wear) {
    const w = ex.chip_health.wear;
    badges.push(`<span class="nk-badge wear" title="Needs ${esc(w.delta_mv)} mV more at ${esc(w.freq)} MHz than in ${esc(w.since)}">Possible chip wear (+${esc(w.delta_mv)} mV @ ${esc(w.freq)} MHz)</span>`);
  }
  if (ex.restart && ex.restart.ts && (Date.now() / 1000 - ex.restart.ts) < 86400) badges.push(`<span class="nk-badge tou" title="${esc(ex.restart.result || "")}">Restart (${esc(ex.restart.kind)}): ${esc(nkWhen(ex.restart.ts))}</span>`);
  return badges.length ? `<div class="nk-badges">${badges.join("")}</div>` : "";
}
function nkCardTiles(d) {
  const nk = lastStatus && lastStatus.nk; if (!nk) return "";
  const ex = (nk.devices || {})[d.name]; if (!ex) return "";
  const cur = (nk.settings && nk.settings.power && nk.settings.power.currency) || "USD";
  const e = ex.econ || {}; const t = [];
  const alt = e.coin && e.coin !== "BTC" && e.coin !== "unknown";
  if (isFieldVisible("profit")) {
    t.push(`<div data-nkf="profit" class="metric"><label>Power cost</label><div class="val">${nkMoney(e.cost_per_day, cur)}<small>/day</small></div><div class="chips">${nkNum(e.kwh_per_day, 2)} kWh/day</div></div>`);
    t.push(`<div data-nkf="profit" class="metric"><label>Est. earned</label>${alt ? `<div class="val" style="font-size:.9rem">—</div><div class="chips">estimates are BTC-only (mining ${esc(e.coin)})</div>` : `<div class="val">${nkNum(e.est_sats_per_day, 0)}<small> sats/day</small></div><div class="chips">${e.est_revenue_per_day != null ? nkMoney(e.est_revenue_per_day, cur) + "/day · " : ""}estimate</div>`}</div>`);
  }
  if (isFieldVisible("block_odds")) {
    t.push(`<div data-nkf="block_odds" class="metric"><label>Block odds${e.coin && e.coin !== "unknown" ? " (" + esc(e.coin) + ")" : ""}</label><div class="val" style="font-size:.95rem">${nkPct(e.chance_day)} /day</div><div class="chips">${nkPct(e.chance_month)} /mo · ${nkPct(e.chance_year)} /yr</div></div>`);
    t.push(`<div data-nkf="block_odds" class="metric"><label>Expected time to block</label><div class="val" style="font-size:.95rem">${nkDur(e.expected_days_to_block)}</div><div class="chips">${e.coin === "unknown" ? "coin unknown: assumes BTC" : "at current difficulty"}</div></div>`);
  }
  if (isFieldVisible("session_best") && d.type === "antminer") {
    const sb = ex.session_best || {};
    const src = sb.source === "pool_switch" ? "since pool switch" : (sb.pool_source === "active_pool" ? "from active pool API" : (sb.source === "api" ? "from miner API (since mining restart)" : (sb.source === "derived" ? "derived from shares since restart" : "waiting for data")));
    t.push(`<div data-nkf="session_best" class="metric"><label>Best share (session / all-time)</label><div class="val" style="font-size:.95rem">${esc(fmtDiff(sb.session_best))} <small>/ ${esc(fmtDiff(sb.all_time_best))}</small></div>
      <div class="chips" title="All-time = highest seen by Miner Hearth since ${esc(nkWhen(sb.tracking_since))}">Session since ${esc(sb.session_start_ts ? nkWhen(sb.session_start_ts) : "—")} · ${esc(src)}</div></div>`);
  }
  if (isFieldVisible("pool_health")) {
    const p = ex.pool_health;
    if (p) {
      const rp = p.reject_pct; const cls = rp == null ? "" : (rp >= 5 ? "nk-bad" : (rp >= 1 ? "nk-warn" : "nk-good"));
      const lat = p.ping_ms != null ? `ping ${Number(p.ping_ms).toFixed(0)} ms` : "";
      const loss = p.ping_loss != null ? `loss ${(Number(p.ping_loss) * (p.ping_loss <= 1 ? 100 : 1)).toFixed(1)}%` : "";
      t.push(`<div data-nkf="pool_health" class="metric"><label>Pool health (${esc(String(p.win_total || 0))} recent shares)</label><div class="val ${cls}" style="font-size:.95rem">${rp == null ? "—" : rp.toFixed(2) + "% rej+stale"}</div>
        <div class="chips">Rej ${esc(nkNum(p.shares_rejected))} · Stale ${esc(nkNum(p.shares_stale))}${lat ? " · " + esc(lat) : ""}${loss ? " · " + esc(loss) : ""}</div></div>`);
    }
  }
  if (isFieldVisible("ambient_temp") && ex.ambient) {
    t.push(`<div data-nkf="ambient_temp" class="metric"><label>${ex.ambient.source === "board inlet sensor" ? "Intake (board inlet)" : "Ambient / intake"}</label><div class="val temp">${esc(nkT(ex.ambient.c, 1))}</div><div class="chips">${esc(ex.ambient.source || "miner")}</div></div>`);
  }
  if (isFieldVisible("chip_health") && ex.chip_health && d.type !== "antminer") {
    const ch = ex.chip_health; const l = ch.latest;
    t.push(`<div data-nkf="chip_health" class="metric"><label>Best stable (chip health)</label><div class="val" style="font-size:.95rem">${l ? esc(l[1]) + " MHz @ " + esc(l[2]) + " mV" : "—"}</div>
      <div class="chips">${l ? "on " + esc(l[0]) : "logged after " + esc(String((((NK.last || {}).settings || {}).chip_health || {}).stable_min || 30)) + " stable min"}${ch.wear ? ` · <span class="nk-warn">wear +${esc(ch.wear.delta_mv)} mV</span>` : ""}</div></div>`);
  }
  return t.join("");
}

/* ---- per-coin odds ---- */
function nk19RenderCoins(nk) {
  const el = $("nkCoins"); if (!el) return;
  if (!isFieldVisible("coin_odds")) { el.hidden = true; return; }
  const groups = nk.coins || []; el.hidden = !groups.length; if (!groups.length) return;
  $("nkCoinsBody").innerHTML = groups.map(g => {
    if (g.coin === "unknown") return `<div class="nk-coin"><h4><span>Unknown coin</span><span class="nk-tiny">${g.devices.length} machine(s)</span></h4>
      <div class="tiny">${esc(g.devices.join(", "))}</div><div class="tiny">${esc(fmtHashrate(g.hashrate_ghs))} · ${esc(g.note || "")}</div></div>`;
    const o = g.odds || {};
    return `<div class="nk-coin"><h4><span>${esc(g.label)} (${esc(g.coin)})</span><span class="nk-tiny">${g.devices.length} machine(s)</span></h4>
      <div class="big">${o.chance_day != null ? nkPct(o.chance_day) + " / day" : "—"}</div>
      <div class="tiny">${o.chance_month != null ? nkPct(o.chance_month) + " / month · " + nkPct(o.chance_year) + " / year" : (g.error ? "chain data: " + esc(g.error) : "waiting for chain data…")}</div>
      <div class="tiny">Expected time to block: <b>${nkDur(o.expected_days_to_block)}</b></div>
      <div class="tiny">${esc(fmtHashrate(g.hashrate_ghs))} · difficulty ${g.difficulty ? nkBig(g.difficulty) : "—"}${g.height ? " · height " + nkNum(g.height) : ""}${g.source ? " · via " + esc(g.source) : ""}</div>
      <div class="tiny">${esc(g.devices.join(", "))}${g.confirm_ready ? ((g.pool_user_devices && g.pool_user_devices.length) ? " · payout check via pool username" : "") : " · <span class='nk-warn'>no payout address → blocks can't be confirmed</span>"}${(g.pool_account_ignored_devices && g.pool_account_ignored_devices.length) ? " · <span class='nk-warn'>pool account name ignored (" + esc(g.pool_account_ignored_devices.join(", ")) + ")</span>" : ""}</div></div>`;
  }).join("");
}

/* ---- fleet comparison ---- */
function nk19CompareRows(data) {
  const nk = data.nk || {};
  return (data.devices || []).map(d => {
    const ex = (nk.devices || {})[d.name] || {}; const p = ex.pool_health || {};
    const err = d.rolling_error_pct ?? d.error_pct ?? d.pool_rejected_pct;
    return {name: d.name, coin: (ex.coin || {}).coin || "unknown", online: !!d.online, hashrate: d.online ? d.hashrate_ghs : null,
            eff: d.online ? d.j_per_th : null, power: d.online ? d.power_w : null, err: err, rej: p.reject_pct,
            temp: d.asic_temp_c, uptime: d.uptime_sec, ambient: ex.ambient ? ex.ambient.c : null};
  });
}
function nk19Sort(key) { if (NK19.sort.key === key) NK19.sort.asc = !NK19.sort.asc; else NK19.sort = {key, asc: key === "name" || key === "eff" || key === "err" || key === "rej" || key === "temp"}; if (lastStatus) nk19RenderCompare(lastStatus); }
function nk19RenderCompare(data) {
  const el = $("nkCompare"); if (!el) return;
  if (!isFieldVisible("fleet_compare") || !(data.devices || []).length) { el.hidden = true; return; }
  el.hidden = false;
  const rows = nk19CompareRows(data); const {key, asc} = NK19.sort;
  rows.sort((a, b) => { const x = a[key], y = b[key]; if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
    const r = typeof x === "string" ? x.localeCompare(y) : x - y; return asc ? r : -r; });
  const cols = [["name", "Machine"], ["coin", "Coin"], ["hashrate", "Hashrate"], ["eff", "J/TH"], ["power", "Power"], ["err", "Error %"], ["rej", "Pool rej %"], ["temp", "Chip temp"], ["ambient", "Ambient"], ["uptime", "Uptime"]];
  const best = {};
  for (const [k, lo] of [["hashrate", false], ["eff", true], ["err", true], ["rej", true], ["temp", true]]) {
    const vals = rows.map(r => r[k]).filter(v => v != null); if (vals.length > 1) best[k] = lo ? Math.min(...vals) : Math.max(...vals);
  }
  const cell = (r, k) => {
    const v = r[k]; const star = best[k] != null && v === best[k] ? ' class="nk-good"' : "";
    let s = "—";
    if (v != null) s = k === "hashrate" ? fmtHashrate(v) : k === "eff" ? Number(v).toFixed(1) : k === "power" ? Math.round(v) + " W"
      : (k === "err" || k === "rej") ? Number(v).toFixed(2) + "%" : (k === "temp" || k === "ambient") ? nkT(v, 1) : k === "uptime" ? fmtUptime(v) : String(v);
    return `<td${star}>${esc(s)}</td>`;
  };
  $("nkCompareTable").innerHTML = `<thead><tr>${cols.map(([k, l]) => `<th class="${key === k ? "sorted" + (asc ? " asc" : "") : ""}" onclick="nk19Sort('${k}')">${esc(l)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(r => `<tr class="${r.online ? "" : "nk-tiny"}"><td><b>${esc(r.name)}</b>${r.online ? "" : " (offline)"}</td>${cols.slice(1).map(([k]) => cell(r, k)).join("")}</tr>`).join("")}</tbody>`;
}

/* ---- pools ---- */
function nk19RenderPools(nk) {
  const el = $("nkPools"); if (!el) return;
  const pools = nk.pools || [];
  if (!isFieldVisible("pool_compare") || !pools.length) { el.hidden = true; return; }
  el.hidden = false;
  const f = (v, d, u) => v == null ? "—" : Number(v).toFixed(d) + (u || "");
  $("nkPoolsTable").innerHTML = `<thead><tr><th>Pool</th><th>Machines</th><th>Accepted 24h</th><th>Rej+stale 24h</th><th>Rej % 7d</th><th>Latency 24h</th><th>Miner ping</th><th>Uptime 24h</th><th>Uptime 7d</th></tr></thead>
    <tbody>${pools.map(p => `<tr><td><b>${esc(p.pool)}</b></td><td>${esc((p.devices || []).join(", ") || "—")}</td>
      <td>${nkNum(p["24h"].accepted)}</td><td>${nkNum((p["24h"].rejected || 0) + (p["24h"].stale || 0))}${p["24h"].reject_pct != null ? " (" + f(p["24h"].reject_pct, 2, "%") + ")" : ""}</td>
      <td>${f(p["7d"].reject_pct, 2, "%")}</td><td>${f(p["24h"].latency_ms, 0, " ms")}</td><td>${f(p["24h"].miner_ping_ms, 0, " ms")}</td>
      <td>${f(p["24h"].uptime_pct, 1, "%")}</td><td>${f(p["7d"].uptime_pct, 1, "%")}</td></tr>`).join("")}</tbody>`;
}

/* ---- settings ---- */
function nk19Fill(nk) {
  const s = nk.settings; if (!s || !s.ui) return;
  $("nk_ui_theme").value = s.ui.theme; $("nk_ui_unit").value = s.ui.temp_unit;
  const devs = ((lastStatus && lastStatus.devices) || []).map(d => d.name);
  const coinOpts = (sel) => `<option value="">Auto-detect</option>` + (nk.coin_list || []).map(c => `<option value="${esc(c.id)}" ${c.id === sel ? "selected" : ""}>${esc(c.label)} (${esc(c.id)})</option>`).join("");
  $("nkCoinMachines").innerHTML = devs.length ? devs.map(n => {
    const ci = ((nk.devices || {})[n] || {}).coin || {};
    return `<label class="nk-row"><b style="min-width:110px">${esc(n)}</b><select data-dev="${esc(n)}" class="nk-coin-ov">${coinOpts((s.coins.overrides || {})[n] || "")}</select>
      <span class="nk-tiny">detected: ${esc(ci.coin || "—")}${ci.reason ? " — " + esc(ci.reason) : ""}</span></label>`; }).join("") : "<div class='sub'>No machines yet.</div>";
  $("nkCoinAddrs").innerHTML = (nk.coin_list || []).filter(c => c.id !== "BTC").map(c => `<label class="nk-row" style="align-items:flex-start"><span class="nk-coinlbl">${esc(c.label)} payout address(es)</span>
      <textarea rows="1" data-coin="${esc(c.id)}" class="nk-coin-addr" style="flex:1;min-width:180px" placeholder="optional, one per line">${esc(((s.coins.payout_addresses || {})[c.id] || []).join("\n"))}</textarea></label>`).join("");
  $("nk_c_fetch").checked = !!s.coins.fetch_chains;
  const pr = s.alerts.pool_rejects;
  $("nk_pr_en").checked = pr.enabled; $("nk_pr_pct").value = pr.pct; $("nk_pr_win").value = pr.window_min; $("nk_pr_min").value = pr.min_shares;
  $("nk_pp_en").checked = s.pools.probe_enabled;
  $("nk_ch_en").checked = s.chip_health.enabled; $("nk_ch_stable").value = s.chip_health.stable_min; $("nk_ch_mv").value = s.chip_health.wear_mv; $("nk_ch_days").value = s.chip_health.min_days;
  $("nk_cw_en").checked = s.alerts.chip_wear.enabled;
  $("nk_q_enabled").checked = s.quiet.enabled; NK19.quiet = JSON.parse(JSON.stringify(s.quiet.windows || [])); nk19RenderQuiet();
  const r = s.restarts;
  $("nk_r_day").innerHTML = NK_DAYS.map((d, i) => `<option value="${i}" ${i === r.weekly.day ? "selected" : ""}>${d}</option>`).join("");
  $("nk_r_week").checked = r.weekly.enabled; $("nk_r_time").value = r.weekly.time; $("nk_r_hung").checked = r.hung.enabled; $("nk_r_hung_min").value = r.hung.minutes;
  $("nk_r_max").value = r.max_per_day; $("nk_r_gap").value = r.min_gap_min; $("nk_r_devs").value = (r.devices || []).join(", ");
  const w = s.webhooks; const remote = !!(lastStatus && lastStatus.access && lastStatus.access.remote);
  const sec = (id, k) => { const el = $(id); el.value = w[k] || ""; el.placeholder = (remote && w["has_" + k]) ? "(saved — leave blank to keep)" : (el.placeholder || ""); el.dataset.has = w["has_" + k] ? "1" : ""; };
  $("nk_w_en").checked = w.enabled; sec("nk_w_discord", "discord_url"); sec("nk_w_tg_token", "telegram_token"); sec("nk_w_ntfy", "ntfy_url"); sec("nk_w_ntfy_tok", "ntfy_token");
  $("nk_w_tg_chat").value = w.telegram_chat_id || "";
  $("nkWebhookTypes").innerHTML = Object.keys(w.types || {}).map(k => `<label class="field-check"><input type="checkbox" data-wt="${esc(k)}" ${w.types[k] ? "checked" : ""}><span>${esc(NK19_WEBHOOK_LABELS[k] || k)}</span></label>`).join("");
  NK19.lastLayoutKey = ""; nk19RenderLayoutLists();
}
function nk19SaveUi() { nkSave({ui: {theme: $("nk_ui_theme").value, temp_unit: $("nk_ui_unit").value}}, "nkLayoutNote"); nk19ApplyUi({theme: $("nk_ui_theme").value, temp_unit: $("nk_ui_unit").value}); }
function nk19SaveCoins() {
  const ov = {}; document.querySelectorAll(".nk-coin-ov").forEach(sel => { ov[sel.dataset.dev] = sel.value; });
  const pa = {}; document.querySelectorAll(".nk-coin-addr").forEach(t => { pa[t.dataset.coin] = t.value.split(/[\s,;]+/).map(x => x.trim()).filter(Boolean); });
  nkSave({coins: {overrides: ov, payout_addresses: pa, fetch_chains: $("nk_c_fetch").checked}}, "nkCoinsSetNote");
}
function nk19SaveHealth() {
  nkSave({alerts: {pool_rejects: {enabled: $("nk_pr_en").checked, pct: nkN("nk_pr_pct"), window_min: nkN("nk_pr_win"), min_shares: nkN("nk_pr_min")}, chip_wear: {enabled: $("nk_cw_en").checked}},
          pools: {probe_enabled: $("nk_pp_en").checked},
          chip_health: {enabled: $("nk_ch_en").checked, stable_min: nkN("nk_ch_stable"), wear_mv: nkN("nk_ch_mv"), min_days: nkN("nk_ch_days")}}, "nkHealthNote");
}
function nk19RenderQuiet() {
  $("nkQuietWins").innerHTML = NK19.quiet.map((w, i) => `<div class="nk-win">
    <div class="row"><input type="text" value="${esc(w.name)}" onchange="NK19.quiet[${i}].name=this.value" style="flex:1;min-width:120px">
      <label><input type="checkbox" ${w.enabled ? "checked" : ""} onchange="NK19.quiet[${i}].enabled=this.checked"> on</label>
      <button class="icon-btn" type="button" onclick="NK19.quiet.splice(${i},1);nk19RenderQuiet()">Remove</button></div>
    <div class="row nk-days">${NK_DAYS.map((dn, di) => `<label><input type="checkbox" ${(w.days || []).includes(di) ? "checked" : ""} onchange="nk19QuietDay(${i},${di},this.checked)">${dn}</label>`).join("")}</div>
    <div class="row"><label>From</label><input type="time" value="${esc(w.start)}" onchange="NK19.quiet[${i}].start=this.value"><label>to</label><input type="time" value="${esc(w.end)}" onchange="NK19.quiet[${i}].end=this.value"></div>
    <div class="row"><label>Fan max</label><input type="number" min="20" max="100" step="5" value="${w.fan_max_pct ?? 50}" onchange="NK19.quiet[${i}].fan_max_pct=Number(this.value)"> %
      <label>Freq cap</label><input type="number" min="100" max="850" step="25" placeholder="—" value="${w.axe_max_freq_mhz ?? ""}" onchange="NK19.quiet[${i}].axe_max_freq_mhz=this.value===''?null:Number(this.value)"> MHz</div>
    <div class="row"><label>Machines</label><input type="text" placeholder="Leave blank for all machines" value="${esc((w.devices || []).join(", "))}" onchange="NK19.quiet[${i}].devices=(typeof nkParseDeviceFilter==='function'?nkParseDeviceFilter(this.value):this.value.split(',').map(x=>x.trim()).filter(Boolean))" style="flex:1;min-width:140px"></div>
  </div>`).join("") || "<div class='sub'>No quiet windows yet.</div>";
}
function nk19QuietDay(i, di, on) { const d = new Set(NK19.quiet[i].days || []); on ? d.add(di) : d.delete(di); NK19.quiet[i].days = [...d].sort(); }
function nk19AddQuiet() { NK19.quiet.push({name: "Night quiet", enabled: true, days: [0, 1, 2, 3, 4, 5, 6], start: "22:00", end: "07:00", devices: [], fan_max_pct: 50, axe_max_freq_mhz: null}); nk19RenderQuiet(); }
function nk19SaveQuiet() { nkSave({quiet: {enabled: $("nk_q_enabled").checked, windows: NK19.quiet}}, "nkQuietNote"); }
function nk19SaveRestarts() {
  nkSave({restarts: {weekly: {enabled: $("nk_r_week").checked, day: Number($("nk_r_day").value), time: $("nk_r_time").value || "04:00"},
                     hung: {enabled: $("nk_r_hung").checked, minutes: nkN("nk_r_hung_min")}, max_per_day: nkN("nk_r_max"), min_gap_min: nkN("nk_r_gap"),
                     devices: $("nk_r_devs").value.split(",").map(x => x.trim()).filter(Boolean)}}, "nkRestartNote");
}
function nk19TrimUrl(s) {
  return String(s || "").replace(/[\r\n\t]+/g, "").trim();
}
function nk19ValidDiscordUrl(url) {
  const u = nk19TrimUrl(url);
  if (!u || u === "__keep__") return {ok: true, url: u};
  const low = u.toLowerCase();
  if (low.startsWith("https://discord.com/api/webhooks/") || low.startsWith("https://discordapp.com/api/webhooks/")) {
    return {ok: true, url: u};
  }
  return {
    ok: false,
    url: u,
    error: "Discord webhook URL must start with https://discord.com/api/webhooks/ or https://discordapp.com/api/webhooks/ (remove extra spaces or check the pasted URL)."
  };
}
function nk19HookPatch() {
  const v = (id, isUrl) => {
    const el = $(id);
    if (el.value === "" && el.dataset.has && lastStatus && lastStatus.access && lastStatus.access.remote) return "__keep__";
    const val = isUrl ? nk19TrimUrl(el.value) : el.value.trim();
    if (isUrl && el.value !== val) el.value = val;
    return val;
  };
  const types = {}; document.querySelectorAll("#nkWebhookTypes input[data-wt]").forEach(c => { types[c.dataset.wt] = c.checked; });
  return {webhooks: {enabled: $("nk_w_en").checked, discord_url: v("nk_w_discord", true), telegram_token: v("nk_w_tg_token", false), telegram_chat_id: $("nk_w_tg_chat").value.trim(),
                     ntfy_url: v("nk_w_ntfy", true), ntfy_token: v("nk_w_ntfy_tok", false), types}};
}
function nk19SaveHooks() {
  const patch = nk19HookPatch();
  const chk = nk19ValidDiscordUrl(patch.webhooks.discord_url);
  if (!chk.ok) { nkNote("nkHooksNote", chk.error, "err"); return; }
  nkSave(patch, "nkHooksNote");
}
async function nk19TestHooks() {
  try {
    const patch = nk19HookPatch();
    const chk = nk19ValidDiscordUrl(patch.webhooks.discord_url);
    if (!chk.ok) { nkNote("nkHooksNote", chk.error, "err"); return; }
    await post("/api/nk/settings", {settings: patch});
    const r = await post("/api/nk/test-webhook", {});
    nkNote("nkHooksNote", (r.results || []).map(x => `${x.service}: ${x.ok ? "sent" : "failed (" + x.error + ")"}`).join(" · ") || "Nothing configured.", r.ok ? "ok" : "err");
  } catch (e) { nkNote("nkHooksNote", "Test failed: " + e.message, "err"); }
}
function nk19ApplyUi(ui) {
  if (!ui) return;
  document.body.classList.toggle("theme-light", ui.theme === "light");
  document.body.classList.toggle("theme-contrast", ui.theme === "contrast");
  if (NK19.unit !== ui.temp_unit) { NK19.unit = ui.temp_unit; NK.histAt = 0; }
  try { localStorage.setItem("nk_ui", JSON.stringify(ui)); } catch (_) {}
}
(function () { try { const ui = JSON.parse(localStorage.getItem("nk_ui") || "null"); if (ui) nk19ApplyUi(ui); } catch (_) {} })();

function nk19Render(data) {
  const nk = data.nk; if (!nk) return;
  nk19ApplyUi(nk.settings && nk.settings.ui);
  nk19RenderCoins(nk); nk19RenderCompare(data); nk19RenderPools(nk);
  nk19RenderLayoutLists();
  nk19ApplyLayout();
}
"""


def inject19(html: str) -> str:
    """Apply the v1.9 UI on top of the v1.8-injected dashboard HTML."""
    def rep(old: str, new: str, count: int = 1) -> None:
        nonlocal html
        if old not in html:
            raise RuntimeError(f"v1.9 dashboard injection point missing: {old[:70]!r}")
        html = html.replace(old, new, count)

    def resub(pattern: str, repl: str, min_count: int = 1) -> None:
        nonlocal html
        html, n = re.subn(pattern, repl, html)
        if n < min_count:
            raise RuntimeError(f"v1.9 dashboard pattern matched {n} times (< {min_count}): {pattern[:70]!r}")

    rep("</style>", NK19_CSS + "</style>")
    rep('  <div class="grid" id="cards"></div>', NK19_PANELS_HTML + '  <div class="grid" id="cards"></div>')
    rep('      <div class="settings-box nk-set" id="nkSetAlerts">', NK19_SETTINGS_HTML + '      <div class="settings-box nk-set" id="nkSetAlerts">')
    # Tag card tiles / panels with their display-field key so the layout can order them.
    resub(r'isFieldVisible\("(\w+)"\) \? `<div class="', r'isFieldVisible("\1") ? `<div data-nkf="\1" class="', 10)
    rep('(isFieldVisible("vr_temp") || isFieldVisible("fans")) ? `<div class="metric"', '(isFieldVisible("vr_temp") || isFieldVisible("fans")) ? `<div data-nkf="vr_temp" class="metric"')
    resub(r'isFieldVisible\("(\w+)"\) \? ((?:render\w+\(d\))|\w+Html) : ""', r'isFieldVisible("\1") ? nkTag(\2, "\1") : ""', 4)
    rep("${renderOverridesSection(d)}", '${nkTag(renderOverridesSection(d), "manual_control")}')
    # Extra tiles live inside the metrics grid; badges right under the card header (both card types).
    rep('<div class="metrics">${metricsTunable}</div>', '<div class="metrics">${metricsTunable}${typeof nkCardTiles === "function" ? nkCardTiles(d) : ""}</div>')
    rep('<div class="metrics">${metricsAnt}</div>', '<div class="metrics">${metricsAnt}${typeof nkCardTiles === "function" ? nkCardTiles(d) : ""}</div>\n      ${typeof nkCardExtras === "function" ? nkCardExtras(d) : ""}')
    # Summary bar items
    for key, label in (("online", "Machines Online"), ("hashrate", "Total Hashrate"), ("power", "Total Power"), ("efficiency", "Fleet Efficiency"),
                       ("hottest_chip", "Hottest Chip"), ("hottest_vr", "Hottest VR"), ("best_share", "Best Share Ever")):
        resub(r'<div class="fleet-stat"><label>' + re.escape(label) + '</label>', f'<div class="fleet-stat" data-nkf="{key}"><label>{label}</label>', 1)
    # Temperatures in °F when chosen (display only)
    resub(r'\$\{Number\(([\w.\[\]]+)\)\.toFixed\((\d)\)\}°C', r'${nkT(\1, \2)}', 4)
    rep('${Math.min(...tVals).toFixed(0)}–${Math.max(...tVals).toFixed(0)}°C', '${nkTR(Math.min(...tVals), Math.max(...tVals))}')
    resub(r'Number\(t\)\.toFixed\(0\) \+ "°"', 'nkTn(t, 0) + "°"', 2)
    rep('$("nkHistTRange").textContent = tl.toFixed(0) + "–" + th.toFixed(0) + " °C";', '$("nkHistTRange").textContent = nkTR(tl, th);')
    rep('const [tl, th] = nkRange(pts, [2, 3]);', 'const tpts = nkUnitF() ? pts.map(p => [p[0], p[1], p[2] == null ? null : p[2] * 9 / 5 + 32, p[3] == null ? null : p[3] * 9 / 5 + 32]) : pts;\n    const [tl0, th0] = nkRange(tpts, [2, 3]); const tl = nkUnitF() ? (tl0 - 32) * 5 / 9 : tl0, th = nkUnitF() ? (th0 - 32) * 5 / 9 : th0;')
    rep('<path d="${nkPath(pts, 2, tl, th)}"', '<path d="${nkPath(tpts, 2, tl0, th0)}"')
    rep('<path d="${nkPath(pts, 3, tl, th)}"', '<path d="${nkPath(tpts, 3, tl0, th0)}"')
    # 24-hour average line on the hashrate chart (fleet total or one machine)
    rep('$("nkHistHrRange").textContent = fmtHashrate(Math.max(0, hl)) + " – " + fmtHashrate(hh);',
        '''const tEnd = pts[pts.length - 1][0]; const last24 = pts.filter(p => p[0] >= tEnd - 86400 && p[1] != null).map(p => p[1]);
    const avg24 = last24.length ? last24.reduce((a, b) => a + b, 0) / last24.length : null;
    if (avg24 != null) { const y = (145 - ((avg24 - hl) / ((hh - hl) || 1)) * 140).toFixed(1);
      $("nkHistHr").insertAdjacentHTML("beforeend", `<line x1="0" x2="1000" y1="${y}" y2="${y}" stroke="#f5c542" stroke-width="1.5" stroke-dasharray="6 5" vector-effect="non-scaling-stroke"/>`); }
    $("nkHistHrRange").innerHTML = esc(fmtHashrate(Math.max(0, hl)) + " – " + fmtHashrate(hh)) + (avg24 != null ? ` · <span class="nk-avg-label">${NK.histDevice === "__fleet__" ? "fleet " : ""}24h avg ${esc(fmtHashrate(avg24))}</span>` : "");''')
    rep('return hot ? `<b>${Number(t).toFixed(1)}°</b>` : `${Number(t).toFixed(1)}°`;', 'return hot ? `<b>${nkTn(t, 1)}°</b>` : `${nkTn(t, 1)}°`;')
    # Hooks
    rep("function render(data) {\n  lastStatus = data;", "function render(data) {\n  lastStatus = data;\n  if (data && data.nk) NK.last = data.nk;")
    rep("  NK.settingsFilled = true;\n}", '  if (typeof nk19Fill === "function") nk19Fill(nk);\n  NK.settingsFilled = true;\n}')
    rep('  if (typeof nkRender === "function") nkRender(data);', '  if (typeof nkRender === "function") nkRender(data);\n  if (typeof nk19Render === "function") nk19Render(data);')
    rep("\nrefresh();\nsetInterval(refresh, 3000);\n</script>", NK19_JS + "\nrefresh();\nsetInterval(refresh, 3000);\n</script>")
    return html
