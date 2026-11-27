"""v1.17.1 tests: dashboard background (asset, stylesheet, setting), version footer, version reporting under a live
update, and the v1.17.1 live-update package for v1.17.0."""

from __future__ import annotations

import base64
import io
import json
import re
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path

import autotune
import dashboard
import miner_hearth
import nk_ui1171
import nk_update

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools"))
import build_v1171_update  # noqa: E402

JPEG = ROOT / "assets" / "dashboard-bg.jpg"


class BackgroundAssetTests(unittest.TestCase):
    def test_asset_is_a_small_jpeg_without_metadata(self):
        data = JPEG.read_bytes()
        self.assertTrue(data.startswith(b"\xff\xd8\xff"))
        self.assertLessEqual(len(data), 400 * 1024)
        self.assertNotIn(b"Exif", data[:4096])

    def test_stylesheet_embeds_image_with_overlay_and_off_switch(self):
        css = nk_ui1171.background_css(JPEG.read_bytes())
        self.assertIn("data:image/jpeg;base64," + base64.b64encode(JPEG.read_bytes()).decode()[:64], css)
        self.assertEqual(css.count("data:image/jpeg"), 1, "image embedded once")
        self.assertEqual(css.count("var(--mh-bg-image) center / cover no-repeat fixed"), 2)
        self.assertLess(len(css.encode()), 400 * 1024)
        self.assertIn("body:not(.mh-bg-off):not(.theme-contrast):not(.theme-light)", css)
        self.assertIn("body.theme-light:not(.mh-bg-off)", css)
        self.assertIn("linear-gradient(rgba(10, 8, 6, .45)", css)
        self.assertIn("cover no-repeat fixed", css)
        self.assertIn("--bg-card: rgba(", css)
        self.assertNotIn("theme-contrast:not", css)  # contrast is only ever excluded

    def test_program_serves_the_bundled_stylesheet(self):
        state = types.SimpleNamespace(hub=None)
        code, body, ctype = dashboard.handle_dashboard_request("GET", "/mh/background.css", b"", state)
        self.assertEqual((code, ctype.split(";")[0]), (200, "text/css"))
        self.assertEqual(body, nk_ui1171.bundled_background_css(ROOT))
        self.assertIsNone(nk_ui1171.bundled_background_css(ROOT / "no-such-dir"))


class DashboardUiTests(unittest.TestCase):
    def test_setting_footer_and_hooks_in_dashboard(self):
        html = dashboard.DASHBOARD_HTML
        self.assertIn('<input type="checkbox" id="nk_ui_bg" onchange="nk1171SaveBg()"><span>Dashboard background</span>', html)
        box = html[html.index('id="nkSetAppearance"'):]
        self.assertLess(box.index('id="nk_ui_bg"'), box.index('id="nkSetLayout"'), "toggle sits in the Appearance box")
        self.assertIn(f'<footer class="mh-footer" id="mhFooter">Miner Hearth <span id="mhFooterVersion">v{autotune.APP_VERSION}</span></footer>', html)
        self.assertIn('if (typeof nk1171Render === "function") nk1171Render(data);', html)
        self.assertIn('href="/mh/background.css"', html)
        self.assertIn("function nk1171ApplyBg", html)
        self.assertNotIn("__APP_VERSION__", html)
        self.assertLess(html.index("function nk1171SaveBg"), html.index("\nrefresh();\nsetInterval(refresh, 3000);"))

    def test_injection_fails_loudly_on_missing_anchor(self):
        with self.assertRaises(RuntimeError):
            nk_ui1171.inject1171("<html></html>")


class SettingTests(unittest.TestCase):
    def test_default_on_and_normalised(self):
        self.assertIs(miner_hearth.normalize_app_settings({})["ui"]["background"], True)
        self.assertIs(miner_hearth.normalize_app_settings({"ui": {"background": False}})["ui"]["background"], False)
        self.assertIs(miner_hearth.normalize_app_settings({"ui": {"background": "off"}})["ui"]["background"], False)
        self.assertIs(miner_hearth.normalize_app_settings({"ui": {"background": None}})["ui"]["background"], True)

    def test_persisted_and_kept_when_theme_changes(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            ud = Path(td) / "userdata"
            hub = miner_hearth.FeatureHub(ud, fetch=lambda u, t=8: b"")
            hub.update_settings({"ui": {"background": False}})
            hub.update_settings({"ui": {"theme": "light", "temp_unit": "F"}})  # what the theme / unit selects send
            self.assertEqual(hub.settings.get()["ui"], {"theme": "light", "temp_unit": "F", "background": False})
            hub2 = miner_hearth.FeatureHub(ud, fetch=lambda u, t=8: b"")
            self.assertIs(hub2.settings.get()["ui"]["background"], False)


class VersionTests(unittest.TestCase):
    def test_program_version_matches_version_file(self):
        self.assertEqual(autotune.PROGRAM_VERSION, (ROOT / "VERSION").read_text(encoding="utf-8").strip())
        self.assertEqual(autotune.APP_VERSION, "1.17.1")

    def test_running_version_is_newer_of_code_and_install(self):
        vt = autotune._version_tuple
        self.assertEqual(max("1.17.0", "1.17.1", key=vt), "1.17.1")
        self.assertEqual(max("1.18.0", "1.17.1", key=vt), "1.18.0")
        self.assertGreater(vt("1.17.10"), vt("1.17.9"))

    def test_update_manager_uses_installed_version(self):
        old = autotune.SHIPPED_VERSION
        try:
            autotune.SHIPPED_VERSION = "1.17.0"   # v1.17.0 install running the v1.17.1 live update
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                hub = miner_hearth.FeatureHub(Path(td) / "userdata", fetch=lambda u, t=8: b"")
                self.assertEqual(hub.updates.app_version, "1.17.0")
        finally:
            autotune.SHIPPED_VERSION = old


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.zip = Path(cls.tmp.name) / "update.zip"
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            rc = build_v1171_update.main([str(cls.zip)])
        assert rc == 0
        cls.data = cls.zip.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_contents_are_allowed_and_small(self):
        with zipfile.ZipFile(io.BytesIO(self.data)) as z:
            names = sorted(z.namelist())
            man = json.loads(z.read("manifest.json")) if "manifest.json" in names else {}
            css = z.read("ui/mh-background.css")
        self.assertIn("ui/mh-background.css", names)
        for n in ("app/autotune.py", "app/dashboard.py", "app/miner_hearth.py", "app/nk_ui1171.py", "NOTES.md",
                  "plugins/mh_restart_fix.py"):
            self.assertIn(n, names)
        self.assertLess(len(css), 400 * 1024)
        self.assertLess(len(self.data), 1024 * 1024)
        if man:
            self.assertEqual(man.get("version"), "1.17.1")

    def test_no_private_identifiers(self):
        with zipfile.ZipFile(io.BytesIO(self.data)) as z:
            blob = b"".join(z.read(n) for n in z.namelist())
        # the only Bitcoin address shipped is the public donate address; no other coin addresses, no MACs
        self.assertEqual(set(re.findall(rb"\bbc1[02-9ac-hj-np-z]{20,}", blob)), {b"bc1q3m8usp6mffv4txhwcltq9lu9jltx02eyukgrvf"})
        self.assertNotIn(b"ecash:q", blob.lower())
        self.assertFalse(re.search(rb"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b", blob))

    def test_applies_on_v1170_then_rolls_back(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            hub = miner_hearth.FeatureHub(Path(td) / "userdata", fetch=lambda u, t=8: b"")
            hub.updates.app_version = "1.17.0"
            hub.update_settings({"updates": {"enabled": True}})

            def post(route, body):
                code, raw, _ = hub.handle("POST", route, "", json.dumps(body).encode(), False)
                return code, json.loads(raw)
            code, r = post("/api/nk/update/check", {"b64": base64.b64encode(self.data).decode()})
            self.assertEqual(code, 200, r)
            code, a = post("/api/nk/update/apply", {"token": r["report"]["token"]})
            self.assertEqual(code, 200, a)
            self.assertTrue(a["restart_required"])
            st = json.loads(hub.handle("GET", "/api/nk/update/status", "", b"", False)[1])
            self.assertEqual(st["current"]["version"], "1.17.1")
            self.assertIn("mh-background.css", [u["name"] for u in st["ui_assets"]])
            code, body, ctype = hub.handle("GET", "/api/nk/update/ui/mh-background.css", "", b"", False)
            self.assertEqual((code, ctype.split(";")[0]), (200, "text/css"))
            self.assertIn(b"data:image/jpeg;base64,", body)
            code, r = post("/api/nk/update/rollback", {})
            self.assertEqual(code, 200, r)
            self.assertIn("1.17.0", str(r["now"]))
            self.assertEqual(hub.handle("GET", "/api/nk/update/ui/mh-background.css", "", b"", False)[0], 404)


class RestartFlagTests(unittest.TestCase):
    def test_new_process_and_quick_restart_clear_saved_flag(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            ud = Path(td) / "userdata"
            ud.mkdir()
            nk_update._write_state(ud, {"enabled": True, "restart_pending": True})
            hub = miner_hearth.FeatureHub(ud, fetch=lambda u, t=8: b"")
            self.assertFalse(nk_update._read_state(ud)["restart_pending"])
            self.assertFalse(json.loads(hub.handle("GET", "/api/nk/update/status", "", b"", False)[1])["restart_pending"])
            hub.update_settings({"updates": {"enabled": True}})
            nk_update._write_state(ud, dict(nk_update._read_state(ud), restart_pending=True))
            calls = []
            hub.restart_hook = lambda: calls.append(1)
            code, raw, _ = hub.handle("POST", "/api/nk/update/restart", "", b"{}", False)
            self.assertEqual(code, 200, raw)
            self.assertFalse(nk_update._read_state(ud)["restart_pending"])


class QuickRestartFixTests(unittest.TestCase):
    PLUGIN = ROOT / "tools" / "v1171_plugins" / "mh_restart_fix.py"

    def _run_plugin(self, frozen):
        import os
        saved = os.environ.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
        had = hasattr(sys, "frozen")
        try:
            if frozen:
                sys.frozen = True
            ns: dict = {}
            exec(compile(self.PLUGIN.read_text(encoding="utf-8"), str(self.PLUGIN), "exec"), ns)
            return ns, os.environ.get("PYINSTALLER_RESET_ENVIRONMENT")
        finally:
            if frozen and not had:
                del sys.frozen
            os.environ.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
            if saved is not None:
                os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = saved

    def test_plugin_sets_reset_only_when_frozen_and_has_no_adapters(self):
        ns, val = self._run_plugin(frozen=True)
        self.assertEqual((ns["ADAPTERS"], val), ([], "1"))
        ns, val = self._run_plugin(frozen=False)
        self.assertIsNone(val)

    def test_plugin_is_a_valid_update_path_and_loads(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "mh_restart_fix.py").write_bytes(self.PLUGIN.read_bytes())
            self.assertEqual(nk_update.load_plugins(d), [])
        nk_update.load_plugins(None)

    def test_desktop_restart_passes_reset_env(self):
        src = (ROOT / "miner_dashboard.py").read_text(encoding="utf-8")
        self.assertIn('env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")', src)
        self.assertIn("close_fds=True, env=env)", src)
        self.assertIn('os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"', (ROOT / "miner_hearth.py").read_text(encoding="utf-8"))


class PublishKitTests(unittest.TestCase):
    STORE = ROOT / "umbrel" / "store"

    def test_workflow_builds_multi_arch_to_lowercase_owner(self):
        wf = (ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
        self.assertIn('tags: ["v*"]', wf)
        self.assertIn("workflow_dispatch:", wf)
        self.assertRegex(wf, r"permissions:\n  contents: read\n  packages: write")
        self.assertIn("tr '[:upper:]' '[:lower:]'", wf)
        self.assertIn("${{ github.repository_owner }}", wf)
        self.assertIn('echo "image=ghcr.io/${owner}/miner-hearth"', wf)
        self.assertIn("platforms: linux/amd64,linux/arm64", wf)
        self.assertIn("org.opencontainers.image.source=https://github.com/${{ github.repository }}", wf)
        self.assertIn("${{ steps.build.outputs.digest }}", wf)
        self.assertIn("GITHUB_STEP_SUMMARY", wf)
        self.assertNotIn("\t", wf)
        # the owner Miner-Hearth becomes the image path GHCR requires
        self.assertEqual("Miner-Hearth".lower(), "miner-hearth")

    def test_store_ids_and_filled_values(self):
        import re as _re
        store = (self.STORE / "umbrel-app-store.yml").read_text(encoding="utf-8")
        sid = _re.search(r'^id: "([a-z-]+)"', store, _re.M).group(1)
        apps = [d for d in self.STORE.iterdir() if d.is_dir()]
        self.assertEqual([d.name for d in apps], ["minerhearth-miner-hearth"])
        for d in apps:
            self.assertTrue(d.name.startswith(sid + "-"), "app ids must start with the store id")
            app = (d / "umbrel-app.yml").read_text(encoding="utf-8")
            dc = (d / "docker-compose.yml").read_text(encoding="utf-8")
            self.assertIn(f"id: {d.name}\n", app)
            self.assertIn(f"APP_HOST: {d.name}_web_1", dc)
            self.assertIn('version: "1.17.1"', app)
            self.assertIn("    image: ghcr.io/miner-hearth/miner-hearth:1.17.1\n", dc)
            self.assertIn("https://raw.githubusercontent.com/Miner-Hearth/Miner-Hearth-ASIC-Dashboard-Umbrel-Store/main/minerhearth-miner-hearth/icon.png", app)
            self.assertIn("repo: https://github.com/Miner-Hearth/Miner-Hearth-ASIC-Dashboard\n", app)
            self.assertIn("submission: https://github.com/Miner-Hearth/Miner-Hearth-ASIC-Dashboard-Umbrel-Store", app)
            for text in (app, store):
                self.assertNotRegex(text, r"<[a-z-]+>", "no placeholders left")
            self.assertTrue((d / "icon.png").read_bytes().startswith(b"\x89PNG"))
            self.assertGreater((d / "data" / ".gitkeep").stat().st_size, 0, "non-empty so a web upload keeps it")


class PackagingTests(unittest.TestCase):
    def test_spec_and_docker_ship_the_asset(self):
        spec = (ROOT / "packaging" / "MinerHearth.spec").read_text(encoding="utf-8")
        self.assertIn('(os.path.join(ROOT, "assets", "dashboard-bg.jpg"), "assets")', spec)
        self.assertIn('"nk_ui1171"', spec)
        self.assertIn('"test_v1171"', spec)
        self.assertIn("COPY --chown=0:0 assets ./assets", (ROOT / "Dockerfile").read_text(encoding="utf-8"))

    def test_changelog_entry(self):
        self.assertTrue(re.search(r"^## v1\.17\.1 \(", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M))


if __name__ == "__main__":
    unittest.main()
