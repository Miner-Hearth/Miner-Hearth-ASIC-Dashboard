#!/usr/bin/env python3
"""Unit, mock-HTTP, mock-CGMiner, and scanner tests for miner-autotune."""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import autotune  # noqa: E402
import scanner  # noqa: E402
from mock_server import BraiinsMockState, CGMinerState, MinerState, MockBraiinsGrpc, MockBraiinsRest, MockCGMiner, MockMiner, NERD_V10371_INFO  # noqa: E402

GAMMA_OPTS = list(range(400, 851, 25))
NERD_OPTS = list(range(525, 851, 25))


def gamma_cfg(**kwargs) -> autotune.DeviceConfig:
    values = dict(
        name="gamma-test",
        ip="127.0.0.1",
        type="gamma",
        min_freq=400,
        max_freq=850,
        step_mhz=25,
        emergency_step_mhz=50,
        asic_target_c=60,
        asic_max_c=65,
        vr_target_c=65,
        vr_max_c=70,
        poll_interval_sec=0.05,
        cooldown_sec=10,
        cool_polls_required=3,
        hysteresis_c=3,
        restart_after_change=False,
        restart_wait_sec=0,
        timeout_sec=2,
        voltage_control=True,
        voltage_start_mv=1210,
        voltage_step_mv=10,
        min_voltage_mv=1150,
        max_voltage_mv=1250,
        error_high_pct=2.0,
        error_low_pct=0.5,
        error_window_polls=5,
        low_error_polls_required=5,
        frequency_options=list(GAMMA_OPTS),
        port=80,
    )
    values.update(kwargs)
    return autotune.DeviceConfig(**values)


def nerd_cfg(**kwargs) -> autotune.DeviceConfig:
    values = dict(
        name="nerd-test",
        ip="127.0.0.1",
        type="nerdoctaxe",
        min_freq=525,
        max_freq=850,
        step_mhz=25,
        emergency_step_mhz=50,
        asic_target_c=60,
        asic_max_c=65,
        vr_target_c=65,
        vr_max_c=70,
        poll_interval_sec=0.05,
        cooldown_sec=10,
        cool_polls_required=3,
        hysteresis_c=3,
        restart_after_change=False,
        restart_wait_sec=0,
        timeout_sec=2,
        voltage_control=False,
        frequency_options=list(NERD_OPTS),
        port=80,
    )
    values.update(kwargs)
    return autotune.DeviceConfig(**values)


def antminer_cfg(**kwargs) -> autotune.DeviceConfig:
    values = dict(
        name="s19-test",
        ip="127.0.0.1",
        type="antminer",
        cgminer_port=4028,
        timeout_sec=2,
        voltage_control=False,
    )
    values.update(kwargs)
    return autotune.DeviceConfig(**values)


def reading(**kwargs) -> autotune.Reading:
    values = dict(
        ok=True,
        asic_temp=55,
        vr_temp=60,
        frequency=525,
        hashrate=1000,
        power=18,
        core_voltage=1210,
        error_percentage=0.2,
    )
    values.update(kwargs)
    return autotune.Reading(**values)


class FrequencyStepTests(unittest.TestCase):
    def test_next_preset_down_and_up(self):
        opts = GAMMA_OPTS
        self.assertEqual(autotune.next_frequency(525, -1, 25, 400, 850, opts), 500)
        self.assertEqual(autotune.next_frequency(525, 1, 25, 400, 850, opts), 550)
        self.assertEqual(autotune.next_frequency(825, 1, 25, 400, 850, opts), 850)

    def test_emergency_skips_at_least_delta(self):
        opts = GAMMA_OPTS
        self.assertEqual(autotune.next_frequency(600, -1, 25, 400, 850, opts, min_delta=50), 550)
        self.assertEqual(autotune.next_frequency(850, -1, 25, 400, 850, opts, min_delta=50), 800)

    def test_never_exceeds_max_or_min(self):
        opts = GAMMA_OPTS
        self.assertEqual(autotune.next_frequency(850, 1, 25, 400, 850, opts), 850)
        self.assertEqual(autotune.next_frequency(400, -1, 25, 400, 850, opts), 400)
        self.assertEqual(autotune.next_frequency(900, 1, 25, 400, 850, None), 850)
        self.assertEqual(autotune.next_frequency(300, -1, 25, 400, 850, None), 400)


class DecisionTests(unittest.TestCase):
    def test_emergency_asic_wins_over_high_error_and_never_raises_voltage(self):
        r = reading(asic_temp=66, vr_temp=60, frequency=700, core_voltage=1210, error_percentage=5.0)
        d = autotune.decide(r, gamma_cfg(), 0, error_pct=5.0)
        self.assertEqual(d.action, "emergency_down")
        self.assertEqual(d.new_freq, 650)
        self.assertIsNone(d.new_voltage)
        self.assertTrue(d.bypass_cooldown)

    def test_temp_above_target_steps_freq_down_and_never_raises_voltage(self):
        r = reading(asic_temp=62, vr_temp=60, frequency=700, core_voltage=1210, error_percentage=4.5)
        d = autotune.decide(r, gamma_cfg(), 3, error_pct=4.5)
        self.assertEqual(d.action, "step_down")
        self.assertEqual(d.new_freq, 675)
        self.assertIsNone(d.new_voltage)
        self.assertFalse(d.bypass_cooldown)
        self.assertIn("will not raise voltage", d.voltage_reason)

    def test_high_error_raises_voltage_10mv_when_cool(self):
        r = reading(asic_temp=55, vr_temp=58, frequency=700, core_voltage=1210)
        d = autotune.decide(r, gamma_cfg(), 0, error_pct=2.8)
        self.assertEqual(d.action, "voltage_up")
        self.assertIsNone(d.new_freq)
        self.assertEqual(d.new_voltage, 1220)

    def test_high_error_at_1250_cap_steps_frequency_down_instead(self):
        r = reading(asic_temp=55, vr_temp=58, frequency=775, core_voltage=1250)
        d = autotune.decide(r, gamma_cfg(), 0, error_pct=3.2)
        self.assertEqual(d.action, "step_down")
        self.assertEqual(d.new_freq, 750)
        self.assertIsNone(d.new_voltage)
        self.assertIn("1250 mV cap", d.reason)

    def test_low_error_for_n_polls_lowers_voltage_for_efficiency(self):
        cfg = gamma_cfg(low_error_polls_required=3)
        # In hysteresis band (58C) or at max_freq so we don't step frequency up.
        r = reading(asic_temp=58, vr_temp=60, frequency=700, core_voltage=1210)
        d1 = autotune.decide(r, cfg, 0, error_pct=0.2, low_error_polls=0)
        self.assertEqual(d1.action, "hold")
        self.assertEqual(d1.low_error_polls, 1)
        d2 = autotune.decide(r, cfg, 0, error_pct=0.2, low_error_polls=1)
        self.assertEqual(d2.action, "hold")
        self.assertEqual(d2.low_error_polls, 2)
        d3 = autotune.decide(r, cfg, 0, error_pct=0.2, low_error_polls=2)
        self.assertEqual(d3.action, "voltage_down")
        self.assertEqual(d3.new_voltage, 1200)
        self.assertEqual(d3.low_error_polls, 0)

    def test_low_error_respects_voltage_floor(self):
        cfg = gamma_cfg(min_voltage_mv=1150, low_error_polls_required=2)
        r = reading(asic_temp=58, vr_temp=60, frequency=700, core_voltage=1150)
        d = autotune.decide(r, cfg, 0, error_pct=0.1, low_error_polls=5)
        self.assertEqual(d.action, "hold")
        self.assertIsNone(d.new_voltage)
        self.assertIn("floor 1150 mV", d.voltage_reason)

    def test_cool_step_up_up_to_850_without_725_ceiling(self):
        cfg = gamma_cfg(cool_polls_required=3)
        r = reading(asic_temp=50, vr_temp=50, frequency=725, core_voltage=1210)
        d = autotune.decide(r, cfg, 2, error_pct=0.4)
        self.assertEqual(d.action, "step_up")
        self.assertEqual(d.new_freq, 750)
        self.assertIsNone(d.new_voltage)

        r_max = reading(asic_temp=50, vr_temp=50, frequency=850, core_voltage=1210)
        d_max = autotune.decide(r_max, cfg, 5, error_pct=1.0)
        self.assertEqual(d_max.action, "hold")
        self.assertIn("at max 850 MHz", d_max.reason)

    def test_step_up_blocked_when_error_above_threshold(self):
        cfg = nerd_cfg(cool_polls_required=1)
        r = reading(asic_temp=50, vr_temp=50, frequency=700, core_voltage=1210)
        d = autotune.decide(r, cfg, 2, error_pct=3.5)
        self.assertEqual(d.action, "hold")
        self.assertIsNone(d.new_freq)
        self.assertIn("blocking step-up", d.reason)

    def test_nerd_hot_chip_via_max_temp(self):
        r = reading(asic_temp=66, vr_temp=50, frequency=700, asic_temps=[50, 51, 66, 52, 53, 50, 49, 51])
        d = autotune.decide(r, nerd_cfg(), 0, error_pct=0.2)
        self.assertEqual(d.action, "emergency_down")
        self.assertEqual(d.new_freq, 650)
        self.assertIsNone(d.new_voltage)


class ErrorMetricExtractionTests(unittest.TestCase):
    def test_error_percentage_preferred_then_shares_then_hashrate_gap(self):
        cfg = gamma_cfg()
        st = autotune.DeviceState(cfg=cfg)
        val, src = autotune.compute_error_sample(reading(error_percentage=1.75), st)
        self.assertAlmostEqual(val, 1.75)
        self.assertEqual(src, "errorPercentage")

        st2 = autotune.DeviceState(cfg=cfg)
        val1, src1 = autotune.compute_error_sample(
            reading(error_percentage=None, shares_accepted=98, shares_rejected=2), st2
        )
        self.assertAlmostEqual(val1, 2.0)
        self.assertEqual(src1, "sharesRejected")
        # Next poll: +90 accepted, +10 rejected => 10% delta
        val2, src2 = autotune.compute_error_sample(
            reading(error_percentage=None, shares_accepted=188, shares_rejected=12), st2
        )
        self.assertAlmostEqual(val2, 10.0)
        self.assertEqual(src2, "sharesRejected")

        st3 = autotune.DeviceState(cfg=cfg)
        val3, src3 = autotune.compute_error_sample(
            reading(error_percentage=None, shares_accepted=None, shares_rejected=None, expected_hashrate=1000, hashrate=950),
            st3,
        )
        self.assertAlmostEqual(val3, 5.0)
        self.assertEqual(src3, "hashrateGap")


class ConfigTests(unittest.TestCase):
    def test_real_config_loads_empty_shipped_devices_and_defaults(self):
        raw = autotune.load_raw_config(ROOT / "config.json")
        cfg = autotune.parse_config(raw, ROOT, config_path=ROOT / "config.json", use_userdata=False)
        self.assertEqual(cfg.devices, [])
        self.assertEqual(autotune.APP_VERSION, "1.17.1")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "1.17.1")
        self.assertEqual(cfg.userdata_file, ROOT / "userdata" / "devices.json")
        self.assertEqual(cfg.defaults["max_freq"], 850)
        self.assertEqual(cfg.defaults["max_voltage_mv"], 1250)
        self.assertEqual(cfg.defaults["voltage_start_mv"], 1210)
        self.assertEqual(cfg.defaults["emergency_voltage_mv"], 1210)
        self.assertEqual(cfg.defaults["min_voltage_mv"], 1150)
        self.assertEqual(cfg.defaults["asic_max_c"], 62.5)
        self.assertEqual(cfg.defaults["vr_max_c"], 65.0)
        self.assertEqual(cfg.defaults["emergency_step_mhz"], 50)
        self.assertEqual(cfg.defaults["error_target_pct"], 0.0)
        self.assertEqual(cfg.defaults["error_high_pct"], 1.0)

    def test_legacy_config_migrates_and_clamps_1250_cap(self):
        raw = {
            "devices": [
                {
                    "name": "gamma-old",
                    "ip": "192.168.1.56",
                    "type": "gamma",
                    "couple_voltage": True,
                    "voltage_anchor_freq": 625,
                    "voltage_anchor_mv": 1210,
                    "max_voltage_mv": 1300,
                },
                {
                    "name": "nerd-old",
                    "ip": "192.168.1.23",
                    "type": "nerdoctaxe",
                    "voltage_control": True,
                },
            ]
        }
        cfg = autotune.parse_config(raw, ROOT, use_userdata=False)
        self.assertTrue(cfg.devices[0].voltage_control)
        self.assertEqual(cfg.devices[0].max_voltage_mv, 1250)
        self.assertEqual(cfg.devices[0].max_freq, 850)
        self.assertFalse(cfg.devices[1].voltage_control)


class TotpTests(unittest.TestCase):
    def test_known_vector(self):
        code = autotune.generate_totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", now=59, digits=6)
        self.assertEqual(code, "287082")


class MockHttpAndTunerTests(unittest.TestCase):
    def setUp(self):
        self.mocks: list[MockMiner] = []
        self.cgmocks: list[MockCGMiner] = []
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name)
        self._loggers = []

    def tearDown(self):
        for mock in self.mocks:
            mock.stop()
        for cg in self.cgmocks:
            cg.stop()
        for logger in self._loggers:
            try:
                logger.close()
            except Exception:
                pass
        self.tmp.cleanup()

    def start(self, state: MinerState) -> MockMiner:
        mock = MockMiner(state, port=0).start()
        self.mocks.append(mock)
        return mock

    def start_cg(self, state: CGMinerState) -> MockCGMiner:
        mock = MockCGMiner(state, port=0).start()
        self.cgmocks.append(mock)
        return mock

    def tuner_for(self, *device_cfgs: autotune.DeviceConfig, dry_run: bool = False, clock=None) -> autotune.Tuner:
        csv_path = self.log_dir / "autotune.csv"
        gcfg = autotune.GlobalConfig(
            devices=list(device_cfgs),
            log_dir=self.log_dir,
            log_file=self.log_dir / "autotune.log",
            csv_file=csv_path,
            dry_run=dry_run,
        )
        logger = autotune.CsvLogger(csv_path)
        self._loggers.append(logger)
        return autotune.Tuner(
            gcfg,
            clock=clock or (lambda: 1_000_000.0),
            sleeper=lambda _s: None,
            csv_logger=logger,
        )

    def test_error_driven_voltage_up_then_cap_then_freq_down(self):
        mock = self.start(
            MinerState(name="g", kind="gamma", frequency=775, temp=54, vr_temp=58, core_voltage=1230, error_percentage=3.0)
        )
        cfg = gamma_cfg(port=mock.port, cooldown_sec=60, error_window_polls=2)
        now = {"t": 100.0}
        tuner = self.tuner_for(cfg, clock=lambda: now["t"])

        # Poll 1: rolling error 3.0% > 2.0%, raise 1230 -> 1240
        d1 = tuner.poll_device(tuner.states[cfg.name])
        self.assertEqual(d1.action, "voltage_up")
        self.assertEqual(mock.state.core_voltage, 1240)
        self.assertEqual(mock.state.frequency, 775)

        # Within cooldown: deferred
        now["t"] = 120.0
        d_wait = tuner.poll_device(tuner.states[cfg.name])
        self.assertEqual(d_wait.action, "hold")
        self.assertIn("cooldown", d_wait.reason)

        # Past cooldown: raise 1240 -> 1250 (cap)
        now["t"] = 170.0
        d2 = tuner.poll_device(tuner.states[cfg.name])
        self.assertEqual(d2.action, "voltage_up")
        self.assertEqual(mock.state.core_voltage, 1250)

        # Past cooldown again at 1250 cap and error still 3.0%: step freq down 775 -> 750
        now["t"] = 240.0
        d3 = tuner.poll_device(tuner.states[cfg.name])
        self.assertEqual(d3.action, "step_down")
        self.assertEqual(mock.state.frequency, 750)
        self.assertEqual(mock.state.core_voltage, 1250)

    def test_temp_above_target_never_raises_voltage_on_high_error(self):
        mock = self.start(
            MinerState(name="g", kind="gamma", frequency=750, temp=62, vr_temp=58, core_voltage=1210, error_percentage=4.0)
        )
        cfg = gamma_cfg(port=mock.port, cooldown_sec=0)
        tuner = self.tuner_for(cfg)
        d = tuner.poll_device(tuner.states[cfg.name])
        self.assertEqual(d.action, "step_down")
        self.assertEqual(mock.state.frequency, 725)
        self.assertEqual(mock.state.core_voltage, 1210)

    def test_efficiency_voltage_step_down_after_low_error_streak(self):
        mock = self.start(
            MinerState(name="g", kind="gamma", frequency=850, temp=52, vr_temp=54, core_voltage=1210, error_percentage=0.1)
        )
        cfg = gamma_cfg(port=mock.port, cooldown_sec=0, low_error_polls_required=3, error_window_polls=2)
        tuner = self.tuner_for(cfg)
        actions = [tuner.poll_device(tuner.states[cfg.name]).action for _ in range(3)]
        self.assertEqual(actions, ["hold", "hold", "voltage_down"])
        self.assertEqual(mock.state.core_voltage, 1200)
        self.assertEqual(mock.state.frequency, 850)

    def test_cool_step_up_above_725_to_850(self):
        mock = self.start(
            MinerState(name="g", kind="gamma", frequency=725, temp=50, vr_temp=50, core_voltage=1210, error_percentage=0.8)
        )
        cfg = gamma_cfg(port=mock.port, cooldown_sec=0, cool_polls_required=2)
        tuner = self.tuner_for(cfg)
        d1 = tuner.poll_device(tuner.states[cfg.name])
        d2 = tuner.poll_device(tuner.states[cfg.name])
        self.assertEqual(d1.action, "hold")
        self.assertEqual(d2.action, "step_up")
        self.assertEqual(mock.state.frequency, 750)
        self.assertEqual(mock.state.core_voltage, 1210)

    def test_octaxe_voltage_never_written_even_with_high_error(self):
        mock = self.start(
            MinerState(
                name="n",
                kind="nerdoctaxe",
                frequency=700,
                temp=66,
                vr_temp=50,
                asic_temps=[50, 51, 66, 52, 53, 50, 49, 51],
                core_voltage=1210,
                error_percentage=5.0,
            )
        )
        cfg = nerd_cfg(port=mock.port)
        tuner = self.tuner_for(cfg)
        decision = tuner.poll_device(tuner.states[cfg.name])
        self.assertEqual(decision.action, "emergency_down")
        self.assertEqual(mock.state.frequency, 650)
        self.assertEqual(mock.state.core_voltage, 1210)
        for patch in mock.state.patches:
            self.assertNotIn("coreVoltage", patch["body"])

    def test_antminer_s19_pro_monitoring_stock_and_braiins(self):
        cg_stock = self.start_cg(CGMinerState(firmware_kind="stock", ghs_5s=110500.0))
        cg_braiins = self.start_cg(CGMinerState(firmware_kind="braiins", ghs_5s=115000.0))

        cfg1 = antminer_cfg(name="s19-stock", cgminer_port=cg_stock.port)
        cfg2 = antminer_cfg(name="s19-braiins", cgminer_port=cg_braiins.port)
        tuner = self.tuner_for(cfg1, cfg2)
        decisions = tuner.poll_all()
        self.assertEqual([d.action for d in decisions], ["monitor", "monitor"])

        r1 = autotune.fetch_antminer_reading(cfg1)
        self.assertTrue(r1.ok)
        self.assertEqual(r1.firmware, "Stock Bitmain")
        self.assertAlmostEqual(r1.hashrate, 110500.0)
        self.assertEqual(r1.board_temps, [54.0, 56.0, 55.0])
        self.assertEqual(r1.chip_temps, [62.0, 65.0, 63.0])
        self.assertEqual(r1.fan_rpms, [6120, 6180, 6090, 6150])
        self.assertEqual(r1.pool_url, "stratum+tcp://pool.example.com:3333")

        r2 = autotune.fetch_antminer_reading(cfg2)
        self.assertEqual(r2.firmware, "Braiins OS")
        self.assertIn("monitor-only", r2.firmware_note)
        self.assertIn("BOS API", r2.firmware_note)

    def test_otp_required_and_session_header(self):
        mock = self.start(
            MinerState(name="n", kind="nerdoctaxe", frequency=700, temp=66, vr_temp=50, require_otp=True, expected_session="abc")
        )
        cfg_bad = nerd_cfg(port=mock.port)
        tuner_bad = self.tuner_for(cfg_bad)
        tuner_bad.poll_device(tuner_bad.states[cfg_bad.name])
        self.assertEqual(mock.state.frequency, 700)

        cfg_ok = nerd_cfg(port=mock.port, otp_session="abc")
        tuner_ok = self.tuner_for(cfg_ok)
        tuner_ok.poll_device(tuner_ok.states[cfg_ok.name])
        self.assertEqual(mock.state.frequency, 650)


class DashboardRenameScanAddTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg_path = self.dir / "config.json"
        self.state_path = self.dir / "autotune-state.json"
        self.mocks: list[MockMiner] = []
        self.cgmocks: list[MockCGMiner] = []
        self.loggers: list[autotune.CsvLogger] = []

    def tearDown(self):
        for m in self.mocks:
            m.stop()
        for c in self.cgmocks:
            c.stop()
        for l in self.loggers:
            l.close()
        self.tmp.cleanup()

    def _make_stack(self, devices: list[dict]):
        from dashboard import SharedState

        raw = {
            "log_dir": str(self.dir / "logs"),
            "state_file": str(self.state_path),
            "dashboard": {"enabled": True, "port": 0, "scan_subnet": "127.0.0.1"},
            "devices": devices,
        }
        self.cfg_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        gcfg = autotune.parse_config(raw, self.dir, config_path=self.cfg_path)
        defaults = {
            d.name: (False if not autotune.is_tunable(d) else (bool(d.enabled_default) if d.enabled_default is not None else True))
            for d in gcfg.devices
        }
        shared = SharedState([d.name for d in gcfg.devices], self.state_path, device_defaults=defaults)
        logger = autotune.CsvLogger(gcfg.csv_file)
        self.loggers.append(logger)
        tuner = autotune.Tuner(gcfg, clock=lambda: 1000.0, sleeper=lambda _s: None, csv_logger=logger, shared=shared)
        shared.attach_tuner(tuner)
        return gcfg, shared, tuner

    def test_rename_persists_to_config_and_state_and_rejects_duplicates(self):
        from dashboard import handle_dashboard_request

        m1 = MockMiner(MinerState(name="g1", kind="gamma", frequency=600, temp=52, vr_temp=54), port=0).start()
        m2 = MockMiner(MinerState(name="g2", kind="gamma", frequency=600, temp=52, vr_temp=54), port=0).start()
        self.mocks.extend([m1, m2])
        _, shared, tuner = self._make_stack([
            {"name": "gamma-56", "ip": "127.0.0.1", "port": m1.port, "type": "gamma"},
            {"name": "gamma-86", "ip": "127.0.0.2", "port": m2.port, "type": "gamma"},
        ])
        shared.set_enabled("gamma-56", False)

        # Duplicate rejected
        st_dup, _, _ = handle_dashboard_request(
            "POST", "/api/device/gamma-56/rename", json.dumps({"name": "gamma-86"}).encode(), shared
        )
        self.assertEqual(st_dup, 409)

        # Valid rename
        st, body, _ = handle_dashboard_request(
            "POST", "/api/device/gamma-56/rename", json.dumps({"name": "Desk Gamma"}).encode(), shared
        )
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body.decode())["name"], "Desk Gamma")
        self.assertIn("Desk Gamma", tuner.states)
        self.assertNotIn("gamma-56", tuner.states)
        self.assertFalse(shared.is_enabled("Desk Gamma"))

        saved_ud = json.loads(tuner.config.userdata_file.read_text(encoding="utf-8"))
        self.assertEqual([d["name"] for d in saved_ud["devices"]], ["Desk Gamma", "gamma-86"])
        self.assertTrue(tuner.config.userdata_file.with_name("devices.json.bak").exists())
        shipped_cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual([d["name"] for d in shipped_cfg["devices"]], ["gamma-56", "gamma-86"])
        saved_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIn("Desk Gamma", saved_state["devices"])
        self.assertFalse(saved_state["devices"]["Desk Gamma"]["enabled"])

    def test_scan_detects_gamma_nerd_antminer_and_add_defaults_autotune_off(self):
        from dashboard import handle_dashboard_request

        g = MockMiner(MinerState(name="g", kind="gamma", model="Gamma 601"), port=0).start()
        n = MockMiner(MinerState(name="n", kind="nerdoctaxe", model="NerdOCTAXE-γ"), port=0).start()
        c = MockCGMiner(CGMinerState(firmware_kind="stock"), port=0).start()
        self.mocks.extend([g, n])
        self.cgmocks.append(c)

        res = scanner.scan_network(
            subnet=f"127.0.0.1:{g.port},127.0.0.1:{n.port},127.0.0.1:{c.port}",
            configured_ips=set(),
            existing_names=set(),
            tcp_timeout=0.5,
            http_timeout=1.5,
        )
        types = {item["type"] for item in res["found"]}
        self.assertEqual(types, {"gamma", "nerdoctaxe", "antminer"})

        # Now test /api/scan and /api/devices/add through dashboard handler -> newly added MUST default OFF.
        _, shared, tuner = self._make_stack([
            {"name": "existing-gamma", "ip": "192.168.1.56", "type": "gamma"},
        ])
        st_scan, body_scan, _ = handle_dashboard_request(
            "POST",
            "/api/scan",
            json.dumps({"subnet": f"127.0.0.1:{g.port}"}).encode(),
            shared,
        )
        self.assertEqual(st_scan, 200)
        scan_data = json.loads(body_scan.decode())
        self.assertEqual(len(scan_data["found"]), 1)
        self.assertEqual(scan_data["found"][0]["type"], "gamma")

        st_add, body_add, _ = handle_dashboard_request(
            "POST",
            "/api/devices/add",
            json.dumps({"name": "New Gamma", "ip": "127.0.0.1", "port": g.port, "type": "gamma"}).encode(),
            shared,
        )
        self.assertEqual(st_add, 201)
        added = json.loads(body_add.decode())
        self.assertFalse(added["enabled"])
        self.assertFalse(shared.is_enabled("New Gamma"))

        saved_ud = json.loads(tuner.config.userdata_file.read_text(encoding="utf-8"))
        new_entry = [d for d in saved_ud["devices"] if d["name"] == "New Gamma"][0]
        self.assertFalse(new_entry["enabled"])
        shipped_cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(len(shipped_cfg["devices"]), 1)
        saved_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertFalse(saved_state["devices"]["New Gamma"]["enabled"])

    def test_antminer_start_autotune_refused(self):
        from dashboard import handle_dashboard_request

        c = MockCGMiner(CGMinerState(), port=0).start()
        self.cgmocks.append(c)
        _, shared, _ = self._make_stack([
            {"name": "antminer-s19", "ip": "127.0.0.1", "cgminer_port": c.port, "type": "antminer"},
        ])
        self.assertFalse(shared.is_enabled("antminer-s19"))
        st, body, _ = handle_dashboard_request("POST", "/api/device/antminer-s19/start", b"{}", shared)
        self.assertEqual(st, 400)
        self.assertIn("monitor-only", json.loads(body.decode())["error"])
        self.assertFalse(shared.is_enabled("antminer-s19"))

    def test_cli_once_skips_dashboard_port(self):
        mock = MockMiner(MinerState(name="g", kind="gamma", frequency=525, temp=50, vr_temp=50), port=0).start()
        self.mocks.append(mock)
        self.cfg_path.write_text(
            json.dumps(
                {
                    "log_dir": str(self.dir / "run"),
                    "state_file": str(self.state_path),
                    "dashboard": {"enabled": True, "port": 1},
                    "devices": [
                        {"name": "gamma-cli", "ip": "127.0.0.1", "port": mock.port, "type": "gamma"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        rc = autotune.main(["--config", str(self.cfg_path), "--dry-run", "--once", "--log-dir", str(self.dir / "run")])
        self.assertEqual(rc, 0)



# Real GET /api/system/info captured from the user's NerdOCTAxe
# (firmware v1.0.37.1 at 192.168.1.23). asicTemps are all zero; hostname is "Axe1".
REAL_NERD_V1_INFO = {
    "asicCount": 8,
    "smallCoreCount": 2040,
    "deviceModel": "NerdOCTAXE-\u03b3",
    "hostip": "192.168.1.23",
    "power": 256,
    "maxPower": 300,
    "temp": 46,
    "vrTemp": 40.4375,
    "vrTempInt": 48.625,
    "hashRate": 13817.7,
    "hashRate_1m": 13875.66,
    "hashRate_10m": 13859,
    "hashRate_1h": 13833.09,
    "coreVoltage": 1210,
    "defaultCoreVoltage": 1210,
    "coreVoltageActual": 1208,
    "fanspeed": 100,
    "fanrpm": 2723,
    "fanrpm2": 2717,
    "duplicateHWNonces": 0,
    "sharesAccepted": 2346,
    "sharesRejected": 1,
    "asicTemps": [0, 0, 0, 0, 0, 0, 0, 0],
    "hostname": "Axe1",
    "frequency": 850,
    "defaultFrequency": 700,
    "overheat_temp": 75,
    "otp": False,
    "ASICModel": "BM1370",
    "uptimeSeconds": 8446,
    "version": "v1.0.37.1",
    "stratum": {"pools": [{"connected": True, "accepted": 2346, "rejected": 1}]},
}


class RealNerdOctaxeV10371Tests(unittest.TestCase):
    def test_parse_ignores_zero_chip_temps_and_uses_hotter_vr(self):
        reading = autotune.parse_nerd_v1_info(REAL_NERD_V1_INFO)
        self.assertTrue(reading.ok)
        self.assertTrue(reading.reachable)
        self.assertEqual(reading.asic_temp, 46)
        self.assertEqual(reading.asic_temps, [])
        self.assertEqual(reading.vr_temp, 48.625)
        self.assertEqual(reading.frequency, 850)
        self.assertEqual(reading.core_voltage, 1210)
        self.assertEqual(reading.core_voltage_actual, 1208)
        self.assertAlmostEqual(reading.hashrate, 13817.7)
        self.assertEqual(reading.shares_accepted, 2346)
        self.assertEqual(reading.shares_rejected, 1)
        self.assertIsNone(reading.error_percentage)
        self.assertEqual(reading.model, "NerdOCTAXE-\u03b3")
        self.assertEqual(reading.version, "v1.0.37.1")
        self.assertEqual(reading.fan_rpms, [2723, 2717])
        self.assertIn("\u03b3", reading.model)

    def test_classify_despite_hostname_axe1_and_unicode_gamma(self):
        self.assertEqual(autotune.classify_axeos_payload(REAL_NERD_V1_INFO), "nerdoctaxe")
        stripped = dict(REAL_NERD_V1_INFO)
        stripped["deviceModel"] = "Unknown Board"
        stripped["hostname"] = "Axe1"
        stripped["version"] = "v1.0.37.1"
        self.assertEqual(autotune.classify_axeos_payload(stripped), "nerdoctaxe")
        generic = {
            "hashRate": 500,
            "ASICModel": "BM1370",
            "asicCount": 1,
            "hostname": "office",
            "temp": 50,
            "vrTemp": 40,
            "frequency": 525,
        }
        self.assertEqual(autotune.classify_axeos_payload(generic), "axeos-generic")

    def test_hold_at_850_not_stepped_down_from_zero_chip_temps(self):
        reading = autotune.parse_nerd_v1_info(REAL_NERD_V1_INFO)
        cfg = nerd_cfg()
        sample, src = autotune.compute_error_sample(reading, autotune.DeviceState(cfg=cfg))
        self.assertEqual(src, "sharesRejected")
        self.assertAlmostEqual(sample, 100.0 * 1 / 2347, places=4)
        d = autotune.decide(reading, cfg, 5, error_pct=sample)
        self.assertEqual(d.action, "hold")
        self.assertIsNone(d.new_freq)
        self.assertIn("at max 850 MHz", d.reason)


class NerdDiscoveryHardeningTests(unittest.TestCase):
    def setUp(self):
        self.mocks = []
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        for m in self.mocks:
            m.stop()
        self.tmp.cleanup()

    def test_fetch_prefers_system_info_when_v2_redirects_to_html(self):
        mock = MockMiner(
            MinerState(
                name="axe1",
                kind="nerdoctaxe",
                v2_enabled=False,
                custom_info=dict(REAL_NERD_V1_INFO),
            ),
            port=0,
        ).start()
        self.mocks.append(mock)
        cfg = nerd_cfg(port=mock.port, ip="127.0.0.1")
        reading = autotune.fetch_reading(cfg)
        self.assertTrue(reading.ok)
        self.assertEqual(reading.asic_temp, 46)
        self.assertEqual(reading.vr_temp, 48.625)
        self.assertEqual(reading.frequency, 850)
        self.assertEqual(reading.model, "NerdOCTAXE-\u03b3")
        self.assertEqual(reading.source, "GET /api/system/info")
        decision = autotune.decide(reading, cfg, 5, error_pct=0.04)
        self.assertEqual(decision.action, "hold")
        self.assertIsNone(decision.new_freq)

    def test_scan_finds_v1_firmware_and_slow_response(self):
        mock = MockMiner(
            MinerState(
                name="axe1",
                kind="nerdoctaxe",
                v2_enabled=False,
                custom_info=dict(REAL_NERD_V1_INFO),
                delay_get_sec=2.5,
            ),
            port=0,
        ).start()
        self.mocks.append(mock)
        res = scanner.scan_network(
            subnet=f"127.0.0.1:{mock.port}",
            configured_ips=set(),
            existing_names=set(),
            tcp_timeout=1.0,
            http_timeout=4.0,
        )
        self.assertEqual(len(res["found"]), 1)
        hit = res["found"][0]
        self.assertEqual(hit["type"], "nerdoctaxe")
        self.assertEqual(hit["model"], "NerdOCTAXE-\u03b3")
        self.assertEqual(hit["frequency_mhz"], 850)
        self.assertEqual(hit["asic_temp_c"], 46)
        self.assertEqual(hit["vr_temp_c"], 48.625)

    def test_configured_device_stays_visible_when_payload_is_unparsed(self):
        from dashboard import SharedState, handle_dashboard_request

        cfg = nerd_cfg()
        state_path = Path(self.tmp.name) / "state.json"
        shared = SharedState([cfg.name], state_path)
        gcfg = autotune.GlobalConfig(
            devices=[cfg],
            log_dir=Path(self.tmp.name),
            log_file=Path(self.tmp.name) / "a.log",
            csv_file=Path(self.tmp.name) / "a.csv",
        )
        tuner = autotune.Tuner(gcfg, clock=lambda: 1.0, sleeper=lambda _s: None, shared=shared)
        orig = autotune.fetch_reading

        def boom(_cfg):
            return autotune.Reading(
                ok=False,
                reachable=True,
                error="online but unparsed /api/system/info HTTP 200: invalid JSON; body: <html>AxeOS",
                source="GET /api/system/info",
            )

        autotune.fetch_reading = boom
        try:
            decision = tuner.poll_device(tuner.states[cfg.name])
        finally:
            autotune.fetch_reading = orig
        self.assertEqual(decision.action, "unparsed")
        snap = shared.status()["devices"][0]
        self.assertTrue(snap["online"])
        self.assertTrue(snap["unparsed"])
        self.assertIn("unparsed", snap["error"].lower())
        st, body, _ = handle_dashboard_request("GET", "/api/status", b"", shared)
        self.assertEqual(st, 200)
        dev = json.loads(body.decode())["devices"][0]
        self.assertTrue(dev["online"])
        self.assertTrue(dev["unparsed"])

    def test_probe_prints_status_headers_timing_and_raw_body(self):
        import io

        mock = MockMiner(
            MinerState(
                name="axe1",
                kind="nerdoctaxe",
                v2_enabled=False,
                custom_info=dict(REAL_NERD_V1_INFO),
            ),
            port=0,
        ).start()
        self.mocks.append(mock)
        buf = io.StringIO()
        rc = autotune.run_probe(f"127.0.0.1:{mock.port}", timeout=4.0, file=buf)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("/api/system/info", out)
        self.assertIn("status: 200", out)
        self.assertIn("elapsed_ms:", out)
        self.assertIn("headers:", out)
        self.assertIn("body:", out)
        self.assertIn("NerdOCTAXE-", out)
        self.assertIn("classified_as: nerdoctaxe", out)
        self.assertIn("v1.0.37.1", out)
        self.assertIn("/api/v2/dashboard", out)
        self.assertIn("status: 302", out)




class V13FeaturesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.mocks = []
        self.cgmocks = []
        self.loggers = []

    def tearDown(self):
        for m in self.mocks:
            m.stop()
        for c in self.cgmocks:
            c.stop()
        for lgr in self.loggers:
            lgr.close()
        self.tmp.cleanup()

    def _make_v13_stack(self, devices, now_fn=None):
        from dashboard import SharedState

        cfg_path = self.dir / "config.json"
        ud_path = self.dir / "userdata" / "devices.json"
        state_path = self.dir / "autotune-state.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "version": "1.5",
                    "log_dir": str(self.dir / "logs"),
                    "state_file": str(state_path),
                    "userdata_file": str(ud_path),
                    "devices": [],
                }
            ),
            encoding="utf-8",
        )
        raw = autotune.load_raw_config(cfg_path)
        gcfg = autotune.parse_config(raw, self.dir, config_path=cfg_path)
        # Seed initial devices via userdata directly for test isolation
        gcfg.devices = [autotune.build_device_config(d, gcfg.defaults) for d in devices]
        autotune.save_userdata_devices(ud_path, gcfg.devices, dry_run=False)
        defaults = {
            d.name: (False if not autotune.is_tunable(d) else (bool(d.enabled_default) if d.enabled_default is not None else True))
            for d in gcfg.devices
        }
        shared = SharedState([d.name for d in gcfg.devices], state_path, device_defaults=defaults)
        logger = autotune.CsvLogger(gcfg.csv_file)
        self.loggers.append(logger)
        tuner = autotune.Tuner(
            gcfg,
            clock=now_fn or (lambda: 1000.0),
            sleeper=lambda _s: None,
            csv_logger=logger,
            shared=shared,
        )
        shared.attach_tuner(tuner)
        return gcfg, shared, tuner

    def test_version_in_header_and_api_status(self):
        from dashboard import handle_dashboard_request

        _, shared, _ = self._make_v13_stack([])
        st_html, html_bytes, _ = handle_dashboard_request("GET", "/", b"", shared)
        self.assertEqual(st_html, 200)
        html = html_bytes.decode("utf-8")
        self.assertIn("v1.17.1", html)

        st_api, api_bytes, _ = handle_dashboard_request("GET", "/api/status", b"", shared)
        self.assertEqual(st_api, 200)
        data = json.loads(api_bytes.decode("utf-8"))
        self.assertEqual(data["version"], "1.17.1")

    def test_migration_from_v12_and_preservation_across_simulated_update(self):
        cfg_path = self.dir / "config.json"
        state_path = self.dir / "autotune-state.json"
        ud_path = self.dir / "userdata" / "devices.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "log_dir": str(self.dir / "logs"),
                    "state_file": str(state_path),
                    "userdata_file": str(ud_path),
                    "devices": [
                        {"name": "My Gamma", "ip": "192.168.1.56", "type": "gamma"},
                        {"name": "My OCTAxe", "ip": "192.168.1.23", "type": "nerdoctaxe"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        state_path.write_text(
            json.dumps(
                {
                    "dry_run": True,
                    "devices": {
                        "My Gamma": {"enabled": False},
                        "My OCTAxe": {"enabled": True},
                    },
                }
            ),
            encoding="utf-8",
        )
        raw_v12 = autotune.load_raw_config(cfg_path)
        cfg1 = autotune.parse_config(raw_v12, self.dir, config_path=cfg_path)
        self.assertTrue(ud_path.exists())
        by_name1 = {d.name: d for d in cfg1.devices}
        self.assertEqual(set(by_name1), {"My Gamma", "My OCTAxe"})
        self.assertFalse(by_name1["My Gamma"].enabled_default)
        self.assertTrue(by_name1["My OCTAxe"].enabled_default)
        self.assertTrue(cfg1.dry_run)

        # Simulate updating to a future version (v1.4) where shipped config.json has devices: []
        cfg_path.write_text(
            json.dumps(
                {
                    "version": "1.5",
                    "log_dir": str(self.dir / "logs"),
                    "state_file": str(state_path),
                    "userdata_file": str(ud_path),
                    "devices": [],
                }
            ),
            encoding="utf-8",
        )
        raw_v14 = autotune.load_raw_config(cfg_path)
        cfg2 = autotune.parse_config(raw_v14, self.dir, config_path=cfg_path)
        self.assertEqual([d.name for d in cfg2.devices], ["My Gamma", "My OCTAxe"])
        self.assertFalse(cfg2.devices[0].enabled_default)
        self.assertTrue(cfg2.devices[1].enabled_default)

        # Mutating userdata creates .bak backup
        autotune.save_userdata_devices(ud_path, cfg2.devices, dry_run=False)
        self.assertTrue(ud_path.with_name("devices.json.bak").exists())

    def test_manual_freq_and_voltage_validation_gamma_octaxe_and_antminer(self):
        from dashboard import handle_dashboard_request

        g_mock = MockMiner(MinerState(name="g", kind="gamma", frequency=525, core_voltage=1210), port=0).start()
        n_mock = MockMiner(MinerState(name="n", kind="nerdoctaxe", frequency=700, core_voltage=1210), port=0).start()
        c_mock = MockCGMiner(CGMinerState(), port=0).start()
        self.mocks.extend([g_mock, n_mock])
        self.cgmocks.append(c_mock)

        gcfg, shared, tuner = self._make_v13_stack(
            [
                {"name": "gamma-1", "ip": "127.0.0.1", "port": g_mock.port, "type": "gamma", "enabled": True},
                {"name": "nerd-1", "ip": "127.0.0.1", "port": n_mock.port, "type": "nerdoctaxe", "enabled": False},
                {"name": "ant-1", "ip": "127.0.0.1", "cgminer_port": c_mock.port, "type": "antminer"},
            ]
        )

        # Antminer manual rejected
        st_ant, _, _ = handle_dashboard_request(
            "POST", "/api/device/ant-1/manual", json.dumps({"frequency_mhz": 600}).encode(), shared
        )
        self.assertEqual(st_ant, 400)

        # Gamma rejected while autotune ON
        st_on, _, _ = handle_dashboard_request(
            "POST", "/api/device/gamma-1/manual", json.dumps({"frequency_mhz": 600, "core_voltage_mv": 1200}).encode(), shared
        )
        self.assertEqual(st_on, 400)

        # Stop gamma autotune, test bounds
        handle_dashboard_request("POST", "/api/device/gamma-1/stop", b"{}", shared)
        st_hi_f, _, _ = handle_dashboard_request(
            "POST", "/api/device/gamma-1/manual", json.dumps({"frequency_mhz": 900}).encode(), shared
        )
        self.assertEqual(st_hi_f, 400)
        st_hi_v, _, _ = handle_dashboard_request(
            "POST", "/api/device/gamma-1/manual", json.dumps({"core_voltage_mv": 1260}).encode(), shared
        )
        self.assertEqual(st_hi_v, 400)
        st_lo_v, _, _ = handle_dashboard_request(
            "POST", "/api/device/gamma-1/manual", json.dumps({"core_voltage_mv": 1000}).encode(), shared
        )
        self.assertEqual(st_lo_v, 400)

        # Valid gamma manual apply
        st_ok, body_ok, _ = handle_dashboard_request(
            "POST", "/api/device/gamma-1/manual", json.dumps({"frequency_mhz": 650, "core_voltage_mv": 1220}).encode(), shared
        )
        self.assertEqual(st_ok, 200)
        self.assertEqual(g_mock.state.snapshot()["frequency"], 650)
        self.assertEqual(g_mock.state.snapshot()["core_voltage"], 1220)
        ud_saved = json.loads(gcfg.userdata_file.read_text(encoding="utf-8"))
        g_saved = [d for d in ud_saved["devices"] if d["name"] == "gamma-1"][0]
        self.assertEqual(g_saved["manual_frequency_mhz"], 650.0)
        self.assertEqual(g_saved["manual_voltage_mv"], 1220)

        # NerdOCTAxe manual voltage allowed, clamped at 1250 mV with warning; autotune still never changes voltage
        st_n, body_n, _ = handle_dashboard_request(
            "POST", "/api/device/nerd-1/manual", json.dumps({"frequency_mhz": 725, "core_voltage_mv": 1310}).encode(), shared
        )
        self.assertEqual(st_n, 200)
        n_res = json.loads(body_n.decode())
        self.assertEqual(n_res["core_voltage_mv"], 1250)
        self.assertIn("1250 mV", n_res["warning"])
        self.assertEqual(n_mock.state.snapshot()["core_voltage"], 1250)

        # Turn OCTAxe autotune ON with high error -> autotune must NEVER write coreVoltage
        n_mock.state.patches.clear()
        n_mock.state.error_percentage = 8.0
        handle_dashboard_request("POST", "/api/device/nerd-1/start", b"{}", shared)
        tuner.poll_device(tuner.states["nerd-1"])
        for patch in n_mock.state.patches:
            self.assertNotIn("coreVoltage", patch["body"])

    def test_resume_autotune_starts_from_manual_settings(self):
        from dashboard import handle_dashboard_request

        now = [1000.0]
        g_mock = MockMiner(
            MinerState(name="g", kind="gamma", frequency=775, temp=50, vr_temp=50, core_voltage=1230, error_percentage=0.8),
            port=0,
        ).start()
        self.mocks.append(g_mock)

        _, shared, tuner = self._make_v13_stack(
            [
                {
                    "name": "gamma-res",
                    "ip": "127.0.0.1",
                    "port": g_mock.port,
                    "type": "gamma",
                    "enabled": True,
                    "cool_polls_required": 2,
                    "cooldown_sec": 60,
                }
            ],
            now_fn=lambda: now[0],
        )
        st = tuner.states["gamma-res"]
        # Poll once while running -> cool_polls becomes 1, last_change_ts simulated at 990
        tuner.poll_device(st)
        st.last_change_ts = 990.0
        self.assertEqual(st.cool_polls, 1)

        # Stop autotune, apply manual 625 MHz / 1180 mV
        handle_dashboard_request("POST", "/api/device/gamma-res/stop", b"{}", shared)
        st_m, _, _ = handle_dashboard_request(
            "POST", "/api/device/gamma-res/manual", json.dumps({"frequency_mhz": 625, "core_voltage_mv": 1180}).encode(), shared
        )
        self.assertEqual(st_m, 200)
        g_mock.state.patches.clear()

        # Resume autotune -> reads 625 / 1180, resets streaks & cooldown, does NOT snap back to 775 / 1230
        st_s, _, _ = handle_dashboard_request("POST", "/api/device/gamma-res/start", b"{}", shared)
        self.assertEqual(st_s, 200)
        self.assertEqual(st.cfg.manual_frequency_mhz, 625.0)
        self.assertEqual(st.cfg.manual_voltage_mv, 1180)
        self.assertEqual(st.cool_polls, 0)
        self.assertEqual(st.last_change_ts, 0.0)

        now[0] += 10.0
        tuner.poll_device(st)
        self.assertEqual(st.cool_polls, 1)
        now[0] += 10.0
        dec = tuner.poll_device(st)
        self.assertEqual(dec.action, "step_up")
        self.assertEqual(dec.new_freq, 650)
        self.assertEqual(g_mock.state.snapshot()["frequency"], 650)
        self.assertEqual(g_mock.state.snapshot()["core_voltage"], 1180)

    def test_pool_patch_payload_and_restart_and_antminer_cgminer(self):
        from dashboard import handle_dashboard_request

        g_mock = MockMiner(MinerState(name="g", kind="gamma"), port=0).start()
        n_mock = MockMiner(MinerState(name="n", kind="nerdoctaxe", v2_enabled=False), port=0).start()
        c_denied = MockCGMiner(CGMinerState(allow_privileged=False), port=0).start()
        c_ok = MockCGMiner(CGMinerState(allow_privileged=True), port=0).start()
        self.mocks.extend([g_mock, n_mock])
        self.cgmocks.extend([c_denied, c_ok])

        _, shared, _ = self._make_v13_stack(
            [
                {"name": "gamma-p", "ip": "127.0.0.1", "port": g_mock.port, "type": "gamma"},
                {"name": "nerd-p", "ip": "127.0.0.1", "port": n_mock.port, "type": "nerdoctaxe"},
                {"name": "ant-denied", "ip": "127.0.0.1", "cgminer_port": c_denied.port, "type": "antminer"},
                {"name": "ant-priv", "ip": "127.0.0.1", "cgminer_port": c_ok.port, "type": "antminer"},
            ]
        )

        # Gamma pool update with blank password -> password key omitted, restart triggered
        st_g, body_g, _ = handle_dashboard_request(
            "POST",
            "/api/device/gamma-p/pool",
            json.dumps(
                {
                    "stratum_url": "solo.ckpool.org",
                    "stratum_port": 3333,
                    "stratum_user": "bc1qnew.gamma",
                    "stratum_password": "",
                    "fallback_url": "backup.ckpool.org",
                    "fallback_port": 4444,
                    "fallback_user": "bc1qnew.fb",
                    "fallback_password": "",
                    "restart": True,
                }
            ).encode(),
            shared,
        )
        self.assertEqual(st_g, 200)
        self.assertTrue(json.loads(body_g.decode())["restarted"])
        self.assertEqual(g_mock.state.restarts, 1)
        patch_body = g_mock.state.patches[-1]["body"]
        self.assertEqual(patch_body["stratumURL"], "solo.ckpool.org")
        self.assertEqual(patch_body["stratumPort"], 3333)
        self.assertEqual(patch_body["stratumUser"], "bc1qnew.gamma")
        self.assertNotIn("stratumPassword", patch_body)
        self.assertEqual(patch_body["fallbackStratumURL"], "backup.ckpool.org")
        self.assertEqual(patch_body["fallbackStratumPort"], 4444)
        self.assertNotIn("fallbackStratumPassword", patch_body)

        # NerdOCTAxe v1.0.37.1 flat PATCH /api/system with password -> sent in PATCH, never returned in /api/status
        st_n, _, _ = handle_dashboard_request(
            "POST",
            "/api/device/nerd-p/pool",
            json.dumps(
                {
                    "stratum_url": "pool.nerd.org",
                    "stratum_port": 3333,
                    "stratum_user": "bc1qnerd.1",
                    "stratum_password": "secret-password",
                    "restart": True,
                }
            ).encode(),
            shared,
        )
        self.assertEqual(st_n, 200)
        self.assertEqual(n_mock.state.patches[-1]["path"], "/api/system")
        self.assertEqual(n_mock.state.patches[-1]["body"]["stratumPassword"], "secret-password")
        self.assertEqual(n_mock.state.restarts, 1)

        _, st_bytes, _ = handle_dashboard_request("GET", "/api/status", b"", shared)
        self.assertNotIn("secret-password", st_bytes.decode("utf-8"))

        # Antminer stock firmware denies privileged CGMiner pool write cleanly
        st_cd, body_cd, _ = handle_dashboard_request(
            "POST",
            "/api/device/ant-denied/pool",
            json.dumps({"stratum_url": "stratum+tcp://btc.pool:3333", "stratum_port": 3333, "stratum_user": "w1"}).encode(),
            shared,
        )
        self.assertEqual(st_cd, 400)
        err_cd = json.loads(body_cd.decode())["error"]
        self.assertIn("Access denied", err_cd)
        self.assertIn("--api-allow", err_cd)

        # Antminer with privileged API allowed succeeds via addpool + switchpool
        st_co, _, _ = handle_dashboard_request(
            "POST",
            "/api/device/ant-priv/pool",
            json.dumps({"stratum_url": "btc.pool.org", "stratum_port": 3333, "stratum_user": "w.new"}).encode(),
            shared,
        )
        self.assertEqual(st_co, 200)
        self.assertIn("addpool", c_ok.state.commands_seen)
        self.assertIn("switchpool", c_ok.state.commands_seen)
        self.assertEqual(c_ok.state.pool_url, "stratum+tcp://btc.pool.org:3333")
        self.assertEqual(c_ok.state.pool_user, "w.new")



class V14FeaturesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.mocks = []
        self.cgmocks = []
        self.loggers = []

    def tearDown(self):
        for m in self.mocks:
            m.stop()
        for c in self.cgmocks:
            c.stop()
        for lgr in self.loggers:
            lgr.close()
        self.tmp.cleanup()

    def _make_stack(self, devices, now_fn=None):
        from dashboard import SharedState

        cfg_path = self.dir / "config.json"
        ud_path = self.dir / "userdata" / "devices.json"
        state_path = self.dir / "autotune-state.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "version": "1.5",
                    "log_dir": str(self.dir / "logs"),
                    "state_file": str(state_path),
                    "userdata_file": str(ud_path),
                    "devices": [],
                }
            ),
            encoding="utf-8",
        )
        raw = autotune.load_raw_config(cfg_path)
        gcfg = autotune.parse_config(raw, self.dir, config_path=cfg_path)
        gcfg.devices = [autotune.build_device_config(d, gcfg.defaults) for d in devices]
        autotune.save_userdata_devices(ud_path, gcfg.devices, dry_run=False)
        defaults = {
            d.name: (False if not autotune.is_tunable(d) else (bool(d.enabled_default) if d.enabled_default is not None else True))
            for d in gcfg.devices
        }
        shared = SharedState([d.name for d in gcfg.devices], state_path, device_defaults=defaults)
        logger = autotune.CsvLogger(gcfg.csv_file)
        self.loggers.append(logger)
        tuner = autotune.Tuner(
            gcfg,
            clock=now_fn or (lambda: 1000.0),
            sleeper=lambda _s: None,
            csv_logger=logger,
            shared=shared,
        )
        shared.attach_tuner(tuner)
        return gcfg, shared, tuner

    def test_braiins_os_plus_full_telemetry_and_no_found_blocks(self):
        c_mock = MockCGMiner(
            CGMinerState(
                firmware_kind="braiins-plus",
                ghs_5s=109500.0,
                ghs_av=108900.0,
                board_temps=[53.0, 55.0, 54.0],
                chip_temps=[67.0, 71.0, 68.0],
                fans=[6010, 6080, 6120, 5990],
                power_w=3120.0,
                power_limit_w=3250.0,
                tuner_mode="PowerTarget",
            ),
            port=0,
        ).start()
        self.cgmocks.append(c_mock)
        cfg = autotune.build_device_config(
            {"name": "S19 Braiins", "ip": "127.0.0.1", "cgminer_port": c_mock.port, "type": "antminer"},
            None,
        )
        reading = autotune.fetch_antminer_reading(cfg)
        self.assertTrue(reading.ok)
        self.assertEqual(reading.firmware, "Braiins OS")
        self.assertIn("Braiins OS+", reading.firmware_note or "")
        self.assertAlmostEqual(reading.hashrate, 109500.0, places=1)
        self.assertIsNotNone(reading.hashrate_1m)
        self.assertIsNotNone(reading.hashrate_5m)
        self.assertIsNotNone(reading.hashrate_15m)
        self.assertIsNotNone(reading.hashrate_24h)
        self.assertIsNotNone(reading.hashrate_avg)
        self.assertEqual(reading.shares_accepted, 45210)
        self.assertEqual(reading.shares_rejected, 112)
        self.assertEqual(reading.shares_stale, 18)
        self.assertEqual(reading.hw_errors, 37)
        self.assertAlmostEqual(reading.pool_rejected_pct, 0.25, places=2)
        self.assertEqual(reading.best_diff, 891234567.0)
        self.assertEqual(reading.power, 3120.0)
        self.assertEqual(reading.power_limit_w, 3250.0)
        self.assertEqual(reading.tuner_mode, "PowerTarget")
        self.assertEqual(len(reading.tuner_chain_statuses), 3)
        self.assertEqual(len(reading.boards), 3)
        # Temperature=0 from devs must be ignored in favor of TEMPS[] chip/board temps
        self.assertEqual(reading.boards[1]["board_temp_c"], 55.0)
        self.assertEqual(reading.boards[1]["chip_temp_c"], 71.0)
        self.assertEqual(reading.asic_temp, 71.0)
        self.assertIsNone(reading.vr_temp)
        self.assertEqual(reading.board_temps, [53.0, 55.0, 54.0])
        self.assertEqual(len(reading.fans), 4)
        self.assertEqual(reading.fans[0]["rpm"], 6010)
        self.assertEqual(reading.fans[0]["speed_pct"], 68.0)
        self.assertEqual(len(reading.pools), 7)
        self.assertTrue(reading.pools[0]["stratum_active"])
        self.assertEqual(reading.last_submitted_diff, 131072.0)
        self.assertEqual(reading.last_share_seconds, 9.0)
        for cmd in ("summary", "devs", "temps", "fans", "tunerstatus", "pools"):
            self.assertIn(cmd, c_mock.state.commands_seen)

    def test_stock_cgminer_fallback_still_works(self):
        c_mock = MockCGMiner(CGMinerState(firmware_kind="stock", ghs_5s=98000.0, ghs_av=97500.0), port=0).start()
        self.cgmocks.append(c_mock)
        cfg = autotune.build_device_config(
            {"name": "S19 Stock", "ip": "127.0.0.1", "cgminer_port": c_mock.port, "type": "antminer"},
            None,
        )
        reading = autotune.fetch_antminer_reading(cfg)
        self.assertTrue(reading.ok)
        self.assertEqual(reading.firmware, "Stock Bitmain")
        self.assertAlmostEqual(reading.hashrate, 98000.0, places=1)
        self.assertEqual(len(reading.fans), 4)
        self.assertEqual(reading.asic_temp, 65.0)

    def test_hexos_v213_full_telemetry_and_graceful_degradation(self):
        g_mock = MockMiner(
            MinerState(
                name="gamma-hex",
                kind="gamma",
                hexos_mode=True,
                hashrate=1160.0,
                expected_hashrate=1100.0,
                power=17.4,
                temp=58.0,
                vr_temp=52.0,
                board_temp=43.0,
                fanspeed=74,
                fanrpm=4400,
                wifi_rssi=-58,
                best_diff="14.2M",
                best_session_diff="2.4M",
                last_submitted_diff=2160,
                last_share_seconds=15,
                stratum_disconnects=2,
                wifi_disconnects=1,
                using_fallback_stratum=True,
                rejected_reasons=[{"message": "stale", "count": 2}],
                domains=[285.0, 291.0, 290.0, 294.0],
                block_height=921450,
            ),
            port=0,
        ).start()
        self.mocks.append(g_mock)
        cfg = autotune.build_device_config(
            {"name": "Gamma Hex", "ip": "127.0.0.1", "port": g_mock.port, "type": "gamma"},
            None,
        )
        r = autotune.fetch_reading(cfg)
        self.assertTrue(r.ok)
        self.assertEqual(r.board_temps, [43.0])
        self.assertNotIn(-1.0, r.asic_temps)  # temp2=-1 ignored
        self.assertAlmostEqual(r.hashrate_1m, 1160.0 * 1.01, places=1)
        self.assertAlmostEqual(r.hashrate_10m, 1160.0 * 0.99, places=1)
        self.assertAlmostEqual(r.hashrate_1h, 1160.0 * 0.98, places=1)
        self.assertEqual(r.fans[0]["speed_pct"], 74.0)
        self.assertEqual(r.wifi_rssi, -58)
        self.assertEqual(r.best_diff_display, "14.2M")
        self.assertEqual(r.best_session_diff_display, "2.4M")
        self.assertEqual(r.last_submitted_diff, 2160.0)
        self.assertEqual(r.last_share_seconds, 15.0)
        self.assertEqual(r.stratum_disconnects, 2)
        self.assertEqual(r.wifi_disconnects, 1)
        self.assertTrue(r.using_fallback_stratum)
        self.assertEqual(r.rejected_reasons, [{"message": "stale", "count": 2}])
        self.assertEqual(len(r.asic_domains), 1)
        self.assertEqual(r.asic_domains[0]["domains"], [285.0, 291.0, 290.0, 294.0])
        self.assertEqual(r.block_height, 921450)

        # Stock minimal AxeOS & NerdOCTAxe v1.0.37.1 degrade gracefully without raising
        r_nerd = autotune.parse_nerd_v1_info(REAL_NERD_V1_INFO)
        self.assertTrue(r_nerd.ok)
        self.assertIsNone(r_nerd.last_submitted_diff)
        self.assertEqual(r_nerd.asic_domains, [])

        r_stock = autotune.parse_gamma_info(
            {
                "hashRate": 500.0,
                "power": 12.0,
                "temp": 55.0,
                "vrTemp": 44.0,
                "frequency": 525,
                "coreVoltage": 1200,
                "sharesAccepted": 100,
                "sharesRejected": 1,
            }
        )
        self.assertTrue(r_stock.ok)
        self.assertIsNone(r_stock.wifi_rssi)
        self.assertFalse(r_stock.using_fallback_stratum)
        self.assertEqual(r_stock.rejected_reasons, [])

    def test_fleet_summary_and_dashboard_html_v14(self):
        from dashboard import handle_dashboard_request

        g_mock = MockMiner(
            MinerState(
                name="gamma-hex",
                kind="gamma",
                hexos_mode=True,
                hashrate=1200.0,
                expected_hashrate=1150.0,
                power=18.0,
                temp=60.0,
                vr_temp=54.0,
                best_diff="25.0M",
                last_submitted_diff=2160,
                last_share_seconds=15,
                using_fallback_stratum=True,
            ),
            port=0,
        ).start()
        n_mock = MockMiner(
            MinerState(
                name="nerd-v1",
                kind="nerdoctaxe",
                v2_enabled=False,
                custom_info=dict(REAL_NERD_V1_INFO),
            ),
            port=0,
        ).start()
        c_mock = MockCGMiner(
            CGMinerState(
                firmware_kind="braiins-plus",
                ghs_5s=108000.0,
                chip_temps=[69.0, 72.0, 70.0],
                board_temps=[55.0, 56.0, 54.0],
                power_w=3100.0,
            ),
            port=0,
        ).start()
        self.mocks.extend([g_mock, n_mock])
        self.cgmocks.append(c_mock)

        _, shared, tuner = self._make_stack(
            [
                {"name": "Gamma", "ip": "127.0.0.1", "port": g_mock.port, "type": "gamma"},
                {"name": "NerdOCTAxe", "ip": "127.0.0.1", "port": n_mock.port, "type": "nerdoctaxe"},
                {"name": "S19 Pro", "ip": "127.0.0.1", "cgminer_port": c_mock.port, "type": "antminer"},
            ]
        )
        for name in ("Gamma", "NerdOCTAxe", "S19 Pro"):
            tuner.poll_device(tuner.states[name])

        st, body, _ = handle_dashboard_request("GET", "/api/status", b"", shared)
        self.assertEqual(st, 200)
        payload = json.loads(body.decode("utf-8"))
        fleet = payload["fleet"]
        self.assertEqual(fleet["online_count"], 3)
        self.assertEqual(fleet["total_count"], 3)
        self.assertGreater(fleet["total_hashrate_ghs"], 115000.0)
        self.assertGreater(fleet["total_power_w"], 3200.0)
        self.assertGreater(fleet["fleet_j_per_th"], 20.0)
        self.assertEqual(fleet["hottest_chip_c"], 72.0)
        self.assertEqual(fleet["hottest_chip_device"], "S19 Pro")
        self.assertEqual(fleet["hottest_vr_c"], 54.0)
        self.assertEqual(fleet["hottest_vr_device"], "Gamma")
        self.assertEqual(fleet["best_share_device"], "S19 Pro")
        self.assertEqual(fleet["best_share"], 891234567.0)

        st_html, html_bytes, _ = handle_dashboard_request("GET", "/", b"", shared)
        self.assertEqual(st_html, 200)
        html = html_bytes.decode("utf-8")
        self.assertIn("fleetSummary", html)
        self.assertIn("Last share", html)
        self.assertIn("FALLBACK", html)
        self.assertIn("<svg", html)
        self.assertNotIn("Found Blocks", html)

    def test_history_store_24h_bounded_and_survives_rename_and_update(self):
        now = [1_000_000.0]
        g_mock = MockMiner(MinerState(name="g", kind="gamma", hashrate=1100.0, temp=56.0), port=0).start()
        self.mocks.append(g_mock)
        gcfg, shared, tuner = self._make_stack(
            [{"name": "Gamma Hist", "ip": "127.0.0.1", "port": g_mock.port, "type": "gamma"}],
            now_fn=lambda: now[0],
        )
        tuner.poll_device(tuner.states["Gamma Hist"])
        now[0] += 120.0
        tuner.poll_device(tuner.states["Gamma Hist"])
        now[0] += 90000.0  # > 24h later prunes the first two points
        tuner.poll_device(tuner.states["Gamma Hist"])

        hist_dir = self.dir / "userdata" / "history"
        self.assertTrue(hist_dir.exists())
        pts = tuner.history.get_series("Gamma Hist")
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0][0], now[0])

        # Rename migrates history file and preserves points
        ok, err = tuner.rename_device("Gamma Hist", "Gamma Renamed")
        self.assertTrue(ok, err)
        self.assertEqual(len(tuner.history.get_series("Gamma Renamed")), 1)
        self.assertEqual(tuner.history.get_series("Gamma Hist"), [])

        # Re-instantiating Tuner (simulated update) loads existing history from userdata/history
        tuner2 = autotune.Tuner(gcfg, clock=lambda: now[0] + 120.0, sleeper=lambda _s: None)
        self.assertEqual(len(tuner2.history.get_series("Gamma Renamed")), 1)


class V15FeaturesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.mocks = []
        self.loggers = []

    def tearDown(self):
        for m in self.mocks:
            m.stop()
        for lgr in self.loggers:
            lgr.close()
        self.tmp.cleanup()

    def _make_stack(self, devices, now_fn=None):
        from dashboard import SharedState

        cfg_path = self.dir / "config.json"
        ud_path = self.dir / "userdata" / "devices.json"
        state_path = self.dir / "autotune-state.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "version": "1.5",
                    "log_dir": str(self.dir / "logs"),
                    "state_file": str(state_path),
                    "userdata_file": str(ud_path),
                    "devices": [],
                }
            ),
            encoding="utf-8",
        )
        raw = autotune.load_raw_config(cfg_path)
        gcfg = autotune.parse_config(raw, self.dir, config_path=cfg_path)
        gcfg.devices = [autotune.build_device_config(d, gcfg.defaults) for d in devices]
        autotune.save_userdata_devices(ud_path, gcfg.devices, dry_run=False)
        defaults = {
            d.name: (False if not autotune.is_tunable(d) else (bool(d.enabled_default) if d.enabled_default is not None else True))
            for d in gcfg.devices
        }
        shared = SharedState([d.name for d in gcfg.devices], state_path, device_defaults=defaults)
        logger = autotune.CsvLogger(gcfg.csv_file)
        self.loggers.append(logger)
        tuner = autotune.Tuner(
            gcfg,
            clock=now_fn or (lambda: 1000.0),
            sleeper=lambda _s: None,
            csv_logger=logger,
            shared=shared,
        )
        shared.attach_tuner(tuner)
        return gcfg, shared, tuner

    def test_parse_nerd_v10371_full_payload(self):
        reading = autotune.parse_nerd_v1_info(dict(NERD_V10371_INFO))
        self.assertTrue(reading.ok)
        self.assertEqual(reading.model, "NerdOCTAXE-\u03b3".encode().decode("unicode_escape") if False else "NerdOCTAXE-γ")
        self.assertEqual(reading.version, "v1.0.37.1")
        self.assertEqual(reading.asic_temp, 50.8125)
        self.assertEqual(reading.asic_temps, [])  # all-zero asicTemps ignored
        self.assertEqual(reading.vr_temp, 48.875)  # max(vrTemp 45.375, vrTempInt 48.875)
        self.assertEqual(reading.frequency, 850)
        self.assertEqual(reading.core_voltage, 1210)
        self.assertEqual(reading.core_voltage_actual, 1208)
        self.assertAlmostEqual(reading.hashrate, 12471.3)
        self.assertAlmostEqual(reading.hashrate_1m, 12559.78)
        self.assertAlmostEqual(reading.hashrate_10m, 12530.2)
        self.assertAlmostEqual(reading.hashrate_1h, 12548.77)
        self.assertAlmostEqual(reading.hashrate_24h, 13015.41)
        self.assertEqual(reading.power, 259)
        self.assertEqual(reading.power_limit_w, 300)
        self.assertAlmostEqual(reading.input_current, 21.5625)
        self.assertEqual(reading.shares_accepted, 22864)
        self.assertEqual(reading.shares_rejected, 1)
        self.assertEqual(reading.pool_difficulty, 12173.0)
        self.assertEqual(reading.best_diff, 20922595573744.0)
        self.assertEqual(reading.best_session_diff, 1198416595.0)
        self.assertIsNone(reading.last_submitted_diff)
        self.assertIsNone(reading.last_share_seconds)
        self.assertFalse(reading.using_fallback_stratum)
        self.assertEqual(reading.wifi_rssi, -74)
        self.assertAlmostEqual(reading.ping_rtt_ms, 39.71428571)
        self.assertAlmostEqual(reading.ping_loss, 0.00952381)
        self.assertEqual(len(reading.fans), 2)
        self.assertEqual(reading.fans[0]["rpm"], 2730)
        self.assertEqual(reading.fans[0]["speed_pct"], 100)
        self.assertEqual(reading.fans[1]["rpm"], 2732)
        self.assertEqual(reading.fans[1]["speed_pct"], 100)
        self.assertEqual(reading.found_blocks, 0)  # v1.15.5: firmware found-block counter is read (extra block trigger)
        # usingFallback true is honoured when present
        payload = dict(NERD_V10371_INFO)
        payload["stratum"] = dict(payload["stratum"])
        payload["stratum"]["usingFallback"] = True
        self.assertTrue(autotune.parse_nerd_v1_info(payload).using_fallback_stratum)

    def test_derived_last_share_waiting_increase_persist_reboot_and_rename(self):
        store_path = self.dir / "userdata" / "last_share.json"
        store = autotune.LastShareStore(store_path)
        base = dict(NERD_V10371_INFO)
        r1 = autotune.parse_nerd_v1_info(base)
        autotune.derive_last_share(r1, "Axe1", 1000.0, store)
        self.assertTrue(r1.last_share_waiting)
        self.assertFalse(r1.last_share_estimated)
        self.assertIsNone(r1.last_submitted_diff)
        self.assertIsNone(r1.last_share_seconds)

        r2 = autotune.parse_nerd_v1_info(base)
        autotune.derive_last_share(r2, "Axe1", 1015.0, store)
        self.assertTrue(r2.last_share_waiting)

        increased = dict(base)
        increased["sharesAccepted"] = 22865
        r3 = autotune.parse_nerd_v1_info(increased)
        autotune.derive_last_share(r3, "Axe1", 1030.0, store)
        self.assertFalse(r3.last_share_waiting)
        self.assertTrue(r3.last_share_estimated)
        self.assertEqual(r3.last_submitted_diff, 12173.0)
        self.assertEqual(r3.last_share_seconds, 0.0)

        r4 = autotune.parse_nerd_v1_info(increased)
        autotune.derive_last_share(r4, "Axe1", 1045.0, store)
        self.assertEqual(r4.last_share_seconds, 15.0)
        self.assertTrue(r4.last_share_estimated)
        self.assertEqual(r4.last_submitted_diff, 12173.0)

        # Survives restart by reloading userdata/last_share.json
        store2 = autotune.LastShareStore(store_path)
        r5 = autotune.parse_nerd_v1_info(increased)
        autotune.derive_last_share(r5, "Axe1", 1045.0, store2)
        self.assertEqual(r5.last_share_seconds, 15.0)
        self.assertTrue(r5.last_share_estimated)

        # Rename migrates the tracking key
        store2.rename("Axe1", "Office OCTAxe")
        r6 = autotune.parse_nerd_v1_info(increased)
        autotune.derive_last_share(r6, "Office OCTAxe", 1050.0, store2)
        self.assertEqual(r6.last_share_seconds, 20.0)
        self.assertIsNone(store2.get("Axe1"))

        # Miner reboot / counter decrease resets cleanly
        reboot = dict(base)
        reboot["sharesAccepted"] = 12
        r7 = autotune.parse_nerd_v1_info(reboot)
        autotune.derive_last_share(r7, "Office OCTAxe", 2000.0, store2)
        self.assertTrue(r7.last_share_waiting)
        self.assertFalse(r7.last_share_estimated)
        self.assertIsNone(r7.last_submitted_diff)
        self.assertIsNone(r7.last_share_seconds)

    def test_firmware_last_share_fields_are_preferred(self):
        store = autotune.LastShareStore(self.dir / "userdata" / "last_share.json")
        reading = autotune.parse_gamma_info(
            {
                "hashRate": 1100.0,
                "power": 18.0,
                "temp": 55.0,
                "vrTemp": 44.0,
                "frequency": 525,
                "coreVoltage": 1200,
                "sharesAccepted": 50,
                "sharesRejected": 0,
                "poolDifficulty": 1000,
                "lastSubmittedDiff": 2160,
                "lastShareSeconds": 15,
            }
        )
        autotune.derive_last_share(reading, "Gamma", 1000.0, store)
        self.assertEqual(reading.last_submitted_diff, 2160.0)
        self.assertEqual(reading.last_share_seconds, 15.0)
        self.assertFalse(reading.last_share_estimated)
        self.assertFalse(reading.last_share_waiting)

    def test_live_card_and_dashboard_html_v15(self):
        from dashboard import handle_dashboard_request

        info = dict(NERD_V10371_INFO)
        mock = MockMiner(
            MinerState(
                name="axe1",
                kind="nerdoctaxe",
                v2_enabled=False,
                custom_info=info,
                frequency=850,
                core_voltage=1210,
            ),
            port=0,
        ).start()
        self.mocks.append(mock)
        now = [1_000_000.0]
        _, shared, tuner = self._make_stack(
            [{"name": "Axe1", "ip": "127.0.0.1", "port": mock.port, "type": "nerdoctaxe", "enabled": False}],
            now_fn=lambda: now[0],
        )
        tuner.poll_device(tuner.states["Axe1"])
        st, body, _ = handle_dashboard_request("GET", "/api/status", b"", shared)
        self.assertEqual(st, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["version"], "1.17.1")
        dev = payload["devices"][0]
        self.assertTrue(dev["last_share_waiting"])
        self.assertAlmostEqual(dev["ping_rtt_ms"], 39.71428571)
        self.assertAlmostEqual(dev["ping_loss"], 0.00952381)
        self.assertAlmostEqual(dev["hashrate_24h_ghs"], 13015.41)
        self.assertEqual(len(dev["fans"]), 2)
        self.assertEqual(dev["best_diff"], 20922595573744.0)
        self.assertFalse(dev["using_fallback_stratum"])
        self.assertNotIn("found_blocks", dev)
        self.assertNotIn("foundBlocks", json.dumps({k: v for k, v in dev.items() if k != "raw_fields"}))
        self.assertNotIn("totalFoundBlocks", json.dumps({k: v for k, v in dev.items() if k != "raw_fields"}))

        info["sharesAccepted"] = int(info["sharesAccepted"]) + 3
        now[0] += 20.0
        tuner.poll_device(tuner.states["Axe1"])
        _, body2, _ = handle_dashboard_request("GET", "/api/status", b"", shared)
        dev2 = json.loads(body2.decode("utf-8"))["devices"][0]
        self.assertFalse(dev2["last_share_waiting"])
        self.assertTrue(dev2["last_share_estimated"])
        self.assertEqual(dev2["last_submitted_diff"], 12173.0)
        self.assertEqual(dev2["last_share_seconds"], 0.0)
        self.assertTrue((self.dir / "userdata" / "last_share.json").exists())

        st_html, html_bytes, _ = handle_dashboard_request("GET", "/", b"", shared)
        self.assertEqual(st_html, 200)
        html = html_bytes.decode("utf-8")
        self.assertIn("v1.17.1", html)
        self.assertIn("waiting for share…", html)
        self.assertIn("≥ ", html)
        self.assertIn("ping ", html)
        self.assertIn("1d ", html)
        self.assertNotIn("Found Blocks", html)
        self.assertNotIn("foundBlocks", html)
        self.assertNotIn("totalFoundBlocks", html)

        # Autotune remains frequency-only and NerdOCTAxe defaults to 800 MHz firmware normal max
        cfg = tuner.states["Axe1"].cfg
        self.assertFalse(cfg.voltage_control)
        self.assertEqual(cfg.max_freq, 800)
        self.assertFalse(cfg.overclock_opt_in)
        reading = autotune.parse_nerd_v1_info(info)
        decision = autotune.decide(reading, cfg, 5, error_pct=0.04)
        self.assertEqual(decision.action, "hold")
        self.assertIsNone(decision.new_freq)
        self.assertIsNone(decision.new_voltage)


class V16DesktopAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.mocks = []
        self._old_home = os.environ.pop("MINER_AUTOTUNE_HOME", None)

    def tearDown(self):
        for m in self.mocks:
            m.stop()
        if self._old_home is None:
            os.environ.pop("MINER_AUTOTUNE_HOME", None)
        else:
            os.environ["MINER_AUTOTUNE_HOME"] = self._old_home
        for h in list(logging.getLogger().handlers):
            try:
                h.close()
            except Exception:
                pass
        logging.getLogger().handlers.clear()
        self.tmp.cleanup()

    def test_app_dir_uses_env_then_exe_folder_not_meipass(self):
        import miner_dashboard as md

        os.environ["MINER_AUTOTUNE_HOME"] = str(self.dir)
        self.assertEqual(md.app_dir(), self.dir.resolve())
        self.assertEqual(autotune._resolve_app_dir(), self.dir.resolve())
        os.environ.pop("MINER_AUTOTUNE_HOME")
        exe_folder = self.dir / "Miners"
        exe_folder.mkdir()
        meipass = self.dir / "_MEI12345"
        meipass.mkdir()
        old_exe = sys.executable
        try:
            sys.frozen = True  # type: ignore[attr-defined]
            sys._MEIPASS = str(meipass)  # type: ignore[attr-defined]
            sys.executable = str(exe_folder / "MinerDashboard.exe")
            self.assertEqual(md.app_dir(), exe_folder.resolve())
            self.assertEqual(autotune._resolve_app_dir(), exe_folder.resolve())
            self.assertEqual(md.bundle_dir(), meipass)
            self.assertEqual(autotune._resolve_bundle_dir(), meipass)
        finally:
            sys.executable = old_exe
            del sys.frozen  # type: ignore[attr-defined]
            del sys._MEIPASS  # type: ignore[attr-defined]
        self.assertEqual(md.app_dir(), ROOT)

    def test_version_prefers_bundled_version_over_stale_file(self):
        bundle = self.dir / "bundle"
        home = self.dir / "home"
        bundle.mkdir()
        home.mkdir()
        (bundle / "VERSION").write_text("1.6\n", encoding="utf-8")
        (home / "VERSION").write_text("1.5\n", encoding="utf-8")
        old_b, old_s = autotune.BUNDLE_DIR, autotune.SCRIPT_DIR
        try:
            autotune.BUNDLE_DIR, autotune.SCRIPT_DIR = bundle, home
            self.assertEqual(autotune._load_app_version(), "1.6")
            (bundle / "VERSION").unlink()
            self.assertEqual(autotune._load_app_version(), "1.5")
        finally:
            autotune.BUNDLE_DIR, autotune.SCRIPT_DIR = old_b, old_s

    def test_choose_port_falls_back_when_busy(self):
        import socket
        import miner_dashboard as md

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            busy = s.getsockname()[1]
            self.assertFalse(md.port_is_free("127.0.0.1", busy))
            chosen = md.choose_port("127.0.0.1", busy)
            self.assertNotEqual(chosen, busy)
            self.assertTrue(md.port_is_free("127.0.0.1", chosen))
        free = md.choose_port("127.0.0.1", busy)
        self.assertEqual(free, busy)

    def test_single_instance_lock(self):
        import miner_dashboard as md

        first = md.SingleInstance(self.dir)
        second = md.SingleInstance(self.dir)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.release()
        self.assertTrue(second.acquire())
        second.release()

    def test_ensure_config_copies_default_but_keeps_existing(self):
        import miner_dashboard as md

        home = self.dir / "fresh"
        cfg = md.ensure_config(home)
        self.assertTrue(cfg.exists())
        data = json.loads(cfg.read_text(encoding="utf-8"))
        self.assertEqual(data["devices"], [])
        existing = self.dir / "existing"
        existing.mkdir()
        (existing / "config.json").write_text('{"devices": [], "log_dir": "mylogs"}', encoding="utf-8")
        md.ensure_config(existing)
        self.assertIn("mylogs", (existing / "config.json").read_text(encoding="utf-8"))

    def test_setup_logging_without_console(self):
        old = sys.stdout
        try:
            sys.stdout = None
            autotune.setup_logging(self.dir / "logs" / "a.log")
            autotune.LOG.info("no console line")
        finally:
            sys.stdout = old
            for h in list(logging.getLogger().handlers):
                h.close()
            logging.getLogger().handlers.clear()
        self.assertIn("no console line", (self.dir / "logs" / "a.log").read_text(encoding="utf-8"))

    def test_window_helpers_degrade_off_windows(self):
        import miner_dashboard as md

        if os.name != "nt":
            self.assertFalse(md.webview2_installed())
            self.assertFalse(md.dotnet_framework_ok())
            self.assertFalse(md.focus_existing_window())
            ok, msg = md.set_autostart(True)
            self.assertFalse(ok)
        orig = md._load_webview
        try:
            md._load_webview = lambda: None
            self.assertFalse(md.run_native_window("http://127.0.0.1:1", "x"))
        finally:
            md._load_webview = orig

    def _stage_v15_folder(self, gamma_port: int) -> Path:
        home = self.dir / "miner-autotune"
        (home / "userdata").mkdir(parents=True)
        shutil.copy2(ROOT / "config.json", home / "config.json")
        (home / "userdata" / "devices.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "app_version": "1.5",
                    "dry_run": True,
                    "devices": [{"name": "Desk Gamma", "ip": "127.0.0.1", "port": gamma_port, "type": "gamma", "enabled": False}],
                }
            ),
            encoding="utf-8",
        )
        return home

    def test_app_services_pick_up_v15_userdata_and_stop_cleanly(self):
        import urllib.request
        import miner_dashboard as md

        g = MockMiner(MinerState(name="g", kind="gamma"), port=0).start()
        self.mocks.append(g)
        home = self._stage_v15_folder(g.port)
        svc = md.AppServices(home, config_path=home / "config.json", port=0)
        svc.start()
        try:
            self.assertTrue(svc.url.startswith("http://127.0.0.1:"))
            deadline = time.time() + 10
            dev = None
            while time.time() < deadline:
                with urllib.request.urlopen(svc.url + "/api/status", timeout=3) as r:
                    data = json.loads(r.read().decode("utf-8"))
                if data["devices"] and data["devices"][0].get("online"):
                    dev = data["devices"][0]
                    break
                time.sleep(0.2)
            self.assertEqual(data["version"], "1.17.1")
            self.assertIsNotNone(dev)
            self.assertEqual(dev["name"], "Desk Gamma")
            self.assertTrue(data["dry_run"])
        finally:
            t0 = time.time()
            svc.stop()
            self.assertLess(time.time() - t0, 5.0)
        self.assertFalse(svc.thread.is_alive())
        self.assertTrue((home / "logs" / "autotune.log").exists())
        self.assertTrue((home / "userdata" / "devices.json").exists())

    def test_smoke_test_cli_runs_headless_and_exits_zero(self):
        import subprocess

        g = MockMiner(MinerState(name="g", kind="gamma"), port=0).start()
        self.mocks.append(g)
        home = self._stage_v15_folder(g.port)
        env = dict(os.environ, MINER_AUTOTUNE_HOME=str(home))
        proc = subprocess.run(
            [sys.executable, str(ROOT / "miner_dashboard.py"), "--smoke-test", "2", "--port", "0"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log = (home / "logs" / "autotune.log").read_text(encoding="utf-8")
        self.assertIn("desktop app v1.17.1", log)
        self.assertIn("Dashboard listening", log)
        self.assertIn("Shutting down", log)
        self.assertFalse((home / "logs" / "instance.json").exists())



class V17FeaturesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.mocks: list[MockMiner] = []
        self.cgmocks: list[MockCGMiner] = []
        self.bmocks: list[Any] = []
        self.loggers: list[autotune.CsvLogger] = []

    def tearDown(self):
        for m in self.mocks:
            m.stop()
        for cg in self.cgmocks:
            cg.stop()
        for b in self.bmocks:
            b.stop()
        for logger in self.loggers:
            try:
                logger.close()
            except Exception:
                pass
        self.tmp.cleanup()

    def _make_stack(self, devices, now_fn=None):
        from dashboard import SharedState

        cfg_path = self.dir / "config.json"
        ud_path = self.dir / "userdata" / "devices.json"
        state_path = self.dir / "autotune-state.json"
        shutil.copy2(ROOT / "config.json", cfg_path)
        raw = autotune.load_raw_config(cfg_path)
        raw["log_dir"] = str(self.dir / "logs")
        raw["state_file"] = str(state_path)
        raw["userdata_file"] = str(ud_path)
        cfg_path.write_text(json.dumps(raw), encoding="utf-8")

        gcfg = autotune.parse_config(raw, self.dir, config_path=cfg_path)
        gcfg.devices = [autotune.build_device_config(d, gcfg.defaults) for d in devices]
        for dev in gcfg.devices:
            autotune.apply_global_and_overrides(dev, gcfg.global_tuner)
        gcfg.device_order = [d.name for d in gcfg.devices]
        autotune.save_userdata_devices(
            ud_path,
            gcfg.devices,
            dry_run=False,
            global_tuner=gcfg.global_tuner,
            display_fields=gcfg.display_fields,
            device_order=gcfg.device_order,
        )
        defaults = {
            d.name: (False if not autotune.is_tunable(d) else (bool(d.enabled_default) if d.enabled_default is not None else True))
            for d in gcfg.devices
        }
        shared = SharedState([d.name for d in gcfg.devices], state_path, device_defaults=defaults)
        logger = autotune.CsvLogger(gcfg.csv_file)
        self.loggers.append(logger)
        tuner = autotune.Tuner(
            gcfg,
            clock=now_fn or (lambda: 1000.0),
            sleeper=lambda _s: None,
            csv_logger=logger,
            shared=shared,
        )
        shared.attach_tuner(tuner)
        return cfg_path, ud_path, gcfg, shared, tuner

    def test_card_reorder_persists_in_userdata_and_status(self):
        from dashboard import handle_dashboard_request

        cfg_path, ud_path, _gcfg, shared, _tuner = self._make_stack([
            {"name": "Alpha", "ip": "127.0.0.10", "type": "gamma", "enabled": True},
            {"name": "Beta", "ip": "127.0.0.11", "type": "nerdoctaxe", "enabled": True},
            {"name": "Charlie", "ip": "127.0.0.12", "type": "antminer", "enabled": False},
        ])
        st, body, _ = handle_dashboard_request(
            "POST",
            "/api/devices/reorder",
            json.dumps({"order": ["Charlie", "Alpha", "Beta"]}).encode("utf-8"),
            shared,
        )
        self.assertEqual(st, 200)
        res = json.loads(body.decode("utf-8"))
        self.assertEqual(res["order"], ["Charlie", "Alpha", "Beta"])

        _, st_body, _ = handle_dashboard_request("GET", "/api/status", b"", shared)
        status = json.loads(st_body.decode("utf-8"))
        self.assertEqual([d["name"] for d in status["devices"]], ["Charlie", "Alpha", "Beta"])

        reloaded = autotune.parse_config(autotune.load_raw_config(cfg_path), self.dir, config_path=cfg_path)
        self.assertEqual([d.name for d in reloaded.devices], ["Charlie", "Alpha", "Beta"])
        ud = json.loads(ud_path.read_text(encoding="utf-8"))
        self.assertEqual(ud["device_order"], ["Charlie", "Alpha", "Beta"])

    def test_display_field_toggles_persist_in_settings_and_userdata(self):
        from dashboard import handle_dashboard_request

        cfg_path, _ud_path, _gcfg, shared, _tuner = self._make_stack([
            {"name": "Gamma-1", "ip": "127.0.0.20", "type": "gamma", "enabled": True},
        ])
        st, body, _ = handle_dashboard_request(
            "POST",
            "/api/settings",
            json.dumps({"display_fields": {"sparklines": False, "wifi_system": False, "fleet_summary": False}}).encode("utf-8"),
            shared,
        )
        self.assertEqual(st, 200)
        res = json.loads(body.decode("utf-8"))
        self.assertFalse(res["display_fields"]["sparklines"])
        self.assertFalse(res["display_fields"]["wifi_system"])
        self.assertTrue(res["display_fields"]["hashrate"])

        _, get_body, _ = handle_dashboard_request("GET", "/api/settings", b"", shared)
        got = json.loads(get_body.decode("utf-8"))
        self.assertFalse(got["display_fields"]["fleet_summary"])
        self.assertGreaterEqual(len(got["display_field_defs"]), 18)

        reloaded = autotune.parse_config(autotune.load_raw_config(cfg_path), self.dir, config_path=cfg_path)
        self.assertFalse(reloaded.display_fields["sparklines"])
        self.assertFalse(reloaded.display_fields["wifi_system"])
        self.assertTrue(reloaded.display_fields["hashrate"])

    def test_new_axe_autotune_logic_tolerance_and_emergency_reset(self):
        cfg = autotune.build_device_config({"name": "g17", "ip": "127.0.0.1", "type": "gamma", "enabled": True})
        self.assertEqual(cfg.asic_max_c, 62.5)
        self.assertEqual(cfg.vr_max_c, 65.0)
        self.assertEqual(cfg.error_target_pct, 0.0)
        self.assertEqual(cfg.error_high_pct, 1.0)
        self.assertEqual(cfg.emergency_voltage_mv, 1210)

        # 0.8% error is in 0.5-1.0% acceptable band -> step freq up when cool without raising voltage
        r_acc = reading(asic_temp=50, vr_temp=52, frequency=700, core_voltage=1200, error_percentage=0.8)
        d_up = autotune.decide(r_acc, cfg, cool_polls=2, error_pct=0.8)
        self.assertEqual(d_up.action, "step_up")
        self.assertEqual(d_up.new_freq, 725)
        self.assertIsNone(d_up.new_voltage)

        # At max_freq with 0.8% error (0.5-1.0% acceptable) -> hold 1200 mV
        r_hold = reading(asic_temp=50, vr_temp=52, frequency=850, core_voltage=1200, error_percentage=0.8)
        d_hold = autotune.decide(r_hold, cfg, cool_polls=5, error_pct=0.8, low_error_polls=10)
        self.assertEqual(d_hold.action, "hold")
        self.assertIsNone(d_hold.new_voltage)

        # Error > 1.0% -> raise voltage until back in tolerance
        r_err = reading(asic_temp=54, vr_temp=56, frequency=775, core_voltage=1200, error_percentage=1.25)
        d_vup = autotune.decide(r_err, cfg, cool_polls=0, error_pct=1.25)
        self.assertEqual(d_vup.action, "voltage_up")
        self.assertEqual(d_vup.new_voltage, 1210)

        # Error < 0.5% for N polls -> lower voltage to find lowest stable voltage
        r_low = reading(asic_temp=50, vr_temp=52, frequency=850, core_voltage=1220, error_percentage=0.2)
        d_vdown = autotune.decide(r_low, cfg, cool_polls=5, error_pct=0.2, low_error_polls=4)
        self.assertEqual(d_vdown.action, "voltage_down")
        self.assertEqual(d_vdown.new_voltage, 1210)

        # Emergency ASIC >= 62.5C with cur_v > 1210 -> drops freq 50 MHz AND resets voltage to 1210 mV
        mock = MockMiner(
            MinerState(name="g", kind="gamma", frequency=800, temp=62.5, vr_temp=58.0, core_voltage=1240, error_percentage=0.5),
            port=0,
        ).start()
        self.mocks.append(mock)
        _, _, _, _, tuner = self._make_stack([
            {"name": "G17", "ip": "127.0.0.1", "port": mock.port, "type": "gamma", "enabled": True, "cooldown_sec": 180}
        ])
        dec = tuner.poll_device(tuner.states["G17"])
        self.assertEqual(dec.action, "emergency_down")
        self.assertEqual(dec.new_freq, 750)
        self.assertEqual(dec.new_voltage, 1210)
        self.assertEqual(mock.state.frequency, 750)
        self.assertEqual(mock.state.core_voltage, 1210)

        # Emergency VR >= 65.0C with cur_v < 1210 -> drops freq 50 MHz, leaves voltage unchanged (1180 mV)
        mock2 = MockMiner(
            MinerState(name="g2", kind="gamma", frequency=750, temp=55.0, vr_temp=65.0, core_voltage=1180, error_percentage=0.5),
            port=0,
        ).start()
        self.mocks.append(mock2)
        _, _, _, _, tuner2 = self._make_stack([
            {"name": "G17-low", "ip": "127.0.0.1", "port": mock2.port, "type": "gamma", "enabled": True}
        ])
        dec2 = tuner2.poll_device(tuner2.states["G17-low"])
        self.assertEqual(dec2.action, "emergency_down")
        self.assertEqual(dec2.new_freq, 700)
        self.assertIsNone(dec2.new_voltage)
        self.assertEqual(mock2.state.frequency, 700)
        self.assertEqual(mock2.state.core_voltage, 1180)

    def test_octaxe_holds_1210_and_lowers_voltage_only_when_vr_hot(self):
        mock = MockMiner(
            MinerState(
                name="oct",
                kind="nerdoctaxe",
                frequency=750,
                temp=52.0,
                vr_temp=54.0,
                asic_temps=[50, 51, 52, 51, 50, 52, 51, 50],
                core_voltage=1210,
                error_percentage=2.0,
            ),
            port=0,
        ).start()
        self.mocks.append(mock)
        _, _, _, _, tuner = self._make_stack([
            {"name": "OCT17", "ip": "127.0.0.1", "port": mock.port, "type": "nerdoctaxe", "enabled": True, "cooldown_sec": 0}
        ])
        cfg = tuner.states["OCT17"].cfg
        self.assertFalse(cfg.voltage_control)
        self.assertTrue(cfg.vr_voltage_relief)

        d1 = tuner.poll_device(tuner.states["OCT17"])
        self.assertEqual(d1.action, "hold")
        self.assertEqual(mock.state.core_voltage, 1210)

        mock.state.vr_temp = 65.0
        d2 = tuner.poll_device(tuner.states["OCT17"])
        self.assertEqual(d2.action, "emergency_down")
        self.assertEqual(mock.state.frequency, 700)
        self.assertEqual(mock.state.core_voltage, 1200)

    def test_global_caps_and_per_machine_overrides_live_and_persisted(self):
        from dashboard import handle_dashboard_request

        cfg_path, _, _, shared, tuner = self._make_stack([
            {"name": "G-A", "ip": "127.0.0.31", "type": "gamma", "enabled": True},
            {"name": "G-B", "ip": "127.0.0.32", "type": "gamma", "enabled": True},
        ])
        st, _, _ = handle_dashboard_request(
            "POST",
            "/api/settings",
            json.dumps({"global_tuner": {"max_freq": 800, "asic_max_c": 61.5}}).encode("utf-8"),
            shared,
        )
        self.assertEqual(st, 200)
        self.assertEqual(tuner.states["G-A"].cfg.max_freq, 800)
        self.assertEqual(tuner.states["G-B"].cfg.asic_max_c, 61.5)

        st2, _, _ = handle_dashboard_request(
            "POST",
            "/api/device/G-A/overrides",
            json.dumps({"overrides": {"max_freq": 750, "max_voltage_mv": 1230}}).encode("utf-8"),
            shared,
        )
        self.assertEqual(st2, 200)
        self.assertEqual(tuner.states["G-A"].cfg.max_freq, 750)
        self.assertEqual(tuner.states["G-A"].cfg.max_voltage_mv, 1230)
        self.assertEqual(tuner.states["G-B"].cfg.max_freq, 800)

        reloaded = autotune.parse_config(autotune.load_raw_config(cfg_path), self.dir, config_path=cfg_path)
        by_n = {d.name: d for d in reloaded.devices}
        self.assertEqual(by_n["G-A"].max_freq, 750)
        self.assertEqual(by_n["G-A"].max_voltage_mv, 1230)
        self.assertEqual(by_n["G-B"].max_freq, 800)
        self.assertEqual(by_n["G-B"].asic_max_c, 61.5)

        st3, _, _ = handle_dashboard_request(
            "POST",
            "/api/device/G-A/overrides",
            json.dumps({"reset": True}).encode("utf-8"),
            shared,
        )
        self.assertEqual(st3, 200)
        self.assertEqual(tuner.states["G-A"].cfg.max_freq, 800)
        self.assertEqual(tuner.states["G-A"].cfg.max_voltage_mv, 1250)

    def test_v16_userdata_migration_preserves_devices_history_and_last_share(self):
        ud_dir = self.dir / "userdata"
        hist_dir = ud_dir / "history"
        hist_dir.mkdir(parents=True)
        (hist_dir / "DefaultGamma.json").write_text('[{"ts": 100, "hr": 1200, "asic": 55, "vr": 58}]', encoding="utf-8")
        (ud_dir / "last_share.json").write_text('{"DefaultGamma": {"accepted": 42, "ts": 100}}', encoding="utf-8")
        (ud_dir / "devices.json").write_text(
            json.dumps({
                "schema_version": 1,
                "app_version": "1.6",
                "dry_run": False,
                "devices": [
                    {
                        "name": "DefaultGamma",
                        "ip": "192.168.1.56",
                        "type": "gamma",
                        "min_freq": 400,
                        "max_freq": 850,
                        "asic_target_c": 60,
                        "asic_max_c": 65,
                        "vr_target_c": 65,
                        "vr_max_c": 70,
                        "error_high_pct": 2.0,
                        "error_low_pct": 0.5,
                        "enabled": True,
                    },
                    {
                        "name": "CustomGamma",
                        "ip": "192.168.1.57",
                        "type": "gamma",
                        "min_freq": 400,
                        "max_freq": 775,
                        "asic_target_c": 60,
                        "asic_max_c": 61.0,
                        "vr_target_c": 65,
                        "vr_max_c": 70,
                        "error_high_pct": 2.0,
                        "error_low_pct": 0.5,
                        "enabled": True,
                    },
                ],
            }),
            encoding="utf-8",
        )
        cfg_path = self.dir / "config.json"
        shutil.copy2(ROOT / "config.json", cfg_path)
        raw = autotune.load_raw_config(cfg_path)
        raw["userdata_file"] = str(ud_dir / "devices.json")
        raw["state_file"] = str(self.dir / "autotune-state.json")
        gcfg = autotune.parse_config(raw, self.dir, config_path=cfg_path)
        by_n = {d.name: d for d in gcfg.devices}

        self.assertEqual(by_n["DefaultGamma"].asic_max_c, 62.5)
        self.assertEqual(by_n["DefaultGamma"].vr_max_c, 65.0)
        self.assertEqual(by_n["DefaultGamma"].error_high_pct, 1.0)
        self.assertEqual(by_n["CustomGamma"].max_freq, 775)
        self.assertEqual(by_n["CustomGamma"].asic_max_c, 61.0)
        self.assertTrue((hist_dir / "DefaultGamma.json").exists())
        self.assertTrue((ud_dir / "last_share.json").exists())

    def test_braiins_grpc_and_rest_test_connection_and_power_nudging(self):
        from dashboard import handle_dashboard_request

        cg = MockCGMiner(
            CGMinerState(
                firmware_kind="braiins-plus",
                power_w=2790.0,
                power_limit_w=2800.0,
                tuner_mode="PowerTarget",
                chip_temps=[70.0, 71.0, 69.0],
                board_temps=[55.0, 56.0, 54.0],
            ),
            port=0,
        ).start()
        self.cgmocks.append(cg)

        bos_state = BraiinsMockState(username="root", password="secret", power_target_w=2800, min_w=1800, max_w=3400)
        grpc_mock = MockBraiinsGrpc(bos_state, port=0).start()
        rest_mock = MockBraiinsRest(bos_state, port=0).start()
        self.bmocks.extend([grpc_mock, rest_mock])

        now = {"t": 1000.0}
        _, _, _, shared, tuner = self._make_stack(
            [{
                "name": "S19-Braiins",
                "ip": "127.0.0.1",
                "type": "antminer",
                "cgminer_port": cg.port,
                "port": rest_mock.port,
                "bos_grpc_port": grpc_mock.port,
                "bos_username": "root",
                "bos_password": "secret",
            }],
            now_fn=lambda: now["t"],
        )
        self.assertFalse(tuner.states["S19-Braiins"].cfg.bos_power_mgmt)

        st, body, _ = handle_dashboard_request("POST", "/api/device/S19-Braiins/braiins-test", b"{}", shared)
        self.assertEqual(st, 200)
        res = json.loads(body.decode("utf-8"))
        self.assertTrue(res["connected"])
        self.assertEqual(res["transport"], "grpc")
        self.assertTrue(res["writes_supported"])
        self.assertEqual(res["current_power_target_w"], 2800)
        self.assertEqual(res["constraints_min_w"], 1800)
        self.assertEqual(res["constraints_max_w"], 3400)
        self.assertFalse(any("SetPowerTarget" in c["path"] for c in bos_state.calls))

        st_bad, bad_body, _ = handle_dashboard_request(
            "POST",
            "/api/device/S19-Braiins/braiins-test",
            json.dumps({"bos_password": "wrong"}).encode("utf-8"),
            shared,
        )
        self.assertEqual(st_bad, 200)
        bad_res = json.loads(bad_body.decode("utf-8"))
        self.assertFalse(bad_res["connected"])
        self.assertFalse(bad_res["writes_supported"])

        grpc_mock.stop()
        st_rest, rest_body, _ = handle_dashboard_request("POST", "/api/device/S19-Braiins/braiins-test", b"{}", shared)
        self.assertEqual(st_rest, 200)
        rest_res = json.loads(rest_body.decode("utf-8"))
        self.assertTrue(rest_res["connected"])
        self.assertEqual(rest_res["transport"], "rest")
        self.assertTrue(rest_res["writes_supported"])
        self.assertEqual(rest_res["current_power_target_w"], 2800)

        grpc_mock2 = MockBraiinsGrpc(bos_state, port=0).start()
        self.bmocks.append(grpc_mock2)
        st_cfg, _, _ = handle_dashboard_request(
            "POST",
            "/api/device/S19-Braiins/braiins-power",
            json.dumps({
                "bos_power_mgmt": True,
                "bos_grpc_port": grpc_mock2.port,
                "bos_min_watts": 2200,
                "bos_max_watts": 2900,
                "bos_step_watts": 50,
                "bos_chip_target_c": 75.0,
                "bos_chip_hot_c": 82.0,
                "bos_cooldown_sec": 300,
                "bos_cool_polls_required": 2,
            }).encode("utf-8"),
            shared,
        )
        self.assertEqual(st_cfg, 200)
        self.assertTrue(tuner.states["S19-Braiins"].cfg.bos_power_mgmt)

        cg.state.chip_temps = [80.0, 84.0, 79.0]
        tuner.poll_device(tuner.states["S19-Braiins"])
        self.assertEqual(bos_state.power_target_w, 2750)

        now["t"] = 1060.0
        tuner.poll_device(tuner.states["S19-Braiins"])
        self.assertEqual(bos_state.power_target_w, 2750)

        cg.state.power_limit_w = 2750.0
        cg.state.chip_temps = [68.0, 67.0, 68.0]
        now["t"] = 1350.0
        tuner.poll_device(tuner.states["S19-Braiins"])
        self.assertEqual(bos_state.power_target_w, 2750)
        now["t"] = 1370.0
        tuner.poll_device(tuner.states["S19-Braiins"])
        self.assertEqual(bos_state.power_target_w, 2800)



# ---------------------------------------------------------------------------
# v1.8 Miner Hearth
# ---------------------------------------------------------------------------

import re as _re  # noqa: E402
from datetime import datetime as _dt  # noqa: E402
from urllib.parse import urlparse as _urlparse  # noqa: E402

import nk_math  # noqa: E402
import nk_net  # noqa: E402
import nk_sounds  # noqa: E402
import miner_hearth  # noqa: E402

PAYOUT = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
OTHER = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
NET_DIFF = 1.0e14


class FakeEsplora:
    """In-memory Esplora/mempool API used as the `fetch` function."""

    def __init__(self, tip=900000, mempool_extras=True):
        self.tip = tip
        self.diff = NET_DIFF
        self.blocks = {}
        self.offline = False
        self.calls = []
        self.mempool_extras = mempool_extras

    def add(self, h, ts, addrs):
        self.blocks[h] = (f"{h:064x}", float(ts), list(addrs))

    def _by_hash(self, bh):
        for h, (hh, ts, addrs) in self.blocks.items():
            if hh == bh:
                return h, ts, addrs
        raise nk_net.NetError("unknown block")

    def __call__(self, url, timeout=8.0):
        self.calls.append(url)
        if self.offline:
            raise nk_net.NetError("offline")
        path = _urlparse(url).path
        if path == "/api/blocks/tip/height":
            return str(self.tip).encode()
        if path == "/api/blocks":
            ts = self.blocks.get(self.tip, (None, 1.7e9, []))[1]
            return json.dumps([{"id": f"{self.tip:064x}", "height": self.tip, "timestamp": ts, "difficulty": self.diff}]).encode()
        m = _re.fullmatch(r"/api/block-height/(\d+)", path)
        if m:
            h = int(m.group(1))
            if h not in self.blocks:
                self.add(h, 1.0, ["1BoatSLRHtKNngkdXEeobR76b53LETtpyT"])
            return self.blocks[h][0].encode()
        m = _re.fullmatch(r"/api/block/([0-9a-f]{64})/txid/0", path)
        if m:
            h, _ts, _a = self._by_hash(m.group(1))
            return ("e" + f"{h:063x}").encode()
        m = _re.fullmatch(r"/api/block/([0-9a-f]{64})", path)
        if m:
            h, ts, _a = self._by_hash(m.group(1))
            return json.dumps({"id": m.group(1), "height": h, "timestamp": ts}).encode()
        m = _re.fullmatch(r"/api/tx/e([0-9a-f]{63})", path)
        if m:
            h = int(m.group(1), 16)
            addrs = self.blocks[h][2]
            return json.dumps({"vin": [{"is_coinbase": True}], "vout": [{"scriptpubkey_address": a, "value": 1} for a in addrs]}).encode()
        if self.mempool_extras and path == "/api/v1/prices":
            return json.dumps({"USD": 60000, "EUR": 55000}).encode()
        if self.mempool_extras and path.startswith("/api/v1/mining/reward-stats/"):
            return json.dumps({"startBlock": self.tip - 143, "endBlock": self.tip, "totalFee": 144 * 2_000_000}).encode()
        raise nk_net.NetError(f"404 {path}")


class V18MinerHearthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.mocks = []
        self.loggers = []
        self.hubs = []

    def tearDown(self):
        for h in self.hubs:
            h.stop()
        for m in self.mocks:
            m.stop()
        for lg in self.loggers:
            try:
                lg.close()
            except Exception:
                pass
        self.tmp.cleanup()

    # --- helpers -----------------------------------------------------------
    def _hub(self, fake=None, clock=None):
        hub = miner_hearth.FeatureHub(self.dir / "userdata", fetch=fake or FakeEsplora(), clock=clock or (lambda: 1_700_000_000.0))
        self.hubs.append(hub)
        return hub

    def _stack(self, devices, clock=None, fake=None):
        helper = V17FeaturesTests("test_card_reorder_persists_in_userdata_and_status")
        helper.dir = self.dir
        helper.loggers = self.loggers
        cfg_path, ud_path, gcfg, shared, tuner = helper._make_stack(devices, now_fn=clock)
        hub = miner_hearth.attach_feature_hub(tuner, shared, start=False, fetch=fake or FakeEsplora())
        if clock is not None:
            hub.clock = clock
        self.hubs.append(hub)
        return ud_path, gcfg, shared, tuner, hub

    def _req(self, shared, method, path, body=None, ip=None, headers=None):
        from dashboard import handle_dashboard_request

        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        st, out, ctype = handle_dashboard_request(method, path, raw, shared, ip, headers)
        try:
            return st, json.loads(out.decode("utf-8")), ctype
        except (UnicodeDecodeError, json.JSONDecodeError):
            return st, out, ctype

    # --- block-found confirmation ------------------------------------------
    def _block_hub(self, payout=(PAYOUT,)):
        fake = FakeEsplora(tip=900000)
        hub = self._hub(fake)
        hub.settings.update({"block": {"payout_addresses": list(payout)}})
        hub.network = {"difficulty": NET_DIFF, "height": 900000}
        return fake, hub

    def test_block_trigger_needs_new_share_at_or_above_network_difficulty(self):
        _fake, hub = self._block_hub()
        cfg = gamma_cfg(name="G")
        t = 1_700_000_000.0
        # First sighting is a baseline only, even if huge (could be an old share).
        self.assertEqual(hub.on_poll(cfg, reading(best_session_diff=2e14), t), [])
        self.assertEqual(hub.blocks.candidates, [])
        # Increase but still below network difficulty: no trigger.
        hub.on_poll(cfg, reading(best_session_diff=5e13), t + 10)
        hub.on_poll(cfg, reading(best_session_diff=9e13), t + 20)
        self.assertEqual(hub.blocks.candidates, [])
        # Same value again: no trigger.
        hub.on_poll(cfg, reading(best_session_diff=9e13), t + 30)
        self.assertEqual(hub.blocks.candidates, [])
        # Estimated last-share (pool difficulty guess) is ignored.
        hub.on_poll(cfg, reading(best_session_diff=9e13, last_submitted_diff=5e14, last_share_estimated=True), t + 35)
        self.assertEqual(hub.blocks.candidates, [])
        # New share >= network difficulty: one pending candidate; v1.15.5 sends one "possible" notice at once
        # (info level, no sound), never a BLOCK FOUND until the explorer confirms it.
        evs = hub.on_poll(cfg, reading(best_session_diff=1.2e14), t + 40)
        self.assertEqual([e["kind"] for e in evs], ["block_possible"])
        self.assertEqual(evs[0]["level"], "info")
        self.assertEqual(len(hub.blocks.candidates), 1)
        self.assertEqual(hub.blocks.candidates[0]["status"], "pending")
        self.assertFalse(any(e["level"] == "block" for e in hub.events))
        # Duplicate trigger within 30 min does not create a second candidate.
        hub.on_poll(cfg, reading(best_session_diff=1.5e14), t + 60)
        self.assertEqual(len(hub.blocks.candidates), 1)

    def test_block_confirmed_only_when_coinbase_pays_configured_address(self):
        fake, hub = self._block_hub()
        cfg = gamma_cfg(name="G")
        t = 1_700_000_000.0
        hub.on_poll(cfg, reading(best_session_diff=1e10), t)
        hub.on_poll(cfg, reading(best_session_diff=1.1e14), t + 15)
        fake.tip = 900001
        fake.add(900001, t + 5, [OTHER.upper(), PAYOUT.upper()])  # bech32 compare is case-insensitive
        hub.check_candidates(t + 60)
        c = hub.blocks.candidates[0]
        self.assertEqual(c["status"], "confirmed")
        self.assertEqual(c["block_height"], 900001)
        blk = [e for e in hub.events if e["kind"] == "block_confirmed"]
        self.assertEqual(len(blk), 1)
        self.assertEqual(blk[0]["level"], "block")
        self.assertEqual(blk[0]["sound"], "block")
        self.assertEqual(len(hub.notifier.sent), 1)  # toast requested
        # Persisted so a restart does not re-alert.
        again = miner_hearth.BlockWatcher(self.dir / "userdata" / "block_watch.json")
        self.assertEqual(again.candidates[0]["status"], "confirmed")

    def test_block_false_positive_other_payout_is_rejected_without_alert(self):
        fake, hub = self._block_hub()
        cfg = gamma_cfg(name="G")
        t = 1_700_000_000.0
        hub.on_poll(cfg, reading(best_diff=1e9), t)
        hub.on_poll(cfg, reading(best_diff=3e14), t + 15)
        for i in range(1, 8):
            fake.add(900000 + i, t + 600 * i, [OTHER])
        fake.add(899999, t - 600, [OTHER])
        fake.add(900000, t - 60, [OTHER])
        fake.tip = 900003
        hub.check_candidates(t + 120)
        self.assertEqual(hub.blocks.candidates[0]["status"], "pending")  # not deep enough yet
        fake.tip = 900007
        hub.check_candidates(t + 4000)
        self.assertEqual(hub.blocks.candidates[0]["status"], "rejected")
        self.assertFalse(any(e["level"] == "block" for e in hub.events))
        self.assertEqual(len(hub.notifier.sent), 0)

    def test_block_match_outside_time_window_does_not_confirm(self):
        fake, hub = self._block_hub()
        cfg = gamma_cfg(name="G")
        t = 1_700_000_000.0
        hub.on_poll(cfg, reading(best_session_diff=1e9), t)
        hub.on_poll(cfg, reading(best_session_diff=2e14), t + 15)
        fake.add(900000, t - 10 * 3600, [PAYOUT])  # old block paying the user (e.g. last year) is not ours
        for i in range(1, 7):
            fake.add(900000 + i, t + 600 * i, [OTHER])
        fake.add(899999, t - 11 * 3600, [OTHER])
        fake.tip = 900006
        hub.check_candidates(t + 4000)
        self.assertEqual(hub.blocks.candidates[0]["status"], "rejected")

    def test_block_without_payout_address_is_only_unconfirmed_possible(self):
        _fake, hub = self._block_hub(payout=())
        cfg = gamma_cfg(name="G")
        t = 1_700_000_000.0
        hub.on_poll(cfg, reading(best_session_diff=1e9), t)
        hub.on_poll(cfg, reading(best_session_diff=2e14), t + 15)
        hub.check_candidates(t + 30)
        self.assertEqual(hub.blocks.candidates[0]["status"], "possible")
        ev = [e for e in hub.events if e["kind"] == "block_possible"][0]
        self.assertEqual(ev["level"], "info")
        self.assertIsNone(ev["sound"])
        self.assertEqual("Block Found", ev["title"])  # v1.16: no height known, so no number
        self.assertIn("Machine: G", ev["message"])
        self.assertEqual(len([e for e in hub.events if e["kind"] == "block_possible"]), 1)  # once, not again from the check
        self.assertEqual(len(hub.notifier.sent), 0)

    def test_block_explorer_offline_stays_pending_and_never_alerts(self):
        fake, hub = self._block_hub()
        cfg = gamma_cfg(name="G")
        t = 1_700_000_000.0
        hub.on_poll(cfg, reading(best_session_diff=1e9), t)
        hub.on_poll(cfg, reading(best_session_diff=2e14), t + 15)
        fake.offline = True
        hub.check_candidates(t + 60)
        hub.tick(t + 400, force=True)
        self.assertEqual(hub.blocks.candidates[0]["status"], "pending")
        self.assertIn("waiting for explorer", hub.blocks.candidates[0]["note"])
        self.assertEqual([e["kind"] for e in hub.events], ["block_possible"])  # v1.15.5: the at-trigger notice only
        hub.check_candidates(t + 49 * 3600)  # expires as unverified, still no alert
        self.assertEqual(hub.blocks.candidates[0]["status"], "unverified")
        self.assertFalse(any(e["level"] == "block" for e in hub.events))

    def test_payout_address_validation_and_explorer_fallback_order(self):
        s = miner_hearth.normalize_app_settings({"block": {"payout_addresses": f"{PAYOUT.upper()}, not-an-address ;1BoatSLRHtKNngkdXEeobR76b53LETtpyT", "explorer": "custom", "custom_base_url": "umbrel.local:3006/api/"}})
        self.assertEqual(s["block"]["payout_addresses"], [PAYOUT, "1BoatSLRHtKNngkdXEeobR76b53LETtpyT"])
        self.assertEqual(s["block"]["custom_base_url"], "http://umbrel.local:3006")
        bases = nk_net.explorer_bases("custom", s["block"]["custom_base_url"])
        self.assertEqual([b[0] for b in bases], ["custom", "mempool", "blockstream"])
        self.assertEqual([b[0] for b in nk_net.explorer_bases("custom", "http://x:1", allow_public_fallback=False)], ["custom"])
        self.assertEqual([b[0] for b in nk_net.explorer_bases("mempool")], ["mempool", "blockstream"])

    def test_network_snapshot_falls_back_and_estimates_adjustment(self):
        fake = FakeEsplora(tip=977134, mempool_extras=False)
        fake.add(977134, 1_795_400_000.0, [OTHER])
        fake.add(977134 - (977134 % 2016), 1_795_400_000.0 - (977134 % 2016) * 550.0, [OTHER])
        calls = {"n": 0}

        def flaky(url, timeout=8.0):
            if url.startswith(nk_net.MEMPOOL_BASE):
                calls["n"] += 1
                raise nk_net.NetError("mempool down")
            return fake(url, timeout)

        ex = nk_net.ExplorerClient(nk_net.explorer_bases("mempool"), fetch=flaky)
        snap = ex.network_snapshot(now=1_795_400_000.0)
        self.assertEqual(snap["height"], 977134)
        self.assertEqual(snap["source"], "blockstream")
        adj = snap["adjustment"]
        self.assertEqual(adj["remaining_blocks"], 2016 - (977134 % 2016))
        self.assertAlmostEqual(adj["change_pct"], (600 / 550 - 1) * 100, places=3)
        self.assertGreater(calls["n"], 0)

    # --- alerts --------------------------------------------------------------
    def test_offline_alert_after_threshold_with_cooldown_and_recovery(self):
        hub = self._hub()
        hub.settings.update({"alerts": {"offline": {"minutes": 3}, "cooldown_min": 30}})
        cfg = gamma_cfg(name="G")
        t = 1_700_000_000.0
        self.assertEqual(hub.on_poll(cfg, reading(), t), [])
        self.assertEqual(hub.on_poll(cfg, autotune.Reading(ok=False, error="timeout"), t + 60), [])
        evs = hub.on_poll(cfg, autotune.Reading(ok=False, error="timeout"), t + 60 + 181)
        self.assertEqual([e["kind"] for e in evs], ["offline"])
        self.assertEqual(evs[0]["sound"], "problem")
        self.assertEqual(hub.on_poll(cfg, autotune.Reading(ok=False), t + 400), [])  # still active: no spam
        rec = hub.on_poll(cfg, reading(), t + 500)
        self.assertEqual([e["kind"] for e in rec], ["recovered"])
        self.assertIsNone(rec[0]["sound"])
        # Goes offline again within the cooldown: no second notification.
        hub.on_poll(cfg, autotune.Reading(ok=False), t + 600)
        self.assertEqual(hub.on_poll(cfg, autotune.Reading(ok=False), t + 900), [])
        # After the cooldown it may alert again.
        hub.on_poll(cfg, reading(), t + 1000)
        hub.on_poll(cfg, autotune.Reading(ok=False), t + 3000)
        self.assertEqual([e["kind"] for e in hub.on_poll(cfg, autotune.Reading(ok=False), t + 3300)], ["offline"])

    def test_hashrate_drop_fallback_pool_and_no_share_alerts_and_toggles(self):
        hub = self._hub()
        cfg = gamma_cfg(name="G")
        t = 1_700_000_000.0
        kinds = []
        for i in range(40):  # 40 min of healthy hashrate with shares increasing
            kinds += [e["kind"] for e in hub.on_poll(cfg, reading(hashrate=1000, shares_accepted=100 + i), t + i * 60)]
        self.assertEqual(kinds, [])
        for i in range(40, 47):  # drops to 40% for 7 minutes
            kinds += [e["kind"] for e in hub.on_poll(cfg, reading(hashrate=400, shares_accepted=100 + i), t + i * 60)]
        self.assertEqual(kinds.count("hashrate_drop"), 1)
        kinds = [e["kind"] for e in hub.on_poll(cfg, reading(hashrate=400, shares_accepted=200, using_fallback_stratum=True, fallback_pool_url="backup.pool"), t + 47 * 60)]
        self.assertEqual(kinds, ["fallback_pool"])
        # Shares stop increasing for 31 min -> no_shares
        out = []
        for i in range(48, 81):
            out += [e["kind"] for e in hub.on_poll(cfg, reading(hashrate=1000, shares_accepted=200), t + i * 60)]
        self.assertEqual(out.count("no_shares"), 1)
        # Antminer: active pool with worse priority counts as fallback.
        r = reading(hashrate=1000, pools=[{"priority": 0, "stratum_active": False}, {"priority": 1, "stratum_active": True}])
        self.assertTrue(miner_hearth._is_on_fallback_pool(r))
        # Toggle off: no alert.
        hub2 = miner_hearth.FeatureHub(None, fetch=FakeEsplora())
        hub2.settings.update({"alerts": {"offline": {"enabled": False, "minutes": 1}}})
        hub2.on_poll(cfg, autotune.Reading(ok=False), t)
        self.assertEqual(hub2.on_poll(cfg, autotune.Reading(ok=False), t + 600), [])

    # --- economics & odds ------------------------------------------------------
    def test_cost_profit_and_block_odds_math(self):
        self.assertEqual(nk_math.block_subsidy_sats(0), 50 * 10**8)
        self.assertEqual(nk_math.block_subsidy_sats(840_000), 312_500_000)
        self.assertEqual(nk_math.block_subsidy_sats(1_050_000), 156_250_000)
        lam = nk_math.blocks_per_day(1000.0, NET_DIFF)  # 1 TH/s
        self.assertAlmostEqual(lam, 1e12 * 86400 / (NET_DIFF * 2**32), places=12)
        odds = nk_math.block_odds(1000.0, NET_DIFF)
        self.assertAlmostEqual(odds["chance_day"], 1 - math.exp(-lam), places=12)
        self.assertAlmostEqual(odds["expected_days_to_block"], 1 / lam, places=3)
        self.assertGreater(odds["chance_year"], odds["chance_month"])
        e = nk_math.economics(1000.0, 20.0, NET_DIFF, 900_000, 0.20, 60000.0, avg_fees_sats=0)
        self.assertAlmostEqual(e["kwh_per_day"], 0.48)
        self.assertAlmostEqual(e["cost_per_day"], 0.096)
        self.assertAlmostEqual(e["est_sats_per_day"], lam * 312_500_000)
        rev = e["est_sats_per_day"] / 1e8 * 60000
        self.assertAlmostEqual(e["est_revenue_per_day"], rev)
        self.assertAlmostEqual(e["break_even_rate"], rev / 0.48)
        self.assertAlmostEqual(e["est_profit_per_day"], rev - 0.096)
        self.assertEqual(nk_math.block_odds(None, NET_DIFF)["chance_day"], 0.0)
        self.assertIsNone(nk_math.economics(0, 0, NET_DIFF, 1, 0.1, None)["est_revenue_per_day"])

    def test_status_payload_has_fleet_and_device_economics(self):
        _ud, _g, shared, tuner, hub = self._stack([{"name": "A", "ip": "127.0.0.21", "type": "gamma"}])
        hub.network = {"height": 900_000, "difficulty": NET_DIFF}
        hub.price = {"price": 60000.0, "currency": "USD"}
        shared.update_device("A", {"online": True, "hashrate_ghs": 1000.0, "power_w": 20.0})
        st, data, _ = self._req(shared, "GET", "/api/status")
        self.assertEqual(st, 200)
        nk = data["nk"]
        self.assertEqual(nk["app_name"], "Miner Hearth")
        self.assertEqual(nk["donate_address"], "bc1q3m8usp6mffv4txhwcltq9lu9jltx02eyukgrvf")
        self.assertAlmostEqual(nk["devices"]["A"]["econ"]["cost_per_day"], 0.072)  # default 0.15/kWh
        self.assertAlmostEqual(nk["fleet_econ"]["chance_day"], nk["devices"]["A"]["econ"]["chance_day"])

    # --- time-of-use schedule ----------------------------------------------------
    def test_schedule_window_matching_including_overnight(self):
        w = miner_hearth.normalize_window({"days": [4], "start": "22:00", "end": "06:00"})
        fri_23 = _dt(2026, 11, 20, 23, 0)
        sat_05 = _dt(2026, 11, 21, 5, 59)
        sat_07 = _dt(2026, 11, 21, 7, 0)
        thu_23 = _dt(2026, 11, 19, 23, 0)
        self.assertTrue(miner_hearth.window_active(w, fri_23))
        self.assertTrue(miner_hearth.window_active(w, sat_05))
        self.assertFalse(miner_hearth.window_active(w, sat_07))
        self.assertFalse(miner_hearth.window_active(w, thu_23))
        s = miner_hearth.normalize_app_settings({"schedule": {"enabled": True, "windows": [
            {"name": "A", "days": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "00:00", "axe_max_freq_mhz": 700},
            {"name": "B", "days": [4], "start": "22:00", "end": "06:00", "axe_max_freq_mhz": 600, "bos_power_target_w": 2500, "devices": ["Rig"]},
        ]}})
        lim = miner_hearth.schedule_limits(s, "Rig", fri_23.timestamp())
        self.assertEqual(lim["max_freq"], 600)
        self.assertEqual(lim["bos_power_target_w"], 2500)
        self.assertEqual(sorted(lim["windows"]), ["A", "B"])
        other = miner_hearth.schedule_limits(s, "Other", fri_23.timestamp())
        self.assertEqual(other["max_freq"], 700)
        s["schedule"]["enabled"] = False
        self.assertIsNone(miner_hearth.schedule_limits(s, "Rig", fri_23.timestamp()))

    def test_schedule_cap_steps_down_but_emergency_always_wins(self):
        cfg = gamma_cfg(asic_target_c=59, asic_max_c=62.5, vr_target_c=62, vr_max_c=65, error_high_pct=1.0)
        sched = {"max_freq": 600, "asic_target_c": None, "bos_power_target_w": None, "windows": ["Peak"]}
        dcfg = autotune.schedule_adjusted_cfg(cfg, sched)
        self.assertEqual(dcfg.max_freq, 600)
        self.assertEqual(cfg.max_freq, 850)  # original untouched
        self.assertEqual(dcfg.asic_max_c, cfg.asic_max_c)  # emergency limits never change
        cool = reading(frequency=800, asic_temp=50, vr_temp=50, core_voltage=1230)
        d = autotune.apply_schedule_cap(autotune.decide(cool, dcfg, 5, error_pct=0.7), cool, cfg, dcfg, sched)
        self.assertEqual(d.action, "step_down")
        self.assertLessEqual(d.new_freq, 600)
        self.assertIn("time-of-use", d.reason)
        hot = reading(frequency=800, asic_temp=63.0, vr_temp=50, core_voltage=1240)
        e = autotune.apply_schedule_cap(autotune.decide(hot, dcfg, 0, error_pct=0.7), hot, cfg, dcfg, sched)
        self.assertEqual(e.action, "emergency_down")
        self.assertEqual(e.new_voltage, 1210)
        self.assertTrue(e.bypass_cooldown)
        # Never steps UP past the window cap.
        at_cap = reading(frequency=600, asic_temp=45, vr_temp=45)
        u = autotune.apply_schedule_cap(autotune.decide(at_cap, dcfg, 10, error_pct=0.2), at_cap, cfg, dcfg, sched)
        self.assertNotEqual(u.action, "step_up")
        # Legacy TOU temperature-cap keys are ignored; normal per-machine ASIC target still applies.
        dcfg2 = autotune.schedule_adjusted_cfg(cfg, {"max_freq": None, "asic_target_c": 55, "windows": ["x"]})
        self.assertEqual(dcfg2.asic_target_c, 59)
        warm = reading(frequency=700, asic_temp=60, vr_temp=50)
        self.assertEqual(autotune.decide(warm, dcfg2, 0, error_pct=0.7).action, "step_down")

    def test_schedule_with_live_tuner_logs_changes_and_bos_window_cap(self):
        g = MockMiner(MinerState(name="g", kind="gamma", frequency=800.0, actual_frequency=800.0, temp=50.0, vr_temp=50.0, core_voltage=1230), port=0).start()
        self.mocks.append(g)
        clock = {"t": _dt(2026, 11, 23, 18, 0).timestamp()}
        _ud, _g, shared, tuner, hub = self._stack(
            [{"name": "G", "ip": "127.0.0.1", "port": g.port, "type": "gamma", "enabled": True, "cooldown_sec": 0}],
            clock=lambda: clock["t"],
        )
        hub.settings.update({"schedule": {"enabled": True, "windows": [{"name": "Evening", "days": [0], "start": "17:00", "end": "21:00", "axe_max_freq_mhz": 600}]}})
        shared.set_enabled("G", True)
        d = tuner.poll_device(tuner.states["G"])
        self.assertEqual(d.action, "step_down")
        self.assertLessEqual(g.state.frequency, 600)
        log = hub.tuning_log.list("G")
        self.assertEqual(log[0]["kind"], "frequency")
        self.assertEqual(log[0]["source"], "schedule")
        self.assertEqual(log[0]["old"], 800.0)
        # Emergency during the window: emergency wins and is logged as such.
        g.state.temp = 63.0
        g.state.core_voltage = 1240
        clock["t"] += 400
        d2 = tuner.poll_device(tuner.states["G"])
        self.assertEqual(d2.action, "emergency_down")
        kinds = [(e["kind"], e["source"]) for e in hub.tuning_log.list("G")[:2]]
        self.assertIn(("voltage", "emergency"), kinds)
        self.assertIn(("frequency", "emergency"), kinds)
        # Braiins power target: the window lowers the ceiling, hot logic still first.
        s19 = antminer_cfg(name="Rig", type="antminer", bos_power_mgmt=True, bos_min_watts=2000, bos_max_watts=3500, bos_cooldown_sec=30)
        r = reading(firmware="Braiins OS", power_limit_w=3200, chip_temps=[70.0])
        win = autotune.dataclasses.replace(s19, bos_max_watts=min(s19.bos_max_watts, 2500), bos_min_watts=min(s19.bos_min_watts, 2500))
        act, new_w, _reason, _ = autotune.decide_bos_power(r, win, 0, 0.0, 1000.0, current_target_w=3200)
        self.assertEqual((act, new_w), ("power_down", 2500))
        hot = reading(firmware="Braiins OS", power_limit_w=2500, chip_temps=[90.0])
        act2, new_w2, _r2, _ = autotune.decide_bos_power(hot, win, 0, 0.0, 1000.0, current_target_w=2500)
        self.assertEqual(act2, "power_down")
        self.assertLess(new_w2, 2500)

    # --- read-only / PIN ---------------------------------------------------------
    def test_lan_read_only_pin_and_header_checks(self):
        _ud, _g, shared, _tuner, hub = self._stack([{"name": "A", "ip": "127.0.0.31", "type": "gamma"}])
        phone = "192.168.1.50"
        hdr = {"X-Requested-With": "MinerHearth"}
        st, data, _ = self._req(shared, "GET", "/api/status", ip=phone)
        self.assertEqual(st, 200)
        self.assertTrue(data["access"]["read_only"])  # default read-only for LAN clients
        for path, body in [("/api/dry-run", {"dry_run": True}), ("/api/device/A/start", {}), ("/api/nk/settings", {"settings": {"lan": {"read_only": False}}}), ("/api/backup/import", {})]:
            st, data, _ = self._req(shared, "POST", path, body, ip=phone, headers=hdr)
            self.assertEqual(st, 403, path)
            self.assertTrue(data.get("read_only"))
        # This PC is never read-only.
        st, data, _ = self._req(shared, "POST", "/api/nk/settings", {"settings": {"lan": {"read_only": False, "pin": "2468"}}})
        self.assertEqual(st, 200)
        self.assertEqual(data["settings"]["lan"]["pin"], "2468")
        # PIN now required for LAN clients.
        st, data, _ = self._req(shared, "GET", "/api/status", ip=phone)
        self.assertEqual(st, 401)
        st, page, ctype = self._req(shared, "GET", "/", ip=phone)
        self.assertEqual(st, 401)
        self.assertIn(b"Enter the dashboard PIN", page)
        st, data, _ = self._req(shared, "GET", "/api/status", ip=phone, headers={"Cookie": "a=b; nk_pin=0000"})
        self.assertEqual(st, 401)
        ok_hdr = {"Cookie": "nk_pin=2468", "X-Requested-With": "MinerHearth"}
        st, data, _ = self._req(shared, "GET", "/api/status", ip=phone, headers=ok_hdr)
        self.assertEqual(st, 200)
        self.assertFalse(data["access"]["read_only"])
        self.assertNotIn("pin", data["nk"]["settings"]["lan"])  # never echoed to LAN clients
        self.assertTrue(data["nk"]["settings"]["lan"]["has_pin"])
        # Write without the custom header (CSRF-style form post) is refused.
        st, _data, _ = self._req(shared, "POST", "/api/dry-run", {"dry_run": True}, ip=phone, headers={"Cookie": "nk_pin=2468"})
        self.assertEqual(st, 403)
        st, _data, _ = self._req(shared, "POST", "/api/dry-run", {"dry_run": True}, ip=phone, headers=ok_hdr)
        self.assertEqual(st, 200)
        st, _data, _ = self._req(shared, "POST", "/api/clipboard", {"text": "x"}, ip=phone, headers=ok_hdr)
        self.assertEqual(st, 403)
        # Brute force lockout.
        for _ in range(10):
            self._req(shared, "GET", "/api/status", ip="192.168.1.77", headers={"Cookie": "nk_pin=1111"})
        st, _d, _ = self._req(shared, "GET", "/api/status", ip="192.168.1.77", headers={"Cookie": "nk_pin=2468"})
        self.assertEqual(st, 429)
        self.assertTrue(miner_hearth.is_loopback("::1"))
        self.assertTrue(miner_hearth.is_loopback("::ffff:127.0.0.1"))
        self.assertFalse(miner_hearth.is_loopback("10.0.0.2"))

    def test_lan_listener_starts_and_stops_from_settings(self):
        import socket as _s
        import urllib.request as _u

        _ud, _g, shared, _tuner, hub = self._stack([])
        with _s.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            free = sk.getsockname()[1]
        hub.update_settings({"lan": {"enabled": True, "port": free}})
        self.assertIsNotNone(hub.lan_server)
        with _u.urlopen(f"http://127.0.0.1:{free}/api/status", timeout=5) as resp:
            self.assertEqual(json.loads(resp.read())["version"], "1.17.1")
        self.assertTrue(hub.lan_info()["running"])
        hub.update_settings({"lan": {"enabled": False}})
        self.assertIsNone(hub.lan_server)

    # --- backup / restore ----------------------------------------------------------
    def test_backup_excludes_passwords_by_default_and_restore_roundtrip(self):
        _ud, gcfg, shared, tuner, hub = self._stack([
            {"name": "Rig", "ip": "127.0.0.41", "type": "antminer", "bos_power_mgmt": True, "bos_password": "s3cret"},
            {"name": "G", "ip": "127.0.0.42", "type": "gamma", "overrides": {"max_freq": 700}},
        ])
        hub.update_settings({"lan": {"pin": "1357"}, "schedule": {"enabled": True, "windows": [{"name": "Peak", "axe_max_freq_mhz": 600}]},
                             "sounds": {"block_sound": "builtin:coin_chime"}})
        tuner.update_settings({"display_fields": {"profit": False}, "device_order": ["G", "Rig"]})
        st, bk, _ = self._req(shared, "GET", "/api/backup/export")
        self.assertEqual(st, 200)
        text = json.dumps(bk)
        self.assertNotIn("s3cret", text)
        self.assertNotIn("1357", text)
        self.assertEqual(bk["userdata"]["device_order"], ["G", "Rig"])
        st, bk_pw, _ = self._req(shared, "GET", "/api/backup/export?include_passwords=1")
        self.assertIn("s3cret", json.dumps(bk_pw))
        self.assertEqual(bk_pw["app_settings"]["lan"]["pin"], "1357")
        hub.update_settings({"lan": {"read_only": False, "pin": ""}})
        st, remote_bk, _ = self._req(shared, "GET", "/api/backup/export?include_passwords=1", ip="192.168.1.9")
        self.assertNotIn("s3cret", json.dumps(remote_bk))  # LAN clients can never pull secrets
        hub.update_settings({"lan": {"pin": "1357"}})

        # Wipe and restore the password-less backup into a fresh stack: secrets kept from same-name device.
        hub.update_settings({"schedule": {"enabled": False, "windows": []}, "sounds": {"block_sound": "builtin:one_up"}})
        st, res, _ = self._req(shared, "POST", "/api/backup/import", bk)
        self.assertEqual(st, 200, res)
        self.assertEqual(res["devices"], 2)
        rig = [d for d in tuner.config.devices if d.name == "Rig"][0]
        self.assertEqual(rig.bos_password, "s3cret")
        self.assertTrue(rig.bos_power_mgmt)
        g = [d for d in tuner.config.devices if d.name == "G"][0]
        self.assertEqual(g.max_freq, 700)
        s = hub.settings.get()
        self.assertTrue(s["schedule"]["enabled"])
        self.assertEqual(s["sounds"]["block_sound"], "builtin:coin_chime")
        self.assertEqual(s["lan"]["pin"], "1357")  # kept, backup had none
        self.assertFalse(tuner.config.display_fields["profit"])
        ud = json.loads((self.dir / "userdata" / "devices.json").read_text())
        self.assertEqual([d["name"] for d in ud["devices"]], ["G", "Rig"])
        st, data, _ = self._req(shared, "GET", "/api/status")
        self.assertEqual([d["name"] for d in data["devices"]], ["G", "Rig"])
        st, err, _ = self._req(shared, "POST", "/api/backup/import", {"hello": 1})
        self.assertEqual(st, 400)
        st, err, _ = self._req(shared, "POST", "/api/backup/import", {"miner_hearth_backup": 1, "userdata": {"devices": [{"name": "X", "ip": "1.2.3.4", "type": "toaster"}]}})
        self.assertEqual(st, 400)
        self.assertEqual(len(tuner.config.devices), 2)  # failed restore changed nothing

    # --- migration -------------------------------------------------------------------
    def test_upgrade_from_v17_userdata_keeps_devices_history_last_share(self):
        ud_dir = self.dir / "userdata"
        (ud_dir / "history").mkdir(parents=True)
        v17 = {
            "schema_version": 2, "app_version": "1.7", "dry_run": True, "scan_subnet": "192.168.7.0/24",
            "device_order": ["Desk", "Rig"],
            "display_fields": {k: True for k in [d["key"] for d in autotune.DISPLAY_FIELD_DEFS[:21]]},
            "global_tuner": autotune.default_global_tuner(),
            "devices": [
                {"name": "Desk", "ip": "192.168.7.20", "type": "gamma", "overrides": {"max_freq": 725}},
                {"name": "Rig", "ip": "192.168.7.30", "type": "antminer", "bos_power_mgmt": True, "bos_password": "pw"},
            ],
        }
        (ud_dir / "devices.json").write_text(json.dumps(v17), encoding="utf-8")
        now = time.time()
        hist = {"schema_version": 1, "device": "Desk", "points": [[now - 3600 + i * 60, 1000.0 + i, 55.0, 60.0] for i in range(60)]}
        hist_text = json.dumps(hist)
        (ud_dir / "history" / "Desk.json").write_text(hist_text, encoding="utf-8")
        ls_text = json.dumps({"schema_version": 1, "devices": {"Desk": {"accepted": 77, "seen_ts": now - 30}}})
        (ud_dir / "last_share.json").write_text(ls_text, encoding="utf-8")
        cfg_path = self.dir / "config.json"
        shutil.copy2(ROOT / "config.json", cfg_path)
        raw = autotune.load_raw_config(cfg_path)
        raw["log_dir"] = str(self.dir / "logs")
        gcfg = autotune.parse_config(raw, self.dir, config_path=cfg_path)
        self.assertEqual([d.name for d in gcfg.devices], ["Desk", "Rig"])
        self.assertTrue(gcfg.dry_run)
        self.assertEqual(gcfg.devices[0].max_freq, 725)
        self.assertEqual(gcfg.devices[1].bos_password, "pw")
        self.assertTrue(gcfg.display_fields["profit"])  # new v1.8 fields default ON
        from dashboard import SharedState

        shared = SharedState([d.name for d in gcfg.devices], gcfg.state_file)
        tuner = autotune.Tuner(gcfg, shared=shared)
        shared.attach_tuner(tuner)
        hub = miner_hearth.attach_feature_hub(tuner, shared, start=False, fetch=FakeEsplora())
        self.hubs.append(hub)
        self.assertTrue((ud_dir / "miner_hearth.json").exists())
        self.assertEqual(hub.settings.get(), miner_hearth.default_app_settings())
        self.assertTrue(hub.migrated_from_v17)
        self.assertGreater(len(hub.long_history.series("Desk", "7d")), 5)
        self.assertEqual((ud_dir / "history" / "Desk.json").read_text(encoding="utf-8"), hist_text)
        self.assertEqual((ud_dir / "last_share.json").read_text(encoding="utf-8"), ls_text)
        ud_after = json.loads((ud_dir / "devices.json").read_text(encoding="utf-8"))
        self.assertEqual([d["name"] for d in ud_after["devices"]], ["Desk", "Rig"])
        self.assertEqual(ud_after["scan_subnet"], "192.168.7.0/24")
        # Shipped config stays generic and shareable.
        shipped = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(shipped["devices"], [])
        self.assertEqual(shipped["dashboard"]["host"], "127.0.0.1")
        self.assertNotIn("payout", json.dumps(shipped).lower())

    # --- history / tuning log / sounds ---------------------------------------------
    def test_long_history_downsampling_fleet_and_csv_export(self):
        _ud, _g, shared, tuner, hub = self._stack([
            {"name": "A", "ip": "127.0.0.51", "type": "gamma"},
            {"name": "B", "ip": "127.0.0.52", "type": "gamma"},
        ])
        t0 = 1_700_000_000.0 - (1_700_000_000.0 % 1800)
        for i in range(120):  # 2 hours at 1-minute polls
            hub.long_history.record("A", t0 + i * 60, 1000.0, 50.0 + (i % 2), 60.0, 20.0)
            hub.long_history.record("B", t0 + i * 60, 500.0, 52.0, 58.0, 10.0)
        a7 = hub.long_history.series("A", "7d")
        a30 = hub.long_history.series("A", "30d")
        self.assertEqual(len(a7), 24)  # 5-min buckets
        self.assertEqual(len(a30), 4)  # 30-min buckets
        self.assertAlmostEqual(a7[0][2], 50.4, places=3)  # bucket mean of 50,51,50,51,50
        st, data, _ = self._req(shared, "GET", "/api/history?range=7d")
        self.assertEqual(data["points"][0][1], 1500.0)  # fleet hashrate sum
        self.assertEqual(data["points"][0][4], 30.0)
        self.assertEqual(data["points"][0][2], 52.0)  # fleet max temp
        st, csv_bytes, ctype = handle_csv = self._req(shared, "GET", "/api/export.csv?device=A&range=30d")
        self.assertEqual(st, 200)
        self.assertTrue(ctype.startswith("text/csv"))
        lines = csv_bytes.decode("utf-8").strip().splitlines()
        self.assertEqual(lines[0], "timestamp_local,unix_ts,device,hashrate_ghs,asic_temp_c,vr_temp_c,power_w")
        self.assertEqual(len(lines), 5)
        self.assertIn(",A,1000.0,", lines[1])
        # retention trims old buckets
        hub.long_history.record("A", t0 + 31 * 86400, 900.0, 50.0, 60.0, 20.0)
        self.assertEqual(len(hub.long_history.series("A", "30d")), 1)
        hub.long_history.save_all()
        self.assertTrue((self.dir / "userdata" / "history_long" / "A.json").exists())

    def test_tuning_log_persists_and_rotates(self):
        path = self.dir / "userdata" / "tuning_log.jsonl"
        tl = miner_hearth.TuningLog(path, max_bytes=2000, keep=2)
        for i in range(60):
            tl.add({"ts": 1000.0 + i, "device": "G" if i % 2 else "N", "kind": "frequency", "old": 500 + i, "new": 525 + i, "reason": "x" * 20, "source": "autotune"})
        self.assertTrue(path.exists())
        self.assertTrue(path.with_name(path.name + ".1").exists())
        self.assertFalse(path.with_name(path.name + ".3").exists())
        self.assertLess(path.stat().st_size, 2400)
        tl2 = miner_hearth.TuningLog(path, max_bytes=2000, keep=2)
        rows = tl2.list("G", limit=5)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["old"], 559)  # newest first
        self.assertTrue(all(r["device"] == "G" for r in rows))

    def test_generated_sounds_custom_upload_and_settings(self):
        for sid in ["coin_chime", "one_up", "level_clear", "power_up", "treasure_chest", "victory_fanfare"]:
            data = nk_sounds.get_builtin_wav(sid)
            self.assertTrue(nk_sounds.looks_like_audio(data, "wav"), sid)
            import io as _io
            import wave as _wave
            with _wave.open(_io.BytesIO(data)) as w:
                self.assertEqual(w.getframerate(), nk_sounds.SAMPLE_RATE)
                self.assertGreater(w.getnframes() / w.getframerate(), 0.2)
                self.assertLess(w.getnframes() / w.getframerate(), 3.0)
        _ud, _g, shared, _tuner, hub = self._stack([])
        st, wav, ctype = self._req(shared, "GET", "/api/sounds/builtin/victory_fanfare.wav")
        self.assertEqual((st, ctype), (200, "audio/wav"))
        import base64 as _b64
        st, res, _ = self._req(shared, "POST", "/api/sounds/upload", {"filename": "../../evil.exe", "data_base64": _b64.b64encode(b"MZ" * 100).decode()})
        self.assertEqual(st, 400)
        st, res, _ = self._req(shared, "POST", "/api/sounds/upload", {"filename": "fake.mp3", "data_base64": _b64.b64encode(b"hello" * 100).decode()})
        self.assertEqual(st, 400)
        st, res, _ = self._req(shared, "POST", "/api/sounds/upload", {"filename": "My Ding!.wav", "data_base64": _b64.b64encode(wav).decode()})
        self.assertEqual(st, 200, res)
        self.assertEqual(res["id"], "custom:My Ding.wav")
        self.assertTrue((self.dir / "userdata" / "sounds" / "My Ding.wav").exists())
        st, _r, _ = self._req(shared, "POST", "/api/nk/settings", {"settings": {"sounds": {"problem_sound": res["id"], "master_mute": True, "block_sound": "builtin:nope"}}})
        s = hub.settings.get()["sounds"]
        self.assertEqual(s["problem_sound"], "custom:My Ding.wav")
        self.assertEqual(s["block_sound"], "builtin:victory_fanfare")  # invalid ref falls back
        self.assertTrue(s["master_mute"])
        st, body, ctype = self._req(shared, "GET", "/api/sounds/custom/My%20Ding.wav")
        self.assertEqual(st, 200)
        st, _r, _ = self._req(shared, "POST", "/api/sounds/delete", {"filename": "My Ding.wav"})
        self.assertEqual(hub.settings.get()["sounds"]["problem_sound"], "builtin:alarm")
        st, res, _ = self._req(shared, "POST", "/api/nk/test-notification", {"kind": "block"})
        self.assertEqual(res["event"]["level"], "block")
        self.assertIn("powershell.exe", " ".join(["powershell.exe"]))
        script = miner_hearth.build_toast_script("Block <found> & 'yay'", "msg")
        self.assertIn("&lt;found&gt; &amp; &apos;yay&apos;", script)
        self.assertNotIn("<found>", script)

    def test_firmware_notices_daily_and_quiet_offline(self):
        self.assertEqual(nk_net.parse_version_tuple("v2.4.2-dirty"), (2, 4, 2))
        self.assertTrue(nk_net.version_is_older((2, 4, 2), (2, 15, 4)))
        self.assertFalse(nk_net.version_is_older((2, 15, 4), (2, 15, 4)))
        self.assertFalse(nk_net.version_is_older(None, (1, 0)))
        self.assertEqual(nk_net.braiins_release_from_text("2026-09-23-0-d6cb014c-26.09.1-plus"), (26, 9, 1))
        self.assertEqual(nk_net.firmware_source_for("gamma", "", "", "v2.4"), "esp-miner")
        self.assertEqual(nk_net.firmware_source_for("axeos-generic", "", "NerdQAxe++", "v1.0"), "nerdqaxe")
        self.assertEqual(nk_net.firmware_source_for("antminer", "Braiins OS", "", ""), "braiins")
        self.assertIsNone(nk_net.firmware_source_for("antminer", "Stock", "", ""))
        calls = []

        def fetch(url, timeout=8.0):
            calls.append(url)
            if "ESP-Miner/releases" in url:
                return json.dumps({"tag_name": "v2.15.4", "html_url": "https://github.com/bitaxeorg/ESP-Miner/releases/tag/v2.15.4"}).encode()
            if "NerdQAxePlus" in url:
                return json.dumps({"tag_name": "v1.1.2"}).encode()
            if "braiins" in url:
                return b'<a href="26.09/">26.09/</a><a href="26.10/">26.10/</a><a href="am3-bbb/">x</a>'
            raise nk_net.NetError("x")

        hub = miner_hearth.FeatureHub(self.dir / "userdata", fetch=fetch, clock=lambda: 5_000_000.0)
        hub.check_firmware()
        self.assertEqual(len(calls), 3)
        hub.check_firmware(5_000_000.0 + 3600)  # same day: no new requests
        self.assertEqual(len(calls), 3)
        s = hub.settings.get()
        ex = hub.device_extras({"name": "G", "type": "gamma", "version": "v2.4.2", "online": True}, s)
        self.assertTrue(ex["firmware"]["update_available"])
        self.assertEqual(ex["firmware"]["latest"], "v2.15.4")
        ex2 = hub.device_extras({"name": "R", "type": "antminer", "is_braiins": True, "version": "bosminer 2026-10-19-0-46be68fe-26.10-plus"}, s)
        self.assertFalse(ex2["firmware"]["update_available"])
        ex3 = hub.device_extras({"name": "N", "type": "nerdoctaxe", "version": "weird"}, s)
        self.assertFalse(ex3["firmware"]["update_available"])
        # Offline: quiet, keeps the last known results, and waits a day.
        hub2 = miner_hearth.FeatureHub(self.dir / "userdata", fetch=lambda u, t=8.0: (_ for _ in ()).throw(nk_net.NetError("offline")), clock=lambda: 5_000_000.0 + 90000)
        self.assertIn("esp-miner", hub2.firmware["latest"])
        hub2.check_firmware()
        self.assertIn("esp-miner", hub2.firmware["latest"])

    def test_hub_tick_fetches_network_price_and_fees(self):
        fake = FakeEsplora(tip=900000)
        fake.add(900000, 1_700_000_000.0, [OTHER])
        hub = self._hub(fake)
        hub.settings.update({"firmware": {"check_enabled": False}})
        hub.tick(1_700_000_100.0, force=True)
        self.assertEqual(hub.network["height"], 900000)
        self.assertEqual(hub.network["difficulty"], NET_DIFF)
        self.assertEqual(hub.price["price"], 60000.0)
        self.assertAlmostEqual(hub.avg_fees, 2_000_000.0)
        fake.offline = True
        hub.tick(1_700_000_500.0, force=True)
        self.assertEqual(hub.network["height"], 900000)  # last good values kept
        self.assertIn("offline", hub.network["error"])

    # --- rebrand ---------------------------------------------------------------------
    def test_rebrand_donate_header_and_packaging(self):
        import miner_dashboard as md

        _ud, _g, shared, _tuner, _hub = self._stack([])
        st, html, _ = self._req(shared, "GET", "/")
        html = html.decode("utf-8")
        self.assertIn("<title>Miner Hearth</title>", html)
        addr = "bc1q3m8usp6mffv4txhwcltq9lu9jltx02eyukgrvf"
        h1 = html.split("<h1>")[1].split("</h1>")[0]
        self.assertTrue(h1.startswith("Miner Hearth <span class=\"ver-pill\" id=\"appVersion\">v1.17.1</span> <span class=\"nk-donate\""))
        self.assertIn(f"Donate BTC: <code id=\"nkDonateAddr\">{addr}</code>", h1)
        self.assertIn("nkCopyDonate()", h1)
        self.assertIn(">Copied<", h1)
        self.assertIn(f'const NK_DONATE = "{addr}";', html)
        self.assertIn("navigator.clipboard", html)
        self.assertIn("window.pywebview.api.copy_text", html)
        self.assertIn("@media (max-width: 720px)", html)
        self.assertEqual(md.APP_NAME, "Miner Hearth")
        self.assertEqual(md.EXE_NAME, "MinerHearth.exe")
        self.assertEqual(md.MUTEX_NAME, "Local\\MinerAutotuneDashboardSingleInstance")
        spec = (ROOT / "packaging" / "MinerHearth.spec").read_text(encoding="utf-8")
        self.assertIn('name="MinerHearth"', spec)
        for mod in ("miner_hearth", "nk_net", "nk_math", "nk_sounds", "nk_ui", "nk_ui19", "nk_coins", "nk_extras", "nk_faults", "nk_fault_codes", "nk_ui110"):
            self.assertIn(f'"{mod}"', spec)
        ver = (ROOT / "packaging" / "version_info.txt").read_text(encoding="utf-8")
        self.assertIn("MinerHearth.exe", ver)
        self.assertIn("(1, 17, 0, 0)", ver)
        for bat in ("enable-autostart.bat", "disable-autostart.bat", "build-exe.bat"):
            self.assertIn("MinerHearth.exe", (ROOT / bat).read_text(encoding="utf-8"), bat)
        self.assertTrue(md.DesktopApi().copy_text("x") in (True, False))
        self.assertIn("Miner Hearth", (ROOT / "README.md").read_text(encoding="utf-8").splitlines()[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# v1.9 Miner Hearth
# ---------------------------------------------------------------------------

import nk_coins  # noqa: E402
import nk_extras  # noqa: E402

BCH_HASH = bytes.fromhex("76a04053bda0a88bda5177b86a15c3b29f559873")
BCH_ADDR = nk_coins.cashaddr_encode("bitcoincash", 0, BCH_HASH)
BCH_SCRIPT = "76a914" + BCH_HASH.hex() + "88ac"
OTHER_SCRIPT = "76a914" + "11" * 20 + "88ac"


class FakeHaskoin:
    """In-memory api.haskoin.com/bch used for multi-coin block confirmation tests."""

    BASE = "https://api.haskoin.com/bch"

    def __init__(self, tip=800000):
        self.tip = tip
        self.blocks = {}
        self.offline = False

    def add(self, h, ts, scripts, address=None):
        self.blocks[h] = {"hash": f"{h:064x}", "height": h, "time": float(ts), "tx": [f"c{h:063x}"],
                          "outputs": [{"pkscript": s, "address": address} for s in scripts]}

    def handles(self, url):
        return url.startswith(self.BASE)

    def __call__(self, url, timeout=8.0):
        if self.offline:
            raise nk_net.NetError("offline")
        path = _urlparse(url).path[len("/bch"):]
        if path == "/block/best":
            return json.dumps({"height": self.tip, "bits": 0x18030000, "time": 1.7e9, "hash": f"{self.tip:064x}"}).encode()
        m = _re.fullmatch(r"/block/height/(\d+)", path)
        if m:
            h = int(m.group(1))
            if h not in self.blocks:
                self.add(h, 1.0, [OTHER_SCRIPT])
            return json.dumps(self.blocks[h]).encode()
        m = _re.fullmatch(r"/block/([0-9a-f]{64})", path)
        if m:
            for b in self.blocks.values():
                if b["hash"] == m.group(1):
                    return json.dumps(b).encode()
        m = _re.fullmatch(r"/transaction/c([0-9a-f]{63})", path)
        if m:
            return json.dumps({"outputs": self.blocks[int(m.group(1), 16)]["outputs"]}).encode()
        raise nk_net.NetError(f"404 {path}")


class RoutedFetch:
    def __init__(self, btc, *others):
        self.btc, self.others = btc, others

    def __call__(self, url, timeout=8.0):
        for o in self.others:
            if o.handles(url):
                return o(url, timeout)
        return self.btc(url, timeout)


class RecordingPoster:
    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": dict(headers)})
        return self.status


class V19MinerHearthTests(V18MinerHearthTests):
    """v1.9 features. Inherits the v1.8 helpers; v1.8 tests are not re-run here."""

    def _hub19(self, fake=None, clock=None, poster=None):
        hub = self._hub(fake, clock)
        hub.webhooks = nk_extras.Webhooks(poster or RecordingPoster(), async_send=False)
        hub.prober = lambda key: (True, 12.5)
        return hub

    # --- coin detection ------------------------------------------------------
    def test_address_codecs_map_to_the_same_script(self):
        self.assertEqual(nk_coins.address_to_script(BCH_ADDR), BCH_SCRIPT)
        self.assertEqual(nk_coins.address_to_script(BCH_ADDR.split(":")[1]), BCH_SCRIPT)  # bare cashaddr
        ecash = nk_coins.cashaddr_encode("ecash", 0, BCH_HASH)
        self.assertEqual(ecash, "ecash:qpm2qsznhks23z7629mms6s4cwef74vcwva87rkuu2")
        self.assertEqual(nk_coins.address_to_script(ecash), BCH_SCRIPT)
        self.assertEqual(nk_coins.address_to_script(nk_coins.cashaddr_encode("bitcoincash", 1, BCH_HASH)), "a914" + BCH_HASH.hex() + "87")
        self.assertEqual(nk_coins.address_to_script(PAYOUT), "0014751e76e8199196d454941c45d1b3a323f1433bd6")
        self.assertEqual(nk_coins.address_to_script("1BoatSLRHtKNngkdXEeobR76b53LETtpyT")[:6], "76a914")
        self.assertIsNone(nk_coins.address_to_script(BCH_ADDR[:-1] + ("q" if BCH_ADDR[-1] != "q" else "p")))  # bad checksum
        self.assertIsNone(nk_coins.address_to_script("not-an-address"))
        self.assertEqual(nk_coins.address_coins(ecash), {"XEC"})
        self.assertEqual(nk_coins.address_coins(BCH_ADDR), {"BCH"})
        self.assertIn("BTC", nk_coins.address_coins(PAYOUT))
        self.assertNotIn("BCH", nk_coins.address_coins(PAYOUT))
        self.assertEqual(nk_coins.nbits_to_difficulty(0x1D00FFFF), 1.0)

    def test_coin_detection_signals_and_manual_override(self):
        stats = {"BTC": {"height": 977144, "difficulty": 1.348e14}, "BCH": {"height": 978584, "difficulty": 5.11e11},
                 "BSV": {"height": 976710, "difficulty": 3.15e10}, "XEC": {"height": 976748, "difficulty": 6.16e9}}
        # Job difficulty matches exactly one chain -> high confidence, even when the pool name says otherwise.
        r = nk_coins.detect_coin({"pool_url": "solo.ckpool.org", "network_difficulty": 6.0857e9}, stats)
        self.assertEqual((r["coin"], r["confidence"]), ("XEC", "high"))
        r = nk_coins.detect_coin({"pool_url": "x.example", "block_height": 978585}, stats)
        self.assertEqual((r["coin"], r["confidence"]), ("BCH", "high"))
        # Height and difficulty must both agree when both are known.
        r = nk_coins.detect_coin({"block_height": 978584, "network_difficulty": 1.348e14}, stats)
        self.assertEqual(r["coin"], "unknown")
        # No chain data: pool host + address format.
        r = nk_coins.detect_coin({"pool_url": "xec.heliospool.com", "pool_user": "ecash:qz3h3f8er6dy7tfrzh5cxptueu2m6rm9u5cdj6rj7y.rig"}, {})
        self.assertEqual((r["coin"], r["confidence"]), ("XEC", "medium"))
        r = nk_coins.detect_coin({"pool_url": "public-pool.io", "pool_user": PAYOUT + ".worker"}, {})
        self.assertEqual(r["coin"], "BTC")
        r = nk_coins.detect_coin({"pool_url": "pool.example.com", "pool_user": BCH_ADDR + ".w"}, {})
        self.assertEqual((r["coin"], r["source"]), ("BCH", "address"))
        # Ambiguous legacy address on an unknown pool: unknown, never a guess.
        r = nk_coins.detect_coin({"pool_url": "pool.example.com", "pool_user": "1BoatSLRHtKNngkdXEeobR76b53LETtpyT"}, {})
        self.assertEqual((r["coin"], r["confidence"]), ("unknown", "none"))
        # Manual override always wins.
        r = nk_coins.detect_coin({"pool_url": "public-pool.io", "pool_user": PAYOUT}, stats, override="bsv")
        self.assertEqual((r["coin"], r["confidence"]), ("BSV", "manual"))

    def test_hub_detects_coin_fetches_only_needed_chains_and_groups_odds(self):
        hub = self._hub19()
        hub.network = {"height": 977144, "difficulty": 1.348e14}
        btc = gamma_cfg(name="BTC rig")
        xec = gamma_cfg(name="XEC rig")
        t = 1_700_000_000.0
        hub.on_poll(btc, reading(pool_url="public-pool.io", pool_user=PAYOUT + ".a", network_difficulty=1.35e14), t)
        self.assertEqual(hub.coin_info["BTC rig"]["coin"], "BTC")
        self.assertEqual(hub._coins_wanted, set())  # a BTC-only fleet never queries other chains
        hub.on_poll(xec, reading(pool_url="xec.heliospool.com", pool_user="ecash:qz3h3f8er6dy7tfrzh5cxptueu2m6rm9u5cdj6rj7y.d", network_difficulty=6.0857e9, hashrate=12000), t)
        self.assertEqual(hub.coin_info["XEC rig"]["coin"], "XEC")
        self.assertIn("XEC", hub._coins_wanted)
        hub.coin_stats["XEC"] = {"height": 976748, "difficulty": 6.16e9, "source": "chronik.e.cash"}
        devs = [{"name": "BTC rig", "online": True, "hashrate_ghs": 1000.0}, {"name": "XEC rig", "online": True, "hashrate_ghs": 12000.0}]
        groups = {g["coin"]: g for g in hub.coins_payload(devs, hub.settings.get())}
        self.assertEqual(groups["XEC"]["devices"], ["XEC rig"])
        self.assertAlmostEqual(groups["XEC"]["odds"]["blocks_per_day"], nk_math.block_odds(12000.0, 6.16e9)["blocks_per_day"], places=9)
        self.assertGreater(groups["XEC"]["odds"]["blocks_per_day"], groups["BTC"]["odds"]["blocks_per_day"] * 1000)
        # Manual override in settings
        hub.settings.update({"coins": {"overrides": {"BTC rig": "dgb"}}})
        hub.on_poll(btc, reading(pool_url="public-pool.io", pool_user=PAYOUT), t + 10)
        self.assertEqual(hub.coin_info["BTC rig"]["coin"], "DGB")

    def test_status_payload_has_per_coin_groups(self):
        _ud, _g, shared, _tuner, hub = self._stack([{"name": "A", "ip": "127.0.0.41", "type": "gamma"}, {"name": "S", "ip": "127.0.0.42", "type": "antminer"}])
        hub.network = {"height": 900_000, "difficulty": NET_DIFF}
        hub.coin_info["A"] = {"coin": "BTC", "confidence": "medium", "source": "pool-hint", "reason": ""}
        shared.update_device("A", {"online": True, "hashrate_ghs": 1000.0})
        shared.update_device("S", {"online": True, "hashrate_ghs": 100000.0})
        st, data, _ = self._req(shared, "GET", "/api/status")
        self.assertEqual(st, 200)
        coins = {g["coin"]: g for g in data["nk"]["coins"]}
        self.assertEqual(coins["BTC"]["devices"], ["A"])
        self.assertEqual(coins["unknown"]["devices"], ["S"])
        self.assertIsNone(coins["unknown"]["odds"])
        self.assertAlmostEqual(coins["BTC"]["odds"]["blocks_per_day"], nk_math.block_odds(1000.0, NET_DIFF)["blocks_per_day"])
        self.assertEqual({c["id"] for c in data["nk"]["coin_list"]}, set(nk_coins.COIN_IDS))

    # --- multi-coin block confirmation -----------------------------------------
    def _bch_hub(self, payout=(BCH_ADDR,)):
        btc = FakeEsplora(tip=900000)
        bch = FakeHaskoin(tip=800000)
        hub = self._hub19(RoutedFetch(btc, bch))
        hub.settings.update({"block": {"payout_addresses": [PAYOUT]}, "coins": {"payout_addresses": {"BCH": list(payout)}}})
        hub.network = {"difficulty": NET_DIFF, "height": 900000}
        hub.coin_stats["BCH"] = {"height": 800000, "difficulty": 5.0e11}
        return btc, bch, hub

    def test_bch_block_confirmed_on_the_bch_chain_with_script_match(self):
        btc, bch, hub = self._bch_hub()
        cfg = gamma_cfg(name="B")
        t = 1_700_000_000.0
        r = dict(pool_url="bch.solopool.org", pool_user=BCH_ADDR + ".rig", network_difficulty=5.0e11)
        hub.on_poll(cfg, reading(best_session_diff=1e9, **r), t)
        self.assertEqual(hub.coin_info["B"]["coin"], "BCH")
        # 6e11 is far below BTC difficulty but above BCH difficulty -> candidate on BCH.
        hub.on_poll(cfg, reading(best_session_diff=6e11, **r), t + 15)
        self.assertEqual(len(hub.blocks.candidates), 1)
        self.assertEqual(hub.blocks.candidates[0]["coin"], "BCH")
        self.assertEqual(hub.blocks.candidates[0]["network_difficulty"], 5.0e11)
        bch.tip = 800001
        bch.add(800001, t + 5, [OTHER_SCRIPT, BCH_SCRIPT], address=None)  # explorer gives only the script
        hub.check_candidates(t + 60)
        c = hub.blocks.candidates[0]
        self.assertEqual(c["status"], "confirmed")
        self.assertEqual(c["paid_to"], BCH_ADDR)
        ev = [e for e in hub.events if e["kind"] == "block_confirmed"]
        self.assertEqual(len(ev), 1)
        self.assertEqual("Block Confirmed #800001", ev[0]["title"])
        self.assertIn("Coin: BCH", ev[0]["message"])
        self.assertFalse(any("/api/block-height/" in u for u in btc.calls))  # never asked the BTC explorer

    def test_bch_false_positive_and_btc_address_never_confirm_alt_block(self):
        # The BTC payout address must not confirm a BCH block, even with the same script.
        btc, bch, hub = self._bch_hub(payout=())
        cfg = gamma_cfg(name="B")
        t = 1_700_000_000.0
        r_acct = dict(pool_url="bch.solopool.org", pool_user="acct_only.rig", network_difficulty=5.0e11)
        hub.on_poll(cfg, reading(best_session_diff=1e9, **r_acct), t)
        hub.on_poll(cfg, reading(best_session_diff=6e11, **r_acct), t + 15)
        hub.check_candidates(t + 30)
        self.assertEqual(hub.blocks.candidates[0]["status"], "possible")
        r = dict(pool_url="bch.solopool.org", pool_user=BCH_ADDR, network_difficulty=5.0e11)
        self.assertFalse(any(e["level"] == "block" for e in hub.events))
        # Now with a BCH address, but the blocks pay someone else -> rejected, no alert.
        self.dir = Path(tempfile.mkdtemp(dir=self.tmp.name))
        btc2, bch2, hub2 = self._bch_hub()
        hub2.on_poll(cfg, reading(best_session_diff=1e9, **r), t)
        hub2.on_poll(cfg, reading(best_session_diff=6e11, **r), t + 15)
        bch2.offline = True
        hub2.check_candidates(t + 60)
        self.assertEqual(hub2.blocks.candidates[0]["status"], "pending")  # explorer down: wait, never alert
        bch2.offline = False
        for i in range(1, 8):
            bch2.add(800000 + i, t + 600 * i, [OTHER_SCRIPT])
        bch2.add(799999, t - 600, [OTHER_SCRIPT])
        bch2.add(800000, t - 60, [OTHER_SCRIPT])
        bch2.tip = 800007
        hub2.check_candidates(t + 4000)
        self.assertEqual(hub2.blocks.candidates[0]["status"], "rejected")
        self.assertFalse(any(e["level"] == "block" for e in hub2.events))

    # --- session best -------------------------------------------------------------
    def test_session_best_api_and_derived_with_restart_reset_and_persistence(self):
        path = self.dir / "session_best.json"
        tr = nk_extras.SessionBestTracker(path)
        t = 1_700_000_000.0
        self.assertEqual(tr.update("S", None, 1000, [5e6], t)["session_best"], 5e6)
        self.assertEqual(tr.update("S", None, 1100, [3e6], t + 100)["session_best"], 5e6)
        out = tr.update("S", None, 1200, [9e6, None], t + 200)
        self.assertEqual((out["session_best"], out["source"], out["all_time_best"]), (9e6, "derived", 9e6))
        out = tr.update("S", None, 30, [1e6], t + 300)  # uptime went down -> miner restarted
        self.assertTrue(out["restarted"])
        self.assertEqual(out["session_best"], 1e6)
        self.assertEqual(out["all_time_best"], 9e6)
        self.assertAlmostEqual(out["session_start_ts"], t + 270)
        again = nk_extras.SessionBestTracker(path)
        self.assertEqual(again.get("S")["all_time_best"], 9e6)
        # API value (CGMiner "Best Share" since start) is used as-is.
        out = again.update("S", 4.2e7, 400, [1e6], t + 400)
        self.assertEqual((out["session_best"], out["source"], out["all_time_best"]), (4.2e7, "api", 4.2e7))
        again.rename("S", "S19 garage")
        self.assertIsNone(again.get("S"))
        self.assertEqual(nk_extras.SessionBestTracker(path).get("S19 garage")["session_best"], 4.2e7)

    def test_antminer_poll_fills_session_best_in_reading(self):
        hub = self._hub19()
        cfg = antminer_cfg(name="S19")
        rd = reading(best_diff=None, best_session_diff=None, uptime_sec=5000, last_submitted_diff=2.5e6,
                     pools=[{"url": "stratum+tcp://pool.example.com:3333", "best_share": 7.5e8}])
        hub.on_poll(cfg, rd, 1_700_000_000.0)
        self.assertEqual(rd.best_session_diff, 7.5e8)
        self.assertEqual(hub.session_best.get("S19")["source"], "derived")
        extras = hub.device_extras({"name": "S19", "online": True, "hashrate_ghs": 100000.0, "type": "antminer"}, hub.settings.get())
        self.assertEqual(extras["session_best"]["session_best"], 7.5e8)

    # --- pool health / alerts / webhooks -----------------------------------------
    def test_pool_rejects_alert_fires_once_and_sends_webhook(self):
        poster = RecordingPoster()
        hub = self._hub19(poster=poster)
        hub.settings.update({"webhooks": {"enabled": True, "ntfy_url": "https://ntfy.sh/nk-test-topic", "ntfy_token": "tk_secret"}})
        cfg = gamma_cfg(name="G")
        t = 1_700_000_000.0
        hub.on_poll(cfg, reading(shares_accepted=100, shares_rejected=0, pool_url="public-pool.io", pool_port=21496), t)
        hub.on_poll(cfg, reading(shares_accepted=108, shares_rejected=1, pool_url="public-pool.io", pool_port=21496), t + 60)
        self.assertFalse([e for e in hub.events if e["kind"] == "pool_rejects"])  # 9 shares < min 10
        hub.on_poll(cfg, reading(shares_accepted=110, shares_rejected=4, pool_url="public-pool.io", pool_port=21496), t + 120)
        evs = [e for e in hub.events if e["kind"] == "pool_rejects"]
        self.assertEqual(len(evs), 1)
        self.assertIn("public-pool.io:21496", evs[0]["message"])
        hub.on_poll(cfg, reading(shares_accepted=111, shares_rejected=6, pool_url="public-pool.io", pool_port=21496), t + 180)
        self.assertEqual(len([e for e in hub.events if e["kind"] == "pool_rejects"]), 1)  # cooldown
        ntfy = [c for c in poster.calls if c["url"] == "https://ntfy.sh/nk-test-topic"]
        self.assertEqual(len(ntfy), 1)
        self.assertEqual(ntfy[0]["headers"]["Authorization"], "Bearer tk_secret")
        self.assertEqual(ntfy[0]["headers"]["Priority"], "4")
        self.assertIn("rejecting shares", ntfy[0]["headers"]["Title"])
        # Counter reset (miner reboot) does not create negative deltas.
        ph = hub.pool_health.update("G", 5, 0, 0, t + 240)
        self.assertEqual((ph["d_acc"], ph["d_rej"]), (0, 0))
        # Pool comparison buckets
        hub.pool_stats.record_probe("public-pool.io:21496", True, 20.0, t + 240)
        hub.pool_stats.record_probe("public-pool.io:21496", False, None, t + 360)
        row = [r for r in hub.pool_stats.summary(t + 400) if r["pool"] == "public-pool.io:21496"][0]
        self.assertEqual(row["24h"]["accepted"], 11)
        self.assertEqual(row["24h"]["rejected"], 6)
        self.assertEqual(row["24h"]["uptime_pct"], 50.0)
        self.assertEqual(row["devices"], ["G"])

    def test_webhooks_off_by_default_types_and_payloads(self):
        poster = RecordingPoster()
        hub = self._hub19(poster=poster)
        self.assertFalse(hub.settings.get()["webhooks"]["enabled"])
        hub.emit(miner_hearth._event("offline", "problem", "G", "G offline", "msg"))
        self.assertEqual(poster.calls, [])  # off by default
        hub.settings.update({"webhooks": {"enabled": True, "discord_url": "https://discord.com/api/webhooks/1/abc",
                                          "telegram_token": "123456:ABCDEFGHIJKLMNOP", "telegram_chat_id": "-100123",
                                          "ntfy_url": "https://ntfy.sh/topic", "types": {"offline": False}}})
        hub.emit(miner_hearth._event("offline", "problem", "G", "G offline", "msg"))
        self.assertEqual(poster.calls, [])  # type turned off
        hub.emit(miner_hearth._event("block_confirmed", "block", "G", "BLOCK FOUND by G!", "block 1"))
        urls = sorted(c["url"] for c in poster.calls)
        self.assertEqual(urls, ["https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOP/sendMessage", "https://discord.com/api/webhooks/1/abc", "https://ntfy.sh/topic"])
        disc = [c for c in poster.calls if "discord" in c["url"]][0]
        self.assertIn("BLOCK FOUND by G!", json.loads(disc["body"])["content"])
        tg = [c for c in poster.calls if "telegram" in c["url"]][0]
        self.assertEqual(json.loads(tg["body"])["chat_id"], "-100123")
        self.assertEqual([c for c in poster.calls if "ntfy" in c["url"]][0]["headers"]["Tags"], "white_check_mark")
        self.assertEqual(set(hub.webhooks.last), {"discord", "telegram", "ntfy"})
        # A failing service is reported, never raised.
        bad = nk_extras.Webhooks(lambda *a: (_ for _ in ()).throw(RuntimeError("boom")), async_send=False)
        res = bad.send(hub.settings.get()["webhooks"], miner_hearth._event("offline", "problem", "G", "x", "y"), force=True)
        self.assertTrue(res and not any(r["ok"] for r in res))
        # Invalid secrets are dropped by validation.
        hub.settings.update({"webhooks": {"discord_url": "javascript:alert(1)", "telegram_token": "nope"}})
        self.assertEqual(hub.settings.get()["webhooks"]["discord_url"], "")
        self.assertEqual(hub.settings.get()["webhooks"]["telegram_token"], "")

    def test_webhook_test_route_secrets_hidden_and_excluded_from_backup(self):
        _ud, _g, shared, tuner, hub = self._stack([{"name": "A", "ip": "127.0.0.51", "type": "gamma"}])
        poster = RecordingPoster()
        hub.webhooks = nk_extras.Webhooks(poster, async_send=False)
        st, data, _ = self._req(shared, "POST", "/api/nk/test-webhook", {})
        self.assertEqual(st, 400)
        hub.settings.update({"webhooks": {"ntfy_url": "https://ntfy.sh/secret-topic-123"}})
        st, data, _ = self._req(shared, "POST", "/api/nk/test-webhook", {})
        self.assertEqual(st, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(poster.calls[0]["url"], "https://ntfy.sh/secret-topic-123")
        self.assertFalse(hub.settings.get()["webhooks"]["enabled"])  # testing does not switch webhooks on
        # LAN clients never see the secret; the local UI does.
        self.assertEqual(miner_hearth.public_settings(hub.settings.get(), remote=True)["webhooks"]["ntfy_url"], "")
        self.assertTrue(miner_hearth.public_settings(hub.settings.get(), remote=True)["webhooks"]["has_ntfy_url"])
        # "__keep__" from the UI keeps the stored secret.
        st, _data, _ = self._req(shared, "POST", "/api/nk/settings", {"settings": {"webhooks": {"ntfy_url": "__keep__", "enabled": True}}})
        self.assertEqual(st, 200)
        self.assertEqual(hub.settings.get()["webhooks"]["ntfy_url"], "https://ntfy.sh/secret-topic-123")
        # Backups leave secrets out unless "include passwords" is ticked.
        b = miner_hearth.build_backup(tuner, hub.settings.get(), include_passwords=False)
        self.assertNotIn("secret-topic-123", json.dumps(b))
        b2 = miner_hearth.build_backup(tuner, hub.settings.get(), include_passwords=True)
        self.assertIn("secret-topic-123", json.dumps(b2))
        # Restoring a backup without secrets keeps the current ones.
        ok, _err, _sum = hub.restore_backup(b)
        self.assertTrue(ok)
        self.assertEqual(hub.settings.get()["webhooks"]["ntfy_url"], "https://ntfy.sh/secret-topic-123")

    # --- restarts -------------------------------------------------------------
    def _restart_cfg(self, **kw):
        s = miner_hearth.default_app_settings()["restarts"]
        for k, v in kw.items():
            if isinstance(v, dict):
                s[k].update(v)
            else:
                s[k] = v
        return s

    def test_restarts_off_by_default_and_hung_detection_with_rate_limits(self):
        rm = nk_extras.RestartManager(self.dir / "restarts.json")
        t = 1_700_000_000.0
        hung = reading(hashrate=0, uptime_sec=7200)
        self.assertIsNone(rm.evaluate("G", hung, miner_hearth.default_app_settings()["restarts"], t + 9999))
        cfg = self._restart_cfg(hung={"enabled": True, "minutes": 15})
        self.assertIsNone(rm.evaluate("G", hung, cfg, t))
        self.assertIsNone(rm.evaluate("G", hung, cfg, t + 14 * 60))
        self.assertIsNone(rm.evaluate("G", reading(ok=False, hashrate=None), cfg, t + 15 * 60))  # offline: never restart, timer resets
        self.assertIsNone(rm.evaluate("G", hung, cfg, t + 16 * 60))
        due = rm.evaluate("G", hung, cfg, t + 31 * 60)
        self.assertEqual(due["kind"], "hung")
        self.assertNotIn("blocked", due)
        rm.record("G", due, t + 31 * 60)
        # Grace period after a restart.
        self.assertIsNone(rm.evaluate("G", hung, cfg, t + 40 * 60))
        # Fresh boot (uptime shorter than the hung window) is not "hung".
        self.assertIsNone(rm.evaluate("G", reading(hashrate=0, uptime_sec=120), cfg, t + 60 * 60))
        rm.evaluate("G", hung, cfg, t + 60 * 60)
        due2 = rm.evaluate("G", hung, cfg, t + 80 * 60)
        self.assertIn("less than 60 min", due2["blocked"])  # min gap
        rm.record("G", {"kind": "hung"}, t + 100 * 60)
        rm.evaluate("G", hung, cfg, t + 200 * 60)
        due3 = rm.evaluate("G", hung, cfg, t + 220 * 60)
        self.assertIn("limit of 2 restarts", due3["blocked"])  # max per day
        # Healthy hashrate never restarts.
        self.assertIsNone(rm.evaluate("H", reading(hashrate=900, uptime_sec=99999), cfg, t))
        # Device filter
        cfg_f = self._restart_cfg(hung={"enabled": True, "minutes": 5}, devices=["Other"])
        self.assertIsNone(rm.evaluate("X", hung, cfg_f, t))
        self.assertIsNone(rm.evaluate("X", hung, cfg_f, t + 3600))
        # Persisted history
        self.assertEqual(len(nk_extras.RestartManager(self.dir / "restarts.json").history["G"]), 2)

    def test_weekly_restart_window_once_per_week(self):
        rm = nk_extras.RestartManager(None)
        cfg = self._restart_cfg(weekly={"enabled": True, "day": 6, "time": "04:00"})
        sun_0410 = _dt(2026, 11, 22, 4, 10).timestamp()  # a Sunday
        self.assertIsNone(rm.evaluate("G", reading(), cfg, _dt(2026, 11, 22, 3, 59).timestamp()))
        self.assertIsNone(rm.evaluate("G", reading(), cfg, _dt(2026, 11, 21, 4, 10).timestamp()))  # Saturday
        due = rm.evaluate("G", reading(), cfg, sun_0410)
        self.assertEqual(due["kind"], "weekly")
        self.assertIsNone(rm.evaluate("G", reading(ok=False), cfg, sun_0410))  # offline machine: skip
        rm.record("G", due, sun_0410)
        self.assertIsNone(rm.evaluate("G", reading(), cfg, sun_0410 + 1000))  # grace + done today
        self.assertIsNone(rm.evaluate("G", reading(), cfg, _dt(2026, 11, 22, 4, 31).timestamp()))
        self.assertEqual(rm.evaluate("G", reading(), cfg, _dt(2026, 11, 29, 4, 0).timestamp())["kind"], "weekly")

    def test_hub_restart_respects_dry_run_and_logs(self):
        hub = self._hub19()

        class StubTuner:
            def __init__(self, dry):
                self.dry, self.calls = dry, []

            def _dry_run(self):
                return self.dry

            def maintenance_restart(self, name):
                self.calls.append(name)
                return "dry-run: would restart" if self.dry else "POST /api/system/restart HTTP 200"

        hub.tuner = StubTuner(True)
        hub.settings.update({"restarts": {"hung": {"enabled": True, "minutes": 5}}})
        cfg = gamma_cfg(name="G")
        t = 1_700_000_000.0
        hub.on_poll(cfg, reading(hashrate=0, uptime_sec=3600), t)
        hub.on_poll(cfg, reading(hashrate=0, uptime_sec=3900), t + 301)
        self.assertEqual(hub.tuner.calls, ["G"])
        ev = [e for e in hub.events if e["kind"] == "restart"]
        self.assertEqual(len(ev), 1)
        self.assertIn("would restart", ev[0]["title"])
        log = hub.tuning_log.list("G")
        self.assertEqual((log[0]["kind"], log[0]["source"], log[0]["dry_run"]), ("restart", "restart", True))

    def test_tuner_maintenance_restart_live_dry_and_antminer_guard(self):
        g = MockMiner(MinerState(name="g", kind="gamma"), port=0).start()
        self.mocks.append(g)
        _ud, _g, shared, tuner, hub = self._stack([{"name": "G", "ip": "127.0.0.1", "port": g.port, "type": "gamma"},
                                                    {"name": "S", "ip": "127.0.0.1", "port": 1, "type": "antminer"}])
        res = tuner.maintenance_restart("G")
        self.assertIn("restart", res.lower())
        self.assertEqual(g.state.restarts, 1)
        with self.assertRaises(autotune.DeviceError):
            tuner.maintenance_restart("S")  # stock firmware / no Braiins login

    # --- quiet mode -------------------------------------------------------------
    def test_decide_quiet_emergency_always_wins(self):
        w = {"fan_max_pct": 40, "windows": ["Night"]}
        self.assertEqual(nk_extras.decide_quiet(None, w, False, 100, 0)[:2], ("cap", 40))
        self.assertEqual(nk_extras.decide_quiet(40, w, False, 100, 0)[0], "none")
        self.assertEqual(nk_extras.decide_quiet(40, w, True, 100, 0)[:2], ("release", None))
        self.assertEqual(nk_extras.decide_quiet(None, w, True, 100, 0)[0], "none")
        self.assertEqual(nk_extras.decide_quiet(None, w, False, 100, 500)[0], "none")  # suspended after emergency
        self.assertEqual(nk_extras.decide_quiet(40, None, False, 100, 0)[0], "release")  # window over
        self.assertEqual(nk_extras.decide_quiet(40, {"fan_max_pct": 60, "windows": ["x"]}, False, 100, 0)[:2], ("cap", 60))

    def test_tuner_quiet_mode_caps_fan_and_emergency_releases(self):
        g = MockMiner(MinerState(name="g", kind="gamma", frequency=800.0, actual_frequency=800.0, temp=50.0, vr_temp=50.0), port=0).start()
        self.mocks.append(g)
        clock = {"t": _dt(2026, 11, 23, 1, 0).timestamp()}
        _ud, _g, shared, tuner, hub = self._stack(
            [{"name": "G", "ip": "127.0.0.1", "port": g.port, "type": "gamma", "enabled": True, "cooldown_sec": 0}],
            clock=lambda: clock["t"],
        )
        self.assertIsNone(hub.quiet_limits("G"))  # off by default
        hub.settings.update({"quiet": {"enabled": True, "windows": [{"name": "Night", "days": [0, 1, 2, 3, 4, 5, 6], "start": "22:00", "end": "07:00",
                                                                        "fan_max_pct": 40, "axe_max_freq_mhz": 600}]}})
        shared.set_enabled("G", True)
        tuner.poll_device(tuner.states["G"])
        fan = [p["body"] for p in g.state.patches if "autofanspeed" in p["body"]]
        self.assertEqual(fan[-1], {"autofanspeed": 0, "fanspeed": 40, "manualFanSpeed": 40})
        self.assertLessEqual(g.state.frequency, 600)  # quiet window also lowers the frequency ceiling
        self.assertEqual(tuner.states["G"].quiet_fan_pct, 40)
        # Emergency: fan straight back to automatic, logged as emergency.
        g.state.temp = 66.0
        clock["t"] += 60
        tuner.poll_device(tuner.states["G"])
        fan = [p["body"] for p in g.state.patches if "autofanspeed" in p["body"]]
        self.assertEqual(fan[-1], {"autofanspeed": 1})
        self.assertIsNone(tuner.states["G"].quiet_fan_pct)
        fan_log = [e for e in hub.tuning_log.list("G") if e["kind"] == "fan"]
        self.assertEqual(fan_log[0]["source"], "emergency")
        # Cooled down but still inside the 15 min suspension: no re-cap.
        g.state.temp = 50.0
        clock["t"] += 300
        n = len(fan)
        tuner.poll_device(tuner.states["G"])
        self.assertEqual(len([p for p in g.state.patches if "autofanspeed" in p["body"]]), n)
        clock["t"] += 900
        tuner.poll_device(tuner.states["G"])
        self.assertEqual([p["body"] for p in g.state.patches if "autofanspeed" in p["body"]][-1]["fanspeed"], 40)
        # Autotune off -> fan released.
        shared.set_enabled("G", False)
        clock["t"] += 60
        tuner.poll_device(tuner.states["G"])
        self.assertEqual([p["body"] for p in g.state.patches if "autofanspeed" in p["body"]][-1], {"autofanspeed": 1})
        # App shutdown releases any cap.
        shared.set_enabled("G", True)
        clock["t"] += 60
        tuner.poll_device(tuner.states["G"])
        self.assertEqual(tuner.release_fan_caps(), 1)
        self.assertEqual([p["body"] for p in g.state.patches if "autofanspeed" in p["body"]][-1], {"autofanspeed": 1})

    # --- chip health ------------------------------------------------------------
    def test_chip_health_logs_stable_points_and_flags_wear(self):
        ch = nk_extras.ChipHealth(self.dir / "chip_health.json")

        def stable_day(day, mv, err=0.2, hot=False):
            t0 = _dt(2026, 1, day, 12, 0).timestamp()
            a = ch.observe("G", 600, mv, err, 1.0, hot, t0, 30)
            return ch.observe("G", 600, mv, err, 1.0, hot, t0 + 31 * 60, 30) or a

        self.assertFalse(stable_day(1, 1200, err=3.0))  # error too high -> not stable
        t0 = _dt(2026, 1, 1, 13, 0).timestamp()
        self.assertFalse(ch.observe("G", 600, 1200, 0.2, 1.0, False, t0, 30))
        self.assertFalse(ch.observe("G", 600, 1200, 0.2, 1.0, True, t0 + 25 * 60, 30))  # emergency restarts the clock
        self.assertFalse(ch.observe("G", 600, 1200, 0.2, 1.0, False, t0 + 35 * 60, 30))
        self.assertTrue(ch.observe("G", 600, 1200, 0.2, 1.0, False, t0 + 56 * 60, 30))
        self.assertFalse(stable_day(1, 1200))  # same day, same voltage: nothing new
        for d in (2, 3, 4):
            self.assertTrue(stable_day(d, 1200))
        self.assertIsNone(ch.wear("G", 20, 14))
        for d in (20, 21, 22):
            self.assertTrue(stable_day(d, 1230))
        w = ch.wear("G", 20, 14)
        self.assertEqual((w["freq"], w["baseline_mv"], w["recent_mv"], w["delta_mv"]), (600, 1200, 1230, 30))
        self.assertIsNone(ch.wear("G", 40, 14))
        self.assertEqual(nk_extras.ChipHealth(self.dir / "chip_health.json").summary("G")["latest"], ["2026-01-22", 600, 1230])

    # --- units / layout / migration ---------------------------------------------
    def test_temperature_unit_conversion(self):
        self.assertEqual(nk_extras.c_to_f(0), 32.0)
        self.assertEqual(nk_extras.c_to_f(65), 149.0)
        self.assertAlmostEqual(nk_extras.f_to_c(212), 100.0)
        self.assertAlmostEqual(nk_extras.f_to_c(nk_extras.c_to_f(61.3)), 61.3)
        self.assertIsNone(nk_extras.c_to_f(None))
        self.assertEqual(nk_extras.format_temp(55, "F"), "131.0 °F")
        self.assertEqual(nk_extras.format_temp(55, "C"), "55.0 °C")
        self.assertEqual(nk_extras.format_temp(None, "F"), "—")
        hub = self._hub19()
        hub.settings.update({"ui": {"temp_unit": "f", "theme": "contrast"}})
        self.assertEqual(hub.settings.get()["ui"], {"theme": "contrast", "temp_unit": "F", "background": True})
        hub.settings.update({"ui": {"theme": "neon"}})
        self.assertEqual(hub.settings.get()["ui"]["theme"], "dark")
        # Display-only: limits stay in °C.
        self.assertEqual(hub.settings.get()["alerts"]["pool_rejects"]["pct"], 5.0)

    def test_layout_normalize_persist_and_reset(self):
        lay = nk_extras.normalize_layout({"sections": ["cards", "bogus", "cards", "charts"], "summary_fields": ["power"], "card_fields": "nope"})
        self.assertLess(lay["sections"].index("cards"), lay["sections"].index("charts"))
        self.assertLess(lay["sections"].index("cards"), lay["sections"].index("compare"))
        self.assertEqual(sorted(lay["sections"]), sorted(nk_extras.SECTION_KEYS))
        self.assertIn("power", lay["summary_fields"])
        self.assertEqual(sorted(lay["summary_fields"]), sorted(nk_extras.SUMMARY_FIELD_KEYS))
        self.assertEqual(lay["card_fields"], nk_extras.CARD_FIELD_KEYS)
        _ud, _g, shared, _tuner, hub = self._stack([{"name": "A", "ip": "127.0.0.61", "type": "gamma"}])
        new_sections = ["cards", "charts"] + [k for k in nk_extras.SECTION_KEYS if k not in ("cards", "charts")]
        st, _d, _ = self._req(shared, "POST", "/api/nk/settings", {"settings": {"layout": {"sections": new_sections}}})
        self.assertEqual(st, 200)
        disk = json.loads((self.dir / "userdata" / "miner_hearth.json").read_text(encoding="utf-8"))
        self.assertEqual(disk["layout"]["sections"][:2], ["cards", "charts"])
        self.assertEqual(disk["layout"]["card_fields"], nk_extras.CARD_FIELD_KEYS)  # other lists untouched
        st, data, _ = self._req(shared, "GET", "/api/status")
        self.assertEqual(data["nk"]["settings"]["layout"]["sections"][:2], ["cards", "charts"])
        # On/off toggles are independent of order.
        self.assertTrue(data["settings"]["display_fields"].get("session_best", True))
        st, data, _ = self._req(shared, "POST", "/api/nk/reset-layout", {})
        self.assertEqual(st, 200)
        self.assertEqual(hub.settings.get()["layout"], nk_extras.default_layout())
        # The dashboard HTML carries the v1.9 hooks.
        from dashboard import DASHBOARD_HTML
        for needle in ('id="nkSetLayout"', 'id="nkCoins"', 'id="nkCompare"', 'id="nkPools"', "function nk19ApplyLayout", 'data-nkf="session_best"', "24h avg"):
            self.assertIn(needle, DASHBOARD_HTML)

    def test_v18_settings_migrate_to_v19_with_backup(self):
        ud = self.dir / "userdata"
        ud.mkdir(parents=True)
        v18 = {"schema_version": 1, "block": {"enabled": True, "payout_addresses": [PAYOUT], "explorer": "mempool"},
               "alerts": {"offline": {"enabled": False, "minutes": 7}}, "lan": {"enabled": False, "port": 8081, "pin": "1234"},
               "display": {"bogus": 1}}
        p = ud / "miner_hearth.json"
        p.write_text(json.dumps(v18), encoding="utf-8")
        store = miner_hearth.SettingsStore(p)
        s = store.get()
        self.assertEqual(store.migrated_from, 1)
        self.assertEqual(s["schema_version"], miner_hearth.SETTINGS_SCHEMA)
        self.assertEqual(s["block"]["payout_addresses"], [PAYOUT])
        self.assertFalse(s["alerts"]["offline"]["enabled"])
        self.assertEqual(s["alerts"]["offline"]["minutes"], 7)
        self.assertEqual(s["lan"]["pin"], "1234")
        self.assertFalse(s["webhooks"]["enabled"])
        self.assertFalse(s["restarts"]["weekly"]["enabled"])
        self.assertFalse(s["restarts"]["hung"]["enabled"])
        self.assertFalse(s["quiet"]["enabled"])
        self.assertEqual(s["ui"], {"theme": "dark", "temp_unit": "C", "background": True})
        self.assertEqual(s["layout"], nk_extras.default_layout())
        self.assertEqual(s["coins"]["overrides"], {})
        bak = ud / "miner_hearth.json.v1.bak"
        self.assertEqual(json.loads(bak.read_text(encoding="utf-8")), v18)
        self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["schema_version"], miner_hearth.SETTINGS_SCHEMA)
        # Second start: no migration, backup untouched.
        store2 = miner_hearth.SettingsStore(p)
        self.assertIsNone(store2.migrated_from)
        self.assertEqual(json.loads(bak.read_text(encoding="utf-8")), v18)

    def test_ambient_from_axeos_and_antminer_inlet(self):
        self.assertEqual(autotune._antminer_intake({"temp_pcb1": "30-32-50-52", "temp_pcb2": "29-31-49-50"}), (29.0, "board inlet sensor"))
        self.assertEqual(autotune._antminer_intake({"Ambient Temp": 24}), (24.0, "miner"))
        self.assertEqual(autotune._antminer_intake({}), (None, ""))
        self.assertIsNone(autotune._ambient_from({"ambientTemp": 150}))
        hub = self._hub19()
        hub.on_poll(gamma_cfg(name="G"), reading(ambient_temp=21.5, ambient_source="miner"), 1_700_000_000.0)
        self.assertEqual(hub.ambient["G"]["c"], 21.5)

    def test_rename_moves_v19_state(self):
        hub = self._hub19()
        t = 1_700_000_000.0
        hub.settings.update({"coins": {"overrides": {"Old": "BCH"}}})
        hub.session_best.update("Old", 5.0, 100, [], t)
        hub.restarts.record("Old", {"kind": "hung"}, t)
        hub.on_rename("Old", "New")
        self.assertEqual(hub.settings.get()["coins"]["overrides"], {"New": "BCH"})
        self.assertIsNotNone(hub.session_best.get("New"))
        self.assertIn("New", hub.restarts.history)


# The v1.9 class inherits helpers from the v1.8 class; do not run the v1.8 tests twice.
for _name in [n for n in dir(V18MinerHearthTests) if n.startswith("test_")]:
    if _name not in V19MinerHearthTests.__dict__:
        setattr(V19MinerHearthTests, _name, None)


# ---------------------------------------------------------------------------
# v1.10 Miner Hearth: miner faults, explainers, Clear/Reset, logs, diagnostics, Details
# ---------------------------------------------------------------------------
import braiins_api  # noqa: E402
import nk_faults  # noqa: E402
from mock_server import SAMPLE_AXEOS_LOG, SAMPLE_BITMAIN_LOG, BitmainWebState, MockBitmainWeb  # noqa: E402


class V110FaultTests(V18MinerHearthTests):
    """v1.10 features. Inherits the v1.8 helpers; earlier tests are not re-run here."""

    def _mock(self, **kw):
        m = MockMiner(MinerState(**kw)).start()
        self.mocks.append(m)
        return m

    def _gamma_stack(self, mock, **dev):
        d = {"name": "G", "ip": "127.0.0.1", "port": mock.port, "type": "gamma", "enabled": False}
        d.update(dev)
        ud, gcfg, shared, tuner, hub = self._stack([d])
        hub.webhooks = nk_extras.Webhooks(RecordingPoster(), async_send=False)
        return ud, gcfg, shared, tuner, hub

    def _poll(self, tuner, name):
        return tuner.poll_device(tuner.states[name])

    # --- parsing per machine ---------------------------------------------------
    def test_axeos_fault_fields_are_parsed_and_explained(self):
        m = self._mock(name="g", kind="gamma", overheat_mode=1, power_fault="VIN under voltage", hardware_fault="Fan Control Failed (Auto)",
                       reset_reason="Reset due to brownout", nominal_voltage=5.0, input_voltage_mv=4600)
        cfg = gamma_cfg(name="G", ip="127.0.0.1", port=m.port)
        r = autotune.fetch_reading(cfg)
        self.assertEqual(r.fault_raw["overheat_mode"], 1)
        self.assertEqual(r.fault_raw["power_fault"], "VIN under voltage")
        faults = nk_faults.collect_faults(r.fault_raw, uptime_sec=600)
        keys = {f["key"] for f in faults}
        self.assertTrue({"axeos:overheat_mode", "axeos:power_fault", "axeos:hardware_fault:fan", "axeos:low_voltage", "reset:brownout"} <= keys, keys)
        for f in faults:
            self.assertTrue(f["mapped"])
            self.assertTrue(f["meaning"] and f["checks"] and f["guide_url"].startswith("https://"), f["key"])
        self.assertEqual(faults[0]["severity"], "problem")  # problems sort first
        pf = next(f for f in faults if f["key"] == "axeos:power_fault")
        self.assertEqual(pf["text"], "VIN under voltage")  # the miner's own words, verbatim
        # A healthy Gamma reports overheat_mode 0 and no power/hardware fault -> nothing shown.
        healthy = self._mock(name="h", kind="gamma")
        r2 = autotune.fetch_reading(gamma_cfg(name="H", ip="127.0.0.1", port=healthy.port))
        self.assertEqual(nk_faults.collect_faults(r2.fault_raw), [])
        # Reset reasons only show while the uptime is short and only when abnormal.
        self.assertEqual(nk_faults.collect_faults({"resetReason": "Reset due to brownout"}, uptime_sec=86400), [])
        self.assertEqual(nk_faults.collect_faults({"resetReason": "Power-on reset"}, uptime_sec=60), [])

    def test_nerd_board_error_shutdown_and_reset_reason(self):
        m = self._mock(name="n", kind="nerdoctaxe", asic_temps=[50, 51], board_error=3, shutdown=True, last_reset_reason="RESET_BROWNOUT")
        cfg = autotune.build_device_config({"name": "N", "ip": "127.0.0.1", "port": m.port, "type": "nerdoctaxe"}, {})
        extra = autotune.Tuner.probe_extra_faults.__get__(type("T", (), {"_cfg": lambda self, n: cfg})())("N", "nerd")
        self.assertEqual(extra["boardError"], 3)
        self.assertTrue(extra["shutdown"])
        self.assertEqual(extra["lastResetReason"], "RESET_BROWNOUT")
        faults = nk_faults.collect_faults(extra, uptime_sec=100)
        f = next(x for x in faults if x["key"] == "nerd:board_error:3")
        self.assertEqual(f["title"], "Power supply fault")
        self.assertIn("PSU FAULT", f["text"])
        self.assertIn("XT60", " ".join(f["checks"]))
        self.assertNotIn("nerd:shutdown", {x["key"] for x in faults})  # the board error explains the shutdown
        self.assertIn("reset:brownout", {x["key"] for x in faults})
        # v2 dashboard parse path carries the flags too; every documented code maps.
        rd = autotune.parse_nerd_v2_dashboard({"system": {"uptime": 5, "boardError": 4, "shutdown": True}, "thermal": {"asicTemp": 50}, "performance": {}})
        self.assertEqual(rd.fault_raw["boardError"], 4)
        for code, title in ((1, "Miner overheated"), (2, "Voltage regulator overheated"), (4, "Over-current protection"), (5, "Voltage protection"), (6, "Coinbase verify failed")):
            self.assertEqual(nk_faults.collect_faults({"boardError": code})[0]["title"], title)
        unknown = nk_faults.collect_faults({"boardError": 9})[0]
        self.assertFalse(unknown["mapped"])
        self.assertTrue(unknown["title"].startswith("Unmapped"))
        self.assertEqual([x["key"] for x in nk_faults.collect_faults({"shutdown": True, "boardError": 0})], ["nerd:shutdown"])

    def test_stock_antminer_chain_and_fan_faults(self):
        st = CGMinerState(chain_acn=[114, 114, 0], chain_acs=[" " + "o" * 112 + "xx", " " + "o" * 114, ""], fans=[6100, 0, 6050, 6080])
        ant = MockCGMiner(st).start()
        self.mocks.append(ant)
        cfg = autotune.build_device_config({"name": "S", "ip": "127.0.0.1", "type": "antminer", "cgminer_port": ant.port}, {})
        r = autotune.fetch_reading(cfg)
        self.assertEqual([c["acn"] for c in r.fault_raw["chains"]], [114, 114, 0])
        faults = nk_faults.collect_faults(r.fault_raw)
        by = {(f["key"], f["code"]) for f in faults}
        self.assertIn(("bitmain:chain_missing", "chain3"), by)
        self.assertIn(("bitmain:chain_bad_chips", "chain1"), by)
        self.assertIn(("bitmain:fan_lost", "fan2"), by)
        # Healthy stock S19: no chain data problems, all fans spinning -> no faults.
        ok = MockCGMiner(CGMinerState(chain_acn=[114, 114, 114])).start()
        self.mocks.append(ok)
        r2 = autotune.fetch_reading(autotune.build_device_config({"name": "S2", "ip": "127.0.0.1", "type": "antminer", "cgminer_port": ok.port}, {}))
        self.assertEqual(nk_faults.collect_faults(r2.fault_raw), [])
        # Immersion / fanless (all zero) is not "fan lost".
        self.assertEqual(nk_faults.collect_faults({"fans": [0, 0, 0, 0]}), [])
        # Raw fields for the Details panel come straight from the API (key names kept).
        self.assertIn("stats.chain_acn1", r.raw_fields)
        self.assertIn("stats.fan1", r.raw_fields)
        self.assertNotIn("pools", " ".join(r.raw_fields))  # pool users are not part of the raw view

    def test_braiins_errors_and_status_parse_with_unmapped_codes(self):
        bs = BraiinsMockState(password="pw", miner_status="ThermalPause",
                              errors=[{"message": "Overheat", "error_codes": [{"code": "T1", "name": "Temperature reached DANGEROUS level"}]},
                                      {"code": "ZZ9", "message": "Something new"}])
        rest = MockBraiinsRest(bs).start()
        self.mocks.append(rest)
        errs, status = braiins_api.get_errors("127.0.0.1", "root", "pw", http_port=rest.port, timeout=3)
        self.assertEqual([e["code"] for e in errs], ["T1", "ZZ9"])
        self.assertEqual(status, "ThermalPause")
        faults = nk_faults.collect_faults({"braiins_errors": errs, "braiins_status": status})
        keys = {f["key"]: f for f in faults}
        self.assertTrue(keys["braiins:T1"]["mapped"])
        self.assertIn("Miner is paused", keys["braiins:T1"]["checks"][0])
        self.assertIn("braiins:status:thermal_pause", keys)
        un = keys["braiins:unmapped:zz9"]
        self.assertFalse(un["mapped"])
        self.assertEqual(un["text"], "Something new")
        with self.assertRaises(PermissionError):
            braiins_api.get_errors("127.0.0.1", "root", "wrong", http_port=rest.port, timeout=3)
        # Every code in the Braiins Academy table is explained; nothing extra was invented.
        self.assertEqual(len(nk_faults.BRAIINS_ERROR_CODES), 37)
        self.assertEqual({k.split(":", 1)[1] for k in nk_faults.CATALOG if k.startswith("braiins:") and ":" not in k.split(":", 1)[1]},
                         set(nk_faults.BRAIINS_ERROR_CODES))

    # --- Clear / Reset safety ----------------------------------------------------
    def test_reset_plan_gating_rules(self):
        F = lambda *keys: [{"key": k, "code": k, "severity": "problem"} for k in keys]  # noqa: E731
        base = {"family": "axeos", "online": True, "asic_c": 40.0, "vr_c": 50.0, "asic_max_c": 65, "vr_max_c": 75}
        opts = miner_hearth.default_app_settings()["faults"]
        t = 1_700_000_000.0
        p = nk_faults.reset_plan(F("axeos:overheat_mode"), base, opts, [], t)
        self.assertEqual((p["action"], p["enabled"]), ("axe_clear_overheat", True))
        hot = nk_faults.reset_plan(F("axeos:overheat_mode"), dict(base, asic_c=50.0), opts, [], t)
        self.assertFalse(hot["enabled"])
        self.assertIn("Still hot", hot["reason"])
        self.assertFalse(nk_faults.reset_plan(F("axeos:overheat_mode"), dict(base, vr_c=70.0), opts, [], t)["enabled"])
        self.assertFalse(nk_faults.reset_plan(F("axeos:overheat_mode"), dict(base, asic_c=None, vr_c=None), opts, [], t)["enabled"])
        off = nk_faults.reset_plan(F("axeos:power_fault"), dict(base, online=False), opts, [], t)
        self.assertEqual((off["action"], off["enabled"], off["reason"]), ("restart", False, "Miner is offline"))
        self.assertTrue(nk_faults.reset_plan(F("axeos:power_fault"), dict(base, asic_c=60.0), opts, [], t)["enabled"])  # restart: only blocked at max
        self.assertFalse(nk_faults.reset_plan(F("axeos:power_fault"), dict(base, asic_c=66.0), opts, [], t)["enabled"])
        rl = nk_faults.reset_plan(F("axeos:power_fault"), base, opts, [t - 120], t)
        self.assertIn("Rate limit", rl["reason"])
        many = nk_faults.reset_plan(F("axeos:power_fault"), base, opts, [t - 7200 * i for i in range(1, 5)], t)
        self.assertIn("4 resets", many["reason"])
        self.assertEqual(nk_faults.reset_plan(F("nerd:board_error:3"), dict(base, family="nerd"), opts, [], t)["action"], "restart")
        br = dict(base, family="braiins", has_bos_password=True, asic_c=40.0, vr_c=None)
        self.assertEqual(nk_faults.reset_plan(F("braiins:status:thermal_pause"), br, opts, [], t)["action"], "braiins_resume")
        self.assertEqual(nk_faults.reset_plan(F("braiins:V2"), br, opts, [], t)["action"], "braiins_restart")
        self.assertFalse(nk_faults.reset_plan(F("braiins:V2"), dict(br, has_bos_password=False), opts, [], t)["enabled"])
        bm = dict(base, family="bitmain")
        nopw = nk_faults.reset_plan(F("bitmain:chain_missing"), bm, opts, [], t)
        self.assertEqual((nopw["action"], nopw["enabled"]), ("bitmain_reboot", False))
        self.assertIn("web login", nopw["reason"])
        self.assertTrue(nk_faults.reset_plan(F("bitmain:chain_missing"), dict(bm, has_web_password=True), opts, [], t)["enabled"])
        self.assertFalse(nk_faults.reset_plan([], base, opts, [], t)["available"])
        self.assertFalse(nk_faults.reset_plan(F("braiins:V2"), dict(base, family="other"), opts, [], t)["available"])
        # Never a factory / config reset anywhere.
        self.assertNotIn("factory", " ".join(nk_faults.ALLOWED_ACTIONS))
        for mod in ("nk_faults.py", "autotune.py", "miner_hearth.py", "braiins_api.py"):
            src = (ROOT / mod).read_text(encoding="utf-8")
            self.assertNotIn("actions/factory-reset", src, mod)
            self.assertNotIn("reset_conf.cgi", src, mod)
            self.assertNotIn("FactoryReset", src, mod)

    def test_clear_overheat_end_to_end_confirm_dry_run_and_logging(self):
        m = self._mock(name="g", kind="gamma", overheat_mode=1, temp=40.0, vr_temp=50.0)
        _ud, _g, shared, tuner, hub = self._gamma_stack(m)
        self._poll(tuner, "G")
        st, data, _ = self._req(shared, "GET", "/api/status")
        fx = data["nk"]["devices"]["G"]["faults"]
        self.assertEqual(fx["active"][0]["key"], "axeos:overheat_mode")
        self.assertEqual((fx["reset"]["action"], fx["reset"]["enabled"]), ("axe_clear_overheat", True))
        self.assertEqual([e["event"] for e in hub.fault_history.list("G")], ["appeared"])
        # No confirmation -> refused; LAN read-only -> refused.
        st, res, _ = self._req(shared, "POST", "/api/nk/clear-fault", {"device": "G", "action": "axe_clear_overheat"})
        self.assertEqual(st, 400)
        st, res, _ = self._req(shared, "POST", "/api/nk/clear-fault", {"device": "G", "action": "axe_clear_overheat", "confirm": True}, ip="192.168.1.9")
        self.assertEqual(st, 403)
        # Dry-run: nothing is sent, but it is logged.
        shared.set_dry_run(True)
        st, res, _ = self._req(shared, "POST", "/api/nk/clear-fault", {"device": "G", "action": "axe_clear_overheat", "confirm": True})
        self.assertEqual(st, 200)
        self.assertTrue(res["dry_run"])
        self.assertIn("dry-run: would PATCH", res["result"])
        self.assertEqual(m.state.overheat_mode, 1)
        # Real run is rate limited only by real (non-dry) resets.
        shared.set_dry_run(False)
        st, res, _ = self._req(shared, "POST", "/api/nk/clear-fault", {"device": "G", "action": "axe_clear_overheat", "confirm": True})
        self.assertEqual(st, 200, res)
        self.assertEqual(m.state.overheat_mode, 0)
        self.assertIn({"path": "/api/system", "body": {"overheat_mode": 0}}, m.state.patches)
        ev = [e["event"] for e in hub.fault_history.list("G")]
        self.assertEqual(ev[:2], ["user_reset", "user_reset"])
        tl = hub.tuning_log.list("G")
        self.assertEqual(tl[0]["source"], "user")
        self.assertEqual(tl[0]["kind"], "fault_reset")
        # Next poll: the flag is gone -> "cleared"; nothing left to reset.
        self._poll(tuner, "G")
        self.assertEqual(hub.fault_history.list("G")[0]["event"], "cleared")
        st, res, _ = self._req(shared, "POST", "/api/nk/clear-fault", {"device": "G", "confirm": True})
        self.assertEqual(st, 409)
        # Hot overheat flag: blocked with a reason.
        m.state.overheat_mode = 1
        m.state.temp = 58.0
        self._poll(tuner, "G")
        st, res, _ = self._req(shared, "POST", "/api/nk/clear-fault", {"device": "G", "action": "axe_clear_overheat", "confirm": True})
        self.assertEqual(st, 409)
        self.assertIn("Still hot", res["error"])
        self.assertEqual(m.state.overheat_mode, 1)

    def test_power_fault_restart_and_nerd_restart_respects_otp(self):
        m = self._mock(name="g", kind="gamma", power_fault="VIN under voltage", temp=50.0, vr_temp=55.0)
        _ud, _g, shared, tuner, hub = self._gamma_stack(m)
        self._poll(tuner, "G")
        st, res, _ = self._req(shared, "POST", "/api/nk/clear-fault", {"device": "G", "action": "restart", "confirm": True})
        self.assertEqual(st, 200, res)
        self.assertEqual(m.state.restarts, 1)
        # Nerd with OTP: the configured TOTP secret is used (wrong/absent -> failure is reported, not hidden).
        n = self._mock(name="n", kind="nerdoctaxe", asic_temps=[50, 51], board_error=3, shutdown=True, require_otp=True, expected_session="sess-1")
        _ud, _g, shared2, tuner2, hub2 = self._stack([{"name": "N", "ip": "127.0.0.1", "port": n.port, "type": "nerdoctaxe", "enabled": False, "otp_session": "sess-1"}])
        hub2.fault_probe["N"] = {"ts": hub2.clock(), "data": tuner2.probe_extra_faults("N", "nerd")}
        self._poll(tuner2, "N")
        st, res, _ = self._req(shared2, "POST", "/api/nk/clear-fault", {"device": "N", "action": "restart", "confirm": True})
        self.assertEqual(st, 200, res)
        self.assertEqual(n.state.restarts, 1)

    def test_bitmain_digest_reboot_only_with_saved_login(self):
        st = CGMinerState(chain_acn=[114, 114, 0])
        ant = MockCGMiner(st).start()
        web = MockBitmainWeb(BitmainWebState(password="example-pass", log_text=SAMPLE_BITMAIN_LOG)).start()
        self.mocks += [ant, web]
        _ud, _g, shared, tuner, hub = self._stack([{"name": "S", "ip": "127.0.0.1", "type": "antminer", "cgminer_port": ant.port, "port": web.port}])
        self._poll(tuner, "S")
        _, data, _ = self._req(shared, "GET", "/api/status")
        r = data["nk"]["devices"]["S"]["faults"]["reset"]
        self.assertEqual((r["action"], r["enabled"]), ("bitmain_reboot", False))
        st_, res, _ = self._req(shared, "POST", "/api/nk/clear-fault", {"device": "S", "action": "bitmain_reboot", "confirm": True})
        self.assertEqual(st_, 409)
        self.assertEqual(web.state.reboots, 0)
        # Saving the login is host-PC only.
        st_, res, _ = self._req(shared, "POST", "/api/nk/web-login", {"device": "S", "web_username": "root", "web_password": "example-pass"}, ip="192.168.1.9")
        self.assertEqual(st_, 403)
        st_, res, _ = self._req(shared, "POST", "/api/nk/web-login", {"device": "S", "web_username": "root", "web_password": "example-pass"})
        self.assertEqual((st_, res["web_has_password"]), (200, True))
        _, data, _ = self._req(shared, "GET", "/api/status")
        self.assertNotIn("example-pass", json.dumps(data))
        st_, res, _ = self._req(shared, "POST", "/api/nk/clear-fault", {"device": "S", "action": "bitmain_reboot", "confirm": True})
        self.assertEqual(st_, 200, res)
        self.assertEqual(web.state.reboots, 1)
        # Log capture over Digest, parsed into explained findings.
        st_, res, _ = self._req(shared, "POST", "/api/nk/log-snapshot", {"device": "S"})
        self.assertEqual(st_, 200, res)
        keys = {f["key"] for f in res["findings"]}
        self.assertTrue({"bitmain:log:find_0_asic", "bitmain:log:power_off_board", "bitmain:log:fan_lost"} <= keys, keys)
        self.assertTrue((hub.userdata_dir / "log_snapshots" / "S.txt").exists())
        # Password stays out of backups unless asked for.
        bk = miner_hearth.build_backup(tuner, hub.settings.get())
        self.assertNotIn("example-pass", json.dumps(bk))
        self.assertIn("example-pass", json.dumps(miner_hearth.build_backup(tuner, hub.settings.get(), include_passwords=True)))
        # Wrong login -> clear message; SHA-256 digest also works.
        tuner.update_web_login("S", {"web_password": "nope"})
        st_, res, _ = self._req(shared, "POST", "/api/nk/log-snapshot", {"device": "S"})
        self.assertEqual(st_, 502)
        self.assertIn("login rejected", res["error"])
        web2 = MockBitmainWeb(BitmainWebState(password="pw2", algorithm="SHA-256", log_text="x")).start()
        self.mocks.append(web2)
        code, body = nk_faults.digest_request("GET", f"http://127.0.0.1:{web2.port}/cgi-bin/log.cgi", "root", "pw2", timeout=3)
        self.assertEqual((code, body), (200, b"x"))

    def test_log_snapshot_axeos_and_braiins(self):
        m = self._mock(name="g", kind="gamma", logs_text=SAMPLE_AXEOS_LOG)
        _ud, _g, shared, tuner, hub = self._gamma_stack(m)
        self._poll(tuner, "G")
        st, res, _ = self._req(shared, "POST", "/api/nk/log-snapshot", {"device": "G"})
        self.assertEqual(st, 200, res)
        keys = [f["key"] for f in res["findings"]]
        self.assertIn("axeos:log:asic_not_found", keys)
        self.assertIn("axeos:log:high_temp", keys)
        self.assertEqual(hub.fault_history.list("G")[0]["event"], "log_snapshot")
        # Firmware without the endpoint -> a clear "not available" message.
        m.state.logs_text = None
        st, res, _ = self._req(shared, "POST", "/api/nk/log-snapshot", {"device": "G"})
        self.assertEqual(st, 502)
        self.assertIn("does not offer", res["error"])
        bs = BraiinsMockState(password="pw", log_text="2026-01-01 ERROR: error code V2: Overcurrent protection triggered\n")
        rest = MockBraiinsRest(bs).start()
        self.mocks.append(rest)
        txt = braiins_api.get_log("127.0.0.1", "root", "pw", http_port=rest.port)
        self.assertEqual([f["key"] for f in nk_faults.parse_log_lines(txt)], ["braiins:V2"])
        # Example lines are scrubbed (IP last octet, Wi-Fi name stays only if not personal key -> text scrub masks IPs).
        f = nk_faults.parse_log_lines("E (1) x: ASIC 0 not found at 192.168.1.50 mac 02:00:00:00:00:09")[0]
        self.assertNotIn("192.168.1.50", f["examples"][0])
        self.assertNotIn("02:00:00:00:00:09", f["examples"][0])

    def test_braiins_resume_and_probe_via_hub(self):
        bs = BraiinsMockState(password="pw", miner_status="ThermalPause",
                              errors=[{"message": "Overheat", "error_codes": [{"code": "T2", "name": "Temperature reached DANGEROUS level"}]}])
        rest = MockBraiinsRest(bs).start()
        ant = MockCGMiner(CGMinerState(firmware_kind="braiins-plus", chip_temps=[40.0, 41.0, 40.0], board_temps=[35.0, 36.0, 35.0])).start()
        self.mocks += [rest, ant]
        _ud, _g, shared, tuner, hub = self._stack([{"name": "B", "ip": "127.0.0.1", "type": "antminer", "cgminer_port": ant.port, "port": rest.port,
                                                     "bos_password": "pw", "bos_grpc_port": 1}])
        self._poll(tuner, "B")
        hub.probe_faults(hub.clock())
        self.assertEqual(hub.fault_probe["B"]["error"], "")
        self._poll(tuner, "B")
        _, data, _ = self._req(shared, "GET", "/api/status")
        fx = data["nk"]["devices"]["B"]["faults"]
        self.assertEqual(fx["family"], "braiins")
        self.assertIn("braiins:T2", {f["key"] for f in fx["active"]})
        self.assertEqual((fx["reset"]["action"], fx["reset"]["enabled"]), ("braiins_resume", True), fx["reset"])
        st, res, _ = self._req(shared, "POST", "/api/nk/clear-fault", {"device": "B", "action": "braiins_resume", "confirm": True})
        self.assertEqual(st, 200, res)
        self.assertEqual(bs.actions, ["resume"])
        self.assertIn("rest", res["result"])

    # --- history, alerts, webhooks -------------------------------------------------
    def test_fault_history_alerts_and_webhook_off_by_default(self):
        poster = RecordingPoster()
        hub = self._hub()
        hub.webhooks = nk_extras.Webhooks(poster, async_send=False)
        hub.settings.update({"webhooks": {"enabled": True, "discord_url": "https://discord.example/api/webhooks/1/x"}})
        self.assertFalse(hub.settings.get()["webhooks"]["types"]["hardware_fault"])
        cfg = gamma_cfg(name="G")
        t = 1_700_000_000.0
        evs = hub.on_poll(cfg, reading(fault_raw={"power_fault": "VIN under voltage"}), t)
        hf = [e for e in evs if e["kind"] == "hardware_fault"]
        self.assertEqual(len(hf), 1)
        self.assertEqual(hf[0]["level"], "problem")
        self.assertEqual(poster.calls, [])  # webhook kind is off by default
        hub.on_poll(cfg, reading(fault_raw={"power_fault": "VIN under voltage"}), t + 30)  # still active: no repeat
        hub.on_poll(cfg, reading(ok=False, reachable=False), t + 60)  # offline keeps the fault
        self.assertEqual(len(hub.fault_history.list("G")), 1)
        evs = hub.on_poll(cfg, reading(fault_raw={}), t + 90)
        self.assertEqual([e["event"] for e in hub.fault_history.list("G")], ["cleared", "appeared"])
        self.assertEqual(hub.fault_history.list("G")[0]["duration_sec"], 90)
        # Turn the webhook kind on: the next new fault is delivered.
        hub.settings.update({"webhooks": {"types": {"hardware_fault": True}}})
        hub.on_poll(cfg, reading(fault_raw={"overheat_mode": 1}), t + 120)
        self.assertEqual(len(poster.calls), 1)
        # Persisted across restarts (active set too: no duplicate "appeared").
        h2 = miner_hearth.FeatureHub(self.dir / "userdata", fetch=FakeEsplora(), clock=lambda: t + 200)
        self.hubs.append(h2)
        self.assertEqual(len(h2.fault_history.list("G")), 3)
        h2.on_poll(cfg, reading(fault_raw={"overheat_mode": 1}), t + 200)
        self.assertEqual(len(h2.fault_history.list("G")), 3)
        h2.on_rename("G", "G2")
        self.assertEqual(h2.fault_history.list("G2")[0]["device"], "G2")
        # Faults can be switched off entirely.
        hub.settings.update({"faults": {"enabled": False}})
        self.assertEqual(hub.on_poll(cfg, reading(fault_raw={"power_fault": "x"}), t + 300), [])

    # --- diagnostics / redaction ---------------------------------------------------
    def test_diagnostics_export_is_redacted(self):
        m = self._mock(name="g", kind="gamma", overheat_mode=1, stratum_user="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4.rig")
        m.state.custom_info = None
        _ud, _g, shared, tuner, hub = self._gamma_stack(m, totp_secret="JBSWY3DPEHPK3PXP")
        hub.settings.update({"lan": {"pin": "4321"}, "webhooks": {"telegram_token": "123456:abcdefghijklmnop"}})
        self._poll(tuner, "G")
        shared.update_device("G", {"note": "mac 02:00:00:00:00:01 at 192.168.1.77", "macAddr": "02:00:00:00:00:01", "ssid": "ExampleNet"})
        st, body, _ = self._req(shared, "GET", "/api/nk/diagnostics?device=G")
        self.assertEqual(st, 200)
        txt = json.dumps(body)
        for secret in ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "02:00:00:00:00:01", "ExampleNet", "4321", "abcdefghijklmnop", "JBSWY3DPEHPK3PXP", "192.168.1.77"):
            self.assertNotIn(secret, txt, secret)
        self.assertIn("192.168.1.x", txt)
        self.assertEqual(body["devices"][0]["faults"][0]["key"], "axeos:overheat_mode")
        self.assertEqual(body["app_version"], "1.17.1")
        self.assertEqual(nk_faults.redact({"user": "a", "list": [{"password": "p"}], "bestSessionDiff": 5}),
                         {"user": "[redacted]", "list": [{"password": "[redacted]"}], "bestSessionDiff": 5})

    # --- migration -------------------------------------------------------------------
    def test_v19_settings_migrate_to_v110_with_backup(self):
        ud = self.dir / "userdata"
        ud.mkdir(parents=True)
        v19 = {"schema_version": 2, "alerts": {"offline": {"enabled": False, "minutes": 5}},
               "webhooks": {"enabled": True, "discord_url": "https://discord.example/x", "types": {"offline": True, "restart": False}},
               "restarts": {"hung": {"enabled": True, "minutes": 20}}, "ui": {"theme": "light", "temp_unit": "F"}}
        p = ud / "miner_hearth.json"
        p.write_text(json.dumps(v19), encoding="utf-8")
        store = miner_hearth.SettingsStore(p)
        s = store.get()
        self.assertEqual(store.migrated_from, 2)
        self.assertEqual(s["schema_version"], 4)
        self.assertEqual(json.loads((ud / "miner_hearth.json.v2.bak").read_text(encoding="utf-8")), v19)
        self.assertFalse(s["alerts"]["offline"]["enabled"])
        self.assertTrue(s["webhooks"]["enabled"])
        self.assertFalse(s["webhooks"]["types"]["restart"])
        self.assertFalse(s["webhooks"]["types"]["hardware_fault"])  # new webhook kind is off
        self.assertTrue(s["alerts"]["hardware_fault"]["enabled"])
        self.assertTrue(s["restarts"]["hung"]["enabled"])
        self.assertEqual(s["ui"]["temp_unit"], "F")
        self.assertEqual(s["faults"], {"enabled": True, "probe_enabled": True, "cool_asic_c": 45.0, "cool_vr_c": 65.0, "min_gap_min": 10.0, "max_per_day": 4, "record_hours": 12.0})
        self.assertEqual(s["layout"]["card_fields"][0], "faults")
        self.assertIn("fault_history", s["layout"]["sections"])
        # Clamps
        n = miner_hearth.normalize_app_settings({"faults": {"cool_asic_c": 500, "max_per_day": 0, "min_gap_min": 1}})
        self.assertEqual((n["faults"]["cool_asic_c"], n["faults"]["max_per_day"], n["faults"]["min_gap_min"]), (70.0, 1, 5.0))

    # --- Details panel / UI -------------------------------------------------------------
    def test_details_raw_fields_and_dashboard_hooks(self):
        m = self._mock(name="g", kind="gamma", hexos_mode=True)
        r = autotune.fetch_reading(gamma_cfg(name="G", ip="127.0.0.1", port=m.port))
        rf = r.raw_fields
        for k in ("info.temp", "info.vrTemp", "info.boardTemp", "info.voltage", "info.fanrpm", "info.frequency", "info.coreVoltageActual"):
            self.assertIn(k, rf)
        self.assertFalse([k for k in rf if "stratumUser" in k or "ssid" in k.lower() or "mac" in k.lower()])
        flat = nk_faults.flatten_fields({"a": {"b": [1, 2, 3], "ssid": "x", "c": [{"d": "pool bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"}]}})
        self.assertEqual(flat["a.b"], [1, 2, 3])
        self.assertNotIn("a.ssid", flat)
        self.assertEqual(flat["a.c[0].d"], "pool [address]")
        import dashboard
        html = dashboard.DASHBOARD_HTML
        for hook in ("nk110OpenDetails", "nk110Modal", "nkFaultPanel", "nkSetFaults", "nk110SaveSettings", "All fields", "Capture log", "Diagnostics"):
            self.assertIn(hook, html)
        self.assertEqual(html.count('typeof nk110Card === "function" ? nk110Card(d)'), 2)
        self.assertEqual(html.count('typeof nk110DetailsBtn === "function" ? nk110DetailsBtn(d)'), 2)
        keys = {d["key"] for d in autotune.DISPLAY_FIELD_DEFS}
        self.assertTrue({"faults", "details_button", "fault_history"} <= keys)
        self.assertIn("hardware_fault", nk_extras.WEBHOOK_TYPES)
        self.assertNotIn("hardware_fault", nk_extras.WEBHOOK_DEFAULT_ON)

    def test_status_snapshot_publishes_fault_and_component_fields(self):
        m = self._mock(name="g", kind="gamma", hardware_fault="Fan Control Failed (Auto)", hexos_mode=True)
        _ud, _g, shared, tuner, hub = self._gamma_stack(m)
        self._poll(tuner, "G")
        _, data, _ = self._req(shared, "GET", "/api/status")
        d = next(x for x in data["devices"] if x["name"] == "G")
        self.assertEqual(d["fault_raw"]["hardware_fault"], "Fan Control Failed (Auto)")
        self.assertIn("info.boardTemp", d["raw_fields"])
        self.assertEqual(d["input_voltage"], 5080)
        f = data["nk"]["devices"]["G"]["faults"]
        self.assertEqual(f["active"][0]["title"], "Fan control failed")
        self.assertEqual(f["reset"]["action"], "restart")
        self.assertTrue(data["nk"]["fault_history"])


for _name in [n for n in dir(V18MinerHearthTests) if n.startswith("test_")]:
    if _name not in V110FaultTests.__dict__:
        setattr(V110FaultTests, _name, None)


# ---------------------------------------------------------------------------
# v1.11 Miner Hearth: comprehensive firmware-alert simulation matrix
# ---------------------------------------------------------------------------
class V111FaultSimulationTests(V110FaultTests):
    """End-to-end fault simulation across AxeOS, NerdQAxe+, Stock Bitmain, and Braiins OS+."""

    def _run_device_poll(self, devices_dicts, target_name, run_probe=False, clock_val=1795260000.0):
        ud, gcfg, shared, tuner, hub = self._stack(devices_dicts)
        hub.clock = lambda: clock_val
        tuner.clock = lambda: clock_val
        self._poll(tuner, target_name)
        if run_probe:
            hub.probe_faults(clock_val)
            self._poll(tuner, target_name)
        _, status = self._req(shared, "GET", "/api/status")[:2]
        fx = (status.get("nk") or {}).get("devices", {}).get(target_name, {}).get("faults") or {}
        return fx, list(hub.events), shared, tuner, hub

    @staticmethod
    def _axeos_base(**kw):
        d = {
            "power": 18.0, "voltage": 5100, "nominalVoltage": 5, "current": 3500,
            "temp": 55.0, "temp2": 0, "vrTemp": 60.0, "hashRate": 1100.0,
            "expectedHashrate": 1100.0, "frequency": 525, "actualFrequency": 525,
            "coreVoltage": 1200, "coreVoltageActual": 1200, "fanspeed": 70.0, "fanrpm": 4200,
            "sharesAccepted": 100, "sharesRejected": 0, "errorPercentage": 0.1,
            "uptimeSeconds": 1000, "wifiStatus": "Connected!", "wifiRSSI": -55,
            "poolConnectionInfo": "stratum+tcp://public-pool.io:21496",
            "isUsingFallbackStratum": 0, "useFallbackStratum": 0,
            "overheat_mode": 0, "miningPaused": False, "version": "v2.15.4",
            "deviceModel": "Gamma", "ASICModel": "BM1370",
        }
        d.update(kw)
        return d

    def test_axeos_ui_and_firmware_fault_matrix(self):
        # Healthy baseline produces zero faults
        m_ok = self._mock(name="g-ok", kind="gamma", custom_info=self._axeos_base())
        fx_ok, ev_ok, *_ = self._run_device_poll([{"name": "G", "ip": "127.0.0.1", "port": m_ok.port, "type": "gamma"}], "G")
        self.assertEqual(fx_ok.get("active"), [])
        self.assertEqual(ev_ok, [])

        scenarios = [
            ("overheat str", {"overheat_mode": "1"}, "axeos:overheat_mode"),
            ("power_fault bool", {"power_fault": True}, "axeos:power_fault"),
            ("voltage sag w/o nominalVoltage", {k: v for k, v in self._axeos_base(voltage=4400).items() if k != "nominalVoltage"}, "axeos:low_voltage"),
            ("chip temp >= 70", {"temp": 73.5}, "axeos:high_temp"),
            ("vrTemp >= 105", {"vrTemp": 107.0}, "axeos:high_vr_temp"),
            ("fan stalled 0 RPM", {"fanrpm": 0, "fanspeed": 80.0}, "axeos:fan_stalled"),
            ("temp sensor -1 while mining", {"temp": -1, "hashRate": 950.0}, "axeos:temp_sensor_error"),
            ("pool disconnected", {"poolConnectionInfo": "Not Connected", "hashRate": 0}, "axeos:pool_disconnected"),
            ("fallback stratum", {"isUsingFallbackStratum": 1, "primaryPoolError": "ECONNREFUSED"}, "axeos:fallback_stratum"),
            ("pool diff low", {"poolDifficulty": 100, "asicDifficulty": 1000}, "axeos:pool_diff_low"),
            ("coinbase not found", {"coinbasePayoutStatus": "not_found", "coinbaseOutputs": [{"value": 100, "address": "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"}]}, "axeos:coinbase_payout"),
            ("wifi failed", {"wifiStatus": "Failed to connect", "wifiRSSI": -88}, "axeos:wifi_disconnected"),
            ("frequency below min", {"frequency": 250, "frequencyOptions": [400, 490, 525, 600]}, "axeos:frequency_low"),
            ("zero hashrate online", {"hashRate": 0.0, "uptimeSeconds": 600}, "axeos:zero_hashrate"),
            ("low hashrate vs expected", {"hashRate": 400.0, "expectedHashrate": 1100.0}, "axeos:low_hashrate"),
            ("dead asic in monitor", {"hashrateMonitor": {"asics": [{"total": 550.0}, {"total": 0.0}]}}, "axeos:dead_asic"),
            ("high errorPercentage", {"errorPercentage": 7.5}, "axeos:high_error_pct"),
        ]
        for idx, (label, patch, expected_key) in enumerate(scenarios):
            payload = patch if "nominalVoltage" not in patch and label.startswith("voltage") else self._axeos_base(**patch)
            m = self._mock(name=f"g{idx}", kind="gamma", custom_info=payload)
            fx, *_ = self._run_device_poll([{"name": "G", "ip": "127.0.0.1", "port": m.port, "type": "gamma"}], "G")
            keys = {f["key"] for f in fx.get("active", [])}
            self.assertIn(expected_key, keys, f"{label}: got {keys}")
            # Ensure wallet addresses from coinbaseOutputs never leak into fault_raw
            self.assertNotIn("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh", json.dumps(fx))

    def test_nerdqaxe_immediate_probe_cross_type_and_tile_alerts(self):
        # Healthy NERD_V10371_INFO (including all-zero asicTemps) must have zero faults
        m_v1 = self._mock(name="axe1-ok", kind="nerdoctaxe", v2_enabled=False, custom_info=dict(NERD_V10371_INFO))
        fx_ok, *_ = self._run_device_poll([{"name": "Axe1", "ip": "127.0.0.1", "port": m_v1.port, "type": "nerdoctaxe"}], "Axe1")
        self.assertEqual(fx_ok.get("active"), [])

        # Immediate first-poll detection of boardError=3 even when user picked type="gamma"
        m_err = self._mock(name="axe1-err", kind="nerdoctaxe", asic_temps=[52, 53, 54, 51], board_error=3, shutdown=True)
        fx_im, ev_im, *_ = self._run_device_poll([{"name": "Axe1", "ip": "127.0.0.1", "port": m_err.port, "type": "gamma"}], "Axe1")
        self.assertIn("nerd:board_error:3", {f["key"] for f in fx_im.get("active", [])})
        self.assertTrue(any(e["kind"] == "hardware_fault" for e in ev_im))

        # Stalled fan (0 RPM at 100% speed), VR critical, partial dead asicTemps
        m_fan = self._mock(name="axe1-fan", kind="nerdoctaxe", asic_temps=[52, 53, 0, 51], vr_temp=116.0, fanrpm=0, fanspeed=100.0)
        fx_fan, *_ = self._run_device_poll([{"name": "Axe1", "ip": "127.0.0.1", "port": m_fan.port, "type": "nerdoctaxe"}], "Axe1")
        k_fan = {f["key"] for f in fx_fan.get("active", [])}
        self.assertTrue({"nerd:fan_stalled", "nerd:vr_temp_high", "nerd:asic_sensor_dead"} <= k_fan, k_fan)

        # Red stratum tile flags & power bounds on /api/system/info
        for label, extra, exp in [
            ("stratum disconnected", {"stratum": {"pools": [{"connected": False, "active": True}]}}, "nerd:stratum_disconnected"),
            ("verifyBlocked", {"stratum": {"pools": [{"connected": True, "verifyBlocked": "address"}]}}, "nerd:verify_blocked"),
            ("poolDiffErr", {"stratum": {"pools": [{"connected": True, "poolDiffErr": True}]}}, "nerd:pool_diff_err"),
            ("voltage out of range", {"voltage": 10500}, "nerd:voltage_out_of_range"),
            ("current high", {"currentA": 24.6, "maxCurrentA": 25}, "nerd:current_high"),
            ("power high", {"power": 290, "maxPower": 300}, "nerd:power_high"),
        ]:
            info = dict(NERD_V10371_INFO)
            info.update(extra)
            m = self._mock(name=label, kind="nerdoctaxe", v2_enabled=False, custom_info=info)
            fx, *_ = self._run_device_poll([{"name": "Axe1", "ip": "127.0.0.1", "port": m.port, "type": "nerdoctaxe"}], "Axe1")
            self.assertIn(exp, {f["key"] for f in fx.get("active", [])}, label)

    def test_stock_antminer_extended_faults_and_web_6060(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        # 4-slot STATS with miner_count=3 and chain_acn4=0 must stay healthy (no false positive)
        cg_ok = MockCGMiner(CGMinerState(chain_acn=[114, 114, 114, 0], miner_count_override=3, board_temps=[55.0, 56.0, 55.0], chip_temps=[65.0, 66.0, 65.0])).start()
        self.mocks.append(cg_ok)
        fx_ok, *_ = self._run_device_poll([{"name": "S19", "ip": "127.0.0.1", "cgminer_port": cg_ok.port, "type": "antminer"}], "S19")
        self.assertEqual(fx_ok.get("active"), [])

        # Slow fan, 0 hashrate online, chip overheat, temp sensor 0, hw%, dead pools, CGMiner warning
        for label, cg_kw, exp in [
            ("slow fan", {"fans": [6100, 400, 6050, 6080]}, "bitmain:fan_slow"),
            ("zero hashrate", {"ghs_5s": 0.0, "ghs_av": 0.0, "elapsed": 600}, "bitmain:zero_hashrate"),
            ("miner_count=2 low rate", {"chain_acn": [114, 114, 0], "miner_count_override": 2, "rate_ideal_ghs": 110000.0, "ghs_5s": 73000.0, "elapsed": 600, "board_temps": [55.0, 56.0], "chip_temps": [65.0, 66.0]}, "bitmain:low_hashrate"),
            ("chip overheat", {"chip_temps": [65.0, 89.0, 66.0]}, "bitmain:temp_high"),
            ("temp sensor 0", {"chip_temps": [65.0, 0.0, 66.0]}, "bitmain:temp_sensor_error"),
            ("hw errors high", {"hw_pct": 4.2}, "bitmain:hw_errors_high"),
            ("all pools dead", {"pools_all_dead": True}, "bitmain:pools_dead"),
            ("cgminer status W", {"summary_status_err": "Temp warning"}, "bitmain:cgminer_error"),
        ]:
            cg = MockCGMiner(CGMinerState(**cg_kw)).start()
            self.mocks.append(cg)
            fx, *_ = self._run_device_poll([{"name": "S19", "ip": "127.0.0.1", "cgminer_port": cg.port, "type": "antminer"}], "S19")
            self.assertIn(exp, {f["key"] for f in fx.get("active", [])}, label)

        # Baseline chain drop across polls (3 -> 2 active chains even when chain_acn3 vanishes)
        cg_st = CGMinerState(chain_acn=[114, 114, 114])
        cg_drop = MockCGMiner(cg_st).start()
        self.mocks.append(cg_drop)
        _ud, _g, shared, tuner, hub = self._stack([{"name": "S19D", "ip": "127.0.0.1", "cgminer_port": cg_drop.port, "type": "antminer"}])
        self._poll(tuner, "S19D")
        with cg_st.lock:
            cg_st.chain_acn = [114, 114]
            cg_st.miner_count_override = 2
        self._poll(tuner, "S19D")
        _, status = self._req(shared, "GET", "/api/status")[:2]
        keys_drop = {f["key"] for f in status["nk"]["devices"]["S19D"]["faults"]["active"]}
        self.assertIn("bitmain:chain_missing", keys_drop)

        # Bitmain Digest /cgi-bin/warning.cgi + summary.cgi + port 6060 /warning
        bws = BitmainWebState(
            password="pw",
            warning_json={"warnings": [{"code": "J0:8", "msg": "Chain 0 chip count low"}]},
            summary_json={"SUMMARY": [{"status": [{"type": "fans", "status": "e", "code": "R:1", "msg": "Fan 2 lost"}]}]},
        )
        bw = MockBitmainWeb(bws).start()
        cg_w = MockCGMiner(CGMinerState()).start()
        self.mocks.extend([bw, cg_w])
        fx_w, *_ = self._run_device_poll([{
            "name": "S19W", "ip": "127.0.0.1", "port": bw.port, "cgminer_port": cg_w.port,
            "type": "antminer", "web_username": "root", "web_password": "pw",
        }], "S19W")
        codes_w = {f["code"] for f in fx_w.get("active", []) if f["key"] == "bitmain:web_warning"}
        self.assertTrue({"J0:8", "R:1"} <= codes_w, codes_w)

        # Port 6060 /warning probe
        class H6060(BaseHTTPRequestHandler):
            def log_message(self, *a): return
            def do_GET(self):
                body = json.dumps({"warning": [{"code": "V:1", "msg": "PSU voltage fault"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        s6060 = ThreadingHTTPServer(("127.0.0.1", 0), H6060)
        threading.Thread(target=s6060.serve_forever, daemon=True).start()
        try:
            _ud, _g, shared6, tuner6, hub6 = self._stack([{"name": "S60", "ip": "127.0.0.1", "cgminer_port": cg_w.port, "type": "antminer"}])
            tuner6._cfg("S60").bitmain_6060_port = s6060.server_address[1]
            self._poll(tuner6, "S60")
            hub6.probe_faults(hub6.clock())
            self._poll(tuner6, "S60")
            _, st6 = self._req(shared6, "GET", "/api/status")[:2]
            acts = st6["nk"]["devices"]["S60"]["faults"]["active"]
            self.assertTrue(any(f["key"] == "bitmain:web_warning" and f["code"] == "V:1" for f in acts), acts)
        finally:
            s6060.shutdown()
            s6060.server_close()

    def test_braiins_nested_status_grpc_fallback_cgminer_and_reboot_loop(self):
        # Nested protobuf-JSON detailed_status via REST
        for payload, exp in [
            ({"stopped": {"thermal_pause": {"reason": "THERMAL_PAUSE_REASON_DANGEROUS_TEMPERATURE"}}}, "braiins:status:thermal_pause"),
            ({"stopped": {"dead_pools": {}}}, "braiins:status:dead_pools"),
            ({"stopped": {"missing_license": {}}}, "braiins:status:missing_license"),
            ({"stopped": {"hardware_error": {"message": "Chain 2 dead"}}}, "braiins:status:hardware_error"),
            ({"stopped": {"tuner_error": {"message": "PSU out of range"}}}, "braiins:status:tuner_error"),
            ({"stopped": {"unsupported_hardware": {"errors": ["Unknown"]}}}, "braiins:status:unsupported_hardware"),
            ({"stopped": {"dps_cooldown": {}}}, "braiins:status:dps_cooldown"),
            ({"stopped": {"user_pause": {}}}, "braiins:status:paused"),
        ]:
            bs = BraiinsMockState(password="pw", miner_status=payload)
            rest = MockBraiinsRest(bs).start()
            cg = MockCGMiner(CGMinerState(firmware_kind="braiins-plus")).start()
            self.mocks.extend([rest, cg])
            fx, *_ = self._run_device_poll([{
                "name": "B", "ip": "127.0.0.1", "port": rest.port, "cgminer_port": cg.port,
                "type": "antminer", "bos_password": "pw", "bos_grpc_port": 1,
            }], "B")
            self.assertIn(exp, {f["key"] for f in fx.get("active", [])}, payload)

        # gRPC fallback when REST port is unreachable
        gs = BraiinsMockState(password="pw", miner_status="thermal_pause",
                              errors=[{"message": "Dangerous temp", "error_codes": [{"code": "T2", "name": "Temp dangerous"}]}])
        grpc = MockBraiinsGrpc(gs).start()
        self.mocks.append(grpc)
        errs, status = braiins_api.get_errors("127.0.0.1", "root", "pw", http_port=1, timeout=2.0, grpc_port=grpc.port)
        self.assertEqual([e["code"] for e in errs], ["T2"])
        self.assertIn("thermal_pause", status)

        # Braiins CGMiner-only (no bos_password): FANS 0/slow, TUNERSTATUS Error, DEVS Dead, TEMPS 0/hot, POOLS Dead
        for label, cg_kw, exp in [
            ("fan lost", {"fans": [6100, 0, 6050, 6080]}, "braiins:cgminer:fan_lost"),
            ("fan slow", {"fans": [6100, 400, 6050, 6080]}, "braiins:cgminer:fan_slow"),
            ("tuner error", {"tuner_chain_statuses": ["Stable", "Error", "Tuning"]}, "braiins:tuner:chain_error"),
            ("dev dead", {"dev_statuses": ["Alive", "Dead", "Alive"]}, "braiins:cgminer:dev_unhealthy"),
            ("temp sensor 0", {"chip_temps": [65.0, 0.0, 66.0]}, "braiins:cgminer:temp_sensor_error"),
            ("temp >= 95", {"chip_temps": [65.0, 97.0, 66.0]}, "braiins:cgminer:temp_high"),
            ("pools dead", {"pools_all_dead": True}, "braiins:cgminer:pools_dead"),
        ]:
            cg = MockCGMiner(CGMinerState(firmware_kind="braiins-plus", **cg_kw)).start()
            self.mocks.append(cg)
            fx, *_ = self._run_device_poll([{"name": "BCG", "ip": "127.0.0.1", "cgminer_port": cg.port, "type": "antminer"}], "BCG")
            self.assertIn(exp, {f["key"] for f in fx.get("active", [])}, label)

        # Repeated reboot loop detection (>= 3 uptime drops within 2 hours)
        hub = self._hub()
        cfg = gamma_cfg(name="LoopG")
        t = 1_700_000_000.0
        for step in range(3):
            hub.on_poll(cfg, reading(uptime_sec=600.0), t + step * 600)
            hub.on_poll(cfg, reading(uptime_sec=30.0), t + step * 600 + 60)
        self.assertIn("reset:loop", {f["key"] for f in hub.faults_now.get("LoopG", [])})

for _name in [n for n in dir(V110FaultTests) if n.startswith("test_")]:
    if _name not in V111FaultSimulationTests.__dict__:
        setattr(V111FaultSimulationTests, _name, None)


# ---------------------------------------------------------------------------
# v1.12 Miner Hearth: safety guards, root-cause diagnostics, pool-user payout,
# Gamma no-tach fan, migration, and 12h rolling pre-fault records
# ---------------------------------------------------------------------------

class V112SafetyAndDiagnosticsTests(V110FaultTests):
    """v1.12 end-to-end tests: pool-user payout, Gamma no-tach fan, OCTAxe rail-off + remembered VR cause,
    component attribution + timestamped causes, safety guards/lockout/migration, and 12h rolling pre-fault records."""

    def test_pool_username_payout_extraction_all_formats_and_redaction(self):
        self.assertEqual(nk_coins.extract_pool_payout_address(f"{PAYOUT}.gamma1", "BTC")["address"], PAYOUT)
        self.assertEqual(nk_coins.extract_pool_payout_address(PAYOUT, "BTC")["address"], PAYOUT)
        self.assertEqual(nk_coins.extract_pool_payout_address(f"{PAYOUT}.rig.01", "BTC")["address"], PAYOUT)
        p2pkh = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
        p2sh = "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"
        self.assertEqual(nk_coins.extract_pool_payout_address(f"{p2pkh}.w1", "BTC")["address"], p2pkh)
        self.assertEqual(nk_coins.extract_pool_payout_address(f"{p2sh}.w1", "BTC")["address"], p2sh)

        raw_bch = BCH_ADDR.split(":", 1)[1]
        self.assertEqual(nk_coins.extract_pool_payout_address(f"{BCH_ADDR}.rig", "BCH")["address"], BCH_ADDR)
        raw_res = nk_coins.extract_pool_payout_address(f"{raw_bch}.rig", "BCH")
        self.assertEqual(raw_res["status"], "pool_user")
        self.assertEqual(nk_coins.address_to_script(raw_res["address"]), nk_coins.address_to_script(BCH_ADDR))

        acct = nk_coins.extract_pool_payout_address("alice_pool_user.worker1", "BTC")
        self.assertEqual(acct["status"], "pool_account_ignored")
        self.assertEqual(acct["address"], "")

        bch = FakeHaskoin(tip=800000)
        btc = FakeEsplora(tip=900000)
        hub = miner_hearth.FeatureHub(self.dir / "userdata", fetch=RoutedFetch(btc, bch), clock=lambda: 1_700_000_000.0)
        hub.settings.update({"block": {"payout_addresses": []}, "coins": {"payout_addresses": {}}})
        hub.network = {"difficulty": NET_DIFF, "height": 900000}
        hub.coin_stats["BCH"] = {"height": 800000, "difficulty": 5.0e11}
        cfg = gamma_cfg(name="BCH-Worker")
        t = 1_700_000_000.0
        r = dict(pool_url="bch.solopool.org", pool_user=f"{raw_bch}.worker1", network_difficulty=5.0e11)
        hub.on_poll(cfg, reading(best_session_diff=1e9, **r), t)
        hub.on_poll(cfg, reading(best_session_diff=6e11, **r), t + 15)
        bch.tip = 800001
        bch.add(800001, t + 5, [OTHER_SCRIPT, BCH_SCRIPT], address=None)
        hub.check_candidates(t + 60)
        self.assertEqual(hub.blocks.candidates[0]["status"], "confirmed")

        from dashboard import SharedState
        shared = SharedState(["BCH-Worker"], self.dir / "state.json")
        shared.update_device("BCH-Worker", {"name": "BCH-Worker", "ip": "192.168.1.50", "pool_user": f"{BCH_ADDR}.worker1"})
        hub.shared = shared
        diag_txt = json.dumps(hub.diagnostics("BCH-Worker"))
        self.assertNotIn(raw_bch, diag_txt)
        self.assertNotIn("192.168.1.50", diag_txt)

    def test_gamma_no_tach_fan_suppressed_until_nonzero_rpm_seen(self):
        info = {
            "ASICModel": "BM1370",
            "hostname": "bitaxe-gamma",
            "temp": 56.0,
            "vrTemp": 59.0,
            "frequency": 525,
            "coreVoltage": 1200,
            "coreVoltageActual": 1195,
            "voltage": 5100,
            "current": 2800,
            "power": 14.3,
            "hashRate": 1050.0,
            "fanspeed": 80.0,
            "fanrpm": 0,
            "sharesAccepted": 10,
            "sharesRejected": 0,
            "uptimeSeconds": 600,
        }
        mock = self._mock(name="GammaNoTach", kind="gamma", custom_info=info)
        _, _, shared, tuner, hub = self._stack([{"name": "GammaNoTach", "ip": "127.0.0.1", "port": mock.port, "type": "gamma", "enabled": False}])
        self._poll(tuner, "GammaNoTach")
        st, payload, _ = self._req(shared, "GET", "/api/status")
        self.assertEqual(st, 200)
        dev = payload["devices"][0]
        self.assertTrue(dev["fans"][0]["no_tach"])
        self.assertIsNone(dev["fans"][0]["rpm"])
        self.assertEqual(payload["nk"]["devices"]["GammaNoTach"]["faults"]["active"], [])

        _, html_b, _ = self._req(shared, "GET", "/")
        self.assertIn("speed % only (no RPM sensor)", html_b.decode("utf-8"))

        # Once nonzero RPM is observed, dropping back to 0 RPM MUST trigger axeos:fan_stalled
        mock.state.custom_info["fanrpm"] = 4100
        self._poll(tuner, "GammaNoTach")
        mock.state.custom_info["fanrpm"] = 0
        self._poll(tuner, "GammaNoTach")
        fx = hub.fault_payload(shared.snapshots["GammaNoTach"], hub.settings.get())
        self.assertIn("axeos:fan_stalled", {f["key"] for f in fx["active"]})

    def test_octaxe_rail_off_synthetic_replay_remembered_vr_cause_and_fault_hold(self):
        now = [1_700_000_000.0]
        info = {
            "deviceModel": "NerdOCTAxe",
            "ASICModel": "BM1370",
            "version": "v1.0.37.1",
            "temp": 58.0,
            "vrTemp": 61.0,
            "vrTempInt": 54.0,
            "asicTemps": [54, 55, 56, 58, 57, 55, 54, 53],
            "frequency": 750,
            "coreVoltage": 1200,
            "coreVoltageActual": 1196,
            "voltage": 12100,
            "minVoltage": 10000,
            "maxVoltage": 13500,
            "currentA": 12.5,
            "maxCurrentA": 22.0,
            "power": 151.0,
            "maxPower": 260.0,
            "hashRate": 9800.0,
            "fanspeed": 85.0,
            "fanrpm": 4800,
            "fanrpm2": 4750,
            "sharesAccepted": 100,
            "sharesRejected": 0,
            "uptimeSeconds": 3600,
            "shutdown": False,
        }
        mock = self._mock(name="OCTAxe1", kind="nerdoctaxe", v2_enabled=False, custom_info=info, frequency=750, core_voltage=1200)
        _, _, shared, tuner, hub = self._stack([{"name": "OCTAxe1", "ip": "127.0.0.1", "port": mock.port, "type": "nerdoctaxe", "enabled": True}])
        tuner.clock = lambda: now[0]
        hub.clock = lambda: now[0]

        # 1. Healthy polls across 10+ min establish last_stable_freq & healthy baseline
        self._poll(tuner, "OCTAxe1")
        now[0] += 610.0
        self._poll(tuner, "OCTAxe1")
        self.assertEqual(tuner.states["OCTAxe1"].last_stable_freq, 750)

        # 2. VR spike to 120.4 C (triggers emergency_down + 3h lockout + remembered VR overheat)
        now[0] += 15.0
        mock.state.custom_info["vrTemp"] = 120.4
        self._poll(tuner, "OCTAxe1")
        self.assertGreater(tuner.states["OCTAxe1"].emergency_lockout_until, now[0])
        self.assertEqual(hub.fault_context.get("OCTAxe1")["last_vr_overheat"]["temp_c"], 120.4)
        self.assertIsNotNone(hub.fault_snapshots.latest("OCTAxe1"))

        # 3. Watchdog reboot leaves v1.0.37.1 shutdown=False, boardError absent/0, rail off (coreVoltageActual=0, power=0.06, asicTemps=[0]*8)
        now[0] += 30.0
        mock.state.custom_info.update({
            "temp": 33.5,
            "vrTemp": 30.3,
            "vrTempInt": 24.75,
            "asicTemps": [0, 0, 0, 0, 0, 0, 0, 0],
            "frequency": 750,
            "coreVoltage": 1200,
            "coreVoltageActual": 0.0,
            "voltage": 12000,
            "currentA": 0.0,
            "power": 0.0625,
            "hashRate": 0.0,
            "uptimeSeconds": 45,
            "shutdown": False,
            "lastResetReason": "SYSTEM.RESET_TASK_WATCHDOG",
        })
        self._poll(tuner, "OCTAxe1")
        snap = shared.snapshots["OCTAxe1"]
        self.assertEqual(snap["last_action"], "fault_hold")
        faults = hub.faults_now["OCTAxe1"]
        rail = next((f for f in faults if f["key"] == "axeos:chip_power_rail_off"), None)
        self.assertIsNotNone(rail)
        self.assertIn("vrTemp overheat", rail.get("text", ""))
        self.assertIn("vrTemp 120.4", rail.get("cause_summary", ""))
        self.assertTrue(rail.get("cause_iso"))
        self.assertIn("power stage", rail.get("component", ""))
        fx = hub.fault_payload(snap, hub.settings.get())
        self.assertTrue(fx["reset"]["available"])
        self.assertEqual(fx["reset"]["action"], "restart")

    def test_component_attribution_and_timestamped_causes(self):
        now = 1_700_000_000.0
        raw_axe = {
            "deviceModel": "NerdOCTAxe",
            "temp": 55.0,
            "vrTemp": 61.0,
            "vrTempInt": 107.5,
            "vr_max_c": 65.0,
            "asicTemps": [54.0, 0.0, 56.0, 55.0],
            "dead_asics": [1], "hashrateMonitor": {"asics": [{"total": 500.0, "errorCount": 14}, {"total": 0.0, "errorCount": 0}]},
            "hashRate": 500.0,
        }
        faults = nk_faults.collect_faults(raw_axe, uptime_sec=600.0)
        faults = nk_faults.annotate_fault_causes(faults, {"now": now, "log_lines": ["2026-11-23 vrTempInt critical 107.5C"]})
        by_key = {f["key"]: f for f in faults}
        self.assertIn("vrTempInt", by_key["axeos:high_vr_temp"]["component"])
        self.assertIn("ASIC slot(s) [2]", by_key["nerd:asic_sensor_dead"]["component"])
        self.assertIn("ASIC slot(s) [2]", by_key["axeos:dead_asic"]["component"])
        self.assertTrue(by_key["axeos:high_vr_temp"]["cause_iso"])
        self.assertIn("107.5C", by_key["axeos:high_vr_temp"].get("log_line", ""))

        raw_ant = {
            "is_braiins": True,
            "chains": [{"idx": 1, "acn": 76, "acs": "oooxoooooo-o"}],
            "cg_devs": [{"id": 2, "status": "Dead", "last_share_time": 0}],
            "cg_chip_temps": [97.0, 65.0, 66.0],
        }
        ant_faults = nk_faults.collect_faults(raw_ant, uptime_sec=600.0)
        ant_by = {f["key"]: f for f in ant_faults}
        self.assertIn("Chain 1", ant_by["bitmain:chain_bad_chips"]["component"])
        self.assertIn("#4", ant_by["bitmain:chain_bad_chips"]["component"])
        self.assertIn("#11", ant_by["bitmain:chain_bad_chips"]["component"])
        self.assertIn("DEVS[2]", ant_by["braiins:cgminer:dev_unhealthy"]["component"])
        self.assertIn("Chain [1] chip sensor", ant_by["braiins:cgminer:temp_high"]["component"])

    def test_safety_guards_lockout_migration_and_plausibility(self):
        ud = self.dir / "mig_ud"
        ud.mkdir()
        legacy_dev = ud / "devices.json"
        legacy_dev.write_text(json.dumps({
            "schema_version": 2,
            "devices": [
                {"name": "NerdDefault", "ip": "127.0.0.1", "type": "nerdoctaxe", "overrides": {"max_freq": 850}},
                {"name": "NerdOpted", "ip": "127.0.0.2", "type": "nerdoctaxe", "overclock_opt_in": True, "overrides": {"max_freq": 850}},
            ],
            "enabled": {},
        }), encoding="utf-8")
        devs_list, _ = autotune.migrate_or_load_userdata({"devices": []}, {}, self.dir / "state.json", legacy_dev)
        self.assertTrue((ud / "devices.json.v2.bak").exists())
        saved = json.loads(legacy_dev.read_text(encoding="utf-8"))
        self.assertEqual(saved["schema_version"], 3)
        devs = {d["name"]: d for d in devs_list}
        self.assertEqual(devs["NerdDefault"]["overrides"]["max_freq"], 800.0)
        self.assertEqual(devs["NerdOpted"]["overrides"]["max_freq"], 850.0)

        cfg_n = nerd_cfg(max_freq=800, vr_target_c=65, vr_max_c=70)
        d_ror = autotune.decide(
            reading(asic_temp=50, vr_temp=56.5, frequency=700, hashrate=9000),
            cfg_n, 5, error_pct=0.1, vr_rise_c=4.5, vr_rise_dt=45.0,
        )
        self.assertEqual(d_ror.action, "step_down")
        self.assertTrue(d_ror.thermal_step_down)
        self.assertIn("VR rapid rise", d_ror.reason)

        d_p85 = autotune.decide(
            reading(asic_temp=50, vr_temp=52, frequency=700, power=215.0, power_limit_w=250.0, hashrate=9000),
            cfg_n, 5, error_pct=0.1,
        )
        self.assertEqual(d_p85.action, "hold")
        d_p95 = autotune.decide(
            reading(asic_temp=50, vr_temp=52, frequency=700, power=240.0, power_limit_w=250.0, hashrate=9000),
            cfg_n, 5, error_pct=0.1,
        )
        self.assertEqual(d_p95.action, "step_down")
        self.assertTrue(d_p95.thermal_step_down)

        d_sag12 = autotune.decide(
            reading(asic_temp=50, vr_temp=52, frequency=700, input_voltage=11.1, hashrate=9000),
            cfg_n, 5, error_pct=0.1,
        )
        self.assertEqual(d_sag12.action, "step_down")
        cfg_g = gamma_cfg()
        d_sag5 = autotune.decide(
            reading(asic_temp=50, vr_temp=52, frequency=600, input_voltage=4.60, hashrate=1000),
            cfg_g, 5, error_pct=0.1,
        )
        self.assertEqual(d_sag5.action, "step_down")

        d_plaus = autotune.decide(
            reading(asic_temp=38.0, vr_temp=108.0, frequency=700, hashrate=9000),
            cfg_n, 5, error_pct=0.1, sensor_suspect_reason="vrTemp=108.0C while asic=38.0C",
        )
        self.assertEqual(d_plaus.action, "step_down")
        self.assertIn("suspect", d_plaus.voltage_reason.lower())

        now = [1_700_000_000.0]
        info = {
            "deviceModel": "NerdOCTAxe",
            "ASICModel": "BM1370",
            "temp": 52.0,
            "vrTemp": 54.0,
            "frequency": 750,
            "coreVoltage": 1200,
            "coreVoltageActual": 1195,
            "voltage": 12100,
            "power": 150.0,
            "maxPower": 260.0,
            "hashRate": 9500.0,
            "fanrpm": 4500,
            "fanspeed": 80.0,
            "sharesAccepted": 50,
            "sharesRejected": 0,
            "uptimeSeconds": 900,
        }
        mock = self._mock(name="NerdSafe", kind="nerdoctaxe", v2_enabled=False, custom_info=info, frequency=750, core_voltage=1200)
        ud_file_run, gcfg_run, shared1, tuner1, hub1 = self._stack([{"name": "NerdSafe", "ip": "127.0.0.1", "port": mock.port, "type": "nerdoctaxe", "enabled": True}])
        tuner1.clock = lambda: now[0]
        hub1.clock = lambda: now[0]
        self._poll(tuner1, "NerdSafe")
        # Thermal step_down bypasses 180s cooldown (30s settle)
        now[0] += 15.0
        mock.state.custom_info["temp"] = 60.5
        self._poll(tuner1, "NerdSafe")
        self.assertEqual(mock.state.frequency, 725)
        mock.state.custom_info["frequency"] = 725
        now[0] += 35.0
        self._poll(tuner1, "NerdSafe")
        self.assertEqual(mock.state.frequency, 700)
        mock.state.custom_info["frequency"] = 700

        # Emergency down persists 3h lockout
        now[0] += 35.0
        mock.state.custom_info["vrTemp"] = 75.0
        self._poll(tuner1, "NerdSafe")
        self.assertTrue((ud_file_run.parent / "tuner_safety.json").exists())
        hub1.stop()

        # Reload fresh Tuner on same userdata dir and verify lockout persists
        from dashboard import SharedState
        cfg_p2 = ud_file_run.parent.parent / "config.json"
        cfg2 = autotune.parse_config(autotune.load_raw_config(cfg_p2), cfg_p2.parent, config_path=cfg_p2, use_userdata=True)
        shared2 = SharedState([d.name for d in cfg2.devices], ud_file_run.parent.parent / "autotune-state.json")
        tuner2 = autotune.Tuner(cfg2, shared=shared2, clock=lambda: now[0])
        shared2.attach_tuner(tuner2)
        self.assertGreater(tuner2.states["NerdSafe"].emergency_lockout_until, now[0])
        now[0] += 600.0
        mock.state.custom_info["temp"] = 48.0
        mock.state.custom_info["vrTemp"] = 48.0
        freq_before = mock.state.frequency
        for _ in range(4):
            now[0] += 15.0
            self._poll(tuner2, "NerdSafe")
        self.assertEqual(mock.state.frequency, freq_before)
        self.assertIn("lockout", tuner2.states["NerdSafe"].last_reason.lower())

    def test_12h_rolling_buffer_persistence_dedupe_redaction_and_delete(self):
        now = [1_700_000_000.0]
        info = {
            "ASICModel": "BM1370",
            "temp": 55.0,
            "vrTemp": 58.0,
            "frequency": 525,
            "coreVoltage": 1200,
            "coreVoltageActual": 1195,
            "voltage": 5100,
            "power": 15.0,
            "hashRate": 1050.0,
            "fanspeed": 80.0,
            "fanrpm": 4200,
            "stratumUser": f"{PAYOUT}.worker1",
            "ssid": "SecretHomeWiFi",
            "macAddr": "AA:BB:CC:DD:EE:FF",
            "sharesAccepted": 10,
            "sharesRejected": 0,
            "uptimeSeconds": 600,
        }
        mock = self._mock(name="RecGamma", kind="gamma", custom_info=info)
        ud_file_run, gcfg_run, shared1, tuner1, hub1 = self._stack([{"name": "RecGamma", "ip": "127.0.0.1", "port": mock.port, "type": "gamma", "enabled": False}])
        tuner1.clock = lambda: now[0]
        hub1.clock = lambda: now[0]

        # 1 old sample > 12h ago (pruned), plus 4 recent healthy polls
        now[0] = 1_700_000_000.0 - 13 * 3600.0
        self._poll(tuner1, "RecGamma")
        now[0] = 1_700_000_000.0
        for i in range(4):
            mock.state.custom_info["temp"] = 55.0 + i * 0.2
            self._poll(tuner1, "RecGamma")
            now[0] += 15.0
        hub1.stop()
        self.assertTrue((ud_file_run.parent / "fault_buffers" / "RecGamma.json.gz").exists())

        # Restart runtime -> buffer persists across restarts
        from dashboard import SharedState
        cfg_p2 = ud_file_run.parent.parent / "config.json"
        cfg2 = autotune.parse_config(autotune.load_raw_config(cfg_p2), cfg_p2.parent, config_path=cfg_p2, use_userdata=True)
        shared2 = SharedState([d.name for d in cfg2.devices], ud_file_run.parent.parent / "autotune-state.json")
        tuner2 = autotune.Tuner(cfg2, shared=shared2, clock=lambda: now[0])
        shared2.attach_tuner(tuner2)
        hub2 = miner_hearth.attach_feature_hub(tuner2, shared2, start=False)
        hub2.clock = lambda: now[0]

        # Trigger fault -> writes userdata/fault_records/RecGamma_<ts>.json.gz
        mock.state.custom_info["overheat_mode"] = 1
        mock.state.custom_info["temp"] = 72.0
        self._poll(tuner2, "RecGamma")
        rec_dir = ud_file_run.parent / "fault_records"
        gz_files = list(rec_dir.glob("RecGamma_*.json.gz"))
        self.assertEqual(len(gz_files), 1)
        rid = gz_files[0].name

        # Clear then re-fire fault within 12h window -> dedupes/extends existing file
        now[0] += 30.0
        mock.state.custom_info["overheat_mode"] = 0
        mock.state.custom_info["temp"] = 56.0
        self._poll(tuner2, "RecGamma")
        now[0] += 30.0
        mock.state.custom_info["vrTemp"] = 108.0
        self._poll(tuner2, "RecGamma")
        self.assertEqual(len(list(rec_dir.glob("RecGamma_*.json.gz"))), 1)

        st_l, res_l, _ = self._req(shared2, "GET", "/api/nk/fault-records?device=RecGamma")
        self.assertEqual(st_l, 200)
        recs = res_l["records"]
        self.assertEqual(len(recs), 1)
        self.assertGreaterEqual(recs[0]["sample_count"], 5)
        self.assertLessEqual(recs[0]["sample_count"], 8)

        st_v, view_json, _ = self._req(shared2, "GET", f"/api/nk/fault-records/view?id={rid}")
        self.assertEqual(st_v, 200)
        view_txt = json.dumps(view_json)
        self.assertNotIn(PAYOUT, view_txt)
        self.assertNotIn("SecretHomeWiFi", view_txt)
        self.assertNotIn("AA:BB:CC:DD:EE:FF", view_txt)

        st_e, exp_json, _ = self._req(shared2, "GET", f"/api/nk/fault-records/export?id={rid}")
        self.assertEqual(st_e, 200)
        exp_txt = json.dumps(exp_json)
        self.assertNotIn(PAYOUT, exp_txt)
        self.assertNotIn("SecretHomeWiFi", exp_txt)

        _, html_b, _ = self._req(shared2, "GET", "/")
        html = html_b.decode("utf-8")
        self.assertIn("nk110DiagnosticsTab", html)
        self.assertIn("Saved Pre-Fault Records", html)
        self.assertIn("Pre-Fault Snapshot vs Current", html)

        st_bad, _, _ = self._req(shared2, "POST", "/api/nk/fault-records/delete", {"id": "../devices.json"})
        self.assertEqual(st_bad, 404)
        st_del, res_del, _ = self._req(shared2, "POST", "/api/nk/fault-records/delete", {"id": rid})
        self.assertEqual(st_del, 200)
        self.assertTrue(res_del["ok"])
        self.assertFalse(gz_files[0].exists())


class Gamma601VoltageRecoveryV113Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.servers = []

    def tearDown(self):
        for srv in self.servers:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        for h in list(logging.getLogger().handlers):
            try:
                h.close()
            except Exception:
                pass
        logging.getLogger().handlers.clear()
        self.tmp.cleanup()

    def _start_gamma601_server(self, state_dict: dict[str, Any]):
        import http.server
        import threading

        class Gamma601Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def _send_json(self, code: int, obj: Any) -> None:
                raw = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/api/system/info":
                    self._send_json(200, dict(state_dict))
                else:
                    self._send_json(404, {"error": "not found"})

            def do_PATCH(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}") if length else {}
                state_dict["patches"].append({"path": self.path, "body": body})
                if "frequency" in body:
                    state_dict["frequency"] = float(body["frequency"])
                if "coreVoltage" in body:
                    state_dict["coreVoltage"] = int(body["coreVoltage"])
                    state_dict["coreVoltageActual"] = int(body["coreVoltage"]) - 12
                self._send_json(200, {"ok": True})

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Gamma601Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.servers.append(srv)
        return srv.server_address[1]

    def _make_tuner(self, devices: list[dict[str, Any]], now_ref: list[float]):
        import miner_hearth
        from dashboard import SharedState

        cfg_path = self.dir / "config.json"
        ud_path = self.dir / "userdata" / "devices.json"
        ud_path.parent.mkdir(parents=True, exist_ok=True)
        ud_path.write_text(json.dumps({"schema": 3, "devices": devices}), encoding="utf-8")
        cfg_path.write_text(
            json.dumps({
                "devices": [],
                "log_dir": str(self.dir / "run"),
                "state_file": str(self.dir / "autotune-state.json"),
                "userdata_file": str(ud_path),
                "dashboard": {"enabled": False},
            }),
            encoding="utf-8",
        )
        gcfg = autotune.parse_config(autotune.load_raw_config(cfg_path), self.dir, config_path=cfg_path, use_userdata=True)
        shared = SharedState([d.name for d in gcfg.devices], gcfg.state_file)
        for d in gcfg.devices:
            if d.enabled_default is not None:
                shared.set_enabled(d.name, bool(d.enabled_default))
        tuner = autotune.Tuner(gcfg, shared=shared, clock=lambda: now_ref[0], sleeper=lambda _s: None)
        shared.attach_tuner(tuner)
        hub = miner_hearth.attach_feature_hub(tuner, shared, start=False)
        hub.clock = lambda: now_ref[0]
        return cfg_path, ud_path, shared, tuner, hub

    def test_gamma_601_discovery_and_full_poll_loop_850mhz_1200mv_4pct_error(self):
        from nk_adapters.axeos import AxeOSAdapter

        g_state: dict[str, Any] = {
            "boardVersion": "601",
            "ASICModel": "BM1370",
            "asicCount": 1,
            "hostname": "office-601",
            "version": "v2.5.1",
            "frequency": 850,
            "actualFrequency": 850,
            "coreVoltage": 1200,
            "coreVoltageActual": 1188,
            "temp": 52.0,
            "vrTemp": 56.0,
            "hashRate": 1180.0,
            "power": 18.0,
            "maxPower": 25.0,
            "voltage": 5000.0,
            "fanspeed": 65,
            "fanrpm": 0,
            "errorPercentage": 4.5,
            "sharesAccepted": 500,
            "sharesRejected": 23,
            "uptimeSeconds": 1800,
            "stratumURL": "public-pool.io",
            "stratumPort": 21496,
            "stratumUser": "bc1qtest.gamma601",
            "patches": [],
        }
        port = self._start_gamma601_server(g_state)

        # Both scanner and AxeOSAdapter must classify renamed-hostname Gamma 601 as 'gamma'
        hit = scanner.probe_host("127.0.0.1", http_port=port, cgminer_port=1)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["type"], "gamma")

        probe = AxeOSAdapter().probe("127.0.0.1", http_port=port, cgminer_port=1)
        self.assertIsNotNone(probe)
        self.assertEqual(probe.device_type, "gamma")

        # Full poll loop with real defaults (cooldown_sec=180)
        now = [1000.0]
        _, _, shared, tuner, _ = self._make_tuner([], now)
        ok, err, _ = tuner.add_device({"name": "Gamma601", "ip": "127.0.0.1", "port": port, "type": hit["type"]})
        self.assertTrue(ok, err)
        st = tuner.states["Gamma601"]
        self.assertTrue(st.cfg.voltage_control)
        shared.set_enabled("Gamma601", True)

        # Poll 1: at 850 MHz cap with 4.5% error -> steps 1200 -> 1220 mV with overclockEnabled=1
        d1 = tuner.poll_device(st)
        self.assertEqual(d1.action, "voltage_up")
        self.assertEqual(g_state["coreVoltage"], 1220)
        self.assertEqual(g_state["patches"][-1]["body"].get("overclockEnabled"), 1)

        # Polls at +15s, +30s, +45s wait on severe-error 60s fast cooldown (not 180s)
        for dt in (15.0, 15.0, 15.0):
            now[0] += dt
            dw = tuner.poll_device(st)
            self.assertEqual(dw.action, "hold")
            self.assertIn("cooldown", dw.reason)

        # Poll at +60s: raises 1220 -> 1240 mV
        now[0] += 15.0
        d2 = tuner.poll_device(st)
        self.assertEqual(d2.action, "voltage_up")
        self.assertEqual(g_state["coreVoltage"], 1240)
        self.assertEqual(g_state["frequency"], 850)

        # Error settles at 1240 mV -> holds at 850 MHz / 1240 mV
        g_state["errorPercentage"] = 0.7
        now[0] += 15.0
        d3 = tuner.poll_device(st)
        self.assertEqual(d3.action, "hold")
        self.assertEqual(g_state["coreVoltage"], 1240)
        self.assertEqual(g_state["frequency"], 850)

        # Severe error (4.6%) at 1240 mV reacts immediately after the 60s severe cooldown (without waiting
        # for rolling-window dilution to catch up) and clamps strictly at HARD_MAX_VOLTAGE_MV (1250 mV)
        g_state["errorPercentage"] = 4.6
        saw_cap_raise = False
        for _ in range(5):
            now[0] += 15.0
            d_step = tuner.poll_device(st)
            if d_step.action == "voltage_up":
                saw_cap_raise = True
                break
        self.assertTrue(saw_cap_raise)
        self.assertEqual(g_state["coreVoltage"], 1250)
        self.assertLessEqual(g_state["coreVoltage"], autotune.HARD_MAX_VOLTAGE_MV)

        # Once at 1250 mV cap and error stays > error_high_pct after cooldown, step frequency down
        now[0] += 180.0
        d5 = tuner.poll_device(st)
        self.assertEqual(d5.action, "step_down")
        self.assertEqual(g_state["frequency"], 825)
        self.assertEqual(g_state["coreVoltage"], 1250)

    def test_previously_misclassified_gamma_601_auto_promoted_and_ui_toggle(self):
        from dashboard import handle_dashboard_request

        g_state: dict[str, Any] = {
            "boardVersion": "601",
            "ASICModel": "BM1370",
            "asicCount": 1,
            "hostname": "office-601",
            "version": "v2.5.1",
            "frequency": 850,
            "coreVoltage": 1200,
            "coreVoltageActual": 1188,
            "temp": 52.0,
            "vrTemp": 56.0,
            "hashRate": 1180.0,
            "power": 18.0,
            "maxPower": 25.0,
            "voltage": 5000.0,
            "fanspeed": 65,
            "fanrpm": 0,
            "errorPercentage": 4.4,
            "patches": [],
        }
        port = self._start_gamma601_server(g_state)
        now = [2000.0]
        # Saved previously as axeos-generic with voltage_control=False (pre-patch bug state)
        _, ud_path, shared, tuner, _ = self._make_tuner(
            [
                {
                    "name": "GammaOld",
                    "ip": "127.0.0.1",
                    "port": port,
                    "type": "axeos-generic",
                    "voltage_control": False,
                    "enabled": True,
                },
                {
                    "name": "GenericBox",
                    "ip": "127.0.0.99",
                    "type": "axeos-generic",
                    "voltage_control": False,
                    "enabled": False,
                },
                {
                    "name": "OctBox",
                    "ip": "127.0.0.98",
                    "type": "nerdoctaxe",
                    "enabled": False,
                },
            ],
            now,
        )
        st = tuner.states["GammaOld"]
        self.assertEqual(st.cfg.type, "axeos-generic")
        self.assertFalse(st.cfg.voltage_control)

        # First poll auto-promotes GammaOld -> gamma + voltage_control=True and raises voltage
        d = tuner.poll_device(st)
        self.assertEqual(st.cfg.type, "gamma")
        self.assertTrue(st.cfg.voltage_control)
        self.assertEqual(d.action, "voltage_up")
        self.assertEqual(g_state["coreVoltage"], 1220)

        saved = json.loads(ud_path.read_text(encoding="utf-8"))
        gamma_saved = [x for x in saved["devices"] if x["name"] == "GammaOld"][0]
        self.assertEqual(gamma_saved["type"], "gamma")
        self.assertTrue(gamma_saved["voltage_control"])

        # UI overrides can toggle voltage_control on axeos-generic/gamma, and UI renders checkbox
        st_code, resp_b, _ = handle_dashboard_request(
            "POST",
            "/api/device/GenericBox/overrides",
            json.dumps({"overrides": {"voltage_control": True}}).encode("utf-8"),
            shared,
        )
        self.assertEqual(st_code, 200)
        self.assertTrue(tuner.states["GenericBox"].cfg.voltage_control)

        # NerdOCTAxe cannot have voltage_control enabled via overrides
        st_n, _, _ = handle_dashboard_request(
            "POST",
            "/api/device/OctBox/overrides",
            json.dumps({"overrides": {"voltage_control": True}}).encode("utf-8"),
            shared,
        )
        self.assertEqual(st_n, 200)
        self.assertFalse(tuner.states["OctBox"].cfg.voltage_control)

        _, html_b, _ = handle_dashboard_request("GET", "/", b"", shared)
        self.assertIn("ov_voltage_control_", html_b.decode("utf-8"))

    def test_core_voltage_actual_fallback_when_core_voltage_missing(self):
        cfg = gamma_cfg(max_freq=850, max_voltage_mv=1250)
        r = reading(asic_temp=52, vr_temp=56, frequency=850, core_voltage=None, core_voltage_actual=1200, error_percentage=4.5)
        d = autotune.decide(r, cfg, 0, error_pct=4.5)
        self.assertEqual(d.action, "voltage_up")
        self.assertEqual(d.new_voltage, 1220)


class Gamma601VoltageFloorAndSpikeTests(unittest.TestCase):
    """Realistic Bitaxe Gamma 601 regressions for voltage-floor hysteresis, rolling-spike dilution, and metric guards."""

    setUp = Gamma601VoltageRecoveryV113Tests.setUp
    tearDown = Gamma601VoltageRecoveryV113Tests.tearDown
    _start_gamma601_server = Gamma601VoltageRecoveryV113Tests._start_gamma601_server
    _make_tuner = Gamma601VoltageRecoveryV113Tests._make_tuner

    def test_gamma_601_850mhz_recovers_voltage_and_never_redescends_across_restart(self):
        import http.server
        import threading
        # Realistic ASIC behavior at 850 MHz: low errors at >=1230 mV, error spike at <=1220 mV.
        def err_for_voltage(v: int) -> float:
            if v >= 1240:
                return 0.15
            if v >= 1230:
                return 0.25
            return 2.40

        g_state: dict[str, Any] = {
            "boardVersion": "601",
            "ASICModel": "BM1370",
            "asicCount": 1,
            "hostname": "bitaxe-gamma-601",
            "version": "v2.5.1",
            "frequency": 850,
            "coreVoltage": 1240,
            "coreVoltageActual": 1232,
            "temp": 51.0,
            "vrTemp": 54.0,
            "hashRate": 1190.0,
            "expectedHashrate": 1200.0,
            "power": 19.5,
            "maxPower": 25.0,
            "voltage": 5000.0,
            "fanspeed": 60,
            "fanrpm": 4200,
            "uptimeSeconds": 600,
            "sharesAccepted": 5000,
            "sharesRejected": 0,
            "errorPercentage": 0.15,
            "patches": [],
        }

        class DynamicGammaHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                if self.path.startswith("/api/system/info"):
                    g_state["errorPercentage"] = err_for_voltage(int(g_state["coreVoltage"]))
                    body = json.dumps(g_state).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_PATCH(self):
                n = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                g_state["patches"].append({"path": self.path, "body": body})
                if "frequency" in body:
                    g_state["frequency"] = float(body["frequency"])
                if "coreVoltage" in body:
                    g_state["coreVoltage"] = int(body["coreVoltage"])
                    g_state["coreVoltageActual"] = int(body["coreVoltage"]) - 8
                self.send_response(200)
                self.end_headers()

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DynamicGammaHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.servers.append(srv)

        now = [1000.0]
        cfg_path, ud_path, shared, tuner, _ = self._make_tuner([], now)
        ok, err, _ = tuner.add_device({"name": "Gamma601", "ip": "127.0.0.1", "port": port, "type": "gamma"})
        self.assertTrue(ok, err)
        shared.set_enabled("Gamma601", True)
        st = tuner.states["Gamma601"]

        # Step down 1240 -> 1230 -> 1220 mV while errors are low, then hit 2.40% on the very first poll at 1220 mV
        for _ in range(45):
            now[0] += 180.0
            tuner.poll_device(st)

        # Must have recovered back up to 1230 mV and stabilized there without oscillating back to 1220 mV
        self.assertEqual(g_state["coreVoltage"], 1230)
        self.assertEqual(g_state["frequency"], 850)
        self.assertEqual(st.bad_voltage_by_freq.get("850"), 1220)
        self.assertEqual(st.learned_min_voltage_mv, 1230)

        patches_after_settle = len(g_state["patches"])
        for _ in range(15):
            now[0] += 180.0
            d = tuner.poll_device(st)
            self.assertEqual(d.action, "hold")
            self.assertEqual(g_state["coreVoltage"], 1230)
        self.assertEqual(len(g_state["patches"]), patches_after_settle)

        # Restart Tuner from persisted userdata + tuner_safety.json -> floor must persist!
        from dashboard import SharedState
        cfg2 = autotune.parse_config(autotune.load_raw_config(cfg_path), self.dir, config_path=cfg_path, use_userdata=True)
        shared2 = SharedState([d.name for d in cfg2.devices], cfg2.state_file)
        shared2.set_enabled("Gamma601", True)
        tuner2 = autotune.Tuner(cfg2, clock=lambda: now[0], sleeper=lambda _s: None, shared=shared2)
        st2 = tuner2.states["Gamma601"]
        self.assertEqual(st2.bad_voltage_by_freq.get("850"), 1220)
        for _ in range(10):
            now[0] += 180.0
            d2 = tuner2.poll_device(st2)
            self.assertEqual(d2.action, "hold")
            self.assertEqual(g_state["coreVoltage"], 1230)

        # Resetting overrides clears the learned floor
        ok_rst, err_rst, _ = tuner2.update_device_overrides("Gamma601", {"reset": True})
        self.assertTrue(ok_rst, err_rst)
        self.assertEqual(st2.bad_voltage_by_freq, {})
        self.assertIsNone(st2.learned_min_voltage_mv)

    def test_single_poll_spike_not_masked_by_rolling_average_and_never_drops_voltage(self):
        g_state: dict[str, Any] = {
            "boardVersion": "601",
            "ASICModel": "BM1370",
            "asicCount": 1,
            "hostname": "bitaxe-gamma-601",
            "version": "v2.5.1",
            "frequency": 850,
            "coreVoltage": 1220,
            "coreVoltageActual": 1212,
            "temp": 51.0,
            "vrTemp": 54.0,
            "hashRate": 1180.0,
            "power": 19.0,
            "maxPower": 25.0,
            "voltage": 5000.0,
            "fanspeed": 60,
            "fanrpm": 4200,
            "uptimeSeconds": 600,
            "errorPercentage": 0.1,
            "patches": [],
        }
        port = self._start_gamma601_server(g_state)
        now = [1000.0]
        _, _, shared, tuner, _ = self._make_tuner([], now)
        ok, err, _ = tuner.add_device({"name": "GammaSpike", "ip": "127.0.0.1", "port": port, "type": "gamma"})
        self.assertTrue(ok, err)
        shared.set_enabled("GammaSpike", True)
        st = tuner.states["GammaSpike"]

        # 4 low-error polls (0.1%)
        for _ in range(4):
            now[0] += 15.0
            d = tuner.poll_device(st)
            self.assertEqual(d.action, "hold")
        self.assertEqual(st.low_error_polls, 4)

        # Poll 5: moderate spike 0.75% (rolling = 0.23% < 0.50%!) -> must hold & reset streak, NEVER voltage_down
        g_state["errorPercentage"] = 0.75
        now[0] += 15.0
        d_mod = tuner.poll_device(st)
        self.assertEqual(d_mod.action, "hold")
        self.assertEqual(st.low_error_polls, 0)
        self.assertEqual(g_state["coreVoltage"], 1220)

        # Build 4 low-error polls again, then spike to 2.0% on poll 5 (rolling = 0.48% < 0.50%!)
        g_state["errorPercentage"] = 0.1
        for _ in range(4):
            now[0] += 15.0
            tuner.poll_device(st)
        self.assertEqual(st.low_error_polls, 4)

        g_state["errorPercentage"] = 2.0
        now[0] += 15.0
        d_hi = tuner.poll_device(st)
        self.assertEqual(d_hi.action, "voltage_up")
        self.assertEqual(g_state["coreVoltage"], 1230)

    def test_no_voltage_drop_on_shares_fallback_warmup_or_missing_metric(self):
        # 1) Missing errorPercentage -> falls back to sharesRejected (0.0%): must NEVER drop voltage
        g_state: dict[str, Any] = {
            "boardVersion": "601",
            "ASICModel": "BM1370",
            "asicCount": 1,
            "hostname": "bitaxe-gamma-601",
            "version": "v2.5.1",
            "frequency": 850,
            "coreVoltage": 1240,
            "coreVoltageActual": 1232,
            "temp": 51.0,
            "vrTemp": 54.0,
            "hashRate": 1150.0,
            "power": 19.0,
            "maxPower": 25.0,
            "voltage": 5000.0,
            "fanspeed": 60,
            "fanrpm": 4200,
            "uptimeSeconds": 600,
            "sharesAccepted": 5000,
            "sharesRejected": 0,
            "patches": [],
        }
        port = self._start_gamma601_server(g_state)
        now = [1000.0]
        _, _, shared, tuner, _ = self._make_tuner([], now)
        ok, err, _ = tuner.add_device({"name": "GammaNoErrPct", "ip": "127.0.0.1", "port": port, "type": "gamma"})
        self.assertTrue(ok, err)
        shared.set_enabled("GammaNoErrPct", True)
        st = tuner.states["GammaNoErrPct"]

        for _ in range(10):
            now[0] += 180.0
            d = tuner.poll_device(st)
            self.assertEqual(d.action, "hold")
            self.assertEqual(g_state["coreVoltage"], 1240)
            self.assertIn("errorPercentage unavailable", d.voltage_reason)

        # 2) Present errorPercentage=0.0 during boot warmup (uptimeSeconds=20 < 90): must NEVER drop voltage
        g_state["errorPercentage"] = 0.0
        g_state["uptimeSeconds"] = 20
        for _ in range(8):
            now[0] += 180.0
            d = tuner.poll_device(st)
            self.assertEqual(d.action, "hold")
            self.assertEqual(g_state["coreVoltage"], 1240)

        # 3) Missing all error metrics after prior low errors: record_error(None) must NOT return stale low cache
        g_state["uptimeSeconds"] = 600
        for _ in range(3):
            now[0] += 15.0
            tuner.poll_device(st)
        self.assertEqual(st.low_error_polls, 3)
        g_state.pop("errorPercentage", None)
        g_state.pop("sharesAccepted", None)
        g_state.pop("sharesRejected", None)
        for _ in range(6):
            now[0] += 180.0
            d = tuner.poll_device(st)
            self.assertEqual(d.action, "hold")
            self.assertEqual(st.low_error_polls, 0)
            self.assertEqual(g_state["coreVoltage"], 1240)


class TOUEnforcementAndUIConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.mocks: list[MockMiner] = []
        self.hubs: list[miner_hearth.FeatureHub] = []
        self.loggers: list[autotune.CsvLogger] = []

    def tearDown(self):
        for h in self.hubs:
            h.stop()
        for m in self.mocks:
            m.stop()
        for lg in self.loggers:
            lg.close()
        self.tmp.cleanup()

    def _stack(self, devs_raw, clock=None):
        return V18MinerHearthTests._stack(self, devs_raw, clock=clock)

    def _req(self, shared, method, path, body=None, ip="127.0.0.1", headers=None):
        return V18MinerHearthTests._req(self, shared, method, path, body=body, ip=ip, headers=headers)

    def test_tou_save_persists_survives_other_settings_and_reloads(self):
        now_t = _dt(2026, 11, 24, 18, 0).timestamp()  # Tuesday 18:00
        ud, _g, shared, _tuner, hub = self._stack(
            [{"name": "Gamma 1", "ip": "127.0.0.91", "type": "gamma", "enabled": True}],
            clock=lambda: now_t,
        )
        sched_patch = {
            "schedule": {
                "enabled": True,
                "windows": [
                    {
                        "name": "Peak hours",
                        "enabled": True,
                        "days": [1],
                        "start": "5:00 PM",
                        "end": "9:00 PM",
                        "devices": ["gamma 1"],
                        "axe_max_freq_mhz": 600,
                        "axe_asic_target_c": 57.0,
                        "bos_power_target_w": None,
                    }
                ],
            }
        }
        st, resp, _ = self._req(shared, "POST", "/api/nk/settings", {"settings": sched_patch})
        self.assertEqual(st, 200)
        self.assertTrue(resp["ok"])
        # 12h AM/PM strings normalized to 24h HH:MM; legacy axe_asic_target_c dropped cleanly
        saved_w = resp["settings"]["schedule"]["windows"][0]
        self.assertEqual(saved_w["start"], "17:00")
        self.assertEqual(saved_w["end"], "21:00")
        self.assertNotIn("axe_asic_target_c", saved_w)
        self.assertNotIn("asic_target_c", resp["schedule_status"])
        self.assertEqual(resp["schedule_status"]["state"], "active")
        self.assertIn("cap 600 MHz", resp["schedule_status"]["label"])
        self.assertIn("until 21:00", resp["schedule_status"]["label"])

        # Saving unrelated sections must never clobber TOU schedule
        for other in (
            {"power": {"rate_per_kwh": 0.25, "currency": "USD", "include_fees": False}},
            {"alerts": {"cooldown_min": 15}},
            {"webhooks": {"enabled": False}},
        ):
            st2, _, _ = self._req(shared, "POST", "/api/nk/settings", {"settings": other})
            self.assertEqual(st2, 200)

        # Fresh FeatureHub reload from disk preserves TOU schedule verbatim
        reloaded = miner_hearth.SettingsStore(ud.parent / "miner_hearth.json").get()["schedule"]
        self.assertTrue(reloaded["enabled"])
        self.assertEqual(len(reloaded["windows"]), 1)
        self.assertEqual(reloaded["windows"][0]["axe_max_freq_mhz"], 600)
        self.assertEqual(reloaded["windows"][0]["start"], "17:00")
        self.assertEqual(reloaded["windows"][0]["end"], "21:00")

    def test_tou_enforces_immediately_inside_cooldown_and_wakes_tuner(self):
        g = MockMiner(
            MinerState(name="Gamma 1", kind="gamma", frequency=850.0, actual_frequency=850.0, temp=51.0, vr_temp=52.0, core_voltage=1240),
            port=0,
        ).start()
        self.mocks.append(g)
        clock = {"t": _dt(2026, 11, 24, 18, 30).timestamp()}  # Tuesday 18:30
        _ud, _g, shared, tuner, hub = self._stack(
            [{"name": "Gamma 1", "ip": "127.0.0.1", "port": g.port, "type": "gamma", "enabled": True, "cooldown_sec": 180}],
            clock=lambda: clock["t"],
        )
        shared.set_enabled("Gamma 1", True)
        # Put the tuner right inside a 180s cooldown (only 5s after a prior change)
        st = tuner.states["Gamma 1"]
        st.last_change_ts = clock["t"] - 5.0
        st.cfg = autotune.dataclasses.replace(st.cfg, frequency_options=None)
        tuner._wake.clear()

        # Save TOU schedule via HTTP API -> must wake tuner immediately
        code, resp, _ = self._req(
            shared,
            "POST",
            "/api/nk/settings",
            {
                "settings": {
                    "schedule": {
                        "enabled": True,
                        "windows": [
                            {
                                "name": "Peak",
                                "enabled": True,
                                "days": [1],
                                "start": "17:00",
                                "end": "21:00",
                                "devices": ["gamma 1"],  # case-insensitive match
                                "axe_max_freq_mhz": 600,
                            }
                        ],
                    }
                }
            },
        )
        self.assertEqual(code, 200)
        self.assertTrue(tuner._wake.is_set())

        # Poll once: must NOT be deferred by 180s cooldown and must step straight to 600 MHz
        d = tuner.poll_device(st)
        self.assertEqual(d.action, "step_down")
        self.assertTrue(d.bypass_cooldown)
        self.assertEqual(d.new_freq, 600.0)
        self.assertEqual(g.state.frequency, 600.0)
        tlog = hub.tuning_log.list("Gamma 1")
        self.assertTrue(tlog and tlog[0]["source"] == "schedule")

        # Braiins OS+ watt-cap TOU also bypasses bos_cooldown_sec when lowering over cap
        s19 = autotune.DeviceConfig(
            name="Rig", ip="127.0.0.2", type="antminer", bos_power_mgmt=True,
            bos_min_watts=1800, bos_max_watts=2400, bos_cooldown_sec=300,
        )
        r = reading(firmware="Braiins OS", power_limit_w=3200.0, chip_temps=[68.0])
        act, new_w, _reason, _ = autotune.decide_bos_power(
            r, s19, 0, last_change_ts=clock["t"] - 10.0, now_ts=clock["t"],
            current_target_w=3200, bypass_cap_cooldown=True,
        )
        self.assertEqual((act, new_w), ("power_down", 2400))

    def test_tou_status_active_vs_off_peak_and_dashboard_ui_confirmation(self):
        clock = {"t": _dt(2026, 11, 24, 18, 0).timestamp()}  # Tue 18:00 (active)
        _ud, _g, shared, _tuner, hub = self._stack(
            [{"name": "Gamma 1", "ip": "127.0.0.92", "type": "gamma", "enabled": True}],
            clock=lambda: clock["t"],
        )
        hub.update_settings(
            {
                "schedule": {
                    "enabled": True,
                    "windows": [
                        {
                            "name": "Peak hours",
                            "enabled": True,
                            "days": [1],
                            "start": "17:00",
                            "end": "21:00",
                            "devices": [],
                            "axe_max_freq_mhz": 600,
                        }
                    ],
                }
            }
        )
        st, status_on, _ = self._req(shared, "GET", "/api/status")
        self.assertEqual(st, 200)
        ss_on = status_on["nk"]["schedule_status"]
        self.assertEqual(ss_on["state"], "active")
        self.assertIn("TOU: Peak hours", ss_on["label"])
        self.assertIn("cap 600 MHz until 21:00", ss_on["label"])
        self.assertEqual(status_on["nk"]["devices"]["Gamma 1"]["schedule"]["until"], "21:00")

        # Move clock to off-peak (Tue 14:00)
        clock["t"] = _dt(2026, 11, 24, 14, 0).timestamp()
        st2, status_off, _ = self._req(shared, "GET", "/api/status")
        self.assertEqual(st2, 200)
        ss_off = status_off["nk"]["schedule_status"]
        self.assertEqual(ss_off["state"], "off_peak")
        self.assertIn("TOU: off-peak", ss_off["label"])
        self.assertIn("next: Peak hours at 17:00", ss_off["label"])
        self.assertIsNone(status_off["nk"]["devices"]["Gamma 1"]["schedule"])

        # Dashboard HTML/JS includes local save note, floating toast, DOM sync, and TOU banner
        code_h, html_bytes, _ = self._req(shared, "GET", "/")
        self.assertEqual(code_h, 200)
        html = html_bytes.decode("utf-8") if isinstance(html_bytes, (bytes, bytearray)) else str(html_bytes)
        for token in (
            'id="nkScheduleNote"',
            'id="nkScheduleStatus"',
            'id="nkPowerNote"',
            'id="nkTouBar"',
            "function nkShowToast(",
            "function nkSyncWindowsFromDom(",
            "function nkNormalizeTimeInput(",
            "function nkRenderTou(",
            '"nkScheduleNote", "Schedule saved"',
            '"nkPowerNote", "Saved power settings"',
            "TOU: off-peak",
        ):
            self.assertIn(token, html)

    def test_tou_save_wakes_sleeping_tuner_thread_and_applies_within_two_seconds(self):
        g = MockMiner(
            MinerState(name="Gamma Wake", kind="gamma", frequency=850.0, actual_frequency=850.0, temp=50.0, vr_temp=50.0, core_voltage=1240),
            port=0,
        ).start()
        self.mocks.append(g)
        now_t = _dt(2026, 11, 24, 18, 0).timestamp()  # Tue 18:00
        _ud, _g, shared, tuner, hub = self._stack(
            [{"name": "Gamma Wake", "ip": "127.0.0.1", "port": g.port, "type": "gamma", "enabled": True, "poll_interval_sec": 30.0}],
            clock=lambda: now_t,
        )
        shared.set_enabled("Gamma Wake", True)
        import threading
        stop_ev = threading.Event()
        wake_ev = tuner._wake

        def _sleep(sec):
            if stop_ev.is_set():
                raise KeyboardInterrupt()
            wake_ev.wait(max(0.0, float(sec)))
            wake_ev.clear()
            if stop_ev.is_set():
                raise KeyboardInterrupt()

        tuner.sleeper = _sleep
        done = threading.Event()

        def _bg():
            try:
                tuner.run(once=False)
            except KeyboardInterrupt:
                pass
            finally:
                done.set()

        th = threading.Thread(target=_bg, daemon=True)
        th.start()
        try:
            # Wait briefly so the initial poll finishes and the thread enters its 30s sleep
            time.sleep(0.25)
            self.assertEqual(g.state.frequency, 850.0)
            t0 = time.monotonic()
            st, resp, _ = self._req(
                shared,
                "POST",
                "/api/nk/settings",
                {
                    "settings": {
                        "schedule": {
                            "enabled": True,
                            "windows": [
                                {"name": "Peak", "enabled": True, "days": [1], "start": "17:00", "end": "21:00", "devices": [], "axe_max_freq_mhz": 600}
                            ],
                        }
                    }
                },
            )
            self.assertEqual(st, 200)
            self.assertTrue(resp["ok"])
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline and g.state.frequency > 600.0:
                time.sleep(0.05)
            self.assertEqual(g.state.frequency, 600.0)
            self.assertLess(time.monotonic() - t0, 2.5)
        finally:
            stop_ev.set()
            tuner.wake()
            done.wait(timeout=2.0)

    def test_gamma_tou_enforces_and_restores_when_autotune_off(self):
        # Simulate v1.15 default: global autotune OFF and device autotune OFF.
        g = MockMiner(
            MinerState(
                name="Gamma Off",
                kind="gamma",
                frequency=850.0,
                actual_frequency=850.0,
                temp=50.0,
                vr_temp=50.0,
                core_voltage=1240,
            ),
            port=0,
        ).start()
        self.mocks.append(g)
        now_t = [_dt(2026, 11, 24, 18, 0).timestamp()]  # Tue 18:00 (inside 17:00-21:00)
        _ud, _g, shared, tuner, hub = self._stack(
            [{
                "name": "Gamma Off",
                "ip": "127.0.0.1",
                "port": g.port,
                "type": "gamma",
                "enabled": False,
                "min_freq": 400.0,
                "max_freq": 950.0,
                "asic_target_c": 60.0,
                "vr_target_c": 75.0,
                "cooldown_sec": 300.0,
            }],
            clock=lambda: now_t[0],
        )
        shared.set_autotune_enabled(False)
        shared.set_enabled("Gamma Off", False)

        # Save TOU schedule capping AxeOS to 600 MHz
        st, resp, _ = self._req(
            shared,
            "POST",
            "/api/nk/settings",
            {
                "settings": {
                    "schedule": {
                        "enabled": True,
                        "windows": [
                            {"name": "Peak", "enabled": True, "days": [1], "start": "17:00", "end": "21:00", "devices": [], "axe_max_freq_mhz": 600}
                        ],
                    }
                }
            },
        )
        self.assertEqual(st, 200)
        self.assertTrue(resp["ok"])

        # Dry-run safety check: does NOT write PATCH /api/system, logs dry_run=True with source='schedule'
        shared.set_dry_run(True)
        tuner.run(once=True)
        self.assertEqual(g.state.frequency, 850.0)
        self.assertEqual([p for p in g.state.patches if isinstance(p.get("body"), dict) and "frequency" in p["body"]], [])
        dry_logs = [e for e in list(hub.tuning_log.entries) if e.get("device") == "Gamma Off" and e.get("source") == "schedule"]
        self.assertTrue(dry_logs)
        self.assertTrue(dry_logs[-1]["dry_run"])

        # Live mode with autotune still OFF: immediately sends PATCH /api/system with frequency=600
        shared.set_dry_run(False)
        tuner.run(once=True)
        self.assertEqual(g.state.frequency, 600.0)
        freq_patches = [p for p in g.state.patches if isinstance(p.get("body"), dict) and "frequency" in p["body"]]
        self.assertTrue(freq_patches)
        self.assertEqual(freq_patches[-1]["path"], "/api/system")
        self.assertEqual(freq_patches[-1]["body"]["frequency"], 600)
        self.assertEqual(freq_patches[-1]["body"].get("overclockEnabled"), 1)
        sched_logs = [e for e in list(hub.tuning_log.entries) if e.get("device") == "Gamma Off" and e.get("source") == "schedule" and not e.get("dry_run")]
        self.assertTrue(sched_logs)
        self.assertEqual(sched_logs[-1]["new"], 600.0)

        # Window ends at 21:05: if temp is above target (65C > 60C), safety guard defers restore
        now_t[0] = _dt(2026, 11, 24, 21, 5).timestamp()
        g.state.temp = 65.0
        tuner.run(once=True)
        self.assertEqual(g.state.frequency, 600.0)

        # Once temp is safe (50C <= 60C), pre-peak frequency 850 MHz is restored via PATCH /api/system
        g.state.temp = 50.0
        tuner.run(once=True)
        self.assertEqual(g.state.frequency, 850.0)
        restored = [e for e in list(hub.tuning_log.entries) if e.get("device") == "Gamma Off" and e.get("source") == "schedule" and not e.get("dry_run")]
        self.assertEqual(restored[-1]["new"], 850.0)
        self.assertIn("restoring pre-peak frequency", restored[-1]["reason"])

    def test_gamma_tou_enforces_and_resumes_when_autotune_on(self):
        g = MockMiner(
            MinerState(
                name="Gamma On",
                kind="gamma",
                frequency=850.0,
                actual_frequency=850.0,
                temp=48.0,
                vr_temp=48.0,
                core_voltage=1240,
            ),
            port=0,
        ).start()
        self.mocks.append(g)
        now_t = [_dt(2026, 11, 24, 18, 0).timestamp()]
        _ud, _g, shared, tuner, hub = self._stack(
            [{
                "name": "Gamma On",
                "ip": "127.0.0.1",
                "port": g.port,
                "type": "gamma",
                "enabled": True,
                "min_freq": 400.0,
                "max_freq": 950.0,
                "step_mhz": 25.0,
                "cool_polls_required": 1,
                "cooldown_sec": 0.0,
                "error_target_pct": 1.0,
                "error_low_pct": 0.5,
                "error_high_pct": 2.0,
            }],
            clock=lambda: now_t[0],
        )
        shared.set_autotune_enabled(True)
        shared.set_enabled("Gamma On", True)
        hub.settings.update({
            "schedule": {
                "enabled": True,
                "windows": [
                    {"name": "Peak", "enabled": True, "days": [1], "start": "17:00", "end": "21:00", "devices": [], "axe_max_freq_mhz": 600}
                ],
            }
        })
        tuner.run(once=True)
        self.assertEqual(g.state.frequency, 600.0)
        sched_logs = [e for e in list(hub.tuning_log.entries) if e.get("device") == "Gamma On" and e.get("source") == "schedule"]
        self.assertTrue(sched_logs)

        # Window ends with autotune ON: tuner resumes normal stepping (not jumping back to 850)
        now_t[0] = _dt(2026, 11, 24, 21, 5).timestamp()
        tuner.run(once=True)
        tuner.run(once=True)
        self.assertGreater(g.state.frequency, 600.0)
        self.assertLess(g.state.frequency, 850.0)

    def test_gamma_tou_matches_hostname_alias_and_ui_warns_empty_axe_cap(self):
        g = MockMiner(
            MinerState(
                name="bitaxe-601",
                kind="gamma",
                frequency=800.0,
                actual_frequency=800.0,
                temp=50.0,
                vr_temp=50.0,
                core_voltage=1240,
            ),
            port=0,
        ).start()
        self.mocks.append(g)
        now_t = _dt(2026, 11, 24, 18, 0).timestamp()
        _ud, _g, shared, tuner, hub = self._stack(
            [{"name": "Gamma Living Room", "ip": "127.0.0.1", "port": g.port, "type": "gamma", "enabled": False}],
            clock=lambda: now_t,
        )
        shared.set_autotune_enabled(False)
        shared.set_enabled("Gamma Living Room", False)
        # Window targets the Gamma's AxeOS hostname 'bitaxe-601' instead of card name 'Gamma Living Room'
        hub.settings.update({
            "schedule": {
                "enabled": True,
                "windows": [
                    {"name": "Peak", "enabled": True, "days": [1], "start": "17:00", "end": "21:00", "devices": ["bitaxe-601"], "axe_max_freq_mhz": 575}
                ],
            }
        })
        tuner.run(once=True)
        self.assertEqual(g.state.frequency, 575.0)

        # UI warns if a TOU window leaves Axe freq cap empty, and TOU temp-cap input is removed
        st, html_bytes, _ = self._req(shared, "GET", "/")
        self.assertEqual(st, 200)
        html = html_bytes.decode("utf-8")
        self.assertIn("has no Axe freq cap", html)
        self.assertNotIn("axe_asic_target_c", html)

    def test_gamma_601_tou_realistic_e2e_with_autotune_on_and_high_errors(self):
        # Realistic user scenario (t104u):
        # - Bitaxe Gamma 601 at 850 MHz with autotune ON (global + device)
        # - Moderate/high HW error (2.5% > 1.0% max) so decide() wants voltage_up (+20 mV severe-error path)
        # - Inside normal cooldown (last_change_ts = now - 10s)
        # - Antminer S19 Pro with Braiins OS+ alongside it
        # - Active TOU window with both Axe freq cap ("600" string/num) and Braiins power target (900 W)
        g = MockMiner(
            MinerState(
                name="Gamma 601",
                kind="gamma",
                frequency=850.0,
                actual_frequency=850.0,
                temp=52.0,
                vr_temp=54.0,
                core_voltage=1220,
                error_percentage=2.5,
            ),
            port=0,
        ).start()
        cg = MockCGMiner(
            CGMinerState(firmware_kind="braiins", ghs_5s=95000.0, board_temps=[55.0, 56.0, 54.0]),
            port=0,
        ).start()
        bos_state = BraiinsMockState(username="root", password="secret", power_target_w=1200, min_w=700, max_w=1600)
        grpc_mock = MockBraiinsGrpc(bos_state, port=0).start()
        rest_mock = MockBraiinsRest(bos_state, port=0).start()
        self.mocks.extend([g, cg, grpc_mock, rest_mock])

        now_t = [_dt(2026, 11, 24, 18, 0).timestamp()]  # Tue 18:00
        _ud, _g, shared, tuner, hub = self._stack(
            [
                {
                    "name": "Gamma 601",
                    "ip": "127.0.0.1",
                    "port": g.port,
                    "type": "gamma",
                    "enabled": True,
                    "min_freq": 400.0,
                    "max_freq": 850.0,
                    "cooldown_sec": 300.0,
                    "error_high_pct": 1.0,
                },
                {
                    "name": "S19-Braiins",
                    "ip": "127.0.0.1",
                    "type": "antminer",
                    "cgminer_port": cg.port,
                    "port": rest_mock.port,
                    "bos_grpc_port": grpc_mock.port,
                    "bos_username": "root",
                    "bos_password": "secret",
                    "bos_power_mgmt": True,
                    "bos_min_watts": 700,
                    "bos_max_watts": 1600,
                },
            ],
            clock=lambda: now_t[0],
        )
        shared.set_autotune_enabled(True)
        shared.set_enabled("Gamma 601", True)
        # Put Gamma inside its 300s cooldown from an earlier change
        tuner.states["Gamma 601"].last_change_ts = now_t[0] - 10.0
        tuner.states["S19-Braiins"].bos_last_target_w = 1200

        # Also verify string "600" in schedule_limits coerces to int 600
        s_str = {"schedule": {"enabled": True, "windows": [
            {"name": "Peak", "enabled": True, "days": [1], "start": "17:00", "end": "21:00", "devices": [], "axe_max_freq_mhz": "600", "bos_power_target_w": "900"}
        ]}}
        lim_g = miner_hearth.schedule_limits(s_str, "Gamma 601", now_t[0])
        self.assertEqual(lim_g["max_freq"], 600)
        self.assertIsInstance(lim_g["max_freq"], int)

        st, resp, _ = self._req(shared, "POST", "/api/nk/settings", {"settings": s_str})
        self.assertEqual(st, 200)
        self.assertTrue(resp["ok"])

        # Next poll must immediately cap BOTH Gamma 601 (<= 600 MHz) and S19-Braiins (<= 900 W)
        tuner.run(once=True)
        self.assertLessEqual(g.state.frequency, 600.0)
        self.assertEqual(g.state.frequency, 600.0)
        freq_patches = [p for p in g.state.patches if isinstance(p.get("body"), dict) and "frequency" in p["body"]]
        self.assertTrue(freq_patches)
        self.assertEqual(freq_patches[-1]["path"], "/api/system")
        self.assertEqual(freq_patches[-1]["body"]["frequency"], 600)
        self.assertEqual(bos_state.power_target_w, 900)
        sched_logs = [e for e in list(hub.tuning_log.entries) if e.get("device") == "Gamma 601" and e.get("source") == "schedule"]
        self.assertTrue(sched_logs)

        # Edge cases on apply_schedule_cap:
        # (a) emergency_down (850 MHz @ 63C) must STILL clamp new_freq down to <= 600 MHz
        cfg_g = tuner.states["Gamma 601"].cfg
        sched_g = {"max_freq": 600, "bos_power_target_w": 900, "windows": ["Peak"]}
        dcfg_g = autotune.schedule_adjusted_cfg(cfg_g, sched_g)
        hot_r = reading(frequency=850.0, asic_temp=63.0, vr_temp=54.0, core_voltage=1240)
        emerg_dec = autotune.apply_schedule_cap(autotune.decide(hot_r, dcfg_g, 0, error_pct=2.5), hot_r, cfg_g, dcfg_g, sched_g)
        self.assertEqual(emerg_dec.action, "emergency_down")
        self.assertEqual(emerg_dec.new_freq, 600.0)
        self.assertEqual(emerg_dec.new_voltage, 1210)
        self.assertTrue(emerg_dec.bypass_cooldown)

        # (b) Even if cfg.max_freq already equals cap (dcfg is cfg), freq > cap still clamps down immediately
        cfg_eq = autotune.dataclasses.replace(cfg_g, max_freq=600.0)
        dcfg_eq = autotune.schedule_adjusted_cfg(cfg_eq, sched_g)
        self.assertIs(dcfg_eq, cfg_eq)
        hold_r = reading(frequency=850.0, asic_temp=52.0, vr_temp=54.0, core_voltage=1220)
        cap_dec = autotune.apply_schedule_cap(autotune.decide(hold_r, dcfg_eq, 0, error_pct=2.5), hold_r, cfg_eq, dcfg_eq, sched_g)
        self.assertEqual(cap_dec.action, "step_down")
        self.assertEqual(cap_dec.new_freq, 600.0)
        self.assertTrue(cap_dec.bypass_cooldown)

    def test_tou_all_machines_wildcard_and_braiins_axe_parity(self):
        # Wildcard strings ("All Machines", "all", "*", etc.) normalize to [] and match
        # both AxeOS and Braiins devices identically across TOU, Quiet, and Restarts.
        now_t = [_dt(2026, 11, 24, 17, 0).timestamp()]  # Tue 17:00
        for raw_devs in ([], ["All Machines"], ["all"], [" ALL MACHINES "], ["*"], ["all devices"]):
            w = miner_hearth.normalize_window({
                "name": "Peak", "enabled": True, "days": [1],
                "start": "16:00", "end": "18:45",
                "devices": raw_devs, "axe_max_freq_mhz": 625, "bos_power_target_w": 950,
            })
            self.assertEqual(w["devices"], [], f"Failed for {raw_devs!r}")
            s = {"schedule": {"enabled": True, "windows": [
                {"name": "Peak", "enabled": True, "days": [1], "start": "16:00", "end": "18:45",
                 "devices": raw_devs, "axe_max_freq_mhz": 625, "bos_power_target_w": 950}
            ]}}
            lim_axe = miner_hearth.schedule_limits(s, "Gamma-Alpha", now_t[0])
            lim_bos = miner_hearth.schedule_limits(s, "S19-Beta", now_t[0])
            self.assertEqual(lim_axe["max_freq"], 625)
            self.assertEqual(lim_bos["bos_power_target_w"], 950)
            qs = {"quiet": {"enabled": True, "windows": [
                {"name": "Q", "enabled": True, "days": [1], "start": "16:00", "end": "18:45",
                 "devices": raw_devs, "fan_max_pct": 45, "axe_max_freq_mhz": 625}
            ]}}
            self.assertIsNotNone(miner_hearth.quiet_limits(qs, "Gamma-Alpha", now_t[0]))

        # Non-matching explicit filter rejects both AxeOS and Braiins equally
        s_other = {"schedule": {"enabled": True, "windows": [
            {"name": "Peak", "enabled": True, "days": [1], "start": "16:00", "end": "18:45",
             "devices": ["Some-Other-Rig"], "axe_max_freq_mhz": 625, "bos_power_target_w": 950}
        ]}}
        self.assertIsNone(miner_hearth.schedule_limits(s_other, "Gamma-Alpha", now_t[0]))
        self.assertIsNone(miner_hearth.schedule_limits(s_other, "S19-Beta", now_t[0]))

        # End-to-end: saving "devices": ["All Machines"] enforces 625 MHz on Gamma-Alpha and
        # keeps it capped across multiple subsequent cool polls (never stepping back up to 850 MHz),
        # while capping S19-Beta to 950 W.
        g = MockMiner(
            MinerState(name="Gamma-Alpha", kind="gamma", frequency=850.0, actual_frequency=850.0,
                       temp=44.0, vr_temp=52.0, core_voltage=1220, error_percentage=0.05),
            port=0,
        ).start()
        cg = MockCGMiner(
            CGMinerState(firmware_kind="braiins", ghs_5s=95000.0, board_temps=[55.0, 56.0, 54.0]),
            port=0,
        ).start()
        bos_state = BraiinsMockState(username="root", password="secret", power_target_w=1200, min_w=700, max_w=1600)
        grpc_mock = MockBraiinsGrpc(bos_state, port=0).start()
        rest_mock = MockBraiinsRest(bos_state, port=0).start()
        self.mocks.extend([g, cg, grpc_mock, rest_mock])

        _ud, _g, shared, tuner, _hub = self._stack(
            [
                {"name": "Gamma-Alpha", "ip": "127.0.0.1", "port": g.port, "type": "gamma",
                 "enabled": True, "cool_polls_required": 2, "cooldown_sec": 0.0},
                {"name": "S19-Beta", "ip": "127.0.0.1", "type": "antminer", "cgminer_port": cg.port,
                 "port": rest_mock.port, "bos_grpc_port": grpc_mock.port,
                 "bos_username": "root", "bos_password": "secret",
                 "bos_power_mgmt": True, "bos_min_watts": 700, "bos_max_watts": 1600},
            ],
            clock=lambda: now_t[0],
        )
        shared.set_autotune_enabled(True)
        shared.set_enabled("Gamma-Alpha", True)
        tuner.states["S19-Beta"].bos_last_target_w = 1200

        st, resp, _ = self._req(
            shared,
            "POST",
            "/api/nk/settings",
            {
                "settings": {
                    "schedule": {
                        "enabled": True,
                        "windows": [{
                            "name": "Peak hours", "enabled": True, "days": [0, 1, 2, 3, 4, 5, 6],
                            "start": "16:00", "end": "18:45",
                            "devices": ["All Machines"],
                            "axe_max_freq_mhz": 625, "bos_power_target_w": 950,
                        }],
                    }
                }
            },
        )
        self.assertEqual(st, 200)
        self.assertEqual(resp["settings"]["schedule"]["windows"][0]["devices"], [])

        # Run 6 polls across the active window: Gamma must drop to 625 and stay at 625
        for _ in range(6):
            now_t[0] += 60.0
            tuner.run(once=True)
            self.assertEqual(g.state.frequency, 625.0)
        self.assertEqual(bos_state.power_target_w, 950)

        # UI uses placeholder (never a pre-filled "All Machines" value) and shows Applies-to note
        st_h, html_bytes, _ = self._req(shared, "GET", "/")
        self.assertEqual(st_h, 200)
        html = html_bytes.decode("utf-8")
        self.assertIn('placeholder="Leave blank for all machines', html)
        self.assertIn("function nkParseDeviceFilter(", html)
        self.assertIn("function nkWinAppliesInfo(", html)
        self.assertIn("nk-win-applies", html)


class V115ReleaseAndAutotuneDefaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.hubs: list[miner_hearth.FeatureHub] = []

    def tearDown(self):
        for h in self.hubs:
            h.stop()
        self.tmp.cleanup()

    def _req(self, shared, method, path, body=None, ip="127.0.0.1", headers=None):
        return V18MinerHearthTests._req(self, shared, method, path, body=body, ip=ip, headers=headers)

    def test_fresh_install_and_new_devices_default_autotune_off_and_persist_across_restarts(self):
        from dashboard import SharedState

        cfg_path = self.dir / "config.json"
        ud_path = self.dir / "userdata" / "devices.json"
        state_path = self.dir / "autotune-state.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "log_dir": str(self.dir / "logs"),
                    "state_file": str(state_path),
                    "userdata_file": str(ud_path),
                    "devices": [],
                }
            ),
            encoding="utf-8",
        )
        # 1) Fresh startup (no userdata): global autotune_enabled must be OFF
        raw1 = autotune.load_raw_config(cfg_path)
        cfg1 = autotune.parse_config(raw1, self.dir, config_path=cfg_path)
        self.assertFalse(cfg1.autotune_enabled)
        shared1 = SharedState(
            [d.name for d in cfg1.devices],
            cfg1.state_file,
            dry_run=bool(cfg1.dry_run),
            device_defaults={},
            autotune_enabled=cfg1.autotune_enabled,
        )
        tuner1 = autotune.Tuner(cfg1, shared=shared1)
        shared1.attach_tuner(tuner1)
        self.assertFalse(shared1.is_autotune_enabled())
        st_code, st_body, _ = self._req(shared1, "GET", "/api/status")
        self.assertEqual(st_code, 200)
        self.assertFalse(st_body["autotune_enabled"])

        # 2) Newly added devices (manual/LAN add Gamma + Tier-2 Avalon + Tier-2 Generic) ALL start with autotune OFF
        for payload in (
            {"name": "Gamma Fresh", "ip": "127.0.0.10", "type": "gamma"},
            {"name": "Avalon Fresh", "ip": "127.0.0.11", "type": "antminer", "family": "avalon"},
            {"name": "Custom Prom", "ip": "127.0.0.12", "type": "antminer", "family": "generic-prometheus"},
        ):
            code, res, _ = self._req(shared1, "POST", "/api/devices/add", payload)
            self.assertEqual(code, 201, res)
            self.assertFalse(res["enabled"])
            self.assertFalse(shared1.is_enabled(payload["name"]))

        # Even if global autotune is turned ON first, newly added devices STILL default to OFF
        code_on, res_on, _ = self._req(shared1, "POST", "/api/autotune", {"enabled": True})
        self.assertEqual(code_on, 200)
        self.assertTrue(res_on["autotune_enabled"])
        code_add2, res_add2, _ = self._req(shared1, "POST", "/api/devices/add", {"name": "Gamma Two", "ip": "127.0.0.13", "type": "gamma"})
        self.assertEqual(code_add2, 201)
        self.assertFalse(res_add2["enabled"])

        # Turn ON Gamma Fresh, keep Gamma Two OFF; restart -> both global ON and per-machine states persist
        code_s, _, _ = self._req(shared1, "POST", "/api/device/Gamma%20Fresh/start", {})
        self.assertEqual(code_s, 200)

        cfg2 = autotune.parse_config(autotune.load_raw_config(cfg_path), self.dir, config_path=cfg_path)
        defaults2 = {d.name: (False if not autotune.is_tunable(d) else bool(d.enabled_default)) for d in cfg2.devices}
        shared2 = SharedState([d.name for d in cfg2.devices], cfg2.state_file, device_defaults=defaults2, autotune_enabled=cfg2.autotune_enabled)
        tuner2 = autotune.Tuner(cfg2, shared=shared2)
        shared2.attach_tuner(tuner2)
        self.assertTrue(shared2.is_autotune_enabled())
        self.assertTrue(tuner2._enabled("Gamma Fresh"))
        self.assertFalse(tuner2._enabled("Gamma Two"))

        # Turn global autotune OFF -> persists as OFF across restart without erasing Gamma Fresh's ON flag
        code_off, res_off, _ = self._req(shared2, "POST", "/api/autotune", {"enabled": False})
        self.assertEqual(code_off, 200)
        self.assertFalse(res_off["autotune_enabled"])
        self.assertFalse(tuner2._enabled("Gamma Fresh"))

        cfg3 = autotune.parse_config(autotune.load_raw_config(cfg_path), self.dir, config_path=cfg_path)
        defaults3 = {d.name: (False if not autotune.is_tunable(d) else bool(d.enabled_default)) for d in cfg3.devices}
        shared3 = SharedState([d.name for d in cfg3.devices], cfg3.state_file, device_defaults=defaults3, autotune_enabled=cfg3.autotune_enabled)
        tuner3 = autotune.Tuner(cfg3, shared=shared3)
        shared3.attach_tuner(tuner3)
        self.assertFalse(shared3.is_autotune_enabled())
        self.assertFalse(tuner3._enabled("Gamma Fresh"))

        # Re-enabling global autotune brings Gamma Fresh back ON while Gamma Two stays OFF
        self._req(shared3, "POST", "/api/autotune", {"enabled": True})
        self.assertTrue(tuner3._enabled("Gamma Fresh"))
        self.assertFalse(tuner3._enabled("Gamma Two"))

    def test_upgrade_from_v114_userdata_preserves_saved_autotune_states(self):
        from dashboard import SharedState

        cfg_path = self.dir / "config.json"
        ud_path = self.dir / "userdata" / "devices.json"
        state_path = self.dir / "autotune-state.json"
        cfg_path.write_text(json.dumps({"state_file": str(state_path), "userdata_file": str(ud_path), "devices": []}), encoding="utf-8")
        ud_path.parent.mkdir(parents=True, exist_ok=True)
        # Simulate v1.14 userdata without "autotune_enabled" key
        ud_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "app_version": "1.14",
                    "devices": [
                        {"name": "Old On", "ip": "192.168.1.50", "type": "gamma", "enabled": True},
                        {"name": "Old Off", "ip": "192.168.1.51", "type": "gamma", "enabled": False},
                    ],
                }
            ),
            encoding="utf-8",
        )
        cfg = autotune.parse_config(autotune.load_raw_config(cfg_path), self.dir, config_path=cfg_path)
        self.assertTrue(cfg.autotune_enabled)
        defaults = {d.name: (False if not autotune.is_tunable(d) else (bool(d.enabled_default) if d.enabled_default is not None else False)) for d in cfg.devices}
        shared = SharedState([d.name for d in cfg.devices], cfg.state_file, device_defaults=defaults, autotune_enabled=cfg.autotune_enabled)
        self.assertTrue(shared.is_autotune_enabled())
        self.assertTrue(shared.is_enabled("Old On"))
        self.assertFalse(shared.is_enabled("Old Off"))

    def test_tailscale_bind_mode_pin_guard_and_ui_copy(self):
        from dashboard import SharedState

        # 1) tailscale_ip_addresses detects only 100.64.0.0/10
        orig_gai = miner_hearth.socket.getaddrinfo
        orig_lan = miner_hearth.lan_ip_addresses
        try:
            miner_hearth.socket.getaddrinfo = lambda *_a, **_kw: [
                (miner_hearth.socket.AF_INET, 0, 0, "", ("192.168.1.20", 0)),
                (miner_hearth.socket.AF_INET, 0, 0, "", ("100.99.42.10", 0)),
                (miner_hearth.socket.AF_INET, 0, 0, "", ("8.8.8.8", 0)),
            ]
            miner_hearth.lan_ip_addresses = lambda: ["192.168.1.20"]
            self.assertEqual(miner_hearth.tailscale_ip_addresses(), ["100.99.42.10"])
        finally:
            miner_hearth.socket.getaddrinfo = orig_gai
            miner_hearth.lan_ip_addresses = orig_lan

        # 2) Tailscale-only bind mode surfaces clear error when no tailnet interface exists
        shared = SharedState([], self.dir / "state.json")
        hub = miner_hearth.FeatureHub(self.dir / "userdata", fetch=FakeEsplora())
        hub.attach(None, shared)
        self.hubs.append(hub)
        orig_ts = miner_hearth.tailscale_ip_addresses
        try:
            miner_hearth.tailscale_ip_addresses = lambda: []
            hub.update_settings({"lan": {"enabled": True, "bind_mode": "tailscale", "port": 8089, "pin": "1234", "read_only": True}})
            info = hub.lan_info()
            self.assertEqual(info["bind_mode"], "tailscale")
            self.assertFalse(info["running"])
            self.assertIn("100.64.0.0/10", info["error"])
        finally:
            miner_hearth.tailscale_ip_addresses = orig_ts

        # 3) AccessGuard enforces PIN on 100.x Tailscale client AND on tailscale serve loopback proxy
        shared.hub = hub
        hub.update_settings({"lan": {"enabled": True, "bind_mode": "all", "port": 8081, "pin": "4321", "read_only": True}})
        lan_cfg = {"enabled": True, "bind_mode": "tailscale", "port": 8081, "pin": "4321", "read_only": True}
        st_deny, _, _ = self._req(shared, "GET", "/api/status", ip="100.88.1.2")
        self.assertEqual(st_deny, 401)
        st_ok, _, _ = self._req(shared, "GET", "/api/status", ip="100.88.1.2", headers={"X-NK-PIN": "4321"})
        self.assertEqual(st_ok, 200)
        st_ts_proxy_deny, _, _ = self._req(shared, "GET", "/api/status", ip="127.0.0.1", headers={"Tailscale-User-Login": "alice@example.ts.net"})
        self.assertEqual(st_ts_proxy_deny, 401)
        st_ts_proxy_ok, _, _ = self._req(shared, "GET", "/api/status", ip="127.0.0.1", headers={"Tailscale-User-Login": "alice@example.ts.net", "X-NK-PIN": "4321"})
        self.assertEqual(st_ts_proxy_ok, 200)
        # Re-test directly with AccessGuard
        guard = miner_hearth.AccessGuard()
        self.assertIsNotNone(guard.check("GET", "/api/status", "100.88.1.2", {}, lan_cfg))
        self.assertIsNone(guard.check("GET", "/api/status", "100.88.1.2", {"X-NK-PIN": "4321"}, lan_cfg))

        # 4) UI HTML checks: Donate BTC, no 'formerly Miner Autotune', global autotune button, Tailscale select
        code_h, html_b, _ = self._req(shared, "GET", "/")
        self.assertEqual(code_h, 200)
        html = html_b.decode("utf-8")
        self.assertIn("Donate BTC:", html)
        self.assertNotIn("formerly miner autotune", html.lower())
        self.assertNotIn("formerly miner-autotune", html.lower())
        self.assertIn('id="autoBadge"', html)
        self.assertIn('id="autoToggle"', html)
        self.assertIn('id="nk_l_bind"', html)
        self.assertIn("tailscale serve", html)


class V1152SessionBestAndBraiinsFixesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _hub(self, now_box):
        hub = miner_hearth.FeatureHub(
            userdata_dir=self.dir,
            fetch=FakeEsplora(),
            clock=lambda: now_box[0],
        )
        hub.settings.update({"blocks": {"enabled": False}})
        return hub

    def test_session_best_resets_on_pool_switch_failover_failback_and_persists_restart(self):
        now = [1_700_000_000.0]
        hub = self._hub(now)
        cfg = antminer_cfg(name="Ant-A")

        def mk_ant_reading(best_diff, uptime, pools, last_diff=None, estimated=False):
            return reading(
                best_diff=best_diff,
                best_session_diff=None,
                uptime_sec=uptime,
                pools=pools,
                pool_url=pools[0]["url"],
                pool_user=pools[0]["user"],
                last_submitted_diff=last_diff,
                last_share_estimated=estimated,
            )

        # 1) Initial mining on Pool 0: summary Best Share is 5.13e10 (since power-on)
        pools0 = [
            {"pool": 0, "url": "stratum+tcp://pool-a.example:3333", "user": "worker.a", "stratum_active": True, "best_share": 5.13e10},
            {"pool": 1, "url": "stratum+tcp://pool-b.example:3333", "user": "worker.b", "stratum_active": False, "best_share": 0.0},
        ]
        r1 = mk_ant_reading(5.13e10, 50000, pools0, last_diff=1.2e6)
        hub.on_poll(cfg, r1, now[0])
        self.assertEqual(r1.best_session_diff, 5.13e10)
        self.assertEqual(hub.session_best.get("Ant-A")["source"], "api")
        self.assertEqual(hub.session_best.get("Ant-A")["all_time_best"], 5.13e10)

        # 2) Failover to Pool 1 while CGMiner Best Share STILL reports 5.13e10 -> must reset!
        now[0] += 30
        pools1 = [
            {"pool": 1, "url": "stratum+tcp://pool-b.example:3333", "user": "worker.b", "stratum_active": True, "best_share": 0.0},
            {"pool": 0, "url": "stratum+tcp://pool-a.example:3333", "user": "worker.a", "stratum_active": False, "best_share": 5.13e10},
        ]
        r2 = mk_ant_reading(5.13e10, 50030, pools1, last_diff=9.9e7, estimated=False)
        hub.on_poll(cfg, r2, now[0])
        self.assertIsNone(r2.best_session_diff)
        sb2 = hub.session_best.get("Ant-A")
        self.assertIsNone(sb2["session_best"])
        self.assertEqual(sb2["source"], "pool_switch")
        self.assertEqual(sb2["all_time_best"], 5.13e10)

        # 3) Restart NK (new FeatureHub loading session_best.json); Pool 1 gets a 2.4M share,
        #    while summary Best Share STILL says 5.13e10 -> must NOT revert to 5.13e10!
        now[0] += 30
        hub2 = self._hub(now)
        pools1_b = [
            {"pool": 1, "url": "stratum+tcp://pool-b.example:3333", "user": "worker.b", "stratum_active": True, "best_share": 2.4e6},
            {"pool": 0, "url": "stratum+tcp://pool-a.example:3333", "user": "worker.a", "stratum_active": False, "best_share": 5.13e10},
        ]
        r3 = mk_ant_reading(5.13e10, 50060, pools1_b, last_diff=5.0e5)
        hub2.on_poll(cfg, r3, now[0])
        self.assertEqual(r3.best_session_diff, 2.4e6)
        self.assertEqual(hub2.session_best.get("Ant-A")["session_best"], 2.4e6)
        self.assertEqual(hub2.session_best.get("Ant-A")["source"], "pool_switch")

        # 4) Failback to Pool 0 -> must reset again and ignore Pool 0's stale 5.13e10 best_share!
        now[0] += 30
        r4 = mk_ant_reading(5.13e10, 50090, pools0, last_diff=1.0e5)
        hub2.on_poll(cfg, r4, now[0])
        self.assertIsNone(r4.best_session_diff)

        # New real share on Pool 0 updates post-switch session best without reverting to 5.13e10
        now[0] += 30
        r5 = mk_ant_reading(5.13e10, 50120, pools0, last_diff=3.1e6, estimated=False)
        hub2.on_poll(cfg, r5, now[0])
        self.assertEqual(r5.best_session_diff, 3.1e6)

        # 5) Worker edit on same pool index/URL also resets session best
        now[0] += 30
        pools0_new_worker = [
            {"pool": 0, "url": "stratum+tcp://pool-a.example:3333", "user": "worker.a2", "stratum_active": True, "best_share": 0.0},
        ]
        r6 = mk_ant_reading(5.13e10, 50150, pools0_new_worker, last_diff=4.0e6)
        hub2.on_poll(cfg, r6, now[0])
        self.assertIsNone(r6.best_session_diff)

        # 6) Miner reboot (uptime drops) clears pool_switched and returns to normal API tracking
        now[0] += 30
        pools_reboot = [
            {"pool": 0, "url": "stratum+tcp://pool-a.example:3333", "user": "worker.a2", "stratum_active": True, "best_share": 1.5e6},
        ]
        r7 = mk_ant_reading(1.5e6, 25, pools_reboot, last_diff=1.5e6)
        hub2.on_poll(cfg, r7, now[0])
        self.assertEqual(r7.best_session_diff, 1.5e6)
        self.assertEqual(hub2.session_best.get("Ant-A")["source"], "api")

    def test_axeos_session_best_resets_on_pool_switch_and_fallback(self):
        now = [1_700_000_000.0]
        hub = self._hub(now)
        cfg = gamma_cfg(name="Gamma-A")

        r1 = reading(
            best_diff=9.0e8,
            best_session_diff=2.4e6,
            best_session_diff_display="2.4M",
            uptime_sec=1200,
            pool_url="pool-a.example",
            pool_port=3333,
            pool_user="worker.1",
            using_fallback_stratum=False,
        )
        hub.on_poll(cfg, r1, now[0])
        self.assertEqual(r1.best_session_diff, 2.4e6)
        self.assertEqual(r1.best_session_diff_display, "2.4M")

        # Failover to fallback stratum -> session_best resets even though AxeOS bestSessionDiff is still 2.4e6
        now[0] += 30
        r2 = reading(
            best_diff=9.0e8,
            best_session_diff=2.4e6,
            best_session_diff_display="2.4M",
            uptime_sec=1230,
            pool_url="pool-a.example",
            pool_port=3333,
            pool_user="worker.1",
            fallback_pool_url="pool-b.example",
            fallback_pool_port=3333,
            fallback_pool_user="worker.fb",
            using_fallback_stratum=True,
        )
        hub.on_poll(cfg, r2, now[0])
        self.assertIsNone(r2.best_session_diff)
        self.assertEqual(r2.best_session_diff_display, "")
        self.assertEqual(hub.session_best.get("Gamma-A")["all_time_best"], 9.0e8)

        # Subsequent real share on fallback updates session_best
        now[0] += 30
        r3 = reading(
            best_diff=9.0e8,
            best_session_diff=2.4e6,
            best_session_diff_display="2.4M",
            uptime_sec=1260,
            pool_url="pool-a.example",
            pool_port=3333,
            pool_user="worker.1",
            fallback_pool_url="pool-b.example",
            fallback_pool_port=3333,
            fallback_pool_user="worker.fb",
            using_fallback_stratum=True,
            last_submitted_diff=1.8e6,
            last_share_estimated=False,
        )
        hub.on_poll(cfg, r3, now[0])
        self.assertEqual(r3.best_session_diff, 1.8e6)

    def test_braiins_low_severity_fixes_write_enabled_power_limit_watt_and_cool_polls_cap(self):
        # 1) Per-chain PowerLimitWatt parsing from Braiins tunerstatus
        parsed = autotune.parse_antminer_readings(
            summary_data={"SUMMARY": [{"Elapsed": 1000, "GHS 5s": 95000.0, "Best Share": 1000}]},
            stats_data={"STATS": [{}, {"BMMiner": "BOSminer", "temp_chip1": 65.0, "frequency": 625.0}]},
            version_data={"VERSION": [{"BOSminer": "24.04", "Type": "Antminer S19 Pro"}]},
            pools_data={"POOLS": []},
            tunerstatus_data={
                "TUNERSTATUS": [{
                    "ApproximateMinerPowerConsumption": 2950,
                    "PowerLimit": 2950,
                    "TunerChainStatus": [
                        {"HashBoardIndex": 0, "Status": "Stable", "PowerLimitWatt": 2950.0, "CurrentFrequency": 625.0},
                    ],
                }]
            },
        )
        self.assertEqual(parsed.tuner_chain_statuses[0]["power_limit_w"], 2950.0)

        # 2) bos_cool_polls capped at bos_cool_polls_required while inside cooldown
        cfg_bos = antminer_cfg(
            name="Rig",
            bos_power_mgmt=True,
            bos_min_watts=2000,
            bos_max_watts=3500,
            bos_step_watts=50,
            bos_chip_target_c=75.0,
            bos_chip_hot_c=82.0,
            bos_cooldown_sec=300.0,
            bos_cool_polls_required=4,
        )
        cool_r = reading(firmware="Braiins OS", chip_temps=[65.0, 66.0, 64.0], power_limit_w=2950.0, hashrate=95000.0)
        streak = 0
        for dt in (10, 20, 30, 40, 50, 60, 70):
            act, _nw, _rsn, streak = autotune.decide_bos_power(
                cool_r, cfg_bos, streak, last_change_ts=1000.0, now_ts=1000.0 + dt, current_target_w=2950
            )
            self.assertEqual(act, "hold")
            self.assertLessEqual(streak, 4)
        self.assertEqual(streak, 4)

        # 3) bos_power_mgmt snapshot/diagnostics: write_enabled=True, clean last_action/last_reason/voltage_reason/firmware_note
        cg = MockCGMiner(
            CGMinerState(
                firmware_kind="braiins",
                power_limit_w=2950.0,
                tuner_mode="PowerTarget",
                chip_temps=[70.0, 71.0, 69.0],
                board_temps=[55.0, 56.0, 54.0],
            ),
            port=0,
        ).start()
        bos_state = BraiinsMockState(username="root", password="secret", power_target_w=2950, min_w=2000, max_w=3500)
        bos_srv = MockBraiinsGrpc(bos_state, port=0).start()
        try:
            gcfg = autotune.GlobalConfig(
                log_dir=self.dir,
                log_file=self.dir / "a.log",
                csv_file=self.dir / "a.csv",
                autotune_enabled=True,
                dry_run=False,
                devices=[
                    autotune.DeviceConfig(
                        name="Ant-BOS",
                        ip="127.0.0.1",
                        type="antminer",
                        cgminer_port=cg.port,
                        bos_power_mgmt=True,
                        bos_grpc_port=bos_srv.port,
                        bos_username="root",
                        bos_password="secret",
                        bos_min_watts=2000,
                        bos_max_watts=3500,
                        bos_step_watts=50,
                        bos_chip_target_c=75.0,
                        bos_chip_hot_c=82.0,
                        bos_cooldown_sec=300.0,
                        bos_cool_polls_required=4,
                        enabled_default=True,
                    )
                ],
            )
            import dashboard as _dash
            shared = _dash.SharedState(["Ant-BOS"], self.dir / "state.json", device_defaults={"Ant-BOS": True}, autotune_enabled=True)
            tuner = autotune.Tuner(gcfg, shared=shared, clock=lambda: 2000.0)
            tuner.poll_device(tuner.states["Ant-BOS"])
            snap = shared.status()["devices"][0]
            self.assertTrue(snap["write_enabled"])
            self.assertEqual(snap["last_action"], "hold")
            self.assertEqual(snap["voltage_reason"], "Braiins power-target mode")
            self.assertEqual(snap["firmware_note"], "Braiins OS+ power-target control active")
            self.assertNotIn("monitor-only", snap["last_reason"].lower())
        finally:
            cg.stop()
            bos_srv.stop()


class V1153ActivePoolSessionBestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _hub(self, now_box):
        hub = miner_hearth.FeatureHub(
            userdata_dir=self.dir,
            fetch=FakeEsplora(),
            clock=lambda: now_box[0],
        )
        hub.settings.update({"blocks": {"enabled": False}})
        return hub

    def test_active_pool_best_share_is_primary_and_ignores_disabled_pools(self):
        now = [1_700_000_000.0]
        hub = self._hub(now)
        cfg = antminer_cfg(name="Ant-Pool")

        # Parse real CGMiner POOLS payload where Pool 0 is Disabled (huge Best Share) and Pool 1 is Stratum Active (3,460,256),
        # while SUMMARY["Best Share"] holds the boot-wide 51.3G high-water mark.
        rd = autotune.parse_antminer_readings(
            summary_data={"SUMMARY": [{"Elapsed": 80000, "GHS 5s": 100000.0, "Best Share": 51_300_000_000}]},
            stats_data={"STATS": [{}, {"BMMiner": "BOSminer", "temp_chip1": 64.0, "frequency": 625.0}]},
            version_data={"VERSION": [{"BOSminer": "24.04", "Type": "Antminer S19 Pro"}]},
            pools_data={
                "POOLS": [
                    {
                        "POOL": 0,
                        "URL": "stratum+tcp://disabled.example:3333",
                        "User": "worker.old",
                        "Status": "Disabled",
                        "Enabled": False,
                        "Stratum Active": False,
                        "Priority": 0,
                        "Best Share": 51_300_000_000,
                    },
                    {
                        "POOL": 1,
                        "URL": "stratum+tcp://pool-c.example:3333",
                        "User": "worker.c",
                        "Status": "Alive",
                        "Enabled": True,
                        "Stratum Active": True,
                        "Priority": 1,
                        "Best Share": 3_460_256,
                        "Last Share Difficulty": 65536,
                    },
                ]
            },
        )
        self.assertTrue(rd.pools[1]["disabled"])
        self.assertFalse(rd.pools[0]["disabled"])
        hub.on_poll(cfg, rd, now[0])
        self.assertEqual(rd.best_session_diff, 3_460_256.0)
        self.assertEqual(rd.best_diff, 51_300_000_000.0)
        sb = hub.session_best.get("Ant-Pool")
        self.assertEqual(sb["session_best"], 3_460_256.0)
        self.assertEqual(sb["all_time_best"], 51_300_000_000.0)
        self.assertEqual(sb["pool_source"], "active_pool")

    def test_brand_new_pool_uses_zero_baseline_and_failback_uses_saved_baseline(self):
        now = [1_700_000_000.0]
        hub = self._hub(now)
        cfg = antminer_cfg(name="Ant-FB")

        def mk(summary_best, uptime, pools, last_diff=None):
            return reading(
                best_diff=summary_best,
                best_session_diff=None,
                uptime_sec=uptime,
                pools=pools,
                pool_url=pools[0]["url"],
                pool_user=pools[0]["user"],
                last_submitted_diff=last_diff,
                last_share_estimated=False,
            )

        # 1) Initial Pool A during this boot: 51.3G
        p_a = [{"pool": 0, "url": "stratum+tcp://pool-a.example:3333", "user": "w.a", "status": "Alive", "stratum_active": True, "best_share": 5.13e10}]
        r1 = mk(5.13e10, 50000, p_a)
        hub.on_poll(cfg, r1, now[0])
        self.assertEqual(r1.best_session_diff, 5.13e10)

        # 2) Switch to brand-new Pool B that ALREADY recorded 128K before NK's first poll -> baseline 0 shows 128K immediately!
        now[0] += 30
        p_b = [
            {"pool": 1, "url": "stratum+tcp://pool-b.example:3333", "user": "w.b", "status": "Alive", "stratum_active": True, "best_share": 128_000.0},
            {"pool": 0, "url": "stratum+tcp://pool-a.example:3333", "user": "w.a", "status": "Dead", "stratum_active": False, "best_share": 5.13e10},
        ]
        r2 = mk(5.13e10, 50030, p_b, last_diff=128_000.0)
        hub.on_poll(cfg, r2, now[0])
        self.assertEqual(r2.best_session_diff, 128_000.0)

        # 3) Failback to Pool A (already seen in this boot, POOLS[0] still 51.3G!) -> resets via saved baseline!
        now[0] += 30
        r3 = mk(5.13e10, 50060, p_a, last_diff=16_384.0)
        hub.on_poll(cfg, r3, now[0])
        self.assertIsNone(r3.best_session_diff)

        # 4) Restart NK; failback protection still holds and ignores stale 51.3G while tracking new 2.1M share
        now[0] += 30
        hub2 = self._hub(now)
        r4 = mk(5.13e10, 50090, p_a, last_diff=2_100_000.0)
        hub2.on_poll(cfg, r4, now[0])
        self.assertEqual(r4.best_session_diff, 2_100_000.0)
        self.assertEqual(hub2.session_best.get("Ant-FB")["all_time_best"], 5.13e10)
