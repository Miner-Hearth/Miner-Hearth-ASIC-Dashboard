"""Miner Hearth v1.13 UI enhancements:
- Dual-side (left & right) Y-axis low/high range labels on EVERY chart (sparklines + history charts)
- Visible-window zoom, series toggles, and dual-unit left/right axes
- Compatibility capture buttons (per device + arbitrary IP) & Redaction Summary modal
- Tier-1 adapter controls (Avalon, VNish, LuxOS, Bitmain Stock, Apollo, BMM) gated behind opt-in write_enabled
- Canonical H/s..EH/s & Sol/s formatting
"""

from __future__ import annotations

import re

NK113_CSS = r"""
  /* ---- v1.13 dual-side chart Y-axes & zoom/series controls ---- */
  .nk-chart-frame { display: grid; grid-template-columns: minmax(46px, auto) 1fr minmax(46px, auto); gap: 6px; align-items: stretch; }
  .nk-chart-frame .nk-chart { width: 100%; min-width: 0; }
  .nk-yaxis { display: flex; flex-direction: column; justify-content: space-between; font-size: .68rem; line-height: 1.1; color: var(--muted); font-variant-numeric: tabular-nums; padding: 4px 0; user-select: none; white-space: nowrap; }
  .nk-yaxis-left { text-align: right; }
  .nk-yaxis-right { text-align: left; }
  .nk-spark-frame { grid-template-columns: auto 1fr auto; gap: 4px; align-items: stretch; }
  .nk-spark-frame svg { width: 100%; height: 34px; min-width: 0; display: block; }
  .nk-spark-frame .nk-yaxis { font-size: .60rem; padding: 1px 0; line-height: 1; }
  .nk-chart-tools { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin: 4px 0; font-size: .74rem; color: var(--muted); }
  .nk-pill-tog { border: 1px solid var(--border); background: var(--bg-soft); color: var(--text); border-radius: 999px; padding: 2px 9px; font-size: .72rem; cursor: pointer; opacity: .55; }
  .nk-pill-tog.on { opacity: 1; border-color: var(--accent); }
  .nk-zoom-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; font-size: .74rem; color: var(--muted); margin-left: auto; }
  .nk-zoom-bar input[type=range] { width: 76px; accent-color: var(--accent); }
  /* ---- v1.13 adapter box & compatibility capture modal ---- */
  .nk-compat-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 8px; padding-top: 8px; border-top: 1px dashed var(--border); }
  .nk-compat-bar input { background: var(--bg-soft); color: var(--text); border: 1px solid var(--border); border-radius: 7px; padding: 5px 8px; font: inherit; font-size: .82rem; min-width: 185px; }
  .nk-adp-box { border: 1px solid var(--border); border-radius: 9px; padding: 8px 10px; background: var(--bg-soft); margin: 8px 0; font-size: .8rem; }
  .nk-adp-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 6px; margin-bottom: 6px; }
  .nk-adp-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 6px; }
  .nk-adp-row input, .nk-adp-row select { background: var(--bg-card); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 4px 7px; font: inherit; font-size: .78rem; }
  .nk-redact-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 6px; margin: 10px 0; }
  .nk-redact-item { display: flex; justify-content: space-between; padding: 5px 8px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-soft); font-size: .8rem; }
"""

NK113_COMPAT_MODAL = r"""
  <div class="nk110-modal" id="nkCompatModal" hidden>
    <div class="nk110-dlg" style="width:min(740px,95vw)">
      <div class="nk110-head">
        <h2 id="nkCompatTitle">Compatibility Capture</h2>
        <button class="icon-btn" type="button" onclick="$('nkCompatModal').hidden=true">Close</button>
      </div>
      <div class="nk110-body" id="nkCompatBody"></div>
    </div>
  </div>
"""

NK113_JS = r"""
/* ---------- v1.13 canonical units, dual-side chart axes, zoom/series toggles, adapter controls & compat capture ---------- */
function fmtHashrateUnit(valGhs, unitKind, isRawHs) {
  if (valGhs === null || valGhs === undefined || valGhs === "") return "—";
  const raw = Number(valGhs);
  if (Number.isNaN(raw)) return "—";
  const hs = isRawHs ? raw : (unitKind === "sol" ? raw * 1e3 : raw * 1e9);
  if (hs < 0) return "—";
  if (unitKind === "sol") {
    const su = [["GSol/s", 1e9], ["MSol/s", 1e6], ["kSol/s", 1e3], ["Sol/s", 1]];
    for (const [lbl, sc] of su) {
      if (hs >= sc || sc === 1) {
        const v = hs / sc;
        return v.toFixed(v < 100 ? 2 : 1) + " " + lbl;
      }
    }
  }
  const hu = [["EH/s", 1e18], ["PH/s", 1e15], ["TH/s", 1e12], ["GH/s", 1e9], ["MH/s", 1e6], ["kH/s", 1e3], ["H/s", 1]];
  for (const [lbl, sc] of hu) {
    if (hs >= sc || sc === 1) {
      const v = hs / sc;
      return v.toFixed(v < 100 ? 2 : 1) + " " + lbl;
    }
  }
  return hs.toFixed(1) + " H/s";
}

function fmtCompactAxisHashrate(ghs, unitKind) {
  if (ghs === null || ghs === undefined || Number.isNaN(Number(ghs))) return "—";
  return fmtHashrateUnit(Math.max(0, Number(ghs)), unitKind || "hash", false);
}

NK.histSeries = NK.histSeries || { hr: true, avg24: true, power: false, asic: true, vr: true };
NK.histZoom = NK.histZoom || [0, 100];
NK.histRawPts = NK.histRawPts || [];

function nkToggleSeries(key) {
  NK.histSeries[key] = !NK.histSeries[key];
  nkRenderHistoryCharts();
}

function nkSetZoom(which, val) {
  let a = Number(NK.histZoom[0] || 0), b = Number(NK.histZoom[1] ?? 100);
  const v = Math.max(0, Math.min(100, Number(val)));
  if (which === "start") a = Math.min(v, b - 5);
  else b = Math.max(v, a + 5);
  NK.histZoom = [Math.max(0, a), Math.min(100, b)];
  const sEl = $("nkZoomStart"), eEl = $("nkZoomEnd");
  if (sEl) sEl.value = NK.histZoom[0];
  if (eEl) eEl.value = NK.histZoom[1];
  nkRenderHistoryCharts();
}

function nkResetZoom() {
  NK.histZoom = [0, 100];
  const sEl = $("nkZoomStart"), eEl = $("nkZoomEnd");
  if (sEl) sEl.value = 0;
  if (eEl) eEl.value = 100;
  nkRenderHistoryCharts();
}

function nkVisiblePoints(pts) {
  if (!pts || pts.length < 2) return pts || [];
  const z0 = Math.max(0, Math.min(95, Number(NK.histZoom[0] || 0)));
  const z1 = Math.max(z0 + 5, Math.min(100, Number(NK.histZoom[1] ?? 100)));
  if (z0 <= 0 && z1 >= 100) return pts;
  const i0 = Math.floor((z0 / 100) * (pts.length - 1));
  const i1 = Math.max(i0 + 2, Math.ceil((z1 / 100) * pts.length));
  return pts.slice(i0, Math.min(pts.length, i1));
}

function nkRangeNonNeg(points, idxs, nonNeg) {
  let lo = Infinity, hi = -Infinity;
  for (const p of points) for (const i of idxs) {
    const v = p[i];
    if (v === null || v === undefined) continue;
    const nv = Number(v);
    if (Number.isNaN(nv)) continue;
    lo = Math.min(lo, nv); hi = Math.max(hi, nv);
  }
  if (lo === Infinity) return [0, 1];
  if (hi === lo) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.08;
  let rLo = lo - pad, rHi = hi + pad;
  if (nonNeg && rLo < 0) rLo = 0;
  if (rHi <= rLo) rHi = rLo + 1;
  return [rLo, rHi];
}

function nkSetAxes(prefix, lHi, lLo, rHi, rLo, lColor, rColor) {
  const lh = $(prefix + "LHi"), ll = $(prefix + "LLo"), rh = $(prefix + "RHi"), rl = $(prefix + "RLo");
  if (lh) { lh.textContent = lHi; lh.style.color = lColor || ""; }
  if (ll) { ll.textContent = lLo; ll.style.color = lColor || ""; }
  if (rh) { rh.textContent = rHi; rh.style.color = rColor || lColor || ""; }
  if (rl) { rl.textContent = rLo; rl.style.color = rColor || lColor || ""; }
}

function nkActiveUnitKind() {
  if (!lastStatus || !lastStatus.devices) return "hash";
  if (NK.histDevice && NK.histDevice !== "__fleet__") {
    const d = lastStatus.devices.find(x => x.name === NK.histDevice);
    return (d && d.hashrate_unit_kind) || "hash";
  }
  const kinds = new Set(lastStatus.devices.map(d => d.hashrate_unit_kind || "hash"));
  return kinds.size === 1 ? Array.from(kinds)[0] : "hash";
}

function nkRenderHistoryCharts() {
  const allPts = NK.histRawPts || [];
  const pts = nkVisiblePoints(allPts);
  const ser = NK.histSeries || { hr: true, avg24: true, power: false, asic: true, vr: true };
  // Update toggle button states
  document.querySelectorAll(".nk-pill-tog[data-s]").forEach(btn => {
    btn.classList.toggle("on", !!ser[btn.dataset.s]);
  });
  const zLbl = $("nkZoomLabel");
  if (zLbl) zLbl.textContent = (NK.histZoom[0] <= 0 && NK.histZoom[1] >= 100) ? "100%" : `${NK.histZoom[0]}–${NK.histZoom[1]}%`;

  if (!pts || pts.length < 2) {
    $("nkHistHr").innerHTML = ""; $("nkHistT").innerHTML = "";
    nkSetAxes("nkHistHr", "—", "—", "—", "—");
    nkSetAxes("nkHistT", "—", "—", "—", "—");
    $("nkHistInfo").textContent = "Not enough data yet for this range.";
    $("nkHistStart").textContent = ""; $("nkHistEnd").textContent = "";
    return;
  }

  const ukind = nkActiveUnitKind();
  const hasPower = pts.some(p => p[4] != null);
  const pBtn = $("nkTogPower");
  if (pBtn) pBtn.style.display = hasPower ? "" : "none";
  const showHr = !!ser.hr;
  const showPwr = !!ser.power && hasPower;

  // Chart 1: Hashrate (+ optional 24h avg, optional dual-unit Power W)
  let hrSvg = "";
  let lHi = "—", lLo = "—", rHi = "—", rLo = "—", lCol = "", rCol = "";
  let rangeParts = [];
  if (showHr) {
    const [hl, hh] = nkRangeNonNeg(pts, [1], true);
    hrSvg += `<path d="${nkPath(pts, 1, hl, hh)}" fill="none" stroke="#3ee0b4" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
    const tEnd = pts[pts.length - 1][0];
    const last24 = pts.filter(p => p[0] >= tEnd - 86400 && p[1] != null).map(p => p[1]);
    const avg24 = last24.length ? last24.reduce((a, b) => a + b, 0) / last24.length : null;
    if (ser.avg24 && avg24 != null) {
      const y = (145 - ((avg24 - hl) / ((hh - hl) || 1)) * 140).toFixed(1);
      hrSvg += `<line x1="0" x2="1000" y1="${y}" y2="${y}" stroke="#f5c542" stroke-width="1.5" stroke-dasharray="6 5" vector-effect="non-scaling-stroke"/>`;
    }
    lHi = fmtCompactAxisHashrate(hh, ukind);
    lLo = fmtCompactAxisHashrate(hl, ukind);
    lCol = "#3ee0b4";
    rangeParts.push(esc(lLo + " – " + lHi) + (ser.avg24 && avg24 != null ? ` · <span class="nk-avg-label">${NK.histDevice === "__fleet__" ? "fleet " : ""}24h avg ${esc(fmtCompactAxisHashrate(avg24, ukind))}</span>` : ""));
    if (!showPwr) {
      rHi = lHi; rLo = lLo; rCol = lCol;
    }
  }
  if (showPwr) {
    const [pl, ph] = nkRangeNonNeg(pts, [4], true);
    hrSvg += `<path d="${nkPath(pts, 4, pl, ph)}" fill="none" stroke="#a78bfa" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
    const pHiStr = Math.round(ph) + " W", pLoStr = Math.round(pl) + " W";
    rHi = pHiStr; rLo = pLoStr; rCol = "#a78bfa";
    rangeParts.push(`<span style="color:#a78bfa">Power ${esc(pLoStr)}–${esc(pHiStr)}</span>`);
    if (!showHr) {
      lHi = pHiStr; lLo = pLoStr; lCol = "#a78bfa";
    }
  }
  $("nkHistHr").innerHTML = hrSvg;
  nkSetAxes("nkHistHr", lHi, lLo, rHi, rLo, lCol, rCol);
  $("nkHistHrRange").innerHTML = rangeParts.join(" · ") || "Hidden";

  // Chart 2: Temperatures (ASIC / VR, same unit mirrored on both sides)
  const tIdxs = [];
  if (ser.asic) tIdxs.push(2);
  if (ser.vr) tIdxs.push(3);
  const tpts = (typeof nkUnitF === "function" && nkUnitF())
    ? pts.map(p => [p[0], p[1], p[2] == null ? null : p[2] * 9 / 5 + 32, p[3] == null ? null : p[3] * 9 / 5 + 32])
    : pts;
  if (tIdxs.length) {
    const [tl0, th0] = nkRange(tpts, tIdxs);
    let tSvg = "";
    if (ser.asic) tSvg += `<path d="${nkPath(tpts, 2, tl0, th0)}" fill="none" stroke="#ff9f43" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
    if (ser.vr) tSvg += `<path d="${nkPath(tpts, 3, tl0, th0)}" fill="none" stroke="#6cb8ff" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
    $("nkHistT").innerHTML = tSvg;
    const uDeg = (typeof nkUnitF === "function" && nkUnitF()) ? "°F" : "°C";
    const tHiS = th0.toFixed(1) + uDeg, tLoS = tl0.toFixed(1) + uDeg;
    nkSetAxes("nkHistT", tHiS, tLoS, tHiS, tLoS);
    const tl = (typeof nkUnitF === "function" && nkUnitF()) ? (tl0 - 32) * 5 / 9 : tl0;
    const th = (typeof nkUnitF === "function" && nkUnitF()) ? (th0 - 32) * 5 / 9 : th0;
    $("nkHistTRange").textContent = (typeof nkTR === "function") ? nkTR(tl, th) : (tLoS + "–" + tHiS);
  } else {
    $("nkHistT").innerHTML = "";
    nkSetAxes("nkHistT", "—", "—", "—", "—");
    $("nkHistTRange").textContent = "Hidden";
  }

  $("nkHistStart").textContent = nkWhen(pts[0][0]);
  $("nkHistEnd").textContent = nkWhen(pts[pts.length - 1][0]);
  $("nkHistInfo").textContent = pts.length + " points" + (pts.length !== allPts.length ? ` (zoomed from ${allPts.length})` : "");
}

// Override nkLoadHist to populate NK.histRawPts and render via nkRenderHistoryCharts
async function nkLoadHist() {
  if (!isFieldVisible("long_history")) return;
  const now = Date.now();
  if (now - NK.histAt < 60000) return;
  NK.histAt = now;
  document.querySelectorAll("#nkHistSeg button[data-r]").forEach(b => b.classList.toggle("on", b.dataset.r === NK.histRange));
  try {
    const r = await fetch(`/api/history?device=${encodeURIComponent(NK.histDevice === "__fleet__" ? "" : NK.histDevice)}&range=${NK.histRange}`);
    const data = await r.json();
    NK.histRawPts = data.points || [];
    nkRenderHistoryCharts();
  } catch (e) { $("nkHistInfo").textContent = "History unavailable: " + e; }
}

// Override sparkline rendering with dual-side Y-axis gutters + unit-kind support
function renderSparklineSvg(points, idx, color, opts) {
  const o = opts || {};
  const vals = (points || []).map(p => p[idx]).filter(v => v != null && !Number.isNaN(Number(v))).map(Number);
  if (vals.length < 2) {
    return `<div style="color:var(--muted);font-size:.71rem;padding:8px 0">Collecting 24h samples…</div>`;
  }
  const w = 160, h = 34, pad = 2;
  let mn = Math.min(...vals), mx = Math.max(...vals);
  if (o.nonNeg && mn < 0) mn = 0;
  if (mx - mn < 1e-6) { mn = o.nonNeg ? Math.max(0, mn - 1) : mn - 1; mx += 1; }
  const coords = vals.map((v, i) => {
    const x = pad + (i / (vals.length - 1)) * (w - 2 * pad);
    const y = h - pad - ((v - mn) / (mx - mn)) * (h - 2 * pad);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const loLbl = o.fmt ? o.fmt(mn) : mn.toFixed(0);
  const hiLbl = o.fmt ? o.fmt(mx) : mx.toFixed(0);
  return `<div class="nk-chart-frame nk-spark-frame">
    <div class="nk-yaxis nk-yaxis-left"><span>${esc(hiLbl)}</span><span>${esc(loLbl)}</span></div>
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <polyline fill="none" stroke="${color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" points="${coords.join(" ")}"/>
    </svg>
    <div class="nk-yaxis nk-yaxis-right"><span>${esc(hiLbl)}</span><span>${esc(loLbl)}</span></div>
  </div>`;
}

function renderSparklines(d) {
  const hist = d.history || [];
  const hrVals = hist.map(p => p[1]).filter(v => v != null);
  const tVals = hist.map(p => p[2]).filter(v => v != null);
  const ukind = d.hashrate_unit_kind || "hash";
  const fmtHr = v => fmtCompactAxisHashrate(v, ukind);
  const fmtTmp = v => (typeof nkT === "function") ? nkT(v, 0) : (Number(v).toFixed(0) + "°C");
  const hrRange = hrVals.length >= 2 ? `${fmtHr(Math.min(...hrVals))}–${fmtHr(Math.max(...hrVals))}` : "";
  const tRange = tVals.length >= 2 ? ((typeof nkTR === "function") ? nkTR(Math.min(...tVals), Math.max(...tVals)) : `${Math.min(...tVals).toFixed(0)}–${Math.max(...tVals).toFixed(0)}°C`) : "";
  return `<div class="spark-grid">
    <div class="spark-box">
      <div class="spark-head"><span>24h Hashrate</span><span>${esc(hrRange)}</span></div>
      ${renderSparklineSvg(hist, 1, "#3ee0b4", {nonNeg: true, fmt: fmtHr})}
    </div>
    <div class="spark-box">
      <div class="spark-head"><span>24h Chip Temp</span><span>${esc(tRange)}</span></div>
      ${renderSparklineSvg(hist, 2, "#f0c14a", {nonNeg: false, fmt: fmtTmp})}
    </div>
  </div>`;
}

/* ---------- Compatibility Capture UI ---------- */
function nkShowCompatResult(res) {
  const modal = $("nkCompatModal"), title = $("nkCompatTitle"), body = $("nkCompatBody");
  if (!modal || !body) return;
  title.textContent = "Compatibility Capture — " + (res.filename || "saved");
  const counts = res.before_counts || {};
  const entries = Object.entries(counts);
  const countRows = entries.length
    ? entries.map(([k, v]) => `<div class="nk-redact-item"><span>${esc(k.replace(/_/g, " "))}</span><b>${esc(v)}</b></div>`).join("")
    : `<div class="sub">No personal data patterns detected in captured responses.</div>`;
  const cleanBadge = res.after_scan_clean
    ? `<span class="badge ok">POST-SAVE ZIP SCAN CLEAN (0 leaks across ${esc(res.files_scanned)} files)</span>`
    : `<span class="badge err">POST-SAVE SCAN FLAGGED LEAKS</span>`;
  body.innerHTML = `
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px">
      ${cleanBadge}
      <span class="sub">Saved in <code>userdata/compat-captures/${esc(res.filename)}</code> (${esc((Number(res.size_bytes || 0) / 1024).toFixed(1))} KB)</span>
    </div>
    <div class="sub">Reachable ports: <b>${esc((res.reachable_ports || []).join(", ") || "none")}</b> · Total redacted items: <b>${esc(res.redacted_total ?? 0)}</b></div>
    <h4 style="margin:12px 0 4px">Redaction Summary (by type)</h4>
    <div class="nk-redact-grid">${countRows}</div>
    <div class="row-actions" style="margin-top:12px">
      ${res.download_url ? `<a class="btn start" style="text-decoration:none;text-align:center" href="${esc(res.download_url)}" download="${esc(res.filename)}">Download Redacted .zip</a>` : ""}
      <button class="icon-btn" type="button" onclick="$('nkCompatModal').hidden=true">Close</button>
    </div>`;
  modal.hidden = false;
}

async function nkRunCompatCapture(deviceName) {
  const modal = $("nkCompatModal"), title = $("nkCompatTitle"), body = $("nkCompatBody");
  if (modal && body) {
    title.textContent = "Compatibility Capture — " + deviceName;
    body.innerHTML = `<div class="sub">Probing read-only miner APIs and scrubbing personal data…</div>`;
    modal.hidden = false;
  }
  try {
    const res = await post("/api/nk/compat-capture", {device: deviceName});
    nkShowCompatResult(res);
  } catch (e) {
    if (body) body.innerHTML = `<div class="err" style="padding:0">Capture failed: ${esc(e.message)}</div>`;
  }
}

async function nkRunCompatCaptureIp() {
  const ipEl = $("nkCompatIp");
  const ip = ipEl ? ipEl.value.trim() : "";
  if (!ip) { alert("Enter a miner IP (or ip:port) first."); return; }
  const modal = $("nkCompatModal"), title = $("nkCompatTitle"), body = $("nkCompatBody");
  if (modal && body) {
    title.textContent = "Compatibility Capture — " + ip;
    body.innerHTML = `<div class="sub">Probing read-only APIs on ${esc(ip)} and scrubbing personal data…</div>`;
    modal.hidden = false;
  }
  try {
    const res = await post("/api/nk/compat-capture", {ip});
    nkShowCompatResult(res);
  } catch (e) {
    if (body) body.innerHTML = `<div class="err" style="padding:0">Capture failed: ${esc(e.message)}</div>`;
  }
}

/* ---------- Tier-1 Adapter Opt-In Controls ---------- */
async function nkToggleWriteEnabled(deviceName, checked) {
  try {
    await post("/api/device/" + encodeURIComponent(deviceName) + "/adapter", {write_enabled: !!checked});
    await refresh();
  } catch (e) { alert("Failed: " + e.message); }
}

async function nkAdapterAction(deviceName, action, params) {
  try {
    const res = await post("/api/device/" + encodeURIComponent(deviceName) + "/adapter-action", {action, params: params || {}});
    cardNotice["adp:" + deviceName] = {cls: "ok", text: res.message || (action + " applied")};
    await refresh();
  } catch (e) {
    cardNotice["adp:" + deviceName] = {cls: "err", text: e.message};
    await refresh();
  }
}

async function nkSaveBitmainCreds(ev, deviceName) {
  ev.preventDefault();
  const key = domKey(deviceName);
  const u = ($("adp_wu_" + key) || {}).value || "";
  const p = ($("adp_wp_" + key) || {}).value || "";
  try {
    await post("/api/device/" + encodeURIComponent(deviceName) + "/adapter", {web_username: u, web_password: p});
    cardNotice["adp:" + deviceName] = {cls: "ok", text: "Saved user web credentials"};
    await refresh();
  } catch (e) {
    cardNotice["adp:" + deviceName] = {cls: "err", text: e.message};
  }
  return false;
}

function nkRenderAdapterBox(d) {
  if (d.type !== "antminer") return "";
  const fam = (d.family || "").toLowerCase();
  if (!fam || fam === "braiins") return "";
  const key = domKey(d.name);
  const wr = !!d.write_enabled;
  const notice = cardNotice["adp:" + d.name];
  const noticeHtml = notice ? `<div class="mini-note ${notice.cls}">${esc(notice.text)}</div>` : "";
  const dis = wr ? "" : "disabled";
  let ctrlHtml = "";
  if (fam === "avalon") {
    ctrlHtml = `<div class="nk-adp-row">
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','workmode',{mode:'0'})">Eco</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','workmode',{mode:'1'})">Standard</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','workmode',{mode:'2'})">Super</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','soft_off')">Sleep</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','soft_on')">Wake</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','reboot')">Reboot</button>
    </div>`;
  } else if (fam === "vnish") {
    ctrlHtml = `<div class="nk-adp-row">
      <input id="adp_vthr_${key}" type="number" min="20" max="100" step="5" value="100" style="width:68px" ${dis}>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','throttle',{pct:Number($('adp_vthr_${key}').value)})">Throttle %</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','pause')">Pause</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','resume')">Resume</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','reboot')">Reboot</button>
    </div>`;
  } else if (fam === "luxos") {
    ctrlHtml = `<div class="nk-adp-row">
      <input id="adp_lprof_${key}" type="text" placeholder="Profile name" ${dis}>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','profileset',{profile:$('adp_lprof_${key}').value})">Apply profile</button>
    </div>`;
  } else if (fam === "bitmain-stock") {
    ctrlHtml = `<form class="nk-adp-row" onsubmit="return nkSaveBitmainCreds(event, '${esc(d.name)}')">
      <input id="adp_wu_${key}" type="text" placeholder="Digest user (e.g. root)" value="${esc(d.web_username || "")}" style="width:120px">
      <input id="adp_wp_${key}" type="password" placeholder="${d.web_has_password ? '•••••• (saved)' : 'Web password'}" style="width:120px">
      <button class="icon-btn" type="submit">Save login</button>
    </form>
    <div class="nk-adp-row">
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','workmode',{mode:'normal'})">Normal</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','workmode',{mode:'low-power'})">Low Power</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','workmode',{mode:'sleep'})">Sleep</button>
      <button class="icon-btn" type="button" ${dis} onclick="nkAdapterAction('${esc(d.name)}','reboot')">Reboot</button>
    </div>`;
  }
  return `<div class="nk-adp-box">
    <div class="nk-adp-head">
      <span><b>${esc(fam.toUpperCase())}</b> · <span>${esc(d.algorithm || "sha256d")}</span></span>
      <label class="field-check"><input type="checkbox" ${wr ? "checked" : ""} onchange="nkToggleWriteEnabled('${esc(d.name)}', this.checked)"><span>Allow write controls</span></label>
    </div>
    ${ctrlHtml}${noticeHtml}
  </div>`;
}
"""


def inject113(html: str) -> str:
    # 1. Inject CSS before </style>
    assert "</style>" in html
    html = html.replace("</style>", NK113_CSS + "\n</style>", 1)

    # 2. Wrap #nkHistHr & #nkHistT with left/right Y-axis gutters + series toggles + zoom bar
    old_hist_hr = '<div class="nk-chart-label"><span>Hashrate</span><span id="nkHistHrRange"></span></div>\n    <svg class="nk-chart" id="nkHistHr" viewBox="0 0 1000 150" preserveAspectRatio="none"></svg>'
    new_hist_hr = '''<div class="nk-chart-label"><span>Hashrate</span><span id="nkHistHrRange"></span></div>
    <div class="nk-chart-tools">
      <button class="nk-pill-tog on" type="button" data-s="hr" onclick="nkToggleSeries('hr')">Hashrate</button>
      <button class="nk-pill-tog on" type="button" data-s="avg24" onclick="nkToggleSeries('avg24')">24h Avg</button>
      <button class="nk-pill-tog" type="button" id="nkTogPower" data-s="power" onclick="nkToggleSeries('power')" style="display:none">Power (W)</button>
      <div class="nk-zoom-bar">
        <span>Zoom:</span>
        <input id="nkZoomStart" type="range" min="0" max="95" value="0" oninput="nkSetZoom('start', this.value)">
        <input id="nkZoomEnd" type="range" min="5" max="100" value="100" oninput="nkSetZoom('end', this.value)">
        <span id="nkZoomLabel">100%</span>
        <button class="icon-btn" type="button" onclick="nkResetZoom()">Reset</button>
      </div>
    </div>
    <div class="nk-chart-frame">
      <div class="nk-yaxis nk-yaxis-left"><span id="nkHistHrLHi">—</span><span id="nkHistHrLLo">—</span></div>
      <svg class="nk-chart" id="nkHistHr" viewBox="0 0 1000 150" preserveAspectRatio="none"></svg>
      <div class="nk-yaxis nk-yaxis-right"><span id="nkHistHrRHi">—</span><span id="nkHistHrRLo">—</span></div>
    </div>'''
    assert old_hist_hr in html, "Missing #nkHistHr block"
    html = html.replace(old_hist_hr, new_hist_hr, 1)

    old_hist_t = '<div class="nk-chart-label"><span>Temperatures (<span style="color:#ff9f43">ASIC</span> / <span style="color:#6cb8ff">VR</span>)</span><span id="nkHistTRange"></span></div>\n    <svg class="nk-chart" id="nkHistT" viewBox="0 0 1000 150" preserveAspectRatio="none"></svg>'
    new_hist_t = '''<div class="nk-chart-label"><span>Temperatures (<span style="color:#ff9f43">ASIC</span> / <span style="color:#6cb8ff">VR</span>)</span><span id="nkHistTRange"></span></div>
    <div class="nk-chart-tools">
      <button class="nk-pill-tog on" type="button" data-s="asic" onclick="nkToggleSeries('asic')">ASIC</button>
      <button class="nk-pill-tog on" type="button" data-s="vr" onclick="nkToggleSeries('vr')">VR</button>
    </div>
    <div class="nk-chart-frame">
      <div class="nk-yaxis nk-yaxis-left"><span id="nkHistTLHi">—</span><span id="nkHistTLLo">—</span></div>
      <svg class="nk-chart" id="nkHistT" viewBox="0 0 1000 150" preserveAspectRatio="none"></svg>
      <div class="nk-yaxis nk-yaxis-right"><span id="nkHistTRHi">—</span><span id="nkHistTRLo">—</span></div>
    </div>'''
    assert old_hist_t in html, "Missing #nkHistT block"
    html = html.replace(old_hist_t, new_hist_t, 1)

    # 3. Add arbitrary-IP Compatibility capture control inside #scanPanel
    scan_anchor = '    <div class="scan-list" id="scanList"></div>\n  </section>'
    scan_add = '''    <div class="nk-compat-bar">
      <strong>Unknown / Custom Miner:</strong>
      <input id="nkCompatIp" type="text" placeholder="IP or ip:port (e.g. 192.168.1.50)">
      <button class="toggle" id="nkCompatIpBtn" type="button" onclick="nkRunCompatCaptureIp()">Compatibility capture</button>
      <span class="sub">Read-only probe + redacted .zip saved to <code>userdata/compat-captures/</code></span>
    </div>
    <div class="scan-list" id="scanList"></div>
  </section>'''
    assert scan_anchor in html
    html = html.replace(scan_anchor, scan_add, 1)

    # 4. Add Compatibility capture button on BOTH Antminer & AxeOS card row-actions + Adapter box on Antminer cards
    ant_btn = '<button class="btn start" disabled title="Monitor-only: Antminer autotune is disabled in this app">Start autotune (monitor only)</button>'
    html = html.replace(
        ant_btn,
        ant_btn + '\n        <button class="icon-btn" type="button" onclick="nkRunCompatCapture(\'${esc(d.name)}\')">Compatibility capture</button>',
        1,
    )
    axe_btn = '<button class="btn stop" ${enabled ? "" : "disabled"} onclick="setDevice(\'${esc(d.name)}\', false)">Stop autotune</button>'
    html = html.replace(
        axe_btn,
        axe_btn + '\n      <button class="icon-btn" type="button" onclick="nkRunCompatCapture(\'${esc(d.name)}\')">Compatibility capture</button>',
        1,
    )
    html = html.replace("${bosSection}", "${bosSection}${nkRenderAdapterBox(d)}", 1)

    # 5. Update antminer/axeos card hashrate formatting to respect hashrate_unit_kind (Sol/s vs H/s)
    html = html.replace("fmtHashrate(d.hashrate_ghs)", "fmtHashrateUnit(d.hashrate_ghs, d.hashrate_unit_kind, false)")

    # 6. Insert modal + JS before closing </script> / </body>
    assert "</body>" in html
    html = html.replace("</body>", NK113_COMPAT_MODAL + "\n</body>", 1)
    assert "\nrefresh();\nsetInterval(refresh, 3000);\n</script>" in html
    html = html.replace(
        "\nrefresh();\nsetInterval(refresh, 3000);\n</script>",
        NK113_JS + "\nrefresh();\nsetInterval(refresh, 3000);\n</script>",
        1,
    )
    return html
