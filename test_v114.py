#!/usr/bin/env python3
"""Comprehensive tests for Miner Hearth v1.14:
1. Manual removal of offline/online machines + full state/history/config cleanup + poll safety.
2. Paste support (Ctrl+V, Shift+Insert, right-click context menu), unmasked webhook URLs,
   whitespace/newline trimming, Discord webhook prefix validation, and HTTP status + error body reporting.
3. Tier 2 compatibility adapters (Whatsminer, Goldshell, Elphapex, IceRiver, Sealminer,
   Generic CGMiner, Generic Prometheus, Generic HTTP-JSON), opt-in writes, scanner detection,
   compatibility capture & redaction.
"""

from __future__ import annotations

import base64
import json
import socket
import ssl
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import autotune
import dashboard
import miner_dashboard
import nk_adapters
import nk_compat
import nk_extras
import miner_hearth
import scanner
from nk_adapters.base import cgminer_request
from nk_adapters.elphapex import ElphapexAdapter, parse_elphapex_stats
from nk_adapters.generic import (
    GenericCGMinerAdapter,
    GenericHTTPJsonAdapter,
    GenericPrometheusAdapter,
    parse_generic_http_json,
    parse_generic_prometheus,
)
from nk_adapters.goldshell import GoldshellAdapter, parse_goldshell_status
from nk_adapters.iceriver import IceRiverAdapter, parse_iceriver_oc_overview, parse_iceriver_userpanel
from nk_adapters.sealminer import SealminerAdapter, parse_sealminer_readings
from nk_adapters.whatsminer import (
    WhatsminerAdapter,
    make_whatsminer_v3_token,
    parse_whatsminer_v2,
    parse_whatsminer_v3,
)
from miner_hearth import FeatureHub


class MockCGMinerServer:
    def __init__(self, handler_fn, *, close_after_send: bool = True, hold_open_sec: float = 0.0, append_nul: bool = True):
        self.handler_fn = handler_fn
        self.close_after_send = close_after_send
        self.hold_open_sec = hold_open_sec
        self.append_nul = append_nul
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self.calls: list[dict] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _handle_conn(self, conn: socket.socket):
        try:
            data = conn.recv(8192).decode("utf-8", errors="replace").strip("\x00\r\n ")
            req = json.loads(data) if data.startswith("{") else {"command": data}
            self.calls.append(req)
            resp = self.handler_fn(req)
            raw = json.dumps(resp).encode("utf-8") + (b"\x00" if self.append_nul else b"")
            conn.sendall(raw)
            if not self.close_after_send and self.hold_open_sec > 0:
                deadline = time.monotonic() + self.hold_open_sec
                while not self._stop and time.monotonic() < deadline:
                    time.sleep(0.05)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _serve(self):
        while not self._stop:
            try:
                self.sock.settimeout(0.2)
                conn, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

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


def http_json(url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            return err.code, json.loads(raw)
        except Exception:
            return err.code, {"raw": raw}


def make_test_stack(tmp: str, devices: list[dict], *, use_userdata: bool = True):
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
    gcfg = autotune.parse_config(raw, d, config_path=cfg_path, use_userdata=use_userdata)
    shared = dashboard.SharedState([dev.name for dev in gcfg.devices], state_path)
    hub = FeatureHub(d / "userdata")
    shared.hub = hub
    logger = autotune.CsvLogger(gcfg.csv_file)
    tuner = autotune.Tuner(gcfg, clock=lambda: 1000.0, sleeper=lambda _s: None, csv_logger=logger, shared=shared)
    shared.attach_tuner(tuner)
    return d, cfg_path, gcfg, shared, hub, tuner, logger


class TestMachineRemovalV114(unittest.TestCase):
    def test_remove_offline_and_online_device_cleans_all_stores_and_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            devices = [
                {"name": "Dead Miner", "ip": "127.0.0.1", "type": "gamma", "port": 1},
                {"name": "Live Miner", "ip": "127.0.0.1", "type": "gamma", "port": 2},
            ]
            d, cfg_path, gcfg, shared, hub, tuner, _ = make_test_stack(tmp, devices, use_userdata=True)
            tuner._persist_userdata()

            # Populate state, history, overrides, faults, shares, safety, and long history for Dead Miner
            tuner.history.record("Dead Miner", 1000.0, 1200.0, 55.0, 60.0)
            tuner.last_share.set("Dead Miner", 42, 1000.0)
            tuner.safety_store.update("Dead Miner", {"last_stable_freq": 550})
            hub.settings.update({"coins": {"overrides": {"Dead Miner": "BCH"}}})
            hub.long_history.record("Dead Miner", 1000.0, 1200.0, 55.0, 60.0, 18.0)
            hub.tuning_log.add({"device": "Dead Miner", "ts": 1000.0, "kind": "freq", "old": 525, "new": 550, "reason": "test"})
            hub.alerts._fire("Dead Miner", "offline", 1000.0, 60.0)
            hub.blocks.observe("Dead Miner", {"session": 1e12}, 5e11, 900000, 1000.0)
            hub.session_best.update("Dead Miner", 1e9, 100.0, [], 1000.0)
            hub.pool_health.update("Dead Miner", 5, 0, 0, 1000.0)
            hub.pool_stats.record_shares("pool1", "Dead Miner", 5, 0, 0, 12.0, 1000.0)
            hub.chip_health.observe("Dead Miner", 550.0, 1200.0, 0.1, 1.0, False, 1000.0)
            hub.restarts.record("Dead Miner", {"reason": "test"}, 1000.0)
            hub.fault_history.add("Dead Miner", "low_hash", 1000.0, msg="low")
            hub.fault_context.update("Dead Miner", {"temp": 60})
            hub.fault_snapshots.record("Dead Miner", "low_hash", {"ts": 1000.0}, 1000.0)
            hub.fault_records.add_sample("Dead Miner", {"ts": 1000.0, "temp": 60}, 24.0, 1000.0)
            hub.coin_info["Dead Miner"] = {"coin": "BTC"}
            hub.pool_info["Dead Miner"] = {"url": "stratum+tcp://pool.example:3333"}
            hub.faults_now["Dead Miner"] = [{"kind": "offline"}]

            # Simulate offline poll on Dead Miner
            dead_state = tuner.states["Dead Miner"]
            dead_state.online = False
            dead_state.last_error = "Connection refused"
            shared.update_device("Dead Miner", {"name": "Dead Miner", "online": False, "last_error": "Connection refused"})

            srv = dashboard.DashboardServer(shared, host="127.0.0.1", port=0).start()
            try:
                # HTML includes Remove button with confirmation dialog
                with urllib.request.urlopen(srv.url + "/", timeout=5) as resp:
                    html = resp.read().decode("utf-8")
                self.assertIn("removeDevice(", html)
                self.assertIn("Remove", html)
                self.assertIn("window.confirm", html)

                # Remove Dead Miner via HTTP API
                code, body = http_json(f"{srv.url}/api/device/Dead%20Miner/remove", method="POST", body={})
                self.assertEqual(code, 200, body)
                self.assertTrue(body.get("ok"))

                # Second removal returns 404
                code2, _ = http_json(f"{srv.url}/api/device/Dead%20Miner/remove", method="POST", body={})
                self.assertEqual(code2, 404)

                # Verify remaining device only
                self.assertNotIn("Dead Miner", tuner.states)
                self.assertEqual([dev.name for dev in tuner.config.devices], ["Live Miner"])
                self.assertNotIn("Dead Miner", shared.snapshots)

                # Verify userdata/devices.json cleared Dead Miner
                ud = json.loads((d / "userdata" / "devices.json").read_text(encoding="utf-8"))
                self.assertEqual([x["name"] for x in ud["devices"]], ["Live Miner"])

                # Verify all auxiliary stores cleared Dead Miner
                self.assertEqual(tuner.history.load_device("Dead Miner"), [])
                self.assertFalse((d / "userdata" / "history" / "Dead_Miner.json").exists())
                self.assertIsNone(tuner.last_share.get("Dead Miner"))
                self.assertEqual(tuner.safety_store.get("Dead Miner"), {})
                self.assertNotIn("Dead Miner", hub.settings.get()["coins"]["overrides"])
                self.assertNotIn("Dead Miner", hub.coin_info)
                self.assertNotIn("Dead Miner", hub.pool_info)
                self.assertNotIn("Dead Miner", hub.faults_now)
                self.assertEqual(hub.long_history.series("Dead Miner", "7d"), [])
                self.assertEqual(hub.tuning_log.list("Dead Miner"), [])
                self.assertEqual([c for c in hub.blocks.recent(20) if c.get("device") == "Dead Miner"], [])
                self.assertIsNone(hub.session_best.get("Dead Miner"))
                self.assertNotIn("Dead Miner", hub.pool_health.dev)
                self.assertNotIn("Dead Miner", hub.chip_health.devices)
                self.assertNotIn("Dead Miner", hub.restarts.history)
                self.assertEqual(hub.fault_history.list("Dead Miner"), [])
                self.assertEqual(hub.fault_records.list_records("Dead Miner"), [])
                self.assertEqual(hub.fault_context.get("Dead Miner"), {})
                self.assertEqual(hub.fault_snapshots.list("Dead Miner"), [])

                # Also verify DELETE /api/device/<name> works on remaining device & persists to config.json when no userdata
                tuner.config.userdata_file = None
                persist_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.assertEqual([x["name"] for x in persist_cfg["devices"]], ["Live Miner"])
                code3, body3 = http_json(f"{srv.url}/api/device/Live%20Miner", method="DELETE")
                self.assertEqual(code3, 200, body3)
                after_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.assertEqual(after_cfg["devices"], [])
            finally:
                srv.stop()

    def test_mid_poll_removal_does_not_crash_or_resurrect_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            devices = [{"name": "Transient", "ip": "127.0.0.1", "type": "gamma", "port": 1}]
            _, _, _, shared, _, tuner, _ = make_test_stack(tmp, devices, use_userdata=True)
            state = tuner.states["Transient"]

            def slow_fetch(cfg, *_a, **_kw):
                # Remove the device while fetch_reading is in flight!
                ok, _msg = tuner.remove_device(cfg.name)
                assert ok
                raise autotune.DeviceOffline("socket interrupted after removal")

            with patch("autotune.fetch_reading", side_effect=slow_fetch):
                # Must not crash poll_all
                tuner.poll_all()

            self.assertNotIn("Transient", tuner.states)
            self.assertNotIn("Transient", shared.snapshots)
            # Also direct _publish after removal must not resurrect it
            tuner._publish(state, None, autotune.Decision("hold", None, "removed", 0))
            self.assertNotIn("Transient", shared.snapshots)


class TestPasteAndWebhookUxV114(unittest.TestCase):
    def test_dashboard_html_has_unmasked_webhooks_context_menu_and_paste_shortcuts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, shared, _, _, _ = make_test_stack(tmp, [])
            srv = dashboard.DashboardServer(shared, host="127.0.0.1", port=0).start()
            try:
                with urllib.request.urlopen(srv.url + "/", timeout=5) as resp:
                    html = resp.read().decode("utf-8")
                # Webhook URL inputs must NOT be password inputs
                self.assertIn('<input type="text" id="nk_w_discord"', html)
                self.assertIn('<input type="text" id="nk_w_ntfy"', html)
                self.assertNotIn('<input type="password" id="nk_w_discord"', html)
                self.assertNotIn('<input type="password" id="nk_w_ntfy"', html)
                # Context menu & keyboard paste shortcuts
                self.assertIn('id="nkCtxMenu"', html)
                for label in ("Cut", "Copy", "Paste", "Select All"):
                    self.assertIn(label, html)
                self.assertIn("Shift", html)
                self.assertIn("Insert", html)
                self.assertIn("user-select: text", html)
                self.assertIn("/api/clipboard", html)
            finally:
                srv.stop()

    def test_clipboard_api_localhost_only_and_desktop_api_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, shared, _, _, _ = make_test_stack(tmp, [])
            srv = dashboard.DashboardServer(shared, host="127.0.0.1", port=0).start()
            try:
                with patch("miner_hearth.read_from_clipboard", return_value="https://discord.com/api/webhooks/123/abc"):
                    code, body = http_json(f"{srv.url}/api/clipboard")
                    self.assertEqual(code, 200)
                    self.assertTrue(body["ok"])
                    self.assertEqual(body["text"], "https://discord.com/api/webhooks/123/abc")

                    # Simulated remote client must be rejected (403)
                    req = urllib.request.Request(
                        f"{srv.url}/api/clipboard",
                        headers={"X-Forwarded-For": "203.0.113.99"},
                    )
                    # Note: client_address is still 127.0.0.1; verify DesktopApi.read_clipboard directly too
                    dapi = miner_dashboard.DesktopApi()
                    self.assertEqual(dapi.read_clipboard(), "https://discord.com/api/webhooks/123/abc")
            finally:
                srv.stop()

    def test_webhook_trimming_prefix_validation_and_discord_error_body_reporting(self):
        # 1. Trimming and prefix validation helpers
        self.assertEqual(
            nk_extras.trim_webhook_url("  \nhttps://discord.com/api/webhooks/111/tok\r\n "),
            "https://discord.com/api/webhooks/111/tok",
        )
        ok, err = nk_extras.validate_discord_webhook_url(" https://discordapp.com/api/webhooks/222/xyz\n")
        self.assertTrue(ok, err)

        ok_bad, err_bad = nk_extras.validate_discord_webhook_url("https://discord.com/channels/1/2")
        self.assertFalse(ok_bad)
        self.assertIn("https://discord.com/api/webhooks/", err_bad)

        # 2. Mock Discord HTTP server returning 401 Invalid Webhook Token and 404 Unknown Webhook
        class DiscordMock(BaseHTTPRequestHandler):
            mode = "401"
            last_body = None

            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                DiscordMock.last_body = self.rfile.read(length).decode("utf-8", errors="replace")
                if DiscordMock.mode == "401":
                    payload = json.dumps({"message": "Invalid Webhook Token", "code": 50027}).encode("utf-8")
                    self.send_response(401)
                elif DiscordMock.mode == "404":
                    payload = json.dumps({"message": "Unknown Webhook", "code": 10015}).encode("utf-8")
                    self.send_response(404)
                else:
                    payload = b""
                    self.send_response(204)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

        dmock = start_http_server(DiscordMock)
        try:
            mock_url = f"http://127.0.0.1:{dmock.server_port}/api/webhooks/999/badtok"
            DiscordMock.mode = "401"
            code, detail = nk_extras.default_poster_detail(mock_url, b'{"content":"hello"}', {"Content-Type": "application/json"})
            self.assertEqual(code, 401)
            self.assertEqual(detail, "Invalid Webhook Token")

            wh = nk_extras.Webhooks(async_send=False)
            wh_cfg = {"enabled": True, "discord_url": mock_url, "types": {"test": True}}
            ev = {"kind": "test", "test": True, "title": "Test Title", "message": "Test Body"}
            res_401 = wh.send(wh_cfg, ev, force=True)
            self.assertFalse(res_401[0]["ok"])
            self.assertEqual(res_401[0]["status"], 401)
            self.assertEqual(res_401[0]["error"], "401 Invalid Webhook Token")

            DiscordMock.mode = "404"
            res_404 = wh.send(wh_cfg, ev, force=True)
            self.assertEqual(res_404[0]["status"], 404)
            self.assertEqual(res_404[0]["error"], "404 Unknown Webhook")
        finally:
            dmock.shutdown()
            dmock.server_close()

        # 3. Dashboard endpoints: /api/nk/settings rejects bad Discord prefix and trims whitespace;
        #    /api/nk/test-webhook surfaces Discord's 401 error body cleanly
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, shared, hub, _, _ = make_test_stack(tmp, [])
            srv = dashboard.DashboardServer(shared, host="127.0.0.1", port=0).start()
            try:
                bad_code, bad_body = http_json(
                    f"{srv.url}/api/nk/settings",
                    method="POST",
                    body={"webhooks": {"discord_url": "https://not-discord.example/hook"}},
                )
                self.assertEqual(bad_code, 400)
                self.assertIn("https://discord.com/api/webhooks/", bad_body["error"])

                good_code, good_body = http_json(
                    f"{srv.url}/api/nk/settings",
                    method="POST",
                    body={"webhooks": {"discord_url": "  \nhttps://discord.com/api/webhooks/12345/secret_tok\r\n  "}},
                )
                self.assertEqual(good_code, 200, good_body)
                self.assertEqual(
                    hub.settings.data["webhooks"]["discord_url"],
                    "https://discord.com/api/webhooks/12345/secret_tok",
                )

                # Test webhook endpoint returning 401 error body
                with patch.object(
                    hub.webhooks, "poster",
                    return_value=(401, "Invalid Webhook Token"),
                ):
                    tcode, tbody = http_json(
                        f"{srv.url}/api/nk/test-webhook",
                        method="POST",
                        body={"discord_url": "https://discord.com/api/webhooks/12345/secret_tok\n"},
                    )
                self.assertEqual(tcode, 200)
                self.assertFalse(tbody["ok"])
                self.assertEqual(tbody["results"][0]["error"], "401 Invalid Webhook Token")
            finally:
                srv.stop()


class TestTier2AdaptersV114(unittest.TestCase):
    def test_whatsminer_v2_and_v3_read_and_opt_in_write(self):
        writes = []

        def cg_v2(req):
            cmd = req.get("command")
            if cmd == "get_version":
                return {"STATUS": [{"STATUS": "S"}], "Msg": {"fw_ver": "20240501.18.REL", "api_ver": "2.0.5"}}
            if cmd == "summary":
                return {
                    "STATUS": [{"STATUS": "S"}],
                    "SUMMARY": [{
                        "MHS av": 112000000.0,
                        "HS RT": 114500000.0,
                        "Temperature": 63.5,
                        "Fan Speed In": 4800,
                        "Fan Speed Out": 5100,
                        "Power_RT": 3380,
                        "Power": 3400,
                        "Power Mode": "Normal",
                        "Power Limit": 3500,
                        "Accepted": 100,
                        "Rejected": 1,
                        "Elapsed": 7200,
                        "Factory GHS": 110000,
                    }],
                }
            if cmd == "devs":
                return {
                    "STATUS": [{"STATUS": "S"}],
                    "DEVS": [
                        {"Chip Temp Avg": 61.0, "Chip Temp Max": 69.0, "Chip Freq Avg": 620.0},
                        {"Chip Temp Avg": 62.0, "Chip Temp Max": 70.0, "Chip Freq Avg": 625.0},
                    ],
                }
            if cmd == "get_miner_info":
                return {"STATUS": [{"STATUS": "S"}], "Msg": {"hostname": "wm-m50s", "mac": "aa:bb:cc:dd:ee:01"}}
            if cmd == "get_error_code":
                return {"STATUS": [{"STATUS": "S"}], "Msg": {"error_code": [{"2010": "2026-11-24"}]}}
            if cmd == "get_psu":
                return {"STATUS": [{"STATUS": "S"}], "Msg": {"name": "P221B", "vin": 238.5, "pin": 3390}}
            if cmd == "pools":
                return {"STATUS": [{"STATUS": "S"}], "POOLS": [{"URL": "stratum+tcp://btc.example:3333", "User": "w.1", "Status": "Alive"}]}
            return {"STATUS": [{"STATUS": "E"}]}

        class V3Mock(MockCGMinerServer):
            pass

        def v3_handler(req):
            cmd = req.get("cmd")
            if cmd == "get.device.info":
                return {
                    "code": 0,
                    "msg": {
                        "miner": {"type": "M60S+", "miner-sn": "SN123", "hash-board-count": 3},
                        "system": {"fwversion": "20260110.v3", "hostname": "wm-m60s"},
                        "salt": "salt1234",
                    },
                }
            if cmd == "get.miner.status":
                return {
                    "code": 0,
                    "msg": {
                        "summary": {
                            "hash-realtime": 186.5,
                            "hash-average": 185.0,
                            "power-realtime": 3420,
                            "power-mode": "High",
                            "board-temperature": [64.0, 65.5, 63.0],
                            "chip-temp-max": 73.0,
                            "fan-speed-in": 5400,
                            "fan-speed-out": 5600,
                            "uptime": 3600,
                        }
                    },
                }
            if cmd == "set.miner.power_mode":
                expected = make_whatsminer_v3_token("set.miner.power_mode", "salt1234", "pw123", int(req["ts"]), "super", req.get("param"))
                if req.get("token") != expected:
                    return {"code": -1, "msg": "bad token"}
                writes.append(req)
                return {"code": 0, "msg": "ok"}
            return {"code": -1, "msg": "unknown"}

        srv_v2 = MockCGMinerServer(cg_v2)
        srv_v3 = V3Mock(v3_handler, append_nul=False)
        try:
            adp = WhatsminerAdapter()
            probe = adp.probe("127.0.0.1", cgminer_port=srv_v2.port, v3_port=srv_v3.port)
            self.assertIsNotNone(probe)
            self.assertEqual(probe.family, "whatsminer")
            self.assertEqual(probe.algorithm, "sha256d")

            tele = adp.fetch_telemetry("127.0.0.1", cgminer_port=srv_v2.port, v3_port=srv_v3.port)
            # v3 is checked alongside v2 and supplies M60S+ / 186.5 TH/s or v2 supplies 114.5 TH/s
            self.assertGreater(tele["hashrate_hs"], 100e12)
            self.assertGreater(tele["power_w"], 3000)

            # Monitor-only gating via Tuner.apply_adapter_action
            with tempfile.TemporaryDirectory() as tmp:
                _, _, _, _, _, tuner, _ = make_test_stack(tmp, [
                    {
                        "name": "WM-Test",
                        "ip": "127.0.0.1",
                        "type": "antminer",
                        "family": "whatsminer",
                        "cgminer_port": srv_v2.port,
                        "web_password": "pw123",
                        "write_enabled": False,
                    }
                ])
                tuner.config.dry_run = False
                ok_blocked, msg_blocked = tuner.apply_adapter_action("WM-Test", "power_mode", {"mode": "low"})
                self.assertFalse(ok_blocked)
                self.assertIn("monitor-only", msg_blocked)

            ok_write, msg_write = adp.set_power_mode("127.0.0.1", "pw123", "low", v3_port=srv_v3.port)
            self.assertTrue(ok_write, msg_write)
            self.assertEqual(len(writes), 1)
            self.assertEqual(writes[0]["param"], "low")
        finally:
            srv_v2.close()
            srv_v3.close()

    def test_goldshell_jwt_login_and_opt_in_workmode(self):
        state = {"level": "1", "restarted": False}

        class GSMock(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def _send(self, code, data, extra_hdr=None):
                raw = json.dumps(data).encode("utf-8")
                self.send_response(code)
                if extra_hdr:
                    for k, v in extra_hdr.items():
                        self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/":
                    raw = b"<html><title>Goldshell Miner</title></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if self.path.startswith("/user/login"):
                    qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    if (qs.get("password") or [""])[0] == "gs-pass":
                        self._send(200, {"JWT Token": "gs-jwt-secret"})
                    else:
                        self._send(401, {"error": "bad password"})
                    return
                if self.path == "/mcb/status":
                    if self.headers.get("Authorization") != "Bearer gs-jwt-secret":
                        self._send(401, {"error": "unauthorized"})
                        return
                    self._send(200, {
                        "name": "Goldshell KA BOX",
                        "model": "KA-BOX",
                        "algo": "kheavyhash",
                        "firmware": "2.4.1",
                        "hashrate": 1.18e12,
                        "avg_hashrate": 1.15e12,
                        "power": 400.0,
                        "devs": [{"temp": 58.0, "chip_temp": 66.0, "fanspeed": 3200}],
                    })
                    return
                if self.path == "/mcb/pools":
                    self._send(200, {"pools": [{"url": "stratum+tcp://kas.example:5555", "user": "kas.1", "active": True}]})
                    return
                if self.path == "/mcb/setting":
                    self._send(200, {"level": state["level"]})
                    return
                self._send(404, {})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/user/login":
                    if body.get("password") == "gs-pass":
                        self._send(200, {"JWT Token": "gs-jwt-secret"})
                    else:
                        self._send(401, {"error": "bad password"})
                    return
                self._send(404, {})

            def do_PUT(self):
                if self.headers.get("Authorization") != "Bearer gs-jwt-secret":
                    self._send(401, {"error": "unauthorized"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/mcb/setting":
                    state["level"] = str(body.get("level"))
                    self._send(200, {"ok": True})
                    return
                if self.path == "/mcb/restart":
                    state["restarted"] = True
                    self._send(200, {"ok": True})
                    return
                self._send(404, {})

        srv = start_http_server(GSMock)
        try:
            adp = GoldshellAdapter()
            probe_anon = adp.probe("127.0.0.1", http_port=srv.server_port)
            self.assertIsNotNone(probe_anon)
            self.assertEqual(probe_anon.family, "goldshell")
            self.assertTrue(probe_anon.auth_required)

            tele = adp.fetch_telemetry("127.0.0.1", port=srv.server_port, username="admin", password="gs-pass")
            self.assertEqual(tele["algorithm"], "kheavyhash")
            self.assertAlmostEqual(tele["hashrate_hs"], 1.18e12, delta=1e9)
            self.assertEqual(tele["power_w"], 400.0)
            self.assertEqual(tele["tuner_mode"], "1")

            ok, _ = adp.set_workmode("127.0.0.1", srv.server_port, "admin", "gs-pass", "2")
            self.assertTrue(ok)
            self.assertEqual(state["level"], "2")

            # Also test Scrypt Mini Doge parsing
            scrypt = parse_goldshell_status({"model": "Mini Doge III", "algo": "scrypt", "hashrate": 650.0, "unit": "MH/s"})
            self.assertEqual(scrypt["algorithm"], "scrypt")
            self.assertAlmostEqual(scrypt["hashrate_hs"], 650e6, delta=1e3)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_elphapex_basic_auth_stats_and_workmode(self):
        state = {"mode": "normal"}
        expected_auth = "Basic " + base64.b64encode(b"root:elph-pw").decode("ascii")

        class ELMock(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def _send(self, code, data, extra_hdr=None):
                raw = json.dumps(data).encode("utf-8")
                self.send_response(code)
                if extra_hdr:
                    for k, v in extra_hdr.items():
                        self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/":
                    raw = b"<html><title>Elphapex DG-Home1</title></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if self.headers.get("Authorization") != expected_auth:
                    self._send(401, {"error": "auth"}, {"WWW-Authenticate": 'Basic realm="Elphapex"'})
                    return
                if self.path == "/cgi-bin/stats.cgi":
                    self._send(200, {
                        "INFO": {"type": "Elphapex DG-Home1", "fw_version": "1.2.0"},
                        "STATS": [{
                            "rate_5s": 2120.0,
                            "rate_avg": 2100.0,
                            "rate_unit": "MH/s",
                            "power": 620.0,
                            "fan": [3400, 3500],
                            "chain": [{"temp_chip": [58, 61], "temp_pcb": [49, 50]}],
                            "elapsed": 900,
                        }],
                    })
                    return
                if self.path == "/cgi-bin/pools.cgi":
                    self._send(200, {"POOLS": [{"url": "stratum+tcp://ltc.example:3333", "user": "doge.1", "status": "Alive"}]})
                    return
                if self.path == "/cgi-bin/luci/getworkmode.cgi":
                    self._send(200, {"mode": state["mode"]})
                    return
                self._send(404, {})

            def do_POST(self):
                if self.headers.get("Authorization") != expected_auth:
                    self._send(401, {"error": "auth"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/cgi-bin/luci/setworkmode.cgi":
                    state["mode"] = str(body.get("workmode", ""))
                    self._send(200, {"status": "ok"})
                    return
                self._send(404, {})

        srv = start_http_server(ELMock)
        try:
            adp = ElphapexAdapter()
            probe = adp.probe("127.0.0.1", http_port=srv.server_port)
            self.assertIsNotNone(probe)
            self.assertEqual(probe.family, "elphapex")
            self.assertTrue(probe.auth_required)
            self.assertEqual(probe.algorithm, "scrypt")

            tele = adp.fetch_telemetry("127.0.0.1", port=srv.server_port, username="root", password="elph-pw")
            self.assertAlmostEqual(tele["hashrate_hs"], 2120e6, delta=1e3)
            self.assertEqual(tele["power_w"], 620.0)
            self.assertEqual(tele["tuner_mode"], "normal")

            ok, _ = adp.set_workmode("127.0.0.1", srv.server_port, "root", "elph-pw", "low-power")
            self.assertTrue(ok)
            self.assertEqual(state["mode"], "low-power")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_iceriver_stock_userpanel_and_oc_overview(self):
        class IRMock(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def _send(self, code, data, extra_hdr=None):
                raw = json.dumps(data).encode("utf-8")
                self.send_response(code)
                if extra_hdr:
                    for k, v in extra_hdr.items():
                        self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8", errors="replace")
                if self.path == "/user/userpanel" and "post=4" in body:
                    self._send(200, {
                        "error": 0,
                        "data": {
                            "model": "IceRiver KS5L",
                            "softver": "1.0.8",
                            "rtpow": 12.1,
                            "avgpow": 12.0,
                            "unit": "TH/s",
                            "power": 3400,
                            "fans": [4500, 4600],
                            "boards": [{"intemp": 51, "outtemp": 64, "chips": 24}],
                            "pools": [{"addr": "stratum+tcp://kas.example:16110", "user": "kas.worker", "connect": True}],
                        },
                    })
                    return
                self._send(404, {})

        srv = start_http_server(IRMock)
        try:
            adp = IceRiverAdapter()
            probe = adp.probe("127.0.0.1", http_port=srv.server_port)
            self.assertIsNotNone(probe)
            self.assertEqual(probe.family, "iceriver")
            self.assertEqual(probe.algorithm, "kheavyhash")

            tele = adp.fetch_telemetry("127.0.0.1", port=srv.server_port)
            self.assertAlmostEqual(tele["hashrate_hs"], 12.1e12, delta=1e9)
            self.assertEqual(tele["power_w"], 3400.0)
            self.assertEqual(tele["asic_temp_c"], 64.0)

            # IceRiver AL0 (blake3) & IceRiver-OC overview payload parsing
            al0 = parse_iceriver_userpanel({"data": {"model": "AL0", "rtpow": 400.0, "unit": "GH/s"}})
            self.assertEqual(al0["algorithm"], "blake3")
            self.assertAlmostEqual(al0["hashrate_hs"], 400e9, delta=1e6)

            oc = parse_iceriver_oc_overview({
                "model": "KS3M-OC",
                "hashrate": 6.45,
                "unit": "TH/s",
                "power": 3100,
                "boards": [{"temp": 63, "chip_temp": 71, "freq": 750, "voltage": 1350}],
                "fans": [4200, 4300],
            })
            self.assertEqual(oc["algorithm"], "kheavyhash")
            self.assertAlmostEqual(oc["hashrate_hs"], 6.45e12, delta=1e9)
            self.assertEqual(oc["tuner_mode"], "")
            self.assertEqual(oc["power_w"], 3100.0)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_sealminer_non_closing_tcp_socket_does_not_hang(self):
        def bd_handler(req):
            cmd = req.get("command")
            if cmd == "version":
                return {"STATUS": [{"STATUS": "S"}], "VERSION": [{"Type": "SEALMINER A2", "Miner": "BDMiner 1.4.2"}]}
            if cmd == "summary":
                return {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"MHS av": 226000000.0, "MHS 5s": 228000000.0, "Power": 3650.0, "Elapsed": 1800}]}
            if cmd == "devs":
                return {"STATUS": [{"STATUS": "S"}], "DEVS": [{"Temperature": 60.0, "Chip Temp Max": 72.0, "Fan Speed": 5200}]}
            if cmd == "pools":
                return {"STATUS": [{"STATUS": "S"}], "POOLS": [{"URL": "stratum+tcp://btc.example:3333", "User": "seal.1", "Status": "Alive"}]}
            return {"STATUS": [{"STATUS": "E"}]}

        # Server intentionally keeps TCP socket open for 4s and omits \x00 terminator (BDMiner quirk!)
        srv = MockCGMinerServer(bd_handler, close_after_send=False, hold_open_sec=4.0, append_nul=False)
        try:
            t0 = time.monotonic()
            resp = cgminer_request("127.0.0.1", srv.port, "version", timeout=2.0)
            elapsed = time.monotonic() - t0
            self.assertIsInstance(resp, dict)
            self.assertLess(elapsed, 1.0, f"Non-closing BDMiner socket should complete immediately, took {elapsed:.2f}s")

            # Also verify autotune.query_cgminer works cleanly
            resp2 = autotune.query_cgminer("127.0.0.1", srv.port, "summary", timeout=2.0)
            self.assertEqual(resp2["SUMMARY"][0]["MHS 5s"], 228000000.0)

            adp = SealminerAdapter()
            probe = adp.probe("127.0.0.1", cgminer_port=srv.port, tcp_timeout=1.5)
            self.assertIsNotNone(probe)
            via_all = nk_adapters.probe_host_all("127.0.0.1", http_port=1, cgminer_port=srv.port, tcp_timeout=1.5)
            self.assertIsNotNone(via_all)
            self.assertEqual(via_all.family, "sealminer")
            self.assertEqual(probe.family, "sealminer")
            self.assertEqual(probe.algorithm, "sha256d")

            tele = adp.fetch_telemetry("127.0.0.1", cgminer_port=srv.port, timeout=1.5)
            self.assertAlmostEqual(tele["hashrate_hs"], 228e12, delta=1e9)
            self.assertEqual(tele["power_w"], 3650.0)
            self.assertFalse(adp.capabilities.restart)
            self.assertFalse(adp.capabilities.freq_tuning)
            self.assertFalse(adp.capabilities.voltage_tuning)
        finally:
            srv.close()

    def test_generic_cgminer_prometheus_and_http_json_adapters(self):
        # 1. Generic CGMiner fallback
        def gen_cg(req):
            cmd = req.get("command")
            if cmd == "version":
                return {"STATUS": [{"STATUS": "S"}], "VERSION": [{"Miner": "CustomMiner 99"}]}
            if cmd == "summary":
                return {"STATUS": [{"STATUS": "S"}], "SUMMARY": [{"MHS 5s": 4200000.0, "MHS av": 4100000.0, "Power": 180.0}]}
            if cmd == "devs":
                return {"STATUS": [{"STATUS": "S"}], "DEVS": [{"Temperature": 54.0, "Fan Speed": 2800}]}
            return {"STATUS": [{"STATUS": "S"}]}

        cg_srv = MockCGMinerServer(gen_cg)

        # 2. Generic Prometheus + HTTP-JSON server
        class GenHttpMock(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                if self.path == "/metrics":
                    body = (
                        "# HELP custom_hashrate_ghs Current hashrate\n"
                        'custom_hashrate_ghs{board="0"} 845.5\n'
                        "custom_temp_c 61.2\n"
                        "custom_watts 950\n"
                        "custom_fan_rpm 4100\n"
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/api/stats":
                    if self.headers.get("Authorization") != "Bearer tok-xyz":
                        self.send_response(401)
                        self.end_headers()
                        return
                    body = json.dumps({
                        "device": {"name": "CustomRig-X", "algo": "sha256d"},
                        "telemetry": {"ghs": 1500.0, "temp_c": 57.5, "power_w": 320.0, "fan": 3900},
                    }).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

        http_srv = start_http_server(GenHttpMock)
        try:
            g_cg = GenericCGMinerAdapter()
            pr_cg = g_cg.probe("127.0.0.1", cgminer_port=cg_srv.port)
            self.assertIsNotNone(pr_cg)
            self.assertEqual(pr_cg.family, "generic-cgminer")
            dev_cg = autotune.DeviceConfig(name="GenCG", ip="127.0.0.1", type="antminer", family="generic-cgminer", cgminer_port=cg_srv.port)
            rd_cg = autotune.fetch_antminer_reading(dev_cg)
            self.assertAlmostEqual(rd_cg.hashrate_hs, 4.2e12, delta=5e9)

            g_prom = GenericPrometheusAdapter()
            prom_cfg = {
                "endpoint_url": f"http://127.0.0.1:{http_srv.server_port}/metrics",
                "hashrate_metric": "custom_hashrate_ghs",
                "hashrate_unit": "GH/s",
                "temp_metric": "custom_temp_c",
                "power_metric": "custom_watts",
                "fan_metric": "custom_fan_rpm",
                "algorithm": "sha256d",
            }
            tele_prom = g_prom.fetch_telemetry("127.0.0.1", port=http_srv.server_port, adapter_config=prom_cfg)
            self.assertAlmostEqual(tele_prom["hashrate_hs"], 845.5e9, delta=1e6)
            self.assertEqual(tele_prom["asic_temp_c"], 61.2)
            self.assertEqual(tele_prom["power_w"], 950.0)
            self.assertEqual(tele_prom["fan_rpms"], [4100])
            self.assertFalse(g_prom.capabilities.restart)

            g_json = GenericHTTPJsonAdapter()
            json_cfg = {
                "endpoint_url": f"http://127.0.0.1:{http_srv.server_port}/api/stats",
                "hashrate_path": "telemetry.ghs",
                "hashrate_unit": "GH/s",
                "temp_path": "telemetry.temp_c",
                "power_path": "telemetry.power_w",
                "fan_path": "telemetry.fan",
                "model_path": "device.name",
                "algorithm_path": "device.algo",
            }
            tele_json = g_json.fetch_telemetry(
                "127.0.0.1",
                port=http_srv.server_port,
                api_token="tok-xyz",
                adapter_config=json_cfg,
            )
            self.assertEqual(tele_json["model"], "CustomRig-X")
            self.assertAlmostEqual(tele_json["hashrate_hs"], 1.5e12, delta=1e6)
            self.assertEqual(tele_json["asic_temp_c"], 57.5)
            self.assertEqual(tele_json["power_w"], 320.0)
            self.assertFalse(g_json.capabilities.restart)
        finally:
            cg_srv.close()
            http_srv.shutdown()
            http_srv.server_close()

    def test_compat_capture_includes_tier2_probes_and_redacts_secrets(self):
        class Tier2CompatHttp(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                if self.path == "/":
                    raw = b"<html><title>Goldshell Miner</title></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if self.path.startswith("/user/login"):
                    qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    if (qs.get("password") or [""])[0] == "gs-pass":
                        self._send(200, {"JWT Token": "gs-jwt-secret"})
                    else:
                        self._send(401, {"error": "bad password"})
                    return
                if self.path == "/mcb/status":
                    raw = json.dumps({
                        "model": "KD-Box-II",
                        "mac": "11:22:33:44:55:66",
                        "sn": "GS-SECRET-999",
                        "jwt": "eyJhbGciOiJIUzI1NiJ9.supersecret.sig",
                        "pool_user": "qz0000000000000000000000000000000000000000.worker1",
                    }).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if self.path == "/metrics":
                    raw = b'miner_hash_rate_5s{worker="secret_worker"} 100.5\n'
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self.send_response(404)
                self.end_headers()

        srv = start_http_server(Tier2CompatCompat := Tier2CompatHttp)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                res = nk_compat.collect_and_save_capture(
                    "127.0.0.1",
                    Path(tmp),
                    label="tier2-test",
                    http_port=srv.server_port,
                    cgminer_port=1,
                    tcp_timeout=0.3,
                    http_timeout=1.5,
                    app_version="1.15",
                )
                self.assertTrue(res["after_scan_clean"], res)
                zp = Path(res["path"])
                self.assertTrue(zp.exists())
                with zipfile.ZipFile(zp, "r") as zf:
                    names = set(zf.namelist())
                    self.assertIn("http_tier2.json", names)
                    self.assertIn("http_tier2.json", names)
                    blob = "\n".join(zf.read(n).decode("utf-8", errors="replace") for n in names)
                    self.assertNotIn("11:22:33:44:55:66", blob)
                    self.assertNotIn("GS-SECRET-999", blob)
                    self.assertNotIn("supersecret", blob)
                    self.assertNotIn("qz0000000000000000000000000000000000000000", blob)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
