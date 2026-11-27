"""Miner Hearth v1.14 UI enhancements:
- Universal paste support (Ctrl+V, Shift+Insert, right-click Cut/Copy/Paste/Select All menu)
  for pywebview on Windows and browsers
- Tier-2 adapter opt-in controls (Whatsminer, Goldshell, Elphapex, IceRiver, Sealminer)
- Generic CGMiner / Prometheus / HTTP-JSON custom endpoint configuration & Add Custom Miner form
"""

from __future__ import annotations

NK114_CSS = r"""
  /* ---- v1.14 editable inputs, context menu & remove button ---- */
  input, textarea, select { user-select: text; -webkit-user-select: text; }
  .icon-btn.danger { color: var(--danger); border-color: rgba(255, 107, 107, 0.45); }
  .icon-btn.danger:hover { border-color: var(--danger); background: rgba(255, 107, 107, 0.12); }
  .nk-ctx-menu {
    position: fixed; z-index: 99999; min-width: 158px;
    background: var(--bg-card); color: var(--text);
    border: 1px solid var(--border); border-radius: 9px;
    box-shadow: 0 10px 28px rgba(0,0,0,.45); padding: 4px;
    font-size: .84rem;
  }
  .nk-ctx-menu button {
    display: flex; align-items: center; justify-content: space-between;
    width: 100%; background: transparent; border: 0; color: inherit;
    padding: 7px 10px; border-radius: 6px; cursor: pointer; text-align: left; font: inherit;
  }
  .nk-ctx-menu button:hover:not(:disabled) { background: var(--bg-soft); color: var(--accent); }
  .nk-ctx-menu button:disabled { opacity: .4; cursor: default; }
  .nk-ctx-menu .nk-ctx-kbd { color: var(--muted); font-size: .72rem; margin-left: 14px; }
  .nk-custom-add {
    display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
    margin-top: 8px; padding-top: 8px; border-top: 1px dashed var(--border);
  }
  .nk-custom-add input, .nk-custom-add select {
    background: var(--bg-soft); color: var(--text); border: 1px solid var(--border);
    border-radius: 7px; padding: 5px 8px; font: inherit; font-size: .8rem;
  }
"""

NK114_CTX_HTML = r"""
<div class="nk-ctx-menu" id="nkCtxMenu" hidden>
  <button type="button" data-act="cut">Cut <span class="nk-ctx-kbd">Ctrl+X</span></button>
  <button type="button" data-act="copy">Copy <span class="nk-ctx-kbd">Ctrl+C</span></button>
  <button type="button" data-act="paste">Paste <span class="nk-ctx-kbd">Ctrl+V</span></button>
  <button type="button" data-act="selectall">Select All <span class="nk-ctx-kbd">Ctrl+A</span></button>
</div>
"""

NK114_JS = r"""
/* ---------- v1.14 Universal Clipboard / Paste / Right-Click Context Menu ---------- */
(function () {
  const EDITABLE_TYPES = new Set(["", "text", "url", "search", "email", "tel", "number", "password"]);
  let ctxTarget = null;
  let lastPasteAt = 0;

  function isEditableEl(el) {
    if (!el || el.nodeType !== 1) return false;
    const tag = (el.tagName || "").toUpperCase();
    if (tag === "TEXTAREA") return !el.disabled;
    if (tag === "INPUT") {
      const t = (el.getAttribute("type") || "text").toLowerCase();
      return EDITABLE_TYPES.has(t) && !el.disabled;
    }
    return false;
  }

  async function readClipboardText() {
    try {
      if (navigator.clipboard && typeof navigator.clipboard.readText === "function") {
        const t = await navigator.clipboard.readText();
        if (typeof t === "string" && t.length > 0) return t;
      }
    } catch (_) {}
    try {
      if (window.pywebview && window.pywebview.api && typeof window.pywebview.api.read_clipboard === "function") {
        const t = await window.pywebview.api.read_clipboard();
        if (typeof t === "string" && t.length > 0) return t;
      }
    } catch (_) {}
    try {
      const r = await fetch("/api/clipboard");
      if (r.ok) {
        const d = await r.json();
        if (d && typeof d.text === "string") return d.text;
      }
    } catch (_) {}
    return "";
  }

  async function writeClipboardText(txt) {
    let ok = false;
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        await navigator.clipboard.writeText(txt);
        ok = true;
      }
    } catch (_) {}
    if (!ok) {
      try {
        if (window.pywebview && window.pywebview.api && typeof window.pywebview.api.copy_text === "function") {
          ok = !!(await window.pywebview.api.copy_text(txt));
        }
      } catch (_) {}
    }
    if (!ok) {
      try {
        await post("/api/clipboard", {text: txt});
      } catch (_) {}
    }
  }

  function insertTextInto(el, rawText) {
    if (!el || el.readOnly || el.disabled) return;
    let txt = String(rawText ?? "");
    if (el.id === "nk_w_discord" || el.id === "nk_w_ntfy") {
      txt = txt.replace(/[\r\n\t]+/g, "").trim();
    }
    el.focus();
    const val = el.value || "";
    const start = (typeof el.selectionStart === "number") ? el.selectionStart : val.length;
    const end = (typeof el.selectionEnd === "number") ? el.selectionEnd : val.length;
    if (typeof el.setRangeText === "function") {
      el.setRangeText(txt, start, end, "end");
    } else {
      el.value = val.slice(0, start) + txt + val.slice(end);
    }
    el.dispatchEvent(new Event("input", {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));
  }

  function hideCtx() {
    const m = $("nkCtxMenu");
    if (m) m.hidden = true;
    ctxTarget = null;
  }

  document.addEventListener("paste", (ev) => {
    lastPasteAt = Date.now();
    const el = ev.target;
    if (isEditableEl(el) && (el.id === "nk_w_discord" || el.id === "nk_w_ntfy")) {
      const clip = ev.clipboardData && ev.clipboardData.getData ? ev.clipboardData.getData("text/plain") : null;
      if (typeof clip === "string") {
        ev.preventDefault();
        insertTextInto(el, clip);
      }
    }
  }, true);

  document.addEventListener("keydown", (ev) => {
    const el = ev.target;
    if (!isEditableEl(el) || el.readOnly) return;
    const isShiftIns = ev.shiftKey && !ev.ctrlKey && !ev.altKey && (ev.key === "Insert" || ev.code === "Insert");
    const isCtrlV = (ev.ctrlKey || ev.metaKey) && !ev.altKey && (ev.key || "").toLowerCase() === "v";
    if (!isShiftIns && !isCtrlV) return;
    const beforeAt = lastPasteAt;
    const beforeVal = el.value;
    setTimeout(async () => {
      if (lastPasteAt > beforeAt || el.value !== beforeVal) return;
      const txt = await readClipboardText();
      if (txt) insertTextInto(el, txt);
    }, 35);
  }, true);

  document.addEventListener("contextmenu", (ev) => {
    const el = ev.target;
    if (!isEditableEl(el)) { hideCtx(); return; }
    const menu = $("nkCtxMenu");
    if (!menu) return;
    ev.preventDefault();
    ctxTarget = el;
    el.focus();
    const isPwd = (el.getAttribute("type") || "").toLowerCase() === "password";
    const s = typeof el.selectionStart === "number" ? el.selectionStart : 0;
    const e = typeof el.selectionEnd === "number" ? el.selectionEnd : 0;
    const hasSel = e > s;
    const ro = !!el.readOnly;
    menu.querySelector('[data-act="cut"]').disabled = isPwd || ro || !hasSel;
    menu.querySelector('[data-act="copy"]').disabled = isPwd || !hasSel;
    menu.querySelector('[data-act="paste"]').disabled = ro;
    menu.querySelector('[data-act="selectall"]').disabled = !(el.value || "").length;
    menu.hidden = false;
    const vw = window.innerWidth || 1024, vh = window.innerHeight || 768;
    const mw = menu.offsetWidth || 165, mh = menu.offsetHeight || 150;
    const left = Math.min(ev.clientX, Math.max(4, vw - mw - 6));
    const top = Math.min(ev.clientY, Math.max(4, vh - mh - 6));
    menu.style.left = left + "px";
    menu.style.top = top + "px";
  }, true);

  document.addEventListener("click", async (ev) => {
    const menu = $("nkCtxMenu");
    if (!menu || menu.hidden) return;
    const btn = ev.target && ev.target.closest ? ev.target.closest("#nkCtxMenu button[data-act]") : null;
    if (!btn) { hideCtx(); return; }
    ev.preventDefault();
    const el = ctxTarget;
    const act = btn.dataset.act;
    hideCtx();
    if (!el) return;
    el.focus();
    const val = el.value || "";
    const s = typeof el.selectionStart === "number" ? el.selectionStart : 0;
    const e = typeof el.selectionEnd === "number" ? el.selectionEnd : 0;
    if (act === "selectall") {
      el.select();
    } else if (act === "copy") {
      if (e > s) await writeClipboardText(val.slice(s, e));
    } else if (act === "cut") {
      if (e > s && !el.readOnly) {
        await writeClipboardText(val.slice(s, e));
        insertTextInto(el, "");
      }
    } else if (act === "paste") {
      const txt = await readClipboardText();
      if (txt) insertTextInto(el, txt);
    }
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") hideCtx();
  });
})();

/* ---------- v1.14 Tier-2 & Generic Adapter UI Controls ---------- */
async function nkSaveTokenCreds(ev, deviceName) {
  ev.preventDefault();
  const key = domKey(deviceName);
  const uEl = $("adp_wu_" + key);
  const pEl = $("adp_wp_" + key);
  const tEl = $("adp_tok_" + key);
  const patch = {};
  if (uEl) patch.web_username = uEl.value.trim();
  if (pEl && pEl.value) patch.web_password = pEl.value;
  if (tEl && tEl.value) patch.api_token = tEl.value.trim();
  try {
    await post("/api/device/" + encodeURIComponent(deviceName) + "/adapter", patch);
    cardNotice["adp:" + deviceName] = {cls: "ok", text: "Saved device credentials"};
    await refresh();
  } catch (e) {
    cardNotice["adp:" + deviceName] = {cls: "err", text: e.message};
  }
  return false;
}

async function nkSaveGenericConfig(ev, deviceName, fam) {
  ev.preventDefault();
  const key = domKey(deviceName);
  const acfg = {};
  const g = (id) => { const el = $(id); return el ? el.value.trim() : ""; };
  if (fam === "generic-prometheus") {
    acfg.metrics_path = g("adp_gp_path_" + key) || "/metrics";
    acfg.hashrate_metric = g("adp_gp_hr_" + key);
    acfg.hashrate_unit = g("adp_gp_unit_" + key) || "H/s";
    acfg.temp_metric = g("adp_gp_temp_" + key);
    acfg.power_metric = g("adp_gp_pwr_" + key);
  } else {
    acfg.endpoint_url = g("adp_gh_url_" + key);
    acfg.hashrate_path = g("adp_gh_hr_" + key);
    acfg.hashrate_unit = g("adp_gh_unit_" + key) || "GH/s";
    acfg.temp_path = g("adp_gh_temp_" + key);
    acfg.power_path = g("adp_gh_pwr_" + key);
  }
  const patch = {adapter_config: acfg};
  const tok = g("adp_tok_" + key);
  if (tok) patch.api_token = tok;
  try {
    await post("/api/device/" + encodeURIComponent(deviceName) + "/adapter", patch);
    cardNotice["adp:" + deviceName] = {cls: "ok", text: "Saved endpoint mapping"};
    await refresh();
  } catch (e) {
    cardNotice["adp:" + deviceName] = {cls: "err", text: e.message};
  }
  return false;
}

const _nk113RenderAdapterBox = nkRenderAdapterBox;
nkRenderAdapterBox = function (d) {
  if (d.type !== "antminer") return "";
  const fam = (d.family || "").toLowerCase();
  const tier1 = new Set(["avalon", "vnish", "luxos", "bitmain-stock"]);
  if (tier1.has(fam)) return _nk113RenderAdapterBox(d);
  if (!fam || fam === "braiins" || fam === "apollo") return "";
  const key = domKey(d.name);
  const wr = !!d.write_enabled;
  const dis = wr ? "" : "disabled";
  const notice = cardNotice["adp:" + d.name];
  const noticeHtml = notice ? `<div class="mini-note ${notice.cls}">${esc(notice.text)}</div>` : "";
  const ac = d.adapter_config || {};
  let ctrlHtml = "";
  let readOnlyOnly = false;

  if (fam === "whatsminer") {
    ctrlHtml = `<form class="nk-adp-row" onsubmit="return nkSaveTokenCreds(event, '${esc(d.name)}')">
      <input id="adp_wu_${key}" type="text" placeholder="v3 account (super)" value="${esc(d.web_username || "super")}" style="width:110px">
      <input id="adp_wp_${key}" type="password" placeholder="${d.web_has_password ? "•••••• (saved)" : "v3 admin pass"}" style="width:120px">
      <button class="icon-btn" type="submit">Save login</button>
    </form>
    <div class="nk-adp-row">
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','power_mode',{mode:'low'})">Low</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','power_mode',{mode:'normal'})">Normal</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','power_mode',{mode:'high'})">High</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','suspend')">Suspend</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','resume')">Resume</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','reboot')">Reboot</button>
    </div>`;
  } else if (fam === "goldshell") {
    ctrlHtml = `<form class="nk-adp-row" onsubmit="return nkSaveTokenCreds(event, '${esc(d.name)}')">
      <input id="adp_wu_${key}" type="text" placeholder="Username" value="${esc(d.web_username || "")}" style="width:110px">
      <input id="adp_wp_${key}" type="password" placeholder="${d.web_has_password ? "•••••• (saved)" : "Web password"}" style="width:120px">
      <button class="icon-btn" type="submit">Save login</button>
    </form>
    <div class="nk-adp-row">
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','workmode',{level:'0'})">Mode 0</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','workmode',{level:'1'})">Mode 1</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','workmode',{level:'2'})">Mode 2</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','reboot')">Reboot</button>
    </div>`;
  } else if (fam === "elphapex") {
    ctrlHtml = `<form class="nk-adp-row" onsubmit="return nkSaveTokenCreds(event, '${esc(d.name)}')">
      <input id="adp_wu_${key}" type="text" placeholder="Basic user" value="${esc(d.web_username || "")}" style="width:110px">
      <input id="adp_wp_${key}" type="password" placeholder="${d.web_has_password ? "•••••• (saved)" : "Web password"}" style="width:120px">
      <button class="icon-btn" type="submit">Save login</button>
    </form>
    <div class="nk-adp-row">
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','workmode',{mode:'0'})">Normal</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','workmode',{mode:'1'})">Low Power</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','workmode',{mode:'2'})">Sleep</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','reboot')">Reboot</button>
    </div>`;
  } else if (fam === "iceriver") {
    ctrlHtml = `<form class="nk-adp-row" onsubmit="return nkSaveTokenCreds(event, '${esc(d.name)}')">
      <input id="adp_tok_${key}" type="password" placeholder="${d.api_has_token ? "•••••• (OC token saved)" : "IceRiver-OC Bearer token"}" style="width:165px">
      <input id="adp_wu_${key}" type="text" placeholder="Stock user" value="${esc(d.web_username || "")}" style="width:95px">
      <input id="adp_wp_${key}" type="password" placeholder="${d.web_has_password ? "••••••" : "Stock pass"}" style="width:95px">
      <button class="icon-btn" type="submit">Save auth</button>
    </form>
    <div class="nk-adp-row">
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','reboot')">Reboot</button>
    </div>`;
  } else if (fam === "generic-prometheus") {
    readOnlyOnly = true;
    ctrlHtml = `<form class="nk-adp-row" onsubmit="return nkSaveGenericConfig(event, '${esc(d.name)}', 'generic-prometheus')">
      <input id="adp_gp_path_${key}" type="text" placeholder="/metrics" value="${esc(ac.metrics_path || "/metrics")}" style="width:95px">
      <input id="adp_gp_hr_${key}" type="text" placeholder="hashrate metric" value="${esc(ac.hashrate_metric || "")}" style="width:130px">
      <select id="adp_gp_unit_${key}"><option value="H/s" ${(ac.hashrate_unit || "H/s") === "H/s" ? "selected" : ""}>H/s</option><option value="GH/s" ${ac.hashrate_unit === "GH/s" ? "selected" : ""}>GH/s</option><option value="TH/s" ${ac.hashrate_unit === "TH/s" ? "selected" : ""}>TH/s</option></select>
      <input id="adp_gp_temp_${key}" type="text" placeholder="temp metric" value="${esc(ac.temp_metric || "")}" style="width:110px">
      <input id="adp_gp_pwr_${key}" type="text" placeholder="power metric" value="${esc(ac.power_metric || "")}" style="width:110px">
      <button class="icon-btn" type="submit">Save map</button>
    </form>`;
  } else if (fam === "generic-http-json") {
    readOnlyOnly = true;
    ctrlHtml = `<form class="nk-adp-row" onsubmit="return nkSaveGenericConfig(event, '${esc(d.name)}', 'generic-http-json')">
      <input id="adp_gh_url_${key}" type="text" placeholder="http://ip/api/status" value="${esc(ac.endpoint_url || "")}" style="flex:1;min-width:170px">
      <input id="adp_gh_hr_${key}" type="text" placeholder="hashrate path (e.g. data.rate)" value="${esc(ac.hashrate_path || "hashrate")}" style="width:140px">
      <select id="adp_gh_unit_${key}"><option value="GH/s" ${(ac.hashrate_unit || "GH/s") === "GH/s" ? "selected" : ""}>GH/s</option><option value="TH/s" ${ac.hashrate_unit === "TH/s" ? "selected" : ""}>TH/s</option><option value="MH/s" ${ac.hashrate_unit === "MH/s" ? "selected" : ""}>MH/s</option><option value="H/s" ${ac.hashrate_unit === "H/s" ? "selected" : ""}>H/s</option></select>
      <input id="adp_gh_temp_${key}" type="text" placeholder="temp path" value="${esc(ac.temp_path || "temp")}" style="width:95px">
      <input id="adp_gh_pwr_${key}" type="text" placeholder="power path" value="${esc(ac.power_path || "power")}" style="width:95px">
      <input id="adp_tok_${key}" type="password" placeholder="${d.api_has_token ? "•••••• (token)" : "Bearer token (opt)"}" style="width:120px">
      <button class="icon-btn" type="submit">Save map</button>
    </form>`;
  } else {
    readOnlyOnly = true;
    ctrlHtml = `<div class="sub">Read-only telemetry adapter (${esc(fam)})</div>`;
  }

  const writeChk = readOnlyOnly
    ? `<span class="badge">Read-only</span>`
    : `<label class="field-check"><input type="checkbox" ${wr ? "checked" : ""} onchange="nkToggleWriteEnabled('${esc(d.name)}', this.checked)"><span>Allow write controls</span></label>`;

  return `<div class="nk-adp-box">
    <div class="nk-adp-head">
      <span><b>${esc(fam.toUpperCase())}</b> · <span>${esc(d.algorithm || "sha256d")}</span></span>
      ${writeChk}
    </div>
    ${ctrlHtml}${noticeHtml}
  </div>`;
};

async function nkAddCustomMiner() {
  const name = ($("nkCustomName")?.value || "").trim();
  const ip = ($("nkCustomIp")?.value || "").trim();
  const fam = ($("nkCustomFam")?.value || "whatsminer").trim();
  const portRaw = Number($("nkCustomPort")?.value || 80);
  const url = ($("nkCustomUrl")?.value || "").trim();
  if (!ip) { alert("Enter miner IP address first."); return; }
  const spec = {
    name: name || (fam + "-" + ip.split(".").pop()),
    ip,
    type: "antminer",
    family: fam,
    port: portRaw || (fam === "generic-prometheus" ? 8081 : 80),
  };
  if (url) spec.adapter_config = {endpoint_url: url};
  try {
    await post("/api/devices/add", spec);
    if ($("nkCustomName")) $("nkCustomName").value = "";
    if ($("nkCustomIp")) $("nkCustomIp").value = "";
    if ($("nkCustomUrl")) $("nkCustomUrl").value = "";
    await refresh();
  } catch (e) {
    alert("Could not add custom miner: " + e.message);
  }
}
"""


def inject114(html: str) -> str:
    assert "</style>" in html
    html = html.replace("</style>", NK114_CSS + "\n</style>", 1)

    scan_anchor = '    <div class="scan-list" id="scanList"></div>\n  </section>'
    custom_bar = '''    <div class="nk-custom-add">
      <strong>Add Tier-2 / Generic Miner:</strong>
      <input id="nkCustomName" type="text" placeholder="Name (optional)" style="width:125px">
      <input id="nkCustomIp" type="text" placeholder="IP (e.g. 192.168.1.60)" style="width:140px">
      <select id="nkCustomFam">
        <option value="whatsminer">Whatsminer</option>
        <option value="goldshell">Goldshell</option>
        <option value="elphapex">Elphapex</option>
        <option value="iceriver">IceRiver</option>
        <option value="sealminer">Sealminer</option>
        <option value="generic-cgminer">Generic CGMiner (:4028)</option>
        <option value="generic-prometheus">Generic Prometheus (:8081/metrics)</option>
        <option value="generic-http-json">Generic HTTP-JSON</option>
      </select>
      <input id="nkCustomPort" type="number" min="1" max="65535" value="80" title="HTTP / Prometheus port" style="width:74px">
      <input id="nkCustomUrl" type="text" placeholder="Custom endpoint URL (HTTP-JSON / Prometheus optional)" style="flex:1;min-width:190px">
      <button class="add-btn" type="button" onclick="nkAddCustomMiner()">Add (watch-only)</button>
    </div>
    <div class="scan-list" id="scanList"></div>
  </section>'''
    assert scan_anchor in html, "Missing scan_anchor for v1.14 custom add"
    html = html.replace(scan_anchor, custom_bar, 1)

    assert "</body>" in html
    html = html.replace("</body>", NK114_CTX_HTML + "\n</body>", 1)

    assert "\nrefresh();\nsetInterval(refresh, 3000);\n</script>" in html
    html = html.replace(
        "\nrefresh();\nsetInterval(refresh, 3000);\n</script>",
        NK114_JS + "\nrefresh();\nsetInterval(refresh, 3000);\n</script>",
        1,
    )
    return html
