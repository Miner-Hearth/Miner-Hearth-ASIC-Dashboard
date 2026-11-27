#!/usr/bin/env python3
"""Tests for Miner Hearth v1.16.2: saved pools.

Synthetic values only: example.com / .example hosts, made-up usernames (SYNTH-ADDR-1.rig) and passwords.
"""

from __future__ import annotations

import json
import tempfile
import threading
import types
import unittest
from pathlib import Path

import autotune
import dashboard
import nk_pools
import miner_hearth

ROOT = Path(__file__).resolve().parent
PW = "s3cret-synthetic-pw"


class ParseTests(unittest.TestCase):
    def test_accepted_forms(self):
        P = nk_pools.parse_stratum
        self.assertEqual(P("stratum+tcp://Pool.Example.com:3333"), ("tcp", "pool.example.com", 3333))
        self.assertEqual(P("stratum+ssl://pool.example.com:4333"), ("ssl", "pool.example.com", 4333))
        self.assertEqual(P("stratum+tls://pool.example.com:4333/"), ("ssl", "pool.example.com", 4333))
        self.assertEqual(P("pool.example.com:3333"), ("tcp", "pool.example.com", 3333))
        self.assertEqual(P("pool.example.com", "3334"), ("tcp", "pool.example.com", 3334))
        self.assertEqual(P("192.0.2.10:3333"), ("tcp", "192.0.2.10", 3333))
        self.assertEqual(P("[2001:db8::1]:3333"), ("tcp", "[2001:db8::1]", 3333))

    def test_rejected_forms(self):
        for url, port in (("", None), ("http://pool.example.com:80", None), ("pool.example.com", None), ("pool example.com:1", None),
                          ("pool.example.com:0", None), ("pool.example.com:70000", None), ("pool.example.com:3333", "3334"),
                          ("pool.example.com:abc", None), ("user@pool.example.com:3333", None), ("pool.example.com:3333/path", None),
                          ("-bad-.example:3333", None), ("pool.example.com", "x")):
            with self.assertRaises(nk_pools.PoolError, msg=(url, port)):
                nk_pools.parse_stratum(url, port)

    def test_entry_validation_and_worker(self):
        e = nk_pools.validate_entry({"url": "btc.pool.example:3333", "user": "SYNTH-ADDR-1.{worker}"})
        self.assertEqual((e["name"], e["password"], e["coin"]), ("btc.pool.example", "x", ""))
        for bad in ({"url": "pool.example.com:3333", "user": ""}, {"url": "pool.example.com:3333", "user": "a b"},
                    {"url": "pool.example.com:3333", "user": "a,b"}, {"url": "pool.example.com:3333", "user": "a", "coin": "NOPE"},
                    {"url": "pool.example.com:3333", "user": "a", "password": "p,w"},
                    {"url": "pool.example.com:3333", "user": "a.{worker}{worker}"}):
            with self.assertRaises(nk_pools.PoolError, msg=bad):
                nk_pools.validate_entry(bad)
        self.assertEqual(nk_pools.apply_worker("SYNTH-ADDR-1.{worker}", "Axe One"), "SYNTH-ADDR-1.AxeOne")
        self.assertEqual(nk_pools.apply_worker("SYNTH-ADDR-1.oldname", "Axe-2", use_machine_worker=True), "SYNTH-ADDR-1.Axe-2")
        self.assertEqual(nk_pools.apply_worker("SYNTH-ADDR-1", "rig_3", use_machine_worker=True), "SYNTH-ADDR-1.rig_3")
        self.assertEqual(nk_pools.apply_worker("SYNTH-ADDR-1.keep", "Axe One"), "SYNTH-ADDR-1.keep")
        self.assertEqual(nk_pools.machine_worker("  !!  "), "worker")


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ud = Path(self.tmp.name) / "userdata"

    def tearDown(self):
        self.tmp.cleanup()

    def test_crud_dedupe_order_and_persistence(self):
        lib = nk_pools.PoolLibrary(self.ud)
        self.assertEqual(lib.list_public(), [])
        a = lib.add({"name": "Solo A", "url": "stratum+tcp://btc.pool.example:3333", "user": "SYNTH-ADDR-1.{worker}", "password": PW})
        self.assertEqual((a["coin_auto"], a["coin_shown"], a["password_is_default"]), ("BTC", "BTC", False))
        self.assertNotIn(PW, json.dumps(a))
        with self.assertRaises(nk_pools.PoolError):   # same pool written differently = duplicate
            lib.add({"url": "BTC.pool.example", "port": 3333, "user": "SYNTH-ADDR-1.{worker}"})
        b = lib.add({"url": "btc.pool.example:3333", "user": "SYNTH-ADDR-2.rig"})        # other user: fine
        c = lib.add({"name": "TLS", "url": "stratum+ssl://xec.pool.example:4333", "user": "SYNTH-ADDR-3", "coin": "xec"})
        self.assertEqual((c["coin"], c["scheme"], c["device_url"]), ("XEC", "ssl", "stratum+ssl://xec.pool.example"))
        e = lib.update(a["id"], {"name": "Solo A2", "url": "btc.pool.example:3334", "user": "SYNTH-ADDR-1.{worker}", "password": ""})
        self.assertEqual(lib.get(a["id"])["password"], PW, "blank password on edit keeps the saved one")
        self.assertEqual(e["port"], 3334)
        with self.assertRaises(nk_pools.PoolError):
            lib.update(b["id"], {"url": "btc.pool.example:3334", "user": "SYNTH-ADDR-1.{worker}"})
        self.assertEqual(lib.move(c["id"], -5), [c["id"], a["id"], b["id"]])
        self.assertEqual(lib.reorder([b["id"], "nope", b["id"]]), [b["id"], c["id"], a["id"]])
        self.assertTrue(lib.delete(c["id"]))
        self.assertFalse(lib.delete(c["id"]))
        lib2 = nk_pools.PoolLibrary(self.ud)            # restart / update: same list, same order
        self.assertEqual([p["id"] for p in lib2.list_public()], [b["id"], a["id"]])
        self.assertEqual(lib2.get(a["id"])["password"], PW)
        self.assertEqual(json.loads((self.ud / "saved_pools.json").read_text())["format"], "miner-hearth-pools")

    def test_export_import(self):
        lib = nk_pools.PoolLibrary(self.ud)
        lib.add({"name": "One", "url": "pool.example.com:3333", "user": "SYNTH-ADDR-1.{worker}", "password": PW})
        ex = lib.export()
        self.assertNotIn(PW, json.dumps(ex))
        self.assertIn(PW, json.dumps(lib.export(include_passwords=True)))
        other = nk_pools.PoolLibrary(Path(self.tmp.name) / "other")
        data = json.dumps({"format": "miner-hearth-pools", "pools": ex["pools"] + [{"url": "bad", "user": "x"}, {"url": "two.example.com:1", "user": "u"}]})
        res = other.import_(data)
        self.assertEqual((res["added"], res["skipped"], res["invalid"]), (2, 0, 1))
        self.assertEqual(other.import_(data)["skipped"], 2)
        self.assertEqual(other.get(other.list_public()[0]["id"])["password"], "x", "export without passwords imports with the default")
        res = other.import_({"pools": [{"url": "three.example.com:2", "user": "u"}]}, replace=True)
        self.assertEqual(len(other.list_public()), 1)
        with self.assertRaises(nk_pools.PoolError):
            other.import_("not json")

    def test_entry_from_device(self):
        snap = {"pool_url": "solo.pool.example", "pool_port": 3333, "pool_user": "SYNTH-ADDR-1.rig",
                "fallback_pool_url": "stratum+tcp://backup.pool.example:4444", "fallback_pool_port": 4444, "fallback_pool_user": "SYNTH-ADDR-1.rig",
                "pools": [{"url": "stratum+tcp://a.pool.example:3333", "user": "SYNTH-ADDR-2.ant"}, {"url": "stratum+tcp://b.pool.example:3333", "user": ""}]}
        F = nk_pools.PoolLibrary.entry_from_device
        self.assertEqual(F(snap, "primary"), {"url": "solo.pool.example:3333", "user": "SYNTH-ADDR-1.rig"})
        self.assertEqual(F(snap, "fallback"), {"url": "stratum+tcp://backup.pool.example:4444", "user": "SYNTH-ADDR-1.rig"})
        self.assertEqual(F(snap, 0), {"url": "stratum+tcp://a.pool.example:3333", "user": "SYNTH-ADDR-2.ant"})
        for slot in (1, 5, "x"):
            with self.assertRaises(nk_pools.PoolError):
                F(snap, slot)
        with self.assertRaises(nk_pools.PoolError):
            F({}, "primary")


class HubRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ud = Path(self.tmp.name) / "userdata"
        self.hub = miner_hearth.FeatureHub(self.ud, fetch=lambda u, t=8: b"")
        self.hub.shared = types.SimpleNamespace(lock=threading.Lock(), snapshots={
            "Axe One": {"pool_url": "solo.pool.example", "pool_port": 3333, "pool_user": "SYNTH-ADDR-1.rig",
                        "fallback_pool_url": "backup.pool.example", "fallback_pool_port": 4444, "fallback_pool_user": "SYNTH-ADDR-1.rig"},
            "S19 One": {"pool_url": "stratum+tcp://a.pool.example:3333", "pool_user": "SYNTH-ADDR-2.ant",
                        "pools": [{"url": "stratum+tcp://a.pool.example:3333", "user": "SYNTH-ADDR-2.ant"},
                                  {"url": "stratum+tcp://b.pool.example:3334", "user": "SYNTH-ADDR-2.ant"}]}})

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, route, body=None, remote=False, query=""):
        code, data, ctype = self.hub.handle(method, route, query, json.dumps(body or {}).encode(), remote)
        return code, (json.loads(data) if ctype == "application/json" else data)

    def test_library_ships_empty(self):
        self.assertFalse((ROOT / "saved_pools.json").exists())
        self.assertFalse((ROOT / "userdata").exists())
        self.assertNotIn("saved_pools", (ROOT / "config.json").read_text())
        code, r = self.call("GET", "/api/nk/pools")
        self.assertEqual((code, r["pools"]), (200, []))
        self.assertIn("BTC", r["coins"])

    def test_routes_never_send_passwords(self):
        code, r = self.call("POST", "/api/nk/pools/save", {"name": "Solo", "url": "stratum+tcp://btc.pool.example:3333", "user": "SYNTH-ADDR-1.{worker}", "password": PW})
        self.assertEqual(code, 200, r)
        pid = r["pool"]["id"]
        code, r = self.call("POST", "/api/nk/pools/save", {"url": "btc.pool.example:3333", "user": "SYNTH-ADDR-1.{worker}"})
        self.assertEqual(code, 400)
        self.assertIn("Already saved", r["error"])
        code, r = self.call("POST", "/api/nk/pools/resolve", {"id": pid, "device": "Axe One"})
        self.assertEqual(r["pool"]["user"], "SYNTH-ADDR-1.AxeOne")
        self.assertNotIn("password", r["pool"])
        for remote in (False, True):
            code, r = self.call("GET", "/api/nk/pools", remote=remote)
            self.assertNotIn(PW, json.dumps(r))
            self.assertTrue(r["pools"][0]["password_masked"].startswith("•"))
        code, data = self.call("GET", "/api/nk/pools/export")
        self.assertNotIn(PW, json.dumps(data))
        code, data = self.call("GET", "/api/nk/pools/export", query="passwords=1")
        self.assertIn(PW, json.dumps(data))
        code, r = self.call("GET", "/api/nk/pools/export", query="passwords=1", remote=True)
        self.assertEqual(code, 403)

    def test_save_from_device_primary_fallback_and_cgminer_rows(self):
        code, r = self.call("POST", "/api/nk/pools/from-device", {"device": "Axe One", "slot": "primary", "name": "Solo"})
        self.assertEqual((code, r["pool"]["url"], r["pool"]["user"], r["pool"]["name"]), (200, "stratum+tcp://solo.pool.example:3333", "SYNTH-ADDR-1.rig", "Solo"), r)
        self.assertTrue(r["pool"]["password_is_default"])
        code, r = self.call("POST", "/api/nk/pools/from-device", {"device": "Axe One", "slot": "fallback"})
        self.assertEqual((code, r["pool"]["url"], r["pool"]["name"]), (200, "stratum+tcp://backup.pool.example:4444", "backup.pool.example"))
        code, r = self.call("POST", "/api/nk/pools/from-device", {"device": "S19 One", "slot": 1})
        self.assertEqual(r["pool"]["url"], "stratum+tcp://b.pool.example:3334")
        code, r = self.call("POST", "/api/nk/pools/from-device", {"device": "Axe One", "slot": "primary"})
        self.assertEqual(code, 400, "already saved")
        code, r = self.call("POST", "/api/nk/pools/from-device", {"device": "Nope", "slot": "primary"})
        self.assertEqual(code, 404)
        ids = [p["id"] for p in self.call("GET", "/api/nk/pools")[1]["pools"]]
        code, r = self.call("POST", "/api/nk/pools/move", {"id": ids[2], "delta": -1})
        self.assertEqual([p["id"] for p in r["pools"]], [ids[0], ids[2], ids[1]])
        code, r = self.call("POST", "/api/nk/pools/delete", {"id": ids[0]})
        self.assertEqual(len(r["pools"]), 2)
        code, r = self.call("POST", "/api/nk/pools/import", {"data": json.dumps({"pools": [{"url": "c.example.com:1", "user": "u"}]})})
        self.assertEqual((code, r["added"]), (200, 1))

    def test_lan_read_only_blocks_changes(self):
        lan = dict(self.hub.settings.section("lan"), enabled=True, read_only=True, pin="")
        denied = self.hub.guard.check("POST", "/api/nk/pools/save", "192.0.2.50", {"X-Requested-With": "MinerHearth"}, lan)
        self.assertIsNotNone(denied)
        self.assertEqual(denied[0], 403)


class PoolSwitchTests(unittest.TestCase):
    """The existing pool-switch flow with a saved pool picked (autotune.Tuner.apply_pool_change)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.hub = miner_hearth.FeatureHub(Path(self.tmp.name) / "userdata", fetch=lambda u, t=8: b"")
        lib = self.hub.pools
        self.p = lib.add({"name": "Solo", "url": "stratum+tcp://btc.pool.example:3333", "user": "SYNTH-ADDR-1.{worker}", "password": PW})["id"]
        self.f = lib.add({"name": "Backup", "url": "backup.pool.example:4444", "user": "SYNTH-ADDR-1.old"})["id"]
        self.s = lib.add({"name": "TLS", "url": "stratum+ssl://tls.pool.example:4333", "user": "SYNTH-ADDR-1.x", "password": PW})["id"]
        self.sent = []
        self._orig = autotune.apply_pool_settings
        autotune.apply_pool_settings = lambda cfg, **kw: (self.sent.append(kw), {"result": "ok", "restarted": False, "restart_required": False})[1]

    def tearDown(self):
        autotune.apply_pool_settings = self._orig
        self.tmp.cleanup()

    def tuner(self, typ="gamma"):
        cfg = types.SimpleNamespace(name="Axe One", type=typ)
        state = types.SimpleNamespace(cfg=cfg, last_action="", last_action_at="", last_reason="")
        t = types.SimpleNamespace(states={"Axe One": state}, _lock=threading.Lock(), hub=self.hub, shared=None, _dry_run=lambda: False)
        t._note_transition = lambda n, k, r: autotune.Tuner._note_transition(t, n, k, r)
        t._resolve_saved_pools = lambda n, c, s: autotune.Tuner._resolve_saved_pools(t, n, c, s)
        return t

    def apply(self, spec, typ="gamma"):
        return autotune.Tuner.apply_pool_change(self.tuner(typ), "Axe One", spec)

    def test_picked_pools_fill_the_change_and_open_the_grace(self):
        ok, err, _ = self.apply({"saved_pool_id": self.p, "fallback_saved_pool_id": self.f, "use_machine_worker": True})
        self.assertTrue(ok, err)
        kw = self.sent[-1]
        self.assertEqual((kw["stratum_url"], kw["stratum_port"], kw["stratum_user"], kw["stratum_password"]),
                         ("btc.pool.example", 3333, "SYNTH-ADDR-1.AxeOne", PW))
        self.assertEqual((kw["fallback_url"], kw["fallback_port"], kw["fallback_user"], kw["fallback_password"]),
                         ("backup.pool.example", 4444, "SYNTH-ADDR-1.AxeOne", "x"))
        g = self.hub.grace.active("Axe One")                     # v1.15.5 pool-switch grace still opens
        self.assertIsNotNone(g)
        self.assertEqual(g["kind"], "nk_pool_change")

    def test_form_fields_win_and_an_edited_address_never_gets_the_saved_password(self):
        ok, err, _ = self.apply({"saved_pool_id": self.p, "stratum_url": "btc.pool.example", "stratum_port": 3333, "stratum_user": "SYNTH-ADDR-9.typed"})
        self.assertTrue(ok, err)
        self.assertEqual((self.sent[-1]["stratum_user"], self.sent[-1]["stratum_password"]), ("SYNTH-ADDR-9.typed", PW))
        ok, err, _ = self.apply({"saved_pool_id": self.p, "stratum_url": "other.pool.example", "stratum_port": 3333, "stratum_user": "u"})
        self.assertTrue(ok, err)
        self.assertEqual(self.sent[-1]["stratum_password"], "")
        ok, err, _ = self.apply({"saved_pool_id": self.p, "stratum_url": "btc.pool.example", "stratum_port": 3333, "stratum_user": "u", "stratum_password": "typed"})
        self.assertEqual(self.sent[-1]["stratum_password"], "typed")

    def test_manual_path_unchanged(self):
        ok, err, _ = self.apply({"stratum_url": "manual.pool.example", "stratum_port": 3335, "stratum_user": "SYNTH-ADDR-1.m"})
        self.assertTrue(ok, err)
        self.assertEqual((self.sent[-1]["stratum_url"], self.sent[-1]["stratum_password"]), ("manual.pool.example", ""))

    def test_antminer_limits_and_ssl_url(self):
        ok, err, _ = self.apply({"saved_pool_id": self.p, "fallback_saved_pool_id": self.f}, typ="antminer")
        self.assertFalse(ok)
        self.assertIn("fallback", err)
        ok, err, _ = self.apply({"saved_pool_id": self.s}, typ="antminer")
        self.assertTrue(ok, err)
        self.assertEqual((self.sent[-1]["stratum_url"], self.sent[-1]["stratum_port"]), ("stratum+ssl://tls.pool.example", 4333))
        ok, err, _ = self.apply({"saved_pool_id": "gone"})
        self.assertFalse(ok)
        self.assertIn("no longer exists", err)

    def test_cgminer_target_url_from_saved_pool(self):
        autotune.apply_pool_settings = self._orig
        cfg = autotune.DeviceConfig(name="S19", ip="192.0.2.20", type="antminer") if hasattr(autotune, "DeviceConfig") else None
        if cfg is None:
            self.skipTest("no DeviceConfig")
        r = autotune.apply_pool_settings(cfg, "stratum+ssl://tls.pool.example", 4333, "SYNTH-ADDR-1.x", dry_run=True)
        self.assertIn("stratum+ssl://tls.pool.example:4333", r["result"])
        r = autotune.apply_pool_settings(cfg, "btc.pool.example", 3333, "SYNTH-ADDR-1.x", dry_run=True)
        self.assertIn("stratum+tcp://btc.pool.example:3333", r["result"])


class DashboardTests(unittest.TestCase):
    def test_ui_pieces_present(self):
        h = dashboard.DASHBOARD_HTML
        for s in ('id="nkSetPools"', "Saved Pools", "nk1162SaveButtons(d)", "nk1162RowSave(d, pi)", "nk1162Picker(d, isAnt)",
                  "...nk1162PoolExtras(name)", "Use this machine's name as the worker", 'type="password" maxlength="200"'):
            self.assertIn(s, h)


if __name__ == "__main__":
    unittest.main()
