"""Miner Hearth v1.17.1 dashboard additions: the firelit stone-wall background (Settings > Appearance >
"Dashboard background", on by default) and a footer label with the running version.

The background itself is a stylesheet with the JPEG embedded as a data: URI, so it can be delivered as a live-update
dashboard file (ui/mh-background.css; the v1.17.0 updater loads ui/*.css at once) and is also served by the program
from assets/dashboard-bg.jpg at /mh/background.css."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

BG_CSS_NAME = "mh-background.css"


def background_css(jpeg: bytes) -> str:
    """The background stylesheet. Off when <body> has class mh-bg-off; never in the high-contrast theme."""
    uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
    dark = "body:not(.mh-bg-off):not(.theme-contrast):not(.theme-light)"
    light = "body.theme-light:not(.mh-bg-off)"
    return f"""/* Miner Hearth v1.17.1: dashboard background (Settings > Appearance > Dashboard background) */
body {{ --mh-bg-image: url("{uri}"); }}
{dark} {{
  background: linear-gradient(rgba(10, 8, 6, .45), rgba(10, 8, 6, .62)), var(--mh-bg-image) center / cover no-repeat fixed;
  --bg-card: rgba(24, 27, 33, .88); --bg-soft: rgba(35, 40, 50, .82);
}}
{light} {{
  background: linear-gradient(rgba(243, 245, 248, .86), rgba(243, 245, 248, .92)), var(--mh-bg-image) center / cover no-repeat fixed;
  --bg-card: rgba(255, 255, 255, .9); --bg-soft: rgba(238, 241, 245, .88);
}}
"""


def bundled_background_css(*dirs: Path) -> Optional[bytes]:
    for d in dirs:
        f = Path(d) / "assets" / "dashboard-bg.jpg"
        try:
            if f.is_file():
                return background_css(f.read_bytes()).encode("utf-8")
        except OSError:
            continue
    return None


NK1171_SETTING_HTML = r"""
        <div class="nk-row"><label class="field-check"><input type="checkbox" id="nk_ui_bg" onchange="nk1171SaveBg()"><span>Dashboard background</span></label></div>
        <div class="mini-note">The firelit stone wall behind the dashboard (v1.17.1). Turn it off for a plain background; the high-contrast theme never uses it.</div>"""

NK1171_FOOTER_HTML = r"""
<footer class="mh-footer" id="mhFooter">Miner Hearth <span id="mhFooterVersion">v__APP_VERSION__</span></footer>
"""

NK1171_CSS = r"""
.mh-footer{margin:18px 24px 14px;text-align:right;font-size:.72rem;color:var(--muted);opacity:.85}
"""

NK1171_HEAD = r"""<link rel="stylesheet" href="/mh/background.css" id="mhBgCss">
<script>try{var u=JSON.parse(localStorage.getItem("nk_ui")||"null");if(u&&u.background===false)document.documentElement.classList.add("mh-bg-off-pending");}catch(e){}</script>
"""

NK1171_JS = r"""
/* ================= Miner Hearth v1.17.1: dashboard background + version footer ================= */
function nk1171ApplyBg(ui) {
  const on = !(ui && ui.background === false);
  document.body.classList.toggle("mh-bg-off", !on);
  document.documentElement.classList.remove("mh-bg-off-pending");
  const cb = $("nk_ui_bg"); if (cb && document.activeElement !== cb) cb.checked = on;
}
if (document.documentElement.classList.contains("mh-bg-off-pending")) document.body.classList.add("mh-bg-off");
function nk1171SaveBg() {
  const on = $("nk_ui_bg").checked;
  nk1171ApplyBg({background: on});
  nkSave({ui: {background: on}}, "nkLayoutNote", on ? "Dashboard background on" : "Dashboard background off");
}
function nk1171Render(data) {
  const nk = data && data.nk; if (!nk) return;
  nk1171ApplyBg(nk.settings && nk.settings.ui);
}
"""


def inject1171(html: str) -> str:
    """Apply the v1.17.1 UI on top of the v1.17.0 dashboard HTML."""
    def rep(old: str, new: str, count: int = 1) -> None:
        nonlocal html
        if old not in html:
            raise RuntimeError(f"v1.17.1 dashboard injection point missing: {old[:70]!r}")
        html = html.replace(old, new, count)

    rep('<div class="mini-note">Display only: limits, targets and the tuner always work in °C, so settings fields stay in °C.</div>',
        '<div class="mini-note">Display only: limits, targets and the tuner always work in °C, so settings fields stay in °C.</div>' + NK1171_SETTING_HTML)
    rep("</style>", NK1171_CSS + "</style>")
    rep("</head>", NK1171_HEAD + "</head>")
    rep("</body>", NK1171_FOOTER_HTML + "</body>")
    rep('  if (typeof nk117Render === "function") nk117Render(data);', '  if (typeof nk117Render === "function") nk117Render(data);\n  if (typeof nk1171Render === "function") nk1171Render(data);')
    rep("\nrefresh();\nsetInterval(refresh, 3000);\n</script>", NK1171_JS + "\nrefresh();\nsetInterval(refresh, 3000);\n</script>")
    return html
