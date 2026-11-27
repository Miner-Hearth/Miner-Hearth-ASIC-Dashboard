"""Miner Hearth v1.15.4 dashboard additions: full event / telemetry log settings and export."""

from __future__ import annotations

NK115_SETTINGS_HTML = r"""
      <div class="settings-box nk-set" id="nkSetEventLog">
        <h3>Full Event Log (for troubleshooting)</h3>
        <label class="nk-row"><input type="checkbox" id="nk_el_en"> Keep a full event &amp; telemetry log on this PC (personal data removed before it is written)</label>
        <div class="nk-row"><label>Machine snapshot every</label><input type="number" id="nk_el_snap" min="1" max="120" step="1" style="width:70px"><label>min,</label>
          <label>all raw API fields every</label><input type="number" id="nk_el_raw" min="0" max="1440" step="5" style="width:70px"><label>min (0 = off)</label></div>
        <div class="nk-row"><label>Keep</label><input type="number" id="nk_el_days" min="1" max="365" step="1" style="width:70px"><label>days, at most</label>
          <input type="number" id="nk_el_mb" min="10" max="5000" step="10" style="width:80px"><label>MB (oldest days are removed first)</label></div>
        <div class="row-actions"><button class="add-btn" type="button" onclick="nk115Save()">Save</button></div>
        <div class="nk-row" style="margin-top:8px"><label>Export</label>
          <select id="nk_el_range"><option value="24h">Last 24 hours</option><option value="7d">Last 7 days</option><option value="30d">Last 30 days</option><option value="all">Everything kept</option></select>
          <label><input type="checkbox" id="nk_el_anon"> Replace machine names with Machine 1, 2, …</label>
          <button class="icon-btn" type="button" onclick="nk115Export()">Export full log</button></div>
        <div class="mini-note" id="nkEventLogNote">Records every alert, fault, connection loss and recovery (with the exact error), tuning change, restart, time-of-use window, pool switch, settings change and app start/stop, plus regular snapshots from each machine. Saved in userdata\logs\. Dismissing or clearing an alert never deletes it from this log. Pool users, wallets, Wi-Fi names, MAC addresses, passwords and tokens are removed and IP addresses masked, so the exported zip is safe to send for review.</div>
        <div class="mini-note" id="nkEventLogStatus"></div>
      </div>
"""

NK115_JS = r"""
/* ================= Miner Hearth v1.15.4: full event log ================= */
const NK115 = {filled: false, lastStatus: 0};
function nk115Fill(nk) {
  const e = (nk.settings || {}).eventlog; if (!e || !$("nk_el_en")) return;
  $("nk_el_en").checked = !!e.enabled; $("nk_el_snap").value = e.snapshot_min; $("nk_el_raw").value = e.raw_snapshot_min;
  $("nk_el_days").value = e.retention_days; $("nk_el_mb").value = e.max_mb; NK115.filled = true;
}
function nk115Save() {
  nkSave({eventlog: {enabled: $("nk_el_en").checked, snapshot_min: nkN("nk_el_snap"), raw_snapshot_min: nkN("nk_el_raw"),
                     retention_days: nkN("nk_el_days"), max_mb: nkN("nk_el_mb")}}, "nkEventLogNote", "Saved event log settings");
  NK115.filled = false;
}
async function nk115Status(force) {
  const now = Date.now(); if (!force && now - NK115.lastStatus < 30000) return; NK115.lastStatus = now;
  try {
    const r = await fetch("/api/nk/eventlog/status", {cache: "no-store"}); const j = await r.json(); const s = j.status || {};
    const mb = (Number(s.total_bytes || 0) / 1048576).toFixed(1);
    $("nkEventLogStatus").textContent = s.files ? `${s.files} day file(s), ${mb} MB, ${s.oldest_day} to ${s.newest_day}` + (s.write_errors ? ` · ${s.write_errors} write error(s): ${s.last_error}` : "")
      : (s.enabled ? "No entries yet." : "Event log is off.");
  } catch (_) {}
}
function nk115B64(buf) {
  const bytes = new Uint8Array(buf); let bin = ""; const CH = 0x8000;
  for (let i = 0; i < bytes.length; i += CH) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
  return btoa(bin);
}
async function nk115Export() {
  const rng = $("nk_el_range").value; const anon = $("nk_el_anon").checked ? "1" : "0";
  const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-");
  const fname = `miner-hearth-full-log-${rng}-${stamp}.zip`;
  nkNote("nkEventLogNote", "Preparing export…");
  try {
    const r = await fetch(`/api/nk/eventlog/export?range=${encodeURIComponent(rng)}&anon=${anon}`, {cache: "no-store"});
    if (!r.ok) throw new Error("HTTP " + r.status);
    const buf = await r.arrayBuffer();
    let res = "";
    if (window.pywebview && window.pywebview.api && window.pywebview.api.save_file_b64) {
      res = await window.pywebview.api.save_file_b64(fname, nk115B64(buf));
      if (!res) { nkNote("nkEventLogNote", "Export cancelled."); return; }
    } else {
      const blob = new Blob([buf], {type: "application/zip"}); const a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = fname; document.body.appendChild(a); a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000); res = "downloaded";
    }
    nkNote("nkEventLogNote", `Full log ${res} (${(buf.byteLength / 1048576).toFixed(2)} MB, personal data removed).`, "ok");
  } catch (e) { nkNote("nkEventLogNote", "Export failed: " + e.message, "err"); }
}
if (typeof nkDismiss === "function") {
  const nk115OrigDismiss = nkDismiss;
  nkDismiss = function (id) {
    try {
      const ev = ((NK.last && NK.last.events) || []).find(e => Number(e.id) === Number(id)) || {};
      post("/api/nk/eventlog/mark", {action: "alert_dismissed", id: Number(id), device: ev.device || "", kind: ev.kind || "", title: ev.title || "", text: ev.message || ""}).catch(() => {});
    } catch (_) {}
    return nk115OrigDismiss(id);
  };
}
function nk115Render(data) {
  const nk = data && data.nk; if (!nk) return;
  if (!NK115.filled) nk115Fill(nk);
  nk115Status(false);
}
"""


def inject115(html: str) -> str:
    """Apply the v1.15.4 UI on top of the v1.14 dashboard HTML."""
    def rep(old: str, new: str, count: int = 1) -> None:
        nonlocal html
        if old not in html:
            raise RuntimeError(f"v1.15.4 dashboard injection point missing: {old[:70]!r}")
        html = html.replace(old, new, count)

    rep('      <div class="settings-box nk-set" id="nkSetAlerts">', NK115_SETTINGS_HTML + '      <div class="settings-box nk-set" id="nkSetAlerts">')
    rep('  if (typeof nk19Render === "function") nk19Render(data);', '  if (typeof nk19Render === "function") nk19Render(data);\n  if (typeof nk115Render === "function") nk115Render(data);')
    rep("\nrefresh();\nsetInterval(refresh, 3000);\n</script>", NK115_JS + "\nrefresh();\nsetInterval(refresh, 3000);\n</script>")
    return html
