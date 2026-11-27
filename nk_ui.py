"""Miner Hearth v1.8 dashboard additions (CSS / HTML / JS injected into dashboard.DASHBOARD_HTML)."""

from miner_hearth import DONATE_ADDRESS

NK_CSS = r"""
  .nk-donate { display: inline-flex; align-items: center; gap: 6px; font-size: .74rem; font-weight: 500; color: var(--muted); letter-spacing: 0; flex-wrap: wrap; }
  .nk-donate code { color: var(--text); background: var(--bg-soft); border: 1px solid var(--border); border-radius: 6px; padding: 2px 6px; font-size: .72rem; user-select: all; }
  .nk-donate .nk-copied { color: var(--ok); font-weight: 700; opacity: 0; transition: opacity .2s; }
  .nk-donate .nk-copied.show { opacity: 1; }
  .nk-donate .nk-copied.fail { color: var(--danger); }
  .nk-netbar { margin: 10px 24px 0; display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: .8rem; color: var(--muted); }
  .nk-netbar b { color: var(--text); font-variant-numeric: tabular-nums; }
  .nk-netbar .nk-up { color: var(--warn); } .nk-netbar .nk-down { color: var(--ok); }
  .nk-banners { margin: 10px 24px 0; display: flex; flex-direction: column; gap: 8px; }
  .nk-banner { display: flex; gap: 10px; align-items: flex-start; padding: 10px 14px; border-radius: 10px; border: 1px solid var(--border); background: var(--bg-card); box-shadow: var(--shadow); }
  .nk-banner.problem { border-color: var(--danger); background: rgba(255,107,107,.08); }
  .nk-banner.info { border-color: var(--info); background: rgba(108,184,255,.07); }
  .nk-banner.block { border-color: #f5c542; background: linear-gradient(90deg, rgba(245,197,66,.22), rgba(62,224,180,.12)); animation: nkGlow 1.6s ease-in-out infinite alternate; }
  @keyframes nkGlow { from { box-shadow: 0 0 0 rgba(245,197,66,.0); } to { box-shadow: 0 0 22px rgba(245,197,66,.45); } }
  .nk-banner .t { font-weight: 700; } .nk-banner .m { font-size: .84rem; color: var(--muted); }
  .nk-banner .x { margin-left: auto; }
  .nk-econ { margin: 10px 24px 0; background: var(--bg-card); border: 1px solid var(--border); border-radius: 14px; padding: 10px 18px; display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px 16px; }
  .nk-econ label { display:block; color: var(--muted); font-size: .7rem; text-transform: uppercase; letter-spacing: .06em; }
  .nk-econ .val { font-weight: 650; font-variant-numeric: tabular-nums; }
  .nk-econ .tiny, .nk-tiny { font-size: .7rem; color: var(--muted); }
  .nk-extras { margin-top: 10px; display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .nk-extras .metric { background: var(--bg-soft); border: 1px solid var(--border); border-radius: 10px; padding: 8px 10px; }
  .nk-badges { display: flex; flex-wrap: wrap; gap: 6px; grid-column: 1 / -1; }
  .nk-badge { font-size: .72rem; padding: 3px 9px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); max-width: 100%; overflow-wrap: anywhere; line-height: 1.25; }
  .nk-set .row-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 8px; }
  .nk-badge.fw { border-color: var(--warn); color: var(--warn); text-decoration: none; }
  .nk-badge.tou { border-color: var(--info); color: var(--info); }
  .nk-badge.tou.offpeak { border-color: var(--border); color: var(--muted); }
  .nk-toubar { justify-content: space-between; border-color: rgba(108, 184, 255, .35); }
  .nk-toubar.active { border-color: var(--info); color: var(--text); }
  .nk-toubar.offpeak { border-color: var(--border); color: var(--muted); }
  .nk-toast { position: fixed; right: 20px; bottom: 20px; z-index: 9999; padding: 10px 14px; border-radius: 10px;
    font-size: .84rem; font-weight: 600; background: var(--bg-card); color: var(--text); border: 1px solid var(--ok);
    box-shadow: 0 8px 24px rgba(0, 0, 0, .45); opacity: 0; pointer-events: none; transform: translateY(8px);
    transition: opacity .18s ease, transform .18s ease; max-width: min(440px, calc(100vw - 40px)); }
  .nk-toast.show { opacity: 1; transform: translateY(0); }
  .nk-toast.err { border-color: var(--danger); color: var(--danger); }
  .nk-toast.warn { border-color: var(--warn); color: var(--warn); }
  .nk-panel { margin: 16px 24px 0; background: var(--bg-card); border: 1px solid var(--border); border-radius: 14px; padding: 12px 16px; }
  .nk-panel h2 { margin: 0; font-size: .85rem; color: var(--muted); font-weight: 650; letter-spacing: .06em; text-transform: uppercase; }
  .nk-panel .bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px; }
  .nk-panel select, .nk-set select, .nk-set input[type=text], .nk-set input[type=number], .nk-set input[type=password], .nk-set textarea, .nk-set input[type=time] {
    background: var(--bg-soft); color: var(--text); border: 1px solid var(--border); border-radius: 7px; padding: 5px 7px; font: inherit; font-size: .82rem; }
  .nk-set textarea { width: 100%; min-height: 54px; resize: vertical; }
  .nk-set .row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; margin: 6px 0; font-size: .82rem; }
  .nk-set .row label { color: var(--muted); }
  .nk-set input[type=number] { width: 86px; }
  .nk-seg button.on { border-color: var(--accent); color: var(--accent); }
  .nk-chart { width: 100%; height: 150px; display: block; background: var(--bg-soft); border-radius: 10px; border: 1px solid var(--border); }
  .nk-chart-label { font-size: .72rem; color: var(--muted); margin: 6px 0 2px; display: flex; justify-content: space-between; }
  .nk-tlog { max-height: 320px; overflow: auto; font-size: .8rem; }
  .nk-tlog .e { display: grid; grid-template-columns: 150px 140px 1fr; gap: 8px; padding: 5px 0; border-bottom: 1px solid var(--border); }
  .nk-tlog .e .k { font-weight: 650; }
  .nk-tlog .src { font-size: .68rem; padding: 1px 6px; border-radius: 999px; border: 1px solid var(--border); margin-left: 4px; color: var(--muted); }
  .nk-tlog .src.emergency { color: var(--danger); border-color: var(--danger); }
  .nk-tlog .src.schedule { color: var(--info); border-color: var(--info); }
  .nk-win { border: 1px solid var(--border); border-radius: 8px; padding: 8px; margin: 6px 0; background: var(--bg-card); }
  .nk-days label { font-size: .74rem; margin-right: 4px; }
  .nk-ro-badge { display: none; }
  body.nk-readonly .nk-ro-badge { display: inline-flex; }
  body.nk-readonly .btn, body.nk-readonly .add-btn, body.nk-readonly .icon-btn:not(.nk-ro-ok), body.nk-readonly .toggle:not(.nk-ro-ok) { pointer-events: none; opacity: .4; }
  @media (max-width: 720px) {
    header { padding: 12px 12px 10px; gap: 8px; }
    h1 { font-size: 1.05rem; flex-wrap: wrap; }
    .nk-donate code { font-size: .64rem; word-break: break-all; }
    .grid { grid-template-columns: 1fr; padding: 12px; gap: 12px; }
    .fleet-bar, .nk-econ { margin-left: 12px; margin-right: 12px; grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 10px 12px; }
    .nk-netbar, .nk-banners, .nk-panel { margin-left: 12px; margin-right: 12px; }
    .settings-cols { grid-template-columns: 1fr; }
    .caps-grid, .bos-grid, .nk-extras { grid-template-columns: 1fr; }
    .nk-tlog .e { grid-template-columns: 1fr; gap: 2px; }
    .logs, footer { padding-left: 12px; padding-right: 12px; }
  }
"""

NK_DONATE_HTML = (
    '<span class="nk-donate" id="nkDonate">Donate BTC: <code id="nkDonateAddr">' + DONATE_ADDRESS + '</code>'
    '<button class="icon-btn nk-ro-ok" id="nkDonateCopy" type="button" onclick="nkCopyDonate()">Copy</button>'
    '<span class="nk-copied" id="nkCopied" role="status" aria-live="polite">Copied</span></span>'
)

NK_TOP_HTML = r"""
  <section class="nk-netbar" id="nkNetBar" hidden></section>
  <section class="nk-netbar nk-toubar" id="nkTouBar" hidden></section>
  <div class="nk-banners" id="nkBanners"></div>
"""

NK_ECON_HTML = r"""
  <section class="nk-econ" id="nkEcon" hidden></section>
"""

NK_PANELS_HTML = r"""
  <section class="nk-panel" id="nkHistoryPanel" hidden>
    <div class="bar">
      <h2>History</h2>
      <select id="nkHistDevice" onchange="nkSetHist(null, this.value)"></select>
      <span class="nk-seg" id="nkHistSeg">
        <button class="icon-btn nk-ro-ok" type="button" data-r="24h" onclick="nkSetHist('24h')">24h</button>
        <button class="icon-btn nk-ro-ok" type="button" data-r="7d" onclick="nkSetHist('7d')">7 days</button>
        <button class="icon-btn nk-ro-ok" type="button" data-r="30d" onclick="nkSetHist('30d')">30 days</button>
      </span>
      <div class="spacer"></div>
      <button class="icon-btn nk-ro-ok" type="button" onclick="nkExportCsv()">Export CSV</button>
    </div>
    <div class="nk-chart-label"><span>Hashrate</span><span id="nkHistHrRange"></span></div>
    <svg class="nk-chart" id="nkHistHr" viewBox="0 0 1000 150" preserveAspectRatio="none"></svg>
    <div class="nk-chart-label"><span>Temperatures (<span style="color:#ff9f43">ASIC</span> / <span style="color:#6cb8ff">VR</span>)</span><span id="nkHistTRange"></span></div>
    <svg class="nk-chart" id="nkHistT" viewBox="0 0 1000 150" preserveAspectRatio="none"></svg>
    <div class="nk-chart-label"><span id="nkHistStart"></span><span id="nkHistInfo"></span><span id="nkHistEnd"></span></div>
  </section>

  <section class="nk-panel" id="nkTuningPanel" hidden>
    <div class="bar">
      <h2>Tuning timeline</h2>
      <select id="nkTlogDevice" onchange="nkLoadTlog(true)"><option value="">All machines</option></select>
      <span class="nk-tiny">Every frequency, voltage and power-target change with its reason (userdata/tuning_log.jsonl, rotated).</span>
    </div>
    <div class="nk-tlog" id="nkTlog">Loading…</div>
  </section>
"""

NK_SETTINGS_HTML = r"""
      <div class="settings-box nk-set" id="nkSetAlerts">
        <h3>Alerts, Sounds &amp; Notifications</h3>
        <div class="row"><label><input type="checkbox" id="nk_mute"> Master mute</label>
          <label>Volume <input type="range" id="nk_volume" min="0" max="1" step="0.05" style="width:110px"></label></div>
        <div class="row"><label style="min-width:96px">Block found</label><select id="nk_block_sound"></select>
          <button class="icon-btn nk-ro-ok" type="button" onclick="nkPreview('nk_block_sound')">&#9654; Preview</button></div>
        <div class="row"><label style="min-width:96px">Problem alerts</label><select id="nk_problem_sound"></select>
          <button class="icon-btn nk-ro-ok" type="button" onclick="nkPreview('nk_problem_sound')">&#9654; Preview</button></div>
        <div class="row"><label>Load your own .wav / .mp3</label><input type="file" id="nk_sound_file" accept=".wav,.mp3,audio/wav,audio/mpeg" onchange="nkUploadSound(this)"></div>
        <div id="nkCustomSounds" class="mini-note"></div>
        <div class="row"><label><input type="checkbox" id="nk_toast"> Windows desktop notification (toast)</label></div>
        <hr style="border-color:var(--border);border-style:solid none none">
        <div class="row"><label><input type="checkbox" id="nk_a_offline"> Machine offline for</label><input type="number" id="nk_a_offline_min" min="0.5" step="0.5"> min</div>
        <div class="row"><label><input type="checkbox" id="nk_a_hr"> Hashrate drop of</label><input type="number" id="nk_a_hr_pct" min="5" max="95" step="1"> %
          <label>vs</label><input type="number" id="nk_a_hr_base" min="5" step="5"> min baseline, sustained <input type="number" id="nk_a_hr_sus" min="1" step="1"> min</div>
        <div class="row"><label><input type="checkbox" id="nk_a_fb"> Switched to fallback pool</label></div>
        <div class="row"><label><input type="checkbox" id="nk_a_ns"> No shares for</label><input type="number" id="nk_a_ns_min" min="1" step="1"> min</div>
        <div class="row"><label>Repeat the same alert at most every</label><input type="number" id="nk_a_cool" min="1" step="1"> min</div>
        <div class="row-actions" style="margin-top:8px">
          <button class="add-btn" type="button" onclick="nkSaveAlerts()">Save alerts &amp; sounds</button>
          <button class="icon-btn" type="button" onclick="nkTestNotify('problem')">Test problem alert</button>
          <button class="icon-btn" type="button" onclick="nkTestNotify('block')">Test block alert</button>
        </div>
        <div class="mini-note" id="nkAlertsNote">Sounds play in this window. Browsers may need one click on the page before they allow sound.</div>
      </div>

      <div class="settings-box nk-set" id="nkSetBlock">
        <h3>Block-Found Confirmation</h3>
        <div class="row"><label><input type="checkbox" id="nk_b_enabled"> Watch for blocks</label></div>
        <div class="row" style="display:block"><label>Your payout address(es), one per line (the address your solo pool pays the coinbase to)</label>
          <textarea id="nk_b_addrs" spellcheck="false" placeholder="bc1q…"></textarea></div>
        <div class="row"><label>Explorer</label><select id="nk_b_explorer"><option value="mempool">mempool.space</option><option value="blockstream">blockstream.info</option><option value="custom">My own mempool / Esplora</option></select></div>
        <div class="row"><label>Custom base URL</label><input type="text" id="nk_b_custom" placeholder="http://umbrel.local:3006" style="flex:1;min-width:180px"></div>
        <div class="row"><label><input type="checkbox" id="nk_b_fallback"> Fall back to public explorers if mine is unreachable</label></div>
        <div class="row"><label><input type="checkbox" id="nk_b_poolfeed"> Ask the pool which blocks my machines found (HeliosPool; read-only)</label></div>
        <div class="row"><label><input type="checkbox" id="nk_b_unknown"> Notify &quot;Block found (unconfirmed, coin unknown)&quot; for coins Miner Hearth doesn't recognise</label></div>
        <div class="row"><label>Blocks announced one by one, per machine per hour</label><input type="number" id="nk_b_maxhr" min="0" max="120" step="1" style="width:70px"> <span class="mini-note">further blocks are summed up in one &quot;N more blocks found in the last hour&quot; message (0 = no limit)</span></div>
        <div class="row-actions"><button class="add-btn" type="button" onclick="nkSaveBlock()">Save block settings</button></div>
        <div class="mini-note">Two alerts per block: <b>Block Found #height</b> as soon as the miner (found-block counter, winning share) or its pool reports one, and <b>Block Confirmed #height</b> once the coin's own blockchain shows the block paying the address in the miner's pool username (or one of these addresses). Both show the payout address, worker, share difficulty and network difficulty. A block that is never confirmed gets a banner note only.</div>
        <div id="nkCandidates" class="mini-note"></div>
      </div>

      <div class="settings-box nk-set" id="nkSetPower">
        <h3>Power Cost &amp; Profit</h3>
        <div class="row"><label>Electricity rate</label><input type="number" id="nk_p_rate" min="0" step="0.001"> <span>per kWh</span>
          <label>Currency</label><select id="nk_p_cur"></select></div>
        <div class="row"><label><input type="checkbox" id="nk_p_fees"> Include average transaction fees (last 144 blocks)</label></div>
        <div class="row-actions"><button class="add-btn" type="button" onclick="nkSavePower()">Save power settings</button></div>
        <div class="mini-note" id="nkPowerNote"></div>
        <div class="mini-note">Earnings are an expected value (hashrate &divide; network difficulty &times; block reward), the long-run average for solo mining. Real solo payouts are all-or-nothing. BTC price from mempool.space (fallback Coinbase).</div>
      </div>

      <div class="settings-box nk-set" id="nkSetSchedule">
        <h3>Time-of-Use Schedule</h3>
        <div class="row"><label><input type="checkbox" id="nk_s_enabled"> Enable peak-hour windows</label></div>
        <div id="nkWindows"></div>
        <div class="row-actions">
          <button class="icon-btn" type="button" onclick="nkAddWindow()">+ Add window</button>
          <button class="add-btn" type="button" onclick="nkSaveSchedule()">Save schedule</button>
        </div>
        <div id="nkScheduleNote" class="mini-note" role="status" aria-live="polite"></div>
        <div id="nkScheduleStatus" class="mini-note"></div>
        <div class="mini-note">During a window, AxeOS machines get a lower frequency cap, and Braiins OS+ S19s a lower power-target ceiling. Your normal global or per-machine temperature caps and emergency protection always still apply. Leave "Machines" blank to apply to all machines.</div>
      </div>

      <div class="settings-box nk-set" id="nkSetLan">
        <h3>Phone / LAN / Tailscale Access</h3>
        <div class="row"><label><input type="checkbox" id="nk_l_enabled"> Enable remote access listener</label></div>
        <div class="row"><label>Bind</label><select id="nk_l_bind"><option value="all">All LAN &amp; Tailscale (0.0.0.0)</option><option value="tailscale">Tailscale only (100.x tailnet IP)</option></select></div>
        <div class="row"><label>Port</label><input type="number" id="nk_l_port" min="1024" max="65535">
          <label>PIN</label><input type="password" id="nk_l_pin" inputmode="numeric" placeholder="4-12 digits (recommended)" style="width:170px"></div>
        <div class="row"><label><input type="checkbox" id="nk_l_ro"> Read-only for remote devices (blocks every change)</label></div>
        <div class="row-actions"><button class="add-btn" type="button" onclick="nkSaveLan()">Save remote access</button></div>
        <div id="nkLanInfo" class="mini-note"></div>
        <div class="mini-note"><b>Default:</b> off (localhost-only). <b>Tailscale (preferred):</b> keep this listener off and run <code>tailscale serve --bg 8080</code> (or your dashboard port) on this PC so the dashboard stays private to your tailnet over HTTPS. <b>Alternative:</b> enable this listener with <em>Tailscale only (100.x)</em> or <em>All LAN &amp; Tailscale</em> and set a PIN. This PC's own window is never PIN-locked or read-only.</div>
      </div>

      <div class="settings-box nk-set" id="nkSetBackup">
        <h3>Backup &amp; Restore</h3>
        <div class="row"><label><input type="checkbox" id="nk_bk_pw"> Include passwords (Braiins, OTP secrets, LAN PIN)</label></div>
        <div class="row-actions"><button class="add-btn nk-ro-ok" type="button" onclick="nkExportBackup()">Export settings (.json)</button></div>
        <div class="row"><label>Restore from file</label><input type="file" id="nk_bk_file" accept=".json,application/json" onchange="nkImportBackup(this)"></div>
        <div id="nkBackupNote" class="mini-note">One file with machines, overrides, visible fields, card order, alerts, sounds and schedules. History and custom sound files are not included.</div>
      </div>

      <div class="settings-box nk-set" id="nkSetMisc">
        <h3>Network Stats &amp; Firmware Notices</h3>
        <div class="row"><label><input type="checkbox" id="nk_n_enabled"> Fetch network stats &amp; BTC price from the explorer</label></div>
        <div class="row"><label><input type="checkbox" id="nk_f_enabled"> Check GitHub / Braiins for new firmware (at most daily)</label></div>
        <div class="row-actions">
          <button class="add-btn" type="button" onclick="nkSaveMisc()">Save</button>
          <button class="icon-btn" type="button" onclick="post('/api/nk/refresh-network').then(()=>nkNote('nkMiscNote','Refreshing…'))">Refresh network now</button>
          <button class="icon-btn" type="button" onclick="post('/api/nk/check-firmware').then(()=>nkNote('nkMiscNote','Checking firmware…'))">Check firmware now</button>
        </div>
        <div id="nkMiscNote" class="mini-note">Sources: bitaxeorg/ESP-Miner and shufps/ESP-Miner-NerdQAxePlus GitHub releases, feeds.braiins-os.com. Offline = no badge, no errors.</div>
      </div>
"""

NK_JS = r"""
const NK_DONATE = "__DONATE__";
const NK = {
  last: null, seen: new Set(), dismissed: new Set(), settingsFilled: false,
  histRange: "24h", histDevice: "__fleet__", histAt: 0, tlogAt: 0, audioUnlocked: false, windows: [],
};
try { NK.seen = new Set(JSON.parse(localStorage.getItem("nk_seen") || "[]")); } catch (_) {}
try { NK.dismissed = new Set(JSON.parse(localStorage.getItem("nk_dismissed") || "[]")); } catch (_) {}
document.addEventListener("click", () => { NK.audioUnlocked = true; }, {once: true});

function nkNote(id, msg, cls) { const el = $(id); if (el) { el.textContent = msg; el.className = "mini-note" + (cls ? " " + cls : ""); } }
function nkShowToast(msg, cls) {
  let el = $("nkToast");
  if (!el) {
    el = document.createElement("div");
    el.id = "nkToast";
    el.className = "nk-toast";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = "nk-toast show" + (cls ? " " + cls : "");
  clearTimeout(NK._toastT);
  NK._toastT = setTimeout(() => { el.classList.remove("show"); }, 3500);
}
function nkNum(v, d) { return (v === null || v === undefined || isNaN(v)) ? "—" : Number(v).toLocaleString(undefined, {maximumFractionDigits: d ?? 0, minimumFractionDigits: d ?? 0}); }
function nkBig(v) {
  if (!v) return "—";
  const u = [["E", 1e18], ["P", 1e15], ["T", 1e12], ["G", 1e9], ["M", 1e6], ["k", 1e3]];
  for (const [s, f] of u) if (v >= f) return (v / f).toFixed(2) + " " + s;
  return String(Math.round(v));
}
function nkMoney(v, cur) {
  if (v === null || v === undefined || isNaN(v)) return "—";
  try { return new Intl.NumberFormat(undefined, {style: "currency", currency: cur || "USD", maximumFractionDigits: Math.abs(v) < 10 ? 2 : 0}).format(v); }
  catch (_) { return (cur || "") + " " + Number(v).toFixed(2); }
}
function nkPct(p) {
  if (!p) return "0%";
  if (p >= 0.01) return (p * 100).toFixed(p >= 0.1 ? 1 : 2) + "%";
  return "1 in " + Math.round(1 / p).toLocaleString();
}
function nkDur(days) {
  if (!days) return "—";
  if (days > 365.25 * 2) return nkNum(days / 365.25, 0) + " years";
  if (days > 60) return nkNum(days / 30.44, 1) + " months";
  if (days >= 2) return nkNum(days, 1) + " days";
  return nkNum(days * 24, 1) + " hours";
}
function nkWhen(ts) { if (!ts) return ""; const d = new Date(ts * 1000); return d.toLocaleString(undefined, {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}); }

async function nkCopyDonate() {
  const addr = NK_DONATE;
  let ok = false;
  try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(addr); ok = true; } } catch (_) {}
  if (!ok) { try { if (window.pywebview && window.pywebview.api && window.pywebview.api.copy_text) ok = !!(await window.pywebview.api.copy_text(addr)); } catch (_) {} }
  if (!ok) {
    try {
      const ta = document.createElement("textarea");
      ta.value = addr; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.top = "-1000px"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.focus(); ta.select(); ta.setSelectionRange(0, addr.length);
      ok = document.execCommand("copy"); ta.remove();
    } catch (_) {}
  }
  if (!ok && ["127.0.0.1", "localhost", "[::1]"].includes(location.hostname)) {
    try { const r = await post("/api/clipboard", {text: addr}); ok = !!r.ok; } catch (_) {}
  }
  const el = $("nkCopied");
  el.textContent = ok ? "Copied" : "Copy failed: select the address and copy it";
  el.classList.toggle("fail", !ok);
  el.classList.add("show");
  if (!ok) { const r = document.createRange(); r.selectNodeContents($("nkDonateAddr")); const s = window.getSelection(); s.removeAllRanges(); s.addRange(r); }
  clearTimeout(NK._copyT); NK._copyT = setTimeout(() => el.classList.remove("show"), 2200);
}

function nkSoundUrl(ref) {
  if (!ref || ref === "none") return null;
  if (ref.startsWith("builtin:")) return "/api/sounds/builtin/" + encodeURIComponent(ref.slice(8)) + ".wav";
  if (ref.startsWith("custom:")) return "/api/sounds/custom/" + encodeURIComponent(ref.slice(7));
  return null;
}
function nkPlay(ref, vol) {
  const url = nkSoundUrl(ref);
  if (!url) return;
  try { const a = new Audio(url); a.volume = Math.max(0, Math.min(1, vol ?? 0.8)); const p = a.play(); if (p && p.catch) p.catch(() => nkNote("nkAlertsNote", "Sound was blocked by the browser: click anywhere on the page once to allow sound.", "warn")); } catch (_) {}
}
function nkPreview(selId) { const vol = parseFloat($("nk_volume").value || "0.8"); nkPlay($(selId).value, vol); }

function nkHandleEvents(nk) {
  const evs = nk.events || [];
  const now = Date.now() / 1000;
  const s = nk.settings || {};
  for (const ev of evs) {
    if (NK.seen.has(ev.id)) continue;
    NK.seen.add(ev.id);
    if (ev.sound && (now - ev.ts) < 180 && !(s.sounds && s.sounds.master_mute)) {
      nkPlay(ev.sound === "block" ? s.sounds.block_sound : s.sounds.problem_sound, s.sounds.volume);
    }
  }
  try { localStorage.setItem("nk_seen", JSON.stringify([...NK.seen].slice(-300))); } catch (_) {}
  const shown = evs.filter(e => !NK.dismissed.has(e.id) && (now - e.ts) < 86400 && (e.level === "block" || (now - e.ts) < 6 * 3600))
    .sort((a, b) => (b.level === "block") - (a.level === "block") || b.ts - a.ts).slice(0, 5);
  $("nkBanners").innerHTML = shown.map(e => `<div class="nk-banner ${esc(e.level)}">
      <div><div class="t">${e.level === "block" ? "&#127881; " : ""}${esc(e.title)}</div><div class="m">${esc(e.message)} <span class="nk-tiny">${esc(nkWhen(e.ts))}</span></div></div>
      <button class="icon-btn nk-ro-ok x" type="button" onclick="nkDismiss(${Number(e.id)})">Dismiss</button></div>`).join("");
}
function nkDismiss(id) {
  NK.dismissed.add(id);
  try { localStorage.setItem("nk_dismissed", JSON.stringify([...NK.dismissed].slice(-300))); } catch (_) {}
  if (NK.last) nkHandleEvents(NK.last);
}

function nkRenderTou(nk) {
  const st = nk && nk.schedule_status;
  const bar = $("nkTouBar");
  const note = $("nkScheduleStatus");
  if (note) {
    if (!st || !st.enabled) { note.textContent = "TOU status: disabled"; note.className = "mini-note"; }
    else { note.textContent = st.label || "TOU: enabled"; note.className = "mini-note " + (st.state === "active" ? "ok" : ""); }
  }
  if (!bar) return;
  if (!st || !st.enabled) { bar.hidden = true; return; }
  bar.hidden = false;
  bar.classList.toggle("active", st.state === "active");
  bar.classList.toggle("offpeak", st.state !== "active");
  bar.innerHTML = `<span><b>${esc(st.label || "TOU: enabled")}</b></span><span class="nk-tiny">Local PC time · applies immediately on save</span>`;
}
function nkRenderNet(nk) {
  const bar = $("nkNetBar");
  const net = nk.network || {};
  if (!isFieldVisible("network_stats") || !(nk.settings && nk.settings.network && nk.settings.network.enabled)) { bar.hidden = true; return; }
  bar.hidden = false;
  if (!net.height) { bar.innerHTML = `<span>Network stats: ${esc(net.error || "loading…")}</span>`; return; }
  const adj = net.adjustment || {};
  const ch = adj.change_pct;
  const chStr = (ch === null || ch === undefined) ? "—" : (ch >= 0 ? "+" : "") + ch.toFixed(2) + "%";
  const price = nk.price || {};
  bar.innerHTML = `<span>Block height <b>${nkNum(net.height)}</b></span>
    <span>Difficulty <b>${nkBig(net.difficulty)}</b></span>
    <span>Next adjustment <b class="${ch > 0 ? "nk-up" : "nk-down"}">${chStr}</b> in <b>${nkNum(adj.remaining_blocks)}</b> blocks${adj.eta_ts ? ` (~${esc(nkWhen(adj.eta_ts))})` : ""} <span class="nk-tiny">${adj.progress_pct != null ? adj.progress_pct.toFixed(1) + "% of epoch" : ""}</span></span>
    ${price.price ? `<span>BTC <b>${nkMoney(price.price, price.currency)}</b></span>` : ""}
    <span class="nk-tiny">via ${esc(net.source || "")}${net.error ? " · " + esc(net.error) : ""}</span>`;
}

function nkRenderEcon(nk) {
  const el = $("nkEcon");
  const showP = isFieldVisible("profit"), showO = isFieldVisible("block_odds");
  if (!showP && !showO) { el.hidden = true; return; }
  el.hidden = false;
  const f = nk.fleet_econ || {};
  const cur = (nk.settings && nk.settings.power && nk.settings.power.currency) || "USD";
  const rate = nk.settings && nk.settings.power ? nk.settings.power.rate_per_kwh : null;
  const parts = [];
  if (showP) {
    parts.push(`<div><label>Fleet power cost</label><div class="val">${nkMoney(f.cost_per_day, cur)}/day</div><div class="tiny">${nkMoney(f.cost_per_month, cur)}/month at ${nkMoney(rate, cur)}/kWh</div></div>`);
    parts.push(`<div><label>Est. earned (expected)</label><div class="val">${nkNum(f.est_sats_per_day, 0)} sats/day</div><div class="tiny">${f.est_revenue_per_day != null ? nkMoney(f.est_revenue_per_day, cur) + "/day · estimate" : "estimate · BTC price unavailable"}</div></div>`);
    parts.push(`<div><label>Est. profit / day</label><div class="val">${nkMoney(f.est_profit_per_day, cur)}</div><div class="tiny">expected value, solo</div></div>`);
    parts.push(`<div><label>Break-even rate</label><div class="val">${f.break_even_rate != null ? nkMoney(f.break_even_rate, cur) + "/kWh" : "—"}</div><div class="tiny">power price where est. earnings = cost</div></div>`);
  }
  if (showO) {
    parts.push(`<div><label>Fleet block odds</label><div class="val">${nkPct(f.chance_day)} / day</div><div class="tiny">${nkPct(f.chance_month)} / month · ${nkPct(f.chance_year)} / year</div></div>`);
    parts.push(`<div><label>Expected time to block</label><div class="val">${nkDur(f.expected_days_to_block)}</div><div class="tiny">at current difficulty (average, not a countdown)</div></div>`);
  }
  el.innerHTML = parts.join("");
}

function nkCardExtras(d) {
  const nk = lastStatus && lastStatus.nk;
  if (!nk) return "";
  const ex = (nk.devices || {})[d.name];
  if (!ex) return "";
  const cur = (nk.settings && nk.settings.power && nk.settings.power.currency) || "USD";
  const e = ex.econ || {};
  const badges = [];
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
  const cells = [];
  if (badges.length) cells.push(`<div class="nk-badges">${badges.join("")}</div>`);
  if (isFieldVisible("profit")) {
    cells.push(`<div class="metric"><label>Power cost</label><div class="val">${nkMoney(e.cost_per_day, cur)}<small>/day</small></div><div class="chips">${nkNum(e.kwh_per_day, 2)} kWh/day</div></div>`);
    cells.push(`<div class="metric"><label>Est. earned</label><div class="val">${nkNum(e.est_sats_per_day, 0)}<small> sats/day</small></div><div class="chips">${e.est_revenue_per_day != null ? nkMoney(e.est_revenue_per_day, cur) + "/day · " : ""}estimate</div></div>`);
  }
  if (isFieldVisible("block_odds")) {
    cells.push(`<div class="metric"><label>Block odds</label><div class="val" style="font-size:.95rem">${nkPct(e.chance_day)} /day</div><div class="chips">${nkPct(e.chance_month)} /mo · ${nkPct(e.chance_year)} /yr</div></div>`);
    cells.push(`<div class="metric"><label>Expected time to block</label><div class="val" style="font-size:.95rem">${nkDur(e.expected_days_to_block)}</div><div class="chips">at current difficulty</div></div>`);
  }
  return cells.length ? `<div class="nk-extras">${cells.join("")}</div>` : "";
}

/* ---------- History charts ---------- */
function nkSetHist(range, device) {
  if (range) NK.histRange = range;
  if (device !== undefined && device !== null) NK.histDevice = device;
  NK.histAt = 0; nkLoadHist();
}
function nkPath(points, idx, lo, hi) {
  const t0 = points[0][0], t1 = points[points.length - 1][0] || t0 + 1;
  let d = "", pen = false;
  for (const p of points) {
    const v = p[idx];
    if (v === null || v === undefined) { pen = false; continue; }
    const x = (t1 === t0) ? 0 : (p[0] - t0) / (t1 - t0) * 1000;
    const y = 145 - ((v - lo) / ((hi - lo) || 1)) * 140;
    d += (pen ? "L" : "M") + x.toFixed(1) + "," + y.toFixed(1);
    pen = true;
  }
  return d;
}
function nkRange(points, idxs) {
  let lo = Infinity, hi = -Infinity;
  for (const p of points) for (const i of idxs) { const v = p[i]; if (v === null || v === undefined) continue; lo = Math.min(lo, v); hi = Math.max(hi, v); }
  if (lo === Infinity) return [0, 1];
  if (hi === lo) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.08; return [lo - pad, hi + pad];
}
async function nkLoadHist() {
  if (!isFieldVisible("long_history")) return;
  const now = Date.now();
  if (now - NK.histAt < 60000) return;
  NK.histAt = now;
  document.querySelectorAll("#nkHistSeg button").forEach(b => b.classList.toggle("on", b.dataset.r === NK.histRange));
  try {
    const r = await fetch(`/api/history?device=${encodeURIComponent(NK.histDevice === "__fleet__" ? "" : NK.histDevice)}&range=${NK.histRange}`);
    const data = await r.json();
    const pts = data.points || [];
    if (pts.length < 2) {
      $("nkHistHr").innerHTML = ""; $("nkHistT").innerHTML = "";
      $("nkHistInfo").textContent = "Not enough data yet for this range."; $("nkHistStart").textContent = ""; $("nkHistEnd").textContent = "";
      return;
    }
    const [hl, hh] = nkRange(pts, [1]);
    $("nkHistHr").innerHTML = `<path d="${nkPath(pts, 1, hl, hh)}" fill="none" stroke="#3ee0b4" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
    const [tl, th] = nkRange(pts, [2, 3]);
    $("nkHistT").innerHTML = `<path d="${nkPath(pts, 2, tl, th)}" fill="none" stroke="#ff9f43" stroke-width="2" vector-effect="non-scaling-stroke"/><path d="${nkPath(pts, 3, tl, th)}" fill="none" stroke="#6cb8ff" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
    $("nkHistHrRange").textContent = fmtHashrate(Math.max(0, hl)) + " – " + fmtHashrate(hh);
    $("nkHistTRange").textContent = tl.toFixed(0) + "–" + th.toFixed(0) + " °C";
    $("nkHistStart").textContent = nkWhen(pts[0][0]); $("nkHistEnd").textContent = nkWhen(pts[pts.length - 1][0]);
    $("nkHistInfo").textContent = pts.length + " points";
  } catch (e) { $("nkHistInfo").textContent = "History unavailable: " + e; }
}
async function nkDownload(url, filename) {
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error("HTTP " + r.status);
    const text = await r.text();
    if (window.pywebview && window.pywebview.api && window.pywebview.api.save_file) {
      const res = await window.pywebview.api.save_file(filename, text);
      return res || "saved";
    }
    const blob = new Blob([text], {type: filename.endsWith(".csv") ? "text/csv" : "application/json"});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = filename; document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    return "downloaded";
  } catch (e) { alert("Download failed: " + e.message); return ""; }
}
function nkExportCsv() {
  const dev = NK.histDevice === "__fleet__" ? "" : NK.histDevice;
  const safe = (dev || "fleet").replace(/[^A-Za-z0-9._-]+/g, "_");
  nkDownload(`/api/export.csv?device=${encodeURIComponent(dev)}&range=${NK.histRange}`, `miner-hearth-${safe}-${NK.histRange}.csv`);
}

/* ---------- Tuning log ---------- */
async function nkLoadTlog(force) {
  if (!isFieldVisible("tuning_log")) return;
  const now = Date.now();
  if (!force && now - NK.tlogAt < 15000) return;
  NK.tlogAt = now;
  try {
    const dev = $("nkTlogDevice").value;
    const r = await fetch(`/api/tuning-log?limit=150&device=${encodeURIComponent(dev)}`);
    const data = await r.json();
    const rows = data.entries || [];
    const unit = k => k === "frequency" ? " MHz" : (k === "voltage" ? " mV" : (k === "power_target" ? " W" : ""));
    const num = v => (v === null || v === undefined) ? "?" : Math.round(Number(v));
    $("nkTlog").innerHTML = rows.length ? rows.map(e => `<div class="e">
        <div class="nk-tiny">${esc(nkWhen(e.ts))}</div>
        <div><b>${esc(e.device)}</b></div>
        <div><span class="k">${esc(String(e.kind).replace("_", " "))} ${num(e.old)} &rarr; ${num(e.new)}${unit(e.kind)}</span><span class="src ${esc(e.source)}">${esc(e.source)}${e.dry_run ? " · dry-run" : ""}</span><br><span class="nk-tiny">${esc(e.reason || "")}</span></div></div>`).join("")
      : "<div class='sub'>No changes recorded yet.</div>";
  } catch (e) { $("nkTlog").textContent = "Tuning log unavailable: " + e; }
}

/* ---------- Settings ---------- */
function nkSoundOptions(nk, selected) {
  const s = nk.sounds || {builtin: [], custom: []};
  const opts = [`<option value="none">(no sound)</option>`]
    .concat((s.builtin || []).map(o => `<option value="builtin:${esc(o.id)}">${esc(o.label)}</option>`))
    .concat((s.custom || []).map(o => `<option value="custom:${esc(o.id)}">Custom: ${esc(o.label)}</option>`));
  return opts.join("").replace(`value="${esc(selected)}"`, `value="${esc(selected)}" selected`);
}
function nkRenderCustomSounds(nk) {
  const c = (nk.sounds && nk.sounds.custom) || [];
  $("nkCustomSounds").innerHTML = c.length ? "Your sounds (userdata/sounds): " + c.map(o => `${esc(o.label)} <button class="icon-btn" type="button" onclick="nkDeleteSound('${esc(o.id)}')">Delete</button>`).join(" · ") : "";
}
function nkFillSettings(nk) {
  const s = nk.settings; if (!s) return;
  $("nk_mute").checked = !!s.sounds.master_mute; $("nk_volume").value = s.sounds.volume;
  $("nk_block_sound").innerHTML = nkSoundOptions(nk, s.sounds.block_sound);
  $("nk_problem_sound").innerHTML = nkSoundOptions(nk, s.sounds.problem_sound);
  nkRenderCustomSounds(nk);
  $("nk_toast").checked = !!s.notifications.desktop_toast;
  const a = s.alerts;
  $("nk_a_offline").checked = a.offline.enabled; $("nk_a_offline_min").value = a.offline.minutes;
  $("nk_a_hr").checked = a.hashrate_drop.enabled; $("nk_a_hr_pct").value = a.hashrate_drop.drop_pct; $("nk_a_hr_base").value = a.hashrate_drop.baseline_min; $("nk_a_hr_sus").value = a.hashrate_drop.sustain_min;
  $("nk_a_fb").checked = a.fallback_pool.enabled;
  $("nk_a_ns").checked = a.no_shares.enabled; $("nk_a_ns_min").value = a.no_shares.minutes;
  $("nk_a_cool").value = a.cooldown_min;
  $("nk_b_enabled").checked = s.block.enabled; $("nk_b_addrs").value = (s.block.payout_addresses || []).join("\n");
  $("nk_b_explorer").value = s.block.explorer; $("nk_b_custom").value = s.block.custom_base_url || ""; $("nk_b_fallback").checked = s.block.allow_public_fallback;
  $("nk_b_poolfeed").checked = s.block.pool_feed !== false; $("nk_b_unknown").checked = s.block.notify_unknown_coin !== false; $("nk_b_maxhr").value = (s.block.max_messages_per_hour ?? 6);
  $("nk_p_rate").value = s.power.rate_per_kwh;
  $("nk_p_cur").innerHTML = (nk.currencies || ["USD"]).concat(nk.currencies && nk.currencies.includes(s.power.currency) ? [] : [s.power.currency]).map(c => `<option ${c === s.power.currency ? "selected" : ""}>${esc(c)}</option>`).join("");
  $("nk_p_fees").checked = s.power.include_fees;
  $("nk_s_enabled").checked = s.schedule.enabled;
  NK.windows = JSON.parse(JSON.stringify(s.schedule.windows || []));
  nkRenderWindows();
  nkRenderTou(nk);
  $("nk_l_enabled").checked = s.lan.enabled; if ($("nk_l_bind")) $("nk_l_bind").value = s.lan.bind_mode || "all"; $("nk_l_port").value = s.lan.port; $("nk_l_pin").value = s.lan.pin || ""; $("nk_l_ro").checked = s.lan.read_only;
  $("nk_n_enabled").checked = s.network.enabled; $("nk_f_enabled").checked = s.firmware.check_enabled;
  NK.settingsFilled = true;
}
function nkRenderLanInfo(nk) {
  const l = nk.lan || {};
  const modeTxt = l.bind_mode === "tailscale" ? "Tailscale-only" : "LAN + Tailscale";
  let msg = l.running ? (`Listening (${modeTxt}). Open remotely: ` + (l.urls || []).join("  or  ") + (l.read_only ? " (read-only)" : " (full control)") + (l.has_pin ? ", PIN required" : ", no PIN (PIN recommended)")) : (l.enabled ? "Enabled but not listening." : "Off: localhost-only (or via tailscale serve).");
  if (l.error) msg += " " + l.error;
  nkNote("nkLanInfo", msg, l.error ? "err" : (l.running ? "ok" : ""));
  const c = nk.block_candidates || [];
  $("nkCandidates").innerHTML = c.length ? "Recent triggers: " + c.slice(0, 5).map(x => `${esc(x.device)} ${esc(nkWhen(x.trigger_ts))}: <b>${esc(x.status)}</b> ${esc(x.note || "")}`).join("<br>") : "";
}
const NK_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const NK_ALL_DEV_TOKENS = new Set(["all", "all machines", "all devices", "*"]);
function nkParseDeviceFilter(raw) {
  const parts = String(raw || "").split(",").map(x => x.trim()).filter(Boolean);
  if (!parts.length || parts.some(x => NK_ALL_DEV_TOKENS.has(x.toLowerCase()))) return [];
  return parts;
}
function nkWinAppliesInfo(w) {
  const devs = (typeof lastStatus === "object" && lastStatus && Array.isArray(lastStatus.devices)) ? lastStatus.devices : [];
  const filt = nkParseDeviceFilter((w && w.devices || []).join(", "));
  if (!devs.length) {
    return filt.length ? {text: "Applies to filter: " + filt.join(", "), warn: false} : {text: "Applies to: all machines", warn: false};
  }
  if (!filt.length) {
    return {text: "Applies to: " + devs.length + " machine" + (devs.length === 1 ? "" : "s") + " (" + devs.map(d => d.name).join(", ") + ")", warn: false};
  }
  const fLow = new Set(filt.map(x => x.toLowerCase()));
  const matched = devs.filter(d => [d.name, d.hostname, d.ip].some(v => v && fLow.has(String(v).trim().toLowerCase()))).map(d => d.name);
  if (!matched.length) {
    return {text: 'Applies to: 0 machines — none match "' + filt.join(", ") + '" (clear box for all)', warn: true};
  }
  return {text: "Applies to: " + matched.length + " machine" + (matched.length === 1 ? "" : "s") + " (" + matched.join(", ") + ")", warn: false};
}
function nkUpdateWinApplies(i) {
  const el = document.querySelector(`.nk-win-applies[data-wa="${i}"]`);
  if (!el || !NK.windows[i]) return;
  const info = nkWinAppliesInfo(NK.windows[i]);
  el.textContent = info.text;
  el.style.color = info.warn ? "var(--bad)" : "";
}
function nkNormalizeTimeInput(v) {
  const s = String(v || "").trim();
  const m24 = /^([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$/.exec(s);
  if (m24) return String(Number(m24[1])).padStart(2, "0") + ":" + m24[2];
  const m12 = /^(1[0-2]|0?[1-9]):([0-5]\d)\s*([AP]M)$/i.exec(s);
  if (m12) {
    let h = Number(m12[1]); const ap = m12[3].toUpperCase();
    if (ap === "AM" && h === 12) h = 0;
    else if (ap === "PM" && h !== 12) h += 12;
    return String(h).padStart(2, "0") + ":" + m12[2];
  }
  return null;
}
function nkSyncWindowsFromDom() {
  const box = $("nkWindows");
  if (!box) return;
  for (const el of box.querySelectorAll("[data-wi][data-wf]")) {
    const i = Number(el.dataset.wi), f = el.dataset.wf;
    if (!NK.windows[i]) continue;
    if (f === "enabled") NK.windows[i].enabled = !!el.checked;
    else if (f === "devices") NK.windows[i].devices = nkParseDeviceFilter(el.value);
    else if (f === "axe_max_freq_mhz" || f === "bos_power_target_w") {
      NK.windows[i][f] = el.value === "" ? null : Number(el.value);
    } else if (f === "start" || f === "end") {
      NK.windows[i][f] = el.value;
    } else {
      NK.windows[i][f] = el.value;
    }
  }
  for (let i = 0; i < NK.windows.length; i++) {
    const dayBoxes = box.querySelectorAll(`[data-wi="${i}"][data-wd]`);
    if (dayBoxes.length) {
      const days = [];
      for (const db of dayBoxes) if (db.checked) days.push(Number(db.dataset.wd));
      NK.windows[i].days = days.sort((a, b) => a - b);
    }
  }
}
function nkRenderWindows() {
  $("nkWindows").innerHTML = NK.windows.map((w, i) => `<div class="nk-win">
    <div class="row"><input type="text" data-wi="${i}" data-wf="name" value="${esc(w.name)}" oninput="NK.windows[${i}].name=this.value" onchange="NK.windows[${i}].name=this.value" style="flex:1;min-width:120px">
      <label><input type="checkbox" data-wi="${i}" data-wf="enabled" ${w.enabled ? "checked" : ""} onchange="NK.windows[${i}].enabled=this.checked"> on</label>
      <button class="icon-btn" type="button" onclick="nkSyncWindowsFromDom();NK.windows.splice(${i},1);nkRenderWindows()">Remove</button></div>
    <div class="row nk-days">${NK_DAYS.map((dn, di) => `<label><input type="checkbox" data-wi="${i}" data-wd="${di}" ${(w.days || []).includes(di) ? "checked" : ""} onchange="nkToggleDay(${i},${di},this.checked)">${dn}</label>`).join("")}</div>
    <div class="row"><label>From</label><input type="time" data-wi="${i}" data-wf="start" value="${esc(w.start)}" oninput="NK.windows[${i}].start=this.value" onchange="NK.windows[${i}].start=this.value"><label>to</label><input type="time" data-wi="${i}" data-wf="end" value="${esc(w.end)}" oninput="NK.windows[${i}].end=this.value" onchange="NK.windows[${i}].end=this.value"></div>
    <div class="row"><label>Axe freq cap</label><input type="number" data-wi="${i}" data-wf="axe_max_freq_mhz" min="100" max="850" step="25" placeholder="—" value="${w.axe_max_freq_mhz ?? ""}" oninput="NK.windows[${i}].axe_max_freq_mhz=this.value===''?null:Number(this.value)" onchange="NK.windows[${i}].axe_max_freq_mhz=this.value===''?null:Number(this.value)"> MHz</div>
    <div class="row"><label>S19 Braiins power target</label><input type="number" data-wi="${i}" data-wf="bos_power_target_w" min="300" max="6000" step="50" placeholder="—" value="${w.bos_power_target_w ?? ""}" oninput="NK.windows[${i}].bos_power_target_w=this.value===''?null:Number(this.value)" onchange="NK.windows[${i}].bos_power_target_w=this.value===''?null:Number(this.value)"> W</div>
    <div class="row"><label>Machines</label><input type="text" data-wi="${i}" data-wf="devices" placeholder="Leave blank for all machines (e.g. Gamma 1, S19)" value="${esc((w.devices || []).join(", "))}" oninput="NK.windows[${i}].devices=nkParseDeviceFilter(this.value);nkUpdateWinApplies(${i})" onchange="NK.windows[${i}].devices=nkParseDeviceFilter(this.value);nkUpdateWinApplies(${i})" style="flex:1;min-width:140px"></div>
    <div class="mini-note nk-win-applies" data-wa="${i}"></div>
  </div>`).join("") || "<div class='sub'>No windows yet.</div>";
  for (let i = 0; i < NK.windows.length; i++) nkUpdateWinApplies(i);
}
function nkToggleDay(i, di, on) { const d = new Set(NK.windows[i].days || []); on ? d.add(di) : d.delete(di); NK.windows[i].days = [...d].sort((a, b) => a - b); }
function nkAddWindow() { nkSyncWindowsFromDom(); NK.windows.push({name: "Peak hours", enabled: true, days: [0, 1, 2, 3, 4], start: "17:00", end: "21:00", devices: [], axe_max_freq_mhz: 600, bos_power_target_w: null}); nkRenderWindows(); }
async function nkSave(patch, noteId, toastLabel) {
  try {
    const r = await post("/api/nk/settings", {settings: patch});
    const warnMsg = (toastLabel && toastLabel.includes("Warning:")) ? (" · " + toastLabel.slice(toastLabel.indexOf("Warning:"))) : "";
    const extra = (patch && patch.schedule && r.schedule_status && r.schedule_status.label) ? (" · " + r.schedule_status.label) : "";
    nkNote(noteId, "Saved" + extra + warnMsg + ".", warnMsg ? "warn" : "ok");
    nkShowToast((toastLabel || "Saved") + extra, "ok");
    if (lastStatus && lastStatus.nk) {
      lastStatus.nk.settings = r.settings;
      lastStatus.nk.lan = r.lan;
      if (r.schedule_status) lastStatus.nk.schedule_status = r.schedule_status;
      nkFillSettings(lastStatus.nk);
      nkRenderLanInfo(lastStatus.nk);
    }
    await refresh();
  } catch (e) {
    nkNote(noteId, "Could not save: " + e.message, "err");
    nkShowToast("Could not save: " + e.message, "err");
  }
}
const nkN = id => Number($(id).value);
function nkSaveAlerts() {
  nkSave({sounds: {master_mute: $("nk_mute").checked, volume: Number($("nk_volume").value), block_sound: $("nk_block_sound").value, problem_sound: $("nk_problem_sound").value},
    notifications: {desktop_toast: $("nk_toast").checked},
    alerts: {cooldown_min: nkN("nk_a_cool"), offline: {enabled: $("nk_a_offline").checked, minutes: nkN("nk_a_offline_min")},
      hashrate_drop: {enabled: $("nk_a_hr").checked, drop_pct: nkN("nk_a_hr_pct"), baseline_min: nkN("nk_a_hr_base"), sustain_min: nkN("nk_a_hr_sus")},
      fallback_pool: {enabled: $("nk_a_fb").checked}, no_shares: {enabled: $("nk_a_ns").checked, minutes: nkN("nk_a_ns_min")}}}, "nkAlertsNote", "Saved alerts & sounds");
}
function nkSaveBlock() {
  nkSave({block: {enabled: $("nk_b_enabled").checked, payout_addresses: $("nk_b_addrs").value.split(/[\s,;]+/).filter(Boolean),
    explorer: $("nk_b_explorer").value, custom_base_url: $("nk_b_custom").value, allow_public_fallback: $("nk_b_fallback").checked,
    pool_feed: $("nk_b_poolfeed").checked, notify_unknown_coin: $("nk_b_unknown").checked, max_messages_per_hour: parseInt($("nk_b_maxhr").value || "6", 10)}}, "nkCandidates", "Saved block settings");
}
function nkSavePower() { nkSave({power: {rate_per_kwh: nkN("nk_p_rate"), currency: $("nk_p_cur").value, include_fees: $("nk_p_fees").checked}}, "nkPowerNote", "Saved power settings"); }
function nkSaveSchedule() {
  nkSyncWindowsFromDom();
  for (let i = 0; i < NK.windows.length; i++) {
    const w = NK.windows[i];
    const s = nkNormalizeTimeInput(w.start);
    const e = nkNormalizeTimeInput(w.end);
    if (!s || !e) {
      nkNote("nkScheduleNote", `Window "${w.name || ("#" + (i + 1))}" has an invalid time (use HH:MM).`, "err");
      nkShowToast("Invalid TOU window time", "err");
      return;
    }
    w.start = s;
    w.end = e;
  }
  const missingAxe = NK.windows.filter(w => w.enabled !== false && (w.axe_max_freq_mhz == null || w.axe_max_freq_mhz === ""));
  const warn = missingAxe.length ? (" · Warning: window '" + (missingAxe[0].name || "Peak") + "' has no Axe freq cap, so AxeOS/Gammas won't be capped") : "";
  nkSave({schedule: {enabled: $("nk_s_enabled").checked, windows: NK.windows}}, "nkScheduleNote", "Schedule saved" + warn);
}
function nkSaveLan() { nkSave({lan: {enabled: $("nk_l_enabled").checked, bind_mode: $("nk_l_bind") ? $("nk_l_bind").value : "all", port: nkN("nk_l_port"), pin: $("nk_l_pin").value.trim(), read_only: $("nk_l_ro").checked}}, "nkLanInfo", "Saved remote access"); }
function nkSaveMisc() { nkSave({network: {enabled: $("nk_n_enabled").checked}, firmware: {check_enabled: $("nk_f_enabled").checked}}, "nkMiscNote"); }
async function nkTestNotify(kind) {
  try { await post("/api/nk/test-notification", {kind}); await refresh(); nkNote("nkAlertsNote", "Test alert sent (banner + sound here, Windows notification on the PC).", "ok"); }
  catch (e) { nkNote("nkAlertsNote", "Test failed: " + e.message, "err"); }
}
function nkUploadSound(input) {
  const f = input.files && input.files[0];
  if (!f) return;
  if (f.size > 5 * 1024 * 1024) { nkNote("nkAlertsNote", "File is larger than 5 MB.", "err"); return; }
  const rd = new FileReader();
  rd.onload = async () => {
    const b64 = String(rd.result).split(",")[1] || "";
    try {
      const r = await post("/api/sounds/upload", {filename: f.name, data_base64: b64});
      if (lastStatus && lastStatus.nk) { lastStatus.nk.sounds.custom = r.custom; nkFillSettings(lastStatus.nk); }
      nkNote("nkAlertsNote", "Uploaded " + f.name + ". Pick it in a sound list, then Save.", "ok");
    } catch (e) { nkNote("nkAlertsNote", "Upload failed: " + e.message, "err"); }
    input.value = "";
  };
  rd.readAsDataURL(f);
}
async function nkDeleteSound(id) {
  try { const r = await post("/api/sounds/delete", {filename: id}); if (lastStatus && lastStatus.nk) { lastStatus.nk.sounds.custom = r.custom; } await refresh(); if (lastStatus && lastStatus.nk) nkFillSettings(lastStatus.nk); }
  catch (e) { nkNote("nkAlertsNote", "Delete failed: " + e.message, "err"); }
}
async function nkExportBackup() {
  const inc = $("nk_bk_pw").checked ? "1" : "0";
  const stamp = new Date().toISOString().slice(0, 10);
  const res = await nkDownload(`/api/backup/export?include_passwords=${inc}`, `miner-hearth-backup-${stamp}.json`);
  if (res) nkNote("nkBackupNote", "Backup " + res + (inc === "1" ? " (includes passwords: keep it private)." : " (passwords excluded)."), "ok");
}
function nkImportBackup(input) {
  const f = input.files && input.files[0];
  if (!f) return;
  const rd = new FileReader();
  rd.onload = async () => {
    try {
      const data = JSON.parse(String(rd.result));
      if (!confirm("Restore settings from " + f.name + "? This replaces your machine list and settings.")) { input.value = ""; return; }
      const r = await post("/api/backup/import", data);
      nkNote("nkBackupNote", "Restored " + r.devices + " machine(s) and all settings.", "ok");
      NK.settingsFilled = false; await refresh();
    } catch (e) { nkNote("nkBackupNote", "Restore failed: " + e.message, "err"); }
    input.value = "";
  };
  rd.readAsText(f);
}

function nkRender(data) {
  const nk = data.nk;
  document.body.classList.toggle("nk-readonly", !!(data.access && data.access.read_only));
  if (!nk) return;
  NK.last = nk;
  nkHandleEvents(nk);
  nkRenderNet(nk);
  nkRenderTou(nk);
  nkRenderEcon(nk);
  if (!NK.settingsFilled) nkFillSettings(nk);
  else { for (let i = 0; i < NK.windows.length; i++) nkUpdateWinApplies(i); }
  nkRenderLanInfo(nk);
  const devs = (data.devices || []).map(d => d.name);
  const hsel = $("nkHistDevice"), tsel = $("nkTlogDevice");
  const want = ["__fleet__"].concat(devs).join("|");
  if (hsel.dataset.k !== want) {
    hsel.innerHTML = `<option value="__fleet__">Whole fleet</option>` + devs.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    hsel.value = devs.includes(NK.histDevice) ? NK.histDevice : "__fleet__"; hsel.dataset.k = want;
    const tv = tsel.value;
    tsel.innerHTML = `<option value="">All machines</option>` + devs.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    tsel.value = devs.includes(tv) ? tv : "";
  }
  $("nkHistoryPanel").hidden = !isFieldVisible("long_history");
  $("nkTuningPanel").hidden = !isFieldVisible("tuning_log");
  nkLoadHist(); nkLoadTlog(false);
}
""".replace("__DONATE__", DONATE_ADDRESS)


def inject(html: str) -> str:
    """Apply the v1.8 rebrand and feature UI to the v1.7 dashboard HTML."""
    def rep(old: str, new: str) -> None:
        nonlocal html
        if old not in html:
            raise RuntimeError(f"dashboard injection point missing: {old[:60]!r}")
        html = html.replace(old, new, 1)

    rep("<title>Miner Autotune</title>", "<title>Miner Hearth</title>")
    rep("</style>", NK_CSS + "</style>")
    rep('<h1>Miner Autotune <span class="ver-pill" id="appVersion">v__APP_VERSION__</span></h1>',
        '<h1>Miner Hearth <span class="ver-pill" id="appVersion">v__APP_VERSION__</span> ' + NK_DONATE_HTML + '</h1>')
    rep('<div class="sub">Bitaxe Gamma 601 &middot; NerdOCTAxe &middot; Antminer S19 Pro &mdash; local dashboard</div>',
        '<div class="sub">AxeOS miners (Bitaxe-class, NerdQAxe / NerdOCTAxe) &middot; Antminer S19 (CGMiner / Braiins OS+) <span class="badge nk-ro-badge">Read-only</span></div>')
    rep("  </header>\n", "  </header>\n" + NK_TOP_HTML)
    rep('  <section class="scan-panel" id="scanPanel" hidden>', NK_ECON_HTML + '  <section class="scan-panel" id="scanPanel" hidden>')
    rep('        <div id="settingsCardOrder"></div>\n      </div>\n', '        <div id="settingsCardOrder"></div>\n      </div>\n' + NK_SETTINGS_HTML)
    rep('  <section class="logs" id="logsSection">', NK_PANELS_HTML + '  <section class="logs" id="logsSection">')
    rep('    <div class="row-actions">\n      <button class="btn start"', '    ${typeof nkCardExtras === "function" ? nkCardExtras(d) : ""}\n    <div class="row-actions">\n      <button class="btn start"')
    rep('document.title = "Miner Autotune v" + data.version;', 'document.title = "Miner Hearth v" + data.version;')
    rep('  $("log").textContent = (data.logs && data.logs.length) ? data.logs.join("\\n") : "No log lines yet.";',
        '  if (typeof nkRender === "function") nkRender(data);\n  $("log").textContent = (data.logs && data.logs.length) ? data.logs.join("\\n") : "No log lines yet.";')
    rep('const opt = {method: "POST", headers: {"Content-Type": "application/json"}};',
        'const opt = {method: "POST", headers: {"Content-Type": "application/json", "X-Requested-With": "MinerHearth"}};')
    rep("Refreshes every 3 seconds from this PC only.", "Refreshes every 3 seconds.")
    rep("refresh();\nsetInterval(refresh, 3000);\n</script>", NK_JS + "\nrefresh();\nsetInterval(refresh, 3000);\n</script>")
    return html
