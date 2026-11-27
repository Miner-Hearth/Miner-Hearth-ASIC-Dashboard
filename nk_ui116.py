"""Miner Hearth v1.16 dashboard additions: live updates (Settings > Live Updates) and update UI assets."""

from __future__ import annotations

NK116_SETTINGS_HTML = r"""
      <div class="settings-box nk-set" id="nkSetUpdates">
        <h3>Live Updates</h3>
        <label class="nk-row"><input type="checkbox" id="nk_up_en"> Allow live updates (off by default). Miner Hearth never downloads or installs anything by itself: you pick an update package below.</label>
        <div class="nk-row"><label>Trusted signing keys</label><textarea id="nk_up_keys" spellcheck="false" placeholder="Ed25519 public keys (64 hex characters), one per line, from whoever publishes your updates" style="flex:1;min-width:220px;height:46px"></textarea></div>
        <label class="nk-row"><input type="checkbox" id="nk_up_req"> Only accept packages signed by one of these keys</label>
        <div class="row-actions"><button class="add-btn" type="button" onclick="nk116Save()">Save</button></div>
        <div class="nk-row" style="margin-top:8px"><label>Update package</label><input type="file" id="nk_up_file" accept=".zip"></div>
        <div class="nk-row"><label>or its URL</label><input type="text" id="nk_up_url" placeholder="https://… (.zip)" style="flex:1;min-width:200px"></div>
        <div class="nk-row"><label>Expected SHA-256 (optional)</label><input type="text" id="nk_up_sha" placeholder="from the publisher, to be sure it's the right file" style="flex:1;min-width:200px"></div>
        <div class="row-actions">
          <button class="icon-btn" type="button" onclick="nk116Check()">Check package</button>
          <button class="add-btn" type="button" id="nk_up_apply" onclick="nk116Apply()" disabled>Apply update</button>
          <button class="icon-btn" type="button" id="nk_up_restart" onclick="nk116Restart()" style="display:none">Quick restart</button>
          <button class="icon-btn" type="button" onclick="nk116Rollback()">Roll back</button>
          <button class="icon-btn" type="button" onclick="nk116Remove()">Remove all updates</button>
        </div>
        <div class="mini-note" id="nkUpReport"></div>
        <div class="mini-note" id="nkUpStatus"></div>
        <div class="mini-note">Every file in a package is checked against its SHA-256 in the package manifest (and the signature, when there is one) before anything changes. Coin data, miner plugins and dashboard files apply at once; program files need a <b>Quick restart</b> (about 10 s: the tuner finishes its current step, saves its state and carries on; miner settings are not touched). The version before each update is kept for <b>Roll back</b>. Updates can only be managed on this PC, not over the LAN.</div>
      </div>
"""

NK116_JS = r"""
/* ================= Miner Hearth v1.16: live updates ================= */
const NK116 = {filled: false, token: "", status: null, ui: ""};
function nk116Fill(nk) {
  const u = (nk.settings || {}).updates; if (!u || !$("nk_up_en")) return;
  $("nk_up_en").checked = !!u.enabled; $("nk_up_req").checked = !!u.require_signature;
  $("nk_up_keys").value = (u.trusted_keys || []).join("\n"); NK116.filled = true;
}
function nk116Save() {
  nkSave({updates: {enabled: $("nk_up_en").checked, require_signature: $("nk_up_req").checked,
                    trusted_keys: $("nk_up_keys").value.split(/[\s,;]+/).filter(Boolean)}}, "nkUpStatus", "Saved live update settings");
  NK116.filled = false; setTimeout(nk116Status, 600);
}
function nk116ReadFile(f) {
  return new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1] || ""); r.onerror = () => rej(new Error("could not read the file")); r.readAsDataURL(f); });
}
function nk116Describe(r) {
  const kinds = Object.entries(r.kinds || {}).map(([k, v]) => `${({app: "program files", plugin: "miner plugins", coins: "coin data", ui: "dashboard files", notes: "notes"})[k] || k}: ${v.length}`).join(", ");
  const sig = r.signature === "trusted" ? "signed by a trusted key ✓" : (r.signature === "untrusted" ? "signed, but not by a key you trust" : "not signed (files verified by SHA-256 only)");
  return `Package ${r.version}${r.title ? " — " + r.title : ""}: ${kinds}. ${sig}. ${r.needs_restart ? "Needs a quick restart." : "Applies without a restart."}` +
         `\nSHA-256 ${r.package_sha256}${r.notes ? "\n" + r.notes.slice(0, 600) : ""}`;
}
async function nk116Check() {
  NK116.token = ""; $("nk_up_apply").disabled = true; nkNote("nkUpReport", "Checking…");
  try {
    const body = {expected_sha256: $("nk_up_sha").value.trim()};
    const f = $("nk_up_file").files && $("nk_up_file").files[0];
    if (f) { if (f.size > 9 * 1024 * 1024) throw new Error("Pick a package under 9 MB, or give its URL"); body.b64 = await nk116ReadFile(f); body.filename = f.name; }
    else if ($("nk_up_url").value.trim()) body.url = $("nk_up_url").value.trim();
    else throw new Error("Choose a package file or enter its URL");
    const r = await post("/api/nk/update/check", body);
    NK116.token = r.report.token; $("nk_up_apply").disabled = false;
    const el = $("nkUpReport"); el.textContent = nk116Describe(r.report); el.className = "mini-note ok"; el.style.whiteSpace = "pre-wrap";
  } catch (e) { nkNote("nkUpReport", "Not accepted: " + e.message, "err"); }
}
async function nk116Apply() {
  if (!NK116.token) return;
  try {
    const r = await post("/api/nk/update/apply", {token: NK116.token});
    NK116.token = ""; $("nk_up_apply").disabled = true;
    nkNote("nkUpReport", `Update ${r.applied} applied. ` + (r.restart_required ? "Use Quick restart to finish." : "Already active."), "ok");
    if (r.hot_reloaded && r.hot_reloaded.ui) setTimeout(() => location.reload(), 900);
    nk116Status();
  } catch (e) { nkNote("nkUpReport", "Not applied: " + e.message, "err"); }
}
async function nk116Rollback() {
  if (!confirm("Go back to the version before the last update?")) return;
  try { const r = await post("/api/nk/update/rollback", {}); nkNote("nkUpReport", `Rolled back: now ${r.now}.` + (r.restart_required ? " Use Quick restart to finish." : ""), "ok"); nk116Status(); }
  catch (e) { nkNote("nkUpReport", e.message, "err"); }
}
async function nk116Remove() {
  if (!confirm("Remove every live update and go back to this version as shipped?")) return;
  try { const r = await post("/api/nk/update/remove", {}); nkNote("nkUpReport", `Now ${r.now}.` + (r.restart_required ? " Use Quick restart to finish." : ""), "ok"); nk116Status(); }
  catch (e) { nkNote("nkUpReport", e.message, "err"); }
}
async function nk116Restart() {
  try {
    await post("/api/nk/update/restart", {});
    nkNote("nkUpReport", "Restarting… the tuner finishes its step, saves its state and carries on. This window reopens by itself.", "ok");
  } catch (e) { nkNote("nkUpReport", e.message, "err"); }
}
async function nk116Status() {
  try {
    const r = await (await fetch("/api/nk/update/status")).json(); NK116.status = r;
    const cur = r.current, prev = r.previous;
    const txt = (r.enabled ? "Live updates: on. " : "Live updates: off. ") +
      (cur ? `Active update: ${cur.version}${cur.signature === "trusted" ? " (signed)" : ""}. ` : `Running ${r.app_version} as shipped. `) +
      (prev ? `Roll back goes to ${prev.version}. ` : (cur ? `Roll back goes to ${r.app_version} as shipped. ` : "")) +
      (r.restart_pending ? "A quick restart is needed to finish the last change." : "") + (r.boot && r.boot.skipped ? " Note: " + r.boot.skipped + "." : "");
    nkNote("nkUpStatus", txt, r.restart_pending ? "warn" : "");
    $("nk_up_restart").style.display = r.restart_pending ? "" : "none";
    nk116LoadUi(r);
  } catch (_) {}
}
function nk116LoadUi(r) {
  const key = JSON.stringify(r.ui_assets || []);
  if (key === NK116.ui) return; NK116.ui = key;
  document.querySelectorAll("[data-nk-update-ui]").forEach(el => el.remove());
  for (const a of (r.ui_assets || [])) {
    const src = "/api/nk/update/ui/" + encodeURIComponent(a.name) + "?v=" + encodeURIComponent(a.v || "");
    if (a.name.endsWith(".css")) { const l = document.createElement("link"); l.rel = "stylesheet"; l.href = src; l.dataset.nkUpdateUi = "1"; document.head.appendChild(l); }
    else if (a.name.endsWith(".js")) { const s = document.createElement("script"); s.src = src; s.dataset.nkUpdateUi = "1"; document.body.appendChild(s); }
  }
}
function nk116Render(data) {
  const nk = data && data.nk; if (!nk) return;
  if (!NK116.filled) nk116Fill(nk);
}
setTimeout(nk116Status, 800);
"""


def inject116(html: str) -> str:
    """Apply the v1.16 UI on top of the v1.15.5 dashboard HTML."""
    def rep(old: str, new: str, count: int = 1) -> None:
        nonlocal html
        if old not in html:
            raise RuntimeError(f"v1.16 dashboard injection point missing: {old[:70]!r}")
        html = html.replace(old, new, count)

    rep('      <div class="settings-box nk-set" id="nkSetMaintenance">', NK116_SETTINGS_HTML + '      <div class="settings-box nk-set" id="nkSetMaintenance">')
    rep('  if (typeof nk1155Render === "function") nk1155Render(data);', '  if (typeof nk1155Render === "function") nk1155Render(data);\n  if (typeof nk116Render === "function") nk116Render(data);')
    rep("\nrefresh();\nsetInterval(refresh, 3000);\n</script>", NK116_JS + "\nrefresh();\nsetInterval(refresh, 3000);\n</script>")
    return html
