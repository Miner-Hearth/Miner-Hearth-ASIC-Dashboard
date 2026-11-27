#!/usr/bin/env python3
"""Tests for Miner Hearth v1.15.4: persistent, redacted full event / telemetry log.

All data here is synthetic (BIP-173 test-vector address, made-up worker / SSID /
locally-administered MAC, documentation-style IPs).
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import tempfile
import time
import types
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

import autotune
import nk_eventlog
import miner_hearth
from nk_eventlog import EventLog, EventLogHandler, redact_entry

WALLET = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"  # public BIP-173 test vector
WORKER = "rig07"
POOL_USER = f"{WALLET}.{WORKER}"
SSID = "TestNet-5G"
MAC = "02:00:00:aa:bb:cc"
IP = "10.0.0.57"
T0 = datetime(2026, 3, 10, 12, 0, 0).timestamp()


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def read_all(el: EventLog) -> list[dict]:
    return list(el.iter_entries())


def types_of(el: EventLog) -> list[str]:
    return [e["type"] for e in read_all(el)]


def reading(**kw) -> autotune.Reading:
    base = dict(ok=True, reachable=True, hashrate=104000.0, power=2950.0, power_limit_w=2950.0, uptime_sec=30000.0,
                chip_temps=[61.0, 63.0], board_temps=[55.0], asic_temp=63.0, pool_url="stratum+tcp://pool.example.org",
                pool_port=3333, pool_user=POOL_USER, shares_accepted=1000, shares_rejected=1, hw_errors=0,
                best_session_diff=3_000_000.0, pools=[{"pool": 0, "url": "stratum+tcp://pool.example.org:3333", "user": POOL_USER,
                                                       "status": "Alive", "stratum_active": True, "best_share": 3_000_000}])
    base.update(kw)
    return autotune.Reading(**base)


class RedactionTests(unittest.TestCase):
    def test_entry_redaction(self):
        e = {"type": "x", "pool_user": POOL_USER, "wifi_ssid": SSID, "ssid": SSID, "mac": MAC,
             "message": f"CGMiner {IP}:4028 summary failed: timed out; mac {MAC}; user {WALLET}.{WORKER} password=hunter22 token: abc123xyz",
             "path": r"C:\Users\SomePerson\AppData\Local\Programs\Miner Hearth\userdata",
             "nested": {"stratumUser": POOL_USER, "list": [f"stratum+tcp://{POOL_USER}@pool.example.org:3333"]},
             "has_password": True, "hashrate": 1.23456789}
        out = redact_entry(e)
        s = json.dumps(out)
        for bad in (WALLET, WORKER, SSID, MAC, IP, "hunter22", "abc123xyz", "SomePerson"):
            self.assertNotIn(bad, s, bad)
        self.assertEqual(out["pool_user"], "[redacted]")
        self.assertIn("10.0.0.x", out["message"])
        self.assertIn("timed out", out["message"])
        self.assertIn(r"C:\Users\[user]", out["path"])
        self.assertTrue(out["has_password"])
        self.assertEqual(out["hashrate"], 1.2346)

    def test_literal_secrets_scrubbed_from_free_text(self):
        out = redact_entry({"message": "connected to HomeWifiXYZ ok"}, secrets=["HomeWifiXYZ"])
        self.assertNotIn("HomeWifiXYZ", json.dumps(out))

    def test_written_file_is_redacted(self):
        with tempfile.TemporaryDirectory() as d:
            el = EventLog(Path(d), clock=Clock())
            el.add_secret(POOL_USER)
            el.write("log", device="Miner A", message=f"pool user {POOL_USER} on {SSID} at {IP} mac {MAC}", ssid=SSID)
            raw = b"".join(p.read_bytes() for p in el.files()).decode()
            for bad in (WALLET, WORKER, SSID, MAC, IP):
                self.assertNotIn(bad, raw)
            self.assertIn("Miner A", raw)

    def test_device_label_never_replaced_by_learned_secret(self):
        with tempfile.TemporaryDirectory() as d:
            el = EventLog(Path(d), clock=Clock())
            el.write("snapshot", device="Rig Seven", pools=[{"user": "Rig Seven"}])
            e = read_all(el)[0]
            self.assertEqual(e["device"], "Rig Seven")
            self.assertEqual(e["pools"][0]["user"], "[redacted]")

    def test_settings_normalization(self):
        d = nk_eventlog.normalize_settings(None)
        self.assertEqual(d, {"enabled": True, "snapshot_min": 5.0, "raw_snapshot_min": 60.0, "retention_days": 14, "max_mb": 200})
        d = nk_eventlog.normalize_settings({"enabled": False, "snapshot_min": 0, "retention_days": 9999, "max_mb": "x"})
        self.assertFalse(d["enabled"])
        self.assertEqual(d["snapshot_min"], 1.0)
        self.assertEqual(d["retention_days"], 365)
        self.assertEqual(d["max_mb"], 200)
        self.assertEqual(miner_hearth.default_app_settings()["eventlog"]["enabled"], True)
        self.assertEqual(miner_hearth.normalize_app_settings({"eventlog": {"snapshot_min": 10}})["eventlog"]["snapshot_min"], 10.0)


class RotationTests(unittest.TestCase):
    def test_daily_rotation_compresses_and_reads_back(self):
        with tempfile.TemporaryDirectory() as d:
            clk = Clock()
            el = EventLog(Path(d), clock=clk)
            el.write("a")
            clk.t += 86400
            el.write("b")
            names = sorted(p.name for p in el.files())
            self.assertEqual(len(names), 2)
            self.assertTrue(names[0].endswith(".jsonl.gz"), names)
            self.assertTrue(names[1].endswith(".jsonl"), names)
            self.assertEqual(types_of(el), ["a", "b"])

    def test_retention_days(self):
        with tempfile.TemporaryDirectory() as d:
            clk = Clock()
            el = EventLog(Path(d), clock=clk, settings={"retention_days": 3})
            for _ in range(6):
                el.write("day")
                clk.t += 86400
            el.write("today")
            days = [EventLog._file_day(p) for p in el.files()]
            self.assertLessEqual(len(days), 4)
            self.assertEqual(days[-1], datetime.fromtimestamp(clk.t).strftime("%Y-%m-%d"))

    def test_size_cap_prunes_oldest_and_keeps_today(self):
        with tempfile.TemporaryDirectory() as d:
            clk = Clock()
            el = EventLog(Path(d), clock=clk, settings={"max_mb": 10})
            for i in range(3):  # fake older day files, 4 MB each
                p = Path(d) / f"nk-events-2026-03-0{i + 1}.jsonl.gz"
                p.write_bytes(os.urandom(4 * 1024 * 1024))
            el.write("now")
            el.maintain()
            days = [EventLog._file_day(p) for p in el.files()]
            self.assertNotIn("2026-03-01", days)
            self.assertIn("2026-03-10", days)
            self.assertLessEqual(sum(p.stat().st_size for p in el.files()), 10 * 1024 * 1024)

    def test_never_raises_when_disk_fails(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "logs"
            blocker.write_text("not a directory")
            el = EventLog(blocker, clock=Clock())
            self.assertIsNone(el.write("x"))
            self.assertEqual(el.write_errors, 1)
            el.observe_poll("A", reading())  # must not raise either

    def test_disabled_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            el = EventLog(Path(d), clock=Clock(), settings={"enabled": False})
            el.write("x")
            self.assertEqual(el.files(), [])


class DeviceObservationTests(unittest.TestCase):
    def test_comm_loss_and_restore_with_miner_still_running(self):
        with tempfile.TemporaryDirectory() as d:
            clk = Clock()
            el = EventLog(Path(d), clock=clk)
            el.observe_poll("Miner A", reading(uptime_sec=10000), latency_ms=120)
            for i in range(3):
                clk.t += 15
                el.observe_poll("Miner A", autotune.Reading(ok=False, error=f"CGMiner {IP}:4028 summary failed: timed out"),
                                latency_ms=8000, exc_class="DeviceOffline <- TimeoutError")
            clk.t += 15
            el.observe_poll("Miner A", reading(uptime_sec=10060), latency_ms=150)
            ents = read_all(el)
            lost = [e for e in ents if e["type"] == "comm_lost"]
            self.assertEqual(len(lost), 1)
            self.assertIn("timed out", lost[0]["error"])
            self.assertNotIn(IP, lost[0]["error"])
            self.assertEqual(lost[0]["exception_class"], "DeviceOffline <- TimeoutError")
            rest = [e for e in ents if e["type"] == "comm_restored"][0]
            self.assertEqual(rest["consecutive_failures"], 3)
            self.assertTrue(rest["miner_kept_running"])
            self.assertIn("lost contact", rest["verdict"])
            self.assertNotIn("device_rebooted", [e["type"] for e in ents])

    def test_reboot_detected_when_uptime_goes_backwards(self):
        with tempfile.TemporaryDirectory() as d:
            clk = Clock()
            el = EventLog(Path(d), clock=clk)
            el.observe_poll("Miner A", reading(uptime_sec=50000))
            clk.t += 15
            el.observe_poll("Miner A", reading(uptime_sec=50015))
            clk.t += 15
            el.observe_poll("Miner A", reading(uptime_sec=40))
            reb = [e for e in read_all(el) if e["type"] == "device_rebooted"]
            self.assertEqual(len(reb), 1)
            self.assertEqual(reb[0]["uptime_now_sec"], 40)

    def test_reboot_detected_across_nk_restart(self):
        with tempfile.TemporaryDirectory() as d:
            clk = Clock()
            el = EventLog(Path(d), clock=clk)
            el.observe_poll("Miner A", reading(uptime_sec=50000))  # first poll writes a snapshot -> persisted uptime
            clk.t += 3600
            el2 = EventLog(Path(d), clock=clk)
            el2.observe_poll("Miner A", reading(uptime_sec=600))  # booted 10 min ago, i.e. while NK was closed
            self.assertIn("device_rebooted", types_of(el2))
            clk.t += 15
            el3 = EventLog(Path(d), clock=clk)
            el3.observe_poll("Miner A", reading(uptime_sec=615))
            self.assertEqual(types_of(el3).count("device_rebooted"), 1)

    def test_comm_loss_during_reboot_verdict(self):
        with tempfile.TemporaryDirectory() as d:
            clk = Clock()
            el = EventLog(Path(d), clock=clk)
            el.observe_poll("Miner A", reading(uptime_sec=50000))
            clk.t += 15
            el.observe_poll("Miner A", autotune.Reading(ok=False, error="connection refused"))
            clk.t += 300
            el.observe_poll("Miner A", reading(uptime_sec=200))
            rest = [e for e in read_all(el) if e["type"] == "comm_restored"][0]
            self.assertFalse(rest["miner_kept_running"])
            self.assertIn("device_rebooted", types_of(el))

    def test_multi_device_comm_loss_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            clk = Clock()
            el = EventLog(Path(d), clock=clk)
            for n in ("Miner A", "Miner B", "Axe C"):
                el.observe_poll(n, reading())
            clk.t += 15
            el.observe_poll("Miner A", autotune.Reading(ok=False, error="timed out"))
            clk.t += 5
            el.observe_poll("Miner B", autotune.Reading(ok=False, error="timed out"))
            el.observe_poll("Axe C", reading())
            multi = [e for e in read_all(el) if e["type"] == "multi_device_comm_loss"]
            self.assertEqual(len(multi), 1)
            self.assertEqual(multi[0]["devices"], ["Miner A", "Miner B"])
            self.assertEqual(multi[0]["still_online"], ["Axe C"])

    def test_ongoing_failure_is_summarized_not_spammed(self):
        with tempfile.TemporaryDirectory() as d:
            clk = Clock()
            el = EventLog(Path(d), clock=clk, settings={"snapshot_min": 120})
            el.observe_poll("Miner A", reading())
            for _ in range(45):
                clk.t += 15
                el.observe_poll("Miner A", autotune.Reading(ok=False, error="timed out"))
            t = types_of(el)
            self.assertEqual(t.count("comm_lost"), 1)
            self.assertEqual(t.count("comm_still_failing"), 2)  # at 20 and 40 failures

    def test_snapshot_pool_switch_and_session_reset(self):
        with tempfile.TemporaryDirectory() as d:
            clk = Clock()
            el = EventLog(Path(d), clock=clk, settings={"snapshot_min": 5})
            el.observe_poll("Miner A", reading(), latency_ms=100)
            clk.t += 60
            el.observe_poll("Miner A", reading(best_session_diff=10.0, pools=[
                {"pool": 0, "url": "stratum+tcp://pool.example.org:3333", "status": "Dead", "stratum_active": False},
                {"pool": 1, "url": "stratum+tcp://backup.example.net:3333", "status": "Alive", "stratum_active": True}]))
            ents = read_all(el)
            t = [e["type"] for e in ents]
            for want in ("comm_first_ok", "snapshot", "raw_snapshot", "pool_switch", "pool_failover", "pool_status_change", "session_best_reset"):
                self.assertIn(want, t, want)
            snap = next(e for e in ents if e["type"] == "snapshot")
            self.assertEqual(snap["power_limit_w"], 2950.0)
            self.assertEqual(snap["uptime_sec"], 30000.0)
            self.assertEqual(snap["api"]["latency_ms"]["avg"], 100.0)
            self.assertEqual(snap["pools"][0]["user"], "[redacted]")
            self.assertEqual(t.count("snapshot"), 1)  # interval not reached yet
            clk.t += 300
            el.observe_poll("Miner A", reading())
            self.assertEqual(types_of(el).count("snapshot"), 2)


class HubIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clk = Clock()
        self.hub = miner_hearth.FeatureHub(Path(self.tmp.name) / "userdata", clock=self.clk)
        self.hub.notifier.toast = lambda *a, **k: False
        self.hub.notifier.play_local = lambda *a, **k: False

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, name="Miner A"):
        return types.SimpleNamespace(name=name, type="antminer", bos_password="", bos_power_mgmt=False, web_password="",
                                     asic_max_c=95, vr_max_c=95, vr_target_c=80, error_high_pct=1.0)

    def test_alert_clear_does_not_erase_log(self):
        el = self.hub.eventlog
        ev = self.hub.emit(miner_hearth._event("offline", "problem", "Miner A", "Miner A is offline", "No usable telemetry for 3 min (timed out)."))
        code, body, ctype = self.hub.handle("POST", "/api/nk/eventlog/mark", "", json.dumps(
            {"action": "alert_dismissed", "id": ev["id"], "device": "Miner A", "kind": "offline", "title": ev["title"]}).encode(), False)
        self.assertEqual(code, 200)
        self.hub.events.clear()  # banners gone
        t = types_of(el)
        self.assertIn("alert", t)
        self.assertIn("user_alert_dismissed", t)
        alert = next(e for e in read_all(el) if e["type"] == "alert")
        self.assertEqual(alert["kind"], "offline")
        self.assertIn("timed out", alert["message"])
        bad = self.hub.handle("POST", "/api/nk/eventlog/mark", "", b'{"action":"rm -rf"}', False)
        self.assertEqual(bad[0], 400)

    def test_on_poll_logs_alert_transitions_and_snapshot(self):
        el = self.hub.eventlog
        cfg = self.cfg()
        self.hub.on_poll(cfg, reading(), self.clk.t)
        for _ in range(16):  # > 3 min offline -> offline alert raised
            self.clk.t += 15
            self.hub.on_poll(cfg, autotune.Reading(ok=False, error="CGMiner summary failed: timed out"), self.clk.t)
        self.clk.t += 15
        self.hub.on_poll(cfg, reading(uptime_sec=30300), self.clk.t)
        t = types_of(el)
        for want in ("comm_first_ok", "snapshot", "comm_lost", "alert", "alert_raised", "alert_recovered", "comm_restored"):
            self.assertIn(want, t, want)
        raised = next(e for e in read_all(el) if e["type"] == "alert_raised")
        self.assertEqual(raised["kind"], "offline")
        self.assertIn("timed out", raised["error"])

    def test_tuning_and_settings_changes_logged_redacted(self):
        el = self.hub.eventlog
        self.hub.log_tuning("Miner A", "power_target", 3250, 2950, "TOU cap", "power_down", source="schedule")
        self.hub.update_settings({"block": {"payout_addresses": [WALLET]}, "lan": {"pin": "123456"},
                                  "webhooks": {"telegram_chat_id": "987654321"}, "eventlog": {"snapshot_min": 10}})
        ents = read_all(el)
        tun = next(e for e in ents if e["type"] == "tuning")
        self.assertEqual((tun["kind"], tun["old"], tun["new"], tun["source"]), ("power_target", 3250, 2950, "schedule"))
        sc = next(e for e in ents if e["type"] == "settings_changed")
        paths = {c["path"] for c in sc["changes"]}
        self.assertIn("eventlog.snapshot_min", paths)
        blob = json.dumps(ents)
        for bad in (WALLET, "123456", "987654321"):
            self.assertNotIn(bad, blob)
        self.assertEqual(el.cfg["snapshot_min"], 10.0)

    def test_tou_window_enter_exit(self):
        el = self.hub.eventlog
        dt = datetime.fromtimestamp(self.clk.t)
        start = f"{dt.hour:02d}:{(dt.minute + 1) % 60:02d}" if dt.minute < 58 else "23:59"
        self.hub.settings.update({"schedule": {"enabled": True, "windows": [
            {"name": "Peak", "enabled": True, "days": list(range(7)), "start": start, "end": "23:59", "devices": [], "bos_power_target_w": 2000}]}})
        self.hub._eventlog_tick(self.hub.settings.get(), self.clk.t)
        self.clk.t += 120
        self.hub._eventlog_tick(self.hub.settings.get(), self.clk.t)
        ents = [e for e in read_all(el) if e["type"].startswith("tou_window")]
        self.assertEqual(ents[0]["type"], "tou_window_enter")
        self.assertEqual(ents[0]["window"], "Peak")
        self.assertEqual(ents[0]["bos_power_target_w"], 2000)
        self.hub.settings.update({"schedule": {"enabled": False}})
        self.clk.t += 60
        self.hub._eventlog_tick(self.hub.settings.get(), self.clk.t)
        self.assertIn("tou_window_exit", types_of(el))
        self.assertIn("heartbeat", types_of(el))

    def test_start_stop_unclean_and_log_handler(self):
        el = self.hub.eventlog
        self.hub._start_eventlog()
        try:
            log = logging.getLogger("autotune")
            old_level = logging.getLogger().level
            logging.getLogger().setLevel(logging.INFO)
            for _ in range(5):
                log.warning("%s: offline — %s", "Miner A", f"CGMiner {IP}:4028 summary failed: timed out")
            try:
                raise ValueError("boom")
            except ValueError:
                log.exception("Miner Hearth poll hook failed for %s", "Miner A")
            logging.getLogger().setLevel(old_level)
        finally:
            self.hub._stop_eventlog()
        ents = read_all(el)
        logs = [e for e in ents if e["type"] == "log"]
        offl = [e for e in logs if "offline" in e["message"]]
        self.assertEqual(len(offl), 1)  # de-duplicated
        self.assertEqual(offl[0]["device"], "Miner A")
        self.assertNotIn(IP, offl[0]["message"])
        exc = next(e for e in logs if e.get("exception_class") == "ValueError")
        self.assertIn("Traceback", exc["traceback"])
        t = [e["type"] for e in ents]
        self.assertIn("app_start", t)
        self.assertIn("app_stop", t)
        # a run that never stopped is reported on the next start
        hub2 = miner_hearth.FeatureHub(Path(self.tmp.name) / "userdata", clock=self.clk)
        hub2._start_eventlog()
        hub3 = miner_hearth.FeatureHub(Path(self.tmp.name) / "userdata", clock=self.clk)
        hub3._start_eventlog()
        hub3._stop_eventlog()
        hub2._stop_eventlog()
        self.assertEqual(types_of(el).count("unclean_shutdown_detected"), 1)

    def test_uncaught_thread_exception_logged(self):
        import threading
        self.hub._start_eventlog()
        try:
            import contextlib
            prev = self.hub._prev_hooks.get("thread")
            self.hub._prev_hooks["thread"] = prev
            with contextlib.redirect_stderr(io.StringIO()):
                th = threading.Thread(target=lambda: (_ for _ in ()).throw(RuntimeError("worker died")), name="nk-test")
                th.start()
                th.join()
        finally:
            self.hub._stop_eventlog()
        e = next(x for x in read_all(self.hub.eventlog) if x["type"] == "uncaught_exception")
        self.assertEqual(e["exception_class"], "RuntimeError")
        self.assertIn("nk-test", e["where"])

    def test_export_zip_ranges_anonymize_and_privacy(self):
        el = self.hub.eventlog
        el.add_secret(POOL_USER)
        el.write("old_event", device="Miner A", t=self.clk.t - 3 * 86400, message="three days ago")
        el.write("comm_lost", device="Miner A", error=f"{IP} timed out user {POOL_USER}")
        el.write("comm_lost", device="Axe B", error="timed out")
        code, data, ctype = self.hub.handle("GET", "/api/nk/eventlog/export", "range=24h", b"", False)
        self.assertEqual((code, ctype), (200, "application/zip"))
        zf = zipfile.ZipFile(io.BytesIO(data))
        self.assertIn("nk-events.jsonl", zf.namelist())
        self.assertIn("manifest.json", zf.namelist())
        self.assertIn("diagnostics.json", zf.namelist())
        lines = [json.loads(x) for x in zf.read("nk-events.jsonl").decode().splitlines()]
        self.assertNotIn("old_event", [x["type"] for x in lines])
        blob = b"".join(zf.read(n) for n in zf.namelist()).decode()
        for bad in (WALLET, WORKER, IP):
            self.assertNotIn(bad, blob)
        man = json.loads(zf.read("manifest.json"))
        self.assertEqual(man["range"], "24h")
        _, data_all, _ = self.hub.handle("GET", "/api/nk/eventlog/export", "range=all&anon=1", b"", False)
        zf2 = zipfile.ZipFile(io.BytesIO(data_all))
        ev2 = zf2.read("nk-events.jsonl").decode()
        self.assertIn("old_event", ev2)
        self.assertNotIn("Miner A", ev2)
        self.assertIn("Machine 1", ev2)
        self.assertIn("user_export_log", types_of(el))
        self.assertEqual(self.hub.handle("GET", "/api/nk/eventlog/export", "range=24h", b"", True)[0], 403)
        code, body, _ = self.hub.handle("GET", "/api/nk/eventlog/status", "", b"", False)
        st = json.loads(body)["status"]
        self.assertGreaterEqual(st["files"], 2)

    def test_remove_device_keeps_entries(self):
        el = self.hub.eventlog
        el.write("comm_lost", device="Miner A", error="timed out")
        self.hub.on_remove("Miner A")
        t = types_of(el)
        self.assertIn("comm_lost", t)
        self.assertIn("device_removed", t)


class ShippedFilesTests(unittest.TestCase):
    def test_version_and_ui(self):
        root = Path(__file__).resolve().parent
        self.assertEqual((root / "VERSION").read_text().strip(), "1.17.1")
        import dashboard
        self.assertIn('id="nkSetEventLog"', dashboard.DASHBOARD_HTML)
        self.assertIn("nk115Export", dashboard.DASHBOARD_HTML)
        cfg = json.loads((root / "config.json").read_text())
        self.assertEqual(cfg["devices"], [])

    def test_desktop_api_binary_save(self):
        import base64
        import miner_dashboard
        with tempfile.TemporaryDirectory() as d:
            api = miner_dashboard.DesktopApi()
            orig = Path.home
            try:
                Path.home = staticmethod(lambda: Path(d))  # type: ignore[assignment]
                (Path(d) / "Downloads").mkdir()
                res = api.save_file_b64("x.zip", base64.b64encode(b"PK\x03\x04data").decode())
            finally:
                Path.home = orig  # type: ignore[assignment]
            self.assertTrue(res.startswith("saved to"), res)
            self.assertEqual((Path(d) / "Downloads" / "x.zip").read_bytes(), b"PK\x03\x04data")


if __name__ == "__main__":
    unittest.main()
