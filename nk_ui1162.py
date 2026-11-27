"""Miner Hearth v1.16.2 dashboard additions: Saved Pools (Settings box, "Save this pool" on every machine's pool
info, and a saved-pool picker in the pool editor)."""

from __future__ import annotations

NK1162_SETTINGS_HTML = r"""
      <div class="settings-box nk-set" id="nkSetPools">
        <h3>Saved Pools</h3>
        <div class="mini-note">Pools you use often, ready to pick when you change a machine's pool (card &gt; Edit pool). Kept in <code>userdata\saved_pools.json</code> on this PC, so updates and restarts keep them. Passwords stay on this PC and are shown masked. Put <code>{worker}</code> in a username (for example <code>youraddress.{worker}</code>) to have each machine's name filled in as its worker.</div>
        <div id="nkPoolList" class="nk-pool-list"></div>
        <form id="nkPoolForm" onsubmit="return nk1162Save(event)" autocomplete="off">
          <input type="hidden" id="nk_pl_id">
          <div class="pool-grid">
            <div class="field"><label>Name</label><input id="nk_pl_name" type="text" maxlength="60" placeholder="e.g. My solo pool"></div>
            <div class="field"><label>Stratum address</label><input id="nk_pl_url" type="text" maxlength="300" placeholder="stratum+tcp://pool.example.com:3333" required></div>
            <div class="field"><label>Port (if not in the address)</label><input id="nk_pl_port" type="number" min="1" max="65535" placeholder="3333"></div>
            <div class="field"><label>Username</label><input id="nk_pl_user" type="text" maxlength="200" placeholder="payout-address.{worker}" required></div>
            <div class="field"><label>Password</label><input id="nk_pl_pass" type="password" maxlength="200" placeholder="x" autocomplete="new-password"></div>
            <div class="field"><label>Coin (optional)</label><input id="nk_pl_coin" type="text" maxlength="12" list="nk_pl_coins" placeholder="auto from the host"><datalist id="nk_pl_coins"></datalist></div>
          </div>
          <div class="row-actions">
            <button class="add-btn" type="submit" id="nk_pl_submit">Add to Saved Pools</button>
            <button class="icon-btn" type="button" onclick="nk1162ClearForm()">Clear</button>
          </div>
        </form>
        <div class="mini-note" id="nkPoolNote"></div>
        <div class="row-actions" style="margin-top:6px">
          <button class="icon-btn" type="button" onclick="nk1162Export()">Export JSON</button>
          <label class="nk-row" style="margin:0"><input type="checkbox" id="nk_pl_expw"> include passwords</label>
          <input type="file" id="nk_pl_imp" accept=".json,application/json" style="max-width:210px">
          <label class="nk-row" style="margin:0"><input type="checkbox" id="nk_pl_repl"> replace the list</label>
          <button class="icon-btn" type="button" onclick="nk1162Import()">Import JSON</button>
        </div>
      </div>
"""

NK1162_CSS = r"""
.nk-pool-list{display:flex;flex-direction:column;gap:4px;margin:6px 0}
.nk-pool-item{display:flex;gap:8px;align-items:center;justify-content:space-between;border:1px solid var(--border,#333);border-radius:6px;padding:5px 8px}
.nk-pool-item .nk-pool-main{min-width:0;overflow:hidden;text-overflow:ellipsis}
.nk-pool-item .nk-pool-sub{font-size:.72rem;color:var(--muted)}
.nk-pool-pick{display:flex;flex-wrap:wrap;gap:8px;align-items:end;margin-bottom:8px}
.nk-pool-pick select{max-width:260px}
.nk-save-pool{font-size:.7rem;padding:1px 6px;margin-left:6px}
"""

NK1162_JS = r"""
/* ================= Miner Hearth v1.16.2: saved pools ================= */
const NK1162 = {pools: [], coins: [], pick: {}, loaded: false};
function nk1162Esc(s) { return esc(s); }
async function nk1162Load() {
  try {
    const r = await (await fetch("/api/nk/pools")).json();
    NK1162.pools = r.pools || []; NK1162.coins = r.coins || []; NK1162.loaded = true;
    nk1162RenderList();
    const dl = $("nk_pl_coins"); if (dl && !dl.childElementCount) dl.innerHTML = NK1162.coins.map(c => `<option value="${esc(c)}">`).join("");
  } catch (_) {}
}
function nk1162Set(r) { if (r && r.pools) { NK1162.pools = r.pools; nk1162RenderList(); } }
function nk1162RenderList() {
  const el = $("nkPoolList"); if (!el) return;
  if (!NK1162.pools.length) { el.innerHTML = `<div class="mini-note">No saved pools yet. Add one below, or use "Save this pool" on a machine's pool info.</div>`; return; }
  el.innerHTML = NK1162.pools.map((p, i) => `<div class="nk-pool-item">
      <div class="nk-pool-main"><b>${esc(p.name)}</b>${p.coin_shown ? ` <span class="badge">${esc(p.coin_shown)}${p.coin ? "" : " (auto)"}</span>` : ""}
        <div class="nk-pool-sub">${esc(p.url)} · ${esc(p.user)} · password ${esc(p.password_masked)}${p.scheme === "ssl" ? " · SSL" : ""}</div></div>
      <div class="row-actions" style="flex-wrap:nowrap">
        <button class="icon-btn" type="button" title="Move up" ${i === 0 ? "disabled" : ""} data-id="${esc(p.id)}" onclick="nk1162Move(this.dataset.id,-1)">▲</button>
        <button class="icon-btn" type="button" title="Move down" ${i === NK1162.pools.length - 1 ? "disabled" : ""} data-id="${esc(p.id)}" onclick="nk1162Move(this.dataset.id,1)">▼</button>
        <button class="icon-btn" type="button" data-id="${esc(p.id)}" onclick="nk1162Edit(this.dataset.id)">Edit</button>
        <button class="icon-btn" type="button" data-id="${esc(p.id)}" onclick="nk1162Delete(this.dataset.id)">Delete</button>
      </div></div>`).join("");
}
function nk1162ClearForm() {
  for (const k of ["id", "name", "url", "port", "user", "pass", "coin"]) $("nk_pl_" + k).value = "";
  $("nk_pl_pass").placeholder = "x"; $("nk_pl_submit").textContent = "Add to Saved Pools";
}
function nk1162Edit(id) {
  const p = NK1162.pools.find(x => x.id === id); if (!p) return;
  $("nk_pl_id").value = p.id; $("nk_pl_name").value = p.name; $("nk_pl_url").value = p.url; $("nk_pl_port").value = "";
  $("nk_pl_user").value = p.user; $("nk_pl_pass").value = ""; $("nk_pl_pass").placeholder = "unchanged (" + p.password_masked + ")";
  $("nk_pl_coin").value = p.coin || ""; $("nk_pl_submit").textContent = "Save changes";
  $("nk_pl_name").focus();
}
function nk1162Validate(url, port) {
  const u = url.trim();
  if (!u) return "Enter the stratum address";
  const m = u.match(/^([a-z0-9+.-]+):\/\/(.*)$/i);
  if (m && !/^(stratum\+tcp|stratum\+ssl|stratum\+tls|stratum|tcp|ssl|tls)$/i.test(m[1])) return "Use stratum+tcp:// or stratum+ssl://";
  const rest = m ? m[2] : u;
  if (!/:\d{1,5}\/?$/.test(rest) && !port) return "Add the port (pool.example.com:3333 or the Port field)";
  return "";
}
async function nk1162Save(ev) {
  ev.preventDefault();
  const body = {id: $("nk_pl_id").value || undefined, name: $("nk_pl_name").value.trim(), url: $("nk_pl_url").value.trim(),
                port: $("nk_pl_port").value.trim(), user: $("nk_pl_user").value.trim(), password: $("nk_pl_pass").value, coin: $("nk_pl_coin").value.trim()};
  const bad = nk1162Validate(body.url, body.port); if (bad) { nkNote("nkPoolNote", bad, "err"); return false; }
  try { const r = await post("/api/nk/pools/save", body); nk1162Set(r); nk1162ClearForm(); nkNote("nkPoolNote", `Saved "${r.pool.name}".`, "ok"); }
  catch (e) { nkNote("nkPoolNote", e.message, "err"); }
  return false;
}
async function nk1162Delete(id) {
  const p = NK1162.pools.find(x => x.id === id); if (!p || !confirm(`Delete the saved pool "${p.name}"? Machines using it are not changed.`)) return;
  try { nk1162Set(await post("/api/nk/pools/delete", {id})); nkNote("nkPoolNote", "Deleted.", "ok"); } catch (e) { nkNote("nkPoolNote", e.message, "err"); }
}
async function nk1162Move(id, delta) { try { nk1162Set(await post("/api/nk/pools/move", {id, delta})); } catch (e) { nkNote("nkPoolNote", e.message, "err"); } }
async function nk1162Export() {
  try {
    const r = await fetch("/api/nk/pools/export" + ($("nk_pl_expw").checked ? "?passwords=1" : ""));
    if (!r.ok) throw new Error((await r.json()).error || r.status);
    const a = document.createElement("a"); a.href = URL.createObjectURL(await r.blob()); a.download = "miner-hearth-saved-pools.json"; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (e) { nkNote("nkPoolNote", "Export failed: " + e.message, "err"); }
}
async function nk1162Import() {
  const f = $("nk_pl_imp").files && $("nk_pl_imp").files[0];
  if (!f) { nkNote("nkPoolNote", "Choose a saved-pools JSON file first", "err"); return; }
  if (f.size > 1024 * 1024) { nkNote("nkPoolNote", "That file is too big for a pool list", "err"); return; }
  try {
    const r = await post("/api/nk/pools/import", {data: await f.text(), replace: $("nk_pl_repl").checked});
    nk1162Set(r); nkNote("nkPoolNote", `Imported ${r.added}, skipped ${r.skipped} already saved${r.invalid ? `, ${r.invalid} not valid (${r.errors.join("; ")})` : ""}.`, r.invalid ? "warn" : "ok");
  } catch (e) { nkNote("nkPoolNote", "Import failed: " + e.message, "err"); }
}
/* --- machine cards --- */
function nk1162SaveButtons(d) {
  if (!d.pool_url && !d.fallback_pool_url) return "";
  const b = (slot, label) => `<button class="icon-btn nk-save-pool" type="button" data-dev="${esc(d.name)}" onclick="nk1162SaveFrom(this.dataset.dev,'${slot}')">${label}</button>`;
  return `<div class="row-actions" style="margin-top:4px">${d.pool_url ? b("primary", "Save this pool") : ""}${d.fallback_pool_url ? b("fallback", "Save fallback pool") : ""}</div>`;
}
function nk1162RowSave(d, i) {
  return `<button class="icon-btn nk-save-pool" type="button" title="Save this pool to Saved Pools" data-dev="${esc(d.name)}" data-i="${i}" onclick="nk1162SaveFrom(this.dataset.dev, Number(this.dataset.i))">Save</button>`;
}
async function nk1162SaveFrom(dev, slot) {
  const name = prompt("Name for this saved pool (blank = the pool's host name):", "");
  if (name === null) return;
  try {
    const r = await post("/api/nk/pools/from-device", {device: dev, slot, name});
    nk1162Set(r); cardNotice["pool:" + dev] = {cls: "ok", text: `Saved "${r.pool.name}" to Saved Pools (password "x"; edit it in Settings > Saved Pools if your pool needs another).`};
  } catch (e) { cardNotice["pool:" + dev] = {cls: "err", text: "Not saved: " + e.message}; }
  if (lastStatus) render(lastStatus);
}
function nk1162Picker(d, isAnt) {
  const key = domKey(d.name), pk = NK1162.pick[d.name] || {};
  const opts = sel => `<option value="">— type it in below —</option>` + NK1162.pools.map(p => `<option value="${esc(p.id)}" ${sel === p.id ? "selected" : ""}>${esc(p.name)} (${esc(p.host)}:${esc(p.port)})</option>`).join("");
  if (!NK1162.pools.length) return `<div class="mini-note">Tip: pools saved in Settings &gt; Saved Pools can be picked here.</div>`;
  return `<div class="nk-pool-pick">
      <div class="field"><label>Saved pool (primary)</label><select id="nkPickP_${key}" data-dev="${esc(d.name)}" onchange="nk1162Pick(this.dataset.dev,'p',this.value)">${opts(pk.p)}</select></div>
      ${isAnt ? `<div class="mini-note" style="max-width:260px">Fallback: this firmware's pool API only sets the active pool; set fallbacks in the miner's web page.</div>`
              : `<div class="field"><label>Saved pool (fallback)</label><select id="nkPickF_${key}" data-dev="${esc(d.name)}" onchange="nk1162Pick(this.dataset.dev,'f',this.value)">${opts(pk.f)}</select></div>`}
      <label class="nk-row" style="margin:0"><input type="checkbox" id="nkPickW_${key}" data-dev="${esc(d.name)}" ${pk.w ? "checked" : ""} onchange="nk1162Worker(this.dataset.dev,this.checked)"> Use this machine's name as the worker</label>
    </div>`;
}
async function nk1162Fill(dev, which, id) {
  const key = domKey(dev), pre = which === "p" ? "p" : "fb", pk = NK1162.pick[dev] || {};
  const pass = $((which === "p" ? "pPass_" : "fbPass_") + key);
  if (!id) { if (pass) pass.placeholder = "Keep existing"; return; }
  try {
    const r = await post("/api/nk/pools/resolve", {id, device: dev, use_machine_worker: !!pk.w});
    $(pre + "Url_" + key).value = r.pool.url; $(pre + "Port_" + key).value = r.pool.port; $(pre + "User_" + key).value = r.pool.user;
    if (pass) { pass.value = ""; pass.placeholder = "saved password (hidden)"; }
    cardNotice["pool:" + dev] = r.pool.scheme === "ssl" ? {cls: "warn", text: "This saved pool uses SSL/TLS stratum; older miner firmware may not support it."} : undefined;
  } catch (e) { cardNotice["pool:" + dev] = {cls: "err", text: e.message}; }
}
function nk1162Pick(dev, which, id) { NK1162.pick[dev] = Object.assign(NK1162.pick[dev] || {}, {[which]: id}); nk1162Fill(dev, which, id); }
function nk1162Worker(dev, on) {
  const pk = NK1162.pick[dev] = Object.assign(NK1162.pick[dev] || {}, {w: on});
  if (pk.p) nk1162Fill(dev, "p", pk.p); if (pk.f) nk1162Fill(dev, "f", pk.f);
}
function nk1162PoolExtras(dev) {
  const pk = NK1162.pick[dev] || {};
  return {saved_pool_id: pk.p || "", fallback_saved_pool_id: pk.f || "", use_machine_worker: !!pk.w};
}
function nk1162PoolDone(dev) { delete NK1162.pick[dev]; }
function nk1162Render(data) { if (!NK1162.loaded) nk1162Load(); }
setTimeout(nk1162Load, 700);
"""


def inject1162(html: str) -> str:
    """Apply the v1.16.2 UI on top of the v1.16 dashboard HTML."""
    def rep(old: str, new: str, count: int = 1) -> None:
        nonlocal html
        if old not in html:
            raise RuntimeError(f"v1.16.2 dashboard injection point missing: {old[:70]!r}")
        html = html.replace(old, new, count)

    rep('      <div class="settings-box nk-set" id="nkSetUpdates">', NK1162_SETTINGS_HTML + '      <div class="settings-box nk-set" id="nkSetUpdates">')
    rep("</style>", NK1162_CSS + "</style>")
    # machine card, pool info (closed): "Save this pool" for primary / fallback, and per row of the CGMiner pool list
    rep('<div class="chips"><b>Primary:</b> ${esc(pSummary)}${fbSummary ? `<br><b>Fallback:</b> ${esc(fbSummary)}` : ""}</div>',
        '<div class="chips"><b>Primary:</b> ${esc(pSummary)}${fbSummary ? `<br><b>Fallback:</b> ${esc(fbSummary)}` : ""}</div>${nk1162SaveButtons(d)}')
    rep("allPoolsHtml = `<div class=\"pool-list\">${d.pools.map(p => {", "allPoolsHtml = `<div class=\"pool-list\">${d.pools.map((p, pi) => {")
    rep("        ${tag}\n      </div>`;\n    }).join(\"\")}</div>`;", "        ${tag}${nk1162RowSave(d, pi)}\n      </div>`;\n    }).join(\"\")}</div>`;")
    # pool editor (open): saved-pool picker above the fields; the picked ids go with the change
    rep("""    <form onsubmit="return submitPool(event, '${esc(d.name)}', ${isAnt ? "true" : "false"})">
      <div class="pool-grid">""", """    <form onsubmit="return submitPool(event, '${esc(d.name)}', ${isAnt ? "true" : "false"})">
      ${nk1162Picker(d, isAnt)}
      <div class="pool-grid">""")
    rep("      restart: !isAntminer\n    });", "      restart: !isAntminer,\n      ...nk1162PoolExtras(name)\n    });")
    rep('    cardNotice["pool:" + name] = {cls: "ok", text: res.result || "Pool updated."};\n    openPoolDevice = null;',
        '    cardNotice["pool:" + name] = {cls: "ok", text: res.result || "Pool updated."};\n    nk1162PoolDone(name);\n    openPoolDevice = null;')
    rep('  if (typeof nk116Render === "function") nk116Render(data);', '  if (typeof nk116Render === "function") nk116Render(data);\n  if (typeof nk1162Render === "function") nk1162Render(data);')
    rep("\nrefresh();\nsetInterval(refresh, 3000);\n</script>", NK1162_JS + "\nrefresh();\nsetInterval(refresh, 3000);\n</script>")
    return html
