"""Miner Hearth v1.15.5 dashboard additions: maintenance grace (pool switches / restarts are not faults)."""

from __future__ import annotations

NK1155_SETTINGS_HTML = r"""
      <div class="settings-box nk-set" id="nkSetMaintenance">
        <h3>Pool Switches &amp; Restarts (no false alarms)</h3>
        <label class="nk-row"><input type="checkbox" id="nk_mg_en"> Recognise pool switches and restarts (by Miner Hearth or done on the miner itself) and pause the alerts that are normal while a miner restarts</label>
        <div class="nk-row"><label>Pause those alerts for up to</label><input type="number" id="nk_mg_min" min="1" max="60" step="1" style="width:70px"><label>min (ends early once the miner is hashing and sending shares again)</label></div>
        <label class="nk-row"><input type="checkbox" id="nk_mg_boot"> Ignore start-up readings (sensor -1 °C, pool not connected yet, 0 GH/s) for the first</label>
        <div class="nk-row"><input type="number" id="nk_mg_boot_s" min="30" max="900" step="10" style="width:70px"><label>seconds after any reboot</label></div>
        <div class="row-actions"><button class="add-btn" type="button" onclick="nk1155Save()">Save</button></div>
        <div class="mini-note" id="nkMaintNote">Overheating, VR temperature, real hardware faults and a failover to the fallback pool always alert. If a miner is still offline, not hashing or not sending shares when the pause ends, you get one alert saying it did not recover after the pool switch. Every paused alert is still written to the full event log. You can also pause a single machine's alerts from its card (Pause alerts) before planned work.</div>
      </div>
"""

NK1155_JS = r"""
/* ================= Miner Hearth v1.15.5: maintenance grace ================= */
const NK1155 = {filled: false};
function nk1155Fill(nk) {
  const m = ((nk.settings || {}).alerts || {}).maintenance; if (!m || !$("nk_mg_en")) return;
  $("nk_mg_en").checked = !!m.grace_enabled; $("nk_mg_min").value = m.grace_min;
  $("nk_mg_boot").checked = !!m.boot_settle_enabled; $("nk_mg_boot_s").value = m.boot_settle_sec; NK1155.filled = true;
}
function nk1155Save() {
  nkSave({alerts: {maintenance: {grace_enabled: $("nk_mg_en").checked, grace_min: nkN("nk_mg_min"),
                                  boot_settle_enabled: $("nk_mg_boot").checked, boot_settle_sec: nkN("nk_mg_boot_s")}}},
         "nkMaintNote", "Saved pool switch / restart settings");
  NK1155.filled = false;
}
function nk1155Key(name) { return String(name).replace(/[^\w-]/g, "_"); }
async function nk1155Pause(name, minutes) {
  const id = "nk1155Note_" + nk1155Key(name);
  try {
    const res = await post("/api/nk/maintenance", {device: name, minutes: Number(minutes), reason: minutes > 0 ? "paused from the dashboard" : ""});
    nkNote(id, minutes > 0 ? `Alerts paused for ${minutes} min` : "Alerts resumed", "ok");
    if (typeof refresh === "function") refresh();
    return res;
  } catch (e) { nkNote(id, "Could not change the pause: " + e.message, "err"); }
}
function nk1155Ask(name) {
  const v = prompt(`Pause restart / pool alerts for ${name} for how many minutes?\n\nOverheating and hardware faults still alert. 0 resumes alerts now.`, "30");
  if (v === null) return;
  const m = Number(v);
  if (!isFinite(m) || m < 0) { alert("Enter a number of minutes (0 to resume)."); return; }
  nk1155Pause(name, m);
}
function nk1155Note(d) {
  const m = nk110Ex(d.name).maintenance;
  const n = esc(d.name).replace(/'/g, "\\'");
  const k = nk1155Key(d.name);
  const btn = m && m.manual
    ? `<button class="icon-btn" type="button" onclick="nk1155Pause('${n}', 0)" title="End the maintenance pause now">Resume alerts</button>`
    : `<button class="icon-btn" type="button" onclick="nk1155Ask('${n}')" title="Pause this machine's offline / hashrate / pool alerts for planned work">Pause alerts</button>`;
  const info = m ? `<span class="nk-badge" style="background:#e8f1fd;color:#1a4f8b;border:1px solid #b9d3f3" title="${esc(m.reason || "")}">ℹ ${esc(m.label)}</span>` : "";
  return `<div class="nk-badges nk-maint" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">${info}${btn}<span class="nk-tiny" id="nk1155Note_${k}"></span></div>`;
}
if (typeof nk110Card === "function") {
  const nk1155OrigCard = nk110Card;
  nk110Card = function (d) {
    let note = "";
    try { note = nk1155Note(d); } catch (_) {}
    return note + nk1155OrigCard(d);
  };
}
async function nk1155TestBlockHook() {
  try {
    const patch = nk19HookPatch();
    const chk = nk19ValidDiscordUrl(patch.webhooks.discord_url);
    if (!chk.ok) { nkNote("nkHooksNote", chk.error, "err"); return; }
    await post("/api/nk/settings", {settings: patch});
    const r = await post("/api/nk/test-webhook", {kind: "block"});
    nkNote("nkHooksNote", "Test block notification: " + ((r.results || []).map(x => `${x.service}: ${x.ok ? "sent" : "failed (" + x.error + ")"}`).join(" · ") || (r.error || "Nothing configured.")), r.ok ? "ok" : "err");
  } catch (e) { nkNote("nkHooksNote", "Test failed: " + e.message, "err"); }
}
function nk1155Render(data) {
  const nk = data && data.nk; if (!nk) return;
  if (!NK1155.filled) nk1155Fill(nk);
}
"""


def inject1155(html: str) -> str:
    """Apply the v1.15.5 UI on top of the v1.15.4 dashboard HTML."""
    def rep(old: str, new: str, count: int = 1) -> None:
        nonlocal html
        if old not in html:
            raise RuntimeError(f"v1.15.5 dashboard injection point missing: {old[:70]!r}")
        html = html.replace(old, new, count)

    rep('      <div class="settings-box nk-set" id="nkSetAlerts">', NK1155_SETTINGS_HTML + '      <div class="settings-box nk-set" id="nkSetAlerts">')
    rep('<button class="icon-btn" type="button" onclick="nk19TestHooks()">Send test</button>',
        '<button class="icon-btn" type="button" onclick="nk19TestHooks()">Send test</button>'
        '<button class="icon-btn" type="button" onclick="nk1155TestBlockHook()" title="Sends a sample Block Found alert to every configured webhook">Send test block notification</button>')
    rep('  if (typeof nk115Render === "function") nk115Render(data);', '  if (typeof nk115Render === "function") nk115Render(data);\n  if (typeof nk1155Render === "function") nk1155Render(data);')
    rep("\nrefresh();\nsetInterval(refresh, 3000);\n</script>", NK1155_JS + "\nrefresh();\nsetInterval(refresh, 3000);\n</script>")
    return html
