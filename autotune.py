#!/usr/bin/env python3
"""Miner Hearth: automatic ASIC frequency/voltage tuner
for AxeOS miners (Bitaxe-class, NerdQAxe/NerdOCTAxe), plus CGMiner monitoring and
optional Braiins OS+ power-target management for Antminer S19.

Polls AxeOS / NerdOS (ESP-Miner and the NerdQAxe fork) over the LAN:
- Frequency steps up/down from ASIC and VR temperatures (Gamma up to 850 MHz; NerdOCTAxe normal max 800 MHz unless opted in).
- Gamma core voltage is driven by rolling HW error rate (1150-1250 mV).
- NerdOCTAxe is frequency-only (core voltage is never written).
- Antminer S19 Pro is monitor-only via CGMiner TCP port 4028.

Dependencies: Python 3.9+ standard library only.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import gzip
import hmac
import http.client
import json
import logging
import math
import os
import re
import shutil
import signal
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import nk_faults

def _resolve_app_dir() -> Path:
    """Folder that holds config.json, userdata/ and logs/.

    - MINER_AUTOTUNE_HOME env var wins (used by the desktop launcher and tests).
    - A PyInstaller-frozen exe uses the folder that contains the .exe, never the
      temporary _MEIPASS extraction folder, so userdata/ survives updates.
    - Otherwise the folder containing this script (run.bat / python autotune.py).
    """
    env_home = (os.environ.get("MINER_HEARTH_HOME", "") or os.environ.get("MINER_AUTOTUNE_HOME", "")).strip()
    if env_home:
        return Path(env_home).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resolve_bundle_dir() -> Path:
    """Folder that holds read-only files shipped inside the program (VERSION, default config)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


SCRIPT_DIR = _resolve_app_dir()
BUNDLE_DIR = _resolve_bundle_dir()
LOG = logging.getLogger("autotune")


def _load_app_version() -> str:
    # Prefer the VERSION shipped inside the program so a stale VERSION file left
    # next to a newer exe never reports the wrong version.
    candidates = [BUNDLE_DIR / "VERSION", SCRIPT_DIR / "VERSION"]
    # v1.17.1: when this file runs from a live update (userdata/updates/current/app) next to a source install,
    # the installed VERSION sits beside the program that was started.
    main_file = getattr(sys.modules.get("__main__"), "__file__", None)
    if main_file and not getattr(sys, "frozen", False):
        candidates.append(Path(main_file).resolve().parent / "VERSION")
    for vfile in candidates:
        try:
            if vfile.exists():
                val = vfile.read_text(encoding="utf-8").strip()
                if val:
                    return val
        except OSError:
            pass
    return "1.15.5"


def _version_tuple(v: str) -> tuple:
    out = []
    for part in str(v).strip().split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


APP_NAME = "Miner Hearth"
# The version of this program code. A live update replaces the code but not the VERSION file shipped with the
# install, so the running version is the newer of the two; SHIPPED_VERSION is what the installed build says.
PROGRAM_VERSION = "1.17.1"
SHIPPED_VERSION = _load_app_version()
APP_VERSION = max(SHIPPED_VERSION, PROGRAM_VERSION, key=_version_tuple)

# 25 MHz grid used for Gamma voltage coupling and for NerdOCTAxe steps up to 850.
# (Stock AxeOS BM1370 dropdowns stop at 625 MHz; 850 is an overclock, unlocked
# in the API with overclockEnabled=1. NerdOCTAXE-γ firmware lists 525–800 and
# marks 850 as the absolute manual max.)
GAMMA_FREQUENCY_OPTIONS = list(range(400, 851, 25))
NERDOCTAXE_FREQUENCY_OPTIONS = list(range(525, 851, 25))

# Axe autotune defaults (v1.7):
# - 850 MHz cap, 1150-1250 mV core voltage, 1210 mV emergency reset
# - ASIC emergency 62.5C, VR emergency 65C, 50 MHz emergency drop
# - Error target 0%, 0.5-1.0% acceptable, >1.0% out of tolerance
HARD_MAX_FREQ_MHZ = 850.0
NERD_NORMAL_MAX_FREQ_MHZ = 800.0
USERDATA_SCHEMA = 3
DEFAULT_EMERGENCY_LOCKOUT_SEC = 10800.0
THERMAL_STEP_DOWN_SETTLE_SEC = 30.0
SEVERE_ERROR_PCT_FLOOR = 4.0
SEVERE_ERROR_VOLTAGE_COOLDOWN_SEC = 60.0
MIN_VOLTAGE_DROP_UPTIME_SEC = 90.0
HARD_MAX_VOLTAGE_MV = 1250
HARD_MIN_VOLTAGE_MV = 1000
DEFAULT_MAX_FREQ_MHZ = 850
DEFAULT_VOLTAGE_START_MV = 1210
DEFAULT_EMERGENCY_VOLTAGE_MV = 1210
DEFAULT_VOLTAGE_FLOOR_MV = 1150
DEFAULT_VOLTAGE_STEP_MV = 10
DEFAULT_MAX_VOLTAGE_MV = 1250
DEFAULT_ASIC_TARGET_C = 59.0
DEFAULT_ASIC_MAX_C = 62.5
DEFAULT_VR_TARGET_C = 62.0
DEFAULT_VR_MAX_C = 65.0
DEFAULT_EMERGENCY_STEP_MHZ = 50
DEFAULT_ERROR_TARGET_PCT = 0.0
DEFAULT_ERROR_LOW_PCT = 0.5
DEFAULT_ERROR_HIGH_PCT = 1.0
DEFAULT_ERROR_WINDOW_POLLS = 5
DEFAULT_LOW_ERROR_POLLS = 5

GLOBAL_TUNER_KEYS = (
    "max_freq",
    "min_voltage_mv",
    "max_voltage_mv",
    "asic_target_c",
    "asic_max_c",
    "vr_target_c",
    "vr_max_c",
    "emergency_step_mhz",
    "emergency_voltage_mv",
    "error_target_pct",
    "error_low_pct",
    "error_high_pct",
)

DISPLAY_FIELD_DEFS: list[dict[str, str]] = [
    {"key": "fleet_summary", "label": "Fleet Summary Bar", "group": "General"},
    {"key": "recent_logs", "label": "Recent Log Console", "group": "General"},
    {"key": "hashrate", "label": "Hashrate & Sub-averages", "group": "Miner Metrics"},
    {"key": "asic_temp", "label": "ASIC / Chip Temperatures", "group": "Miner Metrics"},
    {"key": "vr_temp", "label": "VR / Board Temperatures", "group": "Miner Metrics"},
    {"key": "fans", "label": "Fan RPM & Speed Bars", "group": "Miner Metrics"},
    {"key": "frequency", "label": "Frequency (Set & Actual)", "group": "Miner Metrics"},
    {"key": "core_voltage", "label": "Core Voltage (Set & Measured)", "group": "Miner Metrics"},
    {"key": "power_efficiency", "label": "Power (W) & Efficiency (J/TH)", "group": "Miner Metrics"},
    {"key": "error_rate", "label": "HW Error Rate & Poll Window", "group": "Miner Metrics"},
    {"key": "last_share", "label": "Last Share (Difficulty & Time Ago)", "group": "Shares & Network"},
    {"key": "shares_best_diff", "label": "Shares & Best Difficulty", "group": "Shares & Network"},
    {"key": "wifi_system", "label": "WiFi RSSI, Ping, Uptime & Block Height", "group": "Shares & Network"},
    {"key": "asic_domains", "label": "ASIC Hashrate Domains", "group": "Shares & Network"},
    {"key": "sparklines", "label": "24h Trend Sparklines", "group": "Panels & Controls"},
    {"key": "voltage_reason", "label": "Voltage / Autotune Status Banner", "group": "Panels & Controls"},
    {"key": "manual_control", "label": "Manual Frequency / Voltage Controls", "group": "Panels & Controls"},
    {"key": "pool_info", "label": "Pool Status & Editor", "group": "Panels & Controls"},
    {"key": "s19_boards", "label": "S19 Hashboard Table", "group": "S19 Specific"},
    {"key": "s19_tuner", "label": "S19 Braiins OS+ Power-Target & Tuner", "group": "S19 Specific"},
    {"key": "action_log", "label": "Card Last Action Footer", "group": "Panels & Controls"},
    {"key": "network_stats", "label": "Network Stats in Header (height, difficulty, adjustment)", "group": "Miner Hearth"},
    {"key": "profit", "label": "Power Cost & Estimated Earnings", "group": "Miner Hearth"},
    {"key": "block_odds", "label": "Block Odds & Expected Time to Block", "group": "Miner Hearth"},
    {"key": "firmware_badge", "label": "Firmware Update Badges", "group": "Miner Hearth"},
    {"key": "long_history", "label": "24h / 7d / 30d History Charts & CSV Export", "group": "Miner Hearth"},
    {"key": "tuning_log", "label": "Tuning Change Timeline", "group": "Miner Hearth"},
    {"key": "coin_odds", "label": "Block Odds per Coin (BTC, BCH, BSV, XEC, DGB, FB)", "group": "Miner Hearth v1.9"},
    {"key": "session_best", "label": "Session Best Share (since restart) & All-Time Best", "group": "Miner Hearth v1.9"},
    {"key": "pool_health", "label": "Pool Health (rejected / stale, latency, ping loss)", "group": "Miner Hearth v1.9"},
    {"key": "ambient_temp", "label": "Ambient / Intake Temperature", "group": "Miner Hearth v1.9"},
    {"key": "chip_health", "label": "Chip Health (best stable freq / voltage, wear)", "group": "Miner Hearth v1.9"},
    {"key": "fleet_compare", "label": "Fleet Comparison Table", "group": "Miner Hearth v1.9"},
    {"key": "pool_compare", "label": "Multi-Pool Comparison", "group": "Miner Hearth v1.9"},
    {"key": "faults", "label": "Fault Badge, Explainer & Clear/Reset Button", "group": "Miner Hearth v1.10"},
    {"key": "details_button", "label": "Details Button (all component readings)", "group": "Miner Hearth v1.10"},
    {"key": "fault_history", "label": "Fault History Timeline", "group": "Miner Hearth v1.10"},
]


def default_display_fields() -> dict[str, bool]:
    return {item["key"]: True for item in DISPLAY_FIELD_DEFS}


def default_global_tuner() -> dict[str, Any]:
    return {
        "max_freq": DEFAULT_MAX_FREQ_MHZ,
        "min_voltage_mv": DEFAULT_VOLTAGE_FLOOR_MV,
        "max_voltage_mv": DEFAULT_MAX_VOLTAGE_MV,
        "asic_target_c": DEFAULT_ASIC_TARGET_C,
        "asic_max_c": DEFAULT_ASIC_MAX_C,
        "vr_target_c": DEFAULT_VR_TARGET_C,
        "vr_max_c": DEFAULT_VR_MAX_C,
        "emergency_step_mhz": DEFAULT_EMERGENCY_STEP_MHZ,
        "emergency_voltage_mv": DEFAULT_EMERGENCY_VOLTAGE_MV,
        "error_target_pct": DEFAULT_ERROR_TARGET_PCT,
        "error_low_pct": DEFAULT_ERROR_LOW_PCT,
        "error_high_pct": DEFAULT_ERROR_HIGH_PCT,
    }


def normalize_display_fields(raw: Any) -> dict[str, bool]:
    out = default_display_fields()
    if isinstance(raw, dict):
        for k in out:
            if k in raw:
                out[k] = bool(raw[k])
    return out


def normalize_global_tuner(raw: Any) -> dict[str, Any]:
    out = default_global_tuner()
    if isinstance(raw, dict):
        for k in GLOBAL_TUNER_KEYS:
            if k in raw and raw[k] not in (None, ""):
                try:
                    if k in {"max_freq", "min_voltage_mv", "max_voltage_mv", "emergency_step_mhz", "emergency_voltage_mv"}:
                        out[k] = int(round(float(raw[k])))
                    else:
                        out[k] = float(raw[k])
                except (TypeError, ValueError):
                    pass
    out["max_freq"] = max(400, min(int(HARD_MAX_FREQ_MHZ), int(out["max_freq"])))
    out["max_voltage_mv"] = max(HARD_MIN_VOLTAGE_MV, min(HARD_MAX_VOLTAGE_MV, int(out["max_voltage_mv"])))
    out["min_voltage_mv"] = max(HARD_MIN_VOLTAGE_MV, min(int(out["max_voltage_mv"]), int(out["min_voltage_mv"])))
    out["emergency_voltage_mv"] = max(int(out["min_voltage_mv"]), min(int(out["max_voltage_mv"]), int(out["emergency_voltage_mv"])))
    out["emergency_step_mhz"] = max(5, min(200, int(out["emergency_step_mhz"])))
    out["asic_max_c"] = max(45.0, min(90.0, float(out["asic_max_c"])))
    out["asic_target_c"] = max(35.0, min(float(out["asic_max_c"]) - 0.5, float(out["asic_target_c"])))
    out["vr_max_c"] = max(45.0, min(95.0, float(out["vr_max_c"])))
    out["vr_target_c"] = max(35.0, min(float(out["vr_max_c"]), float(out["vr_target_c"])))
    out["error_target_pct"] = max(0.0, min(10.0, float(out["error_target_pct"])))
    out["error_high_pct"] = max(0.1, min(20.0, float(out["error_high_pct"])))
    out["error_low_pct"] = max(float(out["error_target_pct"]), min(float(out["error_high_pct"]), float(out["error_low_pct"])))
    return out


DEFAULTS = {
    "poll_interval_sec": 15,
    "cooldown_sec": 180,
    "restart_wait_sec": 45,
    "timeout_sec": 8,
    "step_mhz": 25,
    "emergency_step_mhz": DEFAULT_EMERGENCY_STEP_MHZ,
    "cool_polls_required": 3,
    "hysteresis_c": 3.0,
    "restart_after_change": False,
    "max_freq": DEFAULT_MAX_FREQ_MHZ,
    "asic_target_c": DEFAULT_ASIC_TARGET_C,
    "asic_max_c": DEFAULT_ASIC_MAX_C,
    "vr_target_c": DEFAULT_VR_TARGET_C,
    "vr_max_c": DEFAULT_VR_MAX_C,
    "voltage_start_mv": DEFAULT_VOLTAGE_START_MV,
    "emergency_voltage_mv": DEFAULT_EMERGENCY_VOLTAGE_MV,
    "voltage_step_mv": DEFAULT_VOLTAGE_STEP_MV,
    "min_voltage_mv": DEFAULT_VOLTAGE_FLOOR_MV,
    "max_voltage_mv": DEFAULT_MAX_VOLTAGE_MV,
    "error_target_pct": DEFAULT_ERROR_TARGET_PCT,
    "error_high_pct": DEFAULT_ERROR_HIGH_PCT,
    "error_low_pct": DEFAULT_ERROR_LOW_PCT,
    "error_window_polls": DEFAULT_ERROR_WINDOW_POLLS,
    "low_error_polls_required": DEFAULT_LOW_ERROR_POLLS,
    "log_dir": "logs",
    "log_file": "autotune.log",
    "csv_file": "autotune.csv",
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SUPPORTED_TYPES = {"gamma", "nerdoctaxe", "antminer", "axeos-generic"}


@dataclass
class DeviceConfig:
    name: str
    ip: str
    type: str  # "gamma" | "nerdoctaxe" | "antminer" | "axeos-generic"
    min_freq: float = 400.0
    max_freq: float = 850.0
    step_mhz: float = 25.0
    emergency_step_mhz: float = DEFAULT_EMERGENCY_STEP_MHZ
    asic_target_c: float = DEFAULT_ASIC_TARGET_C
    asic_max_c: float = DEFAULT_ASIC_MAX_C
    vr_target_c: float = DEFAULT_VR_TARGET_C
    vr_max_c: float = DEFAULT_VR_MAX_C
    poll_interval_sec: float = 15.0
    cooldown_sec: float = 180.0
    cool_polls_required: int = 3
    hysteresis_c: float = 3.0
    restart_after_change: bool = False
    restart_wait_sec: float = 45.0
    timeout_sec: float = 8.0
    voltage_control: bool = False
    vr_voltage_relief: bool = False
    voltage_start_mv: int = DEFAULT_VOLTAGE_START_MV
    emergency_voltage_mv: int = DEFAULT_EMERGENCY_VOLTAGE_MV
    voltage_step_mv: int = DEFAULT_VOLTAGE_STEP_MV
    min_voltage_mv: int = DEFAULT_VOLTAGE_FLOOR_MV
    max_voltage_mv: int = DEFAULT_MAX_VOLTAGE_MV
    error_target_pct: float = DEFAULT_ERROR_TARGET_PCT
    error_high_pct: float = DEFAULT_ERROR_HIGH_PCT
    error_low_pct: float = DEFAULT_ERROR_LOW_PCT
    error_window_polls: int = DEFAULT_ERROR_WINDOW_POLLS
    low_error_polls_required: int = DEFAULT_LOW_ERROR_POLLS
    frequency_options: list[float] = field(default_factory=list)
    totp_secret: str = ""
    otp_session: str = ""
    port: int = 80
    cgminer_port: int = 4028
    enabled_default: Optional[bool] = None
    manual_frequency_mhz: Optional[float] = None
    manual_voltage_mv: Optional[int] = None
    overrides: dict[str, Any] = field(default_factory=dict)
    overclock_opt_in: bool = False
    # Optional Braiins OS+ S19 power-target management (default OFF)
    bos_power_mgmt: bool = False
    bos_grpc_port: int = 50051
    bos_username: str = "root"
    bos_password: str = ""
    bos_min_watts: int = 2000
    bos_max_watts: int = 3500
    bos_step_watts: int = 50
    bos_chip_target_c: float = 75.0
    bos_chip_hot_c: float = 82.0
    bos_cooldown_sec: float = 300.0
    bos_cool_polls_required: int = 4
    # v1.10: optional stock-Bitmain web login (HTTP Digest) for log capture / reboot. Never preset.
    web_username: str = ""
    web_password: str = ""
    # v1.13: pluggable adapter family, algorithm, coin override, and opt-in write flag (default monitor-only).
    family: str = ""
    algorithm: str = ""
    coin: str = ""
    merged_coin: str = ""
    write_enabled: bool = False
    api_token: str = ""
    adapter_config: dict[str, Any] = field(default_factory=dict)


@dataclass
class GlobalConfig:
    devices: list[DeviceConfig]
    log_dir: Path
    log_file: Path
    csv_file: Path
    dry_run: bool = False
    dashboard_enabled: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    scan_subnet: str = ""
    state_file: Path = field(default_factory=lambda: SCRIPT_DIR / "autotune-state.json")
    userdata_file: Path = field(default_factory=lambda: SCRIPT_DIR / "userdata" / "devices.json")
    config_path: Optional[Path] = None
    defaults: dict[str, Any] = field(default_factory=dict)
    global_tuner: dict[str, Any] = field(default_factory=default_global_tuner)
    display_fields: dict[str, bool] = field(default_factory=default_display_fields)
    device_order: list[str] = field(default_factory=list)
    autotune_enabled: Optional[bool] = None


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return float(default)
    return float(value)


def _as_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return int(default)
    return int(value)


def load_raw_config(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise SystemExit(
                f"{path} is YAML, but PyYAML is not installed. "
                "Use config.json, or run: python -m pip install pyyaml"
            ) from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise SystemExit(f"Config {path} must be a JSON/YAML object")
    return data


def is_tunable(cfg: DeviceConfig) -> bool:
    return cfg.type in {"gamma", "nerdoctaxe", "axeos-generic"}


def build_device_config(entry: dict[str, Any], defaults: Optional[dict[str, Any]] = None) -> DeviceConfig:
    dfl = dict(DEFAULTS)
    if defaults:
        dfl.update(defaults)
    name = str(entry.get("name") or "").strip()
    ip = str(entry.get("ip") or "").strip()
    dtype = str(entry.get("type") or "").strip().lower()
    if not name or not ip:
        raise ValueError(f"Each device needs name and ip, got: {entry!r}")
    if dtype not in SUPPORTED_TYPES:
        raise ValueError(
            f"Device {name}: type must be 'gamma', 'nerdoctaxe', 'axeos-generic', or 'antminer', got {dtype!r}"
        )

    if dtype == "gamma":
        default_opts = list(GAMMA_FREQUENCY_OPTIONS)
        default_min, default_max = 400, DEFAULT_MAX_FREQ_MHZ
        default_asic_target, default_asic_max = DEFAULT_ASIC_TARGET_C, DEFAULT_ASIC_MAX_C
        default_vr_target, default_vr_max = DEFAULT_VR_TARGET_C, DEFAULT_VR_MAX_C
        default_vctrl = True
        default_vr_relief = False
        default_max_v = HARD_MAX_VOLTAGE_MV
    elif dtype in {"nerdoctaxe", "axeos-generic"}:
        default_opts = list(NERDOCTAXE_FREQUENCY_OPTIONS if dtype == "nerdoctaxe" else GAMMA_FREQUENCY_OPTIONS)
        default_min, default_max = (525, int(NERD_NORMAL_MAX_FREQ_MHZ)) if dtype == "nerdoctaxe" else (400, DEFAULT_MAX_FREQ_MHZ)
        default_asic_target, default_asic_max = DEFAULT_ASIC_TARGET_C, DEFAULT_ASIC_MAX_C
        default_vr_target, default_vr_max = DEFAULT_VR_TARGET_C, DEFAULT_VR_MAX_C
        default_vctrl = False
        default_vr_relief = True
        default_max_v = HARD_MAX_VOLTAGE_MV
    else:
        # Antminer S19 Pro (monitor-only unless optional Braiins OS+ power-target is enabled)
        default_opts = []
        default_min, default_max = 0, 0
        default_asic_target, default_asic_max = 75.0, 85.0
        default_vr_target, default_vr_max = 0.0, 0.0
        default_vctrl = False
        default_vr_relief = False
        default_max_v = HARD_MAX_VOLTAGE_MV

    if dtype == "antminer":
        options: list[float] = []
    else:
        opts = entry.get("frequency_options", dfl.get("frequency_options", default_opts))
        options = [float(x) for x in opts] if opts else list(default_opts)

    raw_overrides = entry.get("overrides")
    overrides: dict[str, Any] = {}
    if isinstance(raw_overrides, dict):
        for k in GLOBAL_TUNER_KEYS:
            if k in raw_overrides and raw_overrides[k] not in (None, ""):
                overrides[k] = raw_overrides[k]
    overclock_opt_in = False
    if dtype == "nerdoctaxe":
        ov_oc = raw_overrides.get("overclock_opt_in") if isinstance(raw_overrides, dict) else None
        overclock_opt_in = _as_bool(ov_oc if ov_oc is not None else entry.get("overclock_opt_in"), False)
        if overclock_opt_in:
            overrides["overclock_opt_in"] = True
    if dtype in {"gamma", "axeos-generic"} and isinstance(raw_overrides, dict) and raw_overrides.get("voltage_control") is not None:
        overrides["voltage_control"] = _as_bool(raw_overrides["voltage_control"], dtype == "gamma")

    def pick(key: str, fallback: Any) -> Any:
        if key in overrides and overrides[key] is not None:
            return overrides[key]
        if key in entry and entry[key] is not None:
            return entry[key]
        if key in dfl and dfl[key] is not None:
            return dfl[key]
        return fallback

    if dtype == "antminer":
        min_freq = _as_float(entry.get("min_freq"), default_min)
        max_freq = _as_float(entry.get("max_freq"), default_max)
        asic_target = _as_float(entry.get("asic_target_c"), default_asic_target)
        asic_max = _as_float(entry.get("asic_max_c"), default_asic_max)
        vr_target = _as_float(entry.get("vr_target_c"), default_vr_target)
        vr_max = _as_float(entry.get("vr_max_c"), default_vr_max)
        enabled_default: Optional[bool] = False
    else:
        model_cap = HARD_MAX_FREQ_MHZ if (dtype != "nerdoctaxe" or overclock_opt_in) else NERD_NORMAL_MAX_FREQ_MHZ
        min_freq = _as_float(pick("min_freq", default_min), default_min)
        max_freq = min(model_cap, _as_float(pick("max_freq", default_max), default_max))
        asic_target = _as_float(pick("asic_target_c", default_asic_target), default_asic_target)
        asic_max = _as_float(pick("asic_max_c", default_asic_max), default_asic_max)
        vr_target = _as_float(pick("vr_target_c", default_vr_target), default_vr_target)
        vr_max = _as_float(pick("vr_max_c", default_vr_max), default_vr_max)
        enabled_default = _as_bool(entry["enabled"]) if "enabled" in entry and entry["enabled"] is not None else None

    vctrl = _as_bool(pick("voltage_control", default_vctrl), default_vctrl) if dtype in {"gamma", "axeos-generic"} else False
    vr_relief = _as_bool(pick("vr_voltage_relief", default_vr_relief), default_vr_relief) if (dtype in {"nerdoctaxe", "axeos-generic"} and not vctrl) else False
    max_v = min(HARD_MAX_VOLTAGE_MV, _as_int(pick("max_voltage_mv", default_max_v), default_max_v))
    min_v = max(HARD_MIN_VOLTAGE_MV, min(max_v, _as_int(pick("min_voltage_mv", DEFAULT_VOLTAGE_FLOOR_MV), DEFAULT_VOLTAGE_FLOOR_MV)))
    emerg_v = max(min_v, min(max_v, _as_int(pick("emergency_voltage_mv", DEFAULT_EMERGENCY_VOLTAGE_MV), DEFAULT_EMERGENCY_VOLTAGE_MV)))

    bos_min_w = max(1, _as_int(entry.get("bos_min_watts"), 2000))
    bos_max_w = max(bos_min_w, min(10000, _as_int(entry.get("bos_max_watts"), 3500)))
    bos_step_w = max(10, min(500, _as_int(entry.get("bos_step_watts"), 50)))
    bos_target_c = _as_float(entry.get("bos_chip_target_c"), 75.0)
    bos_hot_c = max(bos_target_c + 1.0, _as_float(entry.get("bos_chip_hot_c"), 82.0))

    dev = DeviceConfig(
        name=name,
        ip=ip,
        type=dtype,
        min_freq=min_freq,
        max_freq=max_freq,
        step_mhz=_as_float(pick("step_mhz", 25), 25),
        emergency_step_mhz=_as_float(pick("emergency_step_mhz", DEFAULT_EMERGENCY_STEP_MHZ), DEFAULT_EMERGENCY_STEP_MHZ),
        asic_target_c=asic_target,
        asic_max_c=asic_max,
        vr_target_c=vr_target,
        vr_max_c=vr_max,
        poll_interval_sec=_as_float(pick("poll_interval_sec", 15), 15),
        cooldown_sec=_as_float(pick("cooldown_sec", 180), 180),
        cool_polls_required=_as_int(pick("cool_polls_required", 3), 3),
        hysteresis_c=_as_float(pick("hysteresis_c", 3), 3),
        restart_after_change=_as_bool(pick("restart_after_change", False), False),
        restart_wait_sec=_as_float(pick("restart_wait_sec", 45), 45),
        timeout_sec=_as_float(pick("timeout_sec", 8), 8),
        voltage_control=vctrl,
        vr_voltage_relief=vr_relief,
        voltage_start_mv=_as_int(pick("voltage_start_mv", DEFAULT_VOLTAGE_START_MV), DEFAULT_VOLTAGE_START_MV),
        emergency_voltage_mv=emerg_v,
        voltage_step_mv=_as_int(pick("voltage_step_mv", DEFAULT_VOLTAGE_STEP_MV), DEFAULT_VOLTAGE_STEP_MV),
        min_voltage_mv=min_v,
        max_voltage_mv=max_v,
        error_target_pct=max(0.0, _as_float(pick("error_target_pct", DEFAULT_ERROR_TARGET_PCT), DEFAULT_ERROR_TARGET_PCT)),
        error_high_pct=_as_float(pick("error_high_pct", DEFAULT_ERROR_HIGH_PCT), DEFAULT_ERROR_HIGH_PCT),
        error_low_pct=_as_float(pick("error_low_pct", DEFAULT_ERROR_LOW_PCT), DEFAULT_ERROR_LOW_PCT),
        error_window_polls=max(1, _as_int(pick("error_window_polls", DEFAULT_ERROR_WINDOW_POLLS), DEFAULT_ERROR_WINDOW_POLLS)),
        low_error_polls_required=max(1, _as_int(pick("low_error_polls_required", DEFAULT_LOW_ERROR_POLLS), DEFAULT_LOW_ERROR_POLLS)),
        frequency_options=options,
        totp_secret=str(entry.get("totp_secret") or ""),
        otp_session=str(entry.get("otp_session") or ""),
        port=_as_int(entry.get("port"), 80),
        cgminer_port=_as_int(entry.get("cgminer_port"), 4028),
        enabled_default=enabled_default,
        manual_frequency_mhz=(_as_float(entry["manual_frequency_mhz"], 0.0) if entry.get("manual_frequency_mhz") not in (None, "") else None),
        manual_voltage_mv=(_as_int(entry["manual_voltage_mv"], 0) if entry.get("manual_voltage_mv") not in (None, "") else None),
        overrides=overrides,
        overclock_opt_in=overclock_opt_in,
        bos_power_mgmt=_as_bool(entry.get("bos_power_mgmt"), False) if dtype == "antminer" else False,
        bos_grpc_port=_as_int(entry.get("bos_grpc_port"), 50051),
        bos_username=str(entry.get("bos_username") or "root"),
        bos_password=str(entry.get("bos_password") or ""),
        web_username=str(entry.get("web_username") or "").strip()[:64] if dtype == "antminer" else "",
        web_password=str(entry.get("web_password") or "") if dtype == "antminer" else "",
        family=str(entry.get("family") or "").strip().lower(),
        algorithm=str(entry.get("algorithm") or "").strip().lower(),
        coin=str(entry.get("coin") or "").strip().upper(),
        merged_coin=str(entry.get("merged_coin") or "").strip().upper(),
        write_enabled=_as_bool(entry.get("write_enabled"), False),
        api_token=str(entry.get("api_token") or ""),
        adapter_config=(dict(entry["adapter_config"]) if isinstance(entry.get("adapter_config"), dict) else {}),
        bos_min_watts=bos_min_w,
        bos_max_watts=bos_max_w,
        bos_step_watts=bos_step_w,
        bos_chip_target_c=bos_target_c,
        bos_chip_hot_c=bos_hot_c,
        bos_cooldown_sec=max(30.0, _as_float(entry.get("bos_cooldown_sec"), 300.0)),
        bos_cool_polls_required=max(1, _as_int(entry.get("bos_cool_polls_required"), 4)),
    )
    if dev.min_freq > dev.max_freq:
        raise ValueError(f"Device {name}: min_freq cannot exceed max_freq")
    if dev.min_voltage_mv > dev.max_voltage_mv:
        dev.min_voltage_mv = dev.max_voltage_mv
    return dev


def apply_global_and_overrides(dev: DeviceConfig, global_tuner: dict[str, Any]) -> None:
    """Recompute tunable device caps/targets from global_tuner + per-device overrides."""
    if not is_tunable(dev):
        return
    gt = normalize_global_tuner(global_tuner)
    ov = dev.overrides or {}

    def val(k: str) -> Any:
        return ov[k] if (k in ov and ov[k] not in (None, "")) else gt[k]

    if dev.type == "nerdoctaxe":
        if "overclock_opt_in" in ov:
            dev.overclock_opt_in = _as_bool(ov["overclock_opt_in"], False)
        model_cap = HARD_MAX_FREQ_MHZ if dev.overclock_opt_in else NERD_NORMAL_MAX_FREQ_MHZ
    else:
        model_cap = HARD_MAX_FREQ_MHZ
    if dev.type in {"gamma", "axeos-generic"} and "voltage_control" in ov:
        dev.voltage_control = _as_bool(ov["voltage_control"], dev.type == "gamma")
        dev.vr_voltage_relief = bool(dev.type == "axeos-generic" and not dev.voltage_control)
    dev.max_freq = max(dev.min_freq, min(model_cap, float(val("max_freq"))))
    dev.max_voltage_mv = max(HARD_MIN_VOLTAGE_MV, min(HARD_MAX_VOLTAGE_MV, int(round(float(val("max_voltage_mv"))))))
    dev.min_voltage_mv = max(HARD_MIN_VOLTAGE_MV, min(dev.max_voltage_mv, int(round(float(val("min_voltage_mv"))))))
    dev.emergency_voltage_mv = max(dev.min_voltage_mv, min(dev.max_voltage_mv, int(round(float(val("emergency_voltage_mv"))))))
    dev.emergency_step_mhz = max(5.0, float(val("emergency_step_mhz")))
    dev.asic_max_c = float(val("asic_max_c"))
    dev.asic_target_c = min(dev.asic_max_c - 0.5, float(val("asic_target_c")))
    dev.vr_max_c = float(val("vr_max_c"))
    dev.vr_target_c = min(dev.vr_max_c, float(val("vr_target_c")))
    dev.error_target_pct = max(0.0, float(val("error_target_pct")))
    dev.error_high_pct = max(0.1, float(val("error_high_pct")))
    dev.error_low_pct = max(dev.error_target_pct, min(dev.error_high_pct, float(val("error_low_pct"))))


def device_config_to_dict(dev: DeviceConfig) -> dict[str, Any]:
    if dev.type == "antminer":
        out: dict[str, Any] = {
            "name": dev.name,
            "ip": dev.ip,
            "type": "antminer",
            "cgminer_port": int(dev.cgminer_port),
            "enabled": False,
            "bos_power_mgmt": bool(dev.bos_power_mgmt),
            "bos_grpc_port": int(dev.bos_grpc_port),
            "bos_username": dev.bos_username,
            "bos_password": dev.bos_password,
            **({"web_username": dev.web_username} if dev.web_username else {}),
            **({"web_password": dev.web_password} if dev.web_password else {}),
            "bos_min_watts": int(dev.bos_min_watts),
            "bos_max_watts": int(dev.bos_max_watts),
            "bos_step_watts": int(dev.bos_step_watts),
            "bos_chip_target_c": float(dev.bos_chip_target_c),
            "bos_chip_hot_c": float(dev.bos_chip_hot_c),
            "bos_cooldown_sec": float(dev.bos_cooldown_sec),
            "bos_cool_polls_required": int(dev.bos_cool_polls_required),
        }
        if dev.family:
            out["family"] = dev.family
        if dev.algorithm:
            out["algorithm"] = dev.algorithm
        if dev.coin:
            out["coin"] = dev.coin
        if dev.merged_coin:
            out["merged_coin"] = dev.merged_coin
        if dev.write_enabled:
            out["write_enabled"] = True
        if dev.api_token:
            out["api_token"] = dev.api_token
        if dev.adapter_config:
            out["adapter_config"] = dict(dev.adapter_config)
        if dev.port and dev.port != 80:
            out["port"] = int(dev.port)
        return out
    out = {
        "name": dev.name,
        "ip": dev.ip,
        "type": dev.type,
        "min_freq": int(dev.min_freq),
        "max_freq": int(dev.max_freq),
        "step_mhz": int(dev.step_mhz),
        "emergency_step_mhz": int(dev.emergency_step_mhz),
        "frequency_options": [int(x) for x in dev.frequency_options],
        "asic_target_c": dev.asic_target_c,
        "asic_max_c": dev.asic_max_c,
        "vr_target_c": dev.vr_target_c,
        "vr_max_c": dev.vr_max_c,
        "voltage_control": bool(dev.voltage_control),
        "vr_voltage_relief": bool(dev.vr_voltage_relief),
        "voltage_start_mv": int(dev.voltage_start_mv),
        "emergency_voltage_mv": int(dev.emergency_voltage_mv),
        "voltage_step_mv": int(dev.voltage_step_mv),
        "min_voltage_mv": int(dev.min_voltage_mv),
        "max_voltage_mv": int(dev.max_voltage_mv),
        "error_target_pct": float(dev.error_target_pct),
        "error_high_pct": float(dev.error_high_pct),
        "error_low_pct": float(dev.error_low_pct),
        "error_window_polls": int(dev.error_window_polls),
        "low_error_polls_required": int(dev.low_error_polls_required),
        "enabled": bool(dev.enabled_default) if dev.enabled_default is not None else False,
    }
    if dev.overclock_opt_in:
        out["overclock_opt_in"] = True
    if dev.overrides:
        out["overrides"] = dict(dev.overrides)
    if dev.type != "gamma":
        out.update({"totp_secret": dev.totp_secret, "otp_session": dev.otp_session})
    if dev.manual_frequency_mhz is not None:
        out["manual_frequency_mhz"] = float(dev.manual_frequency_mhz)
    if dev.manual_voltage_mv is not None:
        out["manual_voltage_mv"] = int(dev.manual_voltage_mv)
    if dev.family:
        out["family"] = dev.family
    if dev.algorithm:
        out["algorithm"] = dev.algorithm
    if dev.coin:
        out["coin"] = dev.coin
    if dev.merged_coin:
        out["merged_coin"] = dev.merged_coin
    if dev.write_enabled:
        out["write_enabled"] = True
    if dev.port and dev.port != 80:
        out["port"] = int(dev.port)
    return out


def _write_json_atomic(path: Path, data: Any, backup: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        bak = path.with_name(path.name + ".bak")
        try:
            bak_tmp = path.with_name(path.name + ".bak.tmp")
            shutil.copy2(path, bak_tmp)
            bak_tmp.replace(bak)
        except OSError as exc:
            LOG.warning("Could not write backup %s: %s", bak, exc)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _load_json_dict(path: Optional[Path]) -> Optional[dict[str, Any]]:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("Could not read JSON file %s: %s", path, exc)
        return None


def _build_userdata_payload(
    devices: list[DeviceConfig],
    dry_run: bool = False,
    scan_subnet: str = "",
    global_tuner: Optional[dict[str, Any]] = None,
    display_fields: Optional[dict[str, bool]] = None,
    device_order: Optional[list[str]] = None,
    autotune_enabled: Optional[bool] = None,
) -> dict[str, Any]:
    order = list(device_order) if device_order is not None else [d.name for d in devices]
    out = {
        "schema_version": USERDATA_SCHEMA,
        "app_version": APP_VERSION,
        "dry_run": bool(dry_run),
        "scan_subnet": scan_subnet,
        "device_order": order,
        "display_fields": normalize_display_fields(display_fields),
        "global_tuner": normalize_global_tuner(global_tuner),
        "devices": [device_config_to_dict(d) for d in devices],
    }
    if autotune_enabled is not None:
        out["autotune_enabled"] = bool(autotune_enabled)
    return out


def save_userdata_devices(
    userdata_path: Optional[Path],
    devices: list[DeviceConfig],
    dry_run: Optional[bool] = None,
    scan_subnet: Optional[str] = None,
    global_tuner: Optional[dict[str, Any]] = None,
    display_fields: Optional[dict[str, bool]] = None,
    device_order: Optional[list[str]] = None,
    autotune_enabled: Optional[bool] = None,
) -> bool:
    if userdata_path is None:
        return False
    existing = _load_json_dict(userdata_path) or {}
    eff_auto = existing.get("autotune_enabled") if autotune_enabled is None else bool(autotune_enabled)
    payload = _build_userdata_payload(
        devices,
        dry_run=bool(existing.get("dry_run", False)) if dry_run is None else bool(dry_run),
        scan_subnet=str(existing.get("scan_subnet") or "") if scan_subnet is None else str(scan_subnet),
        global_tuner=existing.get("global_tuner") if global_tuner is None else global_tuner,
        display_fields=existing.get("display_fields") if display_fields is None else display_fields,
        device_order=[d.name for d in devices] if device_order is None else device_order,
        autotune_enabled=eff_auto if eff_auto is None else bool(eff_auto),
    )
    _write_json_atomic(userdata_path, payload, backup=True)
    return True


def save_userdata_prefs(
    userdata_path: Optional[Path],
    dry_run: Optional[bool] = None,
    scan_subnet: Optional[str] = None,
    global_tuner: Optional[dict[str, Any]] = None,
    display_fields: Optional[dict[str, bool]] = None,
    device_order: Optional[list[str]] = None,
) -> bool:
    if userdata_path is None:
        return False
    existing = _load_json_dict(userdata_path)
    if existing is None:
        existing = {
            "schema_version": USERDATA_SCHEMA,
            "app_version": APP_VERSION,
            "device_order": [],
            "display_fields": default_display_fields(),
            "global_tuner": default_global_tuner(),
            "devices": [],
        }
    if dry_run is not None:
        existing["dry_run"] = bool(dry_run)
    if scan_subnet is not None:
        existing["scan_subnet"] = str(scan_subnet)
    if global_tuner is not None:
        existing["global_tuner"] = normalize_global_tuner(global_tuner)
    if display_fields is not None:
        existing["display_fields"] = normalize_display_fields(display_fields)
    if device_order is not None:
        existing["device_order"] = list(device_order)
    existing["schema_version"] = USERDATA_SCHEMA
    existing["app_version"] = APP_VERSION
    _write_json_atomic(userdata_path, existing, backup=True)
    return True


_LEGACY_V16_DEFAULTS: dict[str, set[Any]] = {
    "max_freq": {850, 850.0},
    "min_voltage_mv": {1150},
    "max_voltage_mv": {1250},
    "asic_target_c": {60, 60.0},
    "asic_max_c": {65, 65.0},
    "vr_target_c": {65, 65.0},
    "vr_max_c": {70, 70.0},
    "emergency_step_mhz": {50, 50.0},
    "emergency_voltage_mv": {1210},
    "error_target_pct": {0, 0.0},
    "error_low_pct": {0.5},
    "error_high_pct": {2.0},
}


def _migrate_v16_userdata(existing: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Migrate a v1.3-v1.6 userdata/devices.json to v1.7 without losing devices or custom overrides."""
    changed = False
    if "global_tuner" not in existing:
        changed = True
        gt = default_global_tuner()
        existing["global_tuner"] = gt
        devs = existing.get("devices")
        if isinstance(devs, list):
            new_devs: list[dict[str, Any]] = []
            for entry in devs:
                if not isinstance(entry, dict):
                    continue
                item = dict(entry)
                dtype = str(item.get("type") or "").strip().lower()
                if dtype in {"gamma", "nerdoctaxe", "axeos-generic"}:
                    ov: dict[str, Any] = dict(item.get("overrides") or {}) if isinstance(item.get("overrides"), dict) else {}
                    for k in GLOBAL_TUNER_KEYS:
                        if k in item and item[k] is not None:
                            val = item[k]
                            if val not in _LEGACY_V16_DEFAULTS.get(k, set()):
                                ov.setdefault(k, val)
                            item[k] = ov[k] if k in ov else gt[k]
                    if ov:
                        item["overrides"] = ov
                new_devs.append(item)
            existing["devices"] = new_devs
    if "display_fields" not in existing:
        existing["display_fields"] = default_display_fields()
        changed = True
    else:
        norm_df = normalize_display_fields(existing.get("display_fields"))
        if norm_df != existing.get("display_fields"):
            existing["display_fields"] = norm_df
            changed = True
    if "device_order" not in existing:
        devs = existing.get("devices") if isinstance(existing.get("devices"), list) else []
        existing["device_order"] = [str(d.get("name") or "").strip() for d in devs if isinstance(d, dict) and str(d.get("name") or "").strip()]
        changed = True
    existing["global_tuner"] = normalize_global_tuner(existing.get("global_tuner"))
    old_sv = int(existing.get("schema_version") or 1)
    if old_sv < USERDATA_SCHEMA:
        devs = existing.get("devices")
        if isinstance(devs, list):
            for item in devs:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "").strip().lower() == "nerdoctaxe":
                    ov = item.get("overrides") if isinstance(item.get("overrides"), dict) else {}
                    opt_in = _as_bool(ov.get("overclock_opt_in") if "overclock_opt_in" in ov else item.get("overclock_opt_in"), False)
                    if not opt_in:
                        capped = False
                        try:
                            if float(item.get("max_freq") or 0) > NERD_NORMAL_MAX_FREQ_MHZ:
                                item["max_freq"] = int(NERD_NORMAL_MAX_FREQ_MHZ)
                                capped = True
                        except (TypeError, ValueError):
                            pass
                        if isinstance(ov, dict) and ov.get("max_freq") is not None:
                            try:
                                if float(ov["max_freq"]) > NERD_NORMAL_MAX_FREQ_MHZ:
                                    ov["max_freq"] = int(NERD_NORMAL_MAX_FREQ_MHZ)
                                    capped = True
                            except (TypeError, ValueError):
                                pass
                        if capped:
                            LOG.warning(
                                "Migration v1.12: capped %s max_freq to %d MHz (firmware normal max; enable overclock_opt_in to unlock >800 MHz)",
                                item.get("name") or "nerdoctaxe",
                                int(NERD_NORMAL_MAX_FREQ_MHZ),
                            )
        existing["schema_version"] = USERDATA_SCHEMA
        changed = True
    return existing, changed


def migrate_or_load_userdata(
    raw_config: dict[str, Any],
    defaults: dict[str, Any],
    state_file: Path,
    userdata_file: Path,
    scan_subnet: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load user devices/prefs from userdata/devices.json, migrating v1.2-v1.6 files cleanly.

    Once userdata_file exists, shipped config.json updates never overwrite saved devices,
    and v1.3-v1.6 settings migrate to v1.7 while preserving custom device tweaks.
    """
    existing = _load_json_dict(userdata_file)
    if existing is not None:
        pre_sv = int(existing.get("schema_version") or 1)
        if pre_sv < USERDATA_SCHEMA and userdata_file.exists():
            bak_v = userdata_file.with_name(f"{userdata_file.name}.v{pre_sv}.bak")
            if not bak_v.exists():
                try:
                    shutil.copy2(userdata_file, bak_v)
                except OSError as exc:
                    LOG.warning("Could not write pre-migration backup %s: %s", bak_v, exc)
        existing, changed = _migrate_v16_userdata(existing)
        raw_devs = existing.get("devices")
        if not isinstance(raw_devs, list):
            raw_devs = []
        if changed:
            existing["app_version"] = APP_VERSION
            _write_json_atomic(userdata_file, existing, backup=True)
            LOG.info("Migrated userdata settings to v%s in %s", APP_VERSION, userdata_file)
        return raw_devs, existing

    # First start of v1.3+ (or missing userdata_file): migrate from v1.2 config.json + autotune-state.json.
    legacy_state = _load_json_dict(state_file) or {}
    legacy_devs_state = legacy_state.get("devices") if isinstance(legacy_state.get("devices"), dict) else {}
    legacy_dry = bool(legacy_state.get("dry_run", False))

    config_devs = raw_config.get("devices")
    if not isinstance(config_devs, list):
        config_devs = []

    migrated_entries: list[dict[str, Any]] = []
    migrated_objs: list[DeviceConfig] = []
    for entry in config_devs:
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        name = str(item.get("name") or "").strip()
        if name and name in legacy_devs_state:
            st_entry = legacy_devs_state[name]
            if isinstance(st_entry, dict) and "enabled" in st_entry:
                item["enabled"] = bool(st_entry["enabled"])
            elif isinstance(st_entry, bool):
                item["enabled"] = st_entry
        elif "enabled" not in item:
            # Preserve pre-v1.3 default for already-configured tunable devices, OFF for Antminer.
            dtype = str(item.get("type") or "").strip().lower()
            item["enabled"] = dtype != "antminer"
        try:
            dev_obj = build_device_config(item, defaults)
        except ValueError as exc:
            LOG.warning("Skipping invalid legacy device %r during migration: %s", entry, exc)
            continue
        migrated_objs.append(dev_obj)
        migrated_entries.append(device_config_to_dict(dev_obj))

    legacy_auto = legacy_state.get("autotune_enabled") if "autotune_enabled" in legacy_state else (any(bool(d.enabled_default) for d in migrated_objs) if migrated_objs else False)
    payload = _build_userdata_payload(migrated_objs, dry_run=legacy_dry, scan_subnet=scan_subnet, autotune_enabled=bool(legacy_auto))
    _write_json_atomic(userdata_file, payload, backup=True)
    if migrated_objs:
        LOG.info(
            "Migrated %d device(s) into separate user data file: %s",
            len(migrated_objs),
            userdata_file,
        )
    else:
        LOG.info("Initialized empty user data file: %s (add miners via Scan network)", userdata_file)
    return migrated_entries, payload


def persist_device_rename(config_path: Optional[Path], old_name: str, new_name: str) -> bool:
    if config_path is None or not config_path.exists():
        return False
    raw = _load_json_dict(config_path)
    if raw is None:
        return False
    changed = False
    for entry in raw.get("devices") or []:
        if isinstance(entry, dict) and str(entry.get("name") or "").strip() == old_name:
            entry["name"] = new_name
            changed = True
            break
    if changed:
        _write_json_atomic(config_path, raw, backup=True)
    return changed


def persist_device_add(config_path: Optional[Path], entry: dict[str, Any]) -> bool:
    if config_path is None:
        return False
    raw = _load_json_dict(config_path) or {"schema_version": 1, "app_version": APP_VERSION, "devices": []}
    devices = raw.setdefault("devices", [])
    devices.append(entry)
    _write_json_atomic(config_path, raw, backup=True)
    return True


def persist_device_remove(config_path: Optional[Path], name: str) -> bool:
    if config_path is None or not config_path.exists():
        return False
    raw = _load_json_dict(config_path)
    if not isinstance(raw, dict):
        return False
    devices = raw.get("devices")
    if not isinstance(devices, list):
        return False
    kept = [d for d in devices if not (isinstance(d, dict) and d.get("name") == name)]
    if len(kept) == len(devices):
        return False
    raw["devices"] = kept
    _write_json_atomic(config_path, raw, backup=True)
    return True


def parse_config(
    raw: dict[str, Any],
    script_dir: Path,
    dry_run: bool = False,
    config_path: Optional[Path] = None,
    use_userdata: bool = True,
) -> GlobalConfig:
    defaults = dict(DEFAULTS)
    incoming = raw.get("defaults") or raw.get("global") or {}
    if isinstance(incoming, dict):
        defaults.update({k: v for k, v in incoming.items() if not str(k).startswith("_")})

    base_dir = config_path.parent if config_path is not None else script_dir
    log_dir_val = raw.get("log_dir") or defaults.get("log_dir") or "logs"
    log_dir = Path(str(log_dir_val))
    if not log_dir.is_absolute():
        log_dir = base_dir / log_dir
    log_file = log_dir / str(raw.get("log_file") or defaults.get("log_file") or "autotune.log")
    csv_file = log_dir / str(raw.get("csv_file") or defaults.get("csv_file") or "autotune.csv")

    dash = raw.get("dashboard") or {}
    if not isinstance(dash, dict):
        dash = {}
    dash_host = str(dash.get("host") or raw.get("dashboard_host") or "127.0.0.1").strip() or "127.0.0.1"
    dash_port = _as_int(dash.get("port", raw.get("dashboard_port", 8080)), 8080)
    dash_enabled = _as_bool(dash.get("enabled", raw.get("dashboard_enabled", True)), True)
    scan_subnet = str(dash.get("scan_subnet") or raw.get("scan_subnet") or "").strip()

    state_val = raw.get("state_file") or dash.get("state_file") or "autotune-state.json"
    state_file = Path(str(state_val))
    if not state_file.is_absolute():
        state_file = base_dir / state_file

    ud_val = raw.get("userdata_file") or dash.get("userdata_file") or "userdata/devices.json"
    userdata_file = Path(str(ud_val))
    if not userdata_file.is_absolute():
        userdata_file = base_dir / userdata_file

    global_tuner = normalize_global_tuner(raw.get("global_tuner"))
    display_fields = normalize_display_fields(raw.get("display_fields"))
    raw_order: list[str] = []
    autotune_enabled: Optional[bool] = False if not use_userdata else None

    if use_userdata:
        devices_raw, ud_meta = migrate_or_load_userdata(
            raw, defaults, state_file=state_file, userdata_file=userdata_file, scan_subnet=scan_subnet
        )
        if "autotune_enabled" in ud_meta:
            autotune_enabled = _as_bool(ud_meta.get("autotune_enabled"), False)
        else:
            autotune_enabled = any(_as_bool(d.get("enabled"), False) for d in devices_raw if isinstance(d, dict))
        if not scan_subnet and ud_meta.get("scan_subnet"):
            scan_subnet = str(ud_meta.get("scan_subnet") or "").strip()
        if not dry_run and "dry_run" in ud_meta:
            dry_run = bool(ud_meta.get("dry_run"))
        if isinstance(ud_meta.get("global_tuner"), dict):
            global_tuner = normalize_global_tuner(ud_meta.get("global_tuner"))
        if isinstance(ud_meta.get("display_fields"), dict):
            display_fields = normalize_display_fields(ud_meta.get("display_fields"))
        if isinstance(ud_meta.get("device_order"), list):
            raw_order = [str(x).strip() for x in ud_meta.get("device_order") if str(x).strip()]
    else:
        devices_raw = raw.get("devices") if isinstance(raw.get("devices"), list) else []

    eff_defaults = dict(defaults)
    eff_defaults.update(global_tuner)

    devices: list[DeviceConfig] = []
    seen_names: set[str] = set()
    for entry in devices_raw:
        if not isinstance(entry, dict):
            continue
        try:
            dev = build_device_config(entry, eff_defaults)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if use_userdata:
            apply_global_and_overrides(dev, global_tuner)
        if dev.name in seen_names:
            raise SystemExit(f"Duplicate device name in config: {dev.name}")
        seen_names.add(dev.name)
        devices.append(dev)

    if raw_order:
        by_name = {d.name: d for d in devices}
        ordered_devs: list[DeviceConfig] = []
        used: set[str] = set()
        for nm in raw_order:
            if nm in by_name and nm not in used:
                ordered_devs.append(by_name[nm])
                used.add(nm)
        for d in devices:
            if d.name not in used:
                ordered_devs.append(d)
        devices = ordered_devs
    device_order = [d.name for d in devices]

    return GlobalConfig(
        devices=devices,
        log_dir=log_dir,
        log_file=log_file,
        csv_file=csv_file,
        dry_run=dry_run,
        dashboard_enabled=dash_enabled,
        dashboard_host=dash_host,
        dashboard_port=dash_port,
        scan_subnet=scan_subnet,
        state_file=state_file,
        userdata_file=userdata_file,
        config_path=config_path,
        defaults=eff_defaults,
        global_tuner=global_tuner,
        display_fields=display_fields,
        device_order=device_order,
        autotune_enabled=autotune_enabled,
    )


# ---------------------------------------------------------------------------
# TOTP (RFC 6238) for optional NerdOCTAxe OTP
# ---------------------------------------------------------------------------

def _base32_decode(secret: str) -> bytes:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    cleaned = "".join(ch for ch in secret.upper().replace(" ", "") if ch != "=")
    bits = ""
    for ch in cleaned:
        if ch not in alphabet:
            raise ValueError(f"Invalid base32 character in TOTP secret: {ch!r}")
        bits += f"{alphabet.index(ch):05b}"
    # Pad to a full number of bytes, then drop leftover leftover bits.
    leftover = len(bits) % 8
    if leftover:
        bits = bits[:-leftover]
    return bytes(int(bits[i : i + 8], 2) for i in range(0, len(bits), 8))


def generate_totp(secret: str, now: Optional[float] = None, step: int = 30, digits: int = 6) -> str:
    key = _base32_decode(secret)
    counter = int((time.time() if now is None else now) // step)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, "sha1").digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class DeviceOffline(Exception):
    pass


class DeviceError(Exception):
    pass


def _is_private_host(host: str) -> bool:
    if not host:
        return False
    if host.lower() in {"localhost", "127.0.0.1", "::1"}:
        return True
    parts = host.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return False
    if octets[0] == 10:
        return True
    if octets[0] == 192 and octets[1] == 168:
        return True
    if octets[0] == 172 and 16 <= octets[1] <= 31:
        return True
    if octets[0] == 127:
        return True
    return False


class _DontFollowRedirects(urllib.request.HTTPRedirectHandler):
    """ESP-IDF firmware maps unknown API paths (e.g. /api/v2/* on v1.0.37.x) to a
    302 redirect to the HTML web UI. Following that wastes time and can time out
    the real /api/system/info call, so API clients must not follow redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _decode_http_body(raw: bytes, headers: dict[str, str]) -> bytes:
    encoding = (headers.get("content-encoding") or "").lower()
    if "gzip" in encoding and raw:
        try:
            return gzip.decompress(raw)
        except Exception:
            return raw
    return raw


def _read_http_body(fp: Any, headers: Optional[dict[str, str]] = None) -> bytes:
    try:
        raw = fp.read() if fp is not None else b""
    except http.client.IncompleteRead as exc:
        raw = exc.partial or b""
    return _decode_http_body(raw or b"", headers or {})


def http_request_ex(
    method: str,
    url: str,
    body: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: float = 8.0,
    retries: Optional[int] = None,
) -> tuple[int, bytes, dict[str, str]]:
    """HTTP helper for ESP32 AxeOS / NerdQAxePlus.

    - Never follows redirects (v1 firmware 404s /api/v2/* to the HTML UI).
    - Sends Connection: close (firmware has keep_alive_enable = false).
    - Bypasses system HTTP proxies for RFC-1918 hosts.
    - Retries GET once on timeout / connection reset / incomplete body.
    - Decodes gzip if the device still sends it.
    """
    method = method.upper()
    if retries is None:
        retries = 1 if method == "GET" else 0
    hdrs = {
        "User-Agent": f"MinerHearth/{APP_VERSION}",
        "Accept": "application/json, text/plain;q=0.8, */*;q=0.1",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    if headers:
        hdrs.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    host = urllib.parse.urlparse(url).hostname or ""
    handlers: list[Any] = [_DontFollowRedirects()]
    if _is_private_host(host):
        handlers.insert(0, urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)

    attempts = max(1, int(retries) + 1)
    last_exc: Optional[Exception] = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with opener.open(req, timeout=timeout) as resp:
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.getcode() or 200, _read_http_body(resp, resp_headers), resp_headers
        except urllib.error.HTTPError as exc:
            resp_headers = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
            return exc.code, _read_http_body(exc, resp_headers), resp_headers
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(0.2)
                continue
            raise DeviceOffline(f"{method} {url} failed: {exc}") from exc
    raise DeviceOffline(f"{method} {url} failed: {last_exc}")


def http_request(
    method: str,
    url: str,
    body: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: float = 8.0,
    retries: Optional[int] = None,
) -> tuple[int, bytes]:
    status, payload, _headers = http_request_ex(
        method, url, body=body, headers=headers, timeout=timeout, retries=retries
    )
    return status, payload


def body_snippet(payload: Optional[bytes], limit: int = 300) -> str:
    if not payload:
        return ""
    return payload[:limit].decode("utf-8", errors="replace")


def _looks_like_html(payload: bytes, headers: Optional[dict[str, str]] = None) -> bool:
    ctype = ((headers or {}).get("content-type") or "").lower()
    if "text/html" in ctype:
        return True
    start = (payload or b"").lstrip()[:64].lower()
    return start.startswith(b"<!doctype") or start.startswith(b"<html")


def _sanitize_json_text(text: str) -> str:
    text = text.lstrip("\ufeff")
    text = text.replace("\x00", "")
    text = re.sub(r"\bNaN\b", "null", text)
    text = re.sub(r"\b-?Infinity\b", "null", text)
    return text


def _json_body(payload: bytes) -> Any:
    if not payload:
        return None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = payload.decode("utf-8", errors="replace")
    try:
        return json.loads(_sanitize_json_text(text))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

@dataclass
class Reading:
    ok: bool
    error: str = ""
    reachable: bool = False
    asic_temp: Optional[float] = None
    vr_temp: Optional[float] = None
    asic_temps: list[float] = field(default_factory=list)
    frequency: Optional[float] = None
    actual_frequency: Optional[float] = None
    hashrate: Optional[float] = None
    hashrate_1m: Optional[float] = None
    hashrate_5m: Optional[float] = None
    hashrate_10m: Optional[float] = None
    hashrate_15m: Optional[float] = None
    hashrate_1h: Optional[float] = None
    hashrate_24h: Optional[float] = None
    hashrate_avg: Optional[float] = None
    power: Optional[float] = None
    power_limit_w: Optional[float] = None
    input_voltage: Optional[float] = None
    input_current: Optional[float] = None
    core_voltage: Optional[float] = None
    core_voltage_actual: Optional[float] = None
    version: str = ""
    model: str = ""
    hostname: str = ""
    source: str = ""
    board_temps: list[float] = field(default_factory=list)
    chip_temps: list[float] = field(default_factory=list)
    fan_rpms: list[int] = field(default_factory=list)
    fans: list[dict[str, Any]] = field(default_factory=list)
    uptime_sec: Optional[float] = None
    pool_url: str = ""
    pool_port: Optional[int] = None
    pool_user: str = ""
    fallback_pool_url: str = ""
    fallback_pool_port: Optional[int] = None
    fallback_pool_user: str = ""
    using_fallback_stratum: bool = False
    firmware: str = ""
    firmware_note: str = ""
    expected_hashrate: Optional[float] = None
    error_percentage: Optional[float] = None
    shares_accepted: Optional[int] = None
    shares_rejected: Optional[int] = None
    shares_stale: Optional[int] = None
    hw_errors: Optional[int] = None
    pool_rejected_pct: Optional[float] = None
    rejected_reasons: list[dict[str, Any]] = field(default_factory=list)
    best_diff: Optional[float] = None
    best_diff_display: str = ""
    best_session_diff: Optional[float] = None
    best_session_diff_display: str = ""
    pool_difficulty: Optional[float] = None
    last_submitted_diff: Optional[float] = None
    # v1.16.1: True when last_submitted_diff is the pool's share target (CGMiner "Last Share Difficulty"), not the
    # share's own difficulty - then it is never shown as the difficulty of a block-winning share.
    last_share_is_target: bool = False
    # v1.15.5: firmware "found blocks" counter (AxeOS blockFound, NerdQAxe foundBlocks, CGMiner "Found Blocks")
    found_blocks: Optional[int] = None
    # v1.15.5: AxeOS / hexOS "showNewBlock" banner flag (rises when the firmware sees one of its shares win a block)
    new_block_flag: Optional[bool] = None
    last_share_seconds: Optional[float] = None
    last_share_estimated: bool = False
    last_share_waiting: bool = False
    ping_rtt_ms: Optional[float] = None
    ping_loss: Optional[float] = None
    wifi_rssi: Optional[int] = None
    stratum_disconnects: Optional[int] = None
    wifi_disconnects: Optional[int] = None
    asic_domains: list[dict[str, Any]] = field(default_factory=list)
    block_height: Optional[int] = None
    network_difficulty: Optional[float] = None
    tuner_mode: str = ""
    tuner_chain_statuses: list[dict[str, Any]] = field(default_factory=list)
    boards: list[dict[str, Any]] = field(default_factory=list)
    pools: list[dict[str, Any]] = field(default_factory=list)
    ambient_temp: Optional[float] = None
    ambient_source: str = ""
    # v1.10: raw fault flags as reported by the firmware, and every readable API field (personal keys removed)
    fault_raw: dict[str, Any] = field(default_factory=dict)
    raw_fields: dict[str, Any] = field(default_factory=dict)
    # v1.13: canonical H/s (or Sol/s), unit kind, algorithm, family, capabilities
    hashrate_hs: Optional[float] = None
    hashrate_unit_kind: str = "hash"
    algorithm: str = "sha256d"
    family: str = ""
    capabilities: dict[str, bool] = field(default_factory=dict)


AMBIENT_KEYS = ("ambientTemp", "ambient_temp", "tempAmbient", "ambient", "inletTemp", "intakeTemp", "temp_inlet",
                "temp_ambient", "envTemp", "temp_env", "Env Temp", "Ambient Temp", "Inlet Temp")


def _ambient_from(data: dict[str, Any]) -> Optional[float]:
    v = _num(data, *AMBIENT_KEYS) if isinstance(data, dict) else None
    return v if v is not None and -30.0 < v < 70.0 else None


def _antminer_intake(stats_item: dict[str, Any]) -> tuple[Optional[float], str]:
    """Explicit ambient keys first; else the coolest board-inlet PCB sensor (temp_pcbN = 'in-in-out-out')."""
    v = _ambient_from(stats_item)
    if v is not None:
        return v, "miner"
    inlets: list[float] = []
    for i in range(1, 17):
        raw = stats_item.get(f"temp_pcb{i}") if isinstance(stats_item, dict) else None
        if isinstance(raw, str) and raw.count("-") == 3:
            try:
                parts = [float(x) for x in raw.split("-")]
            except ValueError:
                continue
            vals = [x for x in parts[:2] if 0 < x < 70]
            if vals:
                inlets.append(min(vals))
    if inlets:
        return min(inlets), "board inlet sensor"
    return None, ""


def _num(data: dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        if key in data and data[key] is not None:
            try:
                return float(data[key])
            except (TypeError, ValueError):
                continue
    return None


def _opt_int(data: dict[str, Any], *keys: str) -> Optional[int]:
    val = _num(data, *keys)
    return int(round(val)) if val is not None else None


def _found_blocks_from(data: Any) -> Optional[int]:
    """v1.15.5: blocks the firmware itself says it found this session (None when not reported)."""
    if not isinstance(data, dict):
        return None
    for key in ("foundBlocks", "Found Blocks", "found_blocks", "blockFound", "blocksFound"):
        if key not in data:
            continue
        v = data.get(key)
        if isinstance(v, bool):
            return int(v)
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            continue
        return n if n >= 0 else None
    return None


def _new_block_flag_from(data: Any) -> Optional[bool]:
    """v1.15.5: AxeOS 'showNewBlock' (true while the firmware shows its 'block found' banner)."""
    if not isinstance(data, dict) or "showNewBlock" not in data:
        return None
    v = data.get("showNewBlock")
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes"}
    return bool(v)


def _positive_temps(raw: Any) -> list[float]:
    """Collect chip/board temps, treating 0 / None / negatives as missing sensors."""
    temps: list[float] = []
    if not isinstance(raw, list):
        return temps
    for item in raw:
        try:
            val = float(item)
        except (TypeError, ValueError):
            continue
        if val > 0:
            temps.append(val)
    return temps


def _vr_temp_from(*sources: Any) -> Optional[float]:
    """Use the hotter of vrTemp and vrTempInt (internal VR sensor) for safety."""
    vals: list[float] = []
    for src in sources:
        if not isinstance(src, dict):
            continue
        for key in ("vrTemp", "vrTempInt"):
            val = _num(src, key)
            if val is not None and val > 0:
                vals.append(val)
    return max(vals) if vals else None


def looks_like_axeos(data: dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return False
    if "thermal" in data or "performance" in data:
        return True
    keys = (
        "ASICModel",
        "asicModel",
        "axeOSVersion",
        "deviceModel",
        "hashRate",
        "hashRate_10m",
        "hashRate10m",
        "vrTemp",
        "frequency",
        "asicCount",
        "coreVoltage",
    )
    return any(k in data for k in keys)


NERD_MODEL_HINTS = ("nerd", "qaxe", "octaxe", "nerdaxe", "nerdhaxe", "nerdeko")
NON_GAMMA_FORK_HINTS = ("lucky", "lv06", "lv07", "lv08")
_GAMMA_BOARD_VERSION_RE = re.compile(r"^60[0-9](?:[._-].*)?$")


def _has_gamma_board_version(*payloads: Optional[dict[str, Any]]) -> bool:
    for data in payloads:
        if not isinstance(data, dict):
            continue
        for container in (data, data.get("system")):
            if not isinstance(container, dict):
                continue
            bv = container.get("boardVersion")
            if bv is not None and _GAMMA_BOARD_VERSION_RE.match(str(bv).strip()):
                return True
    return False


def _identity_strings(data: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("deviceModel", "boardVersion", "ASICModel", "asicModel", "hostname", "version", "boardName"):
        val = data.get(key)
        if val is not None and str(val).strip():
            out.append(str(val))
    network = data.get("network")
    if isinstance(network, dict) and network.get("hostname"):
        out.append(str(network["hostname"]))
    system = data.get("system")
    if isinstance(system, dict):
        for key in ("deviceModel", "hostname", "asicModel", "ASICModel"):
            if system.get(key):
                out.append(str(system[key]))
        net = system.get("network")
        if isinstance(net, dict) and net.get("hostname"):
            out.append(str(net["hostname"]))
    return out


def _asic_count(data: dict[str, Any]) -> int:
    val = _opt_int(data, "asicCount")
    if val is None and isinstance(data.get("performance"), dict):
        val = _opt_int(data["performance"], "asicCount")
    return int(val or 0)


def classify_axeos_payload(*payloads: Optional[dict[str, Any]]) -> str:
    """Classify an AxeOS / NerdQAxePlus JSON payload.

    NerdOCTAxe / NerdQAxe if deviceModel/hostname/boardVersion/version contains
    'nerd'/'octaxe'/'qaxe' (case-insensitive, unicode-safe), or asicCount > 1.
    Otherwise 'gamma' when it looks like a Bitaxe, else 'axeos-generic'.
    Empty string if the payload does not look like AxeOS at all.
    """
    blobs: list[str] = []
    asic_count = 0
    any_axeos = False
    for data in payloads:
        if not isinstance(data, dict):
            continue
        if looks_like_axeos(data):
            any_axeos = True
        blobs.extend(_identity_strings(data))
        asic_count = max(asic_count, _asic_count(data))
    if not any_axeos:
        return ""
    hint = " ".join(blobs).casefold()
    if any(token in hint for token in NERD_MODEL_HINTS):
        return "nerdoctaxe"
    if asic_count > 1:
        return "nerdoctaxe"
    if not any(token in hint for token in NON_GAMMA_FORK_HINTS):
        if "bitaxe" in hint or "gamma" in hint or _has_gamma_board_version(*payloads):
            return "gamma"
    return "axeos-generic"


_DIFF_SUFFIXES = {
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "E": 1e18,
}


def parse_diff_value(val: Any) -> tuple[Optional[float], str]:
    """Parse numeric or SI-suffixed share difficulty ('1.24G', '45.2M', 2160) -> (float, display)."""
    if val is None or isinstance(val, bool):
        return None, ""
    if isinstance(val, (int, float)):
        fval = float(val)
        return (fval, "") if fval >= 0 else (None, "")
    s = str(val).strip()
    if not s:
        return None, ""
    clean = s.replace(",", "")
    m = re.match(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([kKmMgGtTpPeE])?$", clean)
    if m:
        num = float(m.group(1))
        suf = (m.group(2) or "").upper()
        mult = _DIFF_SUFFIXES.get(suf, 1.0)
        return num * mult, s
    return None, s


def wifi_quality_label(rssi: Optional[int]) -> str:
    if rssi is None:
        return ""
    if rssi >= -55:
        return "Excellent"
    if rssi >= -65:
        return "Good"
    if rssi >= -75:
        return "Fair"
    return "Weak"


def compute_j_per_th(power_w: Optional[float], hashrate_ghs: Optional[float]) -> Optional[float]:
    if power_w is None or hashrate_ghs is None or power_w <= 0 or hashrate_ghs <= 0:
        return None
    ths = float(hashrate_ghs) / 1000.0
    if ths <= 0:
        return None
    return round(float(power_w) / ths, 2)


def _stratum_dict(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get("stratum")
    return raw if isinstance(raw, dict) else {}


def _stratum_pools(data: dict[str, Any]) -> list[dict[str, Any]]:
    pools = _stratum_dict(data).get("pools")
    if not isinstance(pools, list):
        return []
    return [p for p in pools if isinstance(p, dict)]


def _connected_stratum_pool(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    pools = _stratum_pools(data)
    for p in pools:
        if p.get("connected") is True or p.get("active") is True:
            return p
    return pools[0] if pools else None


def _sum_pool_accepted(data: dict[str, Any]) -> Optional[int]:
    total = 0
    found = False
    for p in _stratum_pools(data):
        acc = _opt_int(p, "accepted")
        if acc is not None:
            total += acc
            found = True
    return total if found else None


def _shares_accepted_from_payload(data: dict[str, Any]) -> Optional[int]:
    acc = _opt_int(data, "sharesAccepted")
    if acc is not None:
        return acc
    return _sum_pool_accepted(data)


def _shares_rejected_from_payload(data: dict[str, Any]) -> Optional[int]:
    rej = _opt_int(data, "sharesRejected")
    if rej is not None:
        return rej
    total = 0
    found = False
    for pool in _stratum_pools(data):
        val = _opt_int(pool, "rejected")
        if val is not None:
            total += val
            found = True
    return total if found else None


def _normalize_ping_loss(val: Optional[float]) -> Optional[float]:
    """Return packet-loss as a 0-1 fraction. Values > 1 are treated as percents."""
    if val is None:
        return None
    if val < 0:
        return None
    if val > 1.0:
        return val / 100.0
    return val


def _extract_axeos_extra_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Extract optional hexOS v2.13.1 / ESP-Miner / NerdQAxePlus fields, degrading gracefully when absent."""
    board_temps: list[float] = []
    bt = _num(data, "boardTemp")
    if bt is not None and bt > 0:
        board_temps.append(bt)

    fans: list[dict[str, Any]] = []
    raw_fans = data.get("fans")
    if isinstance(raw_fans, list) and raw_fans:
        for idx, fan in enumerate(raw_fans):
            if not isinstance(fan, dict):
                continue
            rpm = _opt_int(fan, "rpm")
            speed = _num(fan, "speedPerc", "speed_pct", "speed", "speedPercent", "fanspeed")
            label = str(fan.get("label") or "").strip()
            if (rpm is not None and rpm > 0) or (speed is not None and speed > 0):
                entry: dict[str, Any] = {"id": idx, "rpm": rpm, "speed_pct": speed}
                if label:
                    entry["label"] = label
                fans.append(entry)
    if not fans:
        fan_speed_pct = _num(data, "fanspeed", "fanSpeed")
        fan_speed_pct2 = _num(data, "fanspeed2", "fanSpeed2")
        if fan_speed_pct2 is None:
            fan_speed_pct2 = fan_speed_pct
        rpm1 = _opt_int(data, "fanrpm", "fanRPM")
        rpm2 = _opt_int(data, "fan2rpm", "fanrpm2", "fanRPM2")
        model_l = str(data.get("deviceModel") or "").lower()
        hint_no_tach = ("601" in model_l) or ("gamma" in model_l and "nerd" not in model_l and "octaxe" not in model_l)
        if rpm1 is not None and rpm1 > 0:
            fans.append({"id": 0, "rpm": rpm1, "speed_pct": fan_speed_pct})
        elif fan_speed_pct is not None and fan_speed_pct > 0:
            fans.append({"id": 0, "rpm": (None if (rpm1 is None or hint_no_tach) else 0), "speed_pct": fan_speed_pct, "no_tach": bool(rpm1 is None or hint_no_tach)})
        if rpm2 is not None and rpm2 > 0:
            fans.append({"id": 1, "rpm": rpm2, "speed_pct": fan_speed_pct2})

    raw_reasons = data.get("sharesRejectedReasons")
    reasons: list[dict[str, Any]] = []
    if isinstance(raw_reasons, list):
        for item in raw_reasons:
            if isinstance(item, dict):
                msg = str(item.get("message") or item.get("reason") or "").strip()
                cnt = _opt_int(item, "count")
                if msg:
                    reasons.append({"message": msg, "count": cnt if cnt is not None else 1})
            elif item is not None and str(item).strip():
                reasons.append({"message": str(item).strip(), "count": 1})

    stratum = _stratum_dict(data)
    connected = _connected_stratum_pool(data)

    best_src = data.get("bestDiff")
    if best_src is None:
        best_src = stratum.get("totalBestDiff")
    best_val, best_disp = parse_diff_value(best_src)

    best_sess_src = data.get("bestSessionDiff")
    if best_sess_src is None and connected is not None:
        best_sess_src = connected.get("bestDiff")
    best_sess_val, best_sess_disp = parse_diff_value(best_sess_src)

    last_diff_val, _ = parse_diff_value(data.get("lastSubmittedDiff"))
    net_diff_val, _ = parse_diff_value(data.get("networkDifficulty") or (connected or {}).get("networkDifficulty"))
    pool_diff_val, _ = parse_diff_value(data.get("poolDifficulty") or (connected or {}).get("poolDifficulty"))

    ping_rtt = _num(data, "lastpingrtt", "lastPingRtt", "pingRtt")
    ping_loss = _normalize_ping_loss(_num(data, "recentpingloss", "recentPingLoss", "pingLoss"))
    if connected is not None:
        if ping_rtt is None:
            ping_rtt = _num(connected, "pingRtt", "pingRTT")
        if ping_loss is None:
            ping_loss = _normalize_ping_loss(_num(connected, "pingLoss"))

    using_fallback = bool(data.get("isUsingFallbackStratum", False))
    if "usingFallback" in stratum:
        using_fallback = bool(stratum.get("usingFallback"))

    current_a = _num(data, "currentA")
    if current_a is None:
        raw_current = _num(data, "current")
        if raw_current is not None and raw_current > 100:
            current_a = raw_current / 1000.0
        else:
            current_a = raw_current

    asic_domains: list[dict[str, Any]] = []
    hm = data.get("hashrateMonitor")
    if isinstance(hm, dict) and isinstance(hm.get("asics"), list):
        for idx, asic_entry in enumerate(hm["asics"]):
            if not isinstance(asic_entry, dict):
                continue
            doms = []
            for dval in (asic_entry.get("domains") or []):
                try:
                    doms.append(float(dval))
                except (TypeError, ValueError):
                    continue
            err_cnt = _opt_int(asic_entry, "errorCount")
            total_hr = _num(asic_entry, "total")
            asic_domains.append(
                {
                    "id": idx,
                    "domains": doms,
                    "error_count": err_cnt,
                    "total": total_hr,
                }
            )

    return {
        "board_temps": board_temps,
        "fans": fans,
        "hashrate_1m": _num(data, "hashRate_1m", "hashRate1m"),
        "hashrate_10m": _num(data, "hashRate_10m", "hashRate10m"),
        "hashrate_1h": _num(data, "hashRate_1h", "hashRate1h"),
        "hashrate_24h": _num(data, "hashRate_24h", "hashRate_1d", "hashRate1d"),
        "input_voltage": _num(data, "voltage"),
        "input_current": current_a,
        "power_limit_w": _num(data, "maxPower", "powerLimit"),
        "rejected_reasons": reasons,
        "best_diff": best_val,
        "best_diff_display": best_disp,
        "best_session_diff": best_sess_val,
        "best_session_diff_display": best_sess_disp,
        "pool_difficulty": pool_diff_val,
        "last_submitted_diff": last_diff_val,
        "found_blocks": _found_blocks_from(data),
        "new_block_flag": _new_block_flag_from(data),
        "last_share_seconds": _num(data, "lastShareSeconds"),
        "ping_rtt_ms": ping_rtt,
        "ping_loss": ping_loss,
        "wifi_rssi": _opt_int(data, "wifiRSSI"),
        "stratum_disconnects": _opt_int(data, "stratumDisconnects"),
        "wifi_disconnects": _opt_int(data, "wifiDisconnects"),
        "using_fallback_stratum": using_fallback,
        "asic_domains": asic_domains,
        "block_height": _opt_int(data, "blockHeight"),
        "network_difficulty": net_diff_val,
        "ambient_temp": _ambient_from(data),
        "ambient_source": "miner" if _ambient_from(data) is not None else "",
        "fault_raw": extract_fault_fields(data),
    }


FAULT_FIELD_KEYS = (
    "overheat_mode", "power_fault", "hardware_fault", "miningPaused", "resetReason", "lastResetReason",
    "nominalVoltage", "minVoltage", "maxVoltage", "voltage", "currentA", "maxCurrentA", "power", "maxPower",
    "temp", "temp2", "vrTemp", "vrTempInt", "overheat_temp", "vrOverheatTemp", "overheatTemp",
    "fanspeed", "fanrpm", "fan2rpm", "fanrpm2", "fanRPM", "fanRPM2",
    "frequency", "expectedHashrate", "hashRate", "errorPercentage", "uptimeSeconds",
    "coreVoltage", "coreVoltageActual", "deviceModel", "ASICModel", "boardVersion", "asicCount",
    "poolConnectionInfo", "isUsingFallbackStratum", "useFallbackStratum", "primaryPoolError",
    "poolDifficulty", "asicDifficulty", "coinbasePayoutStatus",
    "wifiStatus", "wifiRSSI", "shutdown", "boardError",
)


def extract_fault_fields(data: Any) -> dict[str, Any]:
    """Copy documented AxeOS / NerdQAxe+ fault & UI-alert fields (no personal/wallet strings)."""
    out: dict[str, Any] = {}
    if not isinstance(data, dict):
        return out
    for k in FAULT_FIELD_KEYS:
        if k in data and data[k] is not None:
            out[k] = data[k]
    if isinstance(data.get("asicTemps"), list):
        out["asicTemps"] = list(data["asicTemps"])
    if isinstance(data.get("frequencyOptions"), list) and data["frequencyOptions"]:
        nums = [float(x) for x in data["frequencyOptions"] if isinstance(x, (int, float)) and x > 0]
        if nums:
            out["minFrequencyOpt"] = min(nums)
    cb_outs = data.get("coinbaseOutputs")
    if isinstance(cb_outs, list) and cb_outs and str(out.get("coinbasePayoutStatus") or "").lower() == "not_found":
        out["coinbaseMismatch"] = True
    hm = data.get("hashrateMonitor")
    if isinstance(hm, dict) and isinstance(hm.get("asics"), list):
        dead = [i for i, a in enumerate(hm["asics"]) if isinstance(a, dict) and _num(a, "total") == 0]
        if dead:
            out["dead_asics"] = dead
        err_cnts = {}
        for i, a in enumerate(hm["asics"]):
            if isinstance(a, dict):
                ec = _opt_int(a, "errorCount")
                if ec is not None and ec > 0:
                    err_cnts[str(i)] = ec
        if err_cnts:
            out["asic_error_counts"] = err_cnts
    # Safe stratum pool status summary (never copies pool URL/user/address)
    st = data.get("stratum")
    if isinstance(st, dict):
        if st.get("usingFallback") is not None and "isUsingFallbackStratum" not in out:
            out["isUsingFallbackStratum"] = st.get("usingFallback")
        pools = st.get("pools")
        if isinstance(pools, list):
            out["stratum_pools"] = [
                {
                    "connected": sp.get("connected"),
                    "active": bool(sp.get("active")),
                    "verifyBlocked": sp.get("verifyBlocked") or "",
                    "poolDiffErr": bool(sp.get("poolDiffErr")),
                }
                for sp in pools if isinstance(sp, dict)
            ]
    # NerdQAxe+ fans array (from /api/system/info or /api/v2/dashboard thermal.fans)
    sysd = data.get("system") if isinstance(data.get("system"), dict) else {}
    thermd = data.get("thermal") if isinstance(data.get("thermal"), dict) else {}
    pwrd = data.get("power") if isinstance(data.get("power"), dict) else {}
    perfd = data.get("performance") if isinstance(data.get("performance"), dict) else {}
    if sysd or thermd or pwrd:
        out["is_nerd"] = True
    for k in ("shutdown", "boardError", "lastResetReason", "resetReason", "overheatTemp", "vrOverheatTemp", "wifiRSSI"):
        if k in sysd and sysd[k] is not None and k not in out:
            out[k] = sysd[k]
    if sysd.get("uptime") is not None and "uptimeSeconds" not in out:
        out["uptimeSeconds"] = sysd["uptime"]
    for k in ("asicTemp", "vrTemp", "vrTempInt"):
        if k in thermd and thermd[k] is not None and k not in out:
            out[k] = thermd[k]
    if isinstance(thermd.get("asicTemps"), list) and "asicTemps" not in out:
        out["asicTemps"] = list(thermd["asicTemps"])
    raw_fans = thermd.get("fans") if isinstance(thermd.get("fans"), list) else (data.get("fans") if isinstance(data.get("fans"), list) else None)
    if isinstance(raw_fans, list):
        out["nerd_fans"] = [
            {"rpm": _opt_int(f, "rpm"), "speed": _num(f, "speed", "speedPerc", "speedPercent")}
            for f in raw_fans if isinstance(f, dict)
        ]
    for k in ("voltage", "minVoltage", "maxVoltage", "currentA", "maxCurrentA", "maxPower", "power"):
        if k in pwrd and pwrd[k] is not None and k not in out:
            out[k] = pwrd[k]
    cva = _num(pwrd, "coreVoltageActual")
    if cva is not None and "coreVoltageActual" not in out:
        out["coreVoltageActual"] = cva * 1000.0 if 0 < cva < 5 else cva
    if pwrd.get("watts") is not None and "power" not in out:
        out["power"] = pwrd["watts"]
    for k in ("hashRate", "expectedHashrate", "frequency", "errorPercentage", "coreVoltage"):
        if k in perfd and perfd[k] is not None and k not in out:
            out[k] = perfd[k]
    return out


def _extract_stratum_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Read primary & fallback pool URL/port/user (never reads or stores passwords)."""
    p_url = str(data.get("stratumURL") or "").strip()
    p_port = _opt_int(data, "stratumPort")
    p_user = str(data.get("stratumUser") or "").strip()
    f_url = str(data.get("fallbackStratumURL") or "").strip()
    f_port = _opt_int(data, "fallbackStratumPort")
    f_user = str(data.get("fallbackStratumUser") or "").strip()
    stratum = data.get("stratum")
    if isinstance(stratum, dict):
        pools = stratum.get("pools")
        if isinstance(pools, list):
            if len(pools) >= 1 and isinstance(pools[0], dict):
                p_url = p_url or str(pools[0].get("host") or pools[0].get("url") or "").strip()
                p_port = p_port or _opt_int(pools[0], "port")
                p_user = p_user or str(pools[0].get("user") or "").strip()
            if len(pools) >= 2 and isinstance(pools[1], dict):
                f_url = f_url or str(pools[1].get("host") or pools[1].get("url") or "").strip()
                f_port = f_port or _opt_int(pools[1], "port")
                f_user = f_user or str(pools[1].get("user") or "").strip()
    return {
        "pool_url": p_url,
        "pool_port": p_port,
        "pool_user": p_user,
        "fallback_pool_url": f_url,
        "fallback_pool_port": f_port,
        "fallback_pool_user": f_user,
    }


def parse_gamma_info(data: dict[str, Any]) -> Reading:
    temps = _positive_temps(data.get("asicTemps") or data.get("temps") or [])
    t1 = _num(data, "temp")
    t2 = _num(data, "temp2")
    if t1 is not None and t1 > 0:
        temps.append(t1)
    if t2 is not None and t2 > 0:
        temps.append(t2)
    asic = max(temps) if temps else None
    fans: list[int] = []
    for key in ("fanrpm", "fan2rpm", "fanrpm2", "fanRPM", "fanRPM2"):
        rpm = _opt_int(data, key)
        if rpm is not None and rpm > 0:
            fans.append(rpm)
    return Reading(
        ok=True,
        reachable=True,
        asic_temp=asic,
        vr_temp=_vr_temp_from(data),
        asic_temps=temps,
        frequency=_num(data, "frequency"),
        actual_frequency=_num(data, "actualFrequency"),
        hashrate=_num(data, "hashRate", "hashrate", "hashRate_10m", "hashRate10m"),
        power=_num(data, "power"),
        core_voltage=_num(data, "coreVoltage"),
        core_voltage_actual=_num(data, "coreVoltageActual"),
        version=str(data.get("version") or data.get("axeOSVersion") or ""),
        model=str(data.get("deviceModel") or data.get("ASICModel") or data.get("asicModel") or ""),
        source="GET /api/system/info",
        expected_hashrate=_num(data, "expectedHashrate"),
        error_percentage=_num(data, "errorPercentage"),
        shares_accepted=_shares_accepted_from_payload(data),
        shares_rejected=_shares_rejected_from_payload(data),
        fan_rpms=fans,
        uptime_sec=_num(data, "uptimeSeconds", "uptime"),
        **_extract_stratum_fields(data),
        **_extract_axeos_extra_fields(data),
    )


def parse_nerd_v2_dashboard(data: dict[str, Any]) -> Reading:
    thermal = data.get("thermal") if isinstance(data.get("thermal"), dict) else {}
    perf = data.get("performance") if isinstance(data.get("performance"), dict) else {}
    power = data.get("power") if isinstance(data.get("power"), dict) else {}
    system = data.get("system") if isinstance(data.get("system"), dict) else {}
    # 0-valued chip readings are missing sensors (seen on v1.0.37.1 NerdOCTAxe).
    temps = _positive_temps(thermal.get("asicTemps") or [])
    asic_fallback = _num(thermal, "asicTemp")
    if asic_fallback is not None and asic_fallback <= 0:
        asic_fallback = None
    asic = max(temps) if temps else asic_fallback
    actual = _num(power, "coreVoltageActual")
    # v2 dashboard reports coreVoltageActual in volts when the value is small.
    if actual is not None and 0 < actual < 5:
        actual = actual * 1000.0
    set_v = _num(data, "coreVoltage") or _num(perf, "coreVoltage")
    err_pct = _num(perf, "errorPercentage")
    if err_pct is None:
        chips = perf.get("chips") if isinstance(perf.get("chips"), list) else []
        good = 0.0
        bad = 0.0
        for chip in chips:
            if not isinstance(chip, dict):
                continue
            hr = _num(chip, "hashRate") or 0.0
            er = _num(chip, "errorHashRate") or 0.0
            if hr > 0:
                good += hr
            if er > 0:
                bad += er
        if good + bad > 0:
            err_pct = 100.0 * bad / (good + bad)
    fans: list[int] = []
    raw_fans = thermal.get("fans") or []
    if isinstance(raw_fans, list):
        for fan in raw_fans:
            if isinstance(fan, dict):
                rpm = _opt_int(fan, "rpm")
                if rpm is not None and rpm > 0:
                    fans.append(rpm)
    return Reading(
        ok=True,
        reachable=True,
        asic_temp=asic,
        vr_temp=_vr_temp_from(thermal),
        asic_temps=temps,
        frequency=_num(perf, "frequency"),
        actual_frequency=_num(perf, "actualFrequency", "frequency"),
        hashrate=_num(perf, "hashRate", "hashRate10m", "hashRate1m", "hashRate_10m", "hashRate_1m"),
        power=_num(power, "watts", "power"),
        core_voltage=set_v,
        core_voltage_actual=actual,
        version=str(data.get("version") or system.get("version") or ""),
        model=str(data.get("deviceModel") or system.get("deviceModel") or ""),
        source="GET /api/v2/dashboard",
        fault_raw=extract_fault_fields(data),
        expected_hashrate=_num(perf, "expectedHashrate"),
        error_percentage=err_pct,
        shares_accepted=_opt_int(perf, "sharesAccepted"),
        shares_rejected=_opt_int(perf, "sharesRejected"),
        fan_rpms=fans,
        fans=[
            {"id": idx, "rpm": _opt_int(f, "rpm"), "speed_pct": _num(f, "speed", "speedPerc", "speedPercent")}
            for idx, f in enumerate(raw_fans)
            if isinstance(f, dict) and _opt_int(f, "rpm") is not None
        ],
        hashrate_1m=_num(perf, "hashRate_1m", "hashRate1m"),
        hashrate_10m=_num(perf, "hashRate_10m", "hashRate10m"),
        hashrate_1h=_num(perf, "hashRate_1h", "hashRate1h"),
        best_diff=parse_diff_value(perf.get("bestDiff") or data.get("bestDiff"))[0],
        best_diff_display=parse_diff_value(perf.get("bestDiff") or data.get("bestDiff"))[1],
        best_session_diff=parse_diff_value(perf.get("bestSessionDiff") or data.get("bestSessionDiff"))[0],
        best_session_diff_display=parse_diff_value(perf.get("bestSessionDiff") or data.get("bestSessionDiff"))[1],
        last_submitted_diff=parse_diff_value(perf.get("lastSubmittedDiff") or data.get("lastSubmittedDiff"))[0],
        found_blocks=_found_blocks_from(perf) if _found_blocks_from(perf) is not None else _found_blocks_from(data),
        last_share_seconds=_num(perf, "lastShareSeconds") or _num(data, "lastShareSeconds"),
        wifi_rssi=_opt_int(system, "wifiRSSI") or _opt_int(data, "wifiRSSI"),
        uptime_sec=_num(system, "uptime", "uptimeSeconds"),
        **_extract_stratum_fields(data),
    )


def parse_nerd_v1_info(data: dict[str, Any]) -> Reading:
    # On the NerdQAxe fork, `temp` is the maximum chip temperature reported by
    # firmware. Per-chip `asicTemps` may be all zeros (observed on v1.0.37.1
    # NerdOCTAxe) — treat 0 as missing and fall back to `temp`. Never treat 0
    # as a valid/cool reading.
    temps = _positive_temps(data.get("asicTemps") or data.get("temps") or [])
    fallback = _num(data, "temp")
    if fallback is not None and fallback <= 0:
        fallback = None
    asic = max(temps) if temps else fallback
    fans: list[int] = []
    for key in ("fanrpm", "fan2rpm", "fanrpm2", "fanRPM", "fanRPM2"):
        rpm = _opt_int(data, key)
        if rpm is not None and rpm > 0:
            fans.append(rpm)
    model = str(
        data.get("deviceModel")
        or data.get("asicModel")
        or data.get("ASICModel")
        or ""
    )
    return Reading(
        ok=True,
        reachable=True,
        asic_temp=asic,
        vr_temp=_vr_temp_from(data),
        asic_temps=temps,
        frequency=_num(data, "frequency"),
        actual_frequency=_num(data, "actualFrequency"),
        hashrate=_num(data, "hashRate", "hashrate", "hashRate_10m", "hashRate10m", "hashRate_1m", "hashRate1m"),
        power=_num(data, "power"),
        core_voltage=_num(data, "coreVoltage"),
        core_voltage_actual=_num(data, "coreVoltageActual"),
        version=str(data.get("version") or ""),
        model=model,
        source="GET /api/system/info",
        expected_hashrate=_num(data, "expectedHashrate"),
        error_percentage=_num(data, "errorPercentage"),
        shares_accepted=_shares_accepted_from_payload(data),
        shares_rejected=_shares_rejected_from_payload(data),
        fan_rpms=fans,
        uptime_sec=_num(data, "uptimeSeconds", "uptime"),
        **_extract_stratum_fields(data),
        **_extract_axeos_extra_fields(data),
    )


def enrich_reading_identity(reading: Reading, *payloads: Optional[dict[str, Any]]) -> Reading:
    """Fill model/version from /api/v2/identify or /api/v2/system when missing."""
    if reading.model and reading.version:
        return reading
    for data in payloads:
        if not isinstance(data, dict):
            continue
        if not reading.model:
            reading.model = str(data.get("deviceModel") or data.get("asicModel") or data.get("ASICModel") or "")
        if not reading.version:
            reading.version = str(data.get("version") or "")
        if not reading.hostname:
            reading.hostname = str(data.get("hostname") or "").strip()
        if reading.model and reading.version and reading.hostname:
            break
    return reading


# ---------------------------------------------------------------------------
# Frequency stepping
# ---------------------------------------------------------------------------

def next_frequency(
    current: float,
    direction: int,
    step: float,
    min_freq: float,
    max_freq: float,
    options: Optional[list[float]] = None,
    min_delta: Optional[float] = None,
) -> float:
    """Move frequency one step (or at least min_delta) toward direction, clamped."""
    current = float(current)
    min_freq = float(min_freq)
    max_freq = float(max_freq)
    if max_freq < min_freq:
        max_freq = min_freq
    # min_delta is only a "skip at least this far" floor, used for emergency steps.
    # Normal preset steps move to the next adjacent option.
    delta = float(min_delta if min_delta is not None else step)
    force_delta = min_delta is not None

    if options:
        opts = sorted({float(f) for f in options if min_freq - 1e-6 <= float(f) <= max_freq + 1e-6})
        if opts:
            if direction < 0:
                lower = [f for f in opts if f < current - 1e-6]
                if not lower:
                    return max(min_freq, min(current, max_freq))
                if force_delta:
                    far = [f for f in lower if current - f >= delta - 1e-6]
                    return far[-1] if far else lower[0]
                return lower[-1]
            higher = [f for f in opts if f > current + 1e-6]
            if not higher:
                return min(max_freq, max(current, min_freq))
            # Step up one preset only, never past max_freq (already filtered).
            return higher[0]

    if direction < 0:
        new = current - delta
    else:
        new = current + float(step)
    if new < min_freq:
        new = min_freq
    if new > max_freq:
        new = max_freq
    return new



def compute_error_sample(reading: Reading, state: "DeviceState") -> tuple[Optional[float], str]:
    """Extract a hardware error percentage sample for the rolling window."""
    # Always update share counters so delta fallback stays accurate.
    prev_acc = state.last_shares_accepted
    prev_rej = state.last_shares_rejected
    if reading.shares_accepted is not None and reading.shares_rejected is not None:
        state.last_shares_accepted = reading.shares_accepted
        state.last_shares_rejected = reading.shares_rejected

    if reading.error_percentage is not None:
        return max(0.0, float(reading.error_percentage)), "errorPercentage"

    if reading.shares_accepted is not None and reading.shares_rejected is not None:
        if prev_acc is not None and prev_rej is not None:
            da = reading.shares_accepted - prev_acc
            dr = reading.shares_rejected - prev_rej
            if da >= 0 and dr >= 0 and (da + dr) > 0:
                return (dr / (da + dr)) * 100.0, "sharesRejected"
        total = reading.shares_accepted + reading.shares_rejected
        if total > 0:
            return (reading.shares_rejected / total) * 100.0, "sharesRejected"

    if (
        reading.expected_hashrate is not None
        and reading.expected_hashrate > 0
        and reading.hashrate is not None
    ):
        gap = max(0.0, (reading.expected_hashrate - reading.hashrate) / reading.expected_hashrate * 100.0)
        return gap, "hashrateGap"

    return None, ""


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    action: str
    new_freq: Optional[float]
    reason: str
    cool_polls: int
    bypass_cooldown: bool = False
    new_voltage: Optional[int] = None
    low_error_polls: int = 0
    voltage_reason: str = ""
    thermal_step_down: bool = False
    cooldown_override_sec: Optional[float] = None


def _effective_core_voltage_mv(reading: Reading, cfg: DeviceConfig) -> int:
    raw_v: Optional[float] = None
    if reading.core_voltage is not None and reading.core_voltage > 0:
        raw_v = float(reading.core_voltage)
    elif reading.core_voltage_actual is not None and reading.core_voltage_actual > 0:
        raw_v = float(reading.core_voltage_actual)
    if raw_v is None:
        return int(cfg.voltage_start_mv)
    if 0 < raw_v < 5.0:
        raw_v *= 1000.0
    return int(round(raw_v))


def _electrical_guard(reading: Reading, cfg: DeviceConfig) -> tuple[str, str]:
    """Return ('step_down'|'hold_up'|'', reason) for power/current >=85%/95% and input rail sag."""
    fr = reading.fault_raw if isinstance(reading.fault_raw, dict) else {}
    pwr = reading.power
    pwr_lim = reading.power_limit_w if reading.power_limit_w else _num(fr, "maxPower")
    if pwr is not None and pwr_lim is not None and pwr_lim > 0:
        pct = pwr / pwr_lim
        if pct >= 0.95:
            return "step_down", f"power {pwr:.1f} W >= 95% of {pwr_lim:g} W limit"
        if pct >= 0.85:
            return "hold_up", f"power {pwr:.1f} W >= 85% of {pwr_lim:g} W limit; blocking step-up"
    cur = reading.input_current
    cur_lim = _num(fr, "maxCurrentA")
    if cur is not None and cur_lim is not None and cur_lim > 0:
        cpct = cur / cur_lim
        if cpct >= 0.95:
            return "step_down", f"current {cur:.1f} A >= 95% of {cur_lim:g} A max"
        if cpct >= 0.85:
            return "hold_up", f"current {cur:.1f} A >= 85% of {cur_lim:g} A max; blocking step-up"
    vin = reading.input_voltage
    if vin is not None and vin > 0:
        vin_v = vin / 1000.0 if vin > 100 else vin
        is_12v = (
            cfg.type == "nerdoctaxe"
            or (_num(fr, "minVoltage") or 0) >= 8.0
            or (_num(fr, "nominalVoltage") or 0) >= 8.0
            or vin_v >= 7.5
        )
        if is_12v:
            if vin_v < 11.3:
                return "step_down", f"12 V input sag ({vin_v:.2f} V < 11.3 V)"
            if vin_v < 11.6:
                return "hold_up", f"12 V input sag ({vin_v:.2f} V < 11.6 V); blocking step-up"
        else:
            if vin_v < 4.65:
                return "step_down", f"5 V input sag ({vin_v:.2f} V < 4.65 V)"
            if vin_v < 4.85:
                return "hold_up", f"5 V input sag ({vin_v:.2f} V < 4.85 V); blocking step-up"
    return "", ""


def decide(
    reading: Reading,
    cfg: DeviceConfig,
    cool_polls: int,
    error_pct: Optional[float] = None,
    low_error_polls: int = 0,
    *,
    active_faults: Optional[list[dict[str, Any]]] = None,
    vr_rise_c: Optional[float] = None,
    vr_rise_dt: Optional[float] = None,
    lockout_remaining_sec: float = 0.0,
    rollback_freq: Optional[float] = None,
    sensor_suspect_reason: str = "",
    latest_error_pct: Optional[float] = None,
    error_source: str = "",
    error_window_count: Optional[int] = None,
    learned_min_voltage_mv: Optional[int] = None,
) -> Decision:
    if not reading.ok:
        return Decision("offline", None, reading.error or "device offline", 0, low_error_polls=0)

    if not is_tunable(cfg):
        return Decision(
            "monitor",
            None,
            reading.firmware_note or f"monitor only ({reading.firmware or cfg.type})",
            0,
            low_error_polls=0,
            voltage_reason="monitor only",
        )

    fr = reading.fault_raw if isinstance(reading.fault_raw, dict) else {}
    unpowered_cv = (
        reading.core_voltage_actual is not None
        and reading.core_voltage_actual <= 0
        and reading.core_voltage is not None
        and reading.core_voltage > 0
    )
    unpowered_pwr = reading.power is not None and reading.power <= 2.0
    if unpowered_cv or unpowered_pwr:
        why = (
            f"coreVoltageActual={reading.core_voltage_actual:.0f} mV"
            if unpowered_cv and reading.core_voltage_actual is not None
            else f"power={reading.power:.2f} W"
        )
        return Decision(
            "fault_hold",
            None,
            f"chip power rail off ({why}); autotuner holding (no step-up)",
            0,
            low_error_polls=0,
            voltage_reason="fault hold (chip power rail off)",
        )

    asic = reading.asic_temp
    vr = reading.vr_temp
    freq = reading.frequency
    if asic is None or vr is None or freq is None:
        return Decision(
            "invalid",
            None,
            "missing telemetry (need ASIC temp, VR temp, and frequency)",
            0,
            low_error_polls=0,
        )
    if asic <= 0 or vr <= 0 or freq <= 0:
        return Decision(
            "invalid",
            None,
            "sensor or frequency reading is zero or negative",
            0,
            low_error_polls=0,
        )

    cur_v = _effective_core_voltage_mv(reading, cfg)
    err_str = f"{error_pct:.2f}%" if error_pct is not None else "n/a"
    hw_error_source_ok = error_source in {"", "errorPercentage"}
    effective_err_for_high = error_pct
    if hw_error_source_ok and latest_error_pct is not None:
        effective_err_for_high = (
            max(error_pct, latest_error_pct) if error_pct is not None else latest_error_pct
        )
    if (
        hw_error_source_ok
        and latest_error_pct is not None
        and (error_pct is None or latest_error_pct > error_pct)
        and latest_error_pct > cfg.error_high_pct
    ):
        err_high_str = (
            f"{latest_error_pct:.2f}% (avg {error_pct:.2f}%)"
            if error_pct is not None
            else f"{latest_error_pct:.2f}%"
        )
    else:
        err_high_str = f"{effective_err_for_high:.2f}%" if effective_err_for_high is not None else "n/a"
    emerg_v = int(getattr(cfg, "emergency_voltage_mv", DEFAULT_EMERGENCY_VOLTAGE_MV))

    def down_freq(
        label: str,
        bypass: bool,
        min_delta: Optional[float] = None,
        v_reason: str = "",
        emerg_new_v: Optional[int] = None,
    ) -> Decision:
        new_f = next_frequency(
            freq,
            -1,
            cfg.step_mhz,
            cfg.min_freq,
            cfg.max_freq,
            cfg.frequency_options,
            min_delta=min_delta,
        )
        if new_f < freq - 1e-6:
            reason = _hot_reason(asic, vr, cfg, label, freq, new_f)
            if emerg_new_v is not None:
                reason += f"; resetting voltage {cur_v} -> {emerg_new_v} mV"
            return Decision(
                label,
                new_f,
                reason,
                0,
                bypass,
                new_voltage=emerg_new_v,
                low_error_polls=0,
                voltage_reason=v_reason,
            )
        if emerg_new_v is not None:
            return Decision(
                "voltage_down",
                None,
                (
                    f"temps high (ASIC {asic:.1f}C / VR {vr:.1f}C) at min {cfg.min_freq:.0f} MHz; "
                    f"lowering voltage {cur_v} -> {emerg_new_v} mV"
                ),
                0,
                bypass,
                new_voltage=emerg_new_v,
                low_error_polls=0,
                voltage_reason=v_reason,
            )
        return Decision(
            "hold",
            None,
            (
                f"temps still high (ASIC {asic:.1f}C / VR {vr:.1f}C) but frequency "
                f"already at min {cfg.min_freq:.0f} MHz"
            ),
            0,
            bypass,
            low_error_polls=0,
            voltage_reason=v_reason,
        )

    # 0b. Split-sensor plausibility check (VR > 105 C while ASIC < 45 C -> safe step_down + suspect warning)
    if vr > 105.0 and asic < 45.0:
        s_why = sensor_suspect_reason or f"suspect sensor reading (VR {vr:.1f} C while ASIC {asic:.1f} C)"
        new_f = next_frequency(freq, -1, cfg.step_mhz, cfg.min_freq, cfg.max_freq, cfg.frequency_options)
        if new_f < freq - 1e-6:
            return Decision(
                "step_down",
                new_f,
                f"{s_why}; stepping down {freq:.0f} -> {new_f:.0f} MHz",
                0,
                bypass_cooldown=True,
                low_error_polls=0,
                voltage_reason="suspect sensor safety step-down",
                thermal_step_down=True,
            )
        return Decision(
            "fault_hold",
            None,
            f"{s_why}; holding at min {cfg.min_freq:.0f} MHz",
            0,
            low_error_polls=0,
            voltage_reason="suspect sensor hold",
        )

    # 1. Temp emergency always wins (ASIC >= 62.5C or VR >= 65C by default):
    #    - Drop frequency by emergency_step_mhz (50 MHz), bypassing cooldown.
    #    - Voltage-tunable devices: set voltage to emergency_voltage_mv (1210 mV) ONLY if currently > 1210 mV,
    #      leaving it unchanged if already <= 1210 mV.
    #    - Frequency-only VR-relief devices (e.g. OCTAxe): lower voltage ONLY when VR temp is too high.
    if asic >= cfg.asic_max_c or vr >= cfg.vr_max_c:
        emerg_new_v: Optional[int] = None
        if cfg.voltage_control:
            if cur_v > emerg_v:
                emerg_new_v = emerg_v
                vr_note = f"emergency reset {cur_v} -> {emerg_v} mV"
            else:
                vr_note = f"held at {cur_v} mV (<= {emerg_v} mV emergency cap)"
        elif cfg.vr_voltage_relief and vr >= cfg.vr_max_c and cur_v > cfg.min_voltage_mv:
            emerg_new_v = max(int(cfg.min_voltage_mv), min(emerg_v, cur_v - int(cfg.voltage_step_mv)))
            if emerg_new_v == cur_v:
                emerg_new_v = None
                vr_note = f"VR emergency held at {cur_v} mV"
            else:
                vr_note = f"VR temp {vr:.1f}C >= {cfg.vr_max_c:.1f}C; lowering voltage {cur_v} -> {emerg_new_v} mV"
        else:
            vr_note = "frequency-only (VR temp OK; voltage held)"
        return down_freq(
            "emergency_down",
            True,
            min_delta=cfg.emergency_step_mhz,
            v_reason=vr_note,
            emerg_new_v=emerg_new_v,
        )

    # 2. Temp above target steps frequency down; never raise voltage when above target.
    #    For VR-relief devices (OCTAxe), also lower voltage if VR temp exceeds target.
    if asic > cfg.asic_target_c or vr > cfg.vr_target_c:
        relief_v: Optional[int] = None
        if cfg.voltage_control:
            vr_note = "held (temp above target; will not raise voltage)"
        elif cfg.vr_voltage_relief and vr > cfg.vr_target_c and cur_v > cfg.min_voltage_mv:
            relief_v = max(int(cfg.min_voltage_mv), cur_v - int(cfg.voltage_step_mv))
            vr_note = f"VR temp {vr:.1f}C > {cfg.vr_target_c:.1f}C; lowering voltage {cur_v} -> {relief_v} mV"
        else:
            vr_note = "frequency-only"
        d_td = down_freq("step_down", False, v_reason=vr_note, emerg_new_v=relief_v)
        d_td.thermal_step_down = True
        return d_td

    # 2b. Sensor plausibility check (>25 C jump flagged by tuner OR VR > 105 C while ASIC < 45 C)
    split_suspect = (vr > 105.0 and asic < 45.0)
    if sensor_suspect_reason or split_suspect:
        s_why = sensor_suspect_reason or f"suspect sensor reading (VR {vr:.1f} C while ASIC {asic:.1f} C)"
        new_f = next_frequency(freq, -1, cfg.step_mhz, cfg.min_freq, cfg.max_freq, cfg.frequency_options)
        if new_f < freq - 1e-6:
            return Decision(
                "step_down",
                new_f,
                f"{s_why}; stepping down {freq:.0f} -> {new_f:.0f} MHz",
                0,
                bypass_cooldown=True,
                low_error_polls=0,
                voltage_reason="suspect sensor safety step-down",
                thermal_step_down=True,
            )
        return Decision(
            "fault_hold",
            None,
            f"{s_why}; holding at min {cfg.min_freq:.0f} MHz",
            0,
            low_error_polls=0,
            voltage_reason="suspect sensor hold",
        )

    # 2c. VR rate-of-rise guard (rapid VR climb before hitting vr_max_c).
    # v1.15.5: a VR warming up from cold in the first minutes after a (re)boot is normal, not a runaway, as long as it is
    # still well under target; the absolute target / max / emergency limits above keep applying.
    boot_warmup = (reading.uptime_sec is not None and reading.uptime_sec < 300 and vr < cfg.vr_target_c - 5.0)
    if (not boot_warmup and vr_rise_c is not None and vr_rise_dt is not None and vr_rise_dt <= 65.0 and vr_rise_c >= 4.0
            and vr >= max(52.0, cfg.vr_target_c - 10.0)):
        new_f = next_frequency(freq, -1, cfg.step_mhz, cfg.min_freq, cfg.max_freq, cfg.frequency_options)
        if new_f < freq - 1e-6:
            return Decision(
                "step_down",
                new_f,
                f"VR rapid rise (+{vr_rise_c:.1f} C in {vr_rise_dt:.0f}s at VR {vr:.1f} C); stepping down {freq:.0f} -> {new_f:.0f} MHz",
                0,
                bypass_cooldown=True,
                low_error_polls=0,
                voltage_reason="VR rate-of-rise guard",
                thermal_step_down=True,
            )

    # 2d. Hard fault_hold on 0 GH/s, shutdown/boardError/overheat_mode/power_fault/miningPaused, or active problem fault
    fw_fault_reasons: list[str] = []
    if bool(fr.get("shutdown")):
        fw_fault_reasons.append("shutdown=true")
    try:
        if int(fr.get("boardError") or 0) != 0:
            fw_fault_reasons.append(f"boardError={fr.get('boardError')}")
    except (TypeError, ValueError):
        fw_fault_reasons.append(f"boardError={fr.get('boardError')}")
    if not nk_faults._is_falsy_flag(fr.get("overheat_mode")):
        fw_fault_reasons.append("overheat_mode")
    if not nk_faults._is_falsy_flag(fr.get("power_fault")):
        fw_fault_reasons.append("power_fault")
    if bool(fr.get("miningPaused")):
        fw_fault_reasons.append("miningPaused")
    prob_faults = [f for f in (active_faults or []) if isinstance(f, dict) and f.get("severity") == "problem"]
    if prob_faults:
        fw_fault_reasons.append(str(prob_faults[0].get("title") or prob_faults[0].get("key") or "active fault"))
    zero_hr = reading.hashrate is not None and reading.hashrate <= 0 and (reading.uptime_sec is None or reading.uptime_sec >= 60)
    if zero_hr:
        fw_fault_reasons.append("0 GH/s hashrate")
    if fw_fault_reasons:
        return Decision(
            "fault_hold",
            None,
            f"fault hold ({', '.join(fw_fault_reasons)}); no step-up",
            0,
            low_error_polls=0,
            voltage_reason="fault hold",
        )

    # 2e. Power, current, and input-voltage sag guards
    elec_act, elec_reason = _electrical_guard(reading, cfg)
    if elec_act == "step_down":
        new_f = next_frequency(freq, -1, cfg.step_mhz, cfg.min_freq, cfg.max_freq, cfg.frequency_options)
        if new_f < freq - 1e-6:
            return Decision(
                "step_down",
                new_f,
                f"{elec_reason}; stepping down {freq:.0f} -> {new_f:.0f} MHz",
                0,
                bypass_cooldown=True,
                low_error_polls=0,
                voltage_reason="electrical limit step-down",
                thermal_step_down=True,
            )
        return Decision(
            "hold",
            None,
            f"{elec_reason}; already at min {cfg.min_freq:.0f} MHz",
            0,
            low_error_polls=0,
            voltage_reason="electrical limit hold",
        )

    # 2f. Post-emergency lockout & rollback to last stable frequency
    if lockout_remaining_sec > 0:
        if rollback_freq is not None and rollback_freq >= cfg.min_freq - 1e-6 and freq > rollback_freq + 1e-6:
            new_f = next_frequency(freq, -1, cfg.step_mhz, cfg.min_freq, min(cfg.max_freq, rollback_freq), cfg.frequency_options)
            target_f = max(cfg.min_freq, min(new_f, rollback_freq))
            if target_f < freq - 1e-6:
                return Decision(
                    "step_down",
                    target_f,
                    f"post-emergency rollback toward stable {rollback_freq:.0f} MHz ({lockout_remaining_sec / 60.0:.0f}m lockout left)",
                    0,
                    bypass_cooldown=True,
                    low_error_polls=0,
                    voltage_reason="post-emergency rollback",
                    thermal_step_down=True,
                )
        elec_act = "hold_up"
        elec_reason = f"post-emergency lockout ({lockout_remaining_sec / 60.0:.0f}m remaining); blocking step-up"

    # 3. Enforce hard voltage cap if voltage-tunable miner started above max_voltage_mv.
    if cfg.voltage_control and cur_v > cfg.max_voltage_mv:
        return Decision(
            "voltage_down",
            None,
            f"coreVoltage {cur_v} mV exceeds cap {cfg.max_voltage_mv} mV; lowering to {cfg.max_voltage_mv} mV",
            0,
            new_voltage=int(cfg.max_voltage_mv),
            low_error_polls=0,
            voltage_reason=f"clamped from {cur_v} to cap {cfg.max_voltage_mv} mV",
        )

    # 4. Error-tolerance check (v1.7: target 0%, 0.5-1.0% acceptable, >1.0% out of tolerance).
    #    Use max(rolling, latest) for real hardware errorPercentage so rolling average cannot mask spikes.
    if cfg.voltage_control and effective_err_for_high is not None and effective_err_for_high > cfg.error_high_pct:
        if cur_v < cfg.max_voltage_mv and elec_act != "hold_up":
            at_freq_cap = freq >= cfg.max_freq - 1.0
            severe_err = effective_err_for_high >= max(SEVERE_ERROR_PCT_FLOOR, cfg.error_high_pct * 2.0)
            step_mv = int(cfg.voltage_step_mv) * 2 if (at_freq_cap and severe_err) else int(cfg.voltage_step_mv)
            cap_mv = min(int(cfg.max_voltage_mv), HARD_MAX_VOLTAGE_MV)
            new_v = min(cap_mv, cur_v + max(1, step_mv))
            cd_override = min(float(cfg.cooldown_sec), SEVERE_ERROR_VOLTAGE_COOLDOWN_SEC) if (at_freq_cap and severe_err) else None
            v_reason = f"HW error {err_high_str} > {cfg.error_high_pct:.2f}% max; raising {cur_v} -> {new_v} mV"
            return Decision(
                "voltage_up",
                None,
                v_reason,
                0,
                new_voltage=new_v,
                low_error_polls=0,
                voltage_reason=v_reason,
                cooldown_override_sec=cd_override,
            )
        new_f = next_frequency(
            freq,
            -1,
            cfg.step_mhz,
            cfg.min_freq,
            cfg.max_freq,
            cfg.frequency_options,
        )
        at_v_cap = cur_v >= cfg.max_voltage_mv
        limit_note = elec_reason if (elec_act == "hold_up" and not at_v_cap) else f"at {cfg.max_voltage_mv} mV cap"
        if new_f < freq - 1e-6:
            v_reason = f"{elec_reason}; stepping freq down" if elec_act == "hold_up" else f"at {cfg.max_voltage_mv} mV cap with HW error {err_high_str}; stepping freq down"
            return Decision(
                "step_down",
                new_f,
                f"HW error {err_high_str} > {cfg.error_high_pct:.2f}% {limit_note}; {freq:.0f} -> {new_f:.0f} MHz",
                0,
                low_error_polls=0,
                voltage_reason=v_reason,
            )
        return Decision(
            "hold",
            None,
            f"HW error {err_high_str} > {cfg.error_high_pct:.2f}% {limit_note} and min {cfg.min_freq:.0f} MHz",
            0,
            low_error_polls=0,
            voltage_reason=f"{limit_note} and min freq",
        )

    # 5. Normal frequency step-up while comfortably cool AND error rate is in tolerance (<= error_high_pct).
    error_acceptable = effective_err_for_high is None or effective_err_for_high <= cfg.error_high_pct
    comfortably_cool = (
        asic <= cfg.asic_target_c - cfg.hysteresis_c
        and vr <= cfg.vr_target_c - cfg.hysteresis_c
    )
    if comfortably_cool and not error_acceptable:
        return Decision(
            "hold",
            None,
            f"comfortably cool (ASIC {asic:.1f}C / VR {vr:.1f}C) but HW error {err_high_str} > {cfg.error_high_pct:.2f}%; blocking step-up",
            0,
            low_error_polls=0,
            voltage_reason=f"frequency-only ({cur_v} mV hold)" if not cfg.voltage_control else f"error {err_high_str} blocks freq step-up",
        )

    if comfortably_cool and error_acceptable and elec_act == "hold_up" and freq < cfg.max_freq - 1e-6:
        return Decision(
            "hold",
            None,
            elec_reason,
            0,
            low_error_polls=0,
            voltage_reason=elec_reason,
        )

    if comfortably_cool and error_acceptable:
        streak = cool_polls + 1
        if freq < cfg.max_freq - 1e-6:
            if streak >= cfg.cool_polls_required:
                new_f = next_frequency(
                    freq,
                    1,
                    cfg.step_mhz,
                    cfg.min_freq,
                    cfg.max_freq,
                    cfg.frequency_options,
                )
                if new_f > freq + 1e-6 and new_f <= cfg.max_freq + 1e-6:
                    v_note = (
                        f"error {err_str} in tolerance; keeping {cur_v} mV while stepping freq up"
                        if cfg.voltage_control
                        else f"frequency-only (holding {cur_v} mV)"
                    )
                    return Decision(
                        "step_up",
                        new_f,
                        (
                            f"ASIC {asic:.1f}C and VR {vr:.1f}C stayed below target-"
                            f"hysteresis for {streak} polls (HW error {err_str}); "
                            f"step {freq:.0f} -> {new_f:.0f} MHz"
                        ),
                        0,
                        low_error_polls=0,
                        voltage_reason=v_note,
                    )
            return Decision(
                "hold",
                None,
                (
                    f"comfortably cool (ASIC {asic:.1f}C / VR {vr:.1f}C, HW error {err_str}); "
                    f"waiting for {streak}/{cfg.cool_polls_required} cool polls before step-up"
                ),
                streak,
                low_error_polls=0,
                voltage_reason="holding while building cool streak" if cfg.voltage_control else f"frequency-only (holding {cur_v} mV)",
            )
        cool_polls = streak
    else:
        cool_polls = 0

    # 6. Aim for the lowest voltage that keeps errors in tolerance (do NOT chase 0% with high voltage).
    #    Only lower voltage using real hardware errorPercentage (never pool-share or hashrate-gap fallbacks),
    #    only after warmup and with a full error window, never when the latest poll is >= error_low_pct,
    #    and never below any learned bad-voltage stability floor for this frequency.
    eff_min_v = int(cfg.min_voltage_mv)
    has_learned_floor = False
    if learned_min_voltage_mv is not None:
        clamped_floor = min(int(cfg.max_voltage_mv), int(learned_min_voltage_mv))
        if clamped_floor > eff_min_v:
            eff_min_v = clamped_floor
            has_learned_floor = True
    latest_allows_drop = latest_error_pct is None or latest_error_pct < cfg.error_low_pct
    window_ready = error_window_count is None or error_window_count >= max(1, int(cfg.error_window_polls))
    uptime_ready = reading.uptime_sec is None or reading.uptime_sec >= MIN_VOLTAGE_DROP_UPTIME_SEC
    hashrate_ready = reading.hashrate is None or reading.hashrate > 0
    if cfg.voltage_control and error_pct is not None and not hw_error_source_ok:
        low_error_polls = 0
        v_reason = f"holding {cur_v} mV (ASIC errorPercentage unavailable; refusing to lower voltage using {error_source})"
    elif cfg.voltage_control and error_pct is not None and error_pct < cfg.error_low_pct and latest_allows_drop:
        if not uptime_ready or not hashrate_ready:
            low_error_polls = 0
            v_reason = f"holding {cur_v} mV while miner stabilizes before trimming voltage"
        elif cur_v > eff_min_v:
            v_streak = low_error_polls + 1
            if v_streak >= cfg.low_error_polls_required and window_ready:
                new_v = max(eff_min_v, cur_v - int(cfg.voltage_step_mv))
                v_reason = (
                    f"HW error {err_str} < {cfg.error_low_pct:.2f}% for {v_streak} polls; "
                    f"lowering {cur_v} -> {new_v} mV to find lowest stable voltage"
                )
                return Decision(
                    "voltage_down",
                    None,
                    v_reason,
                    cool_polls,
                    new_voltage=new_v,
                    low_error_polls=0,
                    voltage_reason=v_reason,
                )
            if v_streak >= cfg.low_error_polls_required and not window_ready:
                v_reason = (
                    f"low HW error {err_str} < {cfg.error_low_pct:.2f}%; "
                    f"waiting for full {cfg.error_window_polls}-poll error window "
                    f"({error_window_count}/{cfg.error_window_polls})"
                )
            else:
                v_reason = (
                    f"low HW error {err_str} < {cfg.error_low_pct:.2f}%; "
                    f"waiting {v_streak}/{cfg.low_error_polls_required} polls before lowering voltage"
                )
            return Decision(
                "hold",
                None,
                v_reason,
                cool_polls,
                low_error_polls=v_streak,
                voltage_reason=v_reason,
            )
        else:
            if has_learned_floor:
                v_reason = f"HW error {err_str} low; holding at learned stability floor {eff_min_v} mV"
            else:
                v_reason = f"HW error {err_str} low; already at voltage floor {eff_min_v} mV"
    elif cfg.voltage_control:
        if error_pct is None:
            v_reason = f"holding {cur_v} mV (no HW error metric yet)"
        elif not latest_allows_drop and latest_error_pct is not None:
            low_error_polls = 0
            v_reason = f"latest HW error {latest_error_pct:.2f}% >= {cfg.error_low_pct:.2f}% (avg {err_str}); holding {cur_v} mV"
        else:
            v_reason = f"HW error {err_str} acceptable ({cfg.error_low_pct:.2f}-{cfg.error_high_pct:.2f}%); holding {cur_v} mV"
    else:
        v_reason = f"frequency-only (holding {cur_v} mV; lowers only on high VR temp)"

    if comfortably_cool and freq >= cfg.max_freq - 1e-6:
        return Decision(
            "hold",
            None,
            f"comfortably cool (ASIC {asic:.1f}C / VR {vr:.1f}C) at max {cfg.max_freq:.0f} MHz",
            cool_polls,
            low_error_polls=0,
            voltage_reason=v_reason,
        )

    return Decision(
        "hold",
        None,
        (
            f"in hysteresis band (ASIC {asic:.1f}C target {cfg.asic_target_c:.0f}-"
            f"{cfg.hysteresis_c:.0f}, VR {vr:.1f}C target {cfg.vr_target_c:.0f}-"
            f"{cfg.hysteresis_c:.0f}); holding {freq:.0f} MHz"
        ),
        0,
        low_error_polls=0,
        voltage_reason=v_reason,
    )


def decide_bos_power(
    reading: Reading,
    cfg: DeviceConfig,
    cool_polls: int,
    last_change_ts: float,
    now_ts: float,
    current_target_w: Optional[int] = None,
    bypass_cap_cooldown: bool = False,
) -> tuple[str, Optional[int], str, int]:
    """Gentle power-target nudge decision for Antminer S19 running Braiins OS+.

    Works WITH Braiins' own per-chip tuner by nudging power_target only when
    needed and rate-limiting changes by cfg.bos_cooldown_sec.
    Returns: (action, new_watts, reason, new_cool_polls)
    """
    if not reading.ok or cfg.type != "antminer" or not cfg.bos_power_mgmt:
        return "disabled", None, "Braiins power-target management is OFF", 0
    if (reading.firmware or "").lower() != "braiins os":
        return "unsupported", None, f"firmware is {reading.firmware or 'unknown'} (requires Braiins OS)", 0

    cur_w = current_target_w if current_target_w is not None else (
        int(round(reading.power_limit_w)) if reading.power_limit_w else None
    )
    if cur_w is None or cur_w <= 0:
        return "hold", None, "waiting for current Braiins power limit telemetry", 0

    hottest = max(reading.chip_temps) if reading.chip_temps else reading.asic_temp
    if hottest is None or hottest <= 0:
        return "hold", None, "waiting for S19 chip temperature telemetry", 0

    min_w = int(cfg.bos_min_watts)
    max_w = int(cfg.bos_max_watts)
    step_w = int(cfg.bos_step_watts)
    cooldown = max(30.0, float(cfg.bos_cooldown_sec))
    elapsed = now_ts - last_change_ts if last_change_ts > 0 else cooldown + 1.0

    # Hot chip: nudge power target down to help Braiins shed heat
    if hottest >= cfg.bos_chip_hot_c:
        if cur_w <= min_w:
            return "hold", None, f"chip {hottest:.1f}C >= hot {cfg.bos_chip_hot_c:.1f}C, already at min {min_w} W", 0
        if elapsed < cooldown:
            rem = cooldown - elapsed
            return "hold", None, f"chip {hottest:.1f}C >= hot {cfg.bos_chip_hot_c:.1f}C; rate-limit {rem:.0f}s left", 0
        new_w = max(min_w, cur_w - step_w)
        return "power_down", new_w, f"chip {hottest:.1f}C >= hot {cfg.bos_chip_hot_c:.1f}C; nudging power target {cur_w} -> {new_w} W", 0

    # Warm band (between target and hot): hold steady so Braiins tuner can stabilize
    if hottest > cfg.bos_chip_target_c:
        if cur_w > max_w and (elapsed >= cooldown or bypass_cap_cooldown):
            new_w = max(min_w, max_w)
            return "power_down", new_w, f"clamping power target {cur_w} -> {new_w} W cap", 0
        return "hold", None, f"chip {hottest:.1f}C above target {cfg.bos_chip_target_c:.1f}C; holding {cur_w} W", 0

    # Enforce user max cap if current target exceeds it
    if cur_w > max_w:
        if elapsed >= cooldown or bypass_cap_cooldown:
            return "power_down", max_w, f"power target {cur_w} W exceeds max {max_w} W; lowering to {max_w} W", 0
        return "hold", None, f"power target {cur_w} W > max {max_w} W; rate-limit {cooldown - elapsed:.0f}s left", 0

    # Comfortably cool with headroom: slowly nudge power target up after cool streak + cooldown
    headroom_c = cfg.bos_chip_target_c - 4.0
    if hottest <= headroom_c and cur_w < max_w:
        # Do not nudge up while Braiins reports tuner ERROR
        tuner_err = any("error" in str(s).lower() for s in (reading.tuner_chain_statuses or []))
        if tuner_err:
            return "hold", None, f"Braiins chain tuner status reports error; holding {cur_w} W", 0
        req_polls = max(1, int(cfg.bos_cool_polls_required))
        streak = min(cool_polls + 1, req_polls)
        if streak >= req_polls:
            if elapsed < cooldown:
                return "hold", None, f"cool headroom ({hottest:.1f}C <= {headroom_c:.1f}C); rate-limit {cooldown - elapsed:.0f}s left", streak
            new_w = min(max_w, cur_w + step_w)
            return "power_up", new_w, f"cool headroom ({hottest:.1f}C <= {headroom_c:.1f}C for {streak} polls); nudging power target {cur_w} -> {new_w} W", 0
        return "hold", None, f"cool headroom ({hottest:.1f}C); waiting {streak}/{cfg.bos_cool_polls_required} polls before power nudge", streak

    return "hold", None, f"holding {cur_w} W (chip {hottest:.1f}C, target <= {cfg.bos_chip_target_c:.1f}C)", 0


def _hot_reason(asic: float, vr: float, cfg: DeviceConfig, label: str, old: float, new: float) -> str:
    parts = []
    if label == "emergency_down":
        if asic >= cfg.asic_max_c:
            parts.append(f"ASIC {asic:.1f}C >= max {cfg.asic_max_c:g}C")
        if vr >= cfg.vr_max_c:
            parts.append(f"VR {vr:.1f}C >= max {cfg.vr_max_c:g}C")
        prefix = "emergency step-down"
    else:
        if asic > cfg.asic_target_c:
            parts.append(f"ASIC {asic:.1f}C > target {cfg.asic_target_c:g}C")
        if vr > cfg.vr_target_c:
            parts.append(f"VR {vr:.1f}C > target {cfg.vr_target_c:g}C")
        prefix = "step-down"
    return f"{prefix}: {', '.join(parts)}; {old:.0f} -> {new:.0f} MHz"


# ---------------------------------------------------------------------------
# Device I/O
# ---------------------------------------------------------------------------

def _base_url(cfg: DeviceConfig) -> str:
    if cfg.port and cfg.port != 80:
        return f"http://{cfg.ip}:{cfg.port}"
    return f"http://{cfg.ip}"


def _auth_headers(cfg: DeviceConfig) -> dict[str, str]:
    headers: dict[str, str] = {}
    if cfg.otp_session:
        headers["X-OTP-Session"] = cfg.otp_session
    elif cfg.totp_secret:
        headers["X-TOTP"] = generate_totp(cfg.totp_secret)
    return headers


def _parse_dash_temps(value: Any) -> list[float]:
    if not value or not isinstance(value, str):
        return []
    out: list[float] = []
    for part in value.split("-"):
        part = part.strip()
        if not part:
            continue
        try:
            v = float(part)
        except ValueError:
            continue
        if v > 0:
            out.append(v)
    return out


def query_cgminer(ip: str, port: int, command: str, timeout: float = 4.0) -> dict[str, Any]:
    """Send a JSON command to CGMiner/BMMiner/BOSminer TCP API (port 4028)."""
    payload = (json.dumps({"command": command}) + "\n").encode("utf-8")
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(payload)
            chunks: list[bytes] = []
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
                if b"\x00" in data:
                    break
                if b"}" in data or chunks:
                    cand = b"".join(chunks).replace(b"\x00", b"").decode("utf-8", errors="replace").lstrip()
                    if cand.startswith("{"):
                        try:
                            obj, end_idx = json.JSONDecoder().raw_decode(cand)
                            rest = cand[end_idx:].strip()
                            if isinstance(obj, dict) and (not rest or cand.rstrip().endswith("}")):
                                break
                        except json.JSONDecodeError:
                            pass
    except Exception as exc:
        raise DeviceOffline(f"CGMiner {ip}:{port} {command} failed: {exc}") from exc

    raw = b"".join(chunks).replace(b"\x00", b"").decode("utf-8", errors="replace").strip()
    if not raw:
        raise DeviceOffline(f"CGMiner {ip}:{port} {command} returned empty response")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeviceOffline(f"CGMiner {ip}:{port} {command} invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise DeviceOffline(f"CGMiner {ip}:{port} {command} unexpected payload")
    return parsed


def detect_antminer_firmware(
    version_data: Optional[dict[str, Any]],
    stats_data: Optional[dict[str, Any]],
    summary_data: Optional[dict[str, Any]] = None,
    tunerstatus_data: Optional[dict[str, Any]] = None,
) -> tuple[str, str]:
    """Return (firmware_label, note). All Antminer variants are monitor-only."""
    blobs: list[str] = []
    for src in (version_data or {}, stats_data or {}, summary_data or {}, tunerstatus_data or {}):
        for key in ("STATUS", "VERSION", "STATS", "TUNERSTATUS"):
            items = src.get(key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        for k, v in item.items():
                            blobs.append(f"{k}:{v}".lower())
    hay = " ".join(blobs)
    if "boser" in hay or "bosminer" in hay or "braiins" in hay:
        return (
            "Braiins OS",
            "Braiins OS+ detected (possibly tunable via BOS API, monitor-only here)",
        )
    if "vnish" in hay:
        return (
            "VNish",
            "VNish detected (possibly tunable via VNish API, monitor-only here)",
        )
    if "luxminer" in hay or "luxos" in hay:
        return (
            "LuxOS",
            "LuxOS detected (possibly tunable via LuxOS API, monitor-only here)",
        )
    if any(t in hay for t in ("nano3", "nano 3", "avalon", "mm id0", "workmode[", "ghsmm[", "litestats")):
        return (
            "Canaan Avalon",
            "Monitor-only by default (opt-in Avalon ascset workmode/sleep/reboot)",
        )
    if "futurebit" in hay or "apollo" in hay:
        return (
            "FutureBit Apollo",
            "Monitor-only: FutureBit Apollo CGMiner telemetry",
        )
    if "bmminer" in hay or "antminer" in hay or "bitmain" in hay:
        return (
            "Stock Bitmain",
            "Monitor-only: stock Bitmain firmware has no safe frequency API",
        )
    return (
        "CGMiner",
        "Monitor-only: Antminer automatic tuning is disabled",
    )


def _cgminer_status_hdr(data: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    st = data.get("STATUS")
    if isinstance(st, list) and st and isinstance(st[0], dict):
        return st[0]
    return {}


def _cgminer_last_share_seconds(raw_time: Any, when_ts: Optional[float]) -> Optional[float]:
    if raw_time is None or isinstance(raw_time, bool):
        return None
    try:
        tval = float(raw_time)
    except (TypeError, ValueError):
        return None
    if tval <= 0:
        return None
    if tval > 1_000_000_000:
        ref = float(when_ts) if (when_ts is not None and when_ts > 1_000_000_000) else time.time()
        return max(0.0, ref - tval)
    return max(0.0, tval)


def parse_antminer_readings(
    version_data: Optional[dict[str, Any]],
    summary_data: Optional[dict[str, Any]],
    stats_data: Optional[dict[str, Any]],
    pools_data: Optional[dict[str, Any]] = None,
    devs_data: Optional[dict[str, Any]] = None,
    temps_data: Optional[dict[str, Any]] = None,
    fans_data: Optional[dict[str, Any]] = None,
    tunerstatus_data: Optional[dict[str, Any]] = None,
) -> Reading:
    ver_item: dict[str, Any] = {}
    if isinstance(version_data, dict):
        vlist = version_data.get("VERSION")
        if isinstance(vlist, list) and vlist and isinstance(vlist[0], dict):
            ver_item = vlist[0]

    sum_item: dict[str, Any] = {}
    if isinstance(summary_data, dict):
        slist = summary_data.get("SUMMARY")
        if isinstance(slist, list) and slist and isinstance(slist[0], dict):
            sum_item = slist[0]

    when_ts: Optional[float] = None
    for src in (pools_data, devs_data, summary_data, temps_data, fans_data):
        hdr = _cgminer_status_hdr(src)
        w = _num(hdr, "When")
        if w is not None and w > 0:
            when_ts = w
            break

    stats_hdr: dict[str, Any] = {}
    stats_item: dict[str, Any] = {}
    if isinstance(stats_data, dict):
        stlist = stats_data.get("STATS")
        if isinstance(stlist, list):
            for entry in stlist:
                if not isinstance(entry, dict):
                    continue
                if "Type" in entry or "BMMiner" in entry or "BOSminer" in entry:
                    if not stats_hdr:
                        stats_hdr = entry
                if (
                    "GHS 5s" in entry
                    or "GHS av" in entry
                    or "fan1" in entry
                    or "temp1" in entry
                    or "STATS" in entry
                ):
                    stats_item = entry

    # Pools (keep all pools sorted by Priority, active highlighted)
    parsed_pools: list[dict[str, Any]] = []
    if isinstance(pools_data, dict) and isinstance(pools_data.get("POOLS"), list):
        for idx, p_entry in enumerate(pools_data["POOLS"]):
            if not isinstance(p_entry, dict):
                continue
            ls_diff = _num(p_entry, "Last Share Difficulty")
            ls_sec = _cgminer_last_share_seconds(p_entry.get("Last Share Time"), when_ts)
            bs_val, _ = parse_diff_value(p_entry.get("Best Share"))
            p_status = str(p_entry.get("Status") or "")
            p_disabled = bool((p_entry.get("Enabled") is False) or (p_status.strip().lower() == "disabled"))
            parsed_pools.append(
                {
                    "pool": _opt_int(p_entry, "POOL") if _opt_int(p_entry, "POOL") is not None else idx,
                    "url": str(p_entry.get("URL") or ""),
                    "user": str(p_entry.get("User") or ""),
                    "status": p_status,
                    "disabled": p_disabled,
                    "stratum_active": False if p_disabled else bool(p_entry.get("Stratum Active", False)),
                    "priority": _opt_int(p_entry, "Priority") if _opt_int(p_entry, "Priority") is not None else idx,
                    "last_share_diff": ls_diff,
                    "last_share_seconds": ls_sec,
                    "best_share": bs_val,
                }
            )
    parsed_pools.sort(
        key=lambda x: (
            1 if x["disabled"] else 0,
            0 if x["stratum_active"] else 1,
            0 if str(x["status"]).lower() == "alive" else 1,
            x["priority"],
            x["pool"],
        )
    )

    pool_url = ""
    pool_user = ""
    fallback_pool_url = ""
    fallback_pool_user = ""
    enabled_parsed = [p for p in parsed_pools if not p.get("disabled")] or parsed_pools
    if enabled_parsed:
        pool_url = enabled_parsed[0]["url"]
        pool_user = enabled_parsed[0]["user"]
        if len(enabled_parsed) > 1:
            fallback_pool_url = enabled_parsed[1]["url"]
            fallback_pool_user = enabled_parsed[1]["user"]

    status_desc = str(
        _cgminer_status_hdr(summary_data).get("Description")
        or _cgminer_status_hdr(version_data).get("Description")
        or ""
    ).strip()
    model = str(ver_item.get("Type") or stats_hdr.get("Type") or "Antminer")
    version = str(
        ver_item.get("BOSminer")
        or ver_item.get("BMMiner")
        or ver_item.get("Miner")
        or ver_item.get("CGMiner")
        or stats_hdr.get("BMMiner")
        or status_desc
        or ""
    )
    firmware, firmware_note = detect_antminer_firmware(
        version_data, stats_data, summary_data=summary_data, tunerstatus_data=tunerstatus_data
    )

    def _mhs_or_ghs(src: dict[str, Any], ghs_key: str, *mhs_keys: str) -> Optional[float]:
        g = _num(src, ghs_key)
        if g is not None:
            return g
        m = _num(src, *mhs_keys)
        return (m / 1000.0) if m is not None else None

    hashrate = _mhs_or_ghs(sum_item, "GHS 5s", "MHS 5s")
    if hashrate is None:
        hashrate = _mhs_or_ghs(sum_item, "GHS av", "MHS av")
    if hashrate is None:
        hashrate = _num(sum_item, "GHS 30m")
    if hashrate is None:
        hashrate = _num(stats_item, "GHS 5s", "GHS av", "rate_30m", "total rate")

    hashrate_1m = _mhs_or_ghs(sum_item, "GHS 1m", "MHS 1m")
    hashrate_5m = _mhs_or_ghs(sum_item, "GHS 5m", "MHS 5m")
    hashrate_15m = _mhs_or_ghs(sum_item, "GHS 15m", "MHS 15m")
    hashrate_24h = _mhs_or_ghs(sum_item, "GHS 24h", "MHS 24h", "MHS 24H")
    hashrate_avg = _mhs_or_ghs(sum_item, "GHS av", "MHS av")

    uptime = _num(sum_item, "Elapsed")
    if uptime is None:
        uptime = _num(stats_item, "Elapsed")

    shares_accepted = _opt_int(sum_item, "Accepted")
    shares_rejected = _opt_int(sum_item, "Rejected")
    shares_stale = _opt_int(sum_item, "Stale")
    hw_errors = _opt_int(sum_item, "Hardware Errors")
    pool_rejected_pct = _num(sum_item, "Pool Rejected%")
    best_diff, best_diff_disp = parse_diff_value(sum_item.get("Best Share"))
    ambient_c, ambient_src = _antminer_intake(stats_item)

    freq = _num(stats_item, "frequency", "total_freqavg")
    if freq is None:
        chain_freqs = [
            v
            for i in range(1, 17)
            for v in [_num(stats_item, f"freq_avg{i}")]
            if v is not None and v > 0
        ]
        if chain_freqs:
            freq = sum(chain_freqs) / len(chain_freqs)

    # Braiins TEMPS[]
    temp_by_id: dict[int, tuple[Optional[float], Optional[float]]] = {}
    temps_board_list: list[float] = []
    temps_chip_list: list[float] = []
    if isinstance(temps_data, dict) and isinstance(temps_data.get("TEMPS"), list):
        for idx, t_entry in enumerate(temps_data["TEMPS"]):
            if not isinstance(t_entry, dict):
                continue
            tid = _opt_int(t_entry, "ID")
            if tid is None:
                tid = idx
            b_t = _num(t_entry, "Board")
            c_t = _num(t_entry, "Chip")
            b_val = b_t if (b_t is not None and b_t > 0) else None
            c_val = c_t if (c_t is not None and c_t > 0) else None
            temp_by_id[tid] = (b_val, c_val)
            if b_val is not None:
                temps_board_list.append(b_val)
            if c_val is not None:
                temps_chip_list.append(c_val)

    # Braiins TUNERSTATUS[]
    tuner_item: dict[str, Any] = {}
    if isinstance(tunerstatus_data, dict) and isinstance(tunerstatus_data.get("TUNERSTATUS"), list):
        tlist = tunerstatus_data["TUNERSTATUS"]
        if tlist and isinstance(tlist[0], dict):
            tuner_item = tlist[0]
    tuner_mode = str(tuner_item.get("TunerMode") or "")
    power_limit_w = _num(tuner_item, "PowerLimit")
    tuner_chains_raw = tuner_item.get("TunerChainStatus")
    tuner_chain_statuses: list[dict[str, Any]] = []
    chain_status_by_idx: dict[int, str] = {}
    if isinstance(tuner_chains_raw, list):
        for idx, ch in enumerate(tuner_chains_raw):
            if not isinstance(ch, dict):
                continue
            cid = _opt_int(ch, "Chain", "ID")
            if cid is None:
                cid = idx
            st_str = str(ch.get("Status") or "")
            tuner_chain_statuses.append(
                {
                    "id": cid,
                    "status": st_str,
                    "power_limit_w": _num(ch, "PowerLimitWatt", "PowerLimit"),
                }
            )
            chain_status_by_idx[cid] = st_str

    # Braiins DEVS[] (ignore Temperature=0; merge TEMPS[] + TunerChainStatus)
    boards: list[dict[str, Any]] = []
    if isinstance(devs_data, dict) and isinstance(devs_data.get("DEVS"), list):
        for idx, d_entry in enumerate(devs_data["DEVS"]):
            if not isinstance(d_entry, dict):
                continue
            did = _opt_int(d_entry, "ID", "ASC")
            if did is None:
                did = idx
            b_t, c_t = temp_by_id.get(did, (None, None))
            if b_t is None and c_t is None and idx in temp_by_id:
                b_t, c_t = temp_by_id[idx]
            ls_diff = _num(d_entry, "Last Share Difficulty")
            ls_sec = _cgminer_last_share_seconds(d_entry.get("Last Share Time"), when_ts)
            boards.append(
                {
                    "id": did,
                    "hashrate_5s_ghs": _mhs_or_ghs(d_entry, "GHS 5s", "MHS 5s"),
                    "hashrate_15m_ghs": _mhs_or_ghs(d_entry, "GHS 15m", "MHS 15m"),
                    "hashrate_av_ghs": _mhs_or_ghs(d_entry, "GHS av", "MHS av"),
                    "nominal_ghs": _mhs_or_ghs(d_entry, "Nominal GHS", "Nominal MHS"),
                    "hw_errors": _opt_int(d_entry, "Hardware Errors"),
                    "status": str(d_entry.get("Status") or ""),
                    "board_temp_c": b_t,
                    "chip_temp_c": c_t,
                    "last_share_diff": ls_diff,
                    "last_share_seconds": ls_sec,
                    "tuner_status": chain_status_by_idx.get(did, chain_status_by_idx.get(idx, "")),
                }
            )

    # Temps fallback to STATS when TEMPS[] absent
    board_temps: list[float] = list(temps_board_list)
    chip_temps: list[float] = list(temps_chip_list)
    if not board_temps or not chip_temps:
        for i in range(1, 17):
            t_pcb = _parse_dash_temps(stats_item.get(f"temp_pcb{i}"))
            t_chip = _parse_dash_temps(stats_item.get(f"temp_chip{i}"))
            if not temps_board_list:
                if t_pcb:
                    board_temps.append(max(t_pcb))
                else:
                    t_local = _num(stats_item, f"temp{i}")
                    if t_local is not None and t_local > 0:
                        board_temps.append(t_local)
            if not temps_chip_list:
                if t_chip:
                    chip_temps.append(max(t_chip))
                else:
                    t_remote = _num(stats_item, f"temp2_{i}")
                    if t_remote is not None and t_remote > 0:
                        chip_temps.append(t_remote)

    asic_temp: Optional[float] = None
    if chip_temps:
        asic_temp = max(chip_temps)
    elif board_temps:
        asic_temp = max(board_temps)
    else:
        asic_temp = _num(stats_item, "temp_max")

    # Fans: prefer Braiins FANS[], fallback to STATS fan1..fan8
    fans_detail: list[dict[str, Any]] = []
    fan_rpms: list[int] = []
    if isinstance(fans_data, dict) and isinstance(fans_data.get("FANS"), list):
        for idx, f_entry in enumerate(fans_data["FANS"]):
            if not isinstance(f_entry, dict):
                continue
            rpm = _opt_int(f_entry, "RPM")
            spd = _num(f_entry, "Speed", "Speed%")
            fid = _opt_int(f_entry, "ID")
            if fid is None:
                fid = idx
            if rpm is not None and rpm > 0:
                fan_rpms.append(rpm)
            fans_detail.append({"id": fid, "rpm": rpm, "speed_pct": spd})
    if not fan_rpms and not fans_detail:
        for i in range(1, 9):
            rpm = _num(stats_item, f"fan{i}")
            if rpm is not None and rpm > 0:
                r_int = int(round(rpm))
                fan_rpms.append(r_int)
                fans_detail.append({"id": i - 1, "rpm": r_int, "speed_pct": None})

    power = _num(tuner_item, "ApproximateMinerPowerConsumption")
    if power is None:
        power = _num(stats_item, "power", "Chain Power", "watt")

    fault_raw = antminer_fault_fields(stats_item, fans_data)
    fault_raw["is_braiins"] = ("braiins" in firmware.lower())
    if hashrate is not None:
        fault_raw["cg_hashrate_ghs"] = hashrate
    ideal_ghs = _mhs_or_ghs(stats_item, "total_rateideal", "Total Nominal MHS")
    if ideal_ghs is None:
        ideal_ghs = _num(stats_item, "rate_ideal", "Nominal GHS")
    if ideal_ghs is None and boards:
        noms = [b["nominal_ghs"] for b in boards if b.get("nominal_ghs")]
        if noms:
            ideal_ghs = sum(noms)
    if ideal_ghs is not None:
        fault_raw["cg_rate_ideal_ghs"] = ideal_ghs
    if uptime is not None:
        fault_raw["cg_elapsed"] = uptime
    hw_pct = _num(sum_item, "Device Hardware%")
    if hw_pct is not None:
        fault_raw["cg_hw_pct"] = hw_pct
    if boards:
        fault_raw["cg_devs"] = [{"id": b["id"], "status": b["status"]} for b in boards]
    if parsed_pools:
        fault_raw["cg_pools_all_dead"] = all(str(p.get("status") or "").lower() in {"dead", "disabled"} for p in parsed_pools)
    if tuner_chain_statuses:
        fault_raw["tuner_chains"] = list(tuner_chain_statuses)
    # Raw per-chain temps (including 0 / negative / >=125 sensor failures)
    raw_c_temps: list[float] = []
    raw_b_temps: list[float] = []
    if isinstance(temps_data, dict) and isinstance(temps_data.get("TEMPS"), list):
        for t_entry in temps_data["TEMPS"]:
            if not isinstance(t_entry, dict):
                continue
            ct = _num(t_entry, "Chip")
            bt = _num(t_entry, "Board")
            if ct is not None:
                raw_c_temps.append(ct)
            if bt is not None:
                raw_b_temps.append(bt)
    if not raw_c_temps:
        for i in range(1, 17):
            s_c = stats_item.get(f"temp_chip{i}")
            s_b = stats_item.get(f"temp_pcb{i}")
            if s_c is not None and str(s_c).strip():
                vals = []
                for part in str(s_c).split("-"):
                    try:
                        vals.append(float(part.strip()))
                    except ValueError:
                        pass
                if vals:
                    raw_c_temps.append(min(vals) if any(x <= 0 for x in vals) else max(vals))
            if s_b is not None and str(s_b).strip():
                vals = []
                for part in str(s_b).split("-"):
                    try:
                        vals.append(float(part.strip()))
                    except ValueError:
                        pass
                if vals:
                    raw_b_temps.append(min(vals) if any(x <= 0 for x in vals) else max(vals))
    if raw_c_temps:
        fault_raw["cg_chip_temps"] = raw_c_temps
    if raw_b_temps:
        fault_raw["cg_pcb_temps"] = raw_b_temps

    # Last Share (from active pool first, then most recent pool/board)
    last_share_diff: Optional[float] = None
    last_share_sec: Optional[float] = None
    for cand in parsed_pools:
        if cand["stratum_active"] and (cand["last_share_diff"] or cand["last_share_seconds"] is not None):
            last_share_diff = cand["last_share_diff"]
            last_share_sec = cand["last_share_seconds"]
            break
    if last_share_diff is None and last_share_sec is None:
        best_cand = None
        for cand in [*parsed_pools, *boards]:
            if cand.get("last_share_seconds") is not None or cand.get("last_share_diff"):
                if best_cand is None:
                    best_cand = cand
                elif cand.get("last_share_seconds") is not None and (
                    best_cand.get("last_share_seconds") is None
                    or cand["last_share_seconds"] < best_cand["last_share_seconds"]
                ):
                    best_cand = cand
        if best_cand is not None:
            last_share_diff = best_cand.get("last_share_diff")
            last_share_sec = best_cand.get("last_share_seconds")

    return Reading(
        ok=True,
        asic_temp=asic_temp,
        vr_temp=None,
        asic_temps=list(chip_temps),
        frequency=freq,
        actual_frequency=freq,
        hashrate=hashrate,
        hashrate_1m=hashrate_1m,
        hashrate_5m=hashrate_5m,
        hashrate_15m=hashrate_15m,
        hashrate_24h=hashrate_24h,
        hashrate_avg=hashrate_avg,
        power=power,
        power_limit_w=power_limit_w,
        core_voltage=None,
        core_voltage_actual=None,
        version=version,
        model=model,
        source="CGMiner TCP 4028",
        board_temps=board_temps,
        chip_temps=chip_temps,
        fan_rpms=fan_rpms,
        fans=fans_detail,
        uptime_sec=uptime,
        pool_url=pool_url,
        pool_user=pool_user,
        fallback_pool_url=fallback_pool_url,
        fallback_pool_user=fallback_pool_user,
        firmware=firmware,
        firmware_note=firmware_note,
        shares_accepted=shares_accepted,
        shares_rejected=shares_rejected,
        shares_stale=shares_stale,
        hw_errors=hw_errors,
        pool_rejected_pct=pool_rejected_pct,
        best_diff=best_diff,
        best_diff_display=best_diff_disp,
        last_submitted_diff=last_share_diff,
        last_share_is_target=True,
        found_blocks=_found_blocks_from(sum_item),
        last_share_seconds=last_share_sec,
        tuner_mode=tuner_mode,
        tuner_chain_statuses=tuner_chain_statuses,
        boards=boards,
        pools=parsed_pools,
        ambient_temp=ambient_c,
        ambient_source=ambient_src,
        fault_raw=fault_raw,
    )


def antminer_fault_fields(stats_item: dict[str, Any], fans_data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Stock-Bitmain / AML / Braiins chain_acnN / chain_acsN chip status and per-fan RPM."""
    out: dict[str, Any] = {}
    if not isinstance(stats_item, dict):
        stats_item = {}
    n_chains = _opt_int(stats_item, "miner_count", "chain_num")
    chains = []
    # Support newer Bitmain AML array style `chain: [{index, chain_acn, chain_acs, ...}]`
    if isinstance(stats_item.get("chain"), list):
        for idx, ch in enumerate(stats_item["chain"], start=1):
            if not isinstance(ch, dict):
                continue
            ci = _opt_int(ch, "index", "id") or idx
            acn = _opt_int(ch, "chain_acn", "acn")
            acs = str(ch.get("chain_acs") or ch.get("acs") or "").strip()
            chains.append({"idx": ci, "acn": acn, "acs": acs, "present": True})
    else:
        raw_slots: list[tuple[int, Optional[int], str]] = []
        for i in range(1, 17):
            acn = _opt_int(stats_item, f"chain_acn{i}")
            acs = stats_item.get(f"chain_acs{i}")
            if acn is None and acs is None:
                continue
            raw_slots.append((i, acn, str(acs or "").strip()))
        max_active = max((i for i, acn, acs in raw_slots if (acn or 0) > 0 or bool(acs)), default=0)
        for i, acn, acs in raw_slots:
            has_data = any(
                stats_item.get(k) not in (None, "", 0, "0", "0-0")
                for k in (f"freq_avg{i}", f"temp_chip{i}", f"temp_pcb{i}", f"chain_rate{i}")
            )
            if n_chains is not None:
                # Present if within miner_count, or a hole below max_active, or has board sensor data
                present = (i <= n_chains) or (i < max_active) or has_data
            else:
                present = True if ((acn or 0) > 0 or bool(acs) or i < max_active) else has_data
            chains.append({"idx": i, "acn": acn, "acs": acs, "present": present})
    if chains:
        out["chains"] = chains
        if n_chains is not None:
            out["miner_count"] = n_chains
    has_braiins_fans = isinstance(fans_data, dict) and isinstance(fans_data.get("FANS"), list)
    if has_braiins_fans:
        bfans = []
        for f in fans_data["FANS"]:
            if isinstance(f, dict):
                r = _opt_int(f, "RPM")
                bfans.append(0 if r is None else r)
        if bfans:
            out["braiins_fans"] = bfans
    else:
        fan_num = _opt_int(stats_item, "fan_num")
        if isinstance(stats_item.get("fan"), list):
            fans = [int(round(float(v))) for v in stats_item["fan"] if isinstance(v, (int, float))]
        else:
            limit = fan_num if fan_num and 0 < fan_num <= 8 else 4
            fans = []
            for i in range(1, limit + 1):
                v = _num(stats_item, f"fan{i}")
                if v is None:
                    if fan_num and i <= fan_num:
                        fans.append(0)
                        continue
                    break
                fans.append(int(round(v)))
        if fans:
            out["fans"] = fans
            if fan_num:
                out["fan_num"] = fan_num
    return out


def _enrich_reading_units_and_caps(rd: Reading, cfg: Optional[DeviceConfig] = None) -> Reading:
    import nk_adapters
    fw_l = (rd.firmware or "").lower()
    fam = (getattr(cfg, "family", "") or rd.family or "").strip().lower()
    if not fam:
        if "braiins" in fw_l:
            fam = "braiins"
        elif "vnish" in fw_l:
            fam = "vnish"
        elif "luxos" in fw_l:
            fam = "luxos"
        elif "avalon" in fw_l:
            fam = "avalon"
        elif "apollo" in fw_l or "futurebit" in fw_l:
            fam = "apollo"
        elif "btminer" in fw_l or "whatsminer" in fw_l:
            fam = "whatsminer"
        elif "bdminer" in fw_l or "sealminer" in fw_l:
            fam = "sealminer"
        elif "goldshell" in fw_l:
            fam = "goldshell"
        elif "elphapex" in fw_l:
            fam = "elphapex"
        elif "iceriver" in fw_l:
            fam = "iceriver"
        elif "bitmain" in fw_l or "cgminer" in fw_l:
            fam = "bitmain-stock"
        else:
            fam = "axeos"
    rd.family = fam
    adapter = nk_adapters.get_adapter(fam)
    if adapter is not None and not rd.capabilities:
        rd.capabilities = adapter.capabilities.to_dict()
    algo_cfg = (getattr(cfg, "algorithm", "") or "").strip().lower()
    if algo_cfg:
        rd.algorithm = algo_cfg
    elif fam == "bitmain-stock" or rd.model:
        inferred_algo, def_unit = nk_adapters.infer_bitmain_algorithm(rd.model)
        rd.algorithm = inferred_algo
    else:
        rd.algorithm = rd.algorithm or "sha256d"
    rd.hashrate_unit_kind = nk_adapters.unit_kind_for_algorithm(rd.algorithm)
    if rd.hashrate_hs is None and rd.hashrate is not None:
        if rd.algorithm == "equihash":
            rd.hashrate_hs = float(rd.hashrate) * 1e3
        else:
            rd.hashrate_hs = float(rd.hashrate) * 1e9
    return rd


def fetch_antminer_reading(cfg: DeviceConfig) -> Reading:
    import nk_adapters
    timeout = min(cfg.timeout_sec, 4.0)
    port = cfg.cgminer_port or 4028
    http_port = int(cfg.port or 80)
    fam = (cfg.family or "").strip().lower()

    # VNish HTTP /api/v1/* direct path (when configured as vnish or if :4028 fails)
    if fam == "vnish":
        base = f"http://{cfg.ip}" if http_port == 80 else f"http://{cfg.ip}:{http_port}"
        st_i, inf, _, _ = nk_adapters.http_json("GET", f"{base}/api/v1/info", timeout=timeout)
        st_s, summ_v, _, _ = nk_adapters.http_json("GET", f"{base}/api/v1/summary", timeout=timeout)
        if st_i == 200 and isinstance(inf, dict) and st_s == 200 and isinstance(summ_v, dict):
            parsed = nk_adapters.VnishAdapter.parse_http_payloads(inf, summ_v)
            hs = parsed.get("hashrate_hs")
            rd = Reading(
                ok=True,
                asic_temp=parsed.get("asic_temp_c"),
                asic_temps=list(parsed.get("chip_temps") or []),
                board_temps=list(parsed.get("board_temps") or []),
                chip_temps=list(parsed.get("chip_temps") or []),
                hashrate=nk_adapters.hs_to_ghs(hs),
                hashrate_hs=hs,
                hashrate_unit_kind="hash",
                algorithm=parsed.get("algorithm") or "sha256d",
                family="vnish",
                power=parsed.get("power_w"),
                fan_rpms=list(parsed.get("fan_rpms") or []),
                uptime_sec=parsed.get("uptime_sec"),
                pool_url=parsed.get("pool_url") or "",
                pool_user=parsed.get("pool_user") or "",
                pools=list(parsed.get("pools") or []),
                model=parsed.get("model") or "VNish Antminer",
                version=parsed.get("version") or "",
                firmware="VNish",
                firmware_note="Monitor-only by default (opt-in VNish /api/v1 tuning)",
                source=f"VNish HTTP :{http_port}",
                tuner_mode=parsed.get("preset") or "",
                capabilities=nk_adapters.get_adapter("vnish").capabilities.to_dict(),
            )
            return _enrich_reading_units_and_caps(rd, cfg)

    status_errors: list[dict[str, str]] = []

    def _check_status(cmd: str, res: Optional[dict[str, Any]], optional_cmd: bool = True) -> bool:
        if not isinstance(res, dict):
            return False
        st = _cgminer_status_hdr(res)
        code = str(st.get("STATUS") or "").upper()
        msg = str(st.get("Msg") or "").strip()
        if code in {"E", "W", "F"}:
            if not (optional_cmd and "invalid command" in msg.lower()):
                status_errors.append({"cmd": cmd, "status": code, "msg": msg})
            if code == "E":
                return False
        return True

    def _opt_cmd(cmd: str) -> Optional[dict[str, Any]]:
        try:
            res = query_cgminer(cfg.ip, port, cmd, timeout=timeout)
            if not _check_status(cmd, res, optional_cmd=(cmd != "summary")):
                return None
            return res
        except DeviceOffline:
            return None

    summary_data = query_cgminer(cfg.ip, port, "summary", timeout=timeout)
    _check_status("summary", summary_data, optional_cmd=False)
    version_data = _opt_cmd("version")
    pools_data = _opt_cmd("pools")
    devs_data = _opt_cmd("devs")
    temps_data = _opt_cmd("temps")
    fans_data = _opt_cmd("fans")
    tunerstatus_data = _opt_cmd("tunerstatus")
    stats_data = _opt_cmd("stats")
    rd = parse_antminer_readings(
        version_data,
        summary_data,
        stats_data,
        pools_data=pools_data,
        devs_data=devs_data,
        temps_data=temps_data,
        fans_data=fans_data,
        tunerstatus_data=tunerstatus_data,
    )
    # v1.14: Tier-2 Whatsminer / Sealminer / Generic CGMiner detection & parsing
    from nk_adapters.whatsminer import is_whatsminer_payload, parse_whatsminer_v2
    from nk_adapters.sealminer import is_sealminer_payload, parse_sealminer_readings
    if fam == "whatsminer" or (not fam and is_whatsminer_payload(version_data, summary_data, devs_data)):
        psu_data = _opt_cmd("get_psu")
        err_data = _opt_cmd("get_error_code")
        wm = parse_whatsminer_v2(version_data, summary_data, devs_data, pools_data, psu_data, err_data)
        rd.family = "whatsminer"
        rd.firmware = wm["firmware"]
        rd.firmware_note = "Monitor-only by default (opt-in BTMiner v3 :4433 controls)"
        rd.model = wm.get("model") or rd.model
        rd.version = wm.get("version") or rd.version
        if wm.get("hashrate_hs") is not None:
            rd.hashrate_hs = wm["hashrate_hs"]
            rd.hashrate = nk_adapters.hs_to_ghs(wm["hashrate_hs"])
        if wm.get("asic_temp_c") is not None:
            rd.asic_temp = wm["asic_temp_c"]
        if wm.get("ambient_temp_c") is not None:
            rd.ambient_temp = wm["ambient_temp_c"]
            rd.ambient_source = "miner"
        if wm.get("power_w") is not None:
            rd.power = wm["power_w"]
        if wm.get("power_limit_w") is not None:
            rd.power_limit_w = wm["power_limit_w"]
        if wm.get("fan_rpms"):
            rd.fan_rpms = list(wm["fan_rpms"])
        if wm.get("tuner_mode"):
            rd.tuner_mode = wm["tuner_mode"]
        if wm.get("errors"):
            rd.fault_raw["whatsminer_errors"] = wm["errors"]
        rd.capabilities = nk_adapters.get_adapter("whatsminer").capabilities.to_dict()
        return _enrich_reading_units_and_caps(rd, cfg)

    if fam == "sealminer" or (not fam and is_sealminer_payload(version_data, summary_data, stats_data)):
        sm = parse_sealminer_readings(version_data, summary_data, devs_data, stats_data, pools_data)
        rd.family = "sealminer"
        rd.firmware = sm["firmware"]
        rd.firmware_note = "Monitor-only BDMiner TCP :4028 (non-closing socket safe)"
        rd.model = sm.get("model") or rd.model
        rd.version = sm.get("version") or rd.version
        if sm.get("hashrate_hs") is not None:
            rd.hashrate_hs = sm["hashrate_hs"]
            rd.hashrate = nk_adapters.hs_to_ghs(sm["hashrate_hs"])
        if sm.get("asic_temp_c") is not None:
            rd.asic_temp = sm["asic_temp_c"]
        if sm.get("power_w") is not None:
            rd.power = sm["power_w"]
        if sm.get("fan_rpms"):
            rd.fan_rpms = list(sm["fan_rpms"])
        rd.capabilities = nk_adapters.get_adapter("sealminer").capabilities.to_dict()
        return _enrich_reading_units_and_caps(rd, cfg)

    # v1.13: Canaan Avalon MM ID0 / litestats enrichment
    from nk_adapters.avalon import is_avalon_payload, parse_avalon_readings
    if fam == "avalon" or is_avalon_payload(version_data, stats_data, summary_data):
        lite_data = _opt_cmd("litestats") if (not stats_data or "MM ID" not in str(stats_data)) else None
        av = parse_avalon_readings(version_data, summary_data, stats_data, lite_data, devs_data, pools_data)
        rd.family = "avalon"
        rd.firmware = av["firmware"]
        rd.firmware_note = "Monitor-only by default (opt-in Avalon ascset workmode/sleep/reboot)"
        if av.get("model"):
            rd.model = av["model"]
        if av.get("version"):
            rd.version = av["version"]
        if av.get("hashrate_hs") is not None:
            rd.hashrate_hs = av["hashrate_hs"]
            rd.hashrate = nk_adapters.hs_to_ghs(av["hashrate_hs"])
        if av.get("asic_temp_c") is not None:
            rd.asic_temp = av["asic_temp_c"]
        if av.get("ambient_temp_c") is not None:
            rd.ambient_temp = av["ambient_temp_c"]
            rd.ambient_source = "miner"
        if av.get("power_w") is not None:
            rd.power = av["power_w"]
        if av.get("fan_rpms"):
            rd.fan_rpms = list(av["fan_rpms"])
        if av.get("workmode"):
            rd.tuner_mode = av["workmode"]
        if av.get("errors"):
            rd.fault_raw["avalon_errors"] = av["errors"]
        if av.get("mm_raw"):
            nk_faults.flatten_fields(av["mm_raw"], "avalon_mm", rd.raw_fields)
    if status_errors:
        rd.fault_raw["cgminer_status_errors"] = status_errors
    rd.raw_fields = antminer_raw_fields(summary_data, stats_data, devs_data, temps_data, fans_data, tunerstatus_data)
    return _enrich_reading_units_and_caps(rd, cfg)


def antminer_raw_fields(*payloads: Optional[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    names = ("summary", "stats", "devs", "temps", "fans", "tunerstatus")
    keys = {"summary": "SUMMARY", "stats": "STATS", "devs": "DEVS", "temps": "TEMPS", "fans": "FANS", "tunerstatus": "TUNERSTATUS"}
    for nm, data in zip(names, payloads):
        if not isinstance(data, dict):
            continue
        body = data.get(keys[nm])
        if body is None:
            continue
        if isinstance(body, list):
            items = [b for b in body if isinstance(b, dict) and b]
            ksets = [set(b) for b in items]
            if len(items) == 1 or (items and len(items) == len(body) - body.count({}) and sum(len(k) for k in ksets) == len(set().union(*ksets))):
                merged: dict[str, Any] = {}
                for b in items:
                    merged.update(b)
                body = merged
        nk_faults.flatten_fields(body, nm, out)
    return out
def _unparsed_reading(url: str, status: int, payload: bytes, exc: Optional[Exception] = None) -> Reading:
    snippet = body_snippet(payload)
    if exc is not None:
        LOG.warning("Failed to parse %s (HTTP %s): %s | body[:300]=%r", url, status, exc, snippet)
        err = f"online but unparsed {url} HTTP {status}: {exc}; body: {snippet}"
    else:
        LOG.warning("Unusable response from %s (HTTP %s) | body[:300]=%r", url, status, snippet)
        err = f"online but unparsed {url} HTTP {status}; body: {snippet}"
    return Reading(ok=False, reachable=True, error=err, source=f"GET {url}")


def _get_json(url: str, timeout: float) -> tuple[int, bytes, dict[str, str], Optional[dict[str, Any]], Optional[Exception]]:
    """GET an API URL; return (status, body, headers, parsed_dict_or_None, error)."""
    try:
        status, payload, headers = http_request_ex("GET", url, timeout=timeout, retries=1)
    except DeviceOffline as exc:
        return 0, b"", {}, None, exc
    if status == 200 and payload and not _looks_like_html(payload, headers):
        data = _json_body(payload)
        if isinstance(data, dict):
            return status, payload, headers, data, None
    return status, payload, headers, None, None


def _reading_from_adapter_dict(cfg: DeviceConfig, fam: str, parsed: dict[str, Any]) -> Reading:
    import nk_adapters
    hs = parsed.get("hashrate_hs")
    algo = cfg.algorithm or parsed.get("algorithm") or "sha256d"
    ukind = parsed.get("hashrate_unit_kind") or nk_adapters.unit_kind_for_algorithm(algo)
    hr_disp = (hs / 1e3) if (hs is not None and ukind == "sol") else nk_adapters.hs_to_ghs(hs)
    adp = nk_adapters.get_adapter(fam)
    rd = Reading(
        ok=True,
        asic_temp=parsed.get("asic_temp_c"),
        chip_temps=list(parsed.get("chip_temps") or []),
        board_temps=list(parsed.get("board_temps") or []),
        ambient_temp=parsed.get("ambient_temp_c"),
        ambient_source="miner" if parsed.get("ambient_temp_c") is not None else "",
        hashrate=hr_disp,
        hashrate_hs=hs,
        hashrate_unit_kind=ukind,
        algorithm=algo,
        family=fam,
        power=parsed.get("power_w"),
        power_limit_w=parsed.get("power_limit_w"),
        fan_rpms=list(parsed.get("fan_rpms") or []),
        uptime_sec=parsed.get("uptime_sec"),
        shares_accepted=parsed.get("shares_accepted"),
        shares_rejected=parsed.get("shares_rejected"),
        hw_errors=parsed.get("hw_errors"),
        tuner_mode=str(parsed.get("tuner_mode") or ""),
        pool_url=parsed.get("pool_url") or "",
        pool_user=parsed.get("pool_user") or "",
        pools=list(parsed.get("pools") or []),
        model=parsed.get("model") or fam,
        version=parsed.get("version") or "",
        firmware=parsed.get("firmware") or fam,
        firmware_note=f"Monitor-only by default ({fam})",
        source=str(parsed.get("source") or fam),
        capabilities=adp.capabilities.to_dict() if adp else {"monitor": True},
    )
    if parsed.get("errors"):
        rd.fault_raw[f"{fam}_errors"] = parsed["errors"]
    return _enrich_reading_units_and_caps(rd, cfg)


def fetch_reading(cfg: DeviceConfig) -> Reading:
    if cfg.type == "antminer":
        import nk_adapters
        fam = (cfg.family or "").strip().lower()
        http_port = int(cfg.port or 80)
        timeout = min(max(cfg.timeout_sec, 4.0), 10.0)
        if fam == "goldshell":
            try:
                gs = nk_adapters.GoldshellAdapter().fetch_telemetry(
                    cfg.ip, http_port, cfg.web_username, cfg.web_password, cfg.api_token, timeout=timeout
                )
                return _reading_from_adapter_dict(cfg, "goldshell", gs)
            except Exception as exc:
                raise DeviceOffline(f"Goldshell {cfg.ip}:{http_port} failed: {exc}") from exc
        if fam == "elphapex":
            try:
                el = nk_adapters.ElphapexAdapter().fetch_telemetry(
                    cfg.ip, http_port, cfg.web_username, cfg.web_password, timeout=timeout
                )
                return _reading_from_adapter_dict(cfg, "elphapex", el)
            except Exception as exc:
                raise DeviceOffline(f"Elphapex {cfg.ip}:{http_port} failed: {exc}") from exc
        if fam == "iceriver":
            try:
                ir = nk_adapters.IceRiverAdapter().fetch_telemetry(
                    cfg.ip, http_port, int(cfg.cgminer_port or 4028),
                    api_token=cfg.api_token, username=cfg.web_username, password=cfg.web_password, timeout=timeout,
                )
                return _reading_from_adapter_dict(cfg, "iceriver", ir)
            except Exception:
                # Fall back to CGMiner :4028 if IceRiver-OC exposes it
                pass
        if fam == "generic-prometheus":
            try:
                pm = nk_adapters.GenericPrometheusAdapter().fetch_telemetry(
                    cfg.ip, int(cfg.port or 8081), adapter_config=cfg.adapter_config, timeout=timeout
                )
                return _reading_from_adapter_dict(cfg, "generic-prometheus", pm)
            except Exception as exc:
                raise DeviceOffline(f"Prometheus {cfg.ip}:{http_port} failed: {exc}") from exc
        if fam == "generic-http-json":
            try:
                hj = nk_adapters.GenericHTTPJsonAdapter().fetch_telemetry(
                    cfg.ip, http_port, adapter_config=cfg.adapter_config, api_token=cfg.api_token, timeout=timeout
                )
                return _reading_from_adapter_dict(cfg, "generic-http-json", hj)
            except Exception as exc:
                raise DeviceOffline(f"HTTP-JSON {cfg.ip}:{http_port} failed: {exc}") from exc
        try:
            return fetch_antminer_reading(cfg)
        except DeviceOffline as cg_exc:
            import nk_adapters
            fam = (cfg.family or "").strip().lower()
            http_port = int(cfg.port or 80)
            if cfg.web_username and cfg.web_password and fam in {"", "bitmain-stock"}:
                try:
                    bm = nk_adapters.BitmainStockAdapter().fetch_cgi_telemetry(
                        cfg.ip, http_port, cfg.web_username, cfg.web_password, timeout=min(cfg.timeout_sec, 5.0)
                    )
                    hs = bm.get("hashrate_hs")
                    ukind = bm.get("hashrate_unit_kind") or "hash"
                    hr_disp = (hs / 1e3) if (hs is not None and ukind == "sol") else nk_adapters.hs_to_ghs(hs)
                    rd = Reading(
                        ok=True,
                        asic_temp=bm.get("asic_temp_c"),
                        chip_temps=list(bm.get("chip_temps") or []),
                        board_temps=list(bm.get("board_temps") or []),
                        hashrate=hr_disp,
                        hashrate_hs=hs,
                        hashrate_unit_kind=ukind,
                        algorithm=bm.get("algorithm") or "sha256d",
                        family="bitmain-stock",
                        fan_rpms=list(bm.get("fan_rpms") or []),
                        uptime_sec=bm.get("uptime_sec"),
                        pool_url=bm.get("pool_url") or "",
                        pool_user=bm.get("pool_user") or "",
                        pools=list(bm.get("pools") or []),
                        model=bm.get("model") or "Bitmain Antminer",
                        version=bm.get("version") or "",
                        firmware="Stock Bitmain",
                        firmware_note="HTTP Digest CGI telemetry (CGMiner 4028 offline)",
                        source=f"Bitmain HTTP Digest :{http_port}",
                        capabilities=nk_adapters.get_adapter("bitmain-stock").capabilities.to_dict(),
                    )
                    return rd
                except Exception:
                    pass
            if fam == "whatsminer" and nk_adapters.tcp_open(cfg.ip, 4433, 1.0):
                try:
                    wm = nk_adapters.WhatsminerAdapter().fetch_telemetry(
                        cfg.ip, int(cfg.cgminer_port or 4028), 4433, timeout=min(cfg.timeout_sec, 4.0)
                    )
                    return _reading_from_adapter_dict(cfg, "whatsminer", wm)
                except Exception:
                    pass
            if fam == "braiins" and nk_adapters.tcp_open(cfg.ip, 8081, 1.0):
                try:
                    st, body, _ = nk_adapters.http_request_raw("GET", f"http://{cfg.ip}:8081/metrics", timeout=min(cfg.timeout_sec, 4.0))
                    if st == 200 and body:
                        pm = nk_adapters.parse_prometheus_metrics(body.decode("utf-8", errors="replace"))
                        hs = pm.get("hashrate_hs")
                        lbls = pm.get("labels") or {}
                        rd = Reading(
                            ok=True,
                            asic_temp=max(pm["temps"]) if pm["temps"] else None,
                            chip_temps=list(pm["temps"]),
                            hashrate=nk_adapters.hs_to_ghs(hs),
                            hashrate_hs=hs,
                            hashrate_unit_kind="hash",
                            algorithm="sha256d",
                            family="braiins",
                            power=pm.get("power_w"),
                            fan_rpms=list(pm["fans"]),
                            model=str(lbls.get("model") or lbls.get("miner_model") or "Braiins OS+ Miner"),
                            firmware="Braiins OS",
                            firmware_note="Braiins Prometheus :8081/metrics",
                            source="Prometheus :8081/metrics",
                            capabilities=nk_adapters.get_adapter("braiins").capabilities.to_dict(),
                        )
                        return rd
                except Exception:
                    pass
            raise cg_exc
    base = _base_url(cfg)
    timeout = cfg.timeout_sec
    last_unparsed: Optional[Reading] = None

    def parse_v1(data: dict[str, Any]) -> Reading:
        if cfg.type == "gamma":
            return parse_gamma_info(data)
        return parse_nerd_v1_info(data)

    # Prefer /api/system/info: it exists on NerdQAxePlus v1.0.37.x LTS (no v2 API)
    # and on later v1.1+ / stock ESP-Miner. Unknown /api/v2/* paths on v1.0.37.1
    # 302-redirect to the HTML UI, which is slow on ESP32 and used to hide the miner.
    info_url = f"{base}/api/system/info"
    status, payload, headers, data, exc = _get_json(info_url, timeout)
    if data is not None and looks_like_axeos(data):
        try:
            rd = parse_v1(data)
            rd.hostname = str(data.get("hostname") or "").strip()
            rd.raw_fields = nk_faults.flatten_fields(data, "info")
            return _enrich_reading_units_and_caps(rd, cfg)
        except Exception as parse_exc:
            last_unparsed = _unparsed_reading(info_url, status, payload, parse_exc)
    elif status == 401:
        return Reading(
            ok=False,
            reachable=True,
            error="401 Unauthorized on /api/system/info (LAN rule or OTP)",
            source="GET /api/system/info",
        )
    elif status == 200 and payload:
        last_unparsed = _unparsed_reading(info_url, status, payload, exc)
    elif exc is not None:
        LOG.info("%s: %s; will try /api/v2/dashboard if present", cfg.name, exc)

    if cfg.type in {"nerdoctaxe", "axeos-generic"}:
        dash_url = f"{base}/api/v2/dashboard"
        status, payload, headers, data, exc = _get_json(dash_url, timeout)
        if data is not None and ("thermal" in data or "performance" in data):
            try:
                reading = parse_nerd_v2_dashboard(data)
            except Exception as parse_exc:
                last_unparsed = _unparsed_reading(dash_url, status, payload, parse_exc)
            else:
                ident = None
                sysinfo = None
                _, _, _, ident, _ = _get_json(f"{base}/api/v2/identify", min(timeout, 4.0))
                _, _, _, sysinfo, _ = _get_json(f"{base}/api/v2/system", min(timeout, 4.0))
                _enrich_reading_units_and_caps(reading, cfg)
                reading.raw_fields = nk_faults.flatten_fields(data, "dashboard")
                if isinstance(sysinfo, dict):
                    nk_faults.flatten_fields(sysinfo, "system", reading.raw_fields)
                    for k in ("lastResetReason", "resetReason"):
                        if sysinfo.get(k) and k not in reading.fault_raw:
                            reading.fault_raw[k] = sysinfo[k]
                return enrich_reading_identity(reading, ident, sysinfo)
        elif status == 401:
            return Reading(
                ok=False,
                reachable=True,
                error="401 Unauthorized on /api/v2/dashboard (LAN rule or OTP)",
                source="GET /api/v2/dashboard",
            )
        elif status == 200 and payload:
            last_unparsed = _unparsed_reading(dash_url, status, payload, exc)

    if last_unparsed is not None:
        return last_unparsed
    raise DeviceOffline(f"no usable AxeOS telemetry from {cfg.ip}")


def _patch_extras(cfg: DeviceConfig) -> dict[str, Any]:
    # Custom clocks/voltages above the AxeOS dropdown require overclockEnabled.
    if cfg.type == "gamma" or (cfg.type == "axeos-generic" and cfg.voltage_control):
        return {"overclockEnabled": 1}
    return {}


def apply_frequency(cfg: DeviceConfig, new_freq: float, dry_run: bool) -> str:
    """PATCH frequency only (never includes coreVoltage). Raises on failure."""
    if not is_tunable(cfg):
        raise DeviceError(f"refusing to write frequency on monitor-only device {cfg.name} ({cfg.type})")
    freq = int(round(new_freq))
    if freq > cfg.max_freq:
        raise DeviceError(f"refusing to set {freq} MHz above max_freq {cfg.max_freq:.0f}")
    if freq < cfg.min_freq:
        freq = int(round(cfg.min_freq))
    body = {"frequency": freq}
    body.update(_patch_extras(cfg))
    if dry_run:
        return f"dry-run: would PATCH frequency={freq}"

    base = _base_url(cfg)
    headers = _auth_headers(cfg)
    if cfg.type in {"nerdoctaxe", "axeos-generic"}:
        status, payload = http_request(
            "PATCH", f"{base}/api/v2/settings", body=body, headers=headers, timeout=cfg.timeout_sec
        )
        if status in {404, 405}:
            status, payload = http_request(
                "PATCH", f"{base}/api/system", body=body, headers=headers, timeout=cfg.timeout_sec
            )
    else:
        status, payload = http_request(
            "PATCH", f"{base}/api/system", body=body, headers=headers, timeout=cfg.timeout_sec
        )

    if status in {200, 201, 204}:
        return f"PATCH frequency={freq} HTTP {status}"
    text = payload.decode("utf-8", errors="replace")[:300]
    if status == 401:
        raise DeviceError(
            "401 Unauthorized writing frequency. On a NerdOCTAxe, enable OTP in config "
            "(totp_secret or otp_session). On a Gamma, the request must come from a LAN IP. "
            f"Body: {text}"
        )
    raise DeviceError(f"PATCH frequency failed HTTP {status}: {text}")


def apply_voltage(cfg: DeviceConfig, new_mv: int, dry_run: bool, allow_vr_relief: bool = False) -> str:
    """PATCH coreVoltage in millivolts.

    Allowed when voltage_control is True, or on vr_voltage_relief devices ONLY when
    explicitly lowering voltage for high VR temperature (allow_vr_relief=True).
    """
    if not is_tunable(cfg):
        raise DeviceError(f"refusing to change core voltage on {cfg.name} ({cfg.type})")
    if not cfg.voltage_control and not (cfg.vr_voltage_relief and allow_vr_relief):
        raise DeviceError(f"refusing to change core voltage on {cfg.name} ({cfg.type})")
    mv = int(new_mv)
    if mv < cfg.min_voltage_mv:
        mv = int(cfg.min_voltage_mv)
    if mv > min(HARD_MAX_VOLTAGE_MV, int(cfg.max_voltage_mv)):
        mv = min(HARD_MAX_VOLTAGE_MV, int(cfg.max_voltage_mv))
    body = {"coreVoltage": mv}
    body.update(_patch_extras(cfg))
    if dry_run:
        return f"dry-run: would PATCH coreVoltage={mv}"
    base = _base_url(cfg)
    headers = _auth_headers(cfg)
    if cfg.type in {"nerdoctaxe", "axeos-generic"}:
        status, payload = http_request(
            "PATCH", f"{base}/api/v2/settings", body=body, headers=headers, timeout=cfg.timeout_sec
        )
        if status in {404, 405}:
            status, payload = http_request(
                "PATCH", f"{base}/api/system", body=body, headers=headers, timeout=cfg.timeout_sec
            )
    else:
        status, payload = http_request(
            "PATCH", f"{base}/api/system", body=body, headers=headers, timeout=cfg.timeout_sec
        )
    if status in {200, 201, 204}:
        return f"PATCH coreVoltage={mv} HTTP {status}"
    err = payload.decode("utf-8", errors="replace")[:300]
    raise DeviceError(f"PATCH coreVoltage failed HTTP {status}: {err}")


def apply_manual_voltage(cfg: DeviceConfig, new_mv: int, dry_run: bool) -> tuple[str, int, str]:
    """Explicit manual coreVoltage change while autotune is OFF.

    Allowed for Gammas AND NerdOCTAxe/axeos-generic, always clamped to
    [min_voltage_mv, 1250]. Returns (result_str, clamped_mv, warning_note).
    Autotune's apply_voltage() still refuses NerdOCTAxe/axeos-generic.
    """
    if not is_tunable(cfg):
        raise DeviceError(f"refusing to change core voltage on monitor-only device {cfg.name} ({cfg.type})")
    req_mv = int(new_mv)
    floor_mv = min(int(cfg.min_voltage_mv), HARD_MAX_VOLTAGE_MV)
    cap_mv = min(HARD_MAX_VOLTAGE_MV, int(cfg.max_voltage_mv))
    mv = max(floor_mv, min(cap_mv, req_mv))
    warnings: list[str] = []
    if mv != req_mv:
        warnings.append(f"clamped requested {req_mv} mV to {mv} mV (bounds {floor_mv}-{cap_mv} mV)")
    if cfg.type in {"nerdoctaxe", "axeos-generic"}:
        warnings.append(
            "Manual core voltage applied on NerdOCTAxe (capped at 1250 mV); "
            "automatic tuning remains frequency-only."
        )
    warn = "; ".join(warnings)
    body = {"coreVoltage": mv}
    body.update(_patch_extras(cfg))
    if dry_run:
        return f"dry-run: would PATCH coreVoltage={mv}", mv, warn

    base = _base_url(cfg)
    headers = _auth_headers(cfg)
    if cfg.type in {"nerdoctaxe", "axeos-generic"}:
        status, payload = http_request(
            "PATCH", f"{base}/api/v2/settings", body=body, headers=headers, timeout=cfg.timeout_sec
        )
        if status in {404, 405}:
            status, payload = http_request(
                "PATCH", f"{base}/api/system", body=body, headers=headers, timeout=cfg.timeout_sec
            )
    else:
        status, payload = http_request(
            "PATCH", f"{base}/api/system", body=body, headers=headers, timeout=cfg.timeout_sec
        )
    if status in {200, 201, 204}:
        return f"PATCH coreVoltage={mv} HTTP {status}", mv, warn
    err = payload.decode("utf-8", errors="replace")[:300]
    if status == 401:
        raise DeviceError(
            "401 Unauthorized writing coreVoltage. Check LAN IP or OTP settings. "
            f"Body: {err}"
        )
    raise DeviceError(f"PATCH coreVoltage failed HTTP {status}: {err}")


def _cgminer_escape(val: str) -> str:
    return val.replace("\\", "\\\\").replace(",", "\\,")


def query_cgminer_raw(ip: str, port: int, command: str, parameter: str = "", timeout: float = 4.0) -> dict[str, Any]:
    req: dict[str, Any] = {"command": command}
    if parameter != "":
        req["parameter"] = parameter
    payload = (json.dumps(req) + "\n").encode("utf-8")
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(payload)
            chunks: list[bytes] = []
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
                if b"\x00" in data:
                    break
                if b"}" in data:
                    cand = b"".join(chunks).replace(b"\x00", b"").decode("utf-8", errors="replace").strip()
                    if cand.endswith("}"):
                        try:
                            if isinstance(json.loads(cand), dict):
                                break
                        except json.JSONDecodeError:
                            pass
    except Exception as exc:
        raise DeviceOffline(f"CGMiner {ip}:{port} {command} failed: {exc}") from exc
    raw = b"".join(chunks).replace(b"\x00", b"").decode("utf-8", errors="replace").strip()
    if not raw:
        raise DeviceOffline(f"CGMiner {ip}:{port} {command} returned empty response")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeviceOffline(f"CGMiner {ip}:{port} {command} invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise DeviceOffline(f"CGMiner {ip}:{port} {command} unexpected payload")
    return parsed


def apply_pool_settings(
    cfg: DeviceConfig,
    stratum_url: str,
    stratum_port: int,
    stratum_user: str,
    stratum_password: str = "",
    fallback_url: str = "",
    fallback_port: Optional[int] = None,
    fallback_user: str = "",
    fallback_password: str = "",
    restart: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Update pool configuration on AxeOS/NerdOCTAxe (with optional restart) or Antminer CGMiner."""
    url = (stratum_url or "").strip()
    user = (stratum_user or "").strip()
    if not url:
        raise DeviceError("Primary stratum URL cannot be empty")
    if not user:
        raise DeviceError("Primary stratum user/worker cannot be empty")
    port = int(stratum_port)
    if not (1 <= port <= 65535):
        raise DeviceError(f"Primary stratum port must be 1-65535, got {port}")

    fb_url = (fallback_url or "").strip()
    fb_user = (fallback_user or "").strip()
    fb_port_int: Optional[int] = None
    if fb_url:
        fb_port_int = int(fallback_port if fallback_port is not None else port)
        if not (1 <= fb_port_int <= 65535):
            raise DeviceError(f"Fallback stratum port must be 1-65535, got {fb_port_int}")

    if cfg.type == "antminer":
        cg_port = int(cfg.cgminer_port or 4028)
        target_stratum = url if "://" in url else f"stratum+tcp://{url}:{port}"
        if "://" in url and ":" not in url.split("://", 1)[1]:
            target_stratum = f"{url}:{port}"
        if dry_run:
            return {
                "result": f"dry-run: would send CGMiner addpool|{target_stratum},{user},*** and switchpool",
                "restarted": False,
                "restart_required": False,
            }
        priv = query_cgminer_raw(cfg.ip, cg_port, "privileged", timeout=cfg.timeout_sec)
        priv_status = (priv.get("STATUS") or [{}])[0]
        if str(priv_status.get("STATUS") or "").upper() != "S":
            msg = str(priv_status.get("Msg") or "Access denied")
            raise DeviceError(
                f"Antminer CGMiner :{cg_port} rejected privileged access ({msg}). "
                "Stock Bitmain firmware disables remote CGMiner pool writes by default; "
                "change pools in the Antminer web UI or enable '--api-allow W:<your-PC-IP>' "
                "(e.g. on Braiins OS / VNish)."
            )
        param = f"{_cgminer_escape(target_stratum)},{_cgminer_escape(user)},{_cgminer_escape(stratum_password or 'x')}"
        add_res = query_cgminer_raw(cfg.ip, cg_port, "addpool", parameter=param, timeout=cfg.timeout_sec)
        add_status = (add_res.get("STATUS") or [{}])[0]
        if str(add_status.get("STATUS") or "").upper() != "S":
            raise DeviceError(f"CGMiner addpool failed: {add_status.get('Msg') or add_res}")
        # Switch to the newly added pool (highest index in POOLS).
        pools_res = query_cgminer_raw(cfg.ip, cg_port, "pools", timeout=cfg.timeout_sec)
        plist = pools_res.get("POOLS") or []
        if plist and isinstance(plist[-1], dict) and "POOL" in plist[-1]:
            idx = int(plist[-1]["POOL"])
            sw_res = query_cgminer_raw(cfg.ip, cg_port, "switchpool", parameter=str(idx), timeout=cfg.timeout_sec)
            sw_status = (sw_res.get("STATUS") or [{}])[0]
            if str(sw_status.get("STATUS") or "").upper() != "S":
                raise DeviceError(f"CGMiner switchpool failed: {sw_status.get('Msg') or sw_res}")
        return {
            "result": f"CGMiner addpool + switchpool succeeded on {cfg.ip}:{cg_port}",
            "restarted": False,
            "restart_required": False,
        }

    # AxeOS / NerdOCTAxe / NerdQAxePlus:
    # ESP-Miner accepts flat stratum* / fallbackStratum* keys on PATCH /api/system
    # across Bitaxe Gamma and NerdQAxePlus v1/v2 firmware, and requires a restart
    # for the stratum task to reconnect with the new pool.
    flat_body: dict[str, Any] = {
        "stratumURL": url,
        "stratumPort": port,
        "stratumUser": user,
    }
    if stratum_password:
        flat_body["stratumPassword"] = stratum_password
    if fb_url:
        flat_body["fallbackStratumURL"] = fb_url
        flat_body["fallbackStratumPort"] = fb_port_int
        flat_body["fallbackStratumUser"] = fb_user or user
        if fallback_password:
            flat_body["fallbackStratumPassword"] = fallback_password
    elif fallback_url == "":
        # Allow explicitly clearing fallback when fallback_url is sent empty
        flat_body["fallbackStratumURL"] = ""

    if dry_run:
        return {
            "result": f"dry-run: would PATCH /api/system pool={url}:{port} user={user}"
            + (" + POST /api/system/restart" if restart else ""),
            "restarted": False,
            "restart_required": True,
        }

    base = _base_url(cfg)
    headers = _auth_headers(cfg)
    status, payload = http_request(
        "PATCH", f"{base}/api/system", body=flat_body, headers=headers, timeout=cfg.timeout_sec
    )
    used_endpoint = "/api/system"
    if status in {404, 405} and cfg.type in {"nerdoctaxe", "axeos-generic"}:
        # Fallback for v2-only settings builds
        pools_arr: list[dict[str, Any]] = [{"url": url, "port": port, "user": user}]
        if stratum_password:
            pools_arr[0]["password"] = stratum_password
        if fb_url:
            fb_obj: dict[str, Any] = {"url": fb_url, "port": fb_port_int, "user": fb_user or user}
            if fallback_password:
                fb_obj["password"] = fallback_password
            pools_arr.append(fb_obj)
        else:
            pools_arr.append({})
        status, payload = http_request(
            "PATCH",
            f"{base}/api/v2/settings",
            body={"pools": pools_arr},
            headers=headers,
            timeout=cfg.timeout_sec,
        )
        used_endpoint = "/api/v2/settings"

    if status not in {200, 201, 204}:
        err = payload.decode("utf-8", errors="replace")[:300]
        if status == 401:
            raise DeviceError(
                "401 Unauthorized updating pool. Check LAN IP or NerdOCTAxe OTP settings. "
                f"Body: {err}"
            )
        raise DeviceError(f"PATCH {used_endpoint} pool failed HTTP {status}: {err}")

    restarted = False
    rst_msg = ""
    if restart:
        rst_msg = "; " + restart_device(cfg, dry_run=False)
        restarted = True

    return {
        "result": f"PATCH {used_endpoint} pool={url}:{port} HTTP {status}{rst_msg}",
        "restarted": restarted,
        "restart_required": True,
    }


def apply_fan(cfg: DeviceConfig, fan_pct: Optional[int], dry_run: bool) -> str:
    """v1.9 quiet mode: cap the fan (manual %) or hand it back to automatic control (fan_pct=None)."""
    if not is_tunable(cfg):
        raise DeviceError(f"fan control is only supported on AxeOS miners ({cfg.name})")
    if fan_pct is None:
        body: dict[str, Any] = {"autofanspeed": 1}
        what = "fan automatic"
    else:
        pct = max(20, min(100, int(fan_pct)))
        body = {"autofanspeed": 0, "fanspeed": pct, "manualFanSpeed": pct}
        what = f"fan {pct}%"
    if dry_run:
        return f"dry-run: would PATCH {what}"
    base = _base_url(cfg)
    headers = _auth_headers(cfg)
    status, payload = 0, b""
    if cfg.type in {"nerdoctaxe", "axeos-generic"}:
        status, payload = http_request("PATCH", f"{base}/api/v2/settings", body=body, headers=headers, timeout=cfg.timeout_sec)
    if status in {0, 404, 405}:
        status, payload = http_request("PATCH", f"{base}/api/system", body=body, headers=headers, timeout=cfg.timeout_sec)
    if status in {200, 201, 204}:
        return f"PATCH {what} HTTP {status}"
    raise DeviceError(f"PATCH fan failed HTTP {status}: {payload.decode('utf-8', errors='replace')[:200]}")


def restart_device(cfg: DeviceConfig, dry_run: bool) -> str:
    if dry_run:
        return "dry-run: would POST /api/system/restart"
    base = _base_url(cfg)
    headers = _auth_headers(cfg)
    status, payload = http_request(
        "POST", f"{base}/api/system/restart", body={}, headers=headers, timeout=cfg.timeout_sec
    )
    if status in {200, 201, 202, 204}:
        return f"POST restart HTTP {status}"
    text = payload.decode("utf-8", errors="replace")[:300]
    raise DeviceError(f"restart failed HTTP {status}: {text}")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "timestamp",
    "device",
    "type",
    "action",
    "asic_temp_c",
    "vr_temp_c",
    "frequency_mhz",
    "new_frequency_mhz",
    "hashrate_ghs",
    "power_w",
    "core_voltage_mv",
    "new_voltage_mv",
    "error_pct",
    "rolling_error_pct",
    "reason",
]


class CsvLogger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists() or path.stat().st_size == 0
        self._fh = path.open("a", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._fh, fieldnames=CSV_FIELDS)
        if new_file:
            self._writer.writeheader()
            self._fh.flush()

    def write(self, row: dict[str, Any]) -> None:
        out = {k: row.get(k, "") for k in CSV_FIELDS}
        self._writer.writerow(out)
        self._fh.flush()
        try:
            os.fsync(self._fh.fileno())
        except OSError:
            pass

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


def setup_logging(log_file: Path, extra_handlers: Optional[list[logging.Handler]] = None) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    # A windowed (--noconsole) exe has no stdout; log to the file only.
    if sys.stdout is not None:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        stream.flush = sys.stdout.flush  # type: ignore[method-assign]
        root.addHandler(stream)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    if extra_handlers:
        for handler in extra_handlers:
            root.addHandler(handler)
    # Line-buffer stdout when possible so Task Scheduler captures it promptly.
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except Exception:
            pass


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Tuner
# ---------------------------------------------------------------------------

@dataclass
class DeviceState:
    cfg: DeviceConfig
    cool_polls: int = 0
    low_error_polls: int = 0
    error_window: deque[float] = field(default_factory=deque)
    last_error_pct: Optional[float] = None
    last_rolling_error_pct: Optional[float] = None
    last_error_source: str = ""
    last_shares_accepted: Optional[int] = None
    last_shares_rejected: Optional[int] = None
    last_voltage_reason: str = ""
    last_change_ts: float = 0.0
    last_seen_ok: bool = False
    last_action: str = ""
    last_reason: str = ""
    last_action_at: str = ""
    last_poll_at: str = ""
    bos_last_change_ts: float = 0.0
    bos_cool_polls: int = 0
    bos_last_action: str = ""
    bos_last_reason: str = ""
    bos_last_action_at: str = ""
    bos_last_target_w: Optional[int] = None
    quiet_fan_pct: Optional[int] = None
    quiet_suspended_until: float = 0.0
    tou_pre_cap_freq: Optional[float] = None
    tou_applied_freq: Optional[float] = None
    last_change_action: str = ""
    last_stable_freq: Optional[float] = None
    stable_candidate_freq: Optional[float] = None
    stable_since_ts: float = 0.0
    emergency_lockout_until: float = 0.0
    emergency_reason: str = ""
    emergency_at_iso: str = ""
    rollback_freq: Optional[float] = None
    vr_history: deque[tuple[float, float]] = field(default_factory=deque)
    last_valid_asic_c: Optional[float] = None
    last_valid_vr_c: Optional[float] = None
    sensor_suspect: bool = False
    sensor_suspect_reason: str = ""
    sensor_suspect_component: str = ""
    sensor_plausible_streak: int = 0
    bad_voltage_by_freq: dict[str, int] = field(default_factory=dict)
    learned_min_voltage_mv: Optional[int] = None

    def record_error(self, sample: Optional[float], source: str) -> Optional[float]:
        self.last_error_pct = sample
        if sample is None:
            self.last_error_source = ""
            self.last_rolling_error_pct = None
            self.low_error_polls = 0
            return None
        self.last_error_source = source or ""
        maxlen = max(1, int(self.cfg.error_window_polls))
        while len(self.error_window) >= maxlen:
            self.error_window.popleft()
        self.error_window.append(float(sample))
        self.last_rolling_error_pct = self.rolling_error()
        return self.last_rolling_error_pct

    def rolling_error(self) -> Optional[float]:
        if not self.error_window:
            return None
        return sum(self.error_window) / len(self.error_window)

    def clear_error_window(self) -> None:
        self.error_window.clear()
        self.low_error_polls = 0
        self.last_rolling_error_pct = None


def _safe_history_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "device").strip()).strip("._")
    return (cleaned or "device") + ".json"


class HistoryStore:
    """Bounded 24h per-device trend store (hashrate, ASIC temp, VR temp) under userdata/history/."""

    def __init__(
        self,
        history_dir: Optional[Path] = None,
        max_age_sec: float = 86400.0,
        min_interval_sec: float = 60.0,
        max_points: int = 1440,
    ):
        self.history_dir = Path(history_dir) if history_dir is not None else None
        self.max_age_sec = float(max_age_sec)
        self.min_interval_sec = float(min_interval_sec)
        self.max_points = max(10, int(max_points))
        self._lock = threading.RLock()
        self._series: dict[str, list[list[Any]]] = {}
        self._last_saved: dict[str, float] = {}

    def _path_for(self, name: str) -> Optional[Path]:
        if self.history_dir is None:
            return None
        return self.history_dir / _safe_history_filename(name)

    def load_device(self, name: str) -> list[list[Any]]:
        with self._lock:
            if name in self._series:
                return self._series[name]
            pts: list[list[Any]] = []
            path = self._path_for(name)
            if path is not None and path.exists():
                raw = _load_json_dict(path)
                if raw and isinstance(raw.get("points"), list):
                    for row in raw["points"]:
                        if isinstance(row, list) and len(row) >= 3:
                            try:
                                ts = float(row[0])
                                hr = float(row[1]) if row[1] is not None else None
                                at = float(row[2]) if row[2] is not None else None
                                vt = float(row[3]) if len(row) > 3 and row[3] is not None else None
                                pts.append([ts, hr, at, vt])
                            except (TypeError, ValueError):
                                continue
            self._series[name] = pts
            return pts

    def _save_device(self, name: str) -> None:
        path = self._path_for(name)
        if path is None:
            return
        pts = self._series.get(name, [])
        try:
            _write_json_atomic(path, {"schema_version": 1, "device": name, "points": pts}, backup=False)
        except OSError as exc:
            LOG.warning("Could not write history file %s: %s", path, exc)

    def record(
        self,
        name: str,
        ts: float,
        hashrate_ghs: Optional[float],
        asic_temp_c: Optional[float],
        vr_temp_c: Optional[float] = None,
    ) -> None:
        if hashrate_ghs is None and asic_temp_c is None:
            return
        with self._lock:
            pts = self.load_device(name)
            cutoff = float(ts) - self.max_age_sec
            if pts and pts[0][0] < cutoff:
                pts = [row for row in pts if row[0] >= cutoff]
                self._series[name] = pts
            sample = [
                round(float(ts), 1),
                round(float(hashrate_ghs), 2) if hashrate_ghs is not None else None,
                round(float(asic_temp_c), 2) if asic_temp_c is not None else None,
                round(float(vr_temp_c), 2) if vr_temp_c is not None else None,
            ]
            should_save = False
            if not pts or (float(ts) - pts[-1][0]) >= self.min_interval_sec:
                pts.append(sample)
                if len(pts) > self.max_points:
                    pts = pts[-self.max_points :]
                self._series[name] = pts
                should_save = True
            else:
                pts[-1] = sample
                if (float(ts) - self._last_saved.get(name, 0.0)) >= self.min_interval_sec:
                    should_save = True
            if should_save:
                self._save_device(name)
                self._last_saved[name] = float(ts)

    def rename(self, old_name: str, new_name: str) -> None:
        with self._lock:
            pts = self.load_device(old_name)
            self._series.pop(old_name, None)
            self._series[new_name] = pts
            old_p = self._path_for(old_name)
            new_p = self._path_for(new_name)
            if old_p is not None and new_p is not None and old_p.exists():
                try:
                    new_p.parent.mkdir(parents=True, exist_ok=True)
                    old_p.replace(new_p)
                except OSError:
                    self._save_device(new_name)
            elif pts:
                self._save_device(new_name)

    def remove(self, name: str) -> None:
        with self._lock:
            self._series.pop(name, None)
            self._last_saved.pop(name, None)
            p = self._path_for(name)
            if p is not None and p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

    def get_series(self, name: str, max_return: int = 180) -> list[list[Any]]:
        with self._lock:
            pts = self.load_device(name)
            if not pts:
                return []
            if len(pts) <= max_return:
                return [list(r) for r in pts]
            step = (len(pts) - 1) / float(max_return - 1)
            out: list[list[Any]] = []
            seen: set[int] = set()
            for i in range(max_return):
                idx = min(len(pts) - 1, int(round(i * step)))
                if idx not in seen:
                    seen.add(idx)
                    out.append(list(pts[idx]))
            if (len(pts) - 1) not in seen:
                out.append(list(pts[-1]))
            return out


class LastShareStore:
    """Persist derived last-share counters in userdata so they survive dashboard restarts."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        raw = _load_json_dict(self.path) if self.path is not None else None
        devices = raw.get("devices") if isinstance(raw, dict) else None
        if isinstance(devices, dict):
            for name, row in devices.items():
                if not isinstance(row, dict):
                    continue
                try:
                    acc = int(row["accepted"]) if row.get("accepted") is not None else None
                except (TypeError, ValueError):
                    acc = None
                if acc is None:
                    continue
                seen_ts: Optional[float] = None
                if row.get("seen_ts") is not None:
                    try:
                        seen_ts = float(row["seen_ts"])
                    except (TypeError, ValueError):
                        seen_ts = None
                self._data[str(name)] = {"accepted": acc, "seen_ts": seen_ts}
        self._loaded = True

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(
                self.path,
                {"schema_version": 1, "devices": self._data},
                backup=False,
            )
        except OSError as exc:
            LOG.warning("Could not write last-share file %s: %s", self.path, exc)

    def get(self, name: str) -> Optional[dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            row = self._data.get(name)
            return dict(row) if row is not None else None

    def set(self, name: str, accepted: int, seen_ts: Optional[float]) -> None:
        with self._lock:
            self._ensure_loaded()
            self._data[name] = {"accepted": int(accepted), "seen_ts": seen_ts}
            self._save()

    def rename(self, old_name: str, new_name: str) -> None:
        with self._lock:
            self._ensure_loaded()
            if old_name in self._data:
                self._data[new_name] = self._data.pop(old_name)
                self._save()

    def remove(self, name: str) -> None:
        with self._lock:
            self._ensure_loaded()
            if name in self._data:
                self._data.pop(name, None)
                self._save()


def derive_last_share(reading: Reading, name: str, now: float, store: LastShareStore) -> Reading:
    """When firmware has no lastSubmittedDiff/lastShareSeconds, infer last share from accepted-count increases.

    Display uses '≥ poolDifficulty' because every accepted share is at least the pool difficulty.
    The first observation (and a counter decrease after a miner reboot) shows 'waiting for share…'
    until the accepted count increases. Tracking state is persisted in userdata.
    """
    if reading.last_submitted_diff is not None or reading.last_share_seconds is not None:
        reading.last_share_estimated = False
        reading.last_share_waiting = False
        return reading

    count = reading.shares_accepted
    if count is None:
        return reading

    prev = store.get(name)
    if prev is None or count < int(prev["accepted"]):
        store.set(name, int(count), None)
        reading.last_share_waiting = True
        reading.last_share_estimated = False
        reading.last_submitted_diff = None
        reading.last_share_seconds = None
        return reading

    if count > int(prev["accepted"]):
        store.set(name, int(count), float(now))
        reading.last_share_waiting = False
        reading.last_share_estimated = True
        reading.last_share_seconds = 0.0
        if reading.pool_difficulty is not None:
            reading.last_submitted_diff = reading.pool_difficulty
        return reading

    if prev.get("seen_ts") is not None:
        reading.last_share_waiting = False
        reading.last_share_estimated = True
        reading.last_share_seconds = max(0.0, float(now) - float(prev["seen_ts"]))
        if reading.pool_difficulty is not None:
            reading.last_submitted_diff = reading.pool_difficulty
    else:
        reading.last_share_waiting = True
        reading.last_share_estimated = False
    return reading


def schedule_adjusted_cfg(cfg: DeviceConfig, sched: Optional[dict[str, Any]]) -> DeviceConfig:
    """Copy of cfg with a time-of-use window's lower frequency cap applied.

    Normal global or per-machine temperature targets and emergency thresholds
    (asic_target_c / asic_max_c / vr_target_c / vr_max_c) always continue to
    apply unchanged during TOU windows.
    """
    if not sched:
        return cfg
    changes: dict[str, Any] = {}
    cap = sched.get("max_freq")
    if cap is not None and float(cap) < float(cfg.max_freq):
        changes["max_freq"] = max(float(cfg.min_freq), float(cap))
    return dataclasses.replace(cfg, **changes) if changes else cfg


def apply_schedule_cap(
    decision: Decision,
    reading: Reading,
    cfg: DeviceConfig,
    dcfg: DeviceConfig,
    sched: Optional[dict[str, Any]],
) -> Decision:
    """Always enforce an active time-of-use frequency cap when current freq > cap.

    Works regardless of what action decide() returned (step_up, hold, voltage_up,
    voltage_down, fault_hold, or emergency_down), bypassing normal cooldown so the
    cap takes effect on the very next poll. Offline/unparsed/unpowered miners are left
    untouched; emergency_down keeps its emergency action/voltage reset while clamping
    new_freq down to <= cap.
    """
    if not sched:
        return decision
    raw_cap = sched.get("max_freq")
    if raw_cap is None:
        return decision
    try:
        cap_val = float(raw_cap)
    except (TypeError, ValueError):
        return decision
    eff_cap = max(float(cfg.min_freq), min(float(cfg.max_freq), float(dcfg.max_freq), cap_val))
    names = ", ".join(sched.get("windows") or [])
    if decision.action in {"offline", "invalid", "unparsed", "monitor"}:
        return decision
    unpowered = (
        (reading.core_voltage_actual is not None and reading.core_voltage_actual <= 0 and reading.core_voltage is not None and reading.core_voltage > 0)
        or (reading.power is not None and reading.power <= 2.0)
    )
    if decision.action == "fault_hold" and unpowered:
        return decision
    freq = reading.frequency
    tag = f"cap {eff_cap:.0f} MHz"
    if freq is None or freq <= eff_cap + 1e-6:
        if decision.reason and eff_cap < cfg.max_freq - 1e-6:
            decision.reason += f" [time-of-use '{names}': {tag}]"
        return decision
    if cfg.frequency_options:
        opts = sorted({float(f) for f in cfg.frequency_options if cfg.min_freq - 1e-6 <= float(f) <= eff_cap + 1e-6})
        cap_f = opts[-1] if opts else max(cfg.min_freq, min(float(freq), eff_cap))
    else:
        cap_f = max(cfg.min_freq, eff_cap)
    if cap_f >= freq - 1e-6:
        return decision
    if decision.action == "emergency_down":
        if decision.new_freq is None or float(decision.new_freq) > cap_f + 1e-6:
            decision.new_freq = cap_f
        decision.bypass_cooldown = True
        decision.reason += f" [time-of-use '{names}': {tag}]"
        return decision
    if decision.action == "step_down" and decision.new_freq is not None:
        if float(decision.new_freq) > cap_f + 1e-6:
            decision.new_freq = cap_f
        decision.bypass_cooldown = True
        decision.reason += f" [time-of-use '{names}': {tag}]"
        return decision
    return Decision(
        "step_down",
        cap_f,
        f"time-of-use window '{names}': frequency cap {eff_cap:.0f} MHz; {freq:.0f} -> {cap_f:.0f} MHz",
        0,
        bypass_cooldown=True,
        low_error_polls=0,
        voltage_reason=decision.voltage_reason,
    )




class TunerSafetyStore:
    """Persisted post-emergency lockouts and last stable frequencies under userdata/tuner_safety.json."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self.devices: dict[str, dict[str, Any]] = {}
        if self.path and self.path.exists():
            raw = _load_json_dict(self.path)
            if isinstance(raw, dict) and isinstance(raw.get("devices"), dict):
                for k, v in raw["devices"].items():
                    if isinstance(v, dict):
                        self.devices[str(k)] = dict(v)

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            try:
                _write_json_atomic(self.path, {"schema": 1, "devices": self.devices})
            except OSError as exc:
                LOG.warning("Could not save tuner safety state: %s", exc)

    def get(self, name: str) -> dict[str, Any]:
        with self._lock:
            return dict(self.devices.get(name) or {})

    def update(self, name: str, data: dict[str, Any]) -> None:
        with self._lock:
            cur = dict(self.devices.get(name) or {})
            cur.update(data)
            self.devices[name] = cur
        self.save()

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            if old in self.devices:
                self.devices[new] = self.devices.pop(old)
                self.save()

    def remove(self, name: str) -> None:
        with self._lock:
            if name in self.devices:
                self.devices.pop(name, None)
                self.save()


class Tuner:
    def __init__(
        self,
        config: GlobalConfig,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        csv_logger: Optional[CsvLogger] = None,
        shared: Any = None,
    ):
        self.config = config
        self.clock = clock
        self.sleeper = sleeper
        self.csv_logger = csv_logger
        self.shared = shared
        self._lock = threading.RLock()
        self._wake = threading.Event()
        hist_dir: Optional[Path] = None
        if config.config_path is not None or config.userdata_file != (SCRIPT_DIR / "userdata" / "devices.json"):
            hist_dir = config.userdata_file.parent / "history"
        self.history = HistoryStore(hist_dir)
        ls_path: Optional[Path] = None
        if hist_dir is not None:
            ls_path = config.userdata_file.parent / "last_share.json"
        self.last_share = LastShareStore(ls_path)
        safety_path = (config.userdata_file.parent / "tuner_safety.json") if hist_dir is not None else None
        self.safety_store = TunerSafetyStore(safety_path)
        self.emergency_lockout_sec = DEFAULT_EMERGENCY_LOCKOUT_SEC
        self.states = {d.name: DeviceState(cfg=d) for d in config.devices}
        for d in config.devices:
            self.history.load_device(d.name)
            saved_sf = self.safety_store.get(d.name)
            if saved_sf:
                st = self.states[d.name]
                if saved_sf.get("last_stable_freq") is not None:
                    st.last_stable_freq = float(saved_sf["last_stable_freq"])
                if saved_sf.get("emergency_lockout_until"):
                    st.emergency_lockout_until = float(saved_sf["emergency_lockout_until"])
                if saved_sf.get("rollback_freq") is not None:
                    st.rollback_freq = float(saved_sf["rollback_freq"])
                st.emergency_reason = str(saved_sf.get("emergency_reason") or "")
                st.emergency_at_iso = str(saved_sf.get("emergency_at_iso") or "")
                raw_bvf = saved_sf.get("bad_voltage_by_freq")
                if isinstance(raw_bvf, dict):
                    for fk, vv in raw_bvf.items():
                        try:
                            st.bad_voltage_by_freq[str(int(round(float(fk))))] = int(round(float(vv)))
                        except (TypeError, ValueError):
                            continue
        # Miner Hearth v1.8 feature hub (alerts, block watch, schedules, tuning log...).
        # Attached by miner_hearth.attach_feature_hub(); None keeps the classic engine.
        self.hub: Any = None
        self._publish_static()

    def wake(self) -> None:
        self._wake.set()

    def _dry_run(self) -> bool:
        if self.shared is not None:
            return bool(self.shared.is_dry_run())
        return bool(self.config.dry_run)

    def _enabled(self, name: str) -> bool:
        with self._lock:
            state = self.states.get(name)
            if state is not None and not is_tunable(state.cfg):
                return False
        if self.shared is not None:
            if hasattr(self.shared, "is_autotune_enabled") and not self.shared.is_autotune_enabled():
                return False
            return bool(self.shared.is_enabled(name))
        return True

    def set_autotune_enabled(self, enabled: bool) -> bool:
        val = bool(enabled)
        with self._lock:
            self.config.autotune_enabled = val
        if self.shared is not None and hasattr(self.shared, "set_autotune_enabled"):
            self.shared.set_autotune_enabled(val)
        self._persist_userdata()
        if val:
            self.wake()
        return val

    def _persist_userdata(self) -> None:
        if self.config.userdata_file is not None:
            auto_en = (
                self.shared.is_autotune_enabled()
                if (self.shared is not None and hasattr(self.shared, "is_autotune_enabled"))
                else self.config.autotune_enabled
            )
            save_userdata_devices(
                self.config.userdata_file,
                self.config.devices,
                dry_run=self._dry_run(),
                scan_subnet=self.config.scan_subnet,
                global_tuner=self.config.global_tuner,
                display_fields=self.config.display_fields,
                device_order=[d.name for d in self.config.devices],
                autotune_enabled=auto_en,
            )

    def _device_payload_static(self, dev: DeviceConfig) -> dict[str, Any]:
        return {
            "name": dev.name,
            "ip": dev.ip,
            "type": dev.type,
            "family": dev.family,
            "algorithm": dev.algorithm or "sha256d",
            "coin": dev.coin,
            "merged_coin": dev.merged_coin,
            "write_enabled": bool(dev.write_enabled or (dev.type == "antminer" and dev.bos_power_mgmt)),
            "api_has_token": bool(dev.api_token),
            "adapter_config": dict(dev.adapter_config),
            "capabilities": {},
            "tunable": is_tunable(dev),
            "model": "",
            "online": False,
            "asic_target_c": dev.asic_target_c,
            "asic_max_c": dev.asic_max_c,
            "vr_target_c": dev.vr_target_c,
            "vr_max_c": dev.vr_max_c,
            "min_freq": dev.min_freq,
            "max_freq": dev.max_freq,
            "emergency_step_mhz": dev.emergency_step_mhz,
            "voltage_control": dev.voltage_control,
            "vr_voltage_relief": dev.vr_voltage_relief,
            "voltage_start_mv": dev.voltage_start_mv,
            "emergency_voltage_mv": dev.emergency_voltage_mv,
            "min_voltage_mv": dev.min_voltage_mv,
            "max_voltage_mv": dev.max_voltage_mv,
            "error_target_pct": dev.error_target_pct,
            "error_high_pct": dev.error_high_pct,
            "error_low_pct": dev.error_low_pct,
            "error_window_polls": dev.error_window_polls,
            "low_error_polls_required": dev.low_error_polls_required,
            "overrides": dict(dev.overrides),
            "overclock_opt_in": bool(dev.overclock_opt_in),
            "model_normal_max_freq": NERD_NORMAL_MAX_FREQ_MHZ if dev.type == "nerdoctaxe" else HARD_MAX_FREQ_MHZ,
            "manual_frequency_mhz": dev.manual_frequency_mhz,
            "manual_voltage_mv": dev.manual_voltage_mv,
            "bos_power_mgmt": bool(dev.bos_power_mgmt),
            "bos_grpc_port": int(dev.bos_grpc_port),
            "bos_username": dev.bos_username,
            "bos_has_password": bool(dev.bos_password),
            "bos_min_watts": int(dev.bos_min_watts),
            "bos_max_watts": int(dev.bos_max_watts),
            "bos_step_watts": int(dev.bos_step_watts),
            "bos_chip_target_c": float(dev.bos_chip_target_c),
            "bos_chip_hot_c": float(dev.bos_chip_hot_c),
            "bos_cooldown_sec": float(dev.bos_cooldown_sec),
            "bos_cool_polls_required": int(dev.bos_cool_polls_required),
            "bos_last_action": "",
            "bos_last_reason": "",
            "bos_last_action_at": "",
            "pool_url": "",
            "pool_port": None,
            "pool_user": "",
            "fallback_pool_url": "",
            "fallback_pool_port": None,
            "fallback_pool_user": "",
            "using_fallback_stratum": False,
            "history": self.history.get_series(dev.name),
            "voltage_reason": "" if is_tunable(dev) else ("Braiins power-target mode" if (dev.type == "antminer" and dev.bos_power_mgmt) else "monitor only"),
        }

    def _publish_static(self) -> None:
        if self.shared is None:
            return
        with self._lock:
            devs = list(self.config.devices)
            order = [d.name for d in devs]
            self.config.device_order = order
            if hasattr(self.shared, "set_settings"):
                self.shared.set_settings(
                    display_fields=self.config.display_fields,
                    global_tuner=self.config.global_tuner,
                    device_order=order,
                )
        for dev in devs:
            self.shared.update_device(dev.name, self._device_payload_static(dev))

    def reorder_devices(self, order: list[str]) -> tuple[bool, str, list[str]]:
        if not isinstance(order, list):
            return False, "order must be a list of device names", []
        with self._lock:
            by_name = {d.name: d for d in self.config.devices}
            new_devs: list[DeviceConfig] = []
            used: set[str] = set()
            for raw_n in order:
                nm = str(raw_n or "").strip()
                if nm in by_name and nm not in used:
                    new_devs.append(by_name[nm])
                    used.add(nm)
            for d in self.config.devices:
                if d.name not in used:
                    new_devs.append(d)
                    used.add(d.name)
            self.config.devices = new_devs
            new_order = [d.name for d in new_devs]
            self.config.device_order = new_order
            if self.shared is not None and hasattr(self.shared, "set_device_order"):
                self.shared.set_device_order(new_order)
            self._persist_userdata()
            LOG.info("Updated dashboard card order: %s", ", ".join(new_order))
            return True, "", new_order

    def update_settings(self, spec: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        if not isinstance(spec, dict):
            return False, "expected JSON object", {}
        with self._lock:
            if "display_fields" in spec:
                merged = dict(self.config.display_fields)
                if isinstance(spec["display_fields"], dict):
                    for k, v in spec["display_fields"].items():
                        if k in merged:
                            merged[k] = bool(v)
                self.config.display_fields = normalize_display_fields(merged)

            if "global_tuner" in spec and isinstance(spec["global_tuner"], dict):
                merged_gt = dict(self.config.global_tuner)
                merged_gt.update(spec["global_tuner"])
                self.config.global_tuner = normalize_global_tuner(merged_gt)
                self.config.defaults.update(self.config.global_tuner)
                for dev in self.config.devices:
                    if is_tunable(dev):
                        apply_global_and_overrides(dev, self.config.global_tuner)
                        if self.shared is not None:
                            self.shared.update_device(dev.name, self._tunable_limits_payload(dev))

            if "device_order" in spec and isinstance(spec["device_order"], list):
                self.reorder_devices(spec["device_order"])

            if self.shared is not None and hasattr(self.shared, "set_settings"):
                self.shared.set_settings(
                    display_fields=self.config.display_fields,
                    global_tuner=self.config.global_tuner,
                    device_order=self.config.device_order,
                )
            self._persist_userdata()
            return True, "", {
                "display_fields": dict(self.config.display_fields),
                "global_tuner": dict(self.config.global_tuner),
                "device_order": list(self.config.device_order),
            }

    def _tunable_limits_payload(self, dev: DeviceConfig) -> dict[str, Any]:
        return {
            "type": dev.type,
            "voltage_control": bool(dev.voltage_control),
            "vr_voltage_relief": bool(dev.vr_voltage_relief),
            "min_freq": dev.min_freq,
            "max_freq": dev.max_freq,
            "emergency_step_mhz": dev.emergency_step_mhz,
            "asic_target_c": dev.asic_target_c,
            "asic_max_c": dev.asic_max_c,
            "vr_target_c": dev.vr_target_c,
            "vr_max_c": dev.vr_max_c,
            "voltage_start_mv": dev.voltage_start_mv,
            "emergency_voltage_mv": dev.emergency_voltage_mv,
            "min_voltage_mv": dev.min_voltage_mv,
            "max_voltage_mv": dev.max_voltage_mv,
            "error_target_pct": dev.error_target_pct,
            "error_low_pct": dev.error_low_pct,
            "error_high_pct": dev.error_high_pct,
            "overrides": dict(dev.overrides),
            "overclock_opt_in": bool(dev.overclock_opt_in),
            "model_normal_max_freq": NERD_NORMAL_MAX_FREQ_MHZ if dev.type == "nerdoctaxe" else HARD_MAX_FREQ_MHZ,
        }

    def update_device_overrides(self, name: str, spec: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        if not isinstance(spec, dict):
            return False, "expected JSON object", {}
        raw_ov = spec.get("overrides") if isinstance(spec.get("overrides"), dict) else spec
        with self._lock:
            state = self.states.get(name)
            if state is None:
                return False, f"unknown device: {name}", {}
            cfg = state.cfg
            if not is_tunable(cfg):
                return False, f"{name} is not a tunable Axe device", {}
            if spec.get("reset"):
                cfg.overrides = {}
                cfg.overclock_opt_in = False
                if cfg.type in {"gamma", "axeos-generic"}:
                    cfg.voltage_control = (cfg.type == "gamma")
                    cfg.vr_voltage_relief = (cfg.type == "axeos-generic")
                state.bad_voltage_by_freq.clear()
                state.learned_min_voltage_mv = None
                self.safety_store.update(name, {"bad_voltage_by_freq": {}})
            else:
                new_ov = dict(cfg.overrides)
                if cfg.type == "nerdoctaxe":
                    if "overclock_opt_in" in raw_ov:
                        cfg.overclock_opt_in = _as_bool(raw_ov["overclock_opt_in"], False)
                    elif "overclock_opt_in" in spec:
                        cfg.overclock_opt_in = _as_bool(spec["overclock_opt_in"], False)
                    if cfg.overclock_opt_in:
                        new_ov["overclock_opt_in"] = True
                    else:
                        new_ov.pop("overclock_opt_in", None)
                elif cfg.type in {"gamma", "axeos-generic"} and "voltage_control" in raw_ov:
                    v_raw = raw_ov["voltage_control"]
                    default_vc = (cfg.type == "gamma")
                    if v_raw is None or v_raw == "":
                        new_ov.pop("voltage_control", None)
                        cfg.voltage_control = default_vc
                    else:
                        cfg.voltage_control = _as_bool(v_raw, default_vc)
                        if cfg.voltage_control == default_vc:
                            new_ov.pop("voltage_control", None)
                        else:
                            new_ov["voltage_control"] = cfg.voltage_control
                    cfg.vr_voltage_relief = bool(cfg.type == "axeos-generic" and not cfg.voltage_control)
                for k in GLOBAL_TUNER_KEYS:
                    if k not in raw_ov:
                        continue
                    v = raw_ov[k]
                    if v is None or v == "":
                        new_ov.pop(k, None)
                    else:
                        try:
                            if k in {"max_freq", "min_voltage_mv", "max_voltage_mv", "emergency_step_mhz", "emergency_voltage_mv"}:
                                new_ov[k] = int(round(float(v)))
                            else:
                                new_ov[k] = float(v)
                        except (TypeError, ValueError):
                            return False, f"invalid value for {k}: {v!r}", {}
                # Validate against hard bounds & NerdOCTAxe 800 MHz firmware normal cap
                if "max_freq" in new_ov:
                    if cfg.type == "nerdoctaxe" and not cfg.overclock_opt_in and int(new_ov["max_freq"]) > int(NERD_NORMAL_MAX_FREQ_MHZ):
                        return False, f"NerdOCTAxe normal firmware max is {int(NERD_NORMAL_MAX_FREQ_MHZ)} MHz. Enable the explicit overclock opt-in to raise max_freq above {int(NERD_NORMAL_MAX_FREQ_MHZ)} MHz.", {}
                    cap_f = int(HARD_MAX_FREQ_MHZ if (cfg.type != "nerdoctaxe" or cfg.overclock_opt_in) else NERD_NORMAL_MAX_FREQ_MHZ)
                    new_ov["max_freq"] = max(int(cfg.min_freq), min(cap_f, int(new_ov["max_freq"])))
                if "max_voltage_mv" in new_ov:
                    new_ov["max_voltage_mv"] = max(HARD_MIN_VOLTAGE_MV, min(HARD_MAX_VOLTAGE_MV, int(new_ov["max_voltage_mv"])))
                if "min_voltage_mv" in new_ov:
                    new_ov["min_voltage_mv"] = max(HARD_MIN_VOLTAGE_MV, min(HARD_MAX_VOLTAGE_MV, int(new_ov["min_voltage_mv"])))
                if "emergency_voltage_mv" in new_ov:
                    new_ov["emergency_voltage_mv"] = max(HARD_MIN_VOLTAGE_MV, min(HARD_MAX_VOLTAGE_MV, int(new_ov["emergency_voltage_mv"])))
                cfg.overrides = new_ov
            apply_global_and_overrides(cfg, self.config.global_tuner)
            payload = self._tunable_limits_payload(cfg)
            if self.shared is not None:
                self.shared.update_device(name, payload)
            self._persist_userdata()
            LOG.info("%s: updated per-machine overrides (%s)", name, cfg.overrides or "using global defaults")
            return True, "", payload

    def update_braiins_power(self, name: str, spec: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        if not isinstance(spec, dict):
            return False, "expected JSON object", {}
        with self._lock:
            state = self.states.get(name)
            if state is None:
                return False, f"unknown device: {name}", {}
            cfg = state.cfg
            if cfg.type != "antminer":
                return False, f"{name} is not an Antminer S19 device", {}
            if "bos_power_mgmt" in spec:
                cfg.bos_power_mgmt = _as_bool(spec["bos_power_mgmt"], False)
            if "bos_grpc_port" in spec and spec["bos_grpc_port"] not in (None, ""):
                cfg.bos_grpc_port = max(1, min(65535, _as_int(spec["bos_grpc_port"], 50051)))
            if "bos_username" in spec:
                cfg.bos_username = str(spec["bos_username"] or "root").strip() or "root"
            if spec.get("clear_password"):
                cfg.bos_password = ""
            elif "bos_password" in spec and str(spec["bos_password"] or "") != "":
                cfg.bos_password = str(spec["bos_password"])
            if "bos_min_watts" in spec and spec["bos_min_watts"] not in (None, ""):
                cfg.bos_min_watts = max(1, min(10000, _as_int(spec["bos_min_watts"], 2000)))
            if "bos_max_watts" in spec and spec["bos_max_watts"] not in (None, ""):
                cfg.bos_max_watts = max(cfg.bos_min_watts, min(10000, _as_int(spec["bos_max_watts"], 3500)))
            if cfg.bos_min_watts > cfg.bos_max_watts:
                cfg.bos_min_watts = cfg.bos_max_watts
            if "bos_step_watts" in spec and spec["bos_step_watts"] not in (None, ""):
                cfg.bos_step_watts = max(10, min(500, _as_int(spec["bos_step_watts"], 50)))
            if "bos_chip_target_c" in spec and spec["bos_chip_target_c"] not in (None, ""):
                cfg.bos_chip_target_c = max(40.0, min(95.0, _as_float(spec["bos_chip_target_c"], 75.0)))
            if "bos_chip_hot_c" in spec and spec["bos_chip_hot_c"] not in (None, ""):
                cfg.bos_chip_hot_c = max(cfg.bos_chip_target_c + 1.0, min(105.0, _as_float(spec["bos_chip_hot_c"], 82.0)))
            if "bos_cooldown_sec" in spec and spec["bos_cooldown_sec"] not in (None, ""):
                cfg.bos_cooldown_sec = max(30.0, min(3600.0, _as_float(spec["bos_cooldown_sec"], 300.0)))
            if "bos_cool_polls_required" in spec and spec["bos_cool_polls_required"] not in (None, ""):
                cfg.bos_cool_polls_required = max(1, min(60, _as_int(spec["bos_cool_polls_required"], 4)))

            state.bos_cool_polls = 0
            out = {
                "write_enabled": bool(cfg.write_enabled or cfg.bos_power_mgmt),
                "bos_power_mgmt": bool(cfg.bos_power_mgmt),
                "bos_grpc_port": int(cfg.bos_grpc_port),
                "bos_username": cfg.bos_username,
                "bos_has_password": bool(cfg.bos_password),
                "web_username": cfg.web_username,
                "web_has_password": bool(cfg.web_password),
                "bos_min_watts": int(cfg.bos_min_watts),
                "bos_max_watts": int(cfg.bos_max_watts),
                "bos_step_watts": int(cfg.bos_step_watts),
                "bos_chip_target_c": float(cfg.bos_chip_target_c),
                "bos_chip_hot_c": float(cfg.bos_chip_hot_c),
                "bos_cooldown_sec": float(cfg.bos_cooldown_sec),
                "bos_cool_polls_required": int(cfg.bos_cool_polls_required),
            }
            if self.shared is not None:
                self.shared.update_device(name, out)
            self._persist_userdata()
            LOG.info(
                "%s: Braiins power-target management %s (%d-%d W, step %d W, cooldown %.0fs)",
                name,
                "ON" if cfg.bos_power_mgmt else "OFF",
                cfg.bos_min_watts,
                cfg.bos_max_watts,
                cfg.bos_step_watts,
                cfg.bos_cooldown_sec,
            )
            return True, "", out

    def test_braiins_connection(self, name: str, spec: Optional[dict[str, Any]] = None) -> tuple[bool, str, dict[str, Any]]:
        with self._lock:
            state = self.states.get(name)
            if state is None:
                return False, f"unknown device: {name}", {}
            cfg = state.cfg
            if cfg.type != "antminer":
                return False, f"{name} is not an Antminer S19 device", {}
            ip = cfg.ip
            http_port = int(cfg.port or 80)
            grpc_port = int(cfg.bos_grpc_port or 50051)
            username = cfg.bos_username or "root"
            password = cfg.bos_password or ""
            timeout = float(cfg.timeout_sec or 5.0)
            if isinstance(spec, dict):
                if spec.get("bos_grpc_port") not in (None, ""):
                    grpc_port = max(1, min(65535, _as_int(spec["bos_grpc_port"], grpc_port)))
                if spec.get("bos_username"):
                    username = str(spec["bos_username"]).strip() or username
                if spec.get("bos_password") not in (None, ""):
                    password = str(spec["bos_password"])
        import braiins_api
        res = braiins_api.test_connection(
            ip,
            username=username,
            password=password,
            grpc_port=grpc_port,
            http_port=http_port,
            timeout=timeout,
        )
        return True, "", res

    def update_device_adapter(self, name: str, spec: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        if not isinstance(spec, dict):
            return False, "expected JSON object", {}
        with self._lock:
            state = self.states.get(name)
            if state is None:
                return False, f"unknown device: {name}", {}
            cfg = state.cfg
            if "write_enabled" in spec:
                cfg.write_enabled = _as_bool(spec["write_enabled"], False)
            if "coin" in spec:
                cfg.coin = str(spec["coin"] or "").strip().upper()
            if "merged_coin" in spec:
                cfg.merged_coin = str(spec["merged_coin"] or "").strip().upper()
            if "algorithm" in spec:
                cfg.algorithm = str(spec["algorithm"] or "").strip().lower()
            if "family" in spec and spec["family"]:
                cfg.family = str(spec["family"]).strip().lower()
            if "web_username" in spec:
                cfg.web_username = str(spec["web_username"] or "").strip()[:64]
            if spec.get("clear_web_password"):
                cfg.web_password = ""
            elif "web_password" in spec and str(spec["web_password"] or "") != "":
                cfg.web_password = str(spec["web_password"])
            if spec.get("clear_api_token"):
                cfg.api_token = ""
            elif "api_token" in spec and str(spec["api_token"] or "") != "":
                cfg.api_token = str(spec["api_token"]).strip()
            if isinstance(spec.get("adapter_config"), dict):
                merged = dict(cfg.adapter_config)
                for ak, av in spec["adapter_config"].items():
                    if av in (None, ""):
                        merged.pop(str(ak), None)
                    else:
                        merged[str(ak)] = av
                cfg.adapter_config = merged
            out = {
                "write_enabled": bool(cfg.write_enabled or (cfg.type == "antminer" and cfg.bos_power_mgmt)),
                "coin": cfg.coin,
                "merged_coin": cfg.merged_coin,
                "algorithm": cfg.algorithm,
                "family": cfg.family,
                "web_username": cfg.web_username,
                "web_has_password": bool(cfg.web_password),
                "api_has_token": bool(cfg.api_token),
                "adapter_config": dict(cfg.adapter_config),
            }
            if self.shared is not None:
                self.shared.update_device(name, out)
            self._persist_userdata()
            return True, "", out

    def apply_adapter_action(self, name: str, action: str, params: Optional[dict[str, Any]] = None) -> tuple[bool, str]:
        """Execute an opt-in write action for a Tier-1 miner adapter.

        Monitor-only by default: refuses all writes unless cfg.write_enabled is True.
        Respects global dry_run mode.
        """
        import nk_adapters
        params = params if isinstance(params, dict) else {}
        cfg = self._cfg(name)
        if not cfg.write_enabled:
            return False, f"{name} is monitor-only (enable 'Allow write controls' on this device first)"
        dry = self._dry_run()
        rd = getattr(self.states.get(name), "_nk_reading", None)
        fam = (cfg.family or (getattr(rd, "family", "") if rd else "") or "").strip().lower()
        act = str(action or "").strip().lower()
        timeout = min(max(cfg.timeout_sec, 4.0), 10.0)

        if dry:
            return True, f"dry-run: would run {fam or cfg.type} {act} ({params or {}})"

        if fam == "avalon":
            av = nk_adapters.AvalonAdapter()
            port = int(cfg.cgminer_port or 4028)
            if act == "workmode":
                return av.set_workmode(cfg.ip, port, str(params.get("mode", "")), timeout=timeout)
            if act == "soft_off":
                return av.soft_off(cfg.ip, port, timeout=timeout)
            if act == "soft_on":
                return av.soft_on(cfg.ip, port, timeout=timeout)
            if act == "reboot":
                return av.reboot(cfg.ip, port, timeout=timeout)
            return False, f"Unsupported Avalon action: {act}"

        if fam == "vnish":
            vn = nk_adapters.VnishAdapter()
            port = int(cfg.port or 80)
            if act == "throttle":
                return vn.set_throttle(cfg.ip, port, cfg.web_password, int(params.get("pct", 100)), timeout=timeout)
            if act == "preset":
                return vn.set_preset(cfg.ip, port, cfg.web_password, str(params.get("preset", "")), timeout=timeout)
            if act in {"pause", "resume", "restart", "reboot"}:
                return vn.action(cfg.ip, port, cfg.web_password, act, timeout=timeout)
            return False, f"Unsupported VNish action: {act}"

        if fam == "luxos":
            lx = nk_adapters.LuxOSAdapter()
            port = int(cfg.cgminer_port or 4028)
            if act == "profileset":
                return lx.set_profile(cfg.ip, port, str(params.get("profile", "")), timeout=timeout)
            return False, f"Unsupported LuxOS action: {act}"

        if fam == "bitmain-stock":
            bm = nk_adapters.BitmainStockAdapter()
            port = int(cfg.port or 80)
            if act == "workmode":
                return bm.set_workmode(cfg.ip, port, cfg.web_username, cfg.web_password, str(params.get("mode", "")), timeout=timeout)
            if act == "reboot":
                return bm.reboot(cfg.ip, port, cfg.web_username, cfg.web_password, timeout=timeout)
            return False, f"Unsupported Bitmain Stock action: {act}"

        if fam == "whatsminer":
            wm = nk_adapters.WhatsminerAdapter()
            acct = cfg.web_username or "super"
            if act == "power_mode":
                return wm.set_power_mode(cfg.ip, cfg.web_password, str(params.get("mode", "")), account=acct, timeout=timeout)
            if act == "power_pct":
                return wm.set_power_pct(cfg.ip, cfg.web_password, int(params.get("pct", 100)), account=acct, timeout=timeout)
            if act == "suspend":
                return wm.suspend(cfg.ip, cfg.web_password, True, account=acct, timeout=timeout)
            if act == "resume":
                return wm.suspend(cfg.ip, cfg.web_password, False, account=acct, timeout=timeout)
            if act == "reboot":
                return wm.reboot(cfg.ip, cfg.web_password, account=acct, timeout=timeout)
            return False, f"Unsupported Whatsminer action: {act}"

        if fam == "goldshell":
            gs = nk_adapters.GoldshellAdapter()
            port = int(cfg.port or 80)
            if act == "workmode":
                return gs.set_workmode(cfg.ip, port, cfg.web_username, cfg.web_password, str(params.get("level", "")), timeout=timeout)
            if act == "reboot":
                return gs.reboot(cfg.ip, port, cfg.web_username, cfg.web_password, timeout=timeout)
            return False, f"Unsupported Goldshell action: {act}"

        if fam == "elphapex":
            el = nk_adapters.ElphapexAdapter()
            port = int(cfg.port or 80)
            if act == "workmode":
                return el.set_workmode(cfg.ip, port, cfg.web_username, cfg.web_password, str(params.get("mode", "")), timeout=timeout)
            if act == "reboot":
                return el.reboot(cfg.ip, port, cfg.web_username, cfg.web_password, timeout=timeout)
            return False, f"Unsupported Elphapex action: {act}"

        if fam == "iceriver":
            ir = nk_adapters.IceRiverAdapter()
            port = int(cfg.port or 80)
            if act == "settings":
                return ir.oc_apply_settings(cfg.ip, cfg.api_token, dict(params), timeout=timeout)
            if act == "reboot":
                return ir.reboot(cfg.ip, port, api_token=cfg.api_token, timeout=timeout)
            return False, f"Unsupported IceRiver action: {act}"

        return False, f"No opt-in adapter write handler for family '{fam or cfg.type}'"

    def rename_device(self, old_name: str, new_name: str) -> tuple[bool, str]:
        old_name = (old_name or "").strip()
        new_name = (new_name or "").strip()
        if not old_name or not new_name:
            return False, "Name cannot be empty"
        if any(ch in new_name for ch in ("/", "\\", "\0", "\n", "\r")):
            return False, "Name contains invalid characters"
        with self._lock:
            if old_name not in self.states:
                return False, f"Unknown device: {old_name}"
            if new_name == old_name:
                return True, ""
            if new_name in self.states:
                return False, f"A device named '{new_name}' already exists"
            state = self.states.pop(old_name)
            state.cfg.name = new_name
            self.states[new_name] = state
            self.history.rename(old_name, new_name)
            self.last_share.rename(old_name, new_name)
            self.safety_store.rename(old_name, new_name)
            hub_ref = self.hub or (getattr(self.shared, "hub", None) if self.shared is not None else None)
            if hub_ref is not None:
                hub_ref.on_rename(old_name, new_name)
            if self.shared is not None:
                self.shared.rename_device(old_name, new_name)
            if self.config.userdata_file is not None:
                self._persist_userdata()
            else:
                persist_device_rename(self.config.config_path, old_name, new_name)
            LOG.info("Renamed device '%s' -> '%s'", old_name, new_name)
            return True, ""

    def remove_device(self, name: str) -> tuple[bool, str]:
        target = (name or "").strip()
        if not target:
            return False, "Name cannot be empty"
        with self._lock:
            if target not in self.states:
                return False, f"Unknown device: {target}"
            self.states.pop(target, None)
            self.config.devices = [d for d in self.config.devices if d.name != target]
            self.config.device_order = [n for n in self.config.device_order if n != target]
            self.history.remove(target)
            self.last_share.remove(target)
            self.safety_store.remove(target)
            hub_ref = self.hub or (getattr(self.shared, "hub", None) if self.shared is not None else None)
            if hub_ref is not None:
                hub_ref.on_remove(target)
            if self.shared is not None:
                self.shared.remove_device(target)
            if self.config.userdata_file is not None:
                self._persist_userdata()
            if self.config.config_path is not None:
                persist_device_remove(self.config.config_path, target)
            LOG.info("Removed device '%s'", target)
            return True, ""

    def add_device(self, spec: dict[str, Any]) -> tuple[bool, str, Optional[dict[str, Any]]]:
        ip = str(spec.get("ip") or "").strip()
        dtype = str(spec.get("type") or "").strip().lower()
        name = str(spec.get("name") or "").strip()
        if not ip:
            return False, "IP address is required", None
        if dtype not in SUPPORTED_TYPES:
            return False, f"Unsupported type: {dtype}", None
        if not name:
            name = f"{dtype}-{ip.split('.')[-1]}"
        with self._lock:
            if any(d.ip == ip for d in self.config.devices):
                return False, f"A device with IP {ip} is already configured", None
            if name in self.states:
                return False, f"A device named '{name}' already exists", None
            defaults = dict(self.config.defaults or DEFAULTS)
            if self.config.devices:
                defaults.setdefault("poll_interval_sec", self.config.devices[0].poll_interval_sec)
                defaults.setdefault("cooldown_sec", self.config.devices[0].cooldown_sec)
                defaults.setdefault("timeout_sec", self.config.devices[0].timeout_sec)
            entry = dict(spec)
            entry["name"] = name
            entry["ip"] = ip
            entry["type"] = dtype
            # Newly added miners ALWAYS start with autotune OFF.
            entry["enabled"] = False
            try:
                dev = build_device_config(entry, defaults)
            except ValueError as exc:
                return False, str(exc), None
            apply_global_and_overrides(dev, self.config.global_tuner)
            dev.enabled_default = False
            self.config.devices.append(dev)
            state = DeviceState(cfg=dev)
            self.states[dev.name] = state
            if self.shared is not None:
                self.shared.add_device(dev.name, enabled=False, initial=self._device_payload_static(dev))
            persisted = device_config_to_dict(dev)
            persisted["enabled"] = False
            if self.config.userdata_file is not None:
                self._persist_userdata()
            else:
                persist_device_add(self.config.config_path, persisted)
            LOG.info("Added new device '%s' (%s) at %s with autotune OFF", dev.name, dev.type, dev.ip)
            return True, "", persisted

    def restore_devices(self, entries: Any, enabled_map: Optional[dict[str, Any]] = None) -> tuple[bool, str, int]:
        """Replace the device list from a settings backup (Miner Hearth v1.8).

        Secrets missing from a password-less backup (Braiins password, OTP) are
        kept from the currently configured device with the same name.
        """
        if not isinstance(entries, list):
            return False, "devices must be a list", 0
        enabled_map = enabled_map if isinstance(enabled_map, dict) else {}
        with self._lock:
            existing = {d.name: d for d in self.config.devices}
            defaults = dict(self.config.defaults or DEFAULTS)
            new_devs: list[DeviceConfig] = []
            seen: set[str] = set()
            for raw in entries:
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                name = str(item.get("name") or "").strip()
                if not name or name in seen:
                    continue
                if str(item.get("type") or "").strip().lower() not in SUPPORTED_TYPES:
                    return False, f"device {name!r}: unsupported type {item.get('type')!r}", 0
                old = existing.get(name)
                if old is not None:
                    for key in ("bos_password", "totp_secret", "otp_session", "web_password"):
                        if not item.get(key) and getattr(old, key, ""):
                            item[key] = getattr(old, key)
                try:
                    dev = build_device_config(item, defaults)
                except ValueError as exc:
                    return False, f"device {name!r}: {exc}", 0
                apply_global_and_overrides(dev, self.config.global_tuner)
                new_devs.append(dev)
                seen.add(name)
            self.config.devices = new_devs
            new_states: dict[str, DeviceState] = {}
            for d in new_devs:
                st = self.states.get(d.name)
                if st is not None and st.cfg.ip == d.ip:
                    st.cfg = d
                else:
                    st = DeviceState(cfg=d)
                new_states[d.name] = st
                self.history.load_device(d.name)
            self.states = new_states
            self.config.device_order = [d.name for d in new_devs]
            if self.shared is not None and hasattr(self.shared, "replace_devices"):
                en = {}
                for d in new_devs:
                    want = enabled_map.get(d.name)
                    if want is None:
                        want = bool(d.enabled_default) if d.enabled_default is not None else False
                    en[d.name] = bool(want) and is_tunable(d)
                self.shared.replace_devices(
                    [d.name for d in new_devs], en, {d.name: self._device_payload_static(d) for d in new_devs}
                )
            self._persist_userdata()
            LOG.info("Restored %d device(s) from backup", len(new_devs))
            return True, "", len(new_devs)

    def set_device_enabled(self, name: str, enabled: bool) -> tuple[bool, str]:
        with self._lock:
            state = self.states.get(name)
            if state is None:
                return False, f"unknown device: {name}"
            if enabled and not is_tunable(state.cfg):
                return False, f"{name} is monitor-only; autotune cannot be enabled"
            state.cfg.enabled_default = bool(enabled)
            if enabled:
                self.config.autotune_enabled = True
            if self.shared is not None:
                ok, err = self.shared.set_enabled(name, bool(enabled))
                if not ok:
                    return False, err
            if enabled:
                self.on_autotune_started(name)
            else:
                state.cool_polls = 0
                state.low_error_polls = 0
            self._persist_userdata()
            return True, ""

    def on_autotune_started(self, name: str) -> dict[str, Any]:
        """Reset tuner counters/cooldown baseline and sync from current device/manual values."""
        with self._lock:
            state = self.states.get(name)
            if state is None:
                return {}
            cfg = state.cfg
            cur_freq: Optional[float] = cfg.manual_frequency_mhz
            cur_volt: Optional[int] = cfg.manual_voltage_mv
            try:
                reading = fetch_reading(cfg)
                if reading.ok:
                    if reading.frequency is not None:
                        cur_freq = float(reading.frequency)
                    if reading.core_voltage is not None:
                        cur_volt = int(round(reading.core_voltage))
            except Exception:
                if self.shared is not None:
                    snap = (self.shared.status().get("devices") or [])
                    for row in snap:
                        if row.get("name") == name:
                            if cur_freq is None and row.get("frequency_mhz") is not None:
                                cur_freq = float(row["frequency_mhz"])
                            if cur_volt is None and row.get("core_voltage_mv") is not None:
                                cur_volt = int(round(float(row["core_voltage_mv"])))
                            break

            if cur_freq is not None:
                cfg.manual_frequency_mhz = cur_freq
            if cur_volt is not None:
                cfg.manual_voltage_mv = cur_volt

            state.cool_polls = 0
            state.low_error_polls = 0
            state.error_window.clear()
            state.last_error_pct = None
            state.last_rolling_error_pct = None
            state.last_change_ts = 0.0
            state.last_reason = (
                f"resumed autotune from {cur_freq:.0f} MHz"
                + (f" / {cur_volt} mV" if cur_volt is not None else "")
                if cur_freq is not None
                else "autotune resumed"
            )
            if self.shared is not None:
                patch: dict[str, Any] = {
                    "cool_polls": 0,
                    "low_error_polls": 0,
                    "manual_frequency_mhz": cfg.manual_frequency_mhz,
                    "manual_voltage_mv": cfg.manual_voltage_mv,
                    "last_reason": state.last_reason,
                }
                if cur_freq is not None:
                    patch["frequency_mhz"] = cur_freq
                if cur_volt is not None:
                    patch["core_voltage_mv"] = cur_volt
                self.shared.update_device(name, patch)
            LOG.info("%s: %s", name, state.last_reason)
            return {"frequency_mhz": cur_freq, "core_voltage_mv": cur_volt}

    def apply_manual_settings(
        self,
        name: str,
        frequency_mhz: Any = None,
        core_voltage_mv: Any = None,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Validate & apply manual frequency and/or core voltage while autotune is OFF."""
        with self._lock:
            state = self.states.get(name)
            if state is None:
                return False, f"unknown device: {name}", {}
            cfg = state.cfg
            if not is_tunable(cfg):
                return False, f"{name} is monitor-only (Antminer); manual frequency/voltage is not supported", {}
            if self._enabled(name):
                return False, f"Stop autotune on {name} before applying manual frequency or voltage", {}

            freq_to_apply: Optional[float] = None
            if frequency_mhz is not None and str(frequency_mhz).strip() != "":
                try:
                    freq_val = float(frequency_mhz)
                except (TypeError, ValueError):
                    return False, "Frequency must be a number (MHz)", {}
                freq_cap = min(HARD_MAX_FREQ_MHZ, float(cfg.max_freq))
                if freq_val < float(cfg.min_freq) or freq_val > freq_cap:
                    return (
                        False,
                        f"Frequency must be between {cfg.min_freq:.0f} and {freq_cap:.0f} MHz",
                        {},
                    )
                freq_to_apply = round(freq_val, 2)

            volt_to_apply: Optional[int] = None
            warnings: list[str] = []
            if core_voltage_mv is not None and str(core_voltage_mv).strip() != "":
                try:
                    volt_val = int(round(float(core_voltage_mv)))
                except (TypeError, ValueError):
                    return False, "Core voltage must be a number (mV)", {}
                floor_mv = min(int(cfg.min_voltage_mv), HARD_MAX_VOLTAGE_MV)
                cap_mv = min(HARD_MAX_VOLTAGE_MV, int(cfg.max_voltage_mv))
                if volt_val < floor_mv:
                    return (
                        False,
                        f"Core voltage must be at least {floor_mv} mV",
                        {},
                    )
                if cfg.type == "gamma" and volt_val > cap_mv:
                    return (
                        False,
                        f"Core voltage cannot exceed {cap_mv} mV hard cap",
                        {},
                    )
                if volt_val > cap_mv:
                    warnings.append(f"clamped voltage {volt_val} mV to {cap_mv} mV hard cap")
                    volt_val = cap_mv
                volt_to_apply = volt_val

            if freq_to_apply is None and volt_to_apply is None:
                return False, "Provide frequency and/or core voltage to apply", {}

            dry = self._dry_run()
            applied_parts: list[str] = []
            if freq_to_apply is not None:
                try:
                    res_f = apply_frequency(cfg, freq_to_apply, dry)
                except (DeviceError, DeviceOffline) as exc:
                    return False, str(exc), {}
                cfg.manual_frequency_mhz = freq_to_apply
                state.tou_pre_cap_freq = None
                state.tou_applied_freq = None
                applied_parts.append(f"freq={freq_to_apply:.0f} MHz ({res_f})")

            final_volt: Optional[int] = None
            if volt_to_apply is not None:
                try:
                    res_v, clamped_mv, v_warn = apply_manual_voltage(cfg, volt_to_apply, dry)
                except (DeviceError, DeviceOffline) as exc:
                    return False, str(exc), {}
                final_volt = clamped_mv
                cfg.manual_voltage_mv = clamped_mv
                if v_warn and v_warn not in warnings:
                    warnings.append(v_warn)
                applied_parts.append(f"voltage={clamped_mv} mV ({res_v})")

            state.cool_polls = 0
            state.low_error_polls = 0
            state.last_change_ts = 0.0
            state.clear_error_window()
            state.last_action = "manual"
            state.last_action_at = iso_now()
            state.last_reason = "manual apply: " + "; ".join(applied_parts)
            self._persist_userdata()
            if self.hub is not None:
                prev = self.shared.snapshots.get(name, {}) if self.shared is not None else {}
                if freq_to_apply is not None:
                    self.hub.log_tuning(name, "frequency", prev.get("frequency_mhz"), freq_to_apply, "manual apply from dashboard", "manual", source="manual", dry_run=dry)
                if final_volt is not None:
                    self.hub.log_tuning(name, "voltage", prev.get("core_voltage_mv"), final_volt, "manual apply from dashboard", "manual", source="manual", dry_run=dry)

            if self.shared is not None:
                patch: dict[str, Any] = {
                    "manual_frequency_mhz": cfg.manual_frequency_mhz,
                    "manual_voltage_mv": cfg.manual_voltage_mv,
                    "last_action": state.last_action,
                    "last_action_at": state.last_action_at,
                    "last_reason": state.last_reason,
                    "cool_polls": 0,
                    "low_error_polls": 0,
                }
                if freq_to_apply is not None:
                    patch["frequency_mhz"] = freq_to_apply
                if final_volt is not None:
                    patch["core_voltage_mv"] = final_volt
                self.shared.update_device(name, patch)

            warn_str = "; ".join(w for w in warnings if w)
            LOG.info("%s: %s%s", name, state.last_reason, f" [WARN: {warn_str}]" if warn_str else "")
            return (
                True,
                warn_str,
                {
                    "frequency_mhz": cfg.manual_frequency_mhz,
                    "core_voltage_mv": cfg.manual_voltage_mv,
                    "warning": warn_str,
                    "dry_run": dry,
                },
            )

    def apply_pool_change(self, name: str, spec: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        with self._lock:
            state = self.states.get(name)
            if state is None:
                return False, f"unknown device: {name}", {}
            cfg = state.cfg

        try:
            spec = self._resolve_saved_pools(name, cfg, spec)
        except ValueError as exc:
            return False, str(exc), {}
        url = str(spec.get("stratum_url") or spec.get("pool_url") or "").strip()
        user = str(spec.get("stratum_user") or spec.get("pool_user") or "").strip()
        pwd = str(spec.get("stratum_password") or "")
        raw_port = spec.get("stratum_port") if spec.get("stratum_port") not in (None, "") else spec.get("pool_port")
        try:
            port = int(raw_port if raw_port not in (None, "") else 3333)
        except (TypeError, ValueError):
            return False, "Primary stratum port must be a valid integer (1-65535)", {}

        fb_url = str(spec.get("fallback_url") or spec.get("fallback_pool_url") or "").strip()
        fb_user = str(spec.get("fallback_user") or spec.get("fallback_pool_user") or "").strip()
        fb_pwd = str(spec.get("fallback_password") or "")
        raw_fb_port = spec.get("fallback_port") if spec.get("fallback_port") not in (None, "") else spec.get("fallback_pool_port")
        fb_port: Optional[int] = None
        if fb_url:
            try:
                fb_port = int(raw_fb_port if raw_fb_port not in (None, "") else port)
            except (TypeError, ValueError):
                return False, "Fallback stratum port must be a valid integer (1-65535)", {}

        restart = bool(spec.get("restart", True))
        if not self._dry_run():
            self._note_transition(name, "nk_pool_change", "pool settings saved from the Miner Hearth dashboard" + (" (restarting)" if restart else ""))
        try:
            res = apply_pool_settings(
                cfg,
                stratum_url=url,
                stratum_port=port,
                stratum_user=user,
                stratum_password=pwd,
                fallback_url=fb_url,
                fallback_port=fb_port,
                fallback_user=fb_user,
                fallback_password=fb_pwd,
                restart=restart,
                dry_run=self._dry_run(),
            )
        except (DeviceError, DeviceOffline, ValueError) as exc:
            return False, str(exc), {}

        with self._lock:
            state.last_action = "pool_update"
            state.last_action_at = iso_now()
            state.last_reason = res["result"]
            if self.shared is not None:
                self.shared.update_device(
                    name,
                    {
                        "pool_url": url,
                        "pool_port": port,
                        "pool_user": user,
                        "fallback_pool_url": fb_url,
                        "fallback_pool_port": fb_port,
                        "fallback_pool_user": fb_user if fb_url else "",
                        "last_action": state.last_action,
                        "last_action_at": state.last_action_at,
                        "last_reason": state.last_reason,
                    },
                )
        LOG.info("%s: %s", name, res["result"])
        return True, "", res

    def _resolve_saved_pools(self, name: str, cfg: Any, spec: dict[str, Any]) -> dict[str, Any]:
        """v1.16.2: fill a pool change from the saved-pools library.

        spec["saved_pool_id"] / spec["fallback_saved_pool_id"]: a saved pool picked in the pool editor. Empty fields
        are filled from it; the saved password is used when the password field is blank and the address / port
        still match the saved pool (so an edited address never receives another pool's password).
        spec["use_machine_worker"]: replace the worker part of the username with this machine's name."""
        hub = self.hub or (getattr(self.shared, "hub", None) if self.shared is not None else None)
        lib = getattr(hub, "pools", None)
        pid = str(spec.get("saved_pool_id") or "").strip()
        fid = str(spec.get("fallback_saved_pool_id") or "").strip()
        if not pid and not fid:
            return spec
        if lib is None:
            raise ValueError("Saved pools are not available")
        import nk_pools  # local: autotune stays importable on its own
        out = dict(spec)
        mw = bool(spec.get("use_machine_worker"))
        for sid, k_url, k_port, k_user, k_pw, label in ((pid, "stratum_url", "stratum_port", "stratum_user", "stratum_password", "primary"),
                                                         (fid, "fallback_url", "fallback_port", "fallback_user", "fallback_password", "fallback")):
            if not sid:
                continue
            if label == "fallback" and cfg.type == "antminer":
                raise ValueError("This firmware's pool API only sets the active pool; set the fallback pool in the miner's web page")
            try:
                r = lib.resolve(sid, name, mw)
            except nk_pools.PoolError as exc:
                raise ValueError(f"{label} pool: {exc}") from None
            given_url = str(out.get(k_url) or "").strip()
            if not given_url:
                out[k_url], out[k_port] = r["url"], r["port"]
            if not str(out.get(k_user) or "").strip():
                out[k_user] = r["user"]
            elif mw:
                out[k_user] = nk_pools.apply_worker(str(out[k_user]), name, True)
            same = (nk_pools.pool_match(str(out.get(k_url) or ""), out.get(k_port), r["url"], r["port"]))
            if not str(out.get(k_pw) or "") and same:
                out[k_pw] = r["password"]
        return out

    def _publish(self, state: DeviceState, reading: Reading, decision: Decision) -> None:
        cfg = state.cfg
        with self._lock:
            if self.states.get(cfg.name) is not state:
                return
        state.last_action = decision.action
        state.last_reason = decision.reason
        state.last_voltage_reason = decision.voltage_reason or state.last_voltage_reason
        state.last_action_at = iso_now()
        state.last_poll_at = state.last_action_at
        if self.shared is None:
            return
        hottest = None
        if reading.asic_temps:
            hottest = max(reading.asic_temps)
        elif reading.asic_temp is not None:
            hottest = reading.asic_temp
        self.shared.update_device(
            cfg.name,
            {
                "name": cfg.name,
                "ip": cfg.ip,
                "type": cfg.type,
                "family": reading.family or cfg.family,
                "algorithm": cfg.algorithm or reading.algorithm or "sha256d",
                "coin": cfg.coin,
                "merged_coin": cfg.merged_coin,
                "write_enabled": bool(cfg.write_enabled or (cfg.type == "antminer" and cfg.bos_power_mgmt)),
                "api_has_token": bool(cfg.api_token),
                "adapter_config": dict(cfg.adapter_config),
                "capabilities": dict(reading.capabilities),
                "hashrate_hs": reading.hashrate_hs,
                "hashrate_unit_kind": reading.hashrate_unit_kind or "hash",
                "tunable": is_tunable(cfg),
                "model": reading.model,
                "version": reading.version,
                "online": bool(reading.ok or reading.reachable),
                "unparsed": bool(reading.reachable and not reading.ok),
                "asic_temp_c": reading.asic_temp,
                "asic_temps": list(reading.asic_temps),
                "hottest_asic_temp_c": hottest,
                "vr_temp_c": reading.vr_temp,
                "ambient_temp_c": reading.ambient_temp,
                "ambient_source": reading.ambient_source,
                "board_temps": list(reading.board_temps),
                "chip_temps": list(reading.chip_temps),
                "fan_rpms": list(reading.fan_rpms),
                "uptime_sec": reading.uptime_sec,
                "pool_url": reading.pool_url,
                "pool_port": reading.pool_port,
                "pool_user": reading.pool_user,
                "fallback_pool_url": reading.fallback_pool_url,
                "fallback_pool_port": reading.fallback_pool_port,
                "fallback_pool_user": reading.fallback_pool_user,
                "manual_frequency_mhz": cfg.manual_frequency_mhz,
                "manual_voltage_mv": cfg.manual_voltage_mv,
                "firmware": reading.firmware,
                "firmware_note": "Braiins OS+ power-target control active" if (cfg.type == "antminer" and cfg.bos_power_mgmt) else reading.firmware_note,
                "asic_target_c": cfg.asic_target_c,
                "asic_max_c": cfg.asic_max_c,
                "vr_target_c": cfg.vr_target_c,
                "vr_max_c": cfg.vr_max_c,
                "frequency_mhz": reading.frequency,
                "actual_frequency_mhz": reading.actual_frequency,
                "core_voltage_mv": reading.core_voltage,
                "core_voltage_actual_mv": reading.core_voltage_actual,
                "hashrate_ghs": reading.hashrate,
                "hashrate_1m_ghs": reading.hashrate_1m,
                "hashrate_5m_ghs": reading.hashrate_5m,
                "hashrate_10m_ghs": reading.hashrate_10m,
                "hashrate_15m_ghs": reading.hashrate_15m,
                "hashrate_1h_ghs": reading.hashrate_1h,
                "hashrate_24h_ghs": reading.hashrate_24h,
                "hashrate_avg_ghs": reading.hashrate_avg,
                "expected_hashrate_ghs": reading.expected_hashrate,
                "power_w": reading.power,
                "power_limit_w": reading.power_limit_w,
                "j_per_th": compute_j_per_th(reading.power, reading.hashrate),
                "input_voltage": reading.input_voltage,
                "input_current": reading.input_current,
                "fans": list(reading.fans),
                "fault_raw": dict(reading.fault_raw),
                "raw_fields": dict(reading.raw_fields),
                "using_fallback_stratum": bool(reading.using_fallback_stratum),
                "error_pct": state.last_error_pct,
                "rolling_error_pct": state.last_rolling_error_pct,
                "error_window_count": len(state.error_window),
                "error_window_size": cfg.error_window_polls,
                "error_source": state.last_error_source,
                "learned_min_voltage_mv": state.learned_min_voltage_mv,
                "bad_voltage_by_freq": dict(state.bad_voltage_by_freq),
                "error_high_pct": cfg.error_high_pct,
                "error_low_pct": cfg.error_low_pct,
                "shares_accepted": reading.shares_accepted,
                "shares_rejected": reading.shares_rejected,
                "shares_stale": reading.shares_stale,
                "hw_errors": reading.hw_errors,
                "pool_rejected_pct": reading.pool_rejected_pct,
                "rejected_reasons": list(reading.rejected_reasons),
                "best_diff": reading.best_diff,
                "best_diff_display": reading.best_diff_display,
                "best_session_diff": reading.best_session_diff,
                "best_session_diff_display": reading.best_session_diff_display,
                "pool_difficulty": reading.pool_difficulty,
                "last_submitted_diff": reading.last_submitted_diff,
                "last_share_seconds": reading.last_share_seconds,
                "last_share_estimated": bool(reading.last_share_estimated),
                "last_share_waiting": bool(reading.last_share_waiting),
                "ping_rtt_ms": reading.ping_rtt_ms,
                "ping_loss": reading.ping_loss,
                "wifi_rssi": reading.wifi_rssi,
                "wifi_quality": wifi_quality_label(reading.wifi_rssi),
                "stratum_disconnects": reading.stratum_disconnects,
                "wifi_disconnects": reading.wifi_disconnects,
                "asic_domains": list(reading.asic_domains),
                "block_height": reading.block_height,
                "network_difficulty": reading.network_difficulty,
                "tuner_mode": reading.tuner_mode,
                "tuner_chain_statuses": list(reading.tuner_chain_statuses),
                "boards": list(reading.boards),
                "pools": list(reading.pools),
                "history": self.history.get_series(cfg.name),
                "voltage_control": cfg.voltage_control,
                "vr_voltage_relief": cfg.vr_voltage_relief,
                "voltage_start_mv": cfg.voltage_start_mv,
                "emergency_voltage_mv": cfg.emergency_voltage_mv,
                "min_voltage_mv": cfg.min_voltage_mv,
                "max_voltage_mv": cfg.max_voltage_mv,
                "error_target_pct": cfg.error_target_pct,
                "error_low_pct": cfg.error_low_pct,
                "error_high_pct": cfg.error_high_pct,
                "emergency_step_mhz": cfg.emergency_step_mhz,
                "asic_target_c": cfg.asic_target_c,
                "asic_max_c": cfg.asic_max_c,
                "vr_target_c": cfg.vr_target_c,
                "vr_max_c": cfg.vr_max_c,
                "overrides": dict(cfg.overrides),
                "bos_power_mgmt": bool(cfg.bos_power_mgmt),
                "bos_grpc_port": int(cfg.bos_grpc_port),
                "bos_username": cfg.bos_username,
                "bos_has_password": bool(cfg.bos_password),
                "web_username": cfg.web_username,
                "web_has_password": bool(cfg.web_password),
                "bos_min_watts": int(cfg.bos_min_watts),
                "bos_max_watts": int(cfg.bos_max_watts),
                "bos_step_watts": int(cfg.bos_step_watts),
                "bos_chip_target_c": float(cfg.bos_chip_target_c),
                "bos_chip_hot_c": float(cfg.bos_chip_hot_c),
                "bos_cooldown_sec": float(cfg.bos_cooldown_sec),
                "bos_cool_polls_required": int(cfg.bos_cool_polls_required),
                "bos_last_action": state.bos_last_action,
                "bos_last_reason": state.bos_last_reason,
                "bos_last_action_at": state.bos_last_action_at,
                "voltage_reason": decision.voltage_reason or state.last_voltage_reason,
                "last_action": decision.action,
                "last_action_at": state.last_action_at,
                "last_reason": decision.reason,
                "last_poll_at": state.last_poll_at,
                "error": "" if reading.ok else (reading.error or decision.reason),
                "cool_polls": state.cool_polls,
                "low_error_polls": state.low_error_polls,
                "min_freq": cfg.min_freq,
                "max_freq": cfg.max_freq,
                "overclock_opt_in": bool(cfg.overclock_opt_in),
                "model_normal_max_freq": NERD_NORMAL_MAX_FREQ_MHZ if cfg.type == "nerdoctaxe" else HARD_MAX_FREQ_MHZ,
                "last_stable_freq": state.last_stable_freq,
                "rollback_freq": state.rollback_freq,
                "emergency_lockout_remaining_sec": max(0.0, state.emergency_lockout_until - self.clock()) if state.emergency_lockout_until > self.clock() else 0.0,
                "emergency_reason": state.emergency_reason,
                "emergency_at_iso": state.emergency_at_iso,
                "sensor_suspect": bool(state.sensor_suspect),
                "sensor_suspect_reason": state.sensor_suspect_reason,
            },
        )

    def _learned_min_voltage_mv(self, cfg: DeviceConfig, state: DeviceState, freq: Optional[float]) -> Optional[int]:
        if not cfg.voltage_control or freq is None or freq <= 0 or not state.bad_voltage_by_freq:
            return None
        bad_vals: list[int] = []
        for f_key, v_val in state.bad_voltage_by_freq.items():
            try:
                if float(f_key) <= float(freq) + 1.0:
                    bad_vals.append(int(v_val))
            except (TypeError, ValueError):
                continue
        if not bad_vals:
            return None
        margin = max(10, int(cfg.voltage_step_mv))
        floor_mv = min(int(cfg.max_voltage_mv), max(bad_vals) + margin)
        return floor_mv if floor_mv > int(cfg.min_voltage_mv) else None

    def _record_bad_voltage(
        self,
        cfg: DeviceConfig,
        state: DeviceState,
        reading: Reading,
        rolling_err: Optional[float],
        latest_err: Optional[float],
        source: str,
    ) -> None:
        if not cfg.voltage_control or not reading.ok or source not in {"", "errorPercentage"}:
            return
        if reading.frequency is None or reading.frequency <= 0:
            return
        if reading.hashrate is not None and reading.hashrate <= 0:
            return
        if reading.uptime_sec is not None and reading.uptime_sec < 30.0:
            return
        unpowered = (
            (reading.core_voltage_actual is not None and reading.core_voltage_actual <= 0 and (reading.core_voltage or 0) > 0)
            or (reading.power is not None and reading.power <= 2.0)
        )
        if unpowered:
            return
        eff = max(rolling_err, latest_err) if (rolling_err is not None and latest_err is not None) else (latest_err if latest_err is not None else rolling_err)
        if eff is None or eff <= cfg.error_high_pct:
            return
        cur_v = _effective_core_voltage_mv(reading, cfg)
        f_key = str(int(round(float(reading.frequency))))
        prev = state.bad_voltage_by_freq.get(f_key)
        if prev is None or cur_v > prev:
            state.bad_voltage_by_freq[f_key] = int(cur_v)
            self.safety_store.update(cfg.name, {"bad_voltage_by_freq": dict(state.bad_voltage_by_freq)})

    def _lockout_duration_sec(self) -> float:
        if self.hub is not None and hasattr(self.hub, "settings"):
            try:
                mins = float((self.hub.settings.get().get("tuner") or {}).get("emergency_lockout_min", 180))
                return max(0.0, mins * 60.0)
            except Exception:
                pass
        return float(self.emergency_lockout_sec)

    def _update_stability_and_lockout(
        self,
        cfg: DeviceConfig,
        state: DeviceState,
        reading: Reading,
        decision: Decision,
        rolling_err: Optional[float],
        now: float,
        lockout_sec: float,
    ) -> None:
        freq = reading.frequency
        unpowered = (
            (reading.core_voltage_actual is not None and reading.core_voltage_actual <= 0 and (reading.core_voltage or 0) > 0)
            or (reading.power is not None and reading.power <= 2.0)
        )
        # Track healthy stable frequency
        healthy = (
            reading.ok
            and not unpowered
            and (reading.hashrate is not None and reading.hashrate > 0)
            and reading.asic_temp is not None and reading.asic_temp < cfg.asic_target_c
            and reading.vr_temp is not None and reading.vr_temp < cfg.vr_target_c
            and (rolling_err is None or rolling_err <= cfg.error_high_pct)
            and decision.action in {"hold", "step_up", "voltage_down"}
        )
        if healthy and freq is not None and freq > 0:
            if state.stable_candidate_freq is None or abs(freq - state.stable_candidate_freq) > 1e-3:
                state.stable_candidate_freq = float(freq)
                state.stable_since_ts = now
            elif (now - state.stable_since_ts) >= 600.0 and state.last_stable_freq != float(freq):
                state.last_stable_freq = float(freq)
                self.safety_store.update(cfg.name, {"last_stable_freq": state.last_stable_freq})
            if decision.action == "step_up" and state.last_stable_freq is None:
                state.last_stable_freq = float(freq)
                self.safety_store.update(cfg.name, {"last_stable_freq": state.last_stable_freq})
        elif decision.action not in {"hold", "paused", "step_up"}:
            state.stable_candidate_freq = None
            state.stable_since_ts = 0.0

        # Trigger persisted post-emergency lockout + rollback on emergency_down or overheat/rail-off after overheat
        emerg_trigger = decision.action == "emergency_down" or (
            reading.vr_temp is not None and reading.vr_temp >= cfg.vr_max_c
        ) or (
            reading.asic_temp is not None and reading.asic_temp >= cfg.asic_max_c
        )
        if emerg_trigger and lockout_sec > 0:
            rb = state.last_stable_freq
            if rb is None and freq is not None and freq > cfg.min_freq:
                rb = max(cfg.min_freq, freq - cfg.emergency_step_mhz)
            if rb is not None and decision.action == "emergency_down" and decision.new_freq is not None and rb < decision.new_freq:
                rb = max(cfg.min_freq, min(float(cfg.max_freq), rb))
                decision.new_freq = rb
                decision.reason += f" (rolled back to last stable {rb:.0f} MHz)"
            state.rollback_freq = rb
            state.emergency_lockout_until = now + lockout_sec
            state.emergency_reason = decision.reason[:240]
            state.emergency_at_iso = iso_now()
            self.safety_store.update(
                cfg.name,
                {
                    "last_stable_freq": state.last_stable_freq,
                    "rollback_freq": state.rollback_freq,
                    "emergency_lockout_until": state.emergency_lockout_until,
                    "emergency_reason": state.emergency_reason,
                    "emergency_at_iso": state.emergency_at_iso,
                },
            )

    def poll_device(self, state: DeviceState) -> Decision:
        with self._lock:
            if self.states.get(state.cfg.name) is not state:
                return Decision("removed", None, "device removed", 0)
        state._nk_reading = None  # type: ignore[attr-defined]
        decision = self._poll_device_inner(state)
        with self._lock:
            if self.states.get(state.cfg.name) is not state:
                return decision
        reading = getattr(state, "_nk_reading", None)
        if self.hub is not None and reading is not None:
            self.hub.on_poll(state.cfg, reading, self.clock(), state=state)
        return decision

    @staticmethod
    def _reading_aliases(reading: Optional[Reading]) -> Optional[list[str]]:
        if reading is None:
            return None
        aliases: list[str] = []
        if isinstance(reading.hostname, str) and reading.hostname.strip():
            aliases.append(reading.hostname.strip())
        for src in (reading.raw_fields, reading.fault_raw):
            if isinstance(src, dict):
                for key in ("info.hostname", "system.hostname", "dashboard.hostname", "hostname"):
                    val = src.get(key)
                    if isinstance(val, str) and val.strip() and val.strip() not in aliases:
                        aliases.append(val.strip())
        return aliases or None

    def _tou_limits(self, cfg: DeviceConfig, reading: Optional[Reading] = None) -> Optional[dict[str, Any]]:
        if self.hub is None:
            return None
        try:
            aliases = list(self._reading_aliases(reading) or [])
            if cfg.ip and cfg.ip not in aliases:
                aliases.append(cfg.ip)
            try:
                return self.hub.schedule_limits(cfg.name, self.clock(), aliases=aliases)
            except TypeError:
                return self.hub.schedule_limits(cfg.name, self.clock())
        except Exception:
            LOG.exception("%s: TOU schedule lookup failed", cfg.name)
            return None

    def _schedule_limits(self, cfg: DeviceConfig, reading: Optional[Reading] = None) -> Optional[dict[str, Any]]:
        if self.hub is None:
            return None
        sched = self._tou_limits(cfg, reading=reading)
        try:
            quiet = self.hub.quiet_limits(cfg.name, self.clock()) if hasattr(self.hub, "quiet_limits") else None
        except Exception:
            LOG.exception("%s: quiet schedule lookup failed", cfg.name)
            quiet = None
        if quiet and quiet.get("max_freq"):
            sched = dict(sched or {"max_freq": None, "asic_target_c": None, "bos_power_target_w": None, "windows": []})
            sched["windows"] = list(sched.get("windows") or []) + [f"quiet: {w}" for w in quiet.get("windows") or []]
            sched["max_freq"] = quiet["max_freq"] if sched.get("max_freq") is None else min(sched["max_freq"], quiet["max_freq"])
        return sched

    def _resolve_capped_freq(self, cfg: DeviceConfig, target_limit: float) -> float:
        limit = max(float(cfg.min_freq), min(float(cfg.max_freq), float(target_limit)))
        if cfg.frequency_options:
            opts = sorted({float(f) for f in cfg.frequency_options if cfg.min_freq - 1e-6 <= float(f) <= limit + 1e-6})
            if opts:
                return opts[-1]
        return limit

    def _enforce_tou_when_paused(
        self,
        cfg: DeviceConfig,
        state: DeviceState,
        reading: Reading,
        decision: Decision,
        tou_sched: Optional[dict[str, Any]],
        active_faults: list[dict[str, Any]],
        now: float,
    ) -> Optional[Decision]:
        """Enforce TOU caps (and restore pre-peak clock on end) even when autotune is OFF."""
        if not is_tunable(cfg) or not reading.ok or reading.frequency is None:
            return None
        if decision.action in {"offline", "invalid", "unparsed", "monitor"}:
            return None
        freq = float(reading.frequency)
        dry = self._dry_run()

        if tou_sched:
            names = ", ".join(tou_sched.get("windows") or [])
            # 1) Explicit AxeOS frequency cap takes immediate effect (bypasses cooldown).
            if tou_sched.get("max_freq") is not None:
                cap_f = self._resolve_capped_freq(cfg, float(tou_sched["max_freq"]))
                if freq > cap_f + 1e-6:
                    if state.tou_pre_cap_freq is None:
                        state.tou_pre_cap_freq = freq
                    reason = f"time-of-use window '{names}': frequency cap {float(tou_sched['max_freq']):.0f} MHz; {freq:.0f} -> {cap_f:.0f} MHz"
                    try:
                        res = apply_frequency(cfg, cap_f, dry)
                    except (DeviceError, DeviceOffline) as exc:
                        LOG.error("%s: TOU cap failed to apply frequency: %s", cfg.name, exc)
                        return None
                    state.tou_applied_freq = cap_f
                    state.last_change_ts = now
                    state.last_change_action = "step_down"
                    if self.hub is not None:
                        self.hub.log_tuning(cfg.name, "frequency", freq, cap_f, reason, "step_down", source="schedule", dry_run=dry)
                    prefix = "DRY-RUN " if dry else ""
                    LOG.warning("%s: %s%s (%s)", cfg.name, prefix, reason, res)
                    return Decision(
                        "step_down", cap_f, reason, 0,
                        bypass_cooldown=True, low_error_polls=0,
                        voltage_reason="autotune paused; TOU cap active",
                    )

            return None

        # Window ended while autotune is OFF: restore pre-peak clock if we capped it
        # and the user hasn't manually changed frequency since.
        if state.tou_pre_cap_freq is None:
            return None
        if not dry and state.tou_applied_freq is not None and abs(freq - float(state.tou_applied_freq)) > 2.0:
            state.tou_pre_cap_freq = None
            state.tou_applied_freq = None
            return None
        restore_f = self._resolve_capped_freq(cfg, float(state.tou_pre_cap_freq))
        if restore_f <= freq + 1e-6:
            state.tou_pre_cap_freq = None
            state.tou_applied_freq = None
            return None
        # Safety guards: never restore upward into thermal warning/emergency, sensor suspect, or active faults.
        if (
            state.sensor_suspect
            or active_faults
            or decision.action == "emergency_down"
            or (reading.asic_temp is not None and float(reading.asic_temp) > float(cfg.asic_target_c))
            or (reading.vr_temp is not None and float(reading.vr_temp) > float(cfg.vr_target_c))
        ):
            return None
        reason = f"time-of-use window ended: restoring pre-peak frequency {freq:.0f} -> {restore_f:.0f} MHz"
        try:
            res = apply_frequency(cfg, restore_f, dry)
        except (DeviceError, DeviceOffline) as exc:
            LOG.error("%s: TOU restore failed: %s", cfg.name, exc)
            return None
        state.tou_pre_cap_freq = None
        state.tou_applied_freq = None
        state.last_change_ts = now
        state.last_change_action = "step_up"
        if self.hub is not None:
            self.hub.log_tuning(cfg.name, "frequency", freq, restore_f, reason, "step_up", source="schedule", dry_run=dry)
        prefix = "DRY-RUN " if dry else ""
        LOG.warning("%s: %s%s (%s)", cfg.name, prefix, reason, res)
        return Decision(
            "step_up", restore_f, reason, 0,
            bypass_cooldown=True, low_error_polls=0,
            voltage_reason="autotune paused; restored pre-TOU frequency",
        )

    def _quiet_mode(self, cfg: DeviceConfig, state: DeviceState, reading: Reading, decision: Decision) -> None:
        """Scheduled fan cap. Emergency temperatures (or autotune off) always hand the fan back."""
        if self.hub is None or not hasattr(self.hub, "quiet_limits") or not is_tunable(cfg):
            return
        now = self.clock()
        try:
            wanted = self.hub.quiet_limits(cfg.name, now) if self._enabled(cfg.name) else None
        except Exception:
            wanted = None
        hot = bool(
            decision.action == "emergency_down"
            or (reading.asic_temp is not None and reading.asic_temp >= cfg.asic_max_c)
            or (reading.vr_temp is not None and reading.vr_temp >= cfg.vr_max_c)
        )
        import nk_extras
        action, pct, reason = nk_extras.decide_quiet(state.quiet_fan_pct, wanted, hot, now, state.quiet_suspended_until)
        if hot and state.quiet_fan_pct is not None:
            state.quiet_suspended_until = now + 900
        if action == "none":
            return
        try:
            res = apply_fan(cfg, pct, self._dry_run())
        except (DeviceError, DeviceOffline) as exc:
            LOG.error("%s: quiet mode fan change failed: %s", cfg.name, exc)
            return
        old = state.quiet_fan_pct
        state.quiet_fan_pct = pct if action == "cap" else None
        LOG.warning("%s: %s (%s)", cfg.name, reason, res)
        self.hub.log_tuning(cfg.name, "fan", old if old is not None else "auto", pct if pct is not None else "auto", reason,
                            action, source="emergency" if hot else "quiet", dry_run=self._dry_run())

    def release_fan_caps(self) -> int:
        """Give every quiet-mode-capped fan back to automatic control (app shutdown)."""
        n = 0
        with self._lock:
            states = list(self.states.values())
        for st in states:
            if st.quiet_fan_pct is not None:
                try:
                    apply_fan(st.cfg, None, self._dry_run())
                    st.quiet_fan_pct = None
                    n += 1
                except Exception as exc:  # noqa: BLE001
                    LOG.error("%s: could not restore automatic fan: %s", st.cfg.name, exc)
        return n

    def _note_transition(self, name: str, kind: str, reason: str) -> None:
        """v1.15.5: tell the feature hub a restart / reconfiguration is intentional (opens the maintenance grace)."""
        hub = self.hub or (getattr(self.shared, "hub", None) if self.shared is not None else None)
        if hub is not None and hasattr(hub, "expect_transition"):
            try:
                hub.expect_transition(name, kind, reason)
            except Exception:
                LOG.exception("%s: could not note expected transition", name)

    def maintenance_restart(self, name: str) -> str:
        """Restart a miner for the v1.9 scheduled / hung-miner restarts (respects dry-run)."""
        with self._lock:
            cfg = next((d for d in self.config.devices if d.name == name), None)
        if cfg is None:
            raise DeviceError(f"unknown device {name}")
        dry = self._dry_run()
        if cfg.type == "antminer":
            st = self.states.get(name)
            rd = getattr(st, "_nk_reading", None) if st is not None else None
            fw = f"{getattr(rd, 'firmware', '')} {getattr(rd, 'firmware_note', '')}".lower()
            if not (cfg.bos_password or "braiins" in fw or cfg.bos_power_mgmt):
                raise DeviceError("restart is only supported on Braiins OS+ Antminers (set the Braiins login on the card)")
            if dry:
                return "dry-run: would restart bosminer via Braiins API"
            import braiins_api
            res = braiins_api.restart_miner(cfg.ip, username=cfg.bos_username, password=cfg.bos_password,
                                            grpc_port=cfg.bos_grpc_port, http_port=cfg.port, timeout=cfg.timeout_sec)
            return f"Braiins restart via {res.get('transport')}"
        return restart_device(cfg, dry)

    # --- v1.10: fault Clear/Reset, log snapshot, Bitmain web login -----------------
    def _cfg(self, name: str) -> DeviceConfig:
        with self._lock:
            cfg = next((d for d in self.config.devices if d.name == name), None)
        if cfg is None:
            raise DeviceError(f"unknown device {name}")
        return cfg

    def fault_action(self, name: str, action: str) -> str:
        """Run one Clear/Reset action chosen by nk_faults.reset_plan (respects dry-run). Never factory-resets."""
        if action not in nk_faults.ALLOWED_ACTIONS:
            raise DeviceError(f"action {action!r} is not allowed")
        cfg = self._cfg(name)
        dry = self._dry_run()
        what = nk_faults.ACTION_WHAT[action]
        if dry:
            return f"dry-run: would {what}"
        timeout = min(cfg.timeout_sec, 8.0)
        if action in {"axe_clear_overheat", "axe_resume", "restart"}:
            if cfg.type == "antminer":
                raise DeviceError("AxeOS actions are not valid for an Antminer")
            if action == "restart":
                return restart_device(cfg, False)
            base = _base_url(cfg)
            headers = _auth_headers(cfg)
            if action == "axe_clear_overheat":
                status, payload = http_request("PATCH", f"{base}/api/system", body={"overheat_mode": 0}, headers=headers, timeout=timeout)
            else:
                status, payload = http_request("POST", f"{base}/api/system/resume", body={}, headers=headers, timeout=timeout)
            if status in {200, 201, 202, 204}:
                return f"{what} HTTP {status}"
            raise DeviceError(f"{what} failed HTTP {status}: {payload.decode('utf-8', errors='replace')[:200]}")
        if cfg.type != "antminer":
            raise DeviceError(f"{action} is only for Antminers")
        if action in {"braiins_resume", "braiins_restart"}:
            import braiins_api
            fn = braiins_api.resume_miner if action == "braiins_resume" else braiins_api.restart_miner
            res = fn(cfg.ip, username=cfg.bos_username, password=cfg.bos_password, grpc_port=cfg.bos_grpc_port,
                     http_port=cfg.port, timeout=timeout)
            return f"{what.split(' (')[0]} via {res.get('transport')}"
        if action == "bitmain_reboot":
            if not cfg.web_password:
                raise DeviceError("no Bitmain web login saved for this miner")
            status, body = nk_faults.digest_request("GET", f"http://{cfg.ip}:{int(cfg.port or 80)}/cgi-bin/reboot.cgi",
                                                    cfg.web_username or "root", cfg.web_password, timeout=timeout)
            if status == 401:
                raise DeviceError("Bitmain web login was rejected (HTTP 401)")
            if 200 <= status < 300:
                return f"{what} HTTP {status}"
            raise DeviceError(f"{what} failed HTTP {status}")
        raise DeviceError(f"unsupported action {action}")

    def fetch_device_log(self, name: str, family: str) -> tuple[str, str]:
        """Read the miner's current log. Returns (source, text). Raises DeviceError when not available."""
        cfg = self._cfg(name)
        timeout = min(max(cfg.timeout_sec, 6.0), 15.0)
        if cfg.type == "antminer":
            if family == "braiins":
                if not cfg.bos_password:
                    raise DeviceError("save the Braiins login on the card to read its log")
                import braiins_api
                return "Braiins /api/v1/miner/log/errors", braiins_api.get_log(cfg.ip, cfg.bos_username, cfg.bos_password, http_port=cfg.port,
                                                                               which="errors", timeout=timeout)
            if not cfg.web_password:
                raise DeviceError("save this miner's Bitmain web login to read its log")
            status, body = nk_faults.digest_request("GET", f"http://{cfg.ip}:{int(cfg.port or 80)}/cgi-bin/log.cgi",
                                                    cfg.web_username or "root", cfg.web_password, timeout=timeout)
            if status != 200:
                raise DeviceError(f"Bitmain log.cgi HTTP {status}" + (" (login rejected)" if status == 401 else ""))
            return "Bitmain /cgi-bin/log.cgi", body.decode("utf-8", errors="replace")
        base = _base_url(cfg)
        status, payload = http_request("GET", f"{base}/api/system/logs", headers=_auth_headers(cfg), timeout=timeout)
        if status == 200 and payload:
            text = payload.decode("utf-8", errors="replace")
            if text.lstrip().startswith("<"):
                raise DeviceError("this firmware does not offer /api/system/logs")
            return "AxeOS /api/system/logs", text
        raise DeviceError(f"this firmware does not offer /api/system/logs (HTTP {status})")

    def probe_extra_faults(self, name: str, family: str) -> dict[str, Any]:
        """Background probe: NerdQAxe+ v2 dashboard/system flags, Braiins errors/status, Bitmain CGI/6060 warnings."""
        cfg = self._cfg(name)
        timeout = min(cfg.timeout_sec, 5.0)
        out: dict[str, Any] = {}
        if cfg.type == "antminer":
            if family == "braiins":
                if not cfg.bos_password:
                    return out
                import braiins_api
                errs, status = braiins_api.get_errors(
                    cfg.ip, cfg.bos_username, cfg.bos_password,
                    http_port=cfg.port, timeout=timeout, grpc_port=cfg.bos_grpc_port,
                )
                out["braiins_errors"] = errs
                if status:
                    out["braiins_status"] = status
                return out
            # Stock Bitmain: /cgi-bin/warning.cgi + /cgi-bin/summary.cgi (when web login saved) + port 6060 /warning
            warnings: list[dict[str, str]] = []
            if cfg.web_password:
                base = _base_url(cfg)
                try:
                    st_w, body_w = nk_faults.digest_request("GET", f"{base}/cgi-bin/warning.cgi", cfg.web_username, cfg.web_password, timeout=timeout)
                    if st_w == 200 and body_w:
                        txt = body_w.decode("utf-8", errors="replace").strip()
                        try:
                            jw = json.loads(txt)
                            items = jw.get("warnings") or jw.get("warning") or (jw if isinstance(jw, list) else [])
                            for it in items:
                                if isinstance(it, dict):
                                    c = str(it.get("code") or it.get("type") or "warning")
                                    m = str(it.get("msg") or it.get("message") or c)
                                    warnings.append({"code": c, "msg": m, "source": "Bitmain /cgi-bin/warning.cgi"})
                                elif it:
                                    warnings.append({"code": str(it), "msg": str(it), "source": "Bitmain /cgi-bin/warning.cgi"})
                        except json.JSONDecodeError:
                            if txt and txt.lower() not in {"ok", "none", "no warning", "[]", "{}"}:
                                warnings.append({"code": "warning.cgi", "msg": txt[:200], "source": "Bitmain /cgi-bin/warning.cgi"})
                except ConnectionError:
                    pass
                try:
                    st_s, body_s = nk_faults.digest_request("GET", f"{base}/cgi-bin/summary.cgi", cfg.web_username, cfg.web_password, timeout=timeout)
                    if st_s == 200 and body_s:
                        js = json.loads(body_s.decode("utf-8", errors="replace"))
                        for item in (js.get("SUMMARY") or []):
                            if not isinstance(item, dict):
                                continue
                            for st_item in (item.get("status") or []):
                                if not isinstance(st_item, dict):
                                    continue
                                sc = str(st_item.get("status") or "").lower()
                                if sc and sc != "s":
                                    warnings.append({
                                        "code": str(st_item.get("code") or st_item.get("type") or sc),
                                        "msg": str(st_item.get("msg") or st_item.get("type") or "status error"),
                                        "source": "Bitmain /cgi-bin/summary.cgi",
                                    })
                except (ConnectionError, json.JSONDecodeError):
                    pass
            # Optional port 6060 /warning (S19 XP / newer Bitmain)
            p6060 = int(getattr(cfg, "bitmain_6060_port", 6060) or 6060)
            if p6060 > 0:
                _, _, _, w6060, _ = _get_json(f"http://{cfg.ip}:{p6060}/warning", min(timeout, 1.5))
                if isinstance(w6060, dict):
                    out["_port6060"] = True
                    for it in (w6060.get("warning") or w6060.get("warnings") or []):
                        if isinstance(it, dict):
                            c = str(it.get("code") or it.get("type") or "6060")
                            m = str(it.get("msg") or it.get("message") or c)
                            warnings.append({"code": c, "msg": m, "source": "Bitmain :6060/warning"})
                        elif it:
                            warnings.append({"code": str(it), "msg": str(it), "source": "Bitmain :6060/warning"})
                else:
                    out["_port6060"] = False
            if warnings:
                out["bitmain_warnings"] = warnings
            return out
        if family == "nerd" or cfg.type in {"nerdoctaxe", "axeos-generic"}:
            base = _base_url(cfg)
            _, _, _, dash, _ = _get_json(f"{base}/api/v2/dashboard", timeout)
            if isinstance(dash, dict) and ("system" in dash or "thermal" in dash):
                out.update(extract_fault_fields(dash))
                _, _, _, sysinfo, _ = _get_json(f"{base}/api/v2/system", timeout)
                if isinstance(sysinfo, dict) and sysinfo.get("lastResetReason"):
                    out["lastResetReason"] = sysinfo.get("lastResetReason")
                out["_v2"] = True
            else:
                out["_v2"] = False
        return out

    def update_web_login(self, name: str, spec: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        if not isinstance(spec, dict):
            return False, "expected JSON object", {}
        with self._lock:
            state = self.states.get(name)
            if state is None:
                return False, f"unknown device: {name}", {}
            cfg = state.cfg
            if cfg.type != "antminer":
                return False, "the Bitmain web login is only for Antminers", {}
            if "web_username" in spec:
                cfg.web_username = str(spec.get("web_username") or "").strip()[:64]
            if spec.get("clear_password"):
                cfg.web_password = ""
            elif str(spec.get("web_password") or "") != "":
                cfg.web_password = str(spec["web_password"])[:128]
            out = {"web_username": cfg.web_username, "web_has_password": bool(cfg.web_password)}
            if self.shared is not None:
                self.shared.update_device(name, out)
            self._persist_userdata()
        return True, "", out

    def _maybe_promote_gamma(self, cfg: DeviceConfig, reading: Reading) -> None:
        if cfg.type != "axeos-generic":
            return
        if "voltage_control" in (cfg.overrides or {}):
            return
        fr = reading.fault_raw if isinstance(reading.fault_raw, dict) else {}
        rf = reading.raw_fields if isinstance(reading.raw_fields, dict) else {}
        probe: dict[str, Any] = {
            "hashRate": reading.hashrate if reading.hashrate is not None else 1.0,
            "deviceModel": fr.get("deviceModel") or rf.get("info.deviceModel") or reading.model or "",
            "boardVersion": fr.get("boardVersion") or rf.get("info.boardVersion") or "",
            "ASICModel": fr.get("ASICModel") or rf.get("info.ASICModel") or rf.get("info.asicModel") or "",
            "asicCount": fr.get("asicCount") if fr.get("asicCount") is not None else rf.get("info.asicCount"),
            "hostname": rf.get("info.hostname") or "",
            "version": reading.version or rf.get("info.version") or "",
        }
        if classify_axeos_payload(probe) != "gamma":
            return
        cfg.type = "gamma"
        cfg.voltage_control = True
        cfg.vr_voltage_relief = False
        LOG.warning("%s: detected Bitaxe Gamma hardware (%s); enabled automatic voltage control (max %d mV)", cfg.name, probe.get("boardVersion") or probe.get("deviceModel") or "gamma", cfg.max_voltage_mv)
        if self.shared is not None:
            self.shared.update_device(cfg.name, self._tunable_limits_payload(cfg))
        try:
            self._persist_userdata()
        except Exception:
            LOG.exception("%s: failed persisting Gamma promotion", cfg.name)

    def _poll_device_inner(self, state: DeviceState) -> Decision:
        cfg = state.cfg
        t0 = time.monotonic()
        state._nk_exc_class = ""  # type: ignore[attr-defined]
        try:
            reading = fetch_reading(cfg)
        except DeviceOffline as exc:
            reading = Reading(ok=False, error=str(exc))
            cause = exc.__cause__ or exc.__context__
            state._nk_exc_class = type(exc).__name__ + (f" <- {type(cause).__name__}" if cause is not None else "")  # type: ignore[attr-defined]
        except Exception as exc:
            reading = Reading(ok=False, error=f"unexpected error: {type(exc).__name__}: {exc}")
            state._nk_exc_class = type(exc).__name__  # type: ignore[attr-defined]
        state._nk_poll_ms = (time.monotonic() - t0) * 1000.0  # type: ignore[attr-defined]
        with self._lock:
            if self.states.get(cfg.name) is not state:
                return Decision("removed", None, "device removed", 0)
        state._nk_reading = reading  # type: ignore[attr-defined]

        if not reading.ok:
            state.cool_polls = 0
            state.low_error_polls = 0
            state.last_seen_ok = bool(reading.reachable)
            action = "unparsed" if reading.reachable else "offline"
            decision = Decision(action, None, reading.error or ("online but unparsed" if reading.reachable else "device offline"), 0)
            self._log_row(cfg, reading, decision, applied_freq=None, applied_voltage=None, state=state)
            self._publish(state, reading, decision)
            LOG.warning("%s: %s — %s", cfg.name, action, decision.reason)
            return decision

        state.last_seen_ok = True
        self._maybe_promote_gamma(cfg, reading)
        hottest_sample = max(reading.asic_temps) if reading.asic_temps else reading.asic_temp
        self.history.record(cfg.name, self.clock(), reading.hashrate, hottest_sample, reading.vr_temp)
        derive_last_share(reading, cfg.name, self.clock(), self.last_share)

        if not is_tunable(cfg):
            decision = decide(reading, cfg, 0)
            if cfg.type == "antminer" and cfg.bos_power_mgmt:
                now_ts = self.clock()
                cur_target = state.bos_last_target_w or (int(round(reading.power_limit_w)) if reading.power_limit_w else None)
                bcfg = cfg
                sched = self._schedule_limits(cfg, reading=reading)
                if sched and sched.get("bos_power_target_w"):
                    # Time-of-use window: lower the power-target ceiling. Hot-chip logic in
                    # decide_bos_power still runs first, so thermal protection always wins.
                    win_w = int(sched["bos_power_target_w"])
                    bcfg = dataclasses.replace(
                        cfg,
                        bos_max_watts=min(int(cfg.bos_max_watts), win_w),
                        bos_min_watts=min(int(cfg.bos_min_watts), win_w),
                    )
                bos_act, bos_new_w, bos_reason, bos_streak = decide_bos_power(
                    reading, bcfg, state.bos_cool_polls, state.bos_last_change_ts, now_ts,
                    current_target_w=cur_target, bypass_cap_cooldown=bool(sched and sched.get("bos_power_target_w")),
                )
                if bcfg is not cfg and sched:
                    bos_reason += f" [time-of-use: {', '.join(sched['windows'])} cap {sched['bos_power_target_w']} W]"
                state.bos_cool_polls = bos_streak
                state.bos_last_action = bos_act
                state.bos_last_reason = bos_reason
                if bos_act in {"power_up", "power_down"} and bos_new_w is not None:
                    dry = self._dry_run()
                    if self.hub is not None:
                        self.hub.log_tuning(cfg.name, "power_target", cur_target, bos_new_w, bos_reason, bos_act, source="braiins", dry_run=dry)
                    if dry:
                        state.bos_last_change_ts = now_ts
                        state.bos_last_target_w = bos_new_w
                        state.bos_last_action_at = iso_now()
                        state.bos_last_reason = f"DRY-RUN {bos_reason}"
                        LOG.warning("%s: %s", cfg.name, state.bos_last_reason)
                    else:
                        try:
                            import braiins_api
                            res = braiins_api.set_power_target(
                                cfg.ip,
                                bos_new_w,
                                username=cfg.bos_username,
                                password=cfg.bos_password,
                                grpc_port=cfg.bos_grpc_port,
                                http_port=cfg.port,
                                timeout=cfg.timeout_sec,
                            )
                            state.bos_last_change_ts = now_ts
                            state.bos_last_target_w = int(res.get("power_target_w") or bos_new_w)
                            state.bos_last_action_at = iso_now()
                            state.bos_last_reason = f"{bos_reason} (via {res.get('transport')})"
                            LOG.warning("%s: %s", cfg.name, state.bos_last_reason)
                        except Exception as exc:
                            state.bos_last_action = "bos_error"
                            state.bos_last_reason = f"Braiins SetPowerTarget failed: {exc}"
                            LOG.error("%s: %s", cfg.name, state.bos_last_reason)
                bos_act_label = state.bos_last_action if state.bos_last_action in {"power_up", "power_down", "bos_error", "hold"} else "monitor"
                decision = Decision(
                    bos_act_label,
                    None,
                    state.bos_last_reason or decision.reason,
                    0,
                    voltage_reason="Braiins power-target mode",
                )
            else:
                state.bos_last_action = ""
                state.bos_last_reason = ""
            self._log_row(cfg, reading, decision, applied_freq=None, applied_voltage=None, state=state)
            self._publish(state, reading, decision)
            LOG.info(
                "%s: %s | GH/s=%s | chip=%s | fans=%s | fw=%s",
                cfg.name,
                "braiins power" if (cfg.type == "antminer" and cfg.bos_power_mgmt) else "monitor",
                _fmt(reading.hashrate, 1),
                _fmt(reading.asic_temp),
                ",".join(str(f) for f in reading.fan_rpms) if reading.fan_rpms else "-",
                reading.firmware or "unknown",
            )
            return decision

        now = self.clock()
        lockout_sec = self._lockout_duration_sec()
        # Update VR history & sensor plausibility check before decide()
        vr_rise_c: Optional[float] = None
        vr_rise_dt: Optional[float] = None
        if reading.vr_temp is not None and reading.vr_temp > 0:
            state.vr_history.append((now, float(reading.vr_temp)))
            while state.vr_history and now - state.vr_history[0][0] > 300.0:
                state.vr_history.popleft()
            win = [pt for pt in state.vr_history if now - pt[0] <= 65.0]
            if len(win) >= 2 and (win[-1][0] - win[0][0]) >= 10.0:
                vr_rise_c = win[-1][1] - min(pt[1] for pt in win[:-1])
                vr_rise_dt = win[-1][0] - win[0][0]

        cur_asic = reading.asic_temp
        cur_vr = reading.vr_temp
        suspect_msg = ""
        suspect_comp = ""
        if cur_asic is not None and cur_asic > 0 and cur_vr is not None and cur_vr > 0:
            if state.last_valid_asic_c is not None and abs(cur_asic - state.last_valid_asic_c) > 25.0:
                suspect_msg = f"ASIC temp jumped {state.last_valid_asic_c:.1f} -> {cur_asic:.1f} °C in one poll (>25 °C)"
                suspect_comp = "ASIC temp sensor"
            elif state.last_valid_vr_c is not None and abs(cur_vr - state.last_valid_vr_c) > 25.0:
                suspect_msg = f"VR temp jumped {state.last_valid_vr_c:.1f} -> {cur_vr:.1f} °C in one poll (>25 °C)"
                suspect_comp = "VR temp sensor"
            elif cur_vr > 105.0 and cur_asic < 45.0:
                suspect_msg = f"Implausible VR {cur_vr:.1f} °C while ASIC {cur_asic:.1f} °C"
                suspect_comp = "VR / ASIC temp sensors"
            if suspect_msg:
                state.sensor_suspect = True
                state.sensor_suspect_reason = suspect_msg
                state.sensor_suspect_component = suspect_comp
                state.sensor_plausible_streak = 0
                reading.fault_raw["tuner_sensor_suspect"] = suspect_msg
                reading.fault_raw["tuner_sensor_component"] = suspect_comp
            else:
                state.last_valid_asic_c = cur_asic
                state.last_valid_vr_c = cur_vr
                if state.sensor_suspect:
                    state.sensor_plausible_streak += 1
                    if state.sensor_plausible_streak >= 3:
                        state.sensor_suspect = False
                        state.sensor_suspect_reason = ""
                        state.sensor_suspect_component = ""
                    else:
                        reading.fault_raw["tuner_sensor_suspect"] = state.sensor_suspect_reason
                        reading.fault_raw["tuner_sensor_component"] = state.sensor_suspect_component

        sample, source = compute_error_sample(reading, state)
        rolling_err = state.record_error(sample, source)
        sched = self._schedule_limits(cfg, reading=reading)
        tou_sched = self._tou_limits(cfg, reading=reading)
        dcfg = schedule_adjusted_cfg(cfg, sched)
        self._record_bad_voltage(dcfg, state, reading, rolling_err, sample, source)
        state.learned_min_voltage_mv = self._learned_min_voltage_mv(dcfg, state, reading.frequency)
        active_faults = []
        if self.hub is not None and hasattr(self.hub, "faults_now"):
            active_faults = list(self.hub.faults_now.get(cfg.name) or [])
        lock_rem = max(0.0, state.emergency_lockout_until - now) if state.emergency_lockout_until > now else 0.0
        decision = decide(
            reading,
            dcfg,
            state.cool_polls,
            error_pct=rolling_err,
            low_error_polls=state.low_error_polls,
            active_faults=active_faults,
            vr_rise_c=vr_rise_c,
            vr_rise_dt=vr_rise_dt,
            lockout_remaining_sec=lock_rem,
            rollback_freq=state.rollback_freq,
            sensor_suspect_reason=state.sensor_suspect_reason if state.sensor_suspect else "",
            latest_error_pct=sample,
            error_source=source,
            error_window_count=len(state.error_window),
            learned_min_voltage_mv=state.learned_min_voltage_mv,
        )
        if not self._enabled(cfg.name):
            tou_decision = self._enforce_tou_when_paused(cfg, state, reading, decision, tou_sched, active_faults, now)
            if tou_decision is not None:
                self._log_row(cfg, reading, tou_decision, applied_freq=tou_decision.new_freq, applied_voltage=None, state=state)
                self._publish(state, reading, tou_decision)
                return tou_decision
        else:
            state.tou_pre_cap_freq = None
            state.tou_applied_freq = None

        decision = apply_schedule_cap(decision, reading, cfg, dcfg, sched)

        # Record stable frequency (when healthy for >=10 min, or before stepping up)
        self._update_stability_and_lockout(cfg, state, reading, decision, rolling_err, now, lockout_sec)

        try:
            self._quiet_mode(cfg, state, reading, decision)
        except Exception:
            LOG.exception("%s: quiet mode failed", cfg.name)

        is_freq_change = decision.action in {"step_down", "step_up", "emergency_down"} and decision.new_freq is not None
        is_volt_change = decision.new_voltage is not None and decision.action in {"voltage_up", "voltage_down", "emergency_down", "step_down"}

        if is_freq_change or is_volt_change:
            if decision.cooldown_override_sec is not None:
                eff_cd = min(float(cfg.cooldown_sec), float(decision.cooldown_override_sec))
            elif decision.thermal_step_down:
                eff_cd = min(float(cfg.cooldown_sec), THERMAL_STEP_DOWN_SETTLE_SEC)
            else:
                eff_cd = float(cfg.cooldown_sec)
            on_cooldown = (
                not decision.bypass_cooldown
                and state.last_change_ts > 0
                and (now - state.last_change_ts) < eff_cd
            )
            if on_cooldown:
                remaining = eff_cd - (now - state.last_change_ts)
                kept_cool = (
                    max(state.cool_polls, cfg.cool_polls_required)
                    if decision.action == "step_up"
                    else 0
                )
                kept_low = (
                    max(state.low_error_polls, cfg.low_error_polls_required)
                    if decision.action == "voltage_down"
                    else 0
                )
                original_action = decision.action
                decision = Decision(
                    "hold",
                    None,
                    f"{original_action} deferred, cooldown {remaining:.0f}s left ({decision.reason})",
                    kept_cool,
                    low_error_polls=kept_low,
                    voltage_reason=decision.voltage_reason,
                )
                state.cool_polls = kept_cool
                state.low_error_polls = kept_low
            elif not self._enabled(cfg.name):
                state.cool_polls = 0
                state.low_error_polls = 0
                decision = Decision(
                    "paused",
                    None,
                    f"autotune stopped; would have {decision.reason}",
                    0,
                    new_voltage=decision.new_voltage,
                    low_error_polls=0,
                    voltage_reason=f"paused ({decision.voltage_reason})" if decision.voltage_reason else "paused",
                )
                self._log_row(cfg, reading, decision, applied_freq=None, applied_voltage=None, state=state)
                self._publish(state, reading, decision)
                LOG.info(
                    "%s: %s | ASIC=%s VR=%s freq=%s V=%s err=%s GH/s=%s",
                    cfg.name,
                    decision.reason,
                    _fmt(reading.asic_temp),
                    _fmt(reading.vr_temp),
                    _fmt(reading.frequency, 0),
                    _fmt(reading.core_voltage, 0),
                    _fmt(rolling_err, 2),
                    _fmt(reading.hashrate, 2),
                )
                return decision
            else:
                applied = self._apply(cfg, state, reading, decision)
                if applied:
                    state.clear_error_window()
                    state.cool_polls = 0
                else:
                    state.cool_polls = (
                        max(state.cool_polls, cfg.cool_polls_required)
                        if decision.action == "step_up"
                        else 0
                    )
                    state.low_error_polls = (
                        max(state.low_error_polls, cfg.low_error_polls_required)
                        if decision.action == "voltage_down"
                        else 0
                    )
                self._log_row(
                    cfg,
                    reading,
                    decision,
                    applied_freq=decision.new_freq if (applied and is_freq_change) else None,
                    applied_voltage=decision.new_voltage if (applied and is_volt_change) else None,
                    state=state,
                )
                self._publish(state, reading, decision)
                return decision
        else:
            state.cool_polls = decision.cool_polls
            state.low_error_polls = decision.low_error_polls

        self._log_row(cfg, reading, decision, applied_freq=None, applied_voltage=None, state=state)
        self._publish(state, reading, decision)
        level = logging.INFO if decision.action in {"hold", "step_up", "step_down", "voltage_up", "voltage_down", "paused"} else logging.WARNING
        LOG.log(
            level,
            "%s: %s | ASIC=%s VR=%s freq=%s V=%s err=%s GH/s=%s",
            cfg.name,
            decision.reason,
            _fmt(reading.asic_temp),
            _fmt(reading.vr_temp),
            _fmt(reading.frequency, 0),
            _fmt(reading.core_voltage, 0),
            _fmt(rolling_err, 2),
            _fmt(reading.hashrate, 2),
        )
        return decision

    def _apply(self, cfg: DeviceConfig, state: DeviceState, reading: Reading, decision: Decision) -> bool:
        results: list[str] = []
        freq_done = False
        volt_done = False
        want_freq = decision.action in {"step_down", "step_up", "emergency_down"} and decision.new_freq is not None
        want_volt = decision.new_voltage is not None and decision.action in {"voltage_up", "voltage_down", "emergency_down", "step_down"}
        if not want_freq and not want_volt:
            return False

        if want_freq and decision.new_freq is not None:
            new_freq = decision.new_freq
            if new_freq > cfg.max_freq + 1e-6:
                LOG.error("%s: refusing to set %.0f MHz above max_freq %.0f", cfg.name, new_freq, cfg.max_freq)
                return False
            try:
                results.append(apply_frequency(cfg, new_freq, self._dry_run()))
                freq_done = True
            except (DeviceError, DeviceOffline) as exc:
                LOG.error("%s: failed to apply frequency: %s", cfg.name, exc)
                if not want_volt:
                    return False

        if want_volt and decision.new_voltage is not None:
            new_mv = int(decision.new_voltage)
            if new_mv > min(HARD_MAX_VOLTAGE_MV, cfg.max_voltage_mv):
                LOG.error("%s: refusing to set %d mV above cap %d mV", cfg.name, new_mv, cfg.max_voltage_mv)
                return bool(results)
            cur_v = _effective_core_voltage_mv(reading, cfg)
            vr_relief_lower = bool(cfg.vr_voltage_relief and new_mv < cur_v and reading.vr_temp is not None and reading.vr_temp > cfg.vr_target_c)
            try:
                results.append(apply_voltage(cfg, new_mv, self._dry_run(), allow_vr_relief=vr_relief_lower))
                volt_done = True
            except (DeviceError, DeviceOffline) as exc:
                LOG.error("%s: failed to apply voltage: %s", cfg.name, exc)
                if not results:
                    return False

        if not results:
            return False
        result = "; ".join(results)
        if self.hub is not None:
            dry_flag = self._dry_run()
            src = "emergency" if decision.action == "emergency_down" else ("schedule" if "time-of-use" in decision.reason else "autotune")
            if freq_done:
                self.hub.log_tuning(cfg.name, "frequency", reading.frequency, decision.new_freq, decision.reason, decision.action, source=src, dry_run=dry_flag)
            if volt_done:
                self.hub.log_tuning(cfg.name, "voltage", reading.core_voltage, decision.new_voltage, decision.reason, decision.action, source=src, dry_run=dry_flag)
        state.last_change_ts = self.clock()
        state.last_change_action = decision.action
        state.cool_polls = 0
        prefix = "DRY-RUN " if self._dry_run() else ""
        LOG.warning("%s: %s%s (%s)", cfg.name, prefix, decision.reason, result)

        if cfg.restart_after_change:
            if not self._dry_run():
                self._note_transition(cfg.name, "nk_restart", "restart after a tuning change")
            try:
                rst = restart_device(cfg, self._dry_run())
                LOG.info("%s: %s", cfg.name, rst)
            except (DeviceError, DeviceOffline) as exc:
                LOG.error("%s: restart failed: %s", cfg.name, exc)
                return True
            if not self.config.dry_run and cfg.restart_wait_sec > 0:
                LOG.info("%s: waiting %.0fs for restart", cfg.name, cfg.restart_wait_sec)
                self.sleeper(cfg.restart_wait_sec)
        return True

    def _log_row(
        self,
        cfg: DeviceConfig,
        reading: Reading,
        decision: Decision,
        applied_freq: Optional[float],
        applied_voltage: Optional[int],
        state: DeviceState,
    ) -> None:
        if not self.csv_logger:
            return
        self.csv_logger.write(
            {
                "timestamp": iso_now(),
                "device": cfg.name,
                "type": cfg.type,
                "action": decision.action,
                "asic_temp_c": _fmt(reading.asic_temp),
                "vr_temp_c": _fmt(reading.vr_temp),
                "frequency_mhz": _fmt(reading.frequency, 0),
                "new_frequency_mhz": _fmt(applied_freq, 0) if applied_freq is not None else "",
                "hashrate_ghs": _fmt(reading.hashrate, 2),
                "power_w": _fmt(reading.power, 2),
                "core_voltage_mv": _fmt(reading.core_voltage, 0),
                "new_voltage_mv": str(applied_voltage) if applied_voltage is not None else "",
                "error_pct": _fmt(state.last_error_pct, 2),
                "rolling_error_pct": _fmt(state.last_rolling_error_pct, 2),
                "reason": decision.reason,
            }
        )

    def poll_all(self) -> list[Decision]:
        with self._lock:
            states = [self.states[d.name] for d in self.config.devices if d.name in self.states]
        return [self.poll_device(st) for st in states]

    def run(self, once: bool = False) -> None:
        mode = "DRY-RUN" if self._dry_run() else "LIVE"
        with self._lock:
            devs = list(self.config.devices)
        LOG.info("%s v%s starting (%s) with %d device(s)", APP_NAME, APP_VERSION, mode, len(devs))
        for d in devs:
            if not is_tunable(d):
                LOG.info("  %s (%s) at %s:%d — monitor only", d.name, d.type, d.ip, d.cgminer_port)
                continue
            if d.voltage_control:
                volt = (
                    f"HW error-driven voltage: start {d.voltage_start_mv} mV, "
                    f"floor {d.min_voltage_mv} mV, hard cap {d.max_voltage_mv} mV, "
                    f"step {d.voltage_step_mv} mV (err > {d.error_high_pct:.1f}% up / < {d.error_low_pct:.1f}% down)"
                )
            else:
                volt = "frequency only (core voltage will not be changed)"
            LOG.info(
                "  %s (%s) at %s — freq %s-%s MHz, ASIC target/max %.0f/%.0fC, VR target/max %.0f/%.0fC, %s",
                d.name,
                d.type,
                d.ip,
                f"{d.min_freq:.0f}",
                f"{d.max_freq:.0f}",
                d.asic_target_c,
                d.asic_max_c,
                d.vr_target_c,
                d.vr_max_c,
                volt,
            )
        while True:
            started = self.clock()
            self.poll_all()
            if once:
                return
            with self._lock:
                intervals = [d.poll_interval_sec for d in self.config.devices]
            interval = min(intervals) if intervals else 20.0
            elapsed = self.clock() - started
            delay = max(0.0, interval - elapsed)
            if self.sleeper is time.sleep:
                self._wake.wait(delay)
                self._wake.clear()
            else:
                self.sleeper(delay)


def _fmt(value: Optional[float], digits: int = 1) -> str:
    if value is None:
        return ""
    if digits == 0:
        return str(int(round(value)))
    return f"{value:.{digits}f}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_STOP = False


def _handle_stop(signum: int, _frame: Any) -> None:
    global _STOP
    _STOP = True
    LOG.info("Received signal %s, will exit after this cycle", signum)


def default_config_path() -> Path:
    for name in ("config.json", "config.yaml", "config.yml"):
        candidate = SCRIPT_DIR / name
        if candidate.exists():
            return candidate
    return SCRIPT_DIR / "config.json"


PROBE_ENDPOINTS = (
    "/api/system/info",
    "/api/v2/identify",
    "/api/v2/system",
    "/api/v2/dashboard",
    "/api/v2/settings",
)


def _probe_base_url(host: str) -> tuple[str, str]:
    raw = (host or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urllib.parse.urlparse(raw)
        hostname = parsed.hostname or raw
        port = parsed.port
        if port and port != 80:
            return f"{parsed.scheme}://{hostname}:{port}", hostname
        return f"{parsed.scheme}://{hostname}", hostname
    if ":" in raw and not raw.count(":") > 1:
        hostname, port_s = raw.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            return f"http://{raw}", raw
        if port != 80:
            return f"http://{hostname}:{port}", hostname
        return f"http://{hostname}", hostname
    return f"http://{raw}", raw


def run_probe(host: str, timeout: float = 8.0, file: Any = None) -> int:
    """Print diagnostics for each known AxeOS / NerdQAxePlus endpoint. Returns 0 if any JSON API answered."""
    out = file or sys.stdout
    base, hostname = _probe_base_url(host)
    print(f"Probing miner at {base} (timeout {timeout:.0f}s, no redirects, Connection: close)", file=out)
    print(f"Local time: {iso_now()}", file=out)
    any_json = False
    for path in PROBE_ENDPOINTS:
        url = f"{base}{path}"
        print("=" * 72, file=out)
        print(f"GET {url}", file=out)
        started = time.perf_counter()
        try:
            status, payload, headers = http_request_ex("GET", url, timeout=timeout, retries=1)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            print(f"status: {status}", file=out)
            print(f"elapsed_ms: {elapsed_ms:.0f}", file=out)
            print("headers:", file=out)
            if headers:
                for key in sorted(headers):
                    print(f"  {key}: {headers[key]}", file=out)
            else:
                print("  (none)", file=out)
            print(f"body_bytes: {len(payload)}", file=out)
            if _looks_like_html(payload, headers):
                print("note: response looks like HTML (typical when v1 firmware 302s unknown /api/v2 paths to the web UI)", file=out)
            data = _json_body(payload)
            if isinstance(data, dict):
                any_json = True
                kind = classify_axeos_payload(data) or "unknown"
                print(f"json: yes, keys={sorted(data.keys())}", file=out)
                print(f"classified_as: {kind}", file=out)
                if path == "/api/system/info":
                    try:
                        reading = parse_nerd_v1_info(data)
                        print(
                            f"parsed: model={reading.model!r} version={reading.version} "
                            f"asic={reading.asic_temp} vr={reading.vr_temp} "
                            f"freq={reading.frequency} V={reading.core_voltage} "
                            f"hashrate={reading.hashrate} shares={reading.shares_accepted}/{reading.shares_rejected}",
                            file=out,
                        )
                    except Exception as exc:
                        print(f"parse_error: {exc}", file=out)
            else:
                print("json: no", file=out)
            text_body = payload.decode("utf-8", errors="replace") if payload else ""
            print("body:", file=out)
            print(text_body if text_body else "(empty)", file=out)
        except DeviceOffline as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            print(f"status: unreachable", file=out)
            print(f"elapsed_ms: {elapsed_ms:.0f}", file=out)
            print(f"error: {exc}", file=out)

    print("=" * 72, file=out)
    print("TCP 4028 (CGMiner / Antminer, should be closed on a NerdOCTAxe):", file=out)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect((hostname, 4028))
        print("  open — this IP also answers CGMiner; not expected for NerdOCTAxe", file=out)
    except OSError as exc:
        print(f"  closed/unreachable ({exc})", file=out)
    finally:
        sock.close()

    if any_json:
        print("Probe finished: at least one JSON API responded. Paste this output if you still need help.", file=out)
        return 0
    print("Probe finished: no JSON API responded. Check the IP, that you are on the same LAN, and try again.", file=out)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Miner Hearth: auto-tune AxeOS miners (Bitaxe-class, NerdQAxe/NerdOCTAxe) and monitor Antminer S19s."
    )
    parser.add_argument(
        "--config",
        default=str(default_config_path()),
        help="Path to config.json or config.yaml (default: next to this script)",
    )
    parser.add_argument(
        "--probe",
        metavar="HOST",
        default="",
        help="Print HTTP status, headers, timing, and raw body for each miner API endpoint on HOST, then exit (example: --probe 192.168.1.100)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Poll and log decisions, but never change frequency, voltage, or restart",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll every device once and exit (good for a connectivity check)",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Start the local web dashboard (default for a continuous run)",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Do not start the local web dashboard",
    )
    parser.add_argument(
        "--dashboard-host",
        default="",
        help="Dashboard bind address (default: 127.0.0.1 from config; use 0.0.0.0 to allow other PCs on the LAN)",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=0,
        help="Dashboard port (default: 8080 from config)",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open the dashboard in your default browser after it starts",
    )
    parser.add_argument(
        "--log-dir",
        default="",
        help="Override log directory (default: logs next to this script, or config log_dir)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "probe", ""):
        return run_probe(args.probe)
    config_path = Path(args.config)
    if not config_path.is_absolute():
        # Prefer cwd, then the script directory, so Windows shortcuts still work.
        if not config_path.exists():
            alt = SCRIPT_DIR / config_path
            if alt.exists():
                config_path = alt
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        print("Copy or edit config.json next to autotune.py and try again.", file=sys.stderr)
        return 2

    # v1.6+: never run alongside MinerHearth.exe / MinerDashboard.exe (or a second run.bat) on Windows,
    # because two copies would poll and tune the same miners.
    _instance_guard = None
    if os.name == "nt" and not args.once:
        try:
            from miner_dashboard import SingleInstance

            _instance_guard = SingleInstance(SCRIPT_DIR / "logs")
            if not _instance_guard.acquire():
                print(
                    "Miner Hearth is already running (MinerHearth.exe, an old MinerDashboard.exe, run.bat or the scheduled task). "
                    "Close it first so two copies do not tune the same miners.",
                    file=sys.stderr,
                )
                return 3
        except Exception:
            _instance_guard = None

    raw = load_raw_config(config_path)
    config = parse_config(raw, SCRIPT_DIR, dry_run=bool(args.dry_run), config_path=config_path)
    if args.log_dir:
        log_dir = Path(args.log_dir)
        if not log_dir.is_absolute():
            log_dir = SCRIPT_DIR / log_dir
        config.log_dir = log_dir
        config.log_file = log_dir / config.log_file.name
        config.csv_file = log_dir / config.csv_file.name

    want_dashboard = bool(config.dashboard_enabled)
    if args.dashboard:
        want_dashboard = True
    if args.no_dashboard:
        want_dashboard = False
    # A one-shot poll should not leave a web server running unless the user asked.
    if args.once and not args.dashboard:
        want_dashboard = False
    if args.dashboard_host:
        config.dashboard_host = args.dashboard_host
    if args.dashboard_port:
        config.dashboard_port = int(args.dashboard_port)

    from dashboard import DashboardServer, SharedState

    device_defaults = {
        d.name: (False if not is_tunable(d) else (bool(d.enabled_default) if d.enabled_default is not None else False))
        for d in config.devices
    }
    shared = SharedState(
        [d.name for d in config.devices],
        config.state_file,
        dry_run=bool(config.dry_run),
        device_defaults=device_defaults,
        scan_subnet=config.scan_subnet,
        autotune_enabled=config.autotune_enabled,
    )
    # Keep the config flag in sync for anything that still reads it.
    config.dry_run = shared.is_dry_run()

    setup_logging(config.log_file, extra_handlers=[shared.log_handler()])
    LOG.info("%s v%s", APP_NAME, APP_VERSION)
    LOG.info("Loaded config %s", config_path)
    LOG.info("User data file: %s", config.userdata_file)
    LOG.info("Text log: %s", config.log_file)
    LOG.info("CSV log:  %s", config.csv_file)
    LOG.info("State file: %s", config.state_file)

    csv_logger = CsvLogger(config.csv_file)
    tuner = Tuner(config, csv_logger=csv_logger, shared=shared)
    shared.attach_tuner(tuner)
    hub = None
    if not args.once:
        try:
            import miner_hearth

            hub = miner_hearth.attach_feature_hub(tuner, shared, start=True)
        except Exception:
            LOG.exception("Miner Hearth features could not start; autotune continues without them")

    dashboard = None
    if want_dashboard:
        try:
            dashboard = DashboardServer(shared, config.dashboard_host, config.dashboard_port).start()
            LOG.info("Open the dashboard in your browser: %s", dashboard.url)
            if args.open_browser or (sys.stdout is not None and sys.stdout.isatty() and not args.once):
                try:
                    import webbrowser
                    webbrowser.open(dashboard.url)
                except Exception as exc:
                    LOG.info("Could not open a browser automatically: %s", exc)
        except OSError as exc:
            LOG.error(
                "Dashboard could not bind to %s:%s (%s). Autotune will still run. "
                "Use --dashboard-port to pick a free port, or --no-dashboard.",
                config.dashboard_host,
                config.dashboard_port,
                exc,
            )

    # SIGTERM is not available on some Windows environments; handle what exists.
    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handle_stop)
            except Exception:
                pass

    original_sleep = tuner.sleeper

    def interruptible_sleep(seconds: float) -> None:
        end = tuner.clock() + max(0.0, seconds)
        while not _STOP:
            remaining = end - tuner.clock()
            if remaining <= 0:
                return
            original_sleep(min(0.5, remaining))
        raise KeyboardInterrupt

    tuner.sleeper = interruptible_sleep
    try:
        tuner.run(once=args.once)
        return 0
    except KeyboardInterrupt:
        LOG.info("Stopped by user")
        return 0
    except Exception:
        LOG.exception("Fatal error")
        return 1
    finally:
        if "dashboard" in locals() and dashboard is not None:
            try:
                dashboard.stop()
            except Exception:
                pass
        if hub is not None:
            try:
                hub.stop()
            except Exception:
                pass
        csv_logger.close()


if __name__ == "__main__":
    sys.exit(main())
