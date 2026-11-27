#!/usr/bin/env python3
"""Tests for Miner Hearth v1.16: the simpler block alerts and live updates.

Synthetic data only: made-up addresses, machine and worker names, locally built update packages, and a
throw-away signing key (plus the public RFC 8032 Ed25519 test vector).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import tempfile
import types
import unittest
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import autotune
import nk_adapters
import nk_coins
import nk_math
import nk_update
import miner_hearth
from test_v1155_blocks import Base as BlockBase, MINER_ADDR, MINER_SCRIPT, axe_raw

SECRET = hashlib.sha256(b"miner-hearth-test-signing-key").digest()   # synthetic test key
PUBLIC = nk_update.ed25519_public_key(SECRET).hex()
OTHER_SECRET = hashlib.sha256(b"someone-else").digest()


class DifficultyFormatTests(unittest.TestCase):
    def test_like_best_share(self):
        cases = {2_345_678: "2.35M", 1_048_576: "1.05M", 6.3e9: "6.3G", 1.5e14: "150T", 2.5e15: "2.5P", 12_345: "12.3K",
                 999: "999", 1000: "1K", 0.004: "0.004", 0.9: "0.9", 0: "0"}
        for v, want in cases.items():
            self.assertEqual(nk_math.format_difficulty(v), want, v)
        self.assertEqual(nk_math.format_difficulty(None), "")


class BlockAlertFormatTests(BlockBase):
    def _block(self):
        cfg = self.cfg()
        self.poll(cfg, autotune.parse_gamma_info(axe_raw(1.0e8)))
        self.poll(cfg, autotune.parse_gamma_info(axe_raw(1.0e8)))
        self.chain.mine(1, pays=MINER_SCRIPT)
        self.poll(cfg, autotune.parse_gamma_info(axe_raw(2.9e10)))

    def test_block_found_then_block_confirmed_with_the_same_fields(self):
        self._block()
        found = self.block_msgs("Block Found")
        self.assertEqual(len(found), 1, self.discord())
        self.assertTrue(found[0].startswith("\u26CF\uFE0F **Block Found**\n"), found[0])   # height unknown yet: no number
        self.ticks()
        conf = self.block_msgs("Block Confirmed")
        self.assertEqual(len(conf), 1, self.discord())
        self.assertIn("**Block Confirmed #900001**", conf[0])
        for msg in (found[0], conf[0]):
            for line in ("Machine: Axe One", "Coin: XEC", f"Payout address: {MINER_ADDR}", "Worker: rig1",
                         "Network difficulty: 6.3G"):
                self.assertIn(line, msg)
            self.assertNotIn("29G", msg)   # v1.16.1: the session best that jumped is never shown as the share difficulty
            self.assertNotIn("30G", msg)   # nor the all-time best
        self.assertIn("Share difficulty: unknown", found[0])     # this firmware has no last-share difficulty
        self.assertIn("Share difficulty: 7.7G", conf[0])         # filled in from the block hash once the chain has it
        self.assertIn("explorer.e.cash/block-height/900001", conf[0])
        self.assertFalse([m for m in self.discord() if "possible" in m.lower()], "no 'possible block' wording any more")

    def test_test_button_uses_the_new_format(self):
        code, body, _ = self.hub.handle("POST", "/api/nk/test-webhook", "", b'{"kind":"block"}', False)
        self.assertEqual(code, 200, body)
        msg = self.discord()[-1]
        self.assertIn("**TEST: Block Found #123456**", msg)
        for line in ("Payout address:", "Worker:", "Share difficulty: 2.35M", "Network difficulty: 1.05M"):
            self.assertIn(line, msg)


def make_pkg(files: dict[str, bytes], version="1.16.1", min_app="1.16.0", max_app="", secret: bytes = b"",
             tamper: dict[str, bytes] | None = None, extra: dict[str, bytes] | None = None, man_patch: dict | None = None) -> bytes:
    man = {"format": "miner-hearth-update", "format_version": 1, "version": version, "min_app_version": min_app,
           "max_app_version": max_app, "title": "Synthetic test update", "notes": "",
           "files": {p: {"sha256": hashlib.sha256(b).hexdigest(), "size": len(b)} for p, b in files.items()}}
    man.update(man_patch or {})
    mb = json.dumps(man).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", mb)
        if secret:
            zf.writestr("manifest.sig", json.dumps({"public_key": nk_update.ed25519_public_key(secret).hex(),
                                                    "signature": nk_update.ed25519_sign(secret, mb).hex()}))
        for p, b in files.items():
            zf.writestr(p, (tamper or {}).get(p, b))
        for p, b in (extra or {}).items():
            zf.writestr(p, b)
    return buf.getvalue()


COINS_JSON = json.dumps({"coins": {"TSTC": {"label": "Test Coin", "unit": "TSTC", "algo": "sha256d", "unit_kind": "hash"}},
                         "pool_host_hints": [["tstc.", "TSTC"]], "block_time_sec": {"TSTC": 30}}).encode()
PLUGIN = b'''
class FakeAdapter:
    family_id = "fakeminer-test"
    def probe(self, ip, **kw):
        return None
ADAPTERS = [FakeAdapter()]
'''


class Ed25519Tests(unittest.TestCase):
    def test_rfc8032_vector_1(self):
        sk = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        pk = nk_update.ed25519_public_key(sk)
        self.assertEqual(pk.hex(), "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        sig = nk_update.ed25519_sign(sk, b"")
        self.assertEqual(sig.hex(), "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertTrue(nk_update.ed25519_verify(pk, b"", sig))
        self.assertFalse(nk_update.ed25519_verify(pk, b"x", sig))


class PackageVerifyTests(unittest.TestCase):
    def check(self, data, **kw):
        return nk_update.inspect_package(data, "1.16.0", kw.pop("keys", [PUBLIC]), **kw)

    def test_good_signed_package(self):
        r = self.check(make_pkg({"coins.json": COINS_JSON, "app/nk_math.py": b"X = 1\n"}, secret=SECRET), require_signature=True)
        self.assertEqual((r["version"], r["signature"], r["needs_restart"]), ("1.16.1", "trusted", True))
        self.assertEqual(r["hot"], ["coins"])

    def test_rejections(self):
        good = {"coins.json": COINS_JSON}
        bad = {
            "tampered file": make_pkg(good, tamper={"coins.json": COINS_JSON + b" "}),
            "unlisted file": make_pkg(good, extra={"ui/evil.js": b"1"}),
            "path traversal": make_pkg(good, extra={"../evil.py": b"1"}),
            "not updatable": make_pkg({"miner_dashboard.py": b"x=1"}),
            "updater itself": make_pkg({"app/nk_update.py": b"x=1"}),
            "bad python": make_pkg({"plugins/p.py": b"def (:"}),
            "bad coins": make_pkg({"coins.json": b'{"coins": {"x": 1}}'}),
            "too new for app": make_pkg(good, min_app="9.0"),
            "wrong format": make_pkg(good, man_patch={"format": "other"}),
            "not a zip": b"PK-nope",
        }
        for why, data in bad.items():
            with self.assertRaises(nk_update.UpdateError, msg=why):
                self.check(data)
        # broken signature: manifest changed after signing
        data = make_pkg(good, secret=SECRET)
        zin = zipfile.ZipFile(io.BytesIO(data))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zout:
            for n in zin.namelist():
                b = zin.read(n)
                zout.writestr(n, b.replace(b"Synthetic test update", b"Synthetic test updatX") if n == "manifest.json" else b)
        with self.assertRaises(nk_update.UpdateError):
            self.check(buf.getvalue())
        # signed by someone not trusted, or unsigned, when a signature is required
        for d in (make_pkg(good, secret=OTHER_SECRET), make_pkg(good)):
            with self.assertRaises(nk_update.UpdateError):
                self.check(d, require_signature=True)
        self.assertEqual(self.check(make_pkg(good, secret=OTHER_SECRET))["signature"], "untrusted")
        # expected package hash
        d = make_pkg(good)
        with self.assertRaises(nk_update.UpdateError):
            self.check(d, expected_sha256="00" * 32)
        self.assertTrue(self.check(d, expected_sha256=hashlib.sha256(d).hexdigest().upper()))

    def test_url_must_be_https(self):
        with self.assertRaises(nk_update.UpdateError):
            nk_update.fetch_url("http://updates.example/pkg.zip")


class LiveUpdateHubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ud = Path(self.tmp.name) / "userdata"
        self.hub = miner_hearth.FeatureHub(self.ud, fetch=lambda u, t=8: b"")
        self.hub.updates.app_version = "1.16.0"

    def tearDown(self):
        nk_update.apply_coins(None)
        nk_adapters.set_plugin_adapters([])
        self.tmp.cleanup()

    def post(self, route, body, remote=False):
        code, raw, _ = self.hub.handle("POST", route, "", json.dumps(body).encode(), remote)
        return code, json.loads(raw)

    def get(self, route):
        return self.hub.handle("GET", route, "", b"", False)

    def enable(self, **extra):
        self.hub.update_settings({"updates": dict({"enabled": True, "trusted_keys": [PUBLIC]}, **extra)})

    def apply(self, data):
        code, r = self.post("/api/nk/update/check", {"b64": base64.b64encode(data).decode()})
        self.assertEqual(code, 200, r)
        code, a = self.post("/api/nk/update/apply", {"token": r["report"]["token"]})
        self.assertEqual(code, 200, a)
        return a

    def test_off_by_default_and_local_only(self):
        self.assertFalse(miner_hearth.normalize_app_settings({})["updates"]["enabled"])
        data = base64.b64encode(make_pkg({"coins.json": COINS_JSON})).decode()
        code, r = self.post("/api/nk/update/check", {"b64": data})
        self.assertEqual(code, 403)
        self.enable()
        code, r = self.post("/api/nk/update/check", {"b64": data}, remote=True)
        self.assertEqual(code, 403)
        code, r = self.post("/api/nk/update/check", {"b64": data})
        self.assertEqual(code, 200, r)
        self.assertFalse((self.ud / "updates" / "current").exists(), "checking never applies")

    def test_hot_reload_coin_data_plugins_and_ui_then_rollback(self):
        self.enable()
        self.assertIsNone(nk_coins.host_hint("tstc.pool.example"))
        a = self.apply(make_pkg({"coins.json": COINS_JSON, "plugins/fake_test.py": PLUGIN, "ui/custom.css": b"body{outline:0}"},
                                secret=SECRET))
        self.assertFalse(a["restart_required"])
        self.assertEqual(nk_coins.host_hint("stratum+tcp://tstc.pool.example:3333"), "TSTC")
        self.assertIn("TSTC", nk_coins.COINS)
        self.assertEqual(miner_hearth.BLOCK_TIME_SEC["TSTC"], 30.0)
        self.assertIsNotNone(nk_adapters.get_adapter("fakeminer-test"))
        code, body, ctype = self.get("/api/nk/update/ui/custom.css")
        self.assertEqual((code, body, ctype.split(";")[0]), (200, b"body{outline:0}", "text/css"))
        st = json.loads(self.get("/api/nk/update/status")[1])
        self.assertEqual(st["current"]["version"], "1.16.1")
        self.assertEqual([u["name"] for u in st["ui_assets"]], ["custom.css"])
        # second update, then roll back to the first, then back to shipped
        self.apply(make_pkg({"coins.json": COINS_JSON.replace(b"Test Coin", b"Test Coin 2")}, version="1.16.2"))
        self.assertEqual(nk_coins.COINS["TSTC"]["label"], "Test Coin 2")
        self.assertIsNone(nk_adapters.get_adapter("fakeminer-test"))
        code, r = self.post("/api/nk/update/rollback", {})
        self.assertEqual((code, r["now"]), (200, "1.16.1"), r)
        self.assertEqual(nk_coins.COINS["TSTC"]["label"], "Test Coin")
        self.assertIsNotNone(nk_adapters.get_adapter("fakeminer-test"))
        code, r = self.post("/api/nk/update/remove", {})
        self.assertEqual(code, 200, r)
        self.assertNotIn("TSTC", nk_coins.COINS)
        self.assertIsNone(nk_coins.host_hint("tstc.pool.example"))
        self.assertEqual(self.get("/api/nk/update/ui/custom.css")[0], 404)

    def test_turning_updates_off_unloads_them(self):
        self.enable()
        self.apply(make_pkg({"coins.json": COINS_JSON}))
        self.hub.update_settings({"updates": {"enabled": False}})
        self.assertNotIn("TSTC", nk_coins.COINS)
        hub2 = miner_hearth.FeatureHub(self.ud, fetch=lambda u, t=8: b"")   # restart with updates off: still unloaded
        self.assertNotIn("TSTC", nk_coins.COINS)
        hub2.update_settings({"updates": {"enabled": True}})
        self.assertIn("TSTC", nk_coins.COINS)

    def test_program_files_need_a_quick_restart(self):
        self.enable()
        a = self.apply(make_pkg({"app/nk_overlay_probe.py": b"VALUE = 'from the update'\n"}))
        self.assertTrue(a["restart_required"])
        code, r = self.post("/api/nk/update/restart", {})
        self.assertEqual(code, 400)   # not running as the desktop app
        called = []
        self.hub.restart_hook = lambda: called.append(1)
        code, r = self.post("/api/nk/update/restart", {})
        self.assertEqual((code, r["restarting"]), (200, True))
        import time as _t
        for _ in range(30):
            if called:
                break
            _t.sleep(0.1)
        self.assertEqual(called, [1])

    def test_overlay_import_hook_and_crash_guard(self):
        self.enable()
        self.apply(make_pkg({"app/nk_overlay_probe.py": b"VALUE = 'from the update'\n"}))
        before = list(sys.meta_path)
        try:
            info = nk_update.install_overlay(self.ud)
            self.assertTrue(info["app_overlay"])
            sys.modules.pop("nk_overlay_probe", None)
            import nk_overlay_probe  # noqa: F401  (only exists inside the update)
            self.assertEqual(sys.modules["nk_overlay_probe"].VALUE, "from the update")
        finally:
            sys.meta_path[:] = before
            sys.modules.pop("nk_overlay_probe", None)
        # two starts that never reached "started OK": the third start skips the program files
        try:
            nk_update.install_overlay(self.ud)
            info = nk_update.install_overlay(self.ud)
            self.assertTrue(info["skipped"], info)
            self.assertFalse(nk_update.install_overlay(self.ud)["app_overlay"])
        finally:
            sys.meta_path[:] = before
        # off: the overlay is never loaded
        self.hub.update_settings({"updates": {"enabled": False}})
        self.assertFalse(nk_update.install_overlay(self.ud)["app_overlay"])
        sys.meta_path[:] = before


@dataclass
class _State:
    cfg: object = None
    cool_polls: int = 0
    last_stable_freq: float | None = None
    error_window: deque = field(default_factory=lambda: deque(maxlen=5))
    vr_history: deque = field(default_factory=deque)


class RestartStateTests(unittest.TestCase):
    def test_tuner_state_survives_a_quick_restart(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            t1 = types.SimpleNamespace(states={"Axe One": _State(cool_polls=4, last_stable_freq=625.0, error_window=deque([0.5, 0.7], maxlen=5),
                                                                 vr_history=deque([(1.0, 60.0)]))})
            self.assertEqual(nk_update.save_restart_state(Path(d), t1, now=1000.0), 1)
            t2 = types.SimpleNamespace(states={"Axe One": _State()})
            self.assertEqual(nk_update.restore_restart_state(Path(d), t2, now=1100.0), 1)
            s = t2.states["Axe One"]
            self.assertEqual((s.cool_polls, s.last_stable_freq, list(s.error_window), s.error_window.maxlen, list(s.vr_history)),
                             (4, 625.0, [0.5, 0.7], 5, [(1.0, 60.0)]))
            self.assertFalse((Path(d) / "restart_state.json").exists(), "used once")
            nk_update.save_restart_state(Path(d), t1, now=1000.0)
            self.assertEqual(nk_update.restore_restart_state(Path(d), types.SimpleNamespace(states={"Axe One": _State()}), now=1000.0 + 3600), 0)

    def test_real_device_state_round_trip(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            cfg = autotune.DeviceConfig(name="Axe One", ip="192.0.2.10", type="gamma")
            st = autotune.DeviceState(cfg=cfg)
            st.cool_polls, st.last_stable_freq = 3, 600.0
            st.record_error(0.4, "test")
            nk_update.save_restart_state(Path(d), types.SimpleNamespace(states={"Axe One": st}))
            st2 = autotune.DeviceState(cfg=cfg)
            nk_update.restore_restart_state(Path(d), types.SimpleNamespace(states={"Axe One": st2}))
            self.assertEqual((st2.cool_polls, st2.last_stable_freq, list(st2.error_window)), (3, 600.0, [0.4]))


class PackageToolTests(unittest.TestCase):
    def test_make_update_tool_builds_a_package_the_app_accepts(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            src = Path(d) / "src"
            (src / "plugins").mkdir(parents=True)
            (src / "coins.json").write_bytes(COINS_JSON)
            (src / "plugins" / "fake_test.py").write_bytes(PLUGIN)
            out = Path(d) / "pkg.zip"
            res = nk_update.build_package(src, out, "1.16.3", "1.16.0", secret_key_hex=SECRET.hex())
            r = nk_update.inspect_package(out.read_bytes(), "1.16.0", [PUBLIC], require_signature=True)
            self.assertEqual((r["signature"], r["package_sha256"], len(r["files"])), ("trusted", res["sha256"], 2))
            (src / "evil.exe").write_bytes(b"MZ")
            with self.assertRaises(nk_update.UpdateError):
                nk_update.build_package(src, out, "1.16.3")


if __name__ == "__main__":
    unittest.main()
