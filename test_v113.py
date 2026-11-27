#!/usr/bin/env python3
"""Comprehensive tests for Miner Hearth v1.13 (Tier-1 adapters, multi-algo, chart axes, compat capture)."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import autotune
import dashboard
from nk_adapters.apollo import ApolloAdapter
from nk_adapters.avalon import AvalonAdapter, parse_avalon_mm_id, parse_avalon_readings
from nk_adapters.axeos import AXEOS_ASIC_WHITELIST, AxeOSAdapter
from nk_adapters.base import format_hashrate_hs, to_hs
from nk_adapters.bitmain import BitmainStockAdapter, infer_bitmain_algorithm
from nk_adapters.braiins import parse_prometheus_metrics
from nk_adapters.luxos import LuxOSAdapter
from nk_adapters.vnish import VnishAdapter
import nk_coins
import nk_compat
import nk_math
from miner_hearth import FeatureHub


class MockCGMinerServer:
    def __init__(self, handler_fn):
        self.handler_fn = handler_fn
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self.calls: list[dict] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop:
            try:
                self.sock.settimeout(0.2)
                conn, _ = self.sock.accept()
            except OSError:
                continue
            try:
                data = conn.recv(4096).decode("utf-8", errors="replace").strip("\x00\r\n ")
                req = json.loads(data) if data.startswith("{") else {"command": data}
                self.calls.append(req)
                resp = self.handler_fn(req)
                conn.sendall((json.dumps(resp) + "\x00").encode("utf-8"))
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


def start_http_server(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def make_test_stack(tmp: str, devices: list[dict]):
    d = Path(tmp)
    cfg_path = d / "config.json"
    state_path = d / "state.json"
    raw = {
        "log_dir": str(d / "logs"),
        "state_file": str(state_path),
        "userdata_file": str(d / "userdata" / "devices.json"),
        "dashboard": {"enabled": True, "port": 0, "scan_subnet": "127.0.0.1"},
        "devices": devices,
    }
    cfg_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    gcfg = autotune.parse_config(raw, d, config_path=cfg_path)
    shared = dashboard.SharedState([dev.name for dev in gcfg.devices], state_path)
    hub = FeatureHub(d / "userdata")
    shared.hub = hub
    logger = autotune.CsvLogger(gcfg.csv_file)
    tuner = autotune.Tuner(gcfg, clock=lambda: 1000.0, sleeper=lambda _s: None, csv_logger=logger, shared=shared)
    shared.attach_tuner(tuner)
    return gcfg, shared, tuner, logger


class TestCanonicalUnitsAndAdapters(unittest.TestCase):
    def test_unit_conversions_and_formatting(self):
        self.assertEqual(to_hs(1.0, "TH/s"), 1e12)
        self.assertEqual(to_hs(500.0, "MH/s"), 5e8)
        self.assertEqual(to_hs(135.0, "kSol/s"), 135e3)
        self.assertIn("TH/s", format_hashrate_hs(110e12, "hash"))
        self.assertIn("EH/s", format_hashrate_hs(1.2e18, "hash"))
        self.assertIn("kSol/s", format_hashrate_hs(140e3, "sol"))

    def test_avalon_monitor_and_opt_in_ascset(self):
        state = {"workmode": "1", "slept": False, "rebooted": False}

        def cg(req):
            cmd = req.get("command")
            param = req.get("parameter", "")
            if cmd == "version":
                return {"STATUS": [{"STATUS": "S"}], "VERSION": [{"Model": "Avalon Nano 3", "CompileTime": "2026"}]}
            if cmd == "summary":
                return {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 4050.0, "GHS 5s": 4100.0, "Elapsed": 1200}]}
            if cmd == "stats":
                return {
                    "STATUS": [{"STATUS": "S"}],
                    "STATS": [{"MM ID0": "Ver[Nano3-1.0] WORKMODE[1] TMax[72] MTavg[61] FanR[55%] PS[0 1200 135]"}],
                }
            if cmd == "litestats":
                return {"STATUS": [{"STATUS": "S"}], "LITESTATS": [{"MHS 5s": 4100000.0}]}
            if cmd == "ascset":
                if "workmode,set," in param:
                    state["workmode"] = param.split(",")[-1]
                elif "softoff" in param:
                    state["slept"] = True
                elif "softon" in param:
                    state["slept"] = False
                elif "reboot" in param:
                    state["rebooted"] = True
                return {"STATUS": [{"STATUS": "S", "Msg": "OK"}]}
            return {"STATUS": [{"STATUS": "E"}]}

        srv = MockCGMinerServer(cg)
        try:
            adp = AvalonAdapter()
            probe = adp.probe("127.0.0.1", http_port=1, cgminer_port=srv.port)
            self.assertIsNotNone(probe)
            self.assertEqual(probe.family, "avalon")
            self.assertEqual(probe.algorithm, "sha256d")

            kv = parse_avalon_mm_id("WORKMODE[2] TMax[72] PS[0 1200 135]")
            self.assertEqual(kv["WORKMODE"], "2")

            parsed = parse_avalon_readings(
                cg({"command": "version"}),
                cg({"command": "summary"}),
                cg({"command": "stats"}),
                cg({"command": "litestats"}),
            )
            self.assertGreater(parsed["hashrate_hs"], 4e12)
            self.assertEqual(parsed["asic_temp_c"], 72.0)

            with tempfile.TemporaryDirectory() as tmp:
                _, _, tuner, logger = make_test_stack(tmp, [{
                    "name": "Nano3",
                    "type": "antminer",
                    "ip": "127.0.0.1",
                    "cgminer_port": srv.port,
                    "family": "avalon",
                }])
                try:
                    # Monitor-only by default
                    ok, msg = tuner.apply_adapter_action("Nano3", "workmode", {"mode": "2"})
                    self.assertFalse(ok)
                    self.assertIn("monitor-only", msg)

                    # Opt-in write controls
                    tuner.update_device_adapter("Nano3", {"write_enabled": True})
                    ok, _ = tuner.apply_adapter_action("Nano3", "workmode", {"mode": "2"})
                    self.assertTrue(ok and state["workmode"] == "2")
                    ok, _ = tuner.apply_adapter_action("Nano3", "soft_off", {})
                    self.assertTrue(ok and state["slept"])
                    ok, _ = tuner.apply_adapter_action("Nano3", "soft_on", {})
                    self.assertTrue(ok and not state["slept"])
                    ok, _ = tuner.apply_adapter_action("Nano3", "reboot", {})
                    self.assertTrue(ok and state["rebooted"])
                finally:
                    logger.close()
        finally:
            srv.close()

    def test_vnish_monitor_and_opt_in_unlock(self):
        calls = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def _json(self, code, data):
                b = json.dumps(data).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/api/v1/info":
                    return self._json(200, {"miner": "Antminer S19j Pro", "fw_name": "vnish", "fw_version": "1.2.6"})
                if self.path == "/api/v1/summary":
                    return self._json(200, {
                        "miner": {
                            "hr_realtime": 108.5,
                            "power_usage": 3050,
                            "chip_temp": {"max": 68.0},
                            "cooling": {"fans": [{"rpm": 5900}, {"rpm": 6000}]},
                            "pools": [{"url": "stratum+tcp://pool.example:3333", "user": "worker1", "status": "active"}],
                        }
                    })
                return self._json(404, {})

            def do_POST(self):
                ln = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(ln).decode("utf-8") or "{}") if ln else {}
                calls.append((self.path, self.headers.get("Authorization"), body))
                if self.path == "/api/v1/unlock":
                    return self._json(200, {"token": "tok_vnish_test"})
                return self._json(200, {"ok": True})

        srv = start_http_server(H)
        try:
            port = srv.server_address[1]
            adp = VnishAdapter()
            probe = adp.probe("127.0.0.1", http_port=port, cgminer_port=1)
            self.assertIsNotNone(probe)
            self.assertEqual(probe.family, "vnish")

            parsed = VnishAdapter.parse_http_payloads(
                {"miner": "Antminer S19j Pro", "fw_name": "vnish"},
                {"miner": {"hr_realtime": 108.5, "power_usage": 3050, "chip_temp": {"max": 68.0}}},
            )
            self.assertAlmostEqual(parsed["hashrate_hs"], 108.5e9)
            self.assertEqual(parsed["power_w"], 3050.0)

            ok_bad, _ = adp.set_throttle("127.0.0.1", port, "secret", 77)
            self.assertFalse(ok_bad)
            ok, _ = adp.set_throttle("127.0.0.1", port, "secret", 75)
            self.assertTrue(ok)
            self.assertEqual(calls[0][0], "/api/v1/unlock")
            self.assertEqual(calls[1][0], "/api/v1/settings")
            self.assertEqual(calls[1][2]["miner"]["overclock"]["throttle"], 75)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_luxos_logon_and_guaranteed_logoff_in_finally(self):
        seq = []

        def cg(req):
            cmd = req.get("command")
            seq.append(cmd)
            if cmd == "version":
                return {"STATUS": [{"STATUS": "S"}], "VERSION": [{"LUXOS": "2025.1", "Type": "Antminer S19"}]}
            if cmd == "summary":
                return {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 95000.0}]}
            if cmd == "logon":
                return {"STATUS": [{"STATUS": "S"}], "SESSION": [{"SessionID": "sess42"}]}
            if cmd == "profileset":
                return {"STATUS": [{"STATUS": "E", "Msg": "bad profile"}]}
            if cmd == "logoff":
                return {"STATUS": [{"STATUS": "S"}]}
            return {"STATUS": [{"STATUS": "S"}]}

        srv = MockCGMinerServer(cg)
        try:
            adp = LuxOSAdapter()
            probe = adp.probe("127.0.0.1", http_port=1, cgminer_port=srv.port)
            self.assertIsNotNone(probe)
            self.assertEqual(probe.family, "luxos")

            ok, _ = adp.set_profile("127.0.0.1", srv.port, "bad")
            self.assertFalse(ok)
            self.assertIn("logon", seq)
            self.assertIn("profileset", seq)
            self.assertEqual(seq[-1], "logoff")
        finally:
            srv.close()

    def test_bitmain_stock_models_and_user_digest_only(self):
        auth_attempts = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_GET(self):
                auth = self.headers.get("Authorization")
                auth_attempts.append((self.path, auth))
                if not auth or "Digest" not in auth:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Digest realm="antMiner", nonce="abc123", qop="auth"')
                    self.end_headers()
                    return
                body = json.dumps({
                    "STATUS": {"STATUS": "S"},
                    "INFO": {"type": "Antminer Z15"},
                    "SUMMARY": [{"elapsed": 300, "rate_5s": 140.0, "rate_avg": 138.0}],
                    "DEVS": [{"temp_chip": [58, 60]}],
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = start_http_server(H)
        try:
            port = srv.server_address[1]
            self.assertEqual(infer_bitmain_algorithm("Antminer L9")[0], "scrypt")
            self.assertEqual(infer_bitmain_algorithm("Antminer KS5 Pro")[0], "kheavyhash")
            self.assertEqual(infer_bitmain_algorithm("Antminer Z15 Pro")[0], "equihash")
            self.assertEqual(infer_bitmain_algorithm("Antminer E9 Pro")[0], "etchash")
            self.assertEqual(infer_bitmain_algorithm("Antminer S21 XP")[0], "sha256d")

            adp = BitmainStockAdapter()
            # Without user-supplied creds, refuses and makes ZERO HTTP requests
            with self.assertRaises(ValueError):
                adp.fetch_cgi_telemetry("127.0.0.1", port, "", "")
            self.assertEqual(len(auth_attempts), 0)

            # With user-supplied Digest creds, HTTP Digest auth succeeds
            res_with_creds = adp.fetch_cgi_telemetry("127.0.0.1", port, "operator", "userpass")
            self.assertTrue(any(a and "Digest" in a for _, a in auth_attempts))
            self.assertTrue(res_with_creds)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_home_miners_apollo_bitaxe_whitelist_and_bmm(self):
        def cg_apollo(req):
            if req.get("command") == "version":
                return {"STATUS": [{"STATUS": "S"}], "VERSION": [{"Model": "FutureBit Apollo II"}]}
            if req.get("command") == "summary":
                return {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"GHS av": 6100.0}]}
            if req.get("command") == "stats":
                return {"STATUS": [{"STATUS": "S"}], "STATS": [{"temp1": 54.0, "fan1": 1800}]}
            return {"STATUS": [{"STATUS": "S"}]}

        srv = MockCGMinerServer(cg_apollo)
        try:
            probe = ApolloAdapter().probe("127.0.0.1", http_port=1, cgminer_port=srv.port)
            self.assertIsNotNone(probe)
            self.assertEqual(probe.family, "apollo")
        finally:
            srv.close()

        for model in ("BM1362", "BM1366", "BM1368", "BM1370", "BM1397", "BM1398"):
            self.assertIn(model, AXEOS_ASIC_WHITELIST)

        class AxeForkH(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_GET(self):
                data = {"deviceModel": "Lucky Miner LV08", "ASICModel": "BM1368", "hostname": "lv08", "hashRate": 4200}
                b = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        hsrv = start_http_server(AxeForkH)
        try:
            probe = AxeOSAdapter().probe("127.0.0.1", http_port=hsrv.server_address[1], cgminer_port=1)
            self.assertIsNotNone(probe)
            self.assertEqual(probe.family, "axeos")
            self.assertEqual(probe.model, "Lucky Miner LV08")
            self.assertEqual(probe.device_type, "axeos-generic")
        finally:
            hsrv.shutdown()
            hsrv.server_close()

        prom_txt = (
            'bos_hashrate_hashrate_5s{unit="GH/s"} 1120.5\n'
            'bos_power_watts 39.2\n'
            'bos_temperature_max_celsius 58.5\n'
        )
        parsed = parse_prometheus_metrics(prom_txt)
        self.assertAlmostEqual(parsed["power_w"], 39.2)
        self.assertAlmostEqual(parsed["hashrate_hs"], 1120.5e9)

        with tempfile.TemporaryDirectory() as tmp:
            _, _, tuner, logger = make_test_stack(tmp, [{
                "name": "BMM100",
                "type": "antminer",
                "ip": "127.0.0.1",
                "family": "braiins",
                "bos_min_watts": 25,
                "bos_max_watts": 55,
                "bos_target_watts": 40,
            }])
            try:
                st = tuner.states["BMM100"]
                self.assertEqual(st.cfg.bos_min_watts, 25)
                self.assertEqual(st.cfg.bos_max_watts, 55)
                ok_bmm, _, _ = tuner.update_braiins_power('BMM100', {'bos_min_watts': 20, 'bos_max_watts': 60})
                self.assertTrue(ok_bmm)
            finally:
                logger.close()


class TestMultiAlgoMathAndCoins(unittest.TestCase):
    def test_multi_algo_odds_and_coins(self):
        ltc = nk_math.block_odds_algo(9.5e9, 30_000_000.0, "scrypt", avg_block_sec=150.0)
        self.assertGreater(ltc["chance_day"], 0.0)
        doge = nk_math.merged_coin_odds(
            9.5e9,
            {"difficulty": 30_000_000.0, "avg_block_sec": 150.0},
            {"difficulty": 18_000_000.0, "avg_block_sec": 60.0},
            "scrypt",
        )
        self.assertTrue(doge)

        # ETC does NOT use Bitcoin's 2^32 factor: blocks_per_day = H_s * 86400 / diff
        etc = nk_math.blocks_per_day_algo(1e9, 1e15, "etchash")
        self.assertAlmostEqual(etc, (1e9 * 86400.0) / 1e15)

        # ZEC Equihash in Sol/s
        zec = nk_math.blocks_per_day_algo(140_000.0, 80_000_000.0, "equihash")
        self.assertGreater(zec, 0.0)

        for sym in ("LTC", "DOGE", "KAS", "ALPH", "CKB", "ETC", "ZEC", "DASH", "HNS"):
            self.assertIn(sym, nk_coins.COINS)
        self.assertEqual(nk_coins.detect_coin({"pool_user": "unknown_xyz_addr", "algorithm": "unknown_algo"}, {})["coin"], "unknown")
        self.assertEqual(nk_coins.detect_coin({"pool_user": "kaspa:qp000000000000000000000000000000000000000000000"}, {})["coin"], "KAS")
        self.assertEqual(nk_coins.detect_coin({"pool_user": "ltc1qtestaddress00000000000000000000000000"}, {})["coin"], "LTC")
        self.assertIn("LTC", nk_coins.coins_for_algorithm("scrypt"))
        self.assertIn("DOGE", nk_coins.coins_for_algorithm("scrypt"))


class TestCompatibilityCaptureAndRedaction(unittest.TestCase):
    def test_compat_capture_scrubs_synthetic_secrets_and_verifies_zip(self):
        fake_wallet_btc = "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"
        fake_wallet_xec = "ecash:qp0000000000000000000000000000000000000001"
        fake_wallet_kas = "kaspa:qz9999999999999999999999999999999999999999999999"
        fake_wallet_ltc = "ltc1qfakeaddress9999999999999999999999999999"
        fake_mac = "AA:BB:CC:DD:EE:FF"
        fake_email = "miner.owner@example-fake.org"
        fake_ssid = "SecretHomeWiFi5G"
        fake_serial = "SN998877665544"
        fake_pub_ip = "93.184.216.34"
        fake_worker = f"{fake_wallet_btc}.rig_alpha"
        fake_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0In0.sig123456"

        def cg(req):
            cmd = req.get("command")
            if cmd == "pools":
                return {"STATUS": [{"STATUS": "S"}], "POOLS": [{"URL": "stratum+tcp://pool.test:3333", "User": fake_worker}]}
            return {"STATUS": [{"STATUS": "S"}], "INFO": {"serial": fake_serial, "mac": fake_mac, "public_ip": fake_pub_ip}}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_GET(self):
                if self.path == "/api/system/info":
                    b = json.dumps({
                        "deviceModel": "Gamma",
                        "ASICModel": "BM1370",
                        "macAddr": fake_mac,
                        "ssid": fake_ssid,
                        "wifiPass": "supersecretwifi!",
                        "hostname": "johns-garage-miner.local",
                        "stratumUser": fake_wallet_xec,
                        "fallbackStratumUser": fake_wallet_kas,
                        "email": fake_email,
                        "token": fake_jwt,
                        "Cookie": "SESSION=abc999xyz",
                        "Authorization": "Bearer supersecretbearertoken",
                        "alt_addr": fake_wallet_ltc,
                    }).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                    return
                self.send_response(404)
                self.end_headers()

        cg_srv = MockCGMinerServer(cg)
        http_srv = start_http_server(H)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                http_port = http_srv.server_address[1]
                with patch.object(nk_compat, "CGMINER_READ_COMMANDS", ("pools", "version")):
                    res = nk_compat.collect_and_save_capture(
                        ip="127.0.0.1",
                        out_dir=Path(tmp) / "compat-captures",
                        label="SynthMiner",
                        http_port=http_port,
                        cgminer_port=cg_srv.port,
                    )
                self.assertTrue(res["ok"])
                self.assertTrue(res["after_scan_clean"])
                self.assertGreater(res["redacted_total"], 5)
                counts = res["before_counts"]
                self.assertGreater(counts.get("wallet_addresses", 0), 0)
                self.assertGreater(counts.get("mac_addresses", 0), 0)
                self.assertGreater(counts.get("ssids", 0), 0)
                self.assertGreater(counts.get("passwords", 0), 0)
                self.assertGreater(counts.get("emails", 0), 0)
                self.assertGreater(counts.get("public_ips", 0), 0)

                zpath = Path(res["path"])
                self.assertTrue(zpath.exists())
                with zipfile.ZipFile(zpath, "r") as zf:
                    joined = "\n".join(zf.read(n).decode("utf-8", errors="replace") for n in zf.namelist())
                for secret in (
                    fake_wallet_btc, fake_wallet_xec, fake_wallet_kas, fake_wallet_ltc,
                    fake_mac, fake_email, fake_ssid, fake_serial, fake_pub_ip, fake_jwt,
                    "supersecretwifi!", "supersecretbearertoken",
                ):
                    self.assertNotIn(secret, joined, f"Leak found in zip: {secret}")

                dirty = Path(tmp) / "dirty.zip"
                with zipfile.ZipFile(dirty, "w") as zf:
                    zf.writestr("leak.json", json.dumps({"mac": fake_mac, "wallet": fake_wallet_btc}))
                scan_res = nk_compat.verify_redacted_zip(dirty)
                self.assertFalse(scan_res["after_scan_clean"])
                self.assertGreater(scan_res["leak_count"], 0)
        finally:
            cg_srv.close()
            http_srv.shutdown()
            http_srv.server_close()


class TestDashboardUIAndRoutes(unittest.TestCase):
    def test_ui_contains_dual_axis_and_compat_buttons(self):
        html = dashboard.DASHBOARD_HTML
        for marker in (
            "nk-chart-frame",
            "nkHistHrLHi",
            "nkHistHrRHi",
            "nkHistTLHi",
            "nkHistTRHi",
            "nk-spark-frame",
            "nkCompatIpBtn",
            "nkCompatModal",
            "nkRunCompatCapture",
            "nkRenderAdapterBox",
            "fmtHashrateUnit",
        ):
            self.assertIn(marker, html, f"Missing UI marker: {marker}")

    def test_adapter_opt_in_gating_and_compat_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, st, _, logger = make_test_stack(tmp, [{
                "name": "Nano3",
                "type": "antminer",
                "ip": "127.0.0.1",
                "family": "avalon",
            }])
            try:
                code, body, _ = dashboard.handle_dashboard_request(
                    "POST", "/api/device/Nano3/adapter-action",
                    json.dumps({"action": "workmode", "params": {"mode": "2"}}).encode("utf-8"), st,
                )
                self.assertEqual(code, 403)
                self.assertIn("monitor-only", json.loads(body)["error"])

                code, body, _ = dashboard.handle_dashboard_request(
                    "POST", "/api/device/Nano3/adapter",
                    json.dumps({"write_enabled": True}).encode("utf-8"), st,
                )
                self.assertEqual(code, 200)
                self.assertTrue(json.loads(body)["write_enabled"])

                code, body, _ = dashboard.handle_dashboard_request(
                    "POST", "/api/nk/compat-capture",
                    json.dumps({"ip": "127.0.0.1:1"}).encode("utf-8"), st,
                )
                self.assertEqual(code, 200)
                res = json.loads(body)
                self.assertTrue(res["ok"])
                self.assertTrue(res["after_scan_clean"])
                self.assertTrue(res["download_url"].startswith("/api/nk/compat-capture/download?file="))

                code, zbytes, ctype = dashboard.handle_dashboard_request(
                    "GET", res["download_url"], b"", st,
                )
                self.assertEqual(code, 200)
                self.assertEqual(ctype, "application/zip")
                self.assertTrue(zbytes.startswith(b"PK"))
            finally:
                logger.close()


if __name__ == "__main__":
    unittest.main()
