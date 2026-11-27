#!/usr/bin/env python3
"""Tests for Miner Hearth v1.15.5: pool switches / restarts are recognised and do not raise fault alerts.

All data is synthetic: made-up machine names, documentation-style pool hosts, the public BIP-173
test-vector address as a pool user.
"""

from __future__ import annotations

import json
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

import autotune
import nk_faults
import nk_grace
import miner_hearth

WALLET = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"  # public BIP-173 test vector
POOL_A = ("stratum+tcp://pool-a.example.org", 3333)
POOL_B = ("stratum+tcp://pool-b.example.net", 4444)
FALLBACK = ("stratum+tcp://backup.example.com", 3333)
T0 = datetime(2026, 3, 10, 12, 0, 0).timestamp()
POLL = 15.0


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def axe(uptime: float, pool=POOL_A, hr: float = 1100.0, acc: int = 500, temp: float = 55.0, conn: str = "Connected",
        fallback: bool = False, ok: bool = True, **raw_extra) -> autotune.Reading:
    if not ok:
        return autotune.Reading(ok=False, error="HTTP GET /api/system/info failed: timed out")
    raw = {"ASICModel": "BM1370", "temp": temp, "vrTemp": 50.0, "hashRate": hr, "power": 18.0, "poolConnectionInfo": conn,
           "uptimeSeconds": uptime, "fanrpm": 4000, "fanspeed": 60, "overheat_mode": 0, "isUsingFallbackStratum": int(fallback),
           "sharesAccepted": acc, "sharesRejected": 0, "expectedHashrate": 1150.0}
    raw.update(raw_extra)
    return autotune.Reading(ok=True, reachable=True, hashrate=hr, expected_hashrate=1150.0, power=18.0, asic_temp=temp, vr_temp=50.0,
                            uptime_sec=uptime, fault_raw=raw, model="BM1370", pool_url=pool[0], pool_port=pool[1],
                            pool_user=f"{WALLET}.unit1", fallback_pool_url=FALLBACK[0], fallback_pool_port=FALLBACK[1],
                            fallback_pool_user=f"{WALLET}.unit1", shares_accepted=acc, shares_rejected=0,
                            using_fallback_stratum=fallback, fans=[{"id": 0, "rpm": 4000, "speed_pct": 60.0}])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clk = Clock()
        self.hub = miner_hearth.FeatureHub(Path(self.tmp.name) / "userdata", clock=self.clk)
        self.hub.notifier.toast = lambda *a, **k: False
        self.hub.notifier.play_local = lambda *a, **k: False
        self.cfg = types.SimpleNamespace(name="Test Rig", type="gamma", bos_password="", bos_power_mgmt=False, web_password="",
                                         asic_max_c=65, vr_max_c=70, vr_target_c=65, error_high_pct=3.0)
        self.up = 50000.0
        self.acc = 500

    def tearDown(self):
        self.tmp.cleanup()

    def poll(self, r: autotune.Reading) -> list[dict]:
        out = self.hub.on_poll(self.cfg, r, self.clk.t)
        self.clk.t += POLL
        return out

    def steady(self, minutes: float, pool=POOL_A, **kw) -> None:
        for _ in range(int(minutes * 60 / POLL)):
            self.up += POLL
            self.acc += 1
            self.poll(axe(self.up, pool=pool, acc=self.acc, **kw))

    def boot(self, pool=POOL_B, offline_polls: int = 4) -> None:
        """Miner restarts: offline, then the usual first readings after boot, then normal."""
        for _ in range(offline_polls):
            self.poll(axe(0, ok=False))
        self.up, self.acc = 14.0, 0
        self.poll(axe(self.up, pool=pool, hr=0.0, acc=0, temp=-1.0, conn="Not Connected", fanrpm=0))
        self.up += POLL
        self.poll(axe(self.up, pool=pool, hr=400.0, acc=0, conn="Connected"))
        self.up += POLL
        self.acc = 1
        self.poll(axe(self.up, pool=pool, hr=1050.0, acc=1))

    def alerts(self) -> list[dict]:
        """Banners actually raised (these are what go to sound / toast / webhooks)."""
        return [e for e in self.hub.events if e.get("level") in {"problem", "warn"}]

    def log_types(self) -> list[str]:
        return [e["type"] for e in self.hub.eventlog.iter_entries()]


class PoolSwitchTests(Base):
    def test_pool_switch_then_reboot_raises_no_alerts(self):
        self.steady(40)
        self.up += POLL
        self.poll(axe(self.up, pool=POOL_B, acc=self.acc))  # config saved on the miner, it restarts right after
        self.boot()
        self.steady(20, pool=POOL_B)
        self.assertEqual(self.alerts(), [], [e["title"] for e in self.alerts()])
        self.assertFalse(self.hub.fault_history.active.get("Test Rig"))
        t = self.log_types()
        for want in ("pool_change_detected", "maintenance_grace_start", "maintenance_grace_end", "alert_suppressed"):
            self.assertIn(want, t, want)
        end = next(e for e in self.hub.eventlog.iter_entries() if e["type"] == "maintenance_grace_end")
        self.assertEqual(end["result"], "recovered")
        self.assertIsNone(self.hub.grace.card_info("Test Rig"))
        logged = json.dumps(list(self.hub.eventlog.iter_entries()))
        self.assertNotIn(WALLET, logged)

    def test_pool_change_seen_only_after_reboot(self):
        self.steady(40)
        self.boot(offline_polls=6)
        self.assertIn("Pool switch detected", self.hub.grace.card_info("Test Rig")["label"])
        self.steady(1, pool=POOL_B)
        self.assertIsNone(self.hub.grace.card_info("Test Rig"))  # ended early: hashing + shares on the new pool
        self.steady(20, pool=POOL_B)
        self.assertEqual(self.alerts(), [], [e["title"] for e in self.alerts()])
        self.assertIn("pool_change_detected", self.log_types())

    def test_card_shows_info_note_during_grace(self):
        self.steady(5)
        self.up += POLL
        self.poll(axe(self.up, pool=POOL_B, acc=self.acc))
        self.poll(axe(0, ok=False))
        info = self.hub.grace.card_info("Test Rig")
        self.assertIsNotNone(info)
        self.assertIn("Pool switch detected", info["label"])
        self.assertIn("paused up to 10 min", info["label"])

    def test_never_comes_back_alerts_after_grace_with_context(self):
        self.steady(40)
        self.up += POLL
        self.poll(axe(self.up, pool=POOL_B, acc=self.acc))
        start = self.clk.t
        fired_at = None
        for _ in range(int(20 * 60 / POLL)):
            self.poll(axe(0, ok=False))
            if self.alerts() and fired_at is None:
                fired_at = self.clk.t
        a = self.alerts()
        self.assertEqual(len(a), 1, [e["title"] for e in a])
        self.assertEqual(a[0]["kind"], "offline")
        self.assertIn("did not recover after the pool switch", a[0]["title"])
        self.assertGreaterEqual(fired_at - start, 10 * 60)
        self.assertLess(fired_at - start, 11 * 60)
        end = [e for e in self.hub.eventlog.iter_entries() if e["type"] == "maintenance_grace_end"][-1]
        self.assertEqual(end["result"], "expired_not_recovered")

    def test_online_but_never_sends_shares_alerts_after_grace(self):
        self.steady(40)
        self.up += POLL
        self.poll(axe(self.up, pool=POOL_B, acc=self.acc))
        self.boot()
        # hashing but the new pool never accepts a share (e.g. wrong port / user)
        for _ in range(int(12 * 60 / POLL)):
            self.up += POLL
            self.poll(axe(self.up, pool=POOL_B, acc=1))
        kinds = [e["kind"] for e in self.alerts()]
        self.assertIn("no_shares", kinds)
        self.assertTrue(any("did not recover" in e["title"] for e in self.alerts()))

    def test_real_overheat_during_grace_still_alerts(self):
        self.steady(10)
        self.up += POLL
        self.poll(axe(self.up, pool=POOL_B, acc=self.acc))
        self.boot()
        self.hub.expect_transition("Test Rig", "nk_restart", "test")   # make sure a grace is open
        self.up += POLL
        self.poll(axe(self.up, pool=POOL_B, acc=2, overheat_mode=1))
        hw = [e for e in self.alerts() if e["kind"] == "hardware_fault"]
        self.assertTrue(hw, [e["title"] for e in self.alerts()])
        self.assertTrue(any("verheat" in e["title"] for e in hw))

    def test_real_failover_still_fires_fallback_alert(self):
        self.steady(10)
        self.up += POLL
        self.poll(axe(self.up, pool=POOL_A, acc=self.acc + 1, fallback=True))
        kinds = [e["kind"] for e in self.alerts()]
        self.assertIn("fallback_pool", kinds)
        self.assertNotIn("pool_change_detected", self.log_types())

    def test_switch_that_lands_on_fallback_alerts_after_grace(self):
        self.steady(10)
        self.up += POLL
        self.poll(axe(self.up, pool=POOL_B, acc=self.acc))
        for _ in range(int(12 * 60 / POLL)):
            self.up += POLL
            self.acc += 1
            self.poll(axe(self.up, pool=POOL_B, acc=self.acc, fallback=True))
        fb = [e for e in self.alerts() if e["kind"] == "fallback_pool"]
        self.assertEqual(len(fb), 1)
        self.assertIn("did not recover", fb[0]["title"])

    def test_grace_disabled_keeps_old_behaviour(self):
        self.hub.update_settings({"alerts": {"maintenance": {"grace_enabled": False, "boot_settle_enabled": False}}})
        self.steady(10)
        self.up += POLL
        self.poll(axe(self.up, pool=POOL_B, acc=self.acc))
        for _ in range(int(5 * 60 / POLL)):
            self.poll(axe(0, ok=False))
        self.assertIn("offline", [e["kind"] for e in self.alerts()])


class BootAndRestartTests(Base):
    def test_boot_transients_suppressed_after_any_reboot(self):
        self.steady(10)
        self.boot(pool=POOL_A)  # unexpected reboot, same pool
        self.steady(5)
        self.assertEqual([e for e in self.alerts() if e["kind"] == "hardware_fault"], [])
        fx = self.hub.faults_now.get("Test Rig") or []
        self.assertFalse([f for f in fx if f.get("severity") != "info"])
        self.assertIn("alert_suppressed", self.log_types())

    def test_same_fault_long_after_boot_alerts(self):
        self.steady(10)
        self.up += POLL
        self.poll(axe(self.up, acc=self.acc + 1, temp=-1.0))
        hw = [e for e in self.alerts() if e["kind"] == "hardware_fault"]
        self.assertTrue(hw)
        self.assertIn("Temperature sensor", hw[0]["title"])

    def test_nk_initiated_restart_opens_grace(self):
        self.steady(40)
        self.hub.expect_transition("Test Rig", "nk_restart", "restart after a tuning change")
        self.boot(pool=POOL_A, offline_polls=14)  # 3.5 min offline would normally alert
        self.steady(10)
        self.assertEqual(self.alerts(), [], [e["title"] for e in self.alerts()])

    def test_tuner_pool_change_notes_transition(self):
        calls = []
        fake_hub = types.SimpleNamespace(expect_transition=lambda n, k, r: calls.append((n, k, r)))
        tuner = types.SimpleNamespace(hub=fake_hub, shared=None)
        autotune.Tuner._note_transition(tuner, "Test Rig", "nk_pool_change", "pool settings saved")
        self.assertEqual(calls, [("Test Rig", "nk_pool_change", "pool settings saved")])

    def test_err_pct_regression_faults_clear(self):
        raw = {"ASICModel": "BM1370", "temp": 55.0, "hashRate": 1100.0, "power": 18.0, "poolConnectionInfo": "Connected",
               "asic_error_counts": [3, 0, 1], "errorPercentage": 0.4}
        nk_faults.collect_faults(raw, 5000)   # used to raise UnboundLocalError
        self.steady(2, asic_error_counts=[2, 1])
        self.up += POLL
        self.poll(axe(self.up, acc=self.acc + 1, temp=-1.0, asic_error_counts=[2, 1]))
        self.assertTrue(self.hub.faults_now.get("Test Rig"))
        self.up += POLL
        self.poll(axe(self.up, acc=self.acc + 2, asic_error_counts=[2, 1]))
        self.assertFalse([f for f in self.hub.faults_now.get("Test Rig") or [] if f["key"] == "axeos:temp_sensor_error"])

    def test_vr_warmup_after_boot_does_not_step_down(self):
        from test_autotune import nerd_cfg, reading
        cfg_n = nerd_cfg(max_freq=800, vr_target_c=65, vr_max_c=70)
        warm = autotune.decide(reading(asic_temp=50, vr_temp=56.5, frequency=700, hashrate=9000, uptime_sec=60),
                               cfg_n, 5, error_pct=0.1, vr_rise_c=9.0, vr_rise_dt=55.0)
        self.assertNotIn("VR rapid rise", warm.reason)
        later = autotune.decide(reading(asic_temp=50, vr_temp=56.5, frequency=700, hashrate=9000, uptime_sec=4000),
                                cfg_n, 5, error_pct=0.1, vr_rise_c=9.0, vr_rise_dt=55.0)
        self.assertIn("VR rapid rise", later.reason)
        hot = autotune.decide(reading(asic_temp=50, vr_temp=62, frequency=700, hashrate=9000, uptime_sec=60),
                              cfg_n, 5, error_pct=0.1, vr_rise_c=9.0, vr_rise_dt=55.0)
        self.assertEqual(hot.action, "step_down")


class ManualPauseTests(Base):
    def test_pause_route_suppresses_and_resume_restores(self):
        self.steady(10)
        code, body, _ = self.hub.handle("POST", "/api/nk/maintenance", "", json.dumps({"device": "Test Rig", "minutes": 30}).encode(), False)
        self.assertEqual(code, 200)
        info = json.loads(body)["maintenance"]
        self.assertTrue(info["manual"])
        self.assertIn("Maintenance: alerts paused for 30 more min", info["label"])
        for _ in range(int(6 * 60 / POLL)):
            self.poll(axe(0, ok=False))
        self.assertEqual(self.alerts(), [])
        code, body, _ = self.hub.handle("POST", "/api/nk/maintenance", "", json.dumps({"device": "Test Rig", "minutes": 0}).encode(), False)
        self.assertEqual(code, 200)
        self.poll(axe(0, ok=False))
        self.assertIn("offline", [e["kind"] for e in self.alerts()])
        t = self.log_types()
        self.assertIn("maintenance_pause_start", t)
        self.assertIn("maintenance_pause_end", t)

    def test_pause_expires_on_its_own(self):
        self.steady(5)
        self.hub.pause_alerts("Test Rig", 5)
        for _ in range(int(4 * 60 / POLL)):
            self.poll(axe(0, ok=False))
        self.assertEqual(self.alerts(), [])
        for _ in range(int(3 * 60 / POLL)):
            self.poll(axe(0, ok=False))
        self.assertIn("offline", [e["kind"] for e in self.alerts()])

    def test_pause_does_not_hide_overheat(self):
        self.steady(5)
        self.hub.pause_alerts("Test Rig", 60)
        self.up += POLL
        self.poll(axe(self.up, acc=self.acc + 1, overheat_mode=1))
        self.assertTrue([e for e in self.alerts() if e["kind"] == "hardware_fault"])

    def test_suppressed_alert_skips_toast_sound_and_webhooks(self):
        calls = []
        self.hub.notifier.toast = lambda *a, **k: calls.append("toast")
        self.hub.notifier.play_local = lambda *a, **k: calls.append("sound")
        self.hub.webhooks.send = lambda *a, **k: calls.append("webhook")
        self.hub.pause_alerts("Test Rig", 30)
        ev = self.hub.emit(miner_hearth._event("offline", "problem", "Test Rig", "Test Rig is offline", "x"))
        self.assertTrue(ev.get("suppressed"))
        self.assertEqual(calls, [])
        self.assertEqual(self.alerts(), [])
        self.hub.pause_alerts("Test Rig", 0)
        self.hub.emit(miner_hearth._event("offline", "problem", "Test Rig", "Test Rig is offline", "x"))
        self.assertIn("webhook", calls)

    def test_bad_requests(self):
        self.assertEqual(self.hub.handle("POST", "/api/nk/maintenance", "", b'{"device":"Test Rig","minutes":"soon"}', False)[0], 400)
        self.assertEqual(self.hub.handle("POST", "/api/nk/maintenance", "", b'{"minutes":5}', False)[0], 404)


class UnitTests(unittest.TestCase):
    def test_settings_defaults_and_limits(self):
        s = nk_grace.normalize_settings(None)
        self.assertTrue(s["grace_enabled"])
        self.assertEqual(s["grace_min"], 10.0)
        self.assertTrue(s["boot_settle_enabled"])
        self.assertEqual(nk_grace.normalize_settings({"grace_min": 9999})["grace_min"], nk_grace.LIMITS["grace_min"][1])
        app = miner_hearth.normalize_app_settings({})
        self.assertEqual(app["alerts"]["maintenance"]["grace_min"], 10.0)

    def test_signature_ignores_active_pool_and_change_has_no_users(self):
        a = nk_grace.pool_signature(axe(100))
        self.assertEqual(a, nk_grace.pool_signature(axe(100, fallback=True)))
        b = nk_grace.pool_signature(axe(100, pool=POOL_B))
        ch = nk_grace.describe_change(a, b)
        self.assertTrue(ch["primary_changed"])
        self.assertNotIn(WALLET, json.dumps(ch))

    def test_cgminer_pool_list_change(self):
        def rd(url):
            return autotune.Reading(ok=True, pools=[{"pool": 0, "url": url, "user": "u", "status": "Alive", "priority": 0, "stratum_active": True},
                                                    {"pool": 1, "url": FALLBACK[0], "user": "u", "status": "Alive", "priority": 1}])
        g = nk_grace.MaintenanceGrace(None, clock=lambda: T0)
        s = nk_grace.normalize_settings({})
        g.observe("Box", rd("stratum+tcp://pool-a.example.org:3333"), s, T0)
        res = g.observe("Box", rd("stratum+tcp://pool-b.example.net:4444"), s, T0 + 15)
        self.assertTrue(res["opened"])

    def test_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "maintenance.json"
            g = nk_grace.MaintenanceGrace(p, clock=lambda: T0)
            g.pause("Box", 30, "planned", {}, T0)
            g2 = nk_grace.MaintenanceGrace(p, clock=lambda: T0 + 60)
            self.assertTrue(g2.card_info("Box")["manual"])

    def test_shipped_version_and_ui(self):
        root = Path(__file__).resolve().parent
        self.assertEqual((root / "VERSION").read_text().strip(), "1.17.1")
        import dashboard
        self.assertIn('id="nkSetMaintenance"', dashboard.DASHBOARD_HTML)
        self.assertIn("nk1155Pause", dashboard.DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
