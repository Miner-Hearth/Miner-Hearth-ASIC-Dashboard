"""Miner Hearth v1.17.0 dashboard additions: Node Monitoring (Settings box and a Nodes panel).

Node credentials (RPC password, cookie path, macaroon, rune) never reach the browser: the API only returns
has_password-style flags, and the form leaves a secret unchanged when its field is left empty."""

from __future__ import annotations

NK117_PANEL_HTML = r"""
  <section class="nk-panel" id="nkNodes" hidden>
    <div class="nk-panel-head"><h2>Nodes</h2><span class="nk-tiny" id="nkNodesNote">Your own nodes and this computer, checked every minute. Set them up in Settings &rarr; Node Monitoring.</span></div>
    <div class="nk-node-grid" id="nkNodesBody"></div>
  </section>
"""

NK117_SETTINGS_HTML = r"""
      <div class="settings-box nk-set" id="nkSetNodes">
        <h3>Node Monitoring</h3>
        <div class="mini-note">Optional: watch your own Bitcoin Core / Knots node (or another coin's node with the same RPC, e.g. eCash), a Lightning node (LND or Core Lightning), an Electrum server (Fulcrum / Electrs) and this computer's CPU, temperature, memory and disk. Off by default. Passwords, cookies, macaroons and runes are kept in <code>userdata/nodes.json</code> on the Miner Hearth computer and are never sent to a browser. Alerts use the same sounds, notifications and webhooks as the machines ("Node alerts").</div>
        <div class="nk-row">
          <label class="field-check"><input type="checkbox" id="nk_nd_enabled"><span>Monitor nodes</span></label>
          <label class="field-check"><input type="checkbox" id="nk_nd_alerts"><span>Alerts</span></label>
          <label class="field-check"><input type="checkbox" id="nk_nd_host"><span>This computer's CPU / temperature / memory / disk</span></label>
          <label class="field-check" id="nk_nd_umbrel_wrap"><input type="checkbox" id="nk_nd_umbrel"><span>Use the Umbrel Bitcoin / Electrs apps when found</span></label>
        </div>
        <div class="pool-grid">
          <div class="field"><label>Check every (seconds)</label><input id="nk_nd_poll" type="number" min="15" max="3600"></div>
          <div class="field"><label>Alert when behind by (blocks)</label><input id="nk_nd_behind" type="number" min="1" max="1000"></div>
          <div class="field"><label>… for at least (minutes)</label><input id="nk_nd_behind_min" type="number" min="1" max="1440"></div>
          <div class="field"><label>Low disk warning below (%)</label><input id="nk_nd_disk_pct" type="number" min="1" max="50"></div>
          <div class="field"><label>Host temperature alert above (°C)</label><input id="nk_nd_temp" type="number" min="40" max="110"></div>
          <div class="field"><label>Disk to watch (blank = the data folder)</label><input id="nk_nd_disk" type="text" maxlength="260" placeholder="e.g. /mnt/data or D:\"></div>
        </div>
        <div class="row-actions"><button class="add-btn" type="button" onclick="nk117SaveSettings()">Save</button><button class="icon-btn" type="button" onclick="nk117Refresh()">Check now</button></div>
        <div class="mini-note" id="nkNodeSetNote"></div>
        <h4 style="margin:10px 0 4px">Nodes</h4>
        <div id="nkNodeList" class="nk-pool-list"></div>
        <form id="nkNodeForm" onsubmit="return nk117Save(event)" autocomplete="off">
          <input type="hidden" id="nk_nd_id">
          <div class="pool-grid">
            <div class="field"><label>Type</label><select id="nk_nd_kind" onchange="nk117KindChanged()"></select></div>
            <div class="field"><label>Name</label><input id="nk_nd_name" type="text" maxlength="60" placeholder="e.g. Home node"></div>
            <div class="field nk-nd-bitcoind"><label>Coin</label><select id="nk_nd_coin"></select></div>
            <div class="field"><label>Host / IP</label><input id="nk_nd_hostname" type="text" maxlength="200" placeholder="192.168.1.20 or node.local" required></div>
            <div class="field"><label>Port</label><input id="nk_nd_port" type="number" min="1" max="65535"></div>
            <div class="field nk-nd-bitcoind"><label>RPC user</label><input id="nk_nd_user" type="text" maxlength="120" autocomplete="off"></div>
            <div class="field nk-nd-bitcoind"><label>RPC password</label><input id="nk_nd_password" type="password" maxlength="400" autocomplete="new-password"></div>
            <div class="field nk-nd-bitcoind"><label>…or cookie file path (same computer)</label><input id="nk_nd_cookie_path" type="text" maxlength="400" placeholder="~/.bitcoin/.cookie"></div>
            <div class="field nk-nd-lnd"><label>Macaroon (hex or base64; readonly.macaroon is enough)</label><input id="nk_nd_macaroon_hex" type="password" maxlength="4096" autocomplete="new-password"></div>
            <div class="field nk-nd-cln"><label>Rune (read-only rune is enough)</label><input id="nk_nd_rune" type="password" maxlength="4096" autocomplete="new-password"></div>
            <div class="field nk-nd-electrum"><label>Compare with node</label><select id="nk_nd_compare"></select></div>
          </div>
          <div class="nk-row">
            <label class="field-check nk-nd-https"><input type="checkbox" id="nk_nd_https"><span>HTTPS</span></label>
            <label class="field-check nk-nd-electrum"><input type="checkbox" id="nk_nd_ssl"><span>SSL</span></label>
            <label class="field-check"><input type="checkbox" id="nk_nd_insecure" checked><span>Accept a self-signed certificate</span></label>
            <label class="field-check"><input type="checkbox" id="nk_nd_on" checked><span>Enabled</span></label>
          </div>
          <div class="row-actions">
            <button class="add-btn" type="submit" id="nk_nd_submit">Add node</button>
            <button class="icon-btn" type="button" onclick="nk117Test()">Test connection</button>
            <button class="icon-btn" type="button" onclick="nk117ClearForm()">Clear</button>
          </div>
        </form>
        <div class="mini-note" id="nkNodeNote"></div>
      </div>
"""

NK117_CSS = r"""
.nk-node-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:8px}
.nk-node{border:1px solid var(--border,#333);border-radius:8px;padding:8px 10px}
.nk-node h4{margin:0 0 4px;font-size:.9rem;display:flex;justify-content:space-between;gap:6px}
.nk-node .nk-node-kv{font-size:.76rem;display:grid;grid-template-columns:auto 1fr;gap:1px 8px}
.nk-node .nk-node-kv span:nth-child(odd){color:var(--muted)}
.nk-node.ok{border-color:#2e7d32}.nk-node.bad{border-color:#c62828}.nk-node.warn{border-color:#f9a825}
.nk-node-alert{font-size:.74rem;color:#ef9a9a;margin-top:4px}
"""

NK117_JS = r"""
/* ================= Miner Hearth v1.17.0: node monitoring ================= */
const NK117 = {nodes: [], kinds: {}, coins: [], ports: {}, loaded: false, settingsFilled: false};
async function nk117Load() {
  try {
    const r = await (await fetch("/api/nk/nodes")).json();
    NK117.nodes = r.nodes || []; NK117.kinds = r.kinds || {}; NK117.coins = r.coins || []; NK117.ports = r.default_ports || {}; NK117.loaded = true;
    const k = $("nk_nd_kind");
    if (k && !k.childElementCount) k.innerHTML = Object.keys(NK117.kinds).map(x => `<option value="${esc(x)}">${esc(NK117.kinds[x])}</option>`).join("");
    const c = $("nk_nd_coin");
    if (c && !c.childElementCount) c.innerHTML = NK117.coins.map(x => `<option value="${esc(x)}">${esc(x === "OTHER" ? "Other" : x)}</option>`).join("");
    nk117RenderList(); nk117KindChanged(true);
    if (r.error) nkNote("nkNodeNote", r.error, "err");
  } catch (_) {}
}
function nk117Set(r) { if (r && r.nodes) { NK117.nodes = r.nodes; nk117RenderList(); } }
function nk117RenderList() {
  const el = $("nkNodeList"); if (!el) return;
  const cmp = $("nk_nd_compare");
  if (cmp) { const v = cmp.value; cmp.innerHTML = `<option value="">— the Bitcoin network height —</option>` + NK117.nodes.filter(n => n.kind === "bitcoind").map(n => `<option value="${esc(n.id)}">${esc(n.name)}</option>`).join(""); cmp.value = v; }
  if (!NK117.nodes.length) { el.innerHTML = `<div class="mini-note">No nodes added yet.</div>`; return; }
  el.innerHTML = NK117.nodes.map(n => `<div class="nk-pool-item">
      <div class="nk-pool-main"><b>${esc(n.name)}</b> <span class="badge">${esc((NK117.kinds[n.kind] || n.kind).split(" (")[0])}${n.kind === "bitcoind" ? " · " + esc(n.coin) : ""}</span>${n.enabled ? "" : ` <span class="badge">off</span>`}
        <div class="nk-pool-sub">${esc(n.host)}:${esc(n.port)}${n.https || n.ssl ? " · TLS" : ""}${n.has_password ? " · password saved" : ""}${n.has_cookie_path ? " · cookie file" : ""}${n.has_macaroon_hex ? " · macaroon saved" : ""}${n.has_rune ? " · rune saved" : ""}</div></div>
      <div class="row-actions" style="flex-wrap:nowrap">
        <button class="icon-btn" type="button" data-id="${esc(n.id)}" onclick="nk117Edit(this.dataset.id)">Edit</button>
        <button class="icon-btn" type="button" data-id="${esc(n.id)}" onclick="nk117Delete(this.dataset.id)">Delete</button>
      </div></div>`).join("");
}
const NK117_SECRETS = ["password", "cookie_path", "macaroon_hex", "rune"];
function nk117KindChanged(keepPort) {
  const kind = ($("nk_nd_kind") || {}).value || "bitcoind";
  for (const k of ["bitcoind", "lnd", "cln", "electrum"]) document.querySelectorAll(".nk-nd-" + k).forEach(e => { e.style.display = k === kind ? "" : "none"; });
  document.querySelectorAll(".nk-nd-https").forEach(e => { e.style.display = kind === "electrum" ? "none" : ""; });
  const port = $("nk_nd_port"); if (port) port.placeholder = String(NK117.ports[kind] || "");
  if (!keepPort && !$("nk_nd_id").value) $("nk_nd_https").checked = (kind === "lnd" || kind === "cln");
}
function nk117ClearForm() {
  for (const k of ["id", "name", "hostname", "port", "user", "compare", ...NK117_SECRETS]) { const e = $("nk_nd_" + k); if (e) e.value = ""; }
  for (const k of NK117_SECRETS) $("nk_nd_" + k).placeholder = "";
  $("nk_nd_https").checked = false; $("nk_nd_ssl").checked = false; $("nk_nd_insecure").checked = true; $("nk_nd_on").checked = true;
  $("nk_nd_submit").textContent = "Add node"; $("nk_nd_kind").disabled = false; nk117KindChanged();
}
function nk117Edit(id) {
  const n = NK117.nodes.find(x => x.id === id); if (!n) return;
  $("nk_nd_id").value = n.id; $("nk_nd_kind").value = n.kind; $("nk_nd_kind").disabled = true; $("nk_nd_name").value = n.name;
  $("nk_nd_coin").value = n.coin || "BTC"; $("nk_nd_hostname").value = n.host; $("nk_nd_port").value = n.port; $("nk_nd_user").value = n.user || "";
  for (const f of NK117_SECRETS) { const e = $("nk_nd_" + f); e.value = ""; e.placeholder = n["has_" + f] ? "saved (leave empty to keep)" : ""; }
  $("nk_nd_compare").value = n.compare_to || ""; $("nk_nd_https").checked = !!n.https; $("nk_nd_ssl").checked = !!n.ssl;
  $("nk_nd_insecure").checked = n.tls_insecure !== false; $("nk_nd_on").checked = n.enabled !== false;
  $("nk_nd_submit").textContent = "Save changes"; nk117KindChanged(true); $("nk_nd_name").focus();
}
function nk117FormBody() {
  const b = {id: $("nk_nd_id").value || undefined, kind: $("nk_nd_kind").value, name: $("nk_nd_name").value.trim(), coin: $("nk_nd_coin").value,
             host: $("nk_nd_hostname").value.trim(), port: $("nk_nd_port").value.trim() || undefined, user: $("nk_nd_user").value.trim(),
             compare_to: $("nk_nd_compare").value, https: $("nk_nd_https").checked, ssl: $("nk_nd_ssl").checked,
             tls_insecure: $("nk_nd_insecure").checked, enabled: $("nk_nd_on").checked};
  for (const f of NK117_SECRETS) { const v = $("nk_nd_" + f).value; if (v) b[f] = v; }
  return b;
}
async function nk117Save(ev) {
  ev.preventDefault();
  try { const r = await post("/api/nk/nodes/save", nk117FormBody()); nk117Set(r); nk117ClearForm(); nkNote("nkNodeNote", `Saved "${r.node.name}".`, "ok"); }
  catch (e) { nkNote("nkNodeNote", e.message, "err"); }
  return false;
}
async function nk117Test() {
  nkNote("nkNodeNote", "Testing…", "");
  try {
    const r = await post("/api/nk/nodes/test", nk117FormBody());
    nkNote("nkNodeNote", r.ok ? "Connected: " + (r.summary || "OK") : "Failed: " + (r.error || "no answer"), r.ok ? "ok" : "err");
  } catch (e) { nkNote("nkNodeNote", e.message, "err"); }
}
async function nk117Delete(id) {
  const n = NK117.nodes.find(x => x.id === id); if (!n || !confirm(`Remove the node "${n.name}" from Miner Hearth? The node itself is not touched.`)) return;
  try { nk117Set(await post("/api/nk/nodes/delete", {id})); nkNote("nkNodeNote", "Removed.", "ok"); } catch (e) { nkNote("nkNodeNote", e.message, "err"); }
}
async function nk117Refresh() { try { await post("/api/nk/nodes/refresh", {}); nkNote("nkNodeSetNote", "Checking now…", "ok"); setTimeout(refresh, 4000); } catch (e) { nkNote("nkNodeSetNote", e.message, "err"); } }
function nk117SaveSettings() {
  const num = (id, d) => { const v = Number($(id).value); return Number.isFinite(v) && $(id).value !== "" ? v : d; };
  nkSave({nodes: {enabled: $("nk_nd_enabled").checked, alerts: $("nk_nd_alerts").checked, host_stats: $("nk_nd_host").checked, use_umbrel: $("nk_nd_umbrel").checked,
                  poll_sec: num("nk_nd_poll", 60), behind_blocks: num("nk_nd_behind", 3), behind_min: num("nk_nd_behind_min", 20),
                  low_disk_pct: num("nk_nd_disk_pct", 10), temp_max_c: num("nk_nd_temp", 80), disk_path: $("nk_nd_disk").value.trim()}}, "nkNodeSetNote", "Node monitoring saved");
  NK117.settingsFilled = false;
}
function nk117FillSettings(s) {
  if (!s || NK117.settingsFilled || !$("nk_nd_enabled")) return;
  NK117.settingsFilled = true;
  $("nk_nd_enabled").checked = !!s.enabled; $("nk_nd_alerts").checked = s.alerts !== false; $("nk_nd_host").checked = !!s.host_stats;
  $("nk_nd_umbrel").checked = s.use_umbrel !== false; $("nk_nd_poll").value = s.poll_sec ?? 60; $("nk_nd_behind").value = s.behind_blocks ?? 3;
  $("nk_nd_behind_min").value = s.behind_min ?? 20; $("nk_nd_disk_pct").value = s.low_disk_pct ?? 10; $("nk_nd_temp").value = s.temp_max_c ?? 80;
  $("nk_nd_disk").value = s.disk_path || "";
}
function nk117Kv(rows) { return `<div class="nk-node-kv">${rows.filter(r => r[1] !== undefined && r[1] !== null && r[1] !== "").map(r => `<span>${esc(r[0])}</span><span>${esc(r[1])}</span>`).join("")}</div>`; }
function nk117Card(n) {
  const m = n.metrics || {}, cls = n.ok === true ? (n.alerts && n.alerts.length ? "warn" : "ok") : (n.ok === false ? "bad" : "");
  const kindName = (NK117.kinds[n.kind] || n.kind || "").split(" (")[0];
  let rows = [];
  if (n.kind === "bitcoind") rows = [["Chain", m.chain], ["Height", m.blocks], ["Headers", m.headers], ["Synced", m.progress_pct != null ? m.progress_pct + " %" : null],
    ["Peers", m.peers], ["Mempool", m.mempool_tx != null ? `${m.mempool_tx} tx${m.mempool_mb != null ? " · " + m.mempool_mb + " MB" : ""}` : null],
    ["On disk", m.size_on_disk_gb != null ? m.size_on_disk_gb + " GB" + (m.pruned ? " (pruned)" : "") : null], ["Version", m.version]];
  else if (n.kind === "lnd" || n.kind === "cln") rows = [["Alias", m.alias], ["Height", m.blocks], ["Synced to chain", m.synced_to_chain == null ? null : (m.synced_to_chain ? "yes" : "no")],
    ["Channels", m.channels_active != null ? `${m.channels_active} active · ${m.channels_inactive ?? 0} inactive · ${m.channels_pending ?? 0} pending` : null], ["Peers", m.peers], ["Version", m.version]];
  else rows = [["Height", m.blocks], ["Behind", m.behind_blocks], ["Server", m.version]];
  const when = n.checked_ts ? new Date(n.checked_ts * 1000).toLocaleTimeString() : "not checked yet";
  return `<div class="nk-node ${cls}"><h4><span>${esc(n.name)}</span><span class="badge">${esc(kindName)}${n.kind === "bitcoind" ? " · " + esc(n.coin) : ""}${n.source === "umbrel" ? " · Umbrel" : ""}</span></h4>
    ${n.ok === false ? `<div class="nk-node-alert">${esc(n.error || "not answering")}</div>` : nk117Kv(rows)}
    ${(n.alerts || []).map(a => `<div class="nk-node-alert">⚠ ${esc(a)}</div>`).join("")}
    <div class="nk-tiny" style="margin-top:3px">${esc(when)}</div></div>`;
}
function nk117HostCard(h, alerts) {
  if (!h || !Object.keys(h).length) return "";
  const rows = [["CPU", h.cpu_pct != null ? h.cpu_pct + " %" + (h.cpus ? ` of ${h.cpus} cores` : "") : null], ["Load", h.load1],
    ["Temperature", h.temp_c != null ? nkT(h.temp_c) : null], ["Memory", h.ram_used_pct != null ? `${h.ram_used_pct} % of ${h.ram_total_gb} GB` : null],
    ["Disk free", h.disk_free_pct != null ? `${h.disk_free_gb} GB (${h.disk_free_pct} %) of ${h.disk_total_gb} GB` : h.disk_error]];
  return `<div class="nk-node ${alerts && alerts.length ? "warn" : "ok"}"><h4><span>This computer</span><span class="badge">host</span></h4>${nk117Kv(rows)}
    ${(alerts || []).map(a => `<div class="nk-node-alert">⚠ ${esc(a)}</div>`).join("")}</div>`;
}
function nk117Render(data) {
  if (!NK117.loaded) nk117Load();
  const nk = data && data.nk; if (!nk) return;
  if (nk.settings) nk117FillSettings(nk.settings.nodes);
  const st = nk.nodes || {}, panel = $("nkNodes"), body = $("nkNodesBody");
  if (!panel || !body) return;
  const show = !!st.enabled && ((st.nodes || []).length || (st.host && Object.keys(st.host).length));
  panel.hidden = !show;
  if (!show) return;
  body.innerHTML = (st.nodes || []).filter(n => n.enabled !== false).map(nk117Card).join("") + nk117HostCard(st.host, st.host_alerts);
  const uw = $("nk_nd_umbrel_wrap"); if (uw) uw.title = st.umbrel_detected ? "Umbrel apps found" : "No Umbrel apps found";
}
setTimeout(nk117Load, 900);
"""


def inject117(html: str) -> str:
    """Apply the v1.17.0 UI (node monitoring) on top of the v1.16.2 dashboard HTML."""
    def rep(old: str, new: str, count: int = 1) -> None:
        nonlocal html
        if old not in html:
            raise RuntimeError(f"v1.17.0 dashboard injection point missing: {old[:70]!r}")
        html = html.replace(old, new, count)

    rep('      <div class="settings-box nk-set" id="nkSetUpdates">', NK117_SETTINGS_HTML + '      <div class="settings-box nk-set" id="nkSetUpdates">')
    rep('  <section class="nk-panel" id="nkPools" hidden>', NK117_PANEL_HTML.lstrip("\n") + '  <section class="nk-panel" id="nkPools" hidden>')
    rep("</style>", NK117_CSS + "</style>")
    rep('chip_wear: "Chip wear", restart: "Maintenance restart", recovered: "Back online"}',
        'chip_wear: "Chip wear", restart: "Maintenance restart", recovered: "Back online", node: "Node alerts"}')
    rep('  if (typeof nk1162Render === "function") nk1162Render(data);', '  if (typeof nk1162Render === "function") nk1162Render(data);\n  if (typeof nk117Render === "function") nk117Render(data);')
    rep("\nrefresh();\nsetInterval(refresh, 3000);\n</script>", NK117_JS + "\nrefresh();\nsetInterval(refresh, 3000);\n</script>")
    return html
