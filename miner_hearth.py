#!/usr/bin/env python3
"""Miner Hearth feature hub (v1.8 - v1.12, stdlib only).

Adds, on top of the autotune engine and dashboard:
  * block-found detection that only alerts after an explorer confirms the
    block's coinbase pays one of the user's payout addresses,
  * offline / hashrate-drop / fallback-pool / no-share alerts with cooldowns,
  * retro sound + banner + Windows toast notifications,
  * power cost, expected sats/day, break-even rate and block odds,
  * time-of-use schedules that cooperate with the tuner (emergency always wins),
  * 7-day / 30-day downsampled history and CSV export,
  * a persisted, rotated tuning-change timeline,
  * opt-in LAN / phone access with PIN and read-only mode,
  * settings backup / restore, firmware update notices, network stats.
v1.9 adds per-coin detection and odds (nk_coins), session best share, pool health and
multi-pool stats, chip wear tracking, quiet mode, maintenance restarts, webhooks,
themes / temperature units and a customizable layout (nk_extras).

Everything user-specific lives in userdata/ (never in config.json), so the
program stays generic and shareable.
"""

from __future__ import annotations

import base64
import copy
import csv
import hmac
import io
import ipaddress
import gzip
import json
import logging
import os
import re
import socket
import sys
import subprocess  # constants only (DEVNULL); processes start via nk_proc
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote

import nk_coins
import nk_eventlog
import nk_faults
import nk_extras
import nk_legacy
import nk_nodes
import nk_proc
import nk_grace
import nk_math
import nk_net
import nk_poolfeed
import nk_pools
import nk_update
import nk_sounds

LOG = logging.getLogger("autotune.minerhearth")

APP_NAME = "Miner Hearth"
DONATE_ADDRESS = "bc1q3m8usp6mffv4txhwcltq9lu9jltx02eyukgrvf"
SETTINGS_FILE_NAME = "miner_hearth.json"
IS_WINDOWS = os.name == "nt"
MAX_SOUND_BYTES = 5 * 1024 * 1024
CURRENCIES = ["USD", "EUR", "GBP", "CAD", "CHF", "AUD", "JPY"]

ALERT_KINDS = ("offline", "hashrate_drop", "fallback_pool", "no_shares", "pool_rejects", "chip_wear", "hardware_fault")
SETTINGS_SCHEMA = 4
THEMES = ("dark", "light", "contrast")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def nk_node_defaults() -> dict[str, Any]:
    return nk_nodes.default_settings()


def default_app_settings() -> dict[str, Any]:
    return {
        "schema_version": SETTINGS_SCHEMA,
        "sounds": {
            "master_mute": False,
            "volume": 0.8,
            "block_sound": "builtin:victory_fanfare",
            "problem_sound": "builtin:alarm",
        },
        "notifications": {"desktop_toast": True},
        "alerts": {
            "cooldown_min": 30,
            "offline": {"enabled": True, "minutes": 3},
            "hashrate_drop": {"enabled": True, "drop_pct": 30, "baseline_min": 30, "sustain_min": 5},
            "fallback_pool": {"enabled": True},
            "no_shares": {"enabled": True, "minutes": 30},
            "pool_rejects": {"enabled": True, "pct": 5.0, "window_min": 30, "min_shares": 10},
            "chip_wear": {"enabled": True},
            "hardware_fault": {"enabled": True},
            # v1.15.5: pool switches / NK restarts / boot are not faults
            "maintenance": dict(nk_grace.DEFAULTS),
        },
        "block": {
            "enabled": True,
            "payout_addresses": [],
            "explorer": "mempool",
            "custom_base_url": "",
            "allow_public_fallback": True,
            # v1.15.5
            "pool_feed": True,
            "notify_unknown_coin": True,
            "max_messages_per_hour": 6,
        },
        "power": {"rate_per_kwh": 0.15, "currency": "USD", "include_fees": True},
        "schedule": {"enabled": False, "windows": []},
        "lan": {"enabled": False, "port": 8081, "pin": "", "read_only": True, "bind_mode": "all"},
        "firmware": {"check_enabled": True},
        "network": {"enabled": True},
        # --- v1.9 ---
        "coins": {"overrides": {}, "payout_addresses": {}, "fetch_chains": True},
        "pools": {"probe_enabled": True},
        # v1.16: live updates, opt-in; packages come only from a file / URL the user picks
        "updates": {"enabled": False, "trusted_keys": [], "require_signature": False},
        "chip_health": {"enabled": True, "stable_min": 30, "wear_mv": 20, "min_days": 14},
        "quiet": {"enabled": False, "windows": []},
        "restarts": {"weekly": {"enabled": False, "day": 6, "time": "04:00"}, "hung": {"enabled": False, "minutes": 15},
                     "max_per_day": 2, "min_gap_min": 60, "devices": []},
        "webhooks": {"enabled": False, "discord_url": "", "telegram_token": "", "telegram_chat_id": "", "ntfy_url": "", "ntfy_token": "",
                     "types": {k: (k in nk_extras.WEBHOOK_DEFAULT_ON) for k in nk_extras.WEBHOOK_TYPES}},
        "ui": {"theme": "dark", "temp_unit": "C", "background": True},
        "layout": nk_extras.default_layout(),
        # --- v1.10 ---
        "faults": {"enabled": True, "probe_enabled": True, "cool_asic_c": 45.0, "cool_vr_c": 65.0, "min_gap_min": 10.0, "max_per_day": 4, "record_hours": 12.0},
        # --- v1.12 ---
        "tuner": {"emergency_lockout_min": 180},
        # --- v1.15.4: persistent, redacted full event / telemetry log (userdata/logs/) ---
        "eventlog": dict(nk_eventlog.DEFAULTS),
        # --- v1.17.0: node monitoring (opt-in; node credentials live in userdata/nodes.json, never here) ---
        "nodes": nk_node_defaults(),
    }


def _f(v: Any, default: float, lo: Optional[float] = None, hi: Optional[float] = None) -> float:
    try:
        out = float(v)
        if out != out:  # NaN
            raise ValueError
    except (TypeError, ValueError):
        out = float(default)
    if lo is not None:
        out = max(lo, out)
    if hi is not None:
        out = min(hi, out)
    return out


def _i(v: Any, default: int, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
    return int(round(_f(v, default, lo, hi)))


def _b(v: Any, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return bool(v)


def _opt_num(v: Any, lo: float, hi: float, as_int: bool = False) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    out = max(lo, min(hi, out))
    return int(round(out)) if as_int else out


_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$")
_HHMM_AMPM = re.compile(r"^(1[0-2]|0?[1-9]):([0-5]\d)\s*([AP]M)$", re.I)


def _hhmm(v: Any, default: str) -> str:
    s = str(v or "").strip()
    m = _HHMM.match(s)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m2 = _HHMM_AMPM.match(s)
    if m2:
        h = int(m2.group(1))
        ap = m2.group(3).upper()
        if ap == "AM" and h == 12:
            h = 0
        elif ap == "PM" and h != 12:
            h += 12
        return f"{h:02d}:{m2.group(2)}"
    return default


def normalize_sound_ref(v: Any, default: str) -> str:
    s = str(v or "").strip()
    if s.startswith("builtin:") and s[8:] in nk_sounds.BUILTIN_SOUNDS:
        return s
    if s.startswith("custom:") and safe_sound_filename(s[7:]) == s[7:] and s[7:]:
        return s
    if s == "none":
        return s
    return default


def safe_sound_filename(name: str) -> str:
    base = os.path.basename(str(name or "").replace("\\", "/"))
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in {".wav", ".mp3"}:
        return ""
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", stem).strip(" ._")[:60]
    return f"{stem}{ext}" if stem else ""


def normalize_address(a: str) -> str:
    a = str(a or "").strip()
    if re.match(r"^(bc1|tb1|bcrt1)", a, re.I):
        return a.lower()
    return a


def _valid_address_shape(a: str) -> bool:
    return bool(re.fullmatch(r"(bc1|tb1|bcrt1)[02-9ac-hj-np-z]{8,87}|[13mn2][1-9A-HJ-NP-Za-km-z]{25,39}", a))


_ALL_DEVICE_TOKENS = {"all", "all machines", "all devices", "*"}


def normalize_device_filter(raw: Any) -> list[str]:
    if isinstance(raw, str):
        items = [x.strip() for x in raw.split(",")]
    elif isinstance(raw, list):
        items = [str(x).strip() for x in raw]
    else:
        return []
    cleaned = [x[:64] for x in items if x]
    if not cleaned or any(x.lower() in _ALL_DEVICE_TOKENS for x in cleaned):
        return []
    return cleaned[:50]


def device_filter_matches(devices: Any, device_name: str, aliases: Optional[list[str]] = None) -> bool:
    filt = normalize_device_filter(devices)
    if not filt:
        return True
    devs = {d.lower() for d in filt}
    names_low = {str(device_name or "").strip().lower()}
    for a in (aliases or []):
        al = str(a or "").strip().lower()
        if al:
            names_low.add(al)
    names_low.discard("")
    return bool(devs & names_low)


def normalize_window(raw: Any, idx: int = 0) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    days_raw = raw.get("days")
    days = sorted({int(d) for d in days_raw if str(d).lstrip("-").isdigit() and 0 <= int(d) <= 6}) if isinstance(days_raw, list) else [0, 1, 2, 3, 4]
    devices = normalize_device_filter(raw.get("devices"))
    wid = re.sub(r"[^A-Za-z0-9_-]", "", str(raw.get("id") or "")) or f"w{idx + 1}"
    return {
        "id": wid[:32],
        "name": str(raw.get("name") or f"Peak window {idx + 1}").strip()[:60],
        "enabled": _b(raw.get("enabled"), True),
        "days": days,
        "start": _hhmm(raw.get("start"), "17:00"),
        "end": _hhmm(raw.get("end"), "21:00"),
        "devices": devices,
        "axe_max_freq_mhz": _opt_num(raw.get("axe_max_freq_mhz"), 100, 850, as_int=True),
        "bos_power_target_w": _opt_num(raw.get("bos_power_target_w"), 300, 6000, as_int=True),
    }


def normalize_app_settings(raw: Any) -> dict[str, Any]:
    d = default_app_settings()
    r = raw if isinstance(raw, dict) else {}

    s = r.get("sounds") if isinstance(r.get("sounds"), dict) else {}
    d["sounds"]["master_mute"] = _b(s.get("master_mute"), False)
    d["sounds"]["volume"] = _f(s.get("volume"), 0.8, 0.0, 1.0)
    d["sounds"]["block_sound"] = normalize_sound_ref(s.get("block_sound"), d["sounds"]["block_sound"])
    d["sounds"]["problem_sound"] = normalize_sound_ref(s.get("problem_sound"), d["sounds"]["problem_sound"])

    n = r.get("notifications") if isinstance(r.get("notifications"), dict) else {}
    d["notifications"]["desktop_toast"] = _b(n.get("desktop_toast"), True)

    a = r.get("alerts") if isinstance(r.get("alerts"), dict) else {}
    da = d["alerts"]
    da["cooldown_min"] = _f(a.get("cooldown_min"), 30, 1, 1440)
    for kind in ALERT_KINDS:
        src = a.get(kind) if isinstance(a.get(kind), dict) else {}
        da[kind]["enabled"] = _b(src.get("enabled"), True)
    da["offline"]["minutes"] = _f((a.get("offline") or {}).get("minutes") if isinstance(a.get("offline"), dict) else None, 3, 0.5, 1440)
    hd = a.get("hashrate_drop") if isinstance(a.get("hashrate_drop"), dict) else {}
    da["hashrate_drop"]["drop_pct"] = _f(hd.get("drop_pct"), 30, 5, 95)
    da["hashrate_drop"]["baseline_min"] = _f(hd.get("baseline_min"), 30, 5, 720)
    da["hashrate_drop"]["sustain_min"] = _f(hd.get("sustain_min"), 5, 1, 120)
    ns = a.get("no_shares") if isinstance(a.get("no_shares"), dict) else {}
    da["no_shares"]["minutes"] = _f(ns.get("minutes"), 30, 1, 1440)
    da["maintenance"] = nk_grace.normalize_settings(a.get("maintenance"))

    b = r.get("block") if isinstance(r.get("block"), dict) else {}
    d["block"]["enabled"] = _b(b.get("enabled"), True)
    addrs_raw = b.get("payout_addresses")
    if isinstance(addrs_raw, str):
        addrs_raw = re.split(r"[\s,;]+", addrs_raw)
    addrs: list[str] = []
    for x in addrs_raw or []:
        na = normalize_address(x)
        if na and _valid_address_shape(na) and na not in addrs:
            addrs.append(na)
    d["block"]["payout_addresses"] = addrs[:20]
    exp = str(b.get("explorer") or "mempool").strip().lower()
    d["block"]["explorer"] = exp if exp in {"mempool", "blockstream", "custom"} else "mempool"
    d["block"]["custom_base_url"] = nk_net.normalize_base_url(str(b.get("custom_base_url") or ""))
    if d["block"]["explorer"] == "custom" and not d["block"]["custom_base_url"]:
        d["block"]["explorer"] = "mempool"
    d["block"]["allow_public_fallback"] = _b(b.get("allow_public_fallback"), True)
    d["block"]["pool_feed"] = _b(b.get("pool_feed"), True)
    d["block"]["notify_unknown_coin"] = _b(b.get("notify_unknown_coin"), True)
    d["block"]["max_messages_per_hour"] = _i(b.get("max_messages_per_hour"), 6, 0, 120)

    p = r.get("power") if isinstance(r.get("power"), dict) else {}
    d["power"]["rate_per_kwh"] = _f(p.get("rate_per_kwh"), 0.15, 0.0, 100.0)
    cur = str(p.get("currency") or "USD").strip().upper()
    d["power"]["currency"] = cur if re.fullmatch(r"[A-Z]{3}", cur) else "USD"
    d["power"]["include_fees"] = _b(p.get("include_fees"), True)

    sc = r.get("schedule") if isinstance(r.get("schedule"), dict) else {}
    d["schedule"]["enabled"] = _b(sc.get("enabled"), False)
    wins = []
    for i, w in enumerate(sc.get("windows") or [] if isinstance(sc.get("windows"), list) else []):
        nw = normalize_window(w, i)
        if nw:
            wins.append(nw)
    seen_ids: set[str] = set()
    for i, w in enumerate(wins):
        if w["id"] in seen_ids:
            w["id"] = f"{w['id']}_{i + 1}"
        seen_ids.add(w["id"])
    d["schedule"]["windows"] = wins[:24]

    lan = r.get("lan") if isinstance(r.get("lan"), dict) else {}
    d["lan"]["enabled"] = _b(lan.get("enabled"), False)
    d["lan"]["port"] = _i(lan.get("port"), 8081, 1024, 65535)
    pin = str(lan.get("pin") or "").strip()
    d["lan"]["pin"] = pin if re.fullmatch(r"\d{4,12}", pin) else ""
    d["lan"]["read_only"] = _b(lan.get("read_only"), True)
    bm = str(lan.get("bind_mode") or "all").strip().lower()
    d["lan"]["bind_mode"] = bm if bm in {"all", "tailscale"} else "all"

    fw = r.get("firmware") if isinstance(r.get("firmware"), dict) else {}
    d["firmware"]["check_enabled"] = _b(fw.get("check_enabled"), True)
    net = r.get("network") if isinstance(r.get("network"), dict) else {}
    d["network"]["enabled"] = _b(net.get("enabled"), True)
    _normalize_v19(d, r)
    d["eventlog"] = nk_eventlog.normalize_settings(r.get("eventlog"))
    d["nodes"] = nk_nodes.normalize_settings(r.get("nodes"))
    return d


def _sec(r: dict[str, Any], key: str) -> dict[str, Any]:
    v = r.get(key)
    return v if isinstance(v, dict) else {}


def _normalize_v19(d: dict[str, Any], r: dict[str, Any]) -> None:
    a = _sec(r, "alerts")
    pr = _sec(a, "pool_rejects")
    d["alerts"]["pool_rejects"]["pct"] = _f(pr.get("pct"), 5.0, 0.5, 50.0)
    d["alerts"]["pool_rejects"]["window_min"] = _f(pr.get("window_min"), 30, 5, 720)
    d["alerts"]["pool_rejects"]["min_shares"] = _i(pr.get("min_shares"), 10, 1, 100000)

    c = _sec(r, "coins")
    ov = {}
    for k, v in (_sec(c, "overrides")).items():
        v = str(v or "").upper()
        if str(k).strip() and v in nk_coins.COINS:
            ov[str(k).strip()[:64]] = v
    d["coins"]["overrides"] = ov
    pa = {}
    for coin, addrs in (_sec(c, "payout_addresses")).items():
        coin = str(coin).upper()
        if coin in nk_coins.COINS and coin != "BTC":
            lst = nk_coins.normalize_coin_addresses(addrs)
            if lst:
                pa[coin] = lst
    d["coins"]["payout_addresses"] = pa
    d["coins"]["fetch_chains"] = _b(c.get("fetch_chains"), True)

    d["pools"]["probe_enabled"] = _b(_sec(r, "pools").get("probe_enabled"), True)
    up = _sec(r, "updates")
    d["updates"]["enabled"] = _b(up.get("enabled"), False)
    d["updates"]["require_signature"] = _b(up.get("require_signature"), False)
    keys_raw = up.get("trusted_keys") or []
    if isinstance(keys_raw, str):
        keys_raw = re.split(r"[\s,;]+", keys_raw)
    keys = []
    for k in keys_raw if isinstance(keys_raw, list) else []:
        nk_ = nk_update.normalize_key(k)
        if nk_ and nk_ not in keys:
            keys.append(nk_)
    d["updates"]["trusted_keys"] = keys[:20]

    ch = _sec(r, "chip_health")
    d["chip_health"]["enabled"] = _b(ch.get("enabled"), True)
    d["chip_health"]["stable_min"] = _f(ch.get("stable_min"), 30, 5, 720)
    d["chip_health"]["wear_mv"] = _f(ch.get("wear_mv"), 20, 5, 200)
    d["chip_health"]["min_days"] = _i(ch.get("min_days"), 14, 1, 365)

    q = _sec(r, "quiet")
    d["quiet"]["enabled"] = _b(q.get("enabled"), False)
    wins = []
    for i, w in enumerate(q.get("windows") if isinstance(q.get("windows"), list) else []):
        nw = normalize_window(w, i)
        if nw:
            nw["name"] = str((w or {}).get("name") or f"Quiet {i + 1}").strip()[:60]
            nw["fan_max_pct"] = _i((w or {}).get("fan_max_pct"), 50, 20, 100)
            nw.pop("bos_power_target_w", None)
            wins.append(nw)
    d["quiet"]["windows"] = wins[:12]

    rs = _sec(r, "restarts")
    wk, hg = _sec(rs, "weekly"), _sec(rs, "hung")
    d["restarts"]["weekly"] = {"enabled": _b(wk.get("enabled"), False), "day": _i(wk.get("day"), 6, 0, 6), "time": _hhmm(wk.get("time"), "04:00")}
    d["restarts"]["hung"] = {"enabled": _b(hg.get("enabled"), False), "minutes": _f(hg.get("minutes"), 15, 5, 1440)}
    d["restarts"]["max_per_day"] = _i(rs.get("max_per_day"), 2, 1, 6)
    d["restarts"]["min_gap_min"] = _f(rs.get("min_gap_min"), 60, 15, 1440)
    d["restarts"]["devices"] = normalize_device_filter(rs.get("devices"))

    wh = _sec(r, "webhooks")
    dw = d["webhooks"]
    dw["enabled"] = _b(wh.get("enabled"), False)
    for k in ("discord_url", "ntfy_url"):
        v = nk_extras.trim_webhook_url(wh.get(k) or "")
        dw[k] = v if re.match(r"^https?://[^\s]+$", v) and len(v) < 500 else ""
    tok = str(wh.get("telegram_token") or "").strip()
    dw["telegram_token"] = tok if re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{10,}", tok) else ""
    chat = str(wh.get("telegram_chat_id") or "").strip()
    dw["telegram_chat_id"] = chat if re.fullmatch(r"-?[0-9]{1,20}|@[A-Za-z0-9_]{4,64}", chat) else ""
    dw["ntfy_token"] = str(wh.get("ntfy_token") or "").strip()[:200]
    types = _sec(wh, "types")
    dw["types"] = {k: _b(types.get(k), k in nk_extras.WEBHOOK_DEFAULT_ON) for k in nk_extras.WEBHOOK_TYPES}

    ui = _sec(r, "ui")
    th = str(ui.get("theme") or "dark").lower()
    d["ui"]["theme"] = th if th in THEMES else "dark"
    d["ui"]["temp_unit"] = "F" if str(ui.get("temp_unit") or "C").upper() == "F" else "C"
    d["ui"]["background"] = _b(ui.get("background"), True)
    d["layout"] = nk_extras.normalize_layout(r.get("layout"))
    _normalize_v110(d, r)


def _normalize_v110(d: dict[str, Any], r: dict[str, Any]) -> None:
    f = _sec(r, "faults")
    df = d["faults"]
    df["enabled"] = _b(f.get("enabled"), True)
    df["probe_enabled"] = _b(f.get("probe_enabled"), True)
    df["cool_asic_c"] = _f(f.get("cool_asic_c"), 45.0, 30.0, 70.0)
    df["cool_vr_c"] = _f(f.get("cool_vr_c"), 65.0, 40.0, 95.0)
    df["min_gap_min"] = _f(f.get("min_gap_min"), 10.0, 5.0, 1440.0)
    df["max_per_day"] = _i(f.get("max_per_day"), 4, 1, 12)
    df["record_hours"] = _f(f.get("record_hours"), 12.0, 1.0, 72.0)
    t = _sec(r, "tuner")
    d["tuner"]["emergency_lockout_min"] = _i(t.get("emergency_lockout_min"), 180, 0, 1440)


# Secret settings: excluded from backups unless "include passwords", hidden from LAN clients.
SECRET_SETTING_PATHS = (("lan", "pin"),) + tuple(("webhooks", k) for k in nk_extras.WEBHOOK_SECRET_KEYS)


def merge_settings(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge a partial patch (one or more sections) into current settings."""
    out = copy.deepcopy(current)

    def rec(dst: dict[str, Any], src: dict[str, Any]) -> None:
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                rec(dst[k], v)
            else:
                dst[k] = copy.deepcopy(v)

    if isinstance(patch, dict):
        rec(out, patch)
    return normalize_app_settings(out)


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class SettingsStore:
    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self.created = False
        raw = _load_json(self.path) if self.path and self.path.exists() else None
        if raw is None:
            self.created = True
        self.data = normalize_app_settings(raw)
        self.migrated_from = None
        if isinstance(raw, dict) and int(raw.get("schema_version") or 1) < SETTINGS_SCHEMA:
            self.migrated_from = int(raw.get("schema_version") or 1)
            if self.path and self.path.exists():  # keep the old file once, then upgrade in place
                try:
                    bak = self.path.with_name(self.path.name + f".v{self.migrated_from}.bak")
                    if not bak.exists():
                        bak.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")
                except OSError:
                    pass
            self.save()
        if self.created:
            self.save()

    def get(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.data)

    def section(self, name: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.data.get(name) or {})

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.data = merge_settings(self.data, patch)
            self.save()
            return copy.deepcopy(self.data)

    def replace(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.data = normalize_app_settings(data)
            self.save()
            return copy.deepcopy(self.data)

    def save(self) -> None:
        if not self.path:
            return
        try:
            _write_json_atomic(self.path, self.data)
        except OSError as exc:
            LOG.warning("Could not save %s: %s", self.path, exc)


def public_settings(s: dict[str, Any], remote: bool = False) -> dict[str, Any]:
    out = copy.deepcopy(s)
    pin = out["lan"].pop("pin", "")
    out["lan"]["has_pin"] = bool(pin)
    if not remote:
        out["lan"]["pin"] = pin
    wh = out.get("webhooks") or {}
    for k in nk_extras.WEBHOOK_SECRET_KEYS:
        wh["has_" + k] = bool(wh.get(k))
        if remote:
            wh[k] = ""
    return out


def strip_secrets(s: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(s)
    for sec, key in SECRET_SETTING_PATHS:
        if isinstance(out.get(sec), dict):
            out[sec].pop(key, None)
    return out


def strip_secrets_deep(s: dict[str, Any]) -> dict[str, Any]:
    """strip_secrets plus every other value that identifies the user (for the event log)."""
    out = strip_secrets(s)
    wh = out.get("webhooks")
    if isinstance(wh, dict):
        for k in list(wh):
            if k not in {"enabled", "types"}:
                wh[k] = "[set]" if wh[k] else ""
    for sec in ("block", "coins"):
        if isinstance(out.get(sec), dict) and "payout_addresses" in out[sec]:
            v = out[sec]["payout_addresses"]
            out[sec]["payout_addresses"] = f"[{len(v)} set]" if v else ""
    return out


def _app_version() -> str:
    try:
        import autotune
        return str(autotune.APP_VERSION)
    except Exception:
        return ""


def _clear_restart_pending(ud: Any) -> None:
    """v1.17.1: forget the saved "Quick restart needed" flag (v1.17.0 kept showing it after the restart)."""
    if not ud:
        return
    try:
        st = nk_update._read_state(Path(ud))
        if st.get("restart_pending"):
            st["restart_pending"] = False
            nk_update._write_state(Path(ud), st)
    except Exception:
        pass


def _shipped_version() -> str:
    """The version of the installed build (its VERSION file), not of a live update running on top of it (v1.17.1)."""
    try:
        import autotune
        return str(getattr(autotune, "SHIPPED_VERSION", "") or autotune.APP_VERSION)
    except Exception:
        return ""


def quiet_limits(
    settings: dict[str, Any],
    device_name: str,
    now_ts: float,
    aliases: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    q = settings.get("quiet") or {}
    if not q.get("enabled"):
        return None
    dt = datetime.fromtimestamp(now_ts)
    out: dict[str, Any] = {"fan_max_pct": None, "max_freq": None, "windows": []}
    for w in q.get("windows") or []:
        if not device_filter_matches(w.get("devices"), device_name, aliases=aliases):
            continue
        if not window_active(w, dt):
            continue
        out["windows"].append(w["name"])
        for key, src in (("fan_max_pct", "fan_max_pct"), ("max_freq", "axe_max_freq_mhz")):
            v = w.get(src)
            if v is not None:
                out[key] = v if out[key] is None else min(out[key], v)
    return out if out["windows"] else None


# ---------------------------------------------------------------------------
# Time-of-use schedule
# ---------------------------------------------------------------------------

def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def window_active(w: dict[str, Any], dt: datetime) -> bool:
    if not w.get("enabled", True):
        return False
    days = set(w.get("days") or [])
    s, e = _mins(w["start"]), _mins(w["end"])
    m = dt.hour * 60 + dt.minute
    wd = dt.weekday()
    if s == e:
        return wd in days
    if s < e:
        return wd in days and s <= m < e
    # overnight window: starts on a listed day, ends next morning
    return (wd in days and m >= s) or (((wd - 1) % 7) in days and m < e)


def _mins_until_end(w: dict[str, Any], dt: datetime) -> int:
    s, e = _mins(w["start"]), _mins(w["end"])
    m = dt.hour * 60 + dt.minute
    if s == e:
        return 1440 - m
    if s < e:
        return max(1, e - m)
    return (e - m) if m < e else (1440 - m + e)


def _format_cap_parts(max_freq: Any, bos_power_target_w: Any) -> str:
    parts = []
    if max_freq is not None:
        parts.append(f"cap {int(round(float(max_freq)))} MHz")
    if bos_power_target_w is not None:
        parts.append(f"cap {int(round(float(bos_power_target_w)))} W")
    return ", ".join(parts) if parts else "no cap"


def schedule_limits(
    settings: dict[str, Any],
    device_name: str,
    now_ts: float,
    aliases: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    sc = settings.get("schedule") or {}
    if not sc.get("enabled"):
        return None
    dt = datetime.fromtimestamp(now_ts)
    out: dict[str, Any] = {"max_freq": None, "bos_power_target_w": None, "windows": [], "until": None}
    best_rem: Optional[int] = None
    for w in sc.get("windows") or []:
        if not device_filter_matches(w.get("devices"), device_name, aliases=aliases):
            continue
        if not window_active(w, dt):
            continue
        out["windows"].append(w["name"])
        rem = _mins_until_end(w, dt)
        if best_rem is None or rem < best_rem:
            best_rem = rem
            out["until"] = w.get("end")
        for key, src in (("max_freq", "axe_max_freq_mhz"), ("bos_power_target_w", "bos_power_target_w")):
            v = w.get(src)
            if v is not None and v != "":
                try:
                    num = int(round(float(v)))
                except (TypeError, ValueError):
                    continue
                out[key] = num if out[key] is None else min(out[key], num)
    return out if out["windows"] else None


def schedule_summary(settings: dict[str, Any], now_ts: float) -> dict[str, Any]:
    sc = settings.get("schedule") or {}
    enabled = bool(sc.get("enabled"))
    windows = [w for w in (sc.get("windows") or []) if w.get("enabled", True)]
    if not enabled:
        return {"enabled": False, "state": "disabled", "windows": [], "max_freq": None,
                "bos_power_target_w": None, "until": None,
                "next_window": None, "next_start": None, "next_until": None,
                "next_max_freq": None, "label": "TOU: disabled"}
    if not windows:
        return {"enabled": True, "state": "no_windows", "windows": [], "max_freq": None,
                "bos_power_target_w": None, "until": None,
                "next_window": None, "next_start": None, "next_until": None,
                "next_max_freq": None, "label": "TOU: enabled (no active windows configured)"}
    dt = datetime.fromtimestamp(now_ts)
    active_names: list[str] = []
    max_freq = bos_power_target_w = None
    until = None
    best_rem: Optional[int] = None
    for w in windows:
        if not window_active(w, dt):
            continue
        active_names.append(w["name"])
        rem = _mins_until_end(w, dt)
        if best_rem is None or rem < best_rem:
            best_rem = rem
            until = w.get("end")
        if w.get("axe_max_freq_mhz") not in (None, ""):
            try:
                v = int(round(float(w["axe_max_freq_mhz"])))
                max_freq = v if max_freq is None else min(max_freq, v)
            except (TypeError, ValueError):
                pass
        if w.get("bos_power_target_w") not in (None, ""):
            try:
                v = int(round(float(w["bos_power_target_w"])))
                bos_power_target_w = v if bos_power_target_w is None else min(bos_power_target_w, v)
            except (TypeError, ValueError):
                pass
    if active_names:
        cap = _format_cap_parts(max_freq, bos_power_target_w)
        win_txt = ", ".join(active_names)
        until_txt = f" until {until}" if until else ""
        return {
            "enabled": True, "state": "active", "windows": active_names,
            "max_freq": max_freq, "bos_power_target_w": bos_power_target_w,
            "until": until, "next_window": None, "next_start": None, "next_until": None, "next_max_freq": None,
            "label": f"TOU: {win_txt} · {cap}{until_txt}",
        }
    # Off-peak: find soonest upcoming window within the next 7 days
    now_wd = dt.weekday()
    now_m = dt.hour * 60 + dt.minute
    best_wait: Optional[int] = None
    nxt: Optional[dict[str, Any]] = None
    for w in windows:
        days = set(w.get("days") or [])
        s_m = _mins(w["start"])
        for delta in range(0, 8):
            wd = (now_wd + delta) % 7
            if wd not in days:
                continue
            wait = delta * 1440 + (s_m - now_m)
            if wait <= 0:
                continue
            if best_wait is None or wait < best_wait:
                best_wait = wait
                nxt = w
            break
    if nxt is not None:
        ncap = _format_cap_parts(nxt.get("axe_max_freq_mhz"), nxt.get("bos_power_target_w"))
        return {
            "enabled": True, "state": "off_peak", "windows": [],
            "max_freq": None, "bos_power_target_w": None, "until": None,
            "next_window": nxt.get("name"), "next_start": nxt.get("start"), "next_until": nxt.get("end"),
            "next_max_freq": nxt.get("axe_max_freq_mhz"),
            "label": f"TOU: off-peak (next: {nxt.get('name')} at {nxt.get('start')}, {ncap} until {nxt.get('end')})",
        }
    return {"enabled": True, "state": "off_peak", "windows": [],
            "max_freq": None, "bos_power_target_w": None, "until": None,
            "next_window": None, "next_start": None, "next_until": None, "next_max_freq": None,
            "label": "TOU: off-peak"}


# ---------------------------------------------------------------------------
# Tuning log (JSONL, rotated)
# ---------------------------------------------------------------------------

class TuningLog:
    def __init__(self, path: Optional[Path], max_bytes: int = 1_000_000, keep: int = 3, memory: int = 1000):
        self.path = Path(path) if path else None
        self.max_bytes = int(max_bytes)
        self.keep = int(keep)
        self._lock = threading.RLock()
        self.entries: deque = deque(maxlen=memory)
        self._load_tail()

    def _load_tail(self) -> None:
        if not self.path:
            return
        files = [self.path.with_name(f"{self.path.name}.{i}") for i in range(self.keep, 0, -1)] + [self.path]
        for p in files:
            if not p.exists():
                continue
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    try:
                        row = json.loads(line)
                        if isinstance(row, dict):
                            self.entries.append(row)
                    except ValueError:
                        continue
            except OSError:
                continue

    def _rotate(self) -> None:
        assert self.path is not None
        for i in range(self.keep, 0, -1):
            src = self.path if i == 1 else self.path.with_name(f"{self.path.name}.{i - 1}")
            dst = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                try:
                    src.replace(dst)
                except OSError:
                    pass

    def add(self, entry: dict[str, Any]) -> dict[str, Any]:
        row = dict(entry)
        row.setdefault("ts", time.time())
        row.setdefault("iso", datetime.fromtimestamp(row["ts"]).astimezone().isoformat(timespec="seconds"))
        with self._lock:
            self.entries.append(row)
            if self.path:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                        self._rotate()
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(row, default=str) + "\n")
                except OSError as exc:
                    LOG.warning("Could not write tuning log: %s", exc)
        return row

    def list(self, device: str = "", limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = [r for r in self.entries if not device or r.get("device") == device]
        return list(reversed(rows[-max(1, int(limit)):]))

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            for r in self.entries:
                if r.get("device") == old:
                    r["device"] = new

    def remove(self, name: str) -> None:
        with self._lock:
            self.entries = deque((r for r in self.entries if r.get("device") != name), maxlen=self.entries.maxlen)


# ---------------------------------------------------------------------------
# Long history (7d @ 5 min, 30d @ 30 min)
# ---------------------------------------------------------------------------

TIERS = {"7d": (300, 7 * 86400), "30d": (1800, 30 * 86400)}


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "device").strip()).strip("._")
    return (cleaned or "device") + ".json"


class LongHistory:
    """Downsampled per-device history: rows are [bucket_ts, hashrate_ghs, asic_c, vr_c, power_w]."""

    def __init__(self, directory: Optional[Path], save_every_sec: float = 300.0):
        self.dir = Path(directory) if directory else None
        self.save_every = float(save_every_sec)
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, list[list[Any]]]] = {}
        self._counts: dict[tuple[str, str], list[int]] = {}
        self._last_save: dict[str, float] = {}

    def _path(self, name: str) -> Optional[Path]:
        return (self.dir / _safe_name(name)) if self.dir else None

    def exists(self, name: str) -> bool:
        p = self._path(name)
        return bool(p and p.exists())

    def load(self, name: str) -> dict[str, list[list[Any]]]:
        with self._lock:
            if name in self._data:
                return self._data[name]
            tiers: dict[str, list[list[Any]]] = {k: [] for k in TIERS}
            p = self._path(name)
            raw = _load_json(p) if p and p.exists() else None
            if isinstance(raw, dict) and isinstance(raw.get("tiers"), dict):
                for k in TIERS:
                    rows = raw["tiers"].get(k)
                    if isinstance(rows, list):
                        tiers[k] = [list(r)[:5] + [None] * (5 - len(r[:5])) for r in rows if isinstance(r, list) and r]
            self._data[name] = tiers
            return tiers

    def record(self, name: str, ts: float, hr: Optional[float], asic: Optional[float], vr: Optional[float], power: Optional[float], save: bool = True) -> None:
        if hr is None and asic is None and power is None:
            return
        vals = [hr, asic, vr, power]
        with self._lock:
            tiers = self.load(name)
            for tier, (bucket, keep) in TIERS.items():
                rows = tiers[tier]
                bts = float(int(ts // bucket) * bucket)
                if rows and rows[-1][0] == bts:
                    row = rows[-1]
                    counts = self._counts.setdefault((name, tier), [1, 1, 1, 1])
                    for i, v in enumerate(vals):
                        if v is None:
                            continue
                        old = row[i + 1]
                        if old is None:
                            row[i + 1] = round(float(v), 3)
                            counts[i] = 1
                        else:
                            counts[i] += 1
                            row[i + 1] = round(old + (float(v) - old) / counts[i], 3)
                else:
                    rows.append([bts] + [round(float(v), 3) if v is not None else None for v in vals])
                    self._counts[(name, tier)] = [1, 1, 1, 1]
                cutoff = ts - keep
                if rows and rows[0][0] < cutoff:
                    tiers[tier] = [r for r in rows if r[0] >= cutoff]
            if save and (ts - self._last_save.get(name, 0.0)) >= self.save_every:
                self.save(name)
                self._last_save[name] = ts

    def save(self, name: str) -> None:
        p = self._path(name)
        if not p:
            return
        with self._lock:
            data = {"schema_version": 1, "device": name, "tiers": self.load(name)}
            try:
                _write_json_atomic(p, data)
            except OSError as exc:
                LOG.warning("Could not save long history %s: %s", p, exc)

    def save_all(self) -> None:
        with self._lock:
            for name in list(self._data):
                self.save(name)

    def seed_from_points(self, name: str, points: list[list[Any]]) -> int:
        """Seed from v1.7 24h history rows [ts, hr, asic, vr] (migration)."""
        n = 0
        for row in points or []:
            try:
                self.record(name, float(row[0]), row[1], row[2], row[3] if len(row) > 3 else None, None, save=False)
                n += 1
            except (TypeError, ValueError, IndexError):
                continue
        if n:
            self.save(name)
        return n

    def series(self, name: str, tier: str) -> list[list[Any]]:
        with self._lock:
            return [list(r) for r in self.load(name).get(tier, [])]

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            data = self.load(old)
            self._data.pop(old, None)
            self._data[new] = data
            op, np_ = self._path(old), self._path(new)
            if op and op.exists():
                try:
                    op.unlink()
                except OSError:
                    pass
            self.save(new)

    def remove(self, name: str) -> None:
        with self._lock:
            self._data.pop(name, None)
            self._last_save.pop(name, None)
            op = self._path(name)
            if op and op.exists():
                try:
                    op.unlink()
                except OSError:
                    pass


def fleet_series(per_device: dict[str, list[list[Any]]]) -> list[list[Any]]:
    """Sum hashrate & power per bucket, max temps."""
    buckets: dict[float, list[Any]] = {}
    for rows in per_device.values():
        for r in rows:
            ts = float(r[0])
            b = buckets.setdefault(ts, [ts, None, None, None, None])
            for i, mode in ((1, "sum"), (2, "max"), (3, "max"), (4, "sum")):
                v = r[i] if i < len(r) else None
                if v is None:
                    continue
                if b[i] is None:
                    b[i] = float(v)
                elif mode == "sum":
                    b[i] = round(b[i] + float(v), 3)
                else:
                    b[i] = max(b[i], float(v))
    return [buckets[k] for k in sorted(buckets)]


def rows_to_csv(rows: list[list[Any]], device: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["timestamp_local", "unix_ts", "device", "hashrate_ghs", "asic_temp_c", "vr_temp_c", "power_w"])
    for r in rows:
        r = list(r) + [None] * (5 - len(r))
        ts = float(r[0])
        w.writerow([
            datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds"),
            int(ts),
            device,
            *["" if v is None else v for v in r[1:5]],
        ])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def _is_on_fallback_pool(reading: Any) -> bool:
    if getattr(reading, "using_fallback_stratum", False):
        return True
    pools = [
        p for p in (getattr(reading, "pools", None) or [])
        if isinstance(p, dict) and not p.get("disabled") and str(p.get("status") or "").strip().lower() != "disabled"
    ]
    if pools:
        prios = [p.get("priority") for p in pools if p.get("priority") is not None]
        active = [p for p in pools if p.get("stratum_active")]
        if active and prios and active[0].get("priority") is not None:
            return int(active[0]["priority"]) > min(int(x) for x in prios)
    return False


class AlertManager:
    """Stateful per-device problem detection with per-(device, kind) cooldowns."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.dev: dict[str, dict[str, Any]] = {}
        self.last_fired: dict[tuple[str, str], float] = {}

    def _st(self, name: str) -> dict[str, Any]:
        return self.dev.setdefault(name, {
            "offline_since": None, "ever_online": False, "active": set(), "hr": deque(maxlen=2000),
            "acc": None, "acc_change_ts": None,
        })

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            if old in self.dev:
                self.dev[new] = self.dev.pop(old)
            for (d, k) in list(self.last_fired):
                if d == old:
                    self.last_fired[(new, k)] = self.last_fired.pop((d, k))

    def remove(self, name: str) -> None:
        with self._lock:
            self.dev.pop(name, None)
            for k in [key for key in list(self.last_fired) if key[0] == name]:
                self.last_fired.pop(k, None)

    def _fire(self, name: str, kind: str, now: float, cooldown_sec: float) -> bool:
        st = self._st(name)
        if kind in st["active"]:
            return False
        st["active"].add(kind)
        last = self.last_fired.get((name, kind))
        if last is not None and now - last < cooldown_sec:
            return False  # active, but still in cooldown since the last notification
        self.last_fired[(name, kind)] = now
        return True

    def _clear(self, name: str, kind: str) -> bool:
        st = self._st(name)
        if kind in st["active"]:
            st["active"].discard(kind)
            return True
        return False

    def evaluate(self, name: str, reading: Any, settings: dict[str, Any], now: float,
                 suppress: Optional[Callable[[str], str]] = None) -> list[dict[str, Any]]:
        a = settings.get("alerts") or {}
        cooldown = float(a.get("cooldown_min", 30)) * 60.0
        events: list[dict[str, Any]] = []
        online = bool(getattr(reading, "ok", False))

        def fire(kind: str) -> bool:
            # v1.15.5: an expected transition (pool switch, NK restart, maintenance pause) never marks the alert active
            if suppress is not None and suppress(kind):
                return False
            return self._fire(name, kind, now, cooldown)
        with self._lock:
            st = self._st(name)
            # Offline
            if not online:
                if st["offline_since"] is None:
                    st["offline_since"] = now
                off = a.get("offline") or {}
                if off.get("enabled") and (now - st["offline_since"]) >= float(off.get("minutes", 3)) * 60.0:
                    if fire("offline"):
                        mins = (now - st["offline_since"]) / 60.0
                        events.append(_event("offline", "problem", name, f"{name} is offline",
                                             f"No usable telemetry for {mins:.0f} min ({getattr(reading, 'error', '') or 'unreachable'})."))
                return events
            if st["offline_since"] is not None and self._clear(name, "offline"):
                events.append(_event("recovered", "info", name, f"{name} is back online", "Telemetry is flowing again.", sound=False))
            st["offline_since"] = None
            st["ever_online"] = True

            # Hashrate drop
            hr = getattr(reading, "hashrate", None)
            hd = a.get("hashrate_drop") or {}
            if hr is not None and hr >= 0:
                st["hr"].append((now, float(hr)))
            if hd.get("enabled") and st["hr"]:
                sustain = float(hd.get("sustain_min", 5)) * 60.0
                base_win = float(hd.get("baseline_min", 30)) * 60.0
                recent = [v for t, v in st["hr"] if t >= now - sustain]
                base = [v for t, v in st["hr"] if now - sustain - base_win <= t < now - sustain]
                span = (now - st["hr"][0][0])
                if recent and len(base) >= 3 and span >= sustain + base_win * 0.5:
                    b_avg = sum(base) / len(base)
                    r_avg = sum(recent) / len(recent)
                    limit = b_avg * (1.0 - float(hd.get("drop_pct", 30)) / 100.0)
                    if b_avg > 0 and r_avg < limit and max(recent) < limit:
                        if fire("hashrate_drop"):
                            pct = (1.0 - r_avg / b_avg) * 100.0
                            events.append(_event("hashrate_drop", "problem", name, f"{name}: hashrate dropped {pct:.0f}%",
                                                 f"{r_avg:,.1f} GH/s over the last {sustain / 60:.0f} min vs {b_avg:,.1f} GH/s baseline."))
                    elif b_avg > 0 and r_avg >= b_avg * (1.0 - float(hd.get("drop_pct", 30)) / 200.0):
                        self._clear(name, "hashrate_drop")
                # prune
                while st["hr"] and st["hr"][0][0] < now - sustain - base_win - 600:
                    st["hr"].popleft()

            # Fallback pool
            fb = a.get("fallback_pool") or {}
            if _is_on_fallback_pool(reading):
                if fb.get("enabled") and fire("fallback_pool"):
                    url = getattr(reading, "fallback_pool_url", "") or "fallback pool"
                    events.append(_event("fallback_pool", "problem", name, f"{name} switched to its fallback pool",
                                         f"Now mining on {url}. Check the primary pool connection."))
            else:
                self._clear(name, "fallback_pool")

            # No shares for N minutes
            ns = a.get("no_shares") or {}
            limit_s = float(ns.get("minutes", 30)) * 60.0
            acc = getattr(reading, "shares_accepted", None)
            if acc is not None:
                if st["acc"] is None or acc != st["acc"]:
                    st["acc"] = acc
                    st["acc_change_ts"] = now
            last_share_s = getattr(reading, "last_share_seconds", None)
            estimated = bool(getattr(reading, "last_share_estimated", False))
            idle: Optional[float] = None
            if last_share_s is not None and not estimated:
                idle = float(last_share_s)
            elif st["acc_change_ts"] is not None:
                idle = now - st["acc_change_ts"]
            if idle is not None and idle >= limit_s:
                if ns.get("enabled") and fire("no_shares"):
                    events.append(_event("no_shares", "problem", name, f"{name}: no shares for {idle / 60:.0f} min",
                                         "The miner is online but no accepted shares were seen. Check pool, network or hashboards."))
            elif idle is not None:
                self._clear(name, "no_shares")
        return events


_EVENT_SEQ = [0]
_EVENT_LOCK = threading.Lock()


def _event(kind: str, level: str, device: str, title: str, message: str, sound: bool = True, **extra: Any) -> dict[str, Any]:
    with _EVENT_LOCK:
        base = int(time.time() * 1000)
        _EVENT_SEQ[0] = max(_EVENT_SEQ[0] + 1, base)
        eid = _EVENT_SEQ[0]
    ev = {
        "id": eid, "ts": time.time(), "kind": kind, "level": level, "device": device,
        "title": title, "message": message, "sound": ("block" if level == "block" else "problem") if sound else None,
    }
    ev["iso"] = datetime.fromtimestamp(ev["ts"]).astimezone().isoformat(timespec="seconds")
    ev.update(extra)
    return ev


# ---------------------------------------------------------------------------
# Block-found watcher
# ---------------------------------------------------------------------------

DIFF_FIELDS = ("best_session_diff", "best_diff", "last_submitted_diff")
# v1.15.5: average seconds per block height, to know how far back to look after a polling gap.
BLOCK_TIME_SEC = {"BTC": 600, "BCH": 600, "BSV": 600, "XEC": 600, "FB": 30, "DGB": 15, "LTC": 150, "DOGE": 60, "KAS": 1,
                  "ALPH": 16, "CKB": 10, "ETC": 13, "ZEC": 75, "DASH": 150, "HNS": 600,
                  "CHTA": 60}


# v1.16.1: the share that solved a block hashes to the block hash, so its difficulty is diff-1 target / hash
# (SHA-256d chains only). Used when neither the pool nor the miner reports the winning share's difficulty.
DIFF1_TARGET = 0xFFFF * 2 ** 208
SHARE_SRC_PRIORITY = {"pool": 0, "miner_last_share": 1, "block_hash": 2}


def share_diff_from_block_hash(block_hash: Any, coin: Any) -> Optional[float]:
    """Difficulty of the winning share from the block hash (None when the coin is not SHA-256d or the hash is not usable)."""
    h = str(block_hash or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", h):
        return None
    if str((nk_coins.COINS.get(str(coin or "")) or {}).get("algo") or "") != "sha256d":
        return None
    n = int(h, 16)
    if n <= 0:
        return None
    return DIFF1_TARGET / n


def set_winning_share(c: dict[str, Any], value: Any, src: str) -> bool:
    """v1.16.1: record the difficulty of the share that found this block. Never a best / session-best value.
    Better sources (pool > miner last share > block hash) replace worse ones until the first alert has shown
    the value; after that it stays as shown (an unknown value may still be filled in)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if not (v > 0) or v != v or v == float("inf"):
        return False
    cur_src = c.get("share_src")
    if cur_src and c.get("share_diff"):
        if c.get("share_locked"):
            return False
        if SHARE_SRC_PRIORITY.get(src, 9) >= SHARE_SRC_PRIORITY.get(str(cur_src), 9):
            return False
    c["share_diff"] = v
    c["share_src"] = src
    return True


def winning_share(c: dict[str, Any]) -> tuple[Optional[float], str]:
    """(difficulty, source) of the winning share; (None, '') when unknown. Candidates saved by older versions
    (no share_src) held best-difficulty values, so their share_diff is ignored."""
    if c.get("share_src") and c.get("share_diff"):
        try:
            v = float(c["share_diff"])
            if v > 0:
                return v, str(c["share_src"])
        except (TypeError, ValueError):
            pass
    v = share_diff_from_block_hash(c.get("block_hash"), c.get("coin"))
    nd = float(c.get("network_difficulty") or 0)
    if v and nd and v < 0.5 * nd:  # a real block hash always meets the network difficulty: not this coin's hash
        v = None
    if v and set_winning_share(c, v, "block_hash"):
        return v, "block_hash"
    return None, ""


def _dedupe_window(coin: str) -> float:
    """v1.15.5: one candidate per block. Slow chains (BTC-like): one per 30 min per machine (noisy best-diff fields).
    Fast / low-difficulty chains (CheetahCoin, DGB ...): every new winning share or counter step is its own block."""
    bt = float(BLOCK_TIME_SEC.get(str(coin or "BTC"), 600))
    return 1800.0 if bt >= 300 else 0.0


class BlockWatcher:
    """Turns 'share difficulty >= network difficulty' triggers into confirmed blocks.

    A trigger alone never alerts. Confirmation requires a block mined around the
    trigger time whose coinbase transaction pays one of the configured payout
    addresses, as reported by a public (or the user's own) block explorer.
    Firmware 'found blocks' counters are deliberately ignored.
    """

    CONFIRM_DEPTH = 6          # give up after this many blocks past the trigger
    TIME_WINDOW_SEC = 3 * 3600  # block timestamps may be off by up to ~2h
    EXPIRE_SEC = 48 * 3600
    MAX_LOOKUPS_PER_CHECK = 12

    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self.baselines: dict[str, dict[str, float]] = {}
        self.candidates: list[dict[str, Any]] = []
        self.seen: dict[str, float] = {}
        raw = _load_json(self.path) if self.path and self.path.exists() else None
        if isinstance(raw, dict):
            if isinstance(raw.get("baselines"), dict):
                self.baselines = {str(k): {f: float(v) for f, v in (d or {}).items() if isinstance(v, (int, float))} for k, d in raw["baselines"].items() if isinstance(d, dict)}
            if isinstance(raw.get("candidates"), list):
                self.candidates = [c for c in raw["candidates"] if isinstance(c, dict)][-50:]
            if isinstance(raw.get("seen"), dict):
                self.seen = {str(k): float(v) for k, v in raw["seen"].items() if isinstance(v, (int, float))}

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            try:
                _write_json_atomic(self.path, {"schema_version": 1, "baselines": self.baselines, "candidates": self.candidates[-50:], "seen": self.seen})
            except OSError as exc:
                LOG.warning("Could not save block watcher state: %s", exc)

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            if old in self.baselines:
                self.baselines[new] = self.baselines.pop(old)
            if old in self.seen:
                self.seen[new] = self.seen.pop(old)
            for c in self.candidates:
                if c.get("device") == old:
                    c["device"] = new
            self.save()

    def remove(self, name: str) -> None:
        with self._lock:
            changed = name in self.baselines or any(c.get("device") == name for c in self.candidates)
            self.baselines.pop(name, None)
            self.seen.pop(name, None)
            self.candidates = [c for c in self.candidates if c.get("device") != name]
            if changed:
                self.save()

    def observe(self, device: str, diffs: dict[str, Optional[float]], network_difficulty: Optional[float], tip_height: Optional[int], now: float,
                coin: str = "BTC", found_blocks: Optional[int] = None, coin_diffs: Optional[dict[str, float]] = None,
                new_block_flag: Optional[bool] = None, last_share_actual: bool = True) -> Optional[dict[str, Any]]:
        """Record share difficulties / firmware found-block counters; returns a new candidate when a block may have been found.

        v1.15.5: three triggers, any one is enough:
          * a share-difficulty field moved to a value >= network difficulty (as before);
          * the firmware's own found-block counter went up (catches the 2nd+ block of a session, when the
            session / all-time best is already above network difficulty and so never moves again);
          * coin_diffs: other chains this miner may really be on (job difficulty, or pool host when a manual
            coin override disagrees with it) - the lowest difficulty met picks the coin.

        v1.16.1: best / session-best values only trigger (and pick the coin); the alert's share difficulty is
        the miner's last-share difficulty from this same poll, and only when it is new and meets the network
        difficulty (that share is the winner). last_share_actual=False: the firmware's "last share difficulty"
        is the pool's share target (CGMiner), not the share's own difficulty, so it is never used.
        """
        with self._lock:
            base = self.baselines.setdefault(device, {})
            prev_seen = self.seen.get(device)
            self.seen[device] = now
            diffs_by_coin: dict[str, float] = {}
            if network_difficulty:
                diffs_by_coin[coin] = float(network_difficulty)
            for c_, d_ in (coin_diffs or {}).items():
                if d_ and c_ not in diffs_by_coin:
                    diffs_by_coin[c_] = float(d_)
            threshold = min(diffs_by_coin.values()) if diffs_by_coin else None
            trigger_val: Optional[float] = None
            trigger_field = ""
            last_new: Optional[float] = None
            changed = False
            for f in DIFF_FIELDS:
                v = diffs.get(f)
                if v is None or not isinstance(v, (int, float)) or v <= 0:
                    continue
                v = float(v)
                prev = base.get(f)
                if prev is None or v != prev:
                    base[f] = v
                    changed = True
                if prev is None:
                    continue  # first sighting is only a baseline (could be an old share)
                if f == "last_submitted_diff":
                    is_new = v != prev
                    if is_new and last_share_actual:
                        last_new = v
                else:
                    is_new = v > prev
                if is_new and threshold and v >= threshold:
                    if trigger_val is None or v > trigger_val:
                        trigger_val, trigger_field = v, f
            counter_hit = False
            if isinstance(found_blocks, int) and found_blocks >= 0:
                prev_fb = base.get("found_blocks")
                if prev_fb is None or float(found_blocks) != prev_fb:
                    base["found_blocks"] = float(found_blocks)
                    changed = True
                if prev_fb is not None and found_blocks > prev_fb:
                    counter_hit = True
            if isinstance(new_block_flag, bool):  # AxeOS showNewBlock: rising edge = the firmware saw a block (blockFound is 0/1 per boot)
                prev_nb = base.get("new_block_flag")
                if prev_nb is None or float(new_block_flag) != prev_nb:
                    base["new_block_flag"] = float(new_block_flag)
                    changed = True
                if prev_nb is not None and new_block_flag and not prev_nb:
                    counter_hit = True
            cand = None
            if trigger_val is not None or counter_hit:
                live = {"pending", "possible", "confirmed"}
                window = _dedupe_window(coin)
                mine = [c for c in self.candidates if c.get("device") == device and c.get("status") in live]
                recent = [c for c in mine if now - float(c.get("trigger_ts", 0)) < window]
                if window == 0.0 and mine:
                    last = mine[-1]
                    # same block seen twice: counter / banner catching up one poll after the share (or vice versa)
                    if now - float(last.get("trigger_ts", 0)) <= 90 and last.get("trigger") in ("share", "found_blocks") and \
                            ((counter_hit and trigger_val is None and last.get("trigger") == "share" and not last.get("counter_seen")) or
                             (trigger_val is not None and not counter_hit and last.get("trigger") == "found_blocks" and not last.get("share_seen"))):
                        last["counter_seen" if counter_hit else "share_seen"] = True
                        nd = float(last.get("network_difficulty") or threshold or 0)
                        if last_new is not None and nd and last_new >= nd:
                            set_winning_share(last, last_new, "miner_last_share")
                        recent = [last]
                if not recent:
                    pool = self._pool_match(device, prev_seen, now)
                    if pool is not None:  # the pool already reported this block: same block, no second candidate
                        pool["device_trigger"] = "share" if trigger_val is not None else "found_blocks"
                        self.save()
                        return None
                    share = trigger_val  # only for picking the coin; never shown as the share difficulty
                    if share is None:  # counter only: a winning last share, else the session best says which chain it could be
                        share = last_new if last_new is not None and threshold and last_new >= threshold else \
                            max([float(v) for f, v in diffs.items() if isinstance(v, (int, float)) and f != "last_submitted_diff"] or [0.0])
                    met = [c_ for c_, d_ in diffs_by_coin.items() if share and d_ <= share]
                    cand_coin = coin if (coin in met or not met) else met[0]
                    cand = {
                        "id": f"{device}-{int(now)}",
                        "device": device,
                        "coin": cand_coin,
                        "share_diff": None,
                        "trigger_diff": float(share or 0.0),
                        "field": trigger_field or "found_blocks",
                        "trigger": "share" if trigger_val is not None else "found_blocks",
                        "found_blocks": found_blocks if counter_hit else None,
                        "network_difficulty": float(diffs_by_coin.get(cand_coin) or network_difficulty or 0),
                        "trigger_ts": now,
                        # last poll before this one: the share was found somewhere in between (long gaps when NK was closed)
                        "since_ts": float(prev_seen) if prev_seen and prev_seen < now else now,
                        "tip_at_trigger": int(tip_height) if tip_height and cand_coin == coin else None,
                        "status": "pending",
                        "checked_heights": [],
                        "notified": [],
                        "note": "",
                    }
                    if cand_coin != coin:
                        cand["note"] = f"share met the {cand_coin} network difficulty (machine is set to {coin})"
                    if trigger_val is not None and counter_hit:
                        cand["counter_seen"] = True
                    cand["share_seen"] = trigger_val is not None
                    nd = float(cand["network_difficulty"] or threshold or 0)
                    if last_new is not None and nd and last_new >= nd:
                        set_winning_share(cand, last_new, "miner_last_share")
                    cand["id"] = self._unique_id(cand["id"])
                    self.candidates.append(cand)
                    changed = True
            if changed or prev_seen is None or now - float(prev_seen) > 300:
                self.save()
            return cand

    def _unique_id(self, cid: str) -> str:
        ids = {c.get("id") for c in self.candidates}
        if cid not in ids:
            return cid
        n = 2
        while f"{cid}-{n}" in ids:
            n += 1
        return f"{cid}-{n}"

    def _pool_match(self, device: str, prev_seen: Optional[float], now: float) -> Optional[dict[str, Any]]:
        lo = (float(prev_seen) if prev_seen else now - 300) - 180
        for c in reversed(self.candidates):
            if c.get("device") == device and c.get("trigger") == "pool" and not c.get("device_trigger") \
                    and lo <= float(c.get("block_ts") or c.get("trigger_ts") or 0) <= now + 60:
                return c
        return None

    def claimed_blocks(self, coin: str) -> set[int]:
        with self._lock:
            return {int(c["block_height"]) for c in self.candidates
                    if str(c.get("coin") or "BTC") == coin and c.get("block_height") is not None and c.get("status") in {"confirmed", "pending", "possible"}}

    def attach_pool_block(self, device: str, b: dict[str, Any], now: float) -> tuple[Optional[dict[str, Any]], str]:
        """v1.15.5: a block the pool says this machine found. Returns (candidate, 'possible'|'confirmed'|'orphaned'|'').
        Reuses the miner-side candidate for the same block when there is one (one notification per block)."""
        coin, h, state = str(b.get("coin") or ""), int(b["height"]), str(b.get("state") or "pending")
        bts = float(b.get("ts") or now)
        with self._lock:
            cand = None
            for c in self.candidates:
                if str(c.get("coin") or "") == coin and c.get("block_height") is not None and int(c["block_height"]) == h:
                    cand = c
                    break
            if cand is None:
                for c in reversed(self.candidates):
                    if (c.get("device") == device and c.get("block_height") is None and c.get("trigger") != "pool"
                            and c.get("status") in {"pending", "possible", "unverified", "rejected"}
                            and str(c.get("coin") or "") in {coin, nk_coins.UNKNOWN}
                            and float(c.get("since_ts") or c.get("trigger_ts") or 0) - 180 <= bts <= float(c.get("trigger_ts") or 0) + 120):
                        cand = c
                        break
            if cand is None:
                cand = {"id": self._unique_id(f"{device}-pool-{coin}-{h}"), "device": device, "coin": coin, "share_diff": None,
                        "field": "pool", "trigger": "pool", "found_blocks": None, "network_difficulty": float(b.get("netdiff") or 0),
                        "trigger_ts": now, "since_ts": bts, "tip_at_trigger": h, "status": "pending", "checked_heights": [],
                        "notified": [], "note": ""}
                self.candidates.append(cand)
            cand["coin"] = coin
            cand["block_height"] = h
            cand["block_ts"] = bts
            if b.get("hash"):
                cand["block_hash"] = str(b["hash"])
            if b.get("netdiff") and not cand.get("network_difficulty"):
                cand["network_difficulty"] = float(b["netdiff"])
            set_winning_share(cand, b.get("diff"), "pool")  # v1.16.1: the pool's block entry has the winning share's own difficulty
            cand["pool_source"] = str(b.get("source") or "pool")
            cand["pool_worker"] = str(b.get("worker") or "")
            cand["paid_to"] = str(b.get("address") or "")
            what = ""
            if state == "confirmed":
                # v1.16: only the coin's own blockchain makes it "Block Confirmed"; the pool's list is a hint
                cand["pool_confirmed"] = True
                if cand.get("status") not in {"confirmed"}:
                    if cand.get("status") in {"possible", "unverified", "rejected"}:
                        cand["status"] = "pending"
                    cand["note"] = f"{cand['pool_source']} lists block {h} as confirmed; checking the blockchain"
                what = "possible"
            elif state == "orphaned":
                cand["status"] = "rejected"
                cand["note"] = f"{cand['pool_source']} reports block {h} as orphaned (another block won the race)"
                cand["resolved_ts"] = now
                what = "orphaned"
            else:
                if cand.get("status") != "confirmed":
                    cand["status"] = "pending"
                    cand["note"] = f"{cand['pool_source']} reports block {h} found; waiting for confirmations"
                what = "possible"
            self.save()
            return cand, what

    def mark_notified(self, cand_id: str, what: str) -> bool:
        """True the first time (cand, what) is notified; False when it already was (one possible + one confirmed per block)."""
        with self._lock:
            for c in self.candidates:
                if c.get("id") == cand_id:
                    n = list(c.get("notified") or [])
                    if what in n:
                        return False
                    n.append(what)
                    c["notified"] = n
                    self.save()
                    return True
        return True

    def pending_coins(self) -> set[str]:
        with self._lock:
            return {str(c.get("coin") or "BTC") for c in self.candidates if c.get("status") == "pending"}

    def check(
        self,
        explorer: Any,
        payout_addresses: list[str],
        now: float,
        coin: str = "BTC",
        device_payouts: Optional[dict[str, list[str]]] = None,
        device_payout_notes: Optional[dict[str, str]] = None,
    ) -> list[dict[str, Any]]:
        """Advance pending candidates of one coin. Returns candidates whose status changed."""
        changed: list[dict[str, Any]] = []
        with self._lock:
            pending = [c for c in self.candidates if c.get("status") == "pending" and str(c.get("coin") or "BTC") == coin]
        for c in pending:
            dev_name = str(c.get("device") or "")
            eff_addrs = list(device_payouts[dev_name]) if (isinstance(device_payouts, dict) and dev_name in device_payouts) else list(payout_addresses)
            targets = {normalize_address(a).lower() for a in eff_addrs if a} | nk_coins.target_tokens(eff_addrs)
            self._payouts = eff_addrs
            if explorer is None:
                c["note"] = f"No block explorer is available for {coin}, so this cannot be confirmed."
                new_status = "possible"
            elif not targets and isinstance(device_payout_notes, dict) and device_payout_notes.get(dev_name):
                c["note"] = device_payout_notes[dev_name]
                new_status = "possible"
            else:
                new_status = self._check_one(c, explorer, targets, now)
            if new_status:
                with self._lock:
                    c["status"] = new_status
                    c["resolved_ts"] = now
                changed.append(c)
        if pending:
            self.save()
        return changed

    MAX_LOOKBACK_BLOCKS = 600

    def _heights_to_check(self, c: dict[str, Any], start: int, tip: int) -> list[int]:
        """Blocks around the trigger first, then (v1.15.5) further back to cover the time since the last poll,
        so a block found while Miner Hearth was closed / the PC slept is still found."""
        near = list(range(max(0, start - 1), tip + 1))
        gap = max(0.0, float(c.get("trigger_ts") or 0) - float(c.get("since_ts") or c.get("trigger_ts") or 0))
        back = 0
        if gap > 120:
            bt = float(BLOCK_TIME_SEC.get(str(c.get("coin") or "BTC"), 600))
            back = min(self.MAX_LOOKBACK_BLOCKS, int(gap / max(bt, 1.0)) + 3)
        older = list(range(max(0, start - 2), max(-1, start - 2 - back), -1))
        return near + older

    def _check_one(self, c: dict[str, Any], explorer: Any, targets: set[str], now: float) -> Optional[str]:
        if not targets:
            c["note"] = "No payout address configured, so this cannot be confirmed."
            return "possible"
        if now - float(c.get("trigger_ts", now)) > self.EXPIRE_SEC:
            c["note"] = "Explorer could not be reached in time; not confirmed."
            return "unverified"
        try:
            tip = int(explorer.tip_height())
        except Exception as exc:  # explorer offline: stay pending, never alert
            c["note"] = f"waiting for explorer ({exc})"
            return None
        start = c.get("tip_at_trigger")
        if not start:
            start = tip - 2
            c["tip_at_trigger"] = start
        start = int(start)
        lo_ts = float(c.get("since_ts") or c["trigger_ts"]) - self.TIME_WINDOW_SEC
        hi_ts = float(c["trigger_ts"]) + self.TIME_WINDOW_SEC
        checked = set(int(h) for h in c.get("checked_heights") or [])
        heights = self._heights_to_check(c, start, tip)
        if c.get("block_height") is not None:  # the pool told us the height: that block only
            heights = [int(c["block_height"])]
            if tip < heights[0]:
                c["note"] = f"waiting for the explorer to reach block {heights[0]}"
                return None
        claimed = self.claimed_blocks(str(c.get("coin") or "BTC")) - ({int(c["block_height"])} if c.get("block_height") is not None else set())
        lookups = 0
        for h in heights:
            if h in checked:
                continue
            if lookups >= self.MAX_LOOKUPS_PER_CHECK:
                break
            lookups += 1
            try:
                bh = explorer.block_hash_at(h)
                blk = explorer.block(bh)
                bts = float(blk.get("timestamp") or 0)
                if lo_ts <= bts <= hi_ts:
                    addrs = {normalize_address(a).lower() for a in explorer.coinbase_addresses(bh)}
                    hit = addrs & targets
                    if hit and h in claimed:  # already counted for another candidate: a different block is needed
                        hit = set()
                    if hit:
                        c["block_height"] = h
                        c["block_hash"] = bh
                        winning_share(c)  # fills the share difficulty from the block hash when nothing better is known
                        paid = [a for a in getattr(self, "_payouts", []) if a.lower() in hit or ("script:" + (nk_coins.address_to_script(a) or "-")) in hit]
                        c["paid_to"] = paid[0] if paid else sorted(hit)[0]
                        c["note"] = f"Coinbase of block {h} pays {c['paid_to']}"
                        checked.add(h)
                        c["checked_heights"] = sorted(checked)
                        return "confirmed"
                checked.add(h)
            except Exception as exc:
                c["note"] = f"waiting for explorer ({exc})"
                c["checked_heights"] = sorted(checked)
                return None
        c["checked_heights"] = sorted(checked)
        if tip >= start + self.CONFIRM_DEPTH and all(h in checked for h in heights):
            c["note"] = (f"No block between heights {min(heights)} and {tip} paid a configured payout address "
                         "(stale/orphaned share, pool payout, or a different miner).")
            return "rejected"
        c["note"] = f"checked {len(checked)} block(s) up to height {tip}; waiting for more blocks"
        return None

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(c) for c in self.candidates[-limit:]][::-1]


# ---------------------------------------------------------------------------
# Notifications (Windows toast via PowerShell, local sound fallback)
# ---------------------------------------------------------------------------

def _xml_escape(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))


def build_toast_script(title: str, message: str) -> str:
    xml = (
        "<toast><visual><binding template=\"ToastGeneric\">"
        f"<text>{_xml_escape(title)}</text><text>{_xml_escape(message)}</text>"
        "</binding></visual><audio silent=\"true\"/></toast>"
    ).replace("'", "''")
    app_id = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
    return (
        "$ErrorActionPreference='Stop';"
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml('{xml}');"
        "$t = [Windows.UI.Notifications.ToastNotification]::new($x);"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app_id}').Show($t);"
    )


class Notifier:
    def __init__(self, sounds_dir: Optional[Path] = None, runner: Optional[Callable[..., Any]] = None):
        self.sounds_dir = sounds_dir
        self.runner = runner
        self.sent: deque = deque(maxlen=50)

    def toast(self, title: str, message: str) -> bool:
        self.sent.append({"title": title, "message": message, "ts": time.time()})
        if not IS_WINDOWS and self.runner is None:
            return False
        script = build_toast_script(title, message)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]

        def _run() -> None:
            try:
                if self.runner is not None:
                    self.runner(cmd)
                    return
                nk_proc.run(cmd, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as exc:
                LOG.info("Windows notification not shown: %s", exc)

        threading.Thread(target=_run, name="nk-toast", daemon=True).start()
        return True

    def play_local(self, ref: str, volume: float = 1.0) -> bool:
        """Play through the PC speaker when no dashboard window is open (Windows, WAV only)."""
        if not IS_WINDOWS or not ref or ref == "none":
            return False
        try:
            import winsound  # type: ignore[import-not-found]
        except ImportError:
            return False
        data: Optional[bytes] = None
        if ref.startswith("builtin:"):
            data = nk_sounds.get_builtin_wav(ref[8:])
        elif ref.startswith("custom:") and self.sounds_dir and ref.lower().endswith(".wav"):
            p = self.sounds_dir / ref[7:]
            if p.exists():
                data = p.read_bytes()
        if not data:
            data = nk_sounds.get_builtin_wav("alarm")
        threading.Thread(target=lambda: _safe_play(winsound, data), name="nk-sound", daemon=True).start()
        return True


def _safe_play(winsound: Any, data: bytes) -> None:
    try:
        winsound.PlaySound(data, winsound.SND_MEMORY)
    except Exception:
        pass


def copy_to_clipboard(text: str) -> bool:
    """Put text on the Windows clipboard via the Win32 API (no dependencies)."""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
        data = (str(text) + "\0").encode("utf-16-le")
        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not h:
                return False
            p = kernel32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            kernel32.GlobalUnlock(h)
            return bool(user32.SetClipboardData(CF_UNICODETEXT, h))
        finally:
            user32.CloseClipboard()
    except Exception:
        return False


def read_from_clipboard() -> str:
    """Read plain Unicode text from the Windows clipboard via Win32 API (no dependencies)."""
    if not IS_WINDOWS:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        CF_UNICODETEXT = 13
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        if not user32.OpenClipboard(None):
            return ""
        try:
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            ptr = kernel32.GlobalLock(h)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()
    except Exception:
        return ""




def _safe_file_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "").strip()).strip("._")
    return (stem[:48] if stem else "device")


class FaultRecordStore:
    """Rolling per-device high-res poll buffer + automatic gzipped pre-fault captures in userdata/fault_records/."""

    MAX_BUFFER_SAMPLES = 4000
    MAX_RECORD_SAMPLES = 8000
    FLUSH_EVERY_POLLS = 4

    def __init__(self, records_dir: Optional[Path], buffers_dir: Optional[Path] = None):
        self.records_dir = Path(records_dir) if records_dir else None
        self.buffers_dir = Path(buffers_dir) if buffers_dir else (self.records_dir.parent / "fault_buffers" if self.records_dir else None)
        self._lock = threading.RLock()
        self._buffers: dict[str, list[dict[str, Any]]] = {}
        self._loaded_bufs: set[str] = set()
        self._dirty_bufs: set[str] = set()
        self._poll_counts: dict[str, int] = {}
        self._meta_cache: dict[str, tuple[int, int, dict[str, Any]]] = {}

    def _buf_path(self, name: str) -> Optional[Path]:
        if not self.buffers_dir:
            return None
        return self.buffers_dir / f"{_safe_file_stem(name)}.json.gz"

    @staticmethod
    def _write_gz_atomic(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        raw = json.dumps(data, separators=(",", ":"), default=str).encode("utf-8")
        with gzip.open(tmp, "wb", compresslevel=5) as fh:
            fh.write(raw)
        tmp.replace(path)

    @staticmethod
    def _read_gz(path: Path) -> Any:
        try:
            with gzip.open(path, "rb") as fh:
                return json.loads(fh.read().decode("utf-8"))
        except (OSError, ValueError, EOFError):
            return None

    def _ensure_buffer(self, name: str) -> list[dict[str, Any]]:
        if name in self._loaded_bufs:
            return self._buffers.setdefault(name, [])
        self._loaded_bufs.add(name)
        bp = self._buf_path(name)
        pts: list[dict[str, Any]] = []
        if bp and bp.exists():
            raw = self._read_gz(bp)
            if isinstance(raw, dict) and isinstance(raw.get("samples"), list):
                pts = [x for x in raw["samples"] if isinstance(x, dict)][-self.MAX_BUFFER_SAMPLES:]
        self._buffers[name] = pts
        return pts

    def add_sample(self, name: str, sample: dict[str, Any], window_hours: float, now: float) -> None:
        if not name or not isinstance(sample, dict):
            return
        win_sec = max(1.0, float(window_hours or 12.0)) * 3600.0
        cutoff = float(now) - win_sec
        with self._lock:
            buf = self._ensure_buffer(name)
            buf.append(sample)
            if len(buf) > 1 and float(buf[0].get("ts") or 0.0) < cutoff:
                buf = [s for s in buf if float(s.get("ts") or 0.0) >= cutoff]
            if len(buf) > self.MAX_BUFFER_SAMPLES:
                buf = buf[-self.MAX_BUFFER_SAMPLES:]
            self._buffers[name] = buf
            self._dirty_bufs.add(name)
            cnt = self._poll_counts.get(name, 0) + 1
            self._poll_counts[name] = cnt
            should_flush = (cnt % self.FLUSH_EVERY_POLLS == 0)
        if should_flush:
            self.flush_device(name)

    def flush_device(self, name: str) -> None:
        with self._lock:
            if name not in self._dirty_bufs:
                return
            bp = self._buf_path(name)
            if not bp:
                self._dirty_bufs.discard(name)
                return
            snap = list(self._buffers.get(name) or [])
            self._dirty_bufs.discard(name)
        try:
            self._write_gz_atomic(bp, {"schema": 1, "device": name, "samples": snap})
        except OSError as exc:
            LOG.warning("Could not save fault buffer for %s: %s", name, exc)

    def flush_all(self) -> None:
        with self._lock:
            names = list(self._dirty_bufs)
        for n in names:
            self.flush_device(n)

    def _valid_record_path(self, record_id: str) -> Optional[Path]:
        if not self.records_dir:
            return None
        rid = str(record_id or "").strip()
        if not rid or "/" in rid or "\\" in rid or ".." in rid or not rid.endswith(".json.gz"):
            return None
        p = self.records_dir / rid
        try:
            p.resolve().relative_to(self.records_dir.resolve())
        except (OSError, ValueError):
            return None
        return p

    def _record_meta(self, path: Path, payload: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        try:
            st = path.stat()
        except OSError:
            return None
        if payload is None:
            cached = self._meta_cache.get(path.name)
            if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
                return copy.deepcopy(cached[2])
            payload = self._read_gz(path)
        if not isinstance(payload, dict):
            return None
        meta = {
            "id": path.name,
            "filename": path.name,
            "device": str(payload.get("device") or ""),
            "created_ts": float(payload.get("created_ts") or 0.0),
            "created_iso": str(payload.get("created_iso") or ""),
            "updated_ts": float(payload.get("updated_ts") or payload.get("created_ts") or 0.0),
            "updated_iso": str(payload.get("updated_iso") or payload.get("created_iso") or ""),
            "window_hours": float(payload.get("window_hours") or 12.0),
            "window_start_ts": float(payload.get("window_start_ts") or 0.0),
            "window_start_iso": str(payload.get("window_start_iso") or ""),
            "window_end_ts": float(payload.get("window_end_ts") or 0.0),
            "window_end_iso": str(payload.get("window_end_iso") or ""),
            "sample_count": int(payload.get("sample_count") or len(payload.get("samples") or [])),
            "size_bytes": int(st.st_size),
            "triggers": list(payload.get("triggers") or []),
        }
        self._meta_cache[path.name] = (st.st_mtime_ns, st.st_size, copy.deepcopy(meta))
        return meta

    def list_records(self, device: str = "") -> list[dict[str, Any]]:
        if not self.records_dir or not self.records_dir.exists():
            return []
        with self._lock:
            out: list[dict[str, Any]] = []
            for path in self.records_dir.glob("*.json.gz"):
                meta = self._record_meta(path)
                if not meta:
                    continue
                if device and meta.get("device") != device:
                    continue
                out.append(meta)
            out.sort(key=lambda r: (r.get("updated_ts") or 0.0, r.get("created_ts") or 0.0), reverse=True)
            return out

    def capture_on_fault(self, name: str, faults: list[dict[str, Any]], window_hours: float, now: float) -> Optional[dict[str, Any]]:
        if not self.records_dir or not name or not faults:
            return None
        win_h = max(1.0, float(window_hours or 12.0))
        win_sec = win_h * 3600.0
        triggers = [
            {
                "ts": float(now),
                "iso": nk_faults._fmt_ts_local(now),
                "key": str(f.get("key") or ""),
                "title": str(f.get("title") or ""),
                "severity": str(f.get("severity") or ""),
                "component": str(f.get("component") or ""),
                "cause_summary": str(f.get("cause_summary") or ""),
            }
            for f in faults
        ]
        with self._lock:
            buf = self._ensure_buffer(name)
            start_cut = float(now) - win_sec
            win_samples = [copy.deepcopy(s) for s in buf if start_cut <= float(s.get("ts") or 0.0) <= float(now) + 1e-6]
            existing = self.list_records(name)
            latest = existing[0] if existing else None
            # Dedupe/extend if a fault re-fires within the active capture window of the latest record
            if latest and (float(now) - float(latest.get("created_ts") or 0.0)) <= win_sec:
                path = self._valid_record_path(latest["id"])
                cur = self._read_gz(path) if path and path.exists() else None
                if isinstance(cur, dict) and path is not None:
                    cur_samples = list(cur.get("samples") or [])
                    prev_end = float(cur.get("window_end_ts") or (cur_samples[-1].get("ts") if cur_samples else 0.0) or 0.0)
                    delta = [s for s in win_samples if float(s.get("ts") or 0.0) > prev_end + 1e-6]
                    merged = cur_samples + delta
                    if len(merged) > self.MAX_RECORD_SAMPLES:
                        merged = merged[-self.MAX_RECORD_SAMPLES:]
                    cur_trigs = list(cur.get("triggers") or []) + triggers
                    cur["updated_ts"] = float(now)
                    cur["updated_iso"] = nk_faults._fmt_ts_local(now)
                    cur["window_end_ts"] = float(now)
                    cur["window_end_iso"] = nk_faults._fmt_ts_local(now)
                    cur["triggers"] = cur_trigs[-100:]
                    cur["samples"] = merged
                    cur["sample_count"] = len(merged)
                    try:
                        self._write_gz_atomic(path, cur)
                        self._meta_cache.pop(path.name, None)
                        meta = self._record_meta(path, cur)
                    except OSError as exc:
                        LOG.warning("Could not extend fault record %s: %s", path, exc)
                        meta = None
                    self.flush_device(name)
                    return meta

            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
            stem = _safe_file_stem(name)
            self.records_dir.mkdir(parents=True, exist_ok=True)
            cand = self.records_dir / f"{stem}_{stamp}.json.gz"
            idx = 2
            while cand.exists():
                cand = self.records_dir / f"{stem}_{stamp}_{idx}.json.gz"
                idx += 1
            w_start = float(win_samples[0].get("ts") or start_cut) if win_samples else float(now)
            rec = {
                "schema": 1,
                "device": name,
                "created_ts": float(now),
                "created_iso": nk_faults._fmt_ts_local(now),
                "updated_ts": float(now),
                "updated_iso": nk_faults._fmt_ts_local(now),
                "window_hours": win_h,
                "window_start_ts": w_start,
                "window_start_iso": nk_faults._fmt_ts_local(w_start),
                "window_end_ts": float(now),
                "window_end_iso": nk_faults._fmt_ts_local(now),
                "triggers": triggers,
                "sample_count": len(win_samples),
                "samples": win_samples,
            }
            try:
                self._write_gz_atomic(cand, rec)
                self._meta_cache.pop(cand.name, None)
                meta = self._record_meta(cand, rec)
            except OSError as exc:
                LOG.warning("Could not write fault record %s: %s", cand, exc)
                meta = None
        self.flush_device(name)
        return meta

    @staticmethod
    def _summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
        if not samples:
            return {"count": 0}
        def nums(k: str) -> list[float]:
            return [float(s[k]) for s in samples if isinstance(s.get(k), (int, float))]
        asics, vrs, pwrs, hrs, freqs = nums("asic_temp_c"), nums("vr_temp_c"), nums("power_w"), nums("hashrate_ghs"), nums("frequency_mhz")
        return {
            "count": len(samples),
            "first_iso": samples[0].get("iso"),
            "last_iso": samples[-1].get("iso"),
            "asic_min_c": min(asics) if asics else None,
            "asic_max_c": max(asics) if asics else None,
            "vr_min_c": min(vrs) if vrs else None,
            "vr_max_c": max(vrs) if vrs else None,
            "power_min_w": min(pwrs) if pwrs else None,
            "power_max_w": max(pwrs) if pwrs else None,
            "hashrate_min_ghs": min(hrs) if hrs else None,
            "hashrate_max_ghs": max(hrs) if hrs else None,
            "freq_min_mhz": min(freqs) if freqs else None,
            "freq_max_mhz": max(freqs) if freqs else None,
        }

    def get_record(self, record_id: str, redact: bool = True, max_samples: Optional[int] = None) -> Optional[dict[str, Any]]:
        path = self._valid_record_path(record_id)
        if not path or not path.exists():
            return None
        raw = self._read_gz(path)
        if not isinstance(raw, dict):
            return None
        samples = list(raw.get("samples") or [])
        total = len(samples)
        summary = self._summarize(samples)
        if max_samples is not None and max_samples > 0 and total > max_samples:
            tail_n = min(120, max_samples // 2)
            head_budget = max_samples - tail_n
            head = samples[:total - tail_n]
            step = max(1, len(head) // max(1, head_budget))
            picked = head[::step][:head_budget] + samples[-tail_n:]
            samples = picked
        out = {
            "id": path.name,
            "filename": path.name,
            "redacted": bool(redact),
            "device": raw.get("device"),
            "created_ts": raw.get("created_ts"),
            "created_iso": raw.get("created_iso"),
            "updated_ts": raw.get("updated_ts"),
            "updated_iso": raw.get("updated_iso"),
            "window_hours": raw.get("window_hours"),
            "window_start_ts": raw.get("window_start_ts"),
            "window_start_iso": raw.get("window_start_iso"),
            "window_end_ts": raw.get("window_end_ts"),
            "window_end_iso": raw.get("window_end_iso"),
            "triggers": list(raw.get("triggers") or []),
            "total_samples": total,
            "returned_samples": len(samples),
            "summary": summary,
            "samples": samples,
        }
        return nk_faults.redact(out) if redact else out

    def delete_record(self, record_id: str) -> tuple[bool, str]:
        path = self._valid_record_path(record_id)
        if not path:
            return False, "invalid fault record id"
        with self._lock:
            if not path.exists():
                return False, "fault record not found"
            try:
                path.unlink()
                self._meta_cache.pop(path.name, None)
                return True, ""
            except OSError as exc:
                return False, str(exc)

    def rename(self, old_name: str, new_name: str) -> None:
        if not old_name or not new_name or old_name == new_name:
            return
        with self._lock:
            self._ensure_buffer(old_name)
            if old_name in self._buffers:
                self._buffers[new_name] = self._buffers.pop(old_name)
                self._loaded_bufs.add(new_name)
                self._dirty_bufs.add(new_name)
            old_bp = self._buf_path(old_name)
            if old_bp and old_bp.exists():
                try:
                    new_bp = self._buf_path(new_name)
                    if new_bp:
                        new_bp.parent.mkdir(parents=True, exist_ok=True)
                        old_bp.replace(new_bp)
                except OSError:
                    pass
            if self.records_dir and self.records_dir.exists():
                for path in self.records_dir.glob("*.json.gz"):
                    rec = self._read_gz(path)
                    if isinstance(rec, dict) and rec.get("device") == old_name:
                        rec["device"] = new_name
                        try:
                            self._write_gz_atomic(path, rec)
                            self._meta_cache.pop(path.name, None)
                        except OSError:
                            pass

    def remove(self, name: str) -> None:
        if not name:
            return
        with self._lock:
            self._buffers.pop(name, None)
            self._loaded_bufs.discard(name)
            self._dirty_bufs.discard(name)
            self._poll_counts.pop(name, None)
            bp = self._buf_path(name)
            if bp and bp.exists():
                try:
                    bp.unlink()
                except OSError:
                    pass
            if self.records_dir and self.records_dir.exists():
                for path in list(self.records_dir.glob("*.json.gz")):
                    rec = self._read_gz(path)
                    if isinstance(rec, dict) and rec.get("device") == name:
                        try:
                            path.unlink()
                            self._meta_cache.pop(path.name, None)
                        except OSError:
                            pass


# ---------------------------------------------------------------------------
# Access control for LAN / phone clients
# ---------------------------------------------------------------------------

# v1.17.0 headless / container mode: networks whose clients count as "this computer" (set with --trusted-networks).
# Only for a dashboard that sits behind a login proxy (Umbrel / 5tratumOS app_proxy) or on a network you control.
TRUSTED_NETWORKS: list[Any] = []


def set_trusted_networks(spec: Any) -> list[str]:
    nets: list[Any] = []
    for part in re.split(r"[\s,;]+", str(spec or "")):
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            LOG.warning("Ignoring trusted network %r (not an IP network)", part)
    TRUSTED_NETWORKS[:] = nets
    return [str(n) for n in nets]


def is_loopback(ip: Optional[str]) -> bool:
    """True for this computer (and for --trusted-networks clients in headless / container mode)."""
    if ip is None:
        return True
    try:
        addr = ipaddress.ip_address(str(ip).split("%")[0])
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if addr.is_loopback:
            return True
        return any(addr.version == n.version and addr in n for n in TRUSTED_NETWORKS)
    except ValueError:
        return False


def _cookie_value(headers: Any, key: str) -> str:
    raw = ""
    try:
        raw = headers.get("Cookie") or ""
    except AttributeError:
        raw = ""
    for part in str(raw).split(";"):
        k, _, v = part.strip().partition("=")
        if k == key:
            return unquote(v)
    return ""


def _hdr(headers: Any, key: str) -> str:
    try:
        return str(headers.get(key) or "")
    except AttributeError:
        return ""


PIN_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Miner Hearth - PIN</title><style>body{font-family:system-ui,sans-serif;background:#101317;color:#e8edf4;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
form{background:#1a1f27;border:1px solid #2d3643;border-radius:12px;padding:24px;width:min(92vw,340px)}h1{font-size:1.2rem;margin:0 0 12px}input{width:100%;font-size:1.3rem;padding:10px;border-radius:8px;border:1px solid #2d3643;background:#232a35;color:#e8edf4;letter-spacing:.3em}
button{margin-top:12px;width:100%;padding:10px;font-size:1rem;border-radius:8px;border:0;background:#3ee0b4;color:#06251c;font-weight:700}.e{color:#ff6b6b;font-size:.9rem;min-height:1.2em}</style></head>
<body><form onsubmit="document.cookie='nk_pin='+encodeURIComponent(document.getElementById('p').value)+'; path=/; max-age=2592000; SameSite=Strict';location.reload();return false;">
<h1>Miner Hearth</h1><div class="e">__MSG__</div><input id="p" type="password" inputmode="numeric" autocomplete="current-password" placeholder="PIN" autofocus><button type="submit">Unlock</button></form></body></html>"""


class AccessGuard:
    def __init__(self) -> None:
        self._fails: dict[str, deque] = {}
        self._lock = threading.Lock()

    def check(self, method: str, route: str, client_ip: Optional[str], headers: Any, lan: dict[str, Any]) -> Optional[tuple[int, bytes, str]]:
        ts_proxied = bool(_hdr(headers, "Tailscale-User-Login"))
        if is_loopback(client_ip) and not ts_proxied:
            return None
        pin = str(lan.get("pin") or "")
        if pin:
            given = _hdr(headers, "X-NK-PIN") or _cookie_value(headers, "nk_pin")
            with self._lock:
                fails = self._fails.setdefault(str(client_ip), deque(maxlen=20))
                while fails and time.time() - fails[0] > 300:
                    fails.popleft()
                if len(fails) >= 10:
                    return _json_resp({"ok": False, "error": "too many wrong PIN attempts; wait 5 minutes"}, 429)
                if not hmac.compare_digest(given.encode(), pin.encode()):
                    if given:
                        fails.append(time.time())
                    if route in {"/", "/index.html"}:
                        msg = "Wrong PIN" if given else "Enter the dashboard PIN"
                        return 401, PIN_PAGE.replace("__MSG__", msg).encode("utf-8"), "text/html; charset=utf-8"
                    return _json_resp({"ok": False, "error": "PIN required", "pin_required": True}, 401)
        if ts_proxied:
            return None
        if method == "POST":
            if route == "/api/clipboard":
                return _json_resp({"ok": False, "error": "clipboard is only available on the host PC"}, 403)
            if lan.get("read_only", True):
                return _json_resp({"ok": False, "error": "read-only mode: changes are blocked from LAN devices", "read_only": True}, 403)
            if _hdr(headers, "X-Requested-With") not in ("MinerHearth", nk_legacy.LEGACY_REQUEST_HEADER):
                return _json_resp({"ok": False, "error": "missing X-Requested-With header"}, 403)
        return None


def _json_resp(data: Any, status: int = 200) -> tuple[int, bytes, str]:
    return status, json.dumps(data, default=str).encode("utf-8"), "application/json"


def lan_ip_addresses() -> list[str]:
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))  # no packet is sent for UDP connect
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def tailscale_ip_addresses() -> list[str]:
    """Return any IPv4 addresses on host interfaces inside Tailscale CGNAT (100.64.0.0/10)."""
    found: list[str] = []

    def _maybe_add(raw_ip: str) -> None:
        s = str(raw_ip or "").strip().split("%")[0]
        if not s:
            return
        try:
            addr = ipaddress.ip_address(s)
        except ValueError:
            return
        if isinstance(addr, ipaddress.IPv4Address) and addr in _TAILSCALE_NET and s not in found:
            found.append(s)

    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _maybe_add(res[4][0])
    except OSError:
        pass
    try:
        for ip in lan_ip_addresses():
            _maybe_add(ip)
    except Exception:
        pass
    # Fallback: parse `ipconfig` (Windows) or `ip -4 addr` (Linux/macOS) if hostname doesn't list tailnet IP.
    # v1.16.3: this runs from /api/status (every 3 s). It used to start ipconfig without CREATE_NO_WINDOW, which
    # flashed a console window every few seconds on PCs without Tailscale. Now it goes through nk_proc (hidden)
    # and its answer is cached for TAILSCALE_FALLBACK_TTL_SEC, so the command runs at most once a minute or two.
    if not found:
        for ip in _tailscale_fallback_ips():
            _maybe_add(ip)
    return found


TAILSCALE_FALLBACK_TTL_SEC = 120.0
_TS_FALLBACK: dict[str, Any] = {"ts": None, "ips": []}
_TS_FALLBACK_LOCK = threading.Lock()


def _tailscale_fallback_ips(now: Optional[float] = None) -> list[str]:
    now = time.monotonic() if now is None else now
    with _TS_FALLBACK_LOCK:
        ts = _TS_FALLBACK["ts"]
        if ts is not None and now - ts < TAILSCALE_FALLBACK_TTL_SEC:
            return list(_TS_FALLBACK["ips"])
        _TS_FALLBACK["ts"] = now
        ips: list[str] = []
        cmds = [["ipconfig"]] if os.name == "nt" else [["ip", "-4", "addr"], ["ifconfig"]]
        for cmd in cmds:
            try:
                out = nk_proc.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=2)
            except Exception:
                continue
            for m in re.findall(r"\b(100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b", str(out or "")):
                if m not in ips:
                    ips.append(m)
            if ips:
                break
        _TS_FALLBACK["ips"] = ips
        return list(ips)


# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------

SECRET_DEVICE_KEYS = ("bos_password", "totp_secret", "otp_session", "web_password")


def build_backup(tuner: Any, settings: dict[str, Any], include_passwords: bool = False) -> dict[str, Any]:
    import autotune

    devices: list[dict[str, Any]] = []
    enabled: dict[str, bool] = {}
    cfg = tuner.config if tuner is not None else None
    if cfg is not None:
        for d in cfg.devices:
            row = autotune.device_config_to_dict(d)
            if not include_passwords:
                for k in SECRET_DEVICE_KEYS:
                    row.pop(k, None)
            devices.append(row)
            try:
                enabled[d.name] = bool(tuner._enabled(d.name))
            except Exception:
                pass
    app = copy.deepcopy(settings) if include_passwords else strip_secrets(settings)
    return {
        "miner_hearth_backup": 1,
        "app_version": autotune.APP_VERSION,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "include_passwords": bool(include_passwords),
        "userdata": {
            "devices": devices,
            "enabled": enabled,
            "device_order": list(cfg.device_order) if cfg is not None else [],
            "display_fields": dict(cfg.display_fields) if cfg is not None else {},
            "global_tuner": dict(cfg.global_tuner) if cfg is not None else {},
            "scan_subnet": cfg.scan_subnet if cfg is not None else "",
            "dry_run": bool(tuner._dry_run()) if tuner is not None else False,
        },
        "app_settings": app,
    }


def validate_backup(data: Any) -> tuple[bool, str]:
    if not isinstance(data, dict) or (data.get("miner_hearth_backup") != 1 and data.get(nk_legacy.LEGACY_BACKUP_KEY) != 1):
        return False, "Not a Miner Hearth backup file"
    ud = data.get("userdata")
    if not isinstance(ud, dict) or not isinstance(ud.get("devices", []), list):
        return False, "Backup is missing userdata.devices"
    if not isinstance(data.get("app_settings", {}), dict):
        return False, "Backup app_settings must be an object"
    return True, ""


# ---------------------------------------------------------------------------
# Feature hub
# ---------------------------------------------------------------------------



class FaultContextStore:
    """userdata/fault_context.json: remembered VR overheat events & fan tach presence per device."""

    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self.devices: dict[str, dict[str, Any]] = {}
        raw = _load_json(self.path) if self.path and self.path.exists() else None
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
                LOG.warning("Could not save fault context: %s", exc)

    def get(self, name: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.devices.get(name) or {})

    def update(self, name: str, patch: dict[str, Any], save: bool = True) -> None:
        with self._lock:
            cur = dict(self.devices.get(name) or {})
            cur.update(patch)
            self.devices[name] = cur
        if save:
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


class FaultSnapshotStore:
    """userdata/fault_snapshots.json: pre-fault healthy readings captured when a fault or 0 GH/s appears."""

    MAX_PER_DEVICE = 10

    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self.devices: dict[str, list[dict[str, Any]]] = {}
        raw = _load_json(self.path) if self.path and self.path.exists() else None
        if isinstance(raw, dict) and isinstance(raw.get("devices"), dict):
            for k, v in raw["devices"].items():
                if isinstance(v, list):
                    self.devices[str(k)] = [dict(x) for x in v if isinstance(x, dict)][-self.MAX_PER_DEVICE:]

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            try:
                _write_json_atomic(self.path, {"schema": 1, "devices": self.devices})
            except OSError as exc:
                LOG.warning("Could not save fault snapshots: %s", exc)

    def record(self, name: str, trigger: str, snapshot: dict[str, Any], now: float) -> None:
        if not snapshot:
            return
        entry = {
            "captured_ts": now,
            "captured_iso": nk_faults._fmt_ts_local(now),
            "trigger": str(trigger)[:160],
            "snapshot": nk_faults.redact(copy.deepcopy(snapshot)),
        }
        with self._lock:
            lst = self.devices.setdefault(name, [])
            lst.append(entry)
            if len(lst) > self.MAX_PER_DEVICE:
                self.devices[name] = lst[-self.MAX_PER_DEVICE:]
        self.save()

    def latest(self, name: str) -> Optional[dict[str, Any]]:
        with self._lock:
            lst = self.devices.get(name) or []
            return copy.deepcopy(lst[-1]) if lst else None

    def list(self, name: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(copy.deepcopy(self.devices.get(name) or [])))

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


class FeatureHub:
    NET_REFRESH_SEC = 300
    PRICE_REFRESH_SEC = 600
    CANDIDATE_CHECK_SEC = 60
    FIRMWARE_REFRESH_SEC = 86400

    def __init__(
        self,
        userdata_dir: Optional[Path],
        fetch: Optional[nk_net.Fetcher] = None,
        notifier: Optional[Notifier] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.userdata_dir = Path(userdata_dir) if userdata_dir else None
        ud = self.userdata_dir
        self.fetch = fetch or nk_net.default_fetch
        self.clock = clock
        if ud:
            nk_legacy.ensure_settings_file(ud)  # v1.17.0: in-place upgrade keeps the old settings
        self.settings = SettingsStore(ud / SETTINGS_FILE_NAME if ud else None)
        self.tuning_log = TuningLog(ud / "tuning_log.jsonl" if ud else None)
        self.long_history = LongHistory(ud / "history_long" if ud else None)
        self.blocks = BlockWatcher(ud / "block_watch.json" if ud else None)
        self.alerts = AlertManager()
        self.sounds_dir = (ud / "sounds") if ud else None
        self.notifier = notifier or Notifier(self.sounds_dir)
        self.guard = AccessGuard()
        self.events: deque = deque(maxlen=100)
        self._lock = threading.RLock()
        self.network: dict[str, Any] = {}
        self.price: dict[str, Any] = {}
        self.avg_fees: Optional[float] = None
        self.firmware_cache_path = (ud / "firmware_cache.json") if ud else None
        self.firmware: dict[str, Any] = {}
        raw_fw = _load_json(self.firmware_cache_path) if self.firmware_cache_path and self.firmware_cache_path.exists() else None
        if isinstance(raw_fw, dict):
            self.firmware = raw_fw
        self._last_net = 0.0
        self._last_price = 0.0
        self._last_cand = 0.0
        self.last_client_seen = 0.0
        self._override_warned: set[tuple[str, str, str]] = set()
        # v1.15.5: pool-reported blocks (HeliosPool) + per machine/coin hourly message limit
        self.poolfeed = nk_poolfeed.PoolBlockFeed(clock=clock)
        self.poolfeed_async = True
        # v1.16: live updates
        self.updates = nk_update.UpdateManager(ud, _shipped_version() or "0", clock=clock)
        _clear_restart_pending(ud)  # v1.17.1: a new process means the restart has happened
        self.pools = nk_pools.PoolLibrary(ud)  # v1.16.2: saved pools (userdata/saved_pools.json)
        # v1.17.0: node monitoring (own thread; off until Settings > Node Monitoring is turned on)
        self.node_monitor = nk_nodes.NodeMonitor(ud, lambda: self.settings.section("nodes"), self._node_event, clock=clock,
                                                 network_height=self._node_net_height,
                                                 default_disk=str(ud) if ud else "")
        self.restart_hook: Optional[Callable[[], Any]] = None
        try:
            self.updates.set_enabled(bool(self.settings.get()["updates"]["enabled"]))
            if self.settings.get()["updates"]["enabled"]:
                self.updates.hot_reload(True)
        except Exception:
            LOG.exception("live updates: could not load the current update")
        self._poolfeed_busy = False
        self._pool_seen_path = (ud / "pool_blocks_seen.json") if ud else None
        raw_ps = _load_json(self._pool_seen_path) if self._pool_seen_path and self._pool_seen_path.exists() else None
        self._pool_seen: dict[str, str] = {str(k): str(v) for k, v in (raw_ps or {}).items()} if isinstance(raw_ps, dict) else {}
        self._blk_sent: dict[tuple[str, str], list[float]] = {}
        self._blk_batch: dict[tuple[str, str], dict[str, Any]] = {}
        self.tuner: Any = None
        self.shared: Any = None
        self.lan_server: Any = None
        self.lan_bind_host = ""
        self.lan_error = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()
        self.migrated_from_v17 = False
        # --- v1.9 ---
        self.coin_stats: dict[str, dict[str, Any]] = {}
        self.coin_info: dict[str, dict[str, Any]] = {}
        self._coins_wanted: set[str] = set()
        self._last_coin_fetch: dict[str, float] = {}
        self.session_best = nk_extras.SessionBestTracker(ud / "session_best.json" if ud else None)
        self.pool_health = nk_extras.PoolHealth()
        self.pool_info: dict[str, dict[str, Any]] = {}
        self.pool_stats = nk_extras.PoolStats(ud / "pool_stats.json" if ud else None)
        self.chip_health = nk_extras.ChipHealth(ud / "chip_health.json" if ud else None)
        self.restarts = nk_extras.RestartManager(ud / "restarts.json" if ud else None)
        self.webhooks = nk_extras.Webhooks()
        self.prober: Callable[[str], tuple[bool, Optional[float]]] = nk_extras.probe_pool
        self._last_probe = 0.0
        self._wear_emitted: dict[str, float] = {}
        self._restart_note: dict[str, dict[str, Any]] = {}
        self.ambient: dict[str, dict[str, Any]] = {}
        self.migrated_from_v18 = False
        # --- v1.10 ---
        self.fault_history = nk_faults.FaultHistory(ud / "fault_history.json" if ud else None)
        self.log_dir = (ud / "log_snapshots") if ud else None
        self.faults_now: dict[str, list[dict[str, Any]]] = {}
        self.fault_probe: dict[str, dict[str, Any]] = {}
        self.log_meta: dict[str, dict[str, Any]] = {}
        self._last_fault_probe = 0.0
        self._fault_lock = threading.Lock()
        self._baseline_chains: dict[str, int] = {}
        self._baseline_fans: dict[str, int] = {}
        self._uptime_track: dict[str, tuple[float, list[float]]] = {}
        # --- v1.12 ---
        self.fault_context = FaultContextStore(ud / "fault_context.json" if ud else None)
        self.fault_snapshots = FaultSnapshotStore(ud / "fault_snapshots.json" if ud else None)
        self.fault_records = FaultRecordStore(ud / "fault_records" if ud else None, ud / "fault_buffers" if ud else None)
        self._last_healthy: dict[str, dict[str, Any]] = {}
        self._in_fault_or_zero: dict[str, bool] = {}
        self._healthy_vr_since: dict[str, float] = {}
        self._pool_users: dict[str, dict[str, Any]] = {}
        # --- v1.15.4: full event / telemetry log ---
        self.eventlog = nk_eventlog.EventLog(ud / "logs" if ud else None, clock=clock, app_version=_app_version(),
                                             settings=self.settings.get().get("eventlog"))
        self._eventlog_handler: Optional[logging.Handler] = None
        self._prev_hooks: dict[str, Any] = {}
        # --- v1.15.5: expected transitions (pool switch, NK restart, maintenance pause) ---
        self.grace = nk_grace.MaintenanceGrace(ud / "maintenance.json" if ud else None, clock=clock, log=self.eventlog.write)

    # --- wiring ------------------------------------------------------------
    def attach(self, tuner: Any, shared: Any) -> None:
        self.tuner = tuner
        self.shared = shared
        if tuner is not None:
            tuner.hub = self
            # Migration: seed 7d/30d history from the v1.7 24h store once.
            try:
                for d in tuner.config.devices:
                    if not self.long_history.exists(d.name):
                        pts = tuner.history.load_device(d.name)
                        if pts:
                            self.long_history.seed_from_points(d.name, pts)
                            self.migrated_from_v17 = True
            except Exception as exc:
                LOG.warning("Could not seed long history: %s", exc)
        if shared is not None:
            shared.hub = self

    def start(self) -> "FeatureHub":
        self._start_eventlog()
        self.apply_lan()
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="miner-hearth", daemon=True)
            self._thread.start()
        self.node_monitor.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        try:
            self.node_monitor.stop()
        except Exception:
            pass
        self._stop_eventlog()
        try:
            self.long_history.save_all()
            self.pool_stats.save()
            self.fault_records.flush_all()
        except Exception:
            pass
        try:  # never leave a miner with a capped fan when the app closes
            if self.tuner is not None and hasattr(self.tuner, "release_fan_caps"):
                self.tuner.release_fan_caps()
        except Exception:
            LOG.exception("Could not release quiet-mode fan caps")
        self._stop_lan()

    def _loop(self) -> None:
        time.sleep(1.0)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                LOG.exception("Miner Hearth background task failed")
            self._wake.wait(20.0)
            self._wake.clear()

    def explorer(self) -> nk_net.ExplorerClient:
        b = self.settings.section("block")
        return nk_net.ExplorerClient(nk_net.explorer_bases(b.get("explorer", "mempool"), b.get("custom_base_url", ""), b.get("allow_public_fallback", True)), fetch=self.fetch)

    def tick(self, now: Optional[float] = None, force: bool = False) -> None:
        """One round of background work (network stats, price, block confirmation, firmware)."""
        now = self.clock() if now is None else now
        s = self.settings.get()
        self._eventlog_tick(s, now)
        if s["network"]["enabled"] and (force or now - self._last_net >= self.NET_REFRESH_SEC):
            self._last_net = now
            ex = self.explorer()
            try:
                snap = ex.network_snapshot(now)
                if s["power"].get("include_fees"):
                    fees = ex.avg_fees_sats()
                    if fees is not None:
                        self.avg_fees = fees
                with self._lock:
                    self.network = snap
                    self.network["error"] = ""
            except nk_net.NetError as exc:
                with self._lock:
                    self.network = dict(self.network or {})
                    self.network["error"] = "offline or explorer unreachable"
                LOG.debug("Network stats unavailable: %s", exc)
        if s["network"]["enabled"] and (force or now - self._last_price >= self.PRICE_REFRESH_SEC):
            self._last_price = now
            ex = self.explorer()
            p = nk_net.fetch_btc_price(s["power"]["currency"], ex.bases, fetch=self.fetch)
            if p:
                p["updated_ts"] = now
                with self._lock:
                    self.price = p
        if s["network"]["enabled"] and s["coins"]["fetch_chains"]:
            self.refresh_coin_stats(now, force=force)
        if s["pools"]["probe_enabled"] and (force or now - self._last_probe >= self.POOL_PROBE_SEC):
            self._last_probe = now
            self.probe_pools(now)
        # v1.16.1: the pool feed first, so a block the pool lists carries the pool's winning-share difficulty
        # (and height) before the blockchain check can announce it
        if s["block"]["enabled"] and s["network"]["enabled"] and s["block"].get("pool_feed", True):
            self.poll_pool_blocks(now, force=force)
        if force or now - self._last_cand >= self.CANDIDATE_CHECK_SEC:
            self._last_cand = now
            self.check_candidates(now)
        self.flush_block_waits(now)
        self.flush_block_batches(now)
        if s["firmware"]["check_enabled"]:
            self.check_firmware(now)
        if s["faults"]["enabled"] and s["faults"]["probe_enabled"] and (force or now - self._last_fault_probe >= self.FAULT_PROBE_SEC):
            self._last_fault_probe = now
            self.probe_faults(now)

    # --- v1.9: coins -----------------------------------------------------------
    COIN_REFRESH_SEC = 600
    POOL_PROBE_SEC = 120

    def chain(self, coin: str) -> Any:
        if coin == "BTC":
            return self.explorer()
        if coin in nk_coins.COINS:
            return nk_coins.MultiChain(coin, fetch=self.fetch)
        return None

    def refresh_coin_stats(self, now: float, force: bool = False) -> None:
        """Fetch tips for non-BTC coins only when some machine might be mining them."""
        wanted = set(self._coins_wanted) | (self.blocks.pending_coins() - {"BTC", nk_coins.UNKNOWN})
        for coin in sorted(wanted):
            if coin == "BTC" or coin not in nk_coins.COINS:
                continue
            if not force and now - self._last_coin_fetch.get(coin, 0.0) < self.COIN_REFRESH_SEC:
                continue
            self._last_coin_fetch[coin] = now
            try:
                st = nk_coins.MultiChain(coin, fetch=self.fetch).stats()
                st["updated_ts"] = now
                st["error"] = ""
                with self._lock:
                    self.coin_stats[coin] = st
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    old = dict(self.coin_stats.get(coin) or {})
                    old["error"] = "explorer unreachable"
                    self.coin_stats[coin] = old
                LOG.debug("%s chain stats unavailable: %s", coin, exc)

    def all_coin_stats(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            out = {k: dict(v) for k, v in self.coin_stats.items() if v.get("height")}
            if self.network.get("height"):
                out["BTC"] = {"height": self.network.get("height"), "difficulty": self.network.get("difficulty"), "source": self.network.get("source")}
        return out

    def detect_coin(self, cfg: Any, reading: Any, s: dict[str, Any]) -> dict[str, Any]:
        name = getattr(cfg, "name", "")
        algo = (getattr(cfg, "algorithm", "") or getattr(reading, "algorithm", "") or "").strip().lower()
        info = {
            "pool_url": getattr(reading, "pool_url", "") or "",
            "pool_user": getattr(reading, "pool_user", "") or "",
            "block_height": getattr(reading, "block_height", None),
            "network_difficulty": getattr(reading, "network_difficulty", None),
            "algorithm": algo,
        }
        override = (getattr(cfg, "coin", "") or (s["coins"]["overrides"] or {}).get(name, "")).strip().upper()
        res = nk_coins.detect_coin(info, self.all_coin_stats(), override)
        # Which chains do we need tips for? Only fetch beyond BTC when relevant to algo/pool/address.
        want: set[str] = set()
        if res["coin"] not in ("BTC", nk_coins.UNKNOWN):
            want.add(res["coin"])
            merged = (nk_coins.COINS.get(res["coin"]) or {}).get("merged_with")
            if merged:
                want.add(merged)
        merged_cfg = (getattr(cfg, "merged_coin", "") or "").strip().upper()
        if merged_cfg in nk_coins.COINS:
            want.add(merged_cfg)
        if algo and algo != "sha256d":
            want |= nk_coins.coins_for_algorithm(algo)
        hint = nk_coins.host_hint(info["pool_url"])
        if hint and hint != "BTC":
            want.add(hint)
        ac = nk_coins.address_coins(nk_coins.pool_user_address(info["pool_user"]))
        if ac and "BTC" not in ac:
            want |= ac
        if res["coin"] == nk_coins.UNKNOWN and (info["block_height"] or info["network_difficulty"]):
            btc = self.all_coin_stats().get("BTC")
            if not btc or "BTC" not in nk_coins.chain_matches(info["block_height"], info["network_difficulty"], {"BTC": btc}):
                scoped = nk_coins.coins_for_algorithm(algo) if algo else (set(nk_coins.COIN_IDS) - {"BTC"})
                want |= (scoped - {"BTC"})
        with self._lock:
            if want - self._coins_wanted:
                self._wake.set()
            self._coins_wanted |= want
            self.coin_info[name] = res
        return res

    def coin_difficulty(self, coin: str, reading: Any = None) -> Optional[float]:
        if coin == "BTC" or coin == nk_coins.UNKNOWN:
            if coin == nk_coins.UNKNOWN and reading is not None and getattr(reading, "network_difficulty", None):
                return float(reading.network_difficulty)
            return self.network_difficulty(reading)
        if (nk_coins.COINS.get(coin) or {}).get("spiky_difficulty") and reading is not None and getattr(reading, "network_difficulty", None):
            return float(reading.network_difficulty)  # v1.15.5: changes block to block (CheetahCoin), the job knows the current one
        with self._lock:
            d = (self.coin_stats.get(coin) or {}).get("difficulty")
        if d:
            return float(d)
        v = getattr(reading, "network_difficulty", None) if reading is not None else None
        return float(v) if v else None

    def coin_tip(self, coin: str, reading: Any = None) -> Optional[int]:
        if coin in ("BTC", nk_coins.UNKNOWN):
            with self._lock:
                return self.network.get("height") or getattr(reading, "block_height", None)
        with self._lock:
            h = (self.coin_stats.get(coin) or {}).get("height")
        return h or getattr(reading, "block_height", None)

    def payout_addresses_for(self, coin: str, s: Optional[dict[str, Any]] = None) -> list[str]:
        s = s or self.settings.get()
        if coin == "BTC":
            return list(s["block"].get("payout_addresses") or [])
        return list((s["coins"].get("payout_addresses") or {}).get(coin) or [])

    def device_payout_info(self, name: str, coin: str, s: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Resolve block-found confirmation addresses for one device.

        Manual payout addresses in Settings always override. Otherwise extract from
        that machine's pool username (before the first '.' worker suffix); pool
        account names that are not valid addresses are ignored with a clear note.
        """
        s = s or self.settings.get()
        manual = self.payout_addresses_for(coin, s)
        snap = self._snap(name)
        pu_rec = self._pool_users.get(name) or {}
        using_fb = bool(pu_rec.get("using_fallback_stratum", snap.get("using_fallback_stratum")))
        primary = str(pu_rec.get("pool_user") or snap.get("pool_user") or "").strip()
        fallback = str(pu_rec.get("fallback_pool_user") or snap.get("fallback_pool_user") or "").strip()
        candidates = [fallback, primary] if (using_fb and fallback) else [primary, fallback]
        last_status = "empty"
        last_reason = "no pool username reported yet"
        for raw_u in candidates:
            if not raw_u:
                continue
            res = nk_coins.extract_pool_payout_address(raw_u, coin=coin)
            if res.get("address") and manual:
                # v1.15.5: a manual address no longer hides the pool-username address (solo pools pay that one)
                addrs = list(manual) + ([res["address"]] if res["address"] not in manual else [])
                return {
                    "addresses": addrs,
                    "source": "manual",
                    "ready": True,
                    "masked": nk_faults.mask_address(manual[0]),
                    "note": "using manual payout address(es) in Settings and the pool username address",
                }
            if res.get("address"):
                addr = res["address"]
                return {
                    "addresses": [addr],
                    "source": "pool_user",
                    "ready": True,
                    "masked": nk_faults.mask_address(addr),
                    "note": "derived from pool username",
                }
            last_status = res.get("status") or "empty"
            last_reason = res.get("reason") or last_reason
        if manual:
            return {
                "addresses": list(manual),
                "source": "manual",
                "ready": True,
                "masked": nk_faults.mask_address(manual[0]),
                "note": "using manual payout address(es) in Settings",
            }
        return {
            "addresses": [],
            "source": last_status,
            "ready": False,
            "masked": "",
            "note": last_reason,
        }

    # --- v1.9: pools -----------------------------------------------------------
    def probe_pools(self, now: float) -> None:
        keys = self.pool_stats.active_keys(now)
        if not keys:
            return

        def run() -> None:
            for k in keys[:20]:
                ok, ms = self.prober(k)
                self.pool_stats.record_probe(k, ok, ms, self.clock())
        threading.Thread(target=run, name="nk-pool-probe", daemon=True).start()

    def check_candidates(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        s = self.settings.get()
        coins = self.blocks.pending_coins()
        if not coins:
            return
        changed: list[dict[str, Any]] = []
        if nk_coins.UNKNOWN in coins:  # v1.15.5: a coin we can't name is not looked up on the BTC explorer unless the share was BTC-sized
            for cand in list(self.blocks.candidates):
                if cand.get("status") == "pending" and cand.get("coin") == nk_coins.UNKNOWN and not self._unknown_is_btc(cand):
                    cand["status"] = "possible"
                    cand["resolved_ts"] = now
                    cand["note"] = ("Miner Hearth doesn't recognise this machine's coin, so no block explorer can confirm it "
                                    "(unverified). Pick the coin in Settings > Coins to get confirmations.")
                    changed.append(cand)
            self.blocks.save()
        for coin in sorted(coins):
            chain_coin = "BTC" if coin == nk_coins.UNKNOWN else coin
            ex = self.chain(chain_coin) if chain_coin in nk_coins.COINS else None
            dev_payouts: dict[str, list[str]] = {}
            dev_notes: dict[str, str] = {}
            for cand in list(self.blocks.candidates):
                if cand.get("status") == "pending" and str(cand.get("coin") or "BTC") == coin:
                    dn = str(cand.get("device") or "")
                    pinfo = self.device_payout_info(dn, chain_coin, s)
                    dev_payouts[dn] = list(pinfo["addresses"])
                    if not pinfo["addresses"]:
                        if pinfo["source"] == "pool_account_ignored":
                            dev_notes[dn] = "No manual payout address is set and this machine's pool username is a pool account name (ignored), so this cannot be confirmed."
                        elif pinfo["source"] == "coin_mismatch":
                            dev_notes[dn] = f"Pool username address does not match {chain_coin} and no manual payout address is set."
            changed += self.blocks.check(
                ex,
                self.payout_addresses_for(chain_coin, s),
                now,
                coin=coin,
                device_payouts=dev_payouts,
                device_payout_notes=dev_notes,
            )
        for c in changed:
            st = c.get("status")
            if st == "confirmed":
                self._notify_block(c, "confirmed")
            elif st == "possible":
                self._notify_block(c, "possible")
            else:
                LOG.info("Block candidate from %s not confirmed (%s): %s", c["device"], st, c.get("note"))
                try:
                    self.eventlog.write("block_not_confirmed", device=str(c.get("device") or ""), level="info", coin=c.get("coin"),
                                        status=st, note=str(c.get("note") or "")[:300], share_diff=c.get("share_diff"),
                                        network_difficulty=c.get("network_difficulty"))
                except Exception:
                    pass
                if "possible" in (c.get("notified") or []):  # the user saw "possible": say what came of it (banner only, no webhook)
                    coin_lbl = self._coin_label(c)
                    hnum = f" #{c['block_height']}" if c.get("block_height") is not None else ""
                    self.emit(_event("block_unconfirmed", "info", c["device"], f"Block{hnum} from {c['device']} ({coin_lbl}) was not confirmed",
                                     str(c.get("note") or "No matching block was found on the explorer."), sound=False))

    def _unknown_is_btc(self, c: dict[str, Any]) -> bool:
        """An 'unknown coin' candidate is treated as a possible BTC block only when its share was BTC-sized."""
        btc = self.network_difficulty(None)
        share = float(c.get("trigger_diff") or winning_share(c)[0] or 0)
        return bool(btc and share >= 0.9 * float(btc))

    def _coin_label(self, c: dict[str, Any]) -> str:
        lbl = str(c.get("coin") or "BTC")
        if lbl == nk_coins.UNKNOWN:
            return "BTC" if self._unknown_is_btc(c) else "unknown coin"
        return lbl

    def _block_link(self, coin: str, c: dict[str, Any]) -> str:
        url = ""
        try:
            tmpl = (nk_coins.COINS.get(coin) or {}).get("block_url") or ""
            if tmpl and c.get("block_height") is not None and ("{hash}" not in tmpl or c.get("block_hash")):
                url = tmpl.format(height=c.get("block_height"), hash=c.get("block_hash") or "")
        except Exception:
            url = ""
        if coin == "BTC" and c.get("block_hash"):
            url = url or f"https://mempool.space/block/{c['block_hash']}"
        return url

    # --- v1.15.5: pool-reported blocks -----------------------------------------
    def _pool_watch(self) -> tuple[dict[str, set[str]], dict[tuple[str, str], list[tuple[str, str]]]]:
        watch: dict[str, set[str]] = {}
        devmap: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for name, rec in list(self._pool_users.items()):
            url = rec.get("pool_url") or self._snap(name).get("pool_url") or ""
            hc = nk_poolfeed.helios_coin(url)
            if not hc:
                continue
            addr, worker = nk_poolfeed.split_pool_user(rec.get("pool_user") or "")
            if not addr:
                continue
            na = nk_poolfeed._norm_addr(addr)
            watch.setdefault(hc, set()).add(na)
            devmap.setdefault((nk_poolfeed.HELIOS_COINS[hc], na), []).append((name, worker))
        return watch, devmap

    def poll_pool_blocks(self, now: Optional[float] = None, force: bool = False) -> None:
        now = self.clock() if now is None else now
        watch, devmap = self._pool_watch()
        if not watch or self._poolfeed_busy:
            return
        if not force and all(now - self.poolfeed.last_poll.get(hc, 0.0) < self.poolfeed.POLL_SEC for hc in watch):
            return

        def run() -> None:
            try:
                blocks = self.poolfeed.poll(watch, self._pool_seen, now=now, force=force)
                self._apply_pool_blocks(blocks, devmap, now)
            except Exception:
                LOG.exception("pool block feed failed")
            finally:
                self._poolfeed_busy = False
        self._poolfeed_busy = True
        if self.poolfeed_async:
            threading.Thread(target=run, name="nk-pool-blocks", daemon=True).start()
        else:
            run()

    def _apply_pool_blocks(self, blocks: list[dict[str, Any]], devmap: dict[tuple[str, str], list[tuple[str, str]]], now: float) -> None:
        for b in blocks:
            devs = devmap.get((b["coin"], nk_poolfeed._norm_addr(b.get("address"))), [])
            w = str(b.get("worker") or "").strip().lower()
            pick = [n for n, wk in devs if wk and wk.strip().lower() == w] or ([devs[0][0]] if len(devs) == 1 else [])
            dev = pick[0] if pick else (f"pool worker {b.get('worker')}" if b.get("worker") else "one of your machines")
            cand, what = self.blocks.attach_pool_block(dev, b, now)
            self._pool_seen[b["key"]] = b["state"]
            try:
                self.eventlog.write("pool_block", device=dev, level="info", coin=b["coin"], height=b["height"], state=b["state"],
                                    pool=b.get("source"), worker=b.get("worker"), block_hash=b.get("hash"))
            except Exception:
                pass
            if cand is None:
                continue
            if what in ("possible", "confirmed"):
                self._notify_block(cand, what)
            elif what == "orphaned" and "possible" in (cand.get("notified") or []):
                self.emit(_event("block_unconfirmed", "info", dev, f"{b['coin']} block {b['height']} from {dev} was orphaned",
                                 str(cand.get("note") or ""), sound=False))
        if blocks and self._pool_seen_path:
            if len(self._pool_seen) > 2000:
                self._pool_seen = dict(list(self._pool_seen.items())[-1500:])
            try:
                _write_json_atomic(self._pool_seen_path, self._pool_seen)
            except OSError:
                pass

    # --- v1.15.5: per machine/coin hourly limit for block messages ---------------
    BATCH_SEC = 3600.0

    def _batch_block(self, c: dict[str, Any], what: str, coin_lbl: str, s: dict[str, Any]) -> bool:
        """True when this block message is folded into the hourly summary instead of being sent now."""
        limit = int(s["block"].get("max_messages_per_hour", 6) or 0)
        if limit <= 0:
            return False
        key = (str(c.get("device") or ""), coin_lbl)
        now = self.clock()
        first_msg = what == "possible" or "possible" not in (c.get("notified") or [])
        batch = self._blk_batch.get(key)
        if c.get("batched") or (first_msg and len([t for t in self._blk_sent.get(key, []) if now - t < 3600]) >= limit):
            if batch is None:
                batch = self._blk_batch[key] = {"first_ts": now, "blocks": {}, "fields": self._block_fields(c, coin_lbl)}
            ent = batch["blocks"].setdefault(str(c.get("id")), {"state": what})
            if what == "confirmed":
                ent["state"] = "confirmed"
            ws = winning_share(c)[0]
            if ws or not ent.get("share"):
                ent["share"] = nk_math.format_difficulty(ws) if ws else "unknown"
            if what == "possible" or "possible" not in (c.get("notified") or []):
                c["share_locked"] = True
            ent.update(height=c.get("block_height"), url=self._block_link(coin_lbl, c) if c.get("block_hash") else "", last_ts=now)
            c["batched"] = True
            self.blocks.save()
            try:
                self.eventlog.write("block_batched", device=key[0], level="info", coin=coin_lbl, what=what, candidate=c.get("id"),
                                    height=c.get("block_height"), limit_per_hour=limit)
            except Exception:
                pass
            return True
        if first_msg:
            self._blk_sent[key] = [t for t in self._blk_sent.get(key, []) if now - t < 3600] + [now]
        return False

    def flush_block_batches(self, now: Optional[float] = None, force: bool = False) -> list[dict[str, Any]]:
        now = self.clock() if now is None else now
        out = []
        for key, batch in list(self._blk_batch.items()):
            if not force and now - float(batch["first_ts"]) < self.BATCH_SEC:
                continue
            self._blk_batch.pop(key, None)
            ents = list(batch["blocks"].values())
            if not ents:
                continue
            dev, coin_lbl = key
            conf = [e for e in ents if e["state"] == "confirmed"]
            heights = ["#" + str(e["height"]) for e in ents if e.get("height") is not None]
            n = len(ents)
            mins = max(1, int(round((now - float(batch["first_ts"])) / 60)))
            title = f"{n} more Block{'s' if n != 1 else ''} Found in the last {'hour' if mins >= 55 else f'{mins} min'}"
            f = batch.get("fields") or {}
            lines = [f"Machine: {dev}", f"Coin: {coin_lbl}"]
            if f:
                lines += [f"Payout address: {f.get('payout')}", f"Worker: {f.get('worker')}"]
            if heights:
                lines.append("Blocks: " + ", ".join(heights[:15]) + (" …" if len(heights) > 15 else ""))
            shares = [("#" + str(e["height"]) + " " if e.get("height") is not None else "") + str(e.get("share") or "unknown") for e in ents]
            lines.append("Share difficulty: " + ", ".join(shares[:15]) + (" …" if len(shares) > 15 else ""))
            lines.append(f"Confirmed on the blockchain: {len(conf)} of {n}")
            lines.append("(Summed up to stay under the hourly block-alert limit in Settings > Block.)")
            link = next((e["url"] for e in reversed(conf) if e.get("url")), "")
            if link:
                lines.append(link)
            msg = "\n".join(lines)
            ev = _event("block_confirmed" if conf else "block_possible", "block" if conf else "info", dev, title, msg, sound=bool(conf),
                        coin=coin_lbl, batch=n)
            self._blk_sent[key] = [t for t in self._blk_sent.get(key, []) if now - t < 3600] + [now]
            out.append(self.emit(ev))
        return out

    POOL_WAIT_SEC = 150.0

    def _block_fields(self, c: dict[str, Any], coin_lbl: str) -> dict[str, str]:
        """v1.16: the fields every block alert shows (no made-up values: unknown stays unknown)."""
        dev = str(c.get("device") or "")
        pool_user = str(c.get("pool_user") or (self._pool_users.get(dev) or {}).get("pool_user") or "").strip()
        addr, worker = (pool_user.split(".", 1) + [""])[:2] if pool_user else ("", "")
        addr, worker = addr.strip(), worker.strip()
        if c.get("trigger") == "pool" or c.get("pool_source"):
            addr = str(c.get("paid_to") or addr)
            worker = str(c.get("pool_worker") or worker)
        if addr and not nk_coins.address_coins(addr) and not (c.get("pool_source") and c.get("paid_to")):
            payout = f"{addr} (pool account name, not an address)"
        else:
            payout = addr or "not reported by the miner"
        share, _src = winning_share(c)  # v1.16.1: the winning share only, never the best difficulty
        share_txt = nk_math.format_difficulty(share) if share else "unknown"
        net = float(c.get("network_difficulty") or 0)
        return {"machine": dev, "coin": coin_lbl, "payout": payout, "worker": worker or "(none)", "share": share_txt,
                "net": nk_math.format_difficulty(net) if net else "unknown",
                "height": f" #{c['block_height']}" if c.get("block_height") is not None else ""}

    def _notify_block(self, c: dict[str, Any], what: str) -> dict[str, Any]:
        """One "Block Found" and one "Block Confirmed" alert per block, to the banner, toast and every webhook (v1.16 format).

        "Block Found" goes out when the miner or its pool reports a block; "Block Confirmed" only when the coin's own
        blockchain (block explorer) shows the block paying this machine's address."""
        s = self.settings.get()
        coin_lbl = self._coin_label(c)
        unknown = coin_lbl == "unknown coin"
        if unknown and not s["block"].get("notify_unknown_coin", True):
            try:
                self.eventlog.write("block_unknown_coin", device=str(c.get("device") or ""), level="info", candidate=c.get("id"),
                                    note="not sent: 'notify for unknown coins' is off")
            except Exception:
                pass
            return {"kind": "block_" + what, "suppressed": "unknown_coin_off"}
        if what == "confirmed" and "possible" not in (c.get("notified") or []):
            self._notify_block(c, "possible")  # "Block Found" always comes first
        if str(c.get("id") or "") and what in (c.get("notified") or []):
            return {"kind": "block_" + what, "duplicate": True}
        if self._batch_block(c, what, coin_lbl, s):
            self.blocks.mark_notified(str(c.get("id") or ""), what)
            return {"kind": "block_" + what, "batched": True}
        if not self.blocks.mark_notified(str(c.get("id") or ""), what):
            return {"kind": "block_" + what, "duplicate": True}
        dev = str(c.get("device") or "")
        f = self._block_fields(c, "unknown" if unknown else coin_lbl)
        c["share_locked"] = True  # Block Confirmed shows the same share difficulty as Block Found
        self.blocks.save()
        lines = [f"Machine: {f['machine']}"]
        if unknown:
            host = nk_coins.pool_host((self._pool_users.get(dev) or {}).get("pool_url") or self._snap(dev).get("pool_url") or "")
            lines.append(f"Coin: unknown{f' (pool {host})' if host else ''}")
        else:
            lines.append(f"Coin: {coin_lbl}")
        lines += [f"Payout address: {f['payout']}", f"Worker: {f['worker']}",
                  f"Share difficulty: {f['share']}", f"Network difficulty: {f['net']}"]
        url = self._block_link(coin_lbl, c) if c.get("block_hash") else ""
        if what == "confirmed":
            lines.append(f"Confirmed on the {coin_lbl} blockchain.")
            if url:
                lines.append(url)
            ev = _event("block_confirmed", "block", dev, f"Block Confirmed{f['height']}", "\n".join(lines),
                        block_height=c.get("block_height"), block_hash=c.get("block_hash"), coin=coin_lbl, candidate=c.get("id"))
        else:
            if unknown:
                lines.append("Miner Hearth doesn't recognise this coin, so it can't be confirmed on a blockchain. "
                             "Pick the coin in Settings > Coins to get Block Confirmed alerts.")
            elif c.get("pool_confirmed"):
                lines.append(f"{c.get('pool_source') or 'The pool'} lists this block as confirmed; Block Confirmed follows when the blockchain shows it.")
            if url:
                lines.append(url)
            ev = _event("block_possible", "info", dev, f"Block Found{f['height']}", "\n".join(lines),
                        sound=False, coin=nk_coins.UNKNOWN if unknown else coin_lbl, candidate=c.get("id"),
                        block_height=c.get("block_height"))
        return self.emit(ev)

    def flush_block_waits(self, now: float) -> None:
        """v1.16: a block whose pool did not report a height within POOL_WAIT_SEC is announced without the number."""
        for c in list(self.blocks.candidates):
            t = c.get("await_pool_until")
            if t and now >= float(t) and "possible" not in (c.get("notified") or []) and c.get("status") in {"pending", "possible", "confirmed"}:
                c.pop("await_pool_until", None)
                self._notify_block(c, "possible")

    def _block_alt_diffs(self, cfg: Any, reading: Any, coin: str, s: dict[str, Any]) -> dict[str, float]:
        """Other chains this machine may really be mining (v1.15.5), so a wrong manual coin never hides a block:
        the job's own network difficulty, and the pool host's coin when a manual override disagrees with it."""
        out: dict[str, float] = {}
        info = self.coin_info.get(getattr(cfg, "name", "")) or {}
        job = getattr(reading, "network_difficulty", None)
        stats = self.all_coin_stats()
        if job:
            try:
                job = float(job)
                match = nk_coins.chain_matches(getattr(reading, "block_height", None), job, stats)
                c_ = match[0] if len(match) == 1 else coin
                if c_ != coin or not self.coin_difficulty(coin, None):
                    out[c_] = job
            except (TypeError, ValueError):
                pass
        if info.get("source") == "manual":
            hint = nk_coins.host_hint(getattr(reading, "pool_url", "") or "")
            if hint and hint != coin and hint in nk_coins.COINS:
                d = self.coin_difficulty(hint, reading if (nk_coins.COINS.get(hint) or {}).get("spiky_difficulty") else None)
                if d:
                    out.setdefault(hint, float(d))
                key = (getattr(cfg, "name", ""), coin, hint)
                if key not in self._override_warned:
                    self._override_warned.add(key)
                    LOG.warning("%s: coin is set to %s in Settings; the pool host name suggests %s, so blocks of both are watched", key[0], coin, hint)
                    try:
                        self.eventlog.write("coin_override_mismatch", device=key[0], level="warn", configured=coin, pool_host_coin=hint)
                    except Exception:
                        pass
        return out

    def _webhook_logged(self, ev: dict[str, Any], results: list[dict[str, Any]]) -> None:
        """v1.15.5: every webhook delivery (status code / error) goes to the full event log."""
        try:
            for r in results or []:
                self.eventlog.write("webhook_result", device=str(ev.get("device") or ""), level="info" if r.get("ok") else "warn",
                                    kind=ev.get("kind"), event_id=ev.get("id"), service=r.get("service"), ok=bool(r.get("ok")),
                                    status=r.get("status"), error=str(r.get("error") or "")[:300], attempts=r.get("attempts", 1),
                                    test=bool(ev.get("test")))
        except Exception:
            pass

    def check_firmware(self, now: Optional[float] = None, force: bool = False) -> None:
        now = self.clock() if now is None else now
        last = float(self.firmware.get("checked_ts") or 0.0)
        if not force and now - last < self.FIRMWARE_REFRESH_SEC:
            return
        latest: dict[str, Any] = {}
        for key in nk_net.FIRMWARE_SOURCES:
            rel = nk_net.fetch_latest_release(key, fetch=self.fetch)
            if rel:
                latest[key] = rel
            elif key in (self.firmware.get("latest") or {}):
                latest[key] = self.firmware["latest"][key]
        self.firmware = {"checked_ts": now, "latest": latest}
        try:
            self.eventlog.write("firmware_check", level="info", found=sorted(latest),
                                latest={k: {kk: v.get(kk) for kk in ("version", "tag", "name", "published") if isinstance(v, dict) and kk in v} for k, v in latest.items()})
        except Exception:
            pass
        if self.firmware_cache_path:
            try:
                _write_json_atomic(self.firmware_cache_path, self.firmware)
            except OSError:
                pass

    # --- events -----------------------------------------------------------
    def emit(self, ev: dict[str, Any]) -> dict[str, Any]:
        s = self.settings.get()
        dev = str(ev.get("device") or "")
        why = self.grace.suppress_alert(dev, str(ev.get("kind") or "")) if dev and not ev.get("test") else ""
        if why:  # v1.15.5: no banner, sound, toast or webhook for the normal parts of a pool switch / restart
            self.grace.note_suppressed(dev, str(ev.get("kind")), why, str(ev.get("title") or ""))
            ev = dict(ev, suppressed=why)
            return ev
        with self._lock:
            self.events.append(ev)
        self.eventlog.alert(ev)
        lvl = logging.WARNING if ev["level"] in {"problem", "block"} else logging.INFO
        LOG.log(lvl, "ALERT %s: %s — %s", ev["kind"], ev["title"], ev["message"], extra={"nk_eventlog_skip": True})
        if ev.get("sound") and s["notifications"].get("desktop_toast") and ev["level"] in {"block", "problem"}:
            self.notifier.toast(f"{APP_NAME}: {ev['title']}", ev["message"])
        if ev.get("sound") and not s["sounds"]["master_mute"] and (self.clock() - self.last_client_seen) > 30:
            ref = s["sounds"]["block_sound"] if ev["sound"] == "block" else s["sounds"]["problem_sound"]
            self.notifier.play_local(ref, s["sounds"]["volume"])
        try:
            self.webhooks.send(s["webhooks"], ev, on_result=self._webhook_logged)
        except Exception:
            LOG.exception("webhook delivery failed")
        return ev

    # --- tuner hooks -------------------------------------------------------
    def network_difficulty(self, reading: Any = None) -> Optional[float]:
        with self._lock:
            d = self.network.get("difficulty")
        if d:
            return float(d)
        v = getattr(reading, "network_difficulty", None) if reading is not None else None
        return float(v) if v else None

    def on_poll(self, cfg: Any, reading: Any, now: Optional[float] = None, state: Any = None) -> list[dict[str, Any]]:
        now = self.clock() if now is None else now
        s = self.settings.get()
        out: list[dict[str, Any]] = []
        coin = "BTC"
        active_before = self._alert_keys(cfg.name)
        try:
            out += self._grace_poll(cfg, reading, now, s)
        except Exception:
            LOG.exception("Miner Hearth maintenance-grace hook failed for %s", getattr(cfg, "name", "?"))
        try:
            self._pool_users[cfg.name] = {
                "pool_user": str(getattr(reading, "pool_user", "") or "").strip(),
                "fallback_pool_user": str(getattr(reading, "fallback_pool_user", "") or "").strip(),
                "using_fallback_stratum": bool(getattr(reading, "using_fallback_stratum", False)),
                "pool_url": str(getattr(reading, "pool_url", "") or "").strip(),
            }
        except Exception:
            pass
        try:
            out += self._on_poll_v110(cfg, reading, now, s)
        except Exception:
            LOG.exception("Miner Hearth v1.10 fault hook failed for %s", getattr(cfg, "name", "?"))
        try:
            out += self._on_poll_v19(cfg, reading, now, state, s)
            coin = (self.coin_info.get(cfg.name) or {}).get("coin") or "BTC"
        except Exception:
            LOG.exception("Miner Hearth v1.9 poll hook failed for %s", getattr(cfg, "name", "?"))
        try:
            if getattr(reading, "ok", False):
                hottest = max(reading.asic_temps) if getattr(reading, "asic_temps", None) else reading.asic_temp
                self.long_history.record(cfg.name, now, reading.hashrate, hottest, reading.vr_temp, reading.power)
            for ev in self.alerts.evaluate(cfg.name, reading, s, now, suppress=lambda k, _n=cfg.name: self._suppressed(_n, k, now)):
                out.append(self.emit(ev))
            if s["block"]["enabled"] and getattr(reading, "ok", False):
                diffs = {f: getattr(reading, f, None) for f in DIFF_FIELDS}
                if getattr(reading, "last_share_estimated", False):
                    diffs["last_submitted_diff"] = None  # estimate = pool difficulty, not a real share
                tip = self.coin_tip(coin, reading)
                cand = self.blocks.observe(cfg.name, diffs, self.coin_difficulty(coin, reading), tip, now, coin=coin,
                                           found_blocks=getattr(reading, "found_blocks", None),
                                           coin_diffs=self._block_alt_diffs(cfg, reading, coin, s),
                                           new_block_flag=getattr(reading, "new_block_flag", None),
                                           last_share_actual=not getattr(reading, "last_share_is_target", False))
                if cand is not None:
                    LOG.warning("%s: block found? (%s via %s %.0f; winning share difficulty %s, network difficulty %.0f, %s) — checking the blockchain",
                                cfg.name, cand["trigger"], cand.get("field"), float(cand.get("trigger_diff") or 0),
                                f"{cand['share_diff']:.0f}" if cand.get("share_diff") else "unknown", cand["network_difficulty"], cand["coin"])
                    cand["pool_user"] = str(getattr(reading, "pool_user", "") or "").strip()
                    if s["block"].get("pool_feed", True) and s["network"]["enabled"] and nk_poolfeed.helios_coin(getattr(reading, "pool_url", "") or ""):
                        # v1.16: the pool knows the block height; give its feed a moment so "Block Found #<height>" has the number
                        cand["await_pool_until"] = now + self.POOL_WAIT_SEC
                        self.blocks.save()
                        self.poolfeed.last_poll.clear()
                    else:
                        out.append(self._notify_block(cand, "possible"))
                    self._last_cand = 0.0
                    self._wake.set()
        except Exception:
            LOG.exception("Miner Hearth poll hook failed for %s", getattr(cfg, "name", "?"))
        self._eventlog_poll(cfg, reading, now, state, active_before)
        return out

    # --- v1.15.5: expected transitions / maintenance grace ------------------------
    def _suppressed(self, name: str, kind: str, now: float) -> str:
        why = self.grace.suppress_alert(name, kind, now)
        if why:
            self.grace.note_suppressed(name, kind, why, now=now)
        return why

    def _reset_alert_baselines(self, name: str, now: float) -> None:
        """After a pool switch / restart: compare against the new normal, not the restart dip or the old pool."""
        with self.alerts._lock:
            st = self.alerts._st(name)
            st["hr"].clear()
            st["acc"] = None
            st["acc_change_ts"] = now
        try:
            self.pool_health.remove(name)
        except Exception:
            pass

    def expect_transition(self, name: str, kind: str, reason: str) -> None:
        """Called just before Miner Hearth itself restarts or reconfigures a miner."""
        try:
            g = self.grace.expect(name, kind, reason, self.settings.get()["alerts"]["maintenance"], self.clock())
            if g is not None:
                self._reset_alert_baselines(name, self.clock())
        except Exception:
            LOG.exception("could not open maintenance grace for %s", name)

    def pause_alerts(self, name: str, minutes: float, reason: str = "") -> float:
        until = self.grace.pause(name, minutes, reason, self.settings.get()["alerts"]["maintenance"], self.clock())
        if minutes > 0:
            self._reset_alert_baselines(name, self.clock())
        return until

    def _grace_poll(self, cfg: Any, reading: Any, now: float, s: dict[str, Any]) -> list[dict[str, Any]]:
        name = cfg.name
        out: list[dict[str, Any]] = []
        res = self.grace.observe(name, reading, s["alerts"]["maintenance"], now, on_fallback=_is_on_fallback_pool(reading))
        if res.get("opened"):
            self._reset_alert_baselines(name, now)
        if res.get("ended") in {"recovered", "pause_ended"}:
            self._reset_alert_baselines(name, now)
        exp = res.get("expired")
        if exp:
            kind = exp["status"]
            a = s["alerts"]
            if (a.get(kind) or {}).get("enabled", True):
                cooldown = float(a["cooldown_min"]) * 60.0
                with self.alerts._lock:
                    fire = self.alerts._fire(name, kind, now, cooldown)
                    if kind == "offline" and self.alerts._st(name)["offline_since"] is None:
                        self.alerts._st(name)["offline_since"] = now
                if fire:
                    what = nk_grace.KIND_LABELS.get(exp["kind"], "maintenance")
                    out.append(self.emit(_event(kind, "problem", name, f"{name} did not recover after the {what}",
                                                f"Still not healthy {exp['minutes']:.0f} min after the {what}: {exp['detail']}. "
                                                "Alerts that are normal during a restart were paused until now.",
                                                maintenance=exp)))
        return out

    # --- v1.15.4: event log wiring ---------------------------------------------
    def _alert_keys(self, name: str) -> set[str]:
        try:
            with self.alerts._lock:
                st = self.alerts.dev.get(name)
                return set(st["active"]) if st else set()
        except Exception:
            return set()

    def _eventlog_poll(self, cfg: Any, reading: Any, now: float, state: Any, active_before: set[str]) -> None:
        el = self.eventlog
        if not el.enabled:
            return
        try:
            name = cfg.name
            pu = self._pool_users.get(name) or {}
            el.add_secret(pu.get("pool_user"), pu.get("fallback_pool_user"))
            for p in (getattr(reading, "pools", None) or []):
                if isinstance(p, dict):
                    el.add_secret(p.get("user"))
            for key in ("ssid", "wifi_ssid"):
                el.add_secret((getattr(reading, "raw_fields", None) or {}).get(key) if isinstance(getattr(reading, "raw_fields", None), dict) else None)
            el.alert_states(name, active_before, self._alert_keys(name), {"error": str(getattr(reading, "error", "") or "")} if not getattr(reading, "ok", False) else None)
            snap = self._snap(name)
            extra = {
                "type_cfg": getattr(cfg, "type", ""),
                "tuner": {k: snap.get(k) for k in ("autotune_enabled", "enabled", "last_action", "last_reason", "error_pct", "rolling_error_pct",
                                                    "emergency_lockout_remaining_sec", "bos_last_action", "bos_last_reason", "bos_power_target_w",
                                                    "write_enabled") if k in snap},
                "faults": [{"key": f.get("key"), "severity": f.get("severity"), "title": f.get("title")} for f in (self.faults_now.get(name) or [])][:20],
                "alerts_active": sorted(self._alert_keys(name)),
                "pool_health": {k: (self.pool_info.get(name) or {}).get(k) for k in ("pool", "reject_pct", "win_total", "ping_ms", "ping_loss")} if name in self.pool_info else None,
            }
            el.observe_poll(name, reading, now,
                            latency_ms=getattr(state, "_nk_poll_ms", None) if state is not None else None,
                            exc_class=str(getattr(state, "_nk_exc_class", "") or "") if state is not None else "",
                            snapshot_extra=extra)
        except Exception:
            pass

    def _eventlog_tick(self, s: dict[str, Any], now: float) -> None:
        el = self.eventlog
        try:
            el.configure(s.get("eventlog"))
            if not el.enabled:
                return
            dt = datetime.fromtimestamp(now)
            for kind, sec in (("tou", "schedule"), ("quiet", "quiet")):
                sc = s.get(sec) or {}
                active: dict[str, dict[str, Any]] = {}
                if sc.get("enabled"):
                    for w in sc.get("windows") or []:
                        try:
                            if window_active(w, dt):
                                active[str(w.get("name") or w.get("id") or "window")] = {
                                    k: w.get(k) for k in ("start", "end", "days", "devices", "axe_max_freq_mhz", "bos_power_target_w", "fan_max_pct") if k in w}
                        except Exception:
                            continue
                el.windows(kind, active, now)
            names = list(self.shared.snapshots.keys()) if self.shared is not None else []
            online = sum(1 for n in names if self._snap(n).get("online"))
            el.heartbeat({"devices": len(names), "online": online, "events_in_memory": len(self.events),
                          "autotune_global": (self.shared.is_autotune_enabled() if self.shared is not None and hasattr(self.shared, "is_autotune_enabled") else None),
                          "dry_run": (self.shared.is_dry_run() if self.shared is not None and hasattr(self.shared, "is_dry_run") else None)}, now)
        except Exception:
            pass

    def _start_eventlog(self) -> None:
        el = self.eventlog
        try:
            el.configure(self.settings.get().get("eventlog"))
            if self._eventlog_handler is None:
                h = nk_eventlog.EventLogHandler(el)
                logging.getLogger().addHandler(h)
                self._eventlog_handler = h
            if "sys" not in self._prev_hooks:
                import sys as _sys
                prev_sys = _sys.excepthook
                prev_thr = threading.excepthook
                self._prev_hooks = {"sys": prev_sys, "thread": prev_thr}

                def _sys_hook(et: Any, ev: Any, tb: Any) -> None:
                    try:
                        el.exception("main thread", ev, tb)
                    except Exception:
                        pass
                    prev_sys(et, ev, tb)

                def _thr_hook(args: Any) -> None:
                    try:
                        if args.exc_value is not None:
                            el.exception(f"thread {getattr(args.thread, 'name', '?')}", args.exc_value, args.exc_traceback)
                    except Exception:
                        pass
                    prev_thr(args)

                _sys.excepthook = _sys_hook
                threading.excepthook = _thr_hook
            devs = []
            if self.tuner is not None:
                for d in list(getattr(self.tuner.config, "devices", []) or []):
                    devs.append({"name": d.name, "type": getattr(d, "type", ""), "write_enabled": bool(getattr(d, "write_enabled", False)),
                                 "bos_power_mgmt": bool(getattr(d, "bos_power_mgmt", False))})
            el.app_start({"devices": devs, "settings": strip_secrets_deep(self.settings.get())})
        except Exception:
            pass

    def _stop_eventlog(self) -> None:
        try:
            self.eventlog.app_stop("normal shutdown")
        except Exception:
            pass
        try:
            if self._eventlog_handler is not None:
                logging.getLogger().removeHandler(self._eventlog_handler)
                self._eventlog_handler = None
            if self._prev_hooks:
                import sys as _sys
                _sys.excepthook = self._prev_hooks.get("sys") or _sys.__excepthook__
                threading.excepthook = self._prev_hooks.get("thread") or threading.__excepthook__
                self._prev_hooks = {}
        except Exception:
            pass

    def eventlog_export(self, rng: str, anonymize: bool = False) -> tuple[bytes, dict[str, Any]]:
        extra: dict[str, Any] = {"log_status.json": self.eventlog.stats()}
        try:
            extra["diagnostics.json"] = self.diagnostics()
        except Exception as exc:  # noqa: BLE001
            extra["diagnostics.json"] = {"error": f"{type(exc).__name__}: {exc}"}
        return self.eventlog.export_zip(rng, anonymize=anonymize, extra_files=extra)

    def schedule_limits(
        self, name: str, now: Optional[float] = None, aliases: Optional[list[str]] = None
    ) -> Optional[dict[str, Any]]:
        return schedule_limits(self.settings.get(), name, self.clock() if now is None else now, aliases=aliases)

    def quiet_limits(self, name: str, now: Optional[float] = None) -> Optional[dict[str, Any]]:
        return quiet_limits(self.settings.get(), name, self.clock() if now is None else now)

    def _on_poll_v19(self, cfg: Any, reading: Any, now: float, state: Any, s: dict[str, Any]) -> list[dict[str, Any]]:
        name = cfg.name
        out: list[dict[str, Any]] = []
        ok = bool(getattr(reading, "ok", False))
        if ok:
            self.detect_coin(cfg, reading, s)
        # Session best share (Antminer / Braiins & AxeOS): reset on pool switch as well as miner restart.
        if ok:
            dtype = getattr(cfg, "type", "")
            if dtype == "antminer":
                pools = [p for p in (getattr(reading, "pools", None) or []) if isinstance(p, dict)]
                enabled_pools = [
                    p for p in pools
                    if not p.get("disabled") and str(p.get("status") or "").strip().lower() != "disabled"
                ]
                act_pool = next((p for p in enabled_pools if p.get("stratum_active")), None)
                if act_pool is None and enabled_pools:
                    act_pool = next((p for p in enabled_pools if str(p.get("status") or "").strip().lower() == "alive"), None)
                    if act_pool is None and all("stratum_active" not in p for p in enabled_pools):
                        act_pool = enabled_pools[0]
                has_pool_best = False
                if act_pool is not None:
                    p_idx = act_pool.get("pool")
                    p_url = str(act_pool.get("url") or getattr(reading, "pool_url", "") or "").strip().lower()
                    p_user = str(act_pool.get("user") or getattr(reading, "pool_user", "") or "").strip()
                    p_best = act_pool.get("best_share")
                    has_pool_best = p_best is not None
                    pk = f"pool:{'' if p_idx is None else p_idx}|{p_url}|{p_user}" if (p_url or p_user) else ""
                else:
                    p_url = str(getattr(reading, "pool_url", "") or "").strip().lower()
                    p_user = str(getattr(reading, "pool_user", "") or "").strip()
                    p_best = None
                    pk = f"pool:|{p_url}|{p_user}" if (p_url or p_user) else ""
                diffs = [p.get("best_share") for p in enabled_pools]
                post_diffs: list[Optional[float]] = []
                if not getattr(reading, "last_share_estimated", False):
                    ls_diff = getattr(reading, "last_submitted_diff", None)
                    diffs.append(ls_diff)
                    post_diffs.append(ls_diff)
                sb = self.session_best.update(
                    name,
                    getattr(reading, "best_diff", None),
                    getattr(reading, "uptime_sec", None),
                    diffs,
                    now,
                    active_pool_key=pk,
                    active_pool_best=p_best,
                    post_switch_diffs=post_diffs,
                    all_time_hint=getattr(reading, "best_diff", None),
                    prefer_active_pool_best=True,
                    has_active_pool_best=has_pool_best,
                )
                if sb.get("pool_switched") or has_pool_best:
                    reading.best_session_diff = sb.get("session_best")
                    reading.best_session_diff_display = ""
                elif reading.best_session_diff is None and sb.get("session_best"):
                    reading.best_session_diff = sb["session_best"]
            elif dtype in {"gamma", "nerdoctaxe", "axeos-generic"}:
                using_fb = bool(getattr(reading, "using_fallback_stratum", False))
                if using_fb and (getattr(reading, "fallback_pool_url", "") or getattr(reading, "fallback_pool_user", "")):
                    slot = "fallback"
                    p_url = str(getattr(reading, "fallback_pool_url", "") or "").strip().lower()
                    p_port = getattr(reading, "fallback_pool_port", None) or ""
                    p_user = str(getattr(reading, "fallback_pool_user", "") or "").strip()
                else:
                    slot = "primary"
                    p_url = str(getattr(reading, "pool_url", "") or "").strip().lower()
                    p_port = getattr(reading, "pool_port", None) or ""
                    p_user = str(getattr(reading, "pool_user", "") or "").strip()
                pk = f"axe:{slot}|{p_url}:{p_port}|{p_user}" if (p_url or p_user) else ""
                post_diffs = []
                if not getattr(reading, "last_share_estimated", False):
                    post_diffs.append(getattr(reading, "last_submitted_diff", None))
                sb = self.session_best.update(
                    name,
                    getattr(reading, "best_session_diff", None),
                    getattr(reading, "uptime_sec", None),
                    post_diffs,
                    now,
                    active_pool_key=pk,
                    post_switch_diffs=post_diffs,
                    all_time_hint=getattr(reading, "best_diff", None),
                )
                if sb.get("pool_switched"):
                    reading.best_session_diff = sb.get("session_best")
                    reading.best_session_diff_display = "" 
        # Ambient / intake temperature
        if ok and getattr(reading, "ambient_temp", None) is not None:
            self.ambient[name] = {"c": float(reading.ambient_temp), "source": getattr(reading, "ambient_source", "") or "miner", "ts": now}
        # Pool health + multi-pool stats
        if ok:
            pr = s["alerts"]["pool_rejects"]
            ph = self.pool_health.update(name, getattr(reading, "shares_accepted", None), getattr(reading, "shares_rejected", None),
                                         getattr(reading, "shares_stale", None), now, window_sec=float(pr["window_min"]) * 60.0)
            key = nk_coins.pool_key(getattr(reading, "pool_url", ""), getattr(reading, "pool_port", None))
            ping = getattr(reading, "ping_rtt_ms", None)
            self.pool_stats.record_shares(key, name, ph["d_acc"], ph["d_rej"], ph["d_stale"], ping, now)
            ph.update({"pool": key, "ping_ms": ping, "ping_loss": getattr(reading, "ping_loss", None),
                       "shares_accepted": getattr(reading, "shares_accepted", None),
                       "shares_rejected": getattr(reading, "shares_rejected", None), "shares_stale": getattr(reading, "shares_stale", None)})
            self.pool_info[name] = ph
            cooldown = float(s["alerts"]["cooldown_min"]) * 60.0
            with self.alerts._lock:
                if ph["reject_pct"] is not None and ph["win_total"] >= int(pr["min_shares"]) and ph["reject_pct"] >= float(pr["pct"]):
                    if pr.get("enabled") and not self._suppressed(name, "pool_rejects", now) and self.alerts._fire(name, "pool_rejects", now, cooldown):
                        out.append(self.emit(_event("pool_rejects", "problem", name, f"{name}: pool is rejecting shares",
                                                    f"{ph['reject_pct']:.1f}% of the last {ph['win_total']} shares were rejected or stale on {key or 'the pool'} "
                                                    f"(alert at {float(pr['pct']):.1f}%).", pool=key)))
                elif ph["reject_pct"] is not None and ph["reject_pct"] < float(pr["pct"]) / 2.0:
                    self.alerts._clear(name, "pool_rejects")
        # Chip health / wear (tunable AxeOS machines)
        ch = s["chip_health"]
        if ok and ch["enabled"] and getattr(cfg, "type", "") != "antminer":
            asic = getattr(reading, "asic_temp", None)
            vr = getattr(reading, "vr_temp", None)
            hot = bool((asic is not None and asic >= getattr(cfg, "asic_max_c", 999)) or (vr is not None and vr >= getattr(cfg, "vr_max_c", 999)))
            err = getattr(state, "last_rolling_error_pct", None) if state is not None else getattr(reading, "error_percentage", None)
            recorded = self.chip_health.observe(name, getattr(reading, "frequency", None), getattr(reading, "core_voltage", None),
                                                err, float(getattr(cfg, "error_high_pct", 1.0)), hot, now, float(ch["stable_min"]))
            if recorded:
                w = self.chip_health.wear(name, float(ch["wear_mv"]), int(ch["min_days"]))
                if w and s["alerts"]["chip_wear"]["enabled"] and now - self._wear_emitted.get(name, 0.0) > 7 * 86400:
                    self._wear_emitted[name] = now
                    out.append(self.emit(_event("chip_wear", "info", name, f"{name}: possible chip wear",
                                                f"At {w['freq']} MHz it now needs about {w['recent_mv']} mV to run stable, up {w['delta_mv']} mV "
                                                f"from {w['baseline_mv']} mV (since {w['since']}). Consider a lower frequency or better cooling.",
                                                sound=False, wear=w)))
        # Maintenance restarts (off by default, rate limited)
        due = self.restarts.evaluate(name, reading, s["restarts"], now)
        if due:
            if due.get("blocked"):
                last = self._restart_note.get(name, {})
                if now - float(last.get("ts", 0)) > 3600:
                    LOG.warning("%s: %s restart skipped: %s", name, due["kind"], due["blocked"])
                self._restart_note[name] = {"ts": now, "kind": due["kind"], "result": "skipped: " + due["blocked"]}
            elif self.tuner is not None and hasattr(self.tuner, "maintenance_restart"):
                dry = bool(self.tuner._dry_run())
                if not dry:
                    self.expect_transition(name, "nk_restart", f"{due['kind']} maintenance restart")
                try:
                    res = self.tuner.maintenance_restart(name)
                    okr = True
                except Exception as exc:  # noqa: BLE001
                    res, okr = str(exc), False
                self.restarts.record(name, due, now)
                self._restart_note[name] = {"ts": now, "kind": due["kind"], "result": res, "ok": okr}
                self.log_tuning(name, "restart", None, None, f"{due['reason']}: {res}", due["kind"], source="restart", dry_run=dry)
                self.fault_history.add(name, "auto_restart", now, kind=due["kind"], text=f"{due['reason']}: {res}", ok=okr, dry_run=dry)
                out.append(self.emit(_event("restart", "info" if okr else "problem", name,
                                            f"{name}: {'would restart' if dry else 'restarted'} ({due['kind']})" if okr else f"{name}: restart failed",
                                            f"{due['reason']}. {res}", sound=not okr)))
        return out

    def log_tuning(self, device: str, kind: str, old: Any, new: Any, reason: str, action: str = "", source: str = "autotune", dry_run: bool = False) -> None:
        entry = {
            "ts": self.clock(), "device": device, "kind": kind, "old": old, "new": new,
            "action": action, "reason": reason, "source": source, "dry_run": bool(dry_run),
        }
        self.tuning_log.add(entry)
        self.eventlog.tuning(entry)

    def on_rename(self, old: str, new: str) -> None:
        for obj in (self.session_best, self.pool_health, self.pool_stats, self.chip_health, self.restarts, self.fault_history, self.fault_context, self.fault_snapshots, self.fault_records):
            try:
                obj.rename(old, new)
            except Exception:
                LOG.exception("rename failed in %s", type(obj).__name__)
        for dct in (self.coin_info, self.pool_info, self.ambient, self._restart_note, self.faults_now, self.fault_probe, self.log_meta, self._last_healthy, self._in_fault_or_zero, self._healthy_vr_since, self._pool_users):
            if old in dct:
                dct[new] = dct.pop(old)
        s = self.settings.get()
        if old in (s["coins"]["overrides"] or {}):
            ov = dict(s["coins"]["overrides"])
            ov[new] = ov.pop(old)
            s["coins"]["overrides"] = ov
            self.settings.replace(s)
        self.long_history.rename(old, new)
        self.grace.rename(old, new)
        self.eventlog.rename(old, new)
        self.tuning_log.rename(old, new)
        self.alerts.rename(old, new)
        self.blocks.rename(old, new)

    def on_remove(self, name: str) -> None:
        for obj in (self.session_best, self.pool_health, self.pool_stats, self.chip_health, self.restarts, self.fault_history, self.fault_context, self.fault_snapshots, self.fault_records):
            try:
                obj.remove(name)
            except Exception:
                LOG.exception("remove failed in %s", type(obj).__name__)
        for dct in (self.coin_info, self.pool_info, self.ambient, self._restart_note, self.faults_now, self.fault_probe, self.log_meta, self._last_healthy, self._in_fault_or_zero, self._healthy_vr_since, self._pool_users):
            dct.pop(name, None)
        s = self.settings.get()
        if name in (s["coins"]["overrides"] or {}):
            ov = dict(s["coins"]["overrides"])
            ov.pop(name, None)
            s["coins"]["overrides"] = ov
            self.settings.replace(s)
        self.long_history.remove(name)
        self.grace.remove(name)
        self.eventlog.write("device_removed", device=name, note="machine removed; its earlier log entries are kept")
        self.eventlog.forget(name)
        self.tuning_log.remove(name)
        self.alerts.remove(name)
        self.blocks.remove(name)

    # --- LAN server ---------------------------------------------------------
    def _stop_lan(self) -> None:
        if self.lan_server is not None:
            try:
                self.lan_server.stop()
            except Exception:
                pass
            self.lan_server = None

    def apply_lan(self) -> None:
        lan = self.settings.section("lan")
        if self.shared is None:
            return
        want = bool(lan.get("enabled"))
        port = int(lan.get("port") or 8081)
        bind_mode = str(lan.get("bind_mode") or "all").strip().lower()
        if not want:
            if self.lan_server is not None:
                LOG.info("LAN / remote access turned off")
            self._stop_lan()
            self.lan_bind_host = ""
            self.lan_error = ""
            return
        bind_host = "0.0.0.0"
        if bind_mode == "tailscale":
            ts_ips = tailscale_ip_addresses()
            if not ts_ips:
                self._stop_lan()
                self.lan_bind_host = ""
                self.lan_error = "No Tailscale (100.64.0.0/10) interface detected. Start Tailscale or switch bind mode to All LAN & Tailscale."
                LOG.warning(self.lan_error)
                return
            bind_host = ts_ips[0]
        if self.lan_server is not None and int(self.lan_server.port) == port and self.lan_bind_host == bind_host:
            return
        self._stop_lan()
        try:
            from dashboard import DashboardServer

            self.lan_server = DashboardServer(self.shared, bind_host, port).start()
            self.lan_bind_host = bind_host
            self.lan_error = ""
            LOG.info("Remote access ON at %s:%d (mode=%s, PIN %s, %s)", bind_host, port, bind_mode, "set" if lan.get("pin") else "not set", "read-only" if lan.get("read_only") else "full control")
        except OSError as exc:
            self.lan_server = None
            self.lan_bind_host = ""
            self.lan_error = f"Could not listen on {bind_host}:{port}: {exc}"
            LOG.error(self.lan_error)

    def lan_info(self) -> dict[str, Any]:
        lan = self.settings.section("lan")
        port = int(lan.get("port") or 8081)
        bind_mode = str(lan.get("bind_mode") or "all").strip().lower()
        running = self.lan_server is not None
        ts_ips = tailscale_ip_addresses()
        if running:
            if bind_mode == "tailscale" and self.lan_bind_host:
                urls = [f"http://{self.lan_bind_host}:{port}"]
            else:
                seen = []
                for ip in lan_ip_addresses() + ts_ips:
                    if ip not in seen:
                        seen.append(ip)
                urls = [f"http://{ip}:{port}" for ip in seen]
        else:
            urls = []
        return {
            "enabled": bool(lan.get("enabled")),
            "running": running,
            "port": port,
            "bind_mode": bind_mode,
            "bind_host": self.lan_bind_host,
            "tailscale_ips": ts_ips,
            "urls": urls,
            "error": self.lan_error,
            "read_only": bool(lan.get("read_only")),
            "has_pin": bool(lan.get("pin")),
        }

    # --- status -----------------------------------------------------------
    def device_extras(self, d: dict[str, Any], s: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            net = dict(self.network)
            price = (self.price or {}).get("price")
        name = str(d.get("name") or "")
        cinfo = dict(self.coin_info.get(name) or {"coin": (s["coins"]["overrides"] or {}).get(name) or nk_coins.UNKNOWN, "confidence": "none", "reason": "waiting for data"})
        coin = cinfo["coin"]
        is_alt = coin not in ("BTC", nk_coins.UNKNOWN)
        cmeta = nk_coins.COINS.get(coin) or {}
        algo = str(d.get("algorithm") or cmeta.get("algo") or "sha256d").lower()
        cst: dict[str, Any] = {}
        if is_alt:
            with self._lock:
                cst = dict(self.coin_stats.get(coin) or {})
            diff = cst.get("difficulty") or d.get("network_difficulty")
            height = cst.get("height") or d.get("block_height")
        else:
            diff = net.get("difficulty") or d.get("network_difficulty")
            height = net.get("height") or d.get("block_height")
        fees = self.avg_fees if s["power"].get("include_fees") else 0.0
        online = bool(d.get("online"))
        hr = d.get("hashrate_ghs") if online else None
        hr_hs = d.get("hashrate_hs") if online else None
        if hr_hs is None and hr is not None:
            hr_hs = float(hr) * (1e3 if algo == "equihash" else 1e9)
        watts = d.get("power_w") if online else None
        econ = nk_math.economics(
            hr, watts, diff, height, s["power"]["rate_per_kwh"],
            None if (is_alt or algo != "sha256d") else price,
            0.0 if (is_alt or algo != "sha256d") else (fees or 0.0),
            algorithm=algo,
            hashrate_hs=hr_hs,
            network_hashrate_hs=cst.get("network_hashrate_hs"),
            avg_block_sec=cst.get("avg_block_sec"),
        )
        if is_alt or algo != "sha256d":  # fiat earnings only estimated for BTC
            for k in ("est_sats_per_day", "est_revenue_per_day", "est_profit_per_day", "break_even_rate"):
                econ[k] = None
        merged_coin = str(d.get("merged_coin") or cmeta.get("merged_with") or "").upper()
        if merged_coin and merged_coin in nk_coins.COINS and online and hr_hs:
            with self._lock:
                mst = dict(self.coin_stats.get(merged_coin) or {})
            if mst.get("difficulty") or mst.get("network_hashrate_hs"):
                econ["merged_coin"] = merged_coin
                econ["merged_odds"] = nk_math.block_odds_algo(
                    hr_hs, mst.get("difficulty"), algorithm=algo,
                    network_hashrate_hs=mst.get("network_hashrate_hs"),
                    avg_block_sec=mst.get("avg_block_sec"),
                )
        econ["coin"] = coin
        econ["algorithm"] = algo
        fw: dict[str, Any] = {}
        src = nk_net.firmware_source_for(str(d.get("type") or ""), str(d.get("firmware") or ""), str(d.get("model") or ""), str(d.get("version") or ""))
        latest = (self.firmware.get("latest") or {}).get(src) if src else None
        if latest:
            dev_ver_text = str(d.get("version") or "")
            dv = nk_net.braiins_release_from_text(dev_ver_text + " " + str(d.get("firmware_note") or "")) if src == "braiins" else nk_net.parse_version_tuple(dev_ver_text)
            fw = {
                "source": src,
                "source_label": nk_net.FIRMWARE_SOURCES[src]["label"],
                "latest": latest.get("tag"),
                "url": latest.get("url"),
                "installed": dev_ver_text,
                "update_available": nk_net.version_is_older(dv, tuple(latest.get("version") or [])),
                "installed_parsed": bool(dv),
            }
        aliases = [x for x in (d.get("hostname"), d.get("ip")) if x] or None
        lim = schedule_limits(s, name, self.clock(), aliases=aliases)
        now = self.clock()
        sb = self.session_best.get(name)
        wear = self.chip_health.wear(name, float(s["chip_health"]["wear_mv"]), int(s["chip_health"]["min_days"])) if s["chip_health"]["enabled"] else None
        q = quiet_limits(s, name, now)
        amb = self.ambient.get(name)
        if amb and now - float(amb.get("ts", 0)) > 900:
            amb = None
        pinfo = self.device_payout_info(name, "BTC" if coin == nk_coins.UNKNOWN else coin, s)
        payout_summary = {"source": pinfo["source"], "ready": bool(pinfo["ready"]), "masked": pinfo["masked"], "note": pinfo["note"]}
        return {"econ": econ, "firmware": fw, "schedule": lim, "coin": cinfo, "payout_check": payout_summary,
                "session_best": sb, "pool_health": self.pool_info.get(name), "ambient": amb,
                "chip_health": dict(self.chip_health.summary(name), wear=wear) if s["chip_health"]["enabled"] else None,
                "quiet": q, "restart": self._restart_note.get(name), "faults": self.fault_payload(d, s),
                "maintenance": self.grace.card_info(name)}

    def coins_payload(self, devices: list[dict[str, Any]], s: dict[str, Any]) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for d in devices:
            name = str(d.get("name") or "")
            coin = (self.coin_info.get(name) or {}).get("coin") or (s["coins"]["overrides"] or {}).get(name) or nk_coins.UNKNOWN
            g = groups.setdefault(coin, {"coin": coin, "devices": [], "hashrate_ghs": 0.0, "hashrate_hs": 0.0})
            g["devices"].append(name)
            if d.get("online"):
                g["hashrate_ghs"] += float(d.get("hashrate_ghs") or 0.0)
                hs_v = d.get("hashrate_hs")
                if hs_v is None and d.get("hashrate_ghs") is not None:
                    hs_v = float(d["hashrate_ghs"]) * (1e3 if d.get("algorithm") == "equihash" else 1e9)
                g["hashrate_hs"] += float(hs_v or 0.0)
        out = []
        stats = self.all_coin_stats()
        with self._lock:
            errs = {k: v.get("error", "") for k, v in self.coin_stats.items()}
        for coin in [c for c in nk_coins.COIN_IDS if c in groups] + ([nk_coins.UNKNOWN] if nk_coins.UNKNOWN in groups else []):
            g = groups[coin]
            if coin == nk_coins.UNKNOWN:
                g.update({"label": "Unknown coin", "odds": None, "note": "Set the coin per machine in Settings → Coins if detection cannot tell."})
            else:
                st = stats.get(coin) or {}
                p_sources = {dn: self.device_payout_info(dn, coin, s) for dn in g["devices"]}
                ready = bool(self.payout_addresses_for(coin, s)) or any(pi["ready"] for pi in p_sources.values())
                pool_user_devs = [dn for dn, pi in p_sources.items() if pi["source"] == "pool_user"]
                ignored_devs = [dn for dn, pi in p_sources.items() if pi["source"] == "pool_account_ignored"]
                cmeta = nk_coins.COINS[coin]
                calgo = cmeta.get("algo", "sha256d")
                has_net = bool(st.get("difficulty") or st.get("network_hashrate_hs"))
                g.update({"label": cmeta["label"], "unit": cmeta["unit"], "algo": calgo,
                          "unit_kind": cmeta.get("unit_kind", "hash"),
                          "difficulty": st.get("difficulty"), "height": st.get("height"), "source": st.get("source") or "",
                          "error": errs.get(coin, ""),
                          "odds": nk_math.block_odds_algo(g["hashrate_hs"] or None, st.get("difficulty"), algorithm=calgo,
                                                          network_hashrate_hs=st.get("network_hashrate_hs"),
                                                          avg_block_sec=st.get("avg_block_sec")) if has_net else None,
                          "confirm_ready": ready,
                          "pool_user_devices": pool_user_devs,
                          "pool_account_ignored_devices": ignored_devs})
            out.append(g)
        return out

    def status_payload(self, devices: list[dict[str, Any]], remote: bool = False) -> dict[str, Any]:
        s = self.settings.get()
        per: dict[str, Any] = {}
        tot_hr = tot_w = 0.0
        for d in devices:
            ex = self.device_extras(d, s)
            per[str(d.get("name"))] = ex
            if d.get("online"):
                tot_hr += float(d.get("hashrate_ghs") or 0.0)
                tot_w += float(d.get("power_w") or 0.0)
        with self._lock:
            net = dict(self.network)
            price = dict(self.price)
            events = list(self.events)[-40:]
        fees = self.avg_fees if s["power"].get("include_fees") else 0.0
        btc_hr = sum(float(d.get("hashrate_ghs") or 0.0) for d in devices if d.get("online")
                     and (self.coin_info.get(str(d.get("name"))) or {}).get("coin", nk_coins.UNKNOWN) in ("BTC", nk_coins.UNKNOWN))
        fleet = nk_math.economics(btc_hr or None, tot_w or None, net.get("difficulty"), net.get("height"),
                                  s["power"]["rate_per_kwh"], price.get("price"), fees or 0.0)
        fleet["btc_hashrate_ghs"] = btc_hr
        fleet["all_hashrate_ghs"] = tot_hr
        return {
            "app_name": APP_NAME,
            "donate_address": DONATE_ADDRESS,
            "network": net,
            "price": price,
            "avg_fees_sats": self.avg_fees,
            "fleet_econ": fleet,
            "devices": per,
            "events": events,
            "block_candidates": self.blocks.recent(10),
            "settings": public_settings(s, remote=remote),
            "schedule_status": schedule_summary(s, self.clock()),
            "sounds": {"builtin": nk_sounds.builtin_sound_list(), "custom": self.custom_sounds()},
            "lan": self.lan_info(),
            "firmware_checked_ts": self.firmware.get("checked_ts"),
            "currencies": CURRENCIES,
            "coins": self.coins_payload(devices, s),
            "coin_list": [{"id": k, "label": v["label"]} for k, v in nk_coins.COINS.items()],
            "pools": self.pool_stats.summary(self.clock()),
            "nodes": self.node_monitor.public_status(),
            "webhook_last": dict(self.webhooks.last),
            "fault_history": self.fault_history.list("", 60),
            "layout_keys": {"card_fields": nk_extras.CARD_FIELD_KEYS, "summary_fields": nk_extras.SUMMARY_FIELD_KEYS,
                            "sections": nk_extras.SECTION_KEYS, "section_labels": nk_extras.SECTION_LABELS},
        }

    # --- sounds -----------------------------------------------------------
    def custom_sounds(self) -> list[dict[str, Any]]:
        if not self.sounds_dir or not self.sounds_dir.exists():
            return []
        out = []
        for p in sorted(self.sounds_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in {".wav", ".mp3"}:
                out.append({"id": p.name, "label": p.name, "size": p.stat().st_size})
        return out

    def save_custom_sound(self, filename: str, data: bytes) -> tuple[bool, str, str]:
        if not self.sounds_dir:
            return False, "no userdata folder", ""
        name = safe_sound_filename(filename)
        if not name:
            return False, "Only .wav or .mp3 files are supported", ""
        if len(data) > MAX_SOUND_BYTES:
            return False, "Sound file is larger than 5 MB", ""
        if not nk_sounds.looks_like_audio(data, os.path.splitext(name)[1]):
            return False, "File does not look like a valid WAV/MP3", ""
        self.sounds_dir.mkdir(parents=True, exist_ok=True)
        (self.sounds_dir / name).write_bytes(data)
        return True, "", name

    # --- settings ---------------------------------------------------------
    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        old = self.settings.get()
        patch = copy.deepcopy(patch)
        for sec, key in SECRET_SETTING_PATHS:
            if isinstance(patch.get(sec), dict) and key in patch[sec] and patch[sec][key] in (None, "__keep__"):
                patch[sec][key] = (old.get(sec) or {}).get(key, "")
        for sec in ("layout",):  # lists are replaced, not merged
            if isinstance(patch.get(sec), dict):
                merged = dict(old.get(sec) or {})
                merged.update(patch[sec])
                patch[sec] = merged
        new = self.settings.update(patch)
        if new.get("nodes") != old.get("nodes"):
            self.node_monitor.wake()
        try:
            self.eventlog.configure(new.get("eventlog"))
            self.eventlog.settings_changed(strip_secrets_deep(old), strip_secrets_deep(new))
        except Exception:
            pass
        if new["lan"] != old["lan"]:
            self.apply_lan()
        if new["updates"]["enabled"] != old["updates"]["enabled"]:
            try:
                self.updates.set_enabled(new["updates"]["enabled"])
            except Exception:
                LOG.exception("live updates: enable/disable failed")
        if new["power"]["currency"] != old["power"]["currency"] or new["block"] != old["block"]:
            self._last_price = 0.0
            self._last_net = 0.0
            self._wake.set()
        if new.get("schedule") != old.get("schedule") or new.get("quiet") != old.get("quiet"):
            if self.tuner is not None and hasattr(self.tuner, "wake"):
                self.tuner.wake()
        return new

    # --- v1.16: live updates ---------------------------------------------------
    def _pools_route(self, method: str, route: str, qv: Callable[..., str], payload: Callable[[], Any],
                     remote: bool) -> Optional[tuple[int, bytes, str]]:
        """v1.16.2: saved pools. Passwords never leave this PC except in an export made on this PC with passwords ticked.
        LAN clients go through the usual guard (read-only mode blocks every change)."""
        lib = self.pools
        if method in {"GET", "HEAD"}:
            if route == "/api/nk/pools":
                return _json_resp({"ok": True, "pools": lib.list_public(), "max": nk_pools.MAX_POOLS,
                                   "worker_placeholder": nk_pools.WORKER_PLACEHOLDER, "error": lib.load_error,
                                   "coins": sorted(nk_coins.COINS)})
            if route == "/api/nk/pools/export":
                with_pw = qv("passwords") in {"1", "true", "yes"}
                if with_pw and remote:
                    return _json_resp({"ok": False, "error": "Passwords can only be exported on this PC."}, 403)
                data = json.dumps(lib.export(include_passwords=with_pw), indent=1, ensure_ascii=False).encode("utf-8")
                return 200, data, "application/json"
            return None
        if method != "POST":
            return None
        p = payload()
        if not isinstance(p, dict):
            return _json_resp({"ok": False, "error": "expected a JSON object"}, 400)
        try:
            if route == "/api/nk/pools/save":
                if p.get("id"):
                    ent = lib.update(str(p["id"]), p)
                    what = "pool_saved_edit"
                else:
                    ent = lib.add(p)
                    what = "pool_saved_add"
                self._pool_log(what, ent)
                return _json_resp({"ok": True, "pool": ent, "pools": lib.list_public()})
            if route == "/api/nk/pools/delete":
                ok = lib.delete(str(p.get("id") or ""))
                if ok:
                    self._pool_log("pool_saved_delete", {"id": p.get("id")})
                return _json_resp({"ok": ok, "pools": lib.list_public(), **({} if ok else {"error": "not found"})}, 200 if ok else 404)
            if route == "/api/nk/pools/move":
                lib.move(str(p.get("id") or ""), int(p.get("delta") or 0))
                return _json_resp({"ok": True, "pools": lib.list_public()})
            if route == "/api/nk/pools/reorder":
                lib.reorder(list(p.get("ids") or []))
                return _json_resp({"ok": True, "pools": lib.list_public()})
            if route == "/api/nk/pools/from-device":
                dev = str(p.get("device") or "")
                snap = self._snap(dev)
                if not snap:
                    return _json_resp({"ok": False, "error": f"unknown or offline machine: {dev}"}, 404)
                raw = nk_pools.PoolLibrary.entry_from_device(snap, p.get("slot", "primary"))
                raw["name"] = str(p.get("name") or "").strip()
                raw["password"] = p.get("password") or ""
                if p.get("coin"):
                    raw["coin"] = p.get("coin")
                ent = lib.add(raw)
                self._pool_log("pool_saved_from_device", ent, device=dev, slot=str(p.get("slot", "primary")))
                return _json_resp({"ok": True, "pool": ent, "pools": lib.list_public()})
            if route == "/api/nk/pools/import":
                res = lib.import_(p.get("data"), replace=bool(p.get("replace")))
                self._pool_log("pool_saved_import", {"added": res["added"]})
                return _json_resp({"ok": True, **res, "pools": lib.list_public()})
            if route == "/api/nk/pools/resolve":  # what the pool editor fills in (no password)
                r = lib.resolve(str(p.get("id") or ""), str(p.get("device") or "") or None, bool(p.get("use_machine_worker")))
                r.pop("password", None)
                return _json_resp({"ok": True, "pool": r})
        except nk_pools.PoolError as exc:
            return _json_resp({"ok": False, "error": str(exc)}, 400)
        except (TypeError, ValueError) as exc:
            return _json_resp({"ok": False, "error": f"invalid request: {exc}"}, 400)
        return None

    # --- v1.17.0: node monitoring ---------------------------------------------
    def _node_net_height(self, coin: str) -> Optional[int]:
        if coin != "BTC":
            return None
        with self._lock:
            h = (self.network or {}).get("height")
        try:
            return int(h) if h else None
        except (TypeError, ValueError):
            return None

    def _node_event(self, title: str, message: str, level: str, sound: bool) -> None:
        self.emit(_event("node", level, "", title, message, sound=sound))

    def _nodes_route(self, method: str, route: str, payload: Callable[[], Any], remote: bool) -> Optional[tuple[int, bytes, str]]:
        """Node list without credentials. Saving / testing nodes only on this computer (credentials never cross the LAN)."""
        mon = self.node_monitor
        if method in {"GET", "HEAD"}:
            if route == "/api/nk/nodes":
                return _json_resp({"ok": True, "nodes": mon.store.list_public(), "status": mon.public_status(),
                                   "kinds": nk_nodes.KINDS, "coins": list(nk_nodes.COINS), "default_ports": nk_nodes.DEFAULT_PORTS,
                                   "error": mon.store.load_error})
            return None
        if method != "POST":
            return None
        if remote:
            return _json_resp({"ok": False, "error": "Nodes can only be added or changed on the Miner Hearth computer itself."}, 403)
        p = payload()
        if not isinstance(p, dict):
            return _json_resp({"ok": False, "error": "expected a JSON object"}, 400)
        try:
            if route == "/api/nk/nodes/save":
                ent = mon.store.update(str(p["id"]), p) if p.get("id") else mon.store.add(p)
                self.eventlog.write("node_saved", level="info", node_kind=ent["kind"], node_name=ent["name"])
                mon.wake()
                return _json_resp({"ok": True, "node": ent, "nodes": mon.store.list_public()})
            if route == "/api/nk/nodes/delete":
                ok = mon.store.delete(str(p.get("id") or ""))
                return _json_resp({"ok": ok, "nodes": mon.store.list_public(), **({} if ok else {"error": "not found"})}, 200 if ok else 404)
            if route == "/api/nk/nodes/test":
                return _json_resp(mon.test(p))
            if route == "/api/nk/nodes/refresh":
                threading.Thread(target=lambda: mon.tick(force=True), name="mh-nodes-refresh", daemon=True).start()
                return _json_resp({"ok": True})
        except nk_nodes.NodeError as exc:
            return _json_resp({"ok": False, "error": str(exc)}, 400)
        except (TypeError, ValueError, KeyError) as exc:
            return _json_resp({"ok": False, "error": f"invalid request: {exc}"}, 400)
        return None

    def _pool_log(self, what: str, ent: dict[str, Any], **extra: Any) -> None:
        try:
            self.eventlog.write(what, level="info", pool_name=str(ent.get("name") or ""), pool_host=str(ent.get("host") or ""),
                                pool_id=str(ent.get("id") or ""), **extra)
        except Exception:
            pass

    def _update_route(self, method: str, route: str, payload: Callable[[], Any], remote: bool) -> Optional[tuple[int, bytes, str]]:
        s = self.settings.get()
        up = s["updates"]
        if method in {"GET", "HEAD"}:
            if route == "/api/nk/update/status":
                return _json_resp({"ok": True, **self.updates.status()})
            if route.startswith("/api/nk/update/ui/"):
                f = self.updates.ui_file(route.rsplit("/", 1)[1])
                if f is None:
                    return _json_resp({"ok": False, "error": "not found"}, 404)
                return 200, f[0], f[1]
            return None
        if method != "POST":
            return None
        if remote:
            return _json_resp({"ok": False, "error": "Live updates can only be managed on this PC (not over the LAN)."}, 403)
        if not up["enabled"]:
            return _json_resp({"ok": False, "error": "Live updates are off. Turn them on in Settings > Live Updates first."}, 403)
        p = payload() or {}
        try:
            if route == "/api/nk/update/check":
                data = None
                if p.get("b64"):
                    try:
                        data = base64.b64decode(str(p["b64"]), validate=False)
                    except (ValueError, TypeError):
                        return _json_resp({"ok": False, "error": "The uploaded file could not be read."}, 400)
                rep = self.updates.check(up, data=data, url=str(p.get("url") or ""), path=str(p.get("path") or ""),
                                         expected_sha256=str(p.get("expected_sha256") or ""))
                self._update_log("update_checked", rep)
                return _json_resp({"ok": True, "report": rep})
            if route == "/api/nk/update/apply":
                res = self.updates.apply(up, str(p.get("token") or ""))
                self._update_log("update_applied", res["report"], restart_required=res["restart_required"], hot=res["hot_reloaded"])
                self.emit(_event("update", "info", "", f"Update {res['applied']} applied",
                                 "Restart needed for part of it (use Quick restart)." if res["restart_required"] else "Applied while running; no restart needed.",
                                 sound=False))
                return _json_resp({"ok": True, **res, "status": self.updates.status()})
            if route == "/api/nk/update/rollback":
                res = self.updates.rollback()
                self._update_log("update_rollback", {}, now=res["now"], restart_required=res["restart_required"])
                return _json_resp({"ok": True, **res, "status": self.updates.status()})
            if route == "/api/nk/update/remove":
                res = self.updates.remove_all()
                self._update_log("update_removed", {}, restart_required=res["restart_required"])
                return _json_resp({"ok": True, **res, "status": self.updates.status()})
            if route == "/api/nk/update/restart":
                if self.restart_hook is None:
                    return _json_resp({"ok": False, "error": "Quick restart is only available in the desktop app; close and reopen Miner Hearth."}, 400)
                self._update_log("update_restart", {})
                hook = self.restart_hook
                _clear_restart_pending(self.updates.ud)  # v1.17.1: so an older program started next isn't left asking for one
                if getattr(sys, "frozen", False):  # v1.17.1: the new copy unpacks itself (PyInstaller 6.9+ one-file)
                    os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
                threading.Thread(target=lambda: (time.sleep(0.7), hook()), name="nk-quick-restart", daemon=True).start()
                return _json_resp({"ok": True, "restarting": True})
        except nk_update.UpdateError as exc:
            self._update_log("update_rejected", {}, error=str(exc)[:300])
            return _json_resp({"ok": False, "error": str(exc)}, 400)
        return None

    def _update_log(self, kind: str, rep: dict[str, Any], **extra: Any) -> None:
        try:
            self.eventlog.write(kind, level="info", version=rep.get("version"), package_sha256=rep.get("package_sha256"),
                                signature=rep.get("signature"), files=len(rep.get("files") or []), **extra)
        except Exception:
            pass

    # --- HTTP routes --------------------------------------------------------
    def handle(self, method: str, route: str, query: str, body: bytes, remote: bool) -> Optional[tuple[int, bytes, str]]:
        q = parse_qs(query or "")

        def qv(k: str, default: str = "") -> str:
            return (q.get(k) or [default])[0]

        def payload() -> Any:
            try:
                return json.loads(body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None

        if route.startswith("/api/nk/update/"):
            return self._update_route(method, route, payload, remote)
        if route == "/api/nk/pools" or route.startswith("/api/nk/pools/"):
            return self._pools_route(method, route, qv, payload, remote)
        if route == "/api/nk/nodes" or route.startswith("/api/nk/nodes/"):
            return self._nodes_route(method, route, payload, remote)
        if method in {"GET", "HEAD"}:
            if route == "/api/nk/settings":
                cur_s = self.settings.get()
                return _json_resp({"ok": True, "settings": public_settings(cur_s, remote=remote), "schedule_status": schedule_summary(cur_s, self.clock()), "lan": self.lan_info()})
            if route == "/api/nk/fault-history":
                return _json_resp({"ok": True, "entries": self.fault_history.list(qv("device"), int(qv("limit", "200") or 200))})
            if route == "/api/nk/diagnostics":
                return _json_resp(self.diagnostics(qv("device")))
            if route == "/api/nk/eventlog/status":
                return _json_resp({"ok": True, "status": self.eventlog.stats()})
            if route == "/api/nk/eventlog/tail":
                types = {t for t in qv("types").split(",") if t} or None
                return _json_resp({"ok": True, "entries": self.eventlog.tail(int(qv("limit", "200") or 200), types, qv("device"))})
            if route == "/api/nk/eventlog/export":
                if remote:
                    return _json_resp({"ok": False, "error": "export the full log on the Miner Hearth PC"}, 403)
                rng = qv("range", "24h")
                data, man = self.eventlog_export(rng, anonymize=qv("anon") in {"1", "true", "yes"})
                self.eventlog.write("user_export_log", level="info", range=man.get("range"), entries=man.get("entries"), bytes=len(data),
                                    anonymized=man.get("machine_names_anonymized"))
                return 200, data, "application/zip"
            if route == "/api/nk/fault-records":
                return _json_resp({"ok": True, "records": self.fault_records.list_records(qv("device"))})
            if route == "/api/nk/fault-records/view":
                rec = self.fault_records.get_record(qv("id"), redact=True, max_samples=360)
                if rec is None:
                    return _json_resp({"ok": False, "error": "fault record not found"}, 404)
                return _json_resp({"ok": True, "record": rec})
            if route == "/api/nk/fault-records/export":
                rec = self.fault_records.get_record(qv("id"), redact=True, max_samples=None)
                if rec is None:
                    return _json_resp({"ok": False, "error": "fault record not found"}, 404)
                return _json_resp(rec)
            if route == "/api/nk/fault-catalog":
                return _json_resp({"ok": True, "count": len(nk_faults.CATALOG), "guides": nk_faults.GUIDES})
            if route == "/api/tuning-log":
                return _json_resp({"ok": True, "entries": self.tuning_log.list(qv("device"), int(qv("limit", "200") or 200))})
            if route == "/api/history":
                dev, rng = qv("device"), qv("range", "7d")
                rows = self._rows(dev, rng)
                return _json_resp({"ok": True, "device": dev or "__fleet__", "range": rng, "points": rows})
            if route == "/api/export.csv":
                dev, rng = qv("device"), qv("range", "7d")
                rows = self._rows(dev, rng)
                return 200, rows_to_csv(rows, dev or "fleet").encode("utf-8"), "text/csv; charset=utf-8"
            if route.startswith("/api/sounds/builtin/"):
                sid = route.rsplit("/", 1)[-1].removesuffix(".wav")
                data = nk_sounds.get_builtin_wav(sid)
                if data is None:
                    return _json_resp({"ok": False, "error": "unknown sound"}, 404)
                return 200, data, "audio/wav"
            if route.startswith("/api/sounds/custom/"):
                name = safe_sound_filename(unquote(route.rsplit("/", 1)[-1]))
                p = (self.sounds_dir / name) if (self.sounds_dir and name) else None
                if not p or not p.exists():
                    return _json_resp({"ok": False, "error": "unknown sound"}, 404)
                return 200, p.read_bytes(), "audio/mpeg" if name.lower().endswith(".mp3") else "audio/wav"
            if route == "/api/pools":
                return _json_resp({"ok": True, "pools": self.pool_stats.summary(self.clock())})
            if route == "/api/chip-health":
                dev = qv("device")
                return _json_resp({"ok": True, "device": dev, "health": self.chip_health.summary(dev), "points": (self.chip_health.devices.get(dev) or {}).get("points", {})})
            if route == "/api/sounds":
                return _json_resp({"ok": True, "builtin": nk_sounds.builtin_sound_list(), "custom": self.custom_sounds()})
            if route == "/api/clipboard":
                if remote:
                    return _json_resp({"ok": False, "error": "clipboard read is localhost-only"}, 403)
                return _json_resp({"ok": True, "text": read_from_clipboard()})
            if route == "/api/backup/export":
                inc = qv("include_passwords") in {"1", "true", "yes"} and not remote
                return _json_resp(build_backup(self.tuner, self.settings.get(), include_passwords=inc))
            if route == "/api/nk/compat-capture/download":
                fn = Path(unquote(qv("file"))).name
                if not fn or not fn.endswith(".zip") or "/" in fn or "\\" in fn:
                    return _json_resp({"ok": False, "error": "invalid filename"}, 400)
                cap_dir = (self.userdata_dir / "compat-captures") if self.userdata_dir else Path("userdata/compat-captures")
                zp = cap_dir / fn
                if not zp.exists():
                    return _json_resp({"ok": False, "error": "capture zip not found"}, 404)
                return 200, zp.read_bytes(), "application/zip"
            return None

        if method != "POST":
            return None
        if route == "/api/nk/settings":
            p = payload()
            if not isinstance(p, dict):
                return _json_resp({"ok": False, "error": "expected JSON object"}, 400)
            raw_set = p.get("settings", p)
            if isinstance(raw_set, dict) and isinstance(raw_set.get("webhooks"), dict):
                wh_in = raw_set["webhooks"]
                if "discord_url" in wh_in and wh_in["discord_url"] not in (None, "", "__keep__"):
                    ok_u, err_u = nk_extras.validate_discord_webhook_url(str(wh_in["discord_url"]))
                    if not ok_u:
                        return _json_resp({"ok": False, "error": err_u}, 400)
            new = self.update_settings(raw_set)
            return _json_resp({"ok": True, "settings": public_settings(new), "schedule_status": schedule_summary(new, self.clock()), "lan": self.lan_info()})
        if route == "/api/sounds/upload":
            p = payload()
            if not isinstance(p, dict):
                return _json_resp({"ok": False, "error": "expected JSON object"}, 400)
            try:
                data = base64.b64decode(str(p.get("data_base64") or ""), validate=True)
            except (ValueError, TypeError):
                return _json_resp({"ok": False, "error": "invalid base64 data"}, 400)
            ok, err, name = self.save_custom_sound(str(p.get("filename") or ""), data)
            if not ok:
                return _json_resp({"ok": False, "error": err}, 400)
            return _json_resp({"ok": True, "id": f"custom:{name}", "custom": self.custom_sounds()})
        if route == "/api/sounds/delete":
            p = payload() or {}
            name = safe_sound_filename(str(p.get("filename") or ""))
            if name and self.sounds_dir and (self.sounds_dir / name).exists():
                (self.sounds_dir / name).unlink()
            s = self.settings.get()
            patch: dict[str, Any] = {"sounds": {}}
            for k, default in (("block_sound", "builtin:victory_fanfare"), ("problem_sound", "builtin:alarm")):
                if s["sounds"][k] == f"custom:{name}":
                    patch["sounds"][k] = default
            if patch["sounds"]:
                self.update_settings(patch)
            return _json_resp({"ok": True, "custom": self.custom_sounds()})
        if route == "/api/nk/test-notification":
            p = payload() or {}
            level = "block" if p.get("kind") == "block" else "problem"
            ev = self.emit(_event("test", level, "", f"Test {'block' if level == 'block' else 'problem'} notification",
                                  "This is a test from Settings. If you can see and hear this, notifications work.", test=True))
            return _json_resp({"ok": True, "event": ev})
        if route == "/api/nk/test-webhook":
            s = self.settings.get()
            d_url = str((s.get("webhooks") or {}).get("discord_url") or "")
            if d_url:
                ok_u, err_u = nk_extras.validate_discord_webhook_url(d_url)
                if not ok_u:
                    return _json_resp({"ok": False, "error": err_u}, 400)
            tp = payload() or {}
            if str(tp.get("kind") or "") == "block":
                ev = _event("block_possible", "info", "Test Machine", "TEST: Block Found #123456",
                            "Machine: Test Machine\nCoin: XEC\nPayout address: (the address in your miner's pool username)\n"
                            "Worker: test-worker\nShare difficulty: 2.35M\nNetwork difficulty: 1.05M\n"
                            "This is a test from Settings. A real block sends this \"Block Found\" alert, then \"Block Confirmed\" "
                            "with the same fields once the coin's blockchain confirms it.", sound=False, test=True)
            else:
                ev = _event("test", "problem", "", "Miner Hearth test alert", "Webhook test from Settings. If you can read this, alerts will reach you here.", test=True)
            res = self.webhooks.send(dict(s["webhooks"], enabled=True), ev, force=True, on_result=self._webhook_logged)
            if not res:
                return _json_resp({"ok": False, "error": "No webhook is configured (Discord URL, Telegram token + chat id, or ntfy URL)."}, 400)
            return _json_resp({"ok": all(r.get("ok") for r in res), "results": res})
        if route == "/api/nk/maintenance":
            p = payload() or {}
            dev = str(p.get("device") or "")
            if not dev or (self.tuner is not None and dev not in {d.name for d in self.tuner.config.devices}):
                return _json_resp({"ok": False, "error": "unknown machine"}, 404)
            try:
                minutes = float(p.get("minutes") or 0)
            except (TypeError, ValueError):
                return _json_resp({"ok": False, "error": "minutes must be a number"}, 400)
            until = self.pause_alerts(dev, minutes, str(p.get("reason") or "")[:200])
            return _json_resp({"ok": True, "until": until, "maintenance": self.grace.card_info(dev)})
        if route == "/api/nk/eventlog/mark":
            p = payload() or {}
            act = str(p.get("action") or "")
            if act not in {"alert_dismissed", "alerts_cleared", "note"}:
                return _json_resp({"ok": False, "error": "unknown action"}, 400)
            self.eventlog.write("user_" + act, device=str(p.get("device") or "")[:80], level="info",
                                alert_id=p.get("id"), kind=str(p.get("kind") or "")[:60], title=str(p.get("title") or "")[:300],
                                text=str(p.get("text") or "")[:1000],
                                note="Dismissing hides the banner only; the alert stays in this log.")
            return _json_resp({"ok": True})
        if route == "/api/nk/clear-fault":
            p = payload() or {}
            code, res = self.clear_fault(str(p.get("device") or ""), str(p.get("action") or ""), p.get("confirm") is True)
            return _json_resp(res, code)
        if route == "/api/nk/log-snapshot":
            p = payload() or {}
            code, res = self.log_snapshot(str(p.get("device") or ""))
            return _json_resp(res, code)
        if route == "/api/nk/fault-records/delete":
            p = payload() or {}
            ok, err = self.fault_records.delete_record(str(p.get("id") or ""))
            self.eventlog.write("user_fault_record_deleted", level="info", record_id=str(p.get("id") or "")[:120], ok=ok, error=err)
            return _json_resp({"ok": ok, "error": err}, 200 if ok else 404)
        if route == "/api/nk/web-login":
            if remote:
                return _json_resp({"ok": False, "error": "miner logins can only be changed on the host PC"}, 403)
            p = payload() or {}
            if self.tuner is None:
                return _json_resp({"ok": False, "error": "tuner not attached"}, 503)
            ok, err, info = self.tuner.update_web_login(str(p.get("device") or ""), p)
            return _json_resp({"ok": ok, "error": err, **info}, 200 if ok else 400)
        if route == "/api/nk/reset-layout":
            new = self.settings.update({"layout": nk_extras.default_layout()})
            return _json_resp({"ok": True, "settings": public_settings(new)})
        if route == "/api/nk/refresh-network":
            self._last_net = self._last_price = 0.0
            self._wake.set()
            return _json_resp({"ok": True})
        if route == "/api/nk/check-firmware":
            threading.Thread(target=lambda: self.check_firmware(force=True), daemon=True).start()
            return _json_resp({"ok": True})
        if route == "/api/clipboard":
            p = payload() or {}
            ok = copy_to_clipboard(str(p.get("text") or ""))
            return _json_resp({"ok": ok, "error": "" if ok else "clipboard not available"}, 200 if ok else 501)
        if route == "/api/nk/compat-capture":
            if remote:
                return _json_resp({"ok": False, "error": "compatibility capture can only be run from the host PC"}, 403)
            p = payload() or {}
            if not isinstance(p, dict):
                return _json_resp({"ok": False, "error": "expected JSON object"}, 400)
            dev_name = str(p.get("device") or "").strip()
            ip = str(p.get("ip") or "").strip()
            http_port = int(p.get("http_port") or 80)
            cgminer_port = int(p.get("cgminer_port") or 4028)
            web_user = str(p.get("web_username") or "").strip()
            web_pass = str(p.get("web_password") or "")
            label = str(p.get("label") or dev_name or ip or "miner").strip()
            if dev_name:
                if self.tuner is None:
                    return _json_resp({"ok": False, "error": "tuner not attached"}, 503)
                cfg = next((d for d in self.tuner.config.devices if d.name == dev_name), None)
                if cfg is None:
                    return _json_resp({"ok": False, "error": f"unknown device: {dev_name}"}, 404)
                ip = cfg.ip
                http_port = int(cfg.port or 80)
                cgminer_port = int(cfg.cgminer_port or 4028)
                if not web_user and cfg.web_username:
                    web_user = cfg.web_username
                if not web_pass and cfg.web_password:
                    web_pass = cfg.web_password
            if not ip:
                return _json_resp({"ok": False, "error": "device or ip is required"}, 400)
            import autotune
            import nk_compat
            cap_dir = (self.userdata_dir / "compat-captures") if self.userdata_dir else Path("userdata/compat-captures")
            try:
                res = nk_compat.collect_and_save_capture(
                    ip,
                    cap_dir,
                    label=label,
                    http_port=http_port,
                    cgminer_port=cgminer_port,
                    web_username=web_user,
                    web_password=web_pass,
                    app_version=autotune.APP_VERSION,
                )
            except Exception as exc:
                return _json_resp({"ok": False, "error": str(exc)}, 500)
            res["download_url"] = f"/api/nk/compat-capture/download?file={res['filename']}"
            return _json_resp(res)
        if route == "/api/backup/import":
            p = payload()
            ok, err = validate_backup(p)
            if not ok:
                return _json_resp({"ok": False, "error": err}, 400)
            ok, err, summary = self.restore_backup(p)
            if not ok:
                return _json_resp({"ok": False, "error": err}, 400)
            return _json_resp({"ok": True, **summary})
        return None

    def _rows(self, dev: str, rng: str) -> list[list[Any]]:
        names = [d.name for d in self.tuner.config.devices] if self.tuner is not None else []
        if rng == "24h":
            def get24(n: str) -> list[list[Any]]:
                pts = self.tuner.history.get_series(n, max_return=1440) if self.tuner is not None else []
                return [list(r[:4]) + [None] for r in pts]
            if dev and dev != "__fleet__":
                return get24(dev)
            # align 24h points to 5-min buckets for a fleet total
            per = {}
            for n in names:
                b: dict[float, list[Any]] = {}
                for r in get24(n):
                    bts = float(int(r[0] // 300) * 300)
                    b[bts] = [bts] + list(r[1:])
                per[n] = [b[k] for k in sorted(b)]
            return fleet_series(per)
        tier = rng if rng in TIERS else "7d"
        if dev and dev != "__fleet__":
            return self.long_history.series(dev, tier)
        return fleet_series({n: self.long_history.series(n, tier) for n in names})

    # --- v1.10: faults, Clear/Reset, log snapshot, diagnostics -------------------
    FAULT_PROBE_SEC = 120
    LOG_MAX_BYTES = 256 * 1024

    def _snap(self, name: str) -> dict[str, Any]:
        sh = self.shared
        if sh is None:
            return {}
        try:
            with sh.lock:
                return dict(sh.snapshots.get(name) or {})
        except Exception:
            return {}

    @staticmethod
    def device_family(d: dict[str, Any]) -> str:
        t = str(d.get("type") or "")
        if t == "antminer":
            fw = f"{d.get('firmware') or ''} {d.get('firmware_note') or ''} {d.get('version') or ''}".lower()
            if "braiins" in fw or "bos" in fw.split() or d.get("bos_has_password") or d.get("bos_power_mgmt"):
                return "braiins"
            if "stock bitmain" in fw or "bmminer" in fw:
                return "bitmain"
            return "other"
        if t == "nerdoctaxe":
            return "nerd"
        meta = f"{d.get('model') or ''} {d.get('version') or ''} {d.get('firmware') or ''}".lower()
        if "nerd" in meta:
            return "nerd"
        return "axeos"

    def _fault_raw_for(self, name: str, raw: dict[str, Any], now: float) -> dict[str, Any]:
        merged = dict(raw or {})
        pr = self.fault_probe.get(name) or {}
        if pr and now - float(pr.get("ts", 0)) < 600:
            for k, v in (pr.get("data") or {}).items():
                if not k.startswith("_") and k not in merged:
                    merged[k] = v
        return merged

    def _track_baselines_and_reboots(self, name: str, raw: dict[str, Any], uptime_sec: Optional[float], now: float) -> None:
        chains = raw.get("chains") if isinstance(raw.get("chains"), list) else []
        active_chains = sum(1 for c in chains if isinstance(c, dict) and (c.get("acn") or 0) > 0)
        if active_chains > 0:
            prev_c = self._baseline_chains.get(name, 0)
            if active_chains > prev_c:
                self._baseline_chains[name] = active_chains
            elif prev_c > active_chains and not any((c.get("acn") == 0 and c.get("present", True)) for c in chains if isinstance(c, dict)):
                raw["chain_count_dropped"] = f"Active chains dropped from {prev_c} to {active_chains}"
        fans = raw.get("fans") if isinstance(raw.get("fans"), list) else []
        if fans:
            prev_f = self._baseline_fans.get(name, 0)
            if len(fans) > prev_f and any((f or 0) > 0 for f in fans):
                self._baseline_fans[name] = len(fans)
            elif prev_f > len(fans):
                raw["fan_count_dropped"] = f"Reported fans dropped from {prev_f} to {len(fans)}"
        if uptime_sec is not None and uptime_sec >= 0:
            prev_u, boots = self._uptime_track.get(name, (uptime_sec, []))
            boots = [t for t in boots if now - t < 7200]
            if prev_u > 300 and uptime_sec < prev_u - 60 and uptime_sec < 300:
                boots.append(now)
            self._uptime_track[name] = (uptime_sec, boots)
            if len(boots) >= 3:
                raw["recent_reboots_2h"] = len(boots)

    def _on_poll_v110(self, cfg: Any, reading: Any, now: float, s: dict[str, Any]) -> list[dict[str, Any]]:
        name = cfg.name
        out: list[dict[str, Any]] = []
        if not s["faults"]["enabled"]:
            self.faults_now.pop(name, None)
            return out
        if not (getattr(reading, "ok", False) or getattr(reading, "reachable", False)):
            return out  # offline: keep the last known faults, the offline alert covers it
        fam = self.device_family({
            "type": getattr(cfg, "type", ""),
            "model": getattr(reading, "model", ""),
            "version": getattr(reading, "version", ""),
            "firmware": getattr(reading, "firmware", ""),
            "firmware_note": getattr(reading, "firmware_note", ""),
            "bos_has_password": bool(getattr(cfg, "bos_password", "")),
            "bos_power_mgmt": bool(getattr(cfg, "bos_power_mgmt", False)),
        })
        if (
            name not in self.fault_probe
            and s["faults"]["probe_enabled"]
            and self.tuner is not None
            and hasattr(self.tuner, "probe_extra_faults")
            and (
                (fam == "nerd" and getattr(reading, "source", "") != "GET /api/v2/dashboard")
                or (fam == "braiins" and bool(getattr(cfg, "bos_password", "")))
                or (fam in {"bitmain", "other"} and bool(getattr(cfg, "web_password", "")))
            )
        ):
            try:
                data = self.tuner.probe_extra_faults(name, fam)
                self.fault_probe[name] = {"ts": now, "data": data, "error": ""}
            except Exception as exc:  # noqa: BLE001
                self.fault_probe[name] = {"ts": now, "data": {}, "error": str(exc)[:200]}
        raw = self._fault_raw_for(name, getattr(reading, "fault_raw", {}) or {}, now)
        up = getattr(reading, "uptime_sec", None)
        self._track_baselines_and_reboots(name, raw, up, now)

        # v1.12: Track fan tach presence (suppress false stall on Gamma 601 with no RPM sensor)
        ctx = self.fault_context.get(name)
        tach_seen = dict(ctx.get("fan_tach_seen") or {})
        tach_changed = False
        fans_list = getattr(reading, "fans", None) or []
        for idx, fan in enumerate(fans_list, start=1):
            if isinstance(fan, dict) and (fan.get("rpm") or 0) > 0:
                if not tach_seen.get(f"fan{idx}"):
                    tach_seen[f"fan{idx}"] = True
                    tach_changed = True
        for rkey, lbl in (("fanrpm", "fan1"), ("fan2rpm", "fan2")):
            try:
                if float(raw.get(rkey) or 0) > 0 and not tach_seen.get(lbl):
                    tach_seen[lbl] = True
                    tach_changed = True
            except (TypeError, ValueError):
                pass
        if isinstance(raw.get("nerd_fans"), list):
            for idx, nf in enumerate(raw["nerd_fans"], start=1):
                try:
                    if isinstance(nf, dict) and float(nf.get("rpm") or 0) > 0 and not tach_seen.get(f"fan{idx}"):
                        tach_seen[f"fan{idx}"] = True
                        tach_changed = True
                except (TypeError, ValueError):
                    pass
        if tach_changed:
            self.fault_context.update(name, {"fan_tach_seen": tach_seen})
        raw["fan_tach_seen"] = tach_seen
        if fam != "nerd" and isinstance(fans_list, list):
            for idx, fan in enumerate(fans_list, start=1):
                if isinstance(fan, dict):
                    lbl = f"fan{idx}"
                    if (fan.get("rpm") is None or fan.get("rpm") == 0) and (fan.get("speed_pct") or 0) > 0:
                        if tach_seen.get(lbl):
                            fan["no_tach"] = False
                            fan["rpm"] = 0
                        else:
                            fan["no_tach"] = True
                            fan["rpm"] = None
                            raw["fan_no_tach"] = True
            if self.shared is not None:
                self.shared.update_device(name, {"fans": list(fans_list)})

        # v1.12: Remember VR overheat even if firmware watchdog clears shutdown/boardError flags
        vr_val, vr_comp = nk_faults._vr_sensor_pick(raw)
        vr_sensor = "vrTempInt" if vr_comp.startswith("vrTempInt") else "vrTemp"
        vr_lim = getattr(cfg, "vr_max_c", 65.0)
        hr_now = getattr(reading, "hashrate", None)
        pwr_now = getattr(reading, "power", None)
        be_now = raw.get("boardError")
        vr_hot_event = (
            (vr_val is not None and (vr_val >= max(float(vr_lim), 65.0) or vr_val >= 94.0))
            or str(be_now) == "2"
        )
        last_vr = ctx.get("last_vr_overheat") if isinstance(ctx.get("last_vr_overheat"), dict) else None
        if vr_hot_event:
            rec_vr = {
                "temp_c": float(vr_val) if vr_val is not None else float(vr_lim),
                "vr_max_c": float(vr_lim),
                "sensor": vr_sensor,
                "ts": now,
                "iso": nk_faults._fmt_ts_local(now),
            }
            self.fault_context.update(name, {"last_vr_overheat": rec_vr})
            self._healthy_vr_since.pop(name, None)
            last_vr = rec_vr
        elif last_vr:
            # Clear remembered VR overheat only after 15 minutes of healthy powered hashing
            if (hr_now is not None and hr_now > 0) and (pwr_now is None or pwr_now > 5.0) and (vr_val is None or vr_val < float(getattr(cfg, "vr_target_c", 62.0))):
                hs = self._healthy_vr_since.setdefault(name, now)
                if now - hs >= 900.0:
                    self.fault_context.update(name, {"last_vr_overheat": None})
                    last_vr = None
            else:
                self._healthy_vr_since.pop(name, None)
        if last_vr and (now - float(last_vr.get("ts") or now)) <= 86400.0:
            raw["nk_last_vr_overheat"] = last_vr

        faults = nk_faults.collect_faults(raw, up)
        log_lines: list[str] = []
        if self.log_dir is not None:
            lpath = self.log_dir / (_safe_name(name)[:-5] + ".txt")
            if lpath.exists():
                try:
                    log_lines = lpath.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
                except OSError:
                    pass
        active_since = {k: float(v.get("since") or now) for k, v in (self.fault_history.active.get(name) or {}).items() if isinstance(v, dict)}
        faults = nk_faults.annotate_fault_causes(
            faults,
            {
                "now": now,
                "active_since": active_since,
                "last_vr_overheat": raw.get("nk_last_vr_overheat"),
                "tuning_log": self.tuning_log.list(name, 10),
                "fault_history": self.fault_history.list(name, 20),
                "log_meta": self.log_meta.get(name),
                "log_lines": log_lines,
            },
        )
        # v1.15.5: faults that are a normal part of a restart / pool switch are informational, not alerts
        m_set = s["alerts"].get("maintenance") or nk_grace.DEFAULTS
        for f in faults:
            if f.get("severity") in {"problem", "warn"}:
                why = self.grace.suppress_fault(name, str(f.get("key") or ""), up, m_set, now)
                if why:
                    f["suppressed_severity"] = f["severity"]
                    f["severity"] = "info"
                    f["suppressed"] = why
                    self.grace.note_suppressed(name, "fault:" + str(f.get("key")), why, f"{f.get('title')}: {f.get('text')}", now=now)
        with self._fault_lock:
            self.faults_now[name] = faults
        # v1.12: Record every poll into rolling pre-fault buffer (default 12h)
        snap_now = self._snap(name)
        poll_sample = {
            "ts": float(now),
            "iso": nk_faults._fmt_ts_local(now),
            "online": bool(getattr(reading, "ok", False) or getattr(reading, "reachable", False)),
            "hashrate_ghs": hr_now,
            "power_w": pwr_now,
            "asic_temp_c": getattr(reading, "asic_temp", None),
            "asic_temps": list(getattr(reading, "asic_temps", []) or []),
            "vr_temp_c": getattr(reading, "vr_temp", None),
            "frequency_mhz": getattr(reading, "frequency", None),
            "actual_frequency_mhz": getattr(reading, "actual_frequency", None),
            "core_voltage_mv": getattr(reading, "core_voltage", None),
            "core_voltage_actual_mv": getattr(reading, "core_voltage_actual", None),
            "input_voltage": getattr(reading, "input_voltage", None),
            "input_current": getattr(reading, "input_current", None),
            "error_pct": snap_now.get("error_pct"),
            "shares_accepted": getattr(reading, "shares_accepted", None),
            "shares_rejected": getattr(reading, "shares_rejected", None),
            "uptime_sec": up,
            "fans": copy.deepcopy(fans_list),
            "pool_url": getattr(reading, "pool_url", None),
            "pool_user": getattr(reading, "pool_user", None),
            "tuner_state": {
                "last_action": snap_now.get("last_action"),
                "last_reason": snap_now.get("last_reason"),
                "emergency_lockout_remaining_sec": snap_now.get("emergency_lockout_remaining_sec"),
            },
            "fault_raw": copy.deepcopy(raw),
            "raw_fields": copy.deepcopy(getattr(reading, "raw_fields", None) or {}),
        }
        rec_hours = float((s.get("faults") or {}).get("record_hours") or 12.0)
        self.fault_records.add_sample(name, poll_sample, rec_hours, now)

        tracked = [f for f in faults if f["severity"] in {"problem", "warn"}]
        appeared, cleared = self.fault_history.observe(name, tracked, now)
        if appeared:
            self.fault_records.capture_on_fault(name, appeared, rec_hours, now)

        # v1.12: Buffer healthy readings and save pre-fault snapshot when a fault or 0 GH/s first appears
        has_prob = any(f.get("severity") in {"problem", "warn"} for f in faults)
        is_zero_hr = bool(getattr(reading, "ok", False) and (hr_now is not None and hr_now <= 0))
        fault_or_zero = has_prob or is_zero_hr
        was_fault_or_zero = self._in_fault_or_zero.get(name, False)
        if not fault_or_zero and getattr(reading, "ok", False) and (hr_now is None or hr_now > 0):
            self._last_healthy[name] = {
                "ts": now,
                "iso": nk_faults._fmt_ts_local(now),
                "hashrate_ghs": hr_now,
                "power_w": pwr_now,
                "asic_temp_c": getattr(reading, "asic_temp", None),
                "vr_temp_c": getattr(reading, "vr_temp", None),
                "asic_temps": list(getattr(reading, "asic_temps", []) or []),
                "frequency_mhz": getattr(reading, "frequency", None),
                "core_voltage_mv": getattr(reading, "core_voltage", None),
                "core_voltage_actual_mv": getattr(reading, "core_voltage_actual", None),
                "input_voltage": getattr(reading, "input_voltage", None),
                "input_current": getattr(reading, "input_current", None),
                "fans": list(fans_list),
            }
            self._in_fault_or_zero[name] = False
        elif fault_or_zero and not was_fault_or_zero:
            self._in_fault_or_zero[name] = True
            prev_healthy = self._last_healthy.get(name)
            if prev_healthy:
                trig = (appeared[0]["title"] if appeared else (tracked[0]["title"] if tracked else "0 GH/s hashrate"))
                self.fault_snapshots.record(name, trig, prev_healthy, now)
        a = s["alerts"]
        cooldown = float(a["cooldown_min"]) * 60.0
        for f in appeared:
            self.eventlog.write("fault_appeared", device=name, level="warn" if f.get("severity") == "problem" else "info",
                                key=f.get("key"), severity=f.get("severity"), title=f.get("title"), text=f.get("text"),
                                mapped=f.get("mapped"), cause=f.get("cause_summary"), first_seen=f.get("first_seen_iso"), log_line=f.get("log_line"), checks=(f.get("checks") or [])[:3])
            LOG.warning("%s: fault appeared: %s (%s)", name, f["title"], f["text"], extra={"nk_eventlog_skip": True})
            if f["severity"] != "problem" or not a["hardware_fault"]["enabled"]:
                continue
            with self.alerts._lock:
                fire = self.alerts._fire(name, "hw:" + f["key"], now, cooldown)
            if fire:
                first = (f.get("checks") or [""])[0]
                out.append(self.emit(_event("hardware_fault", "problem", name, f"{name}: {f['title']}",
                                            f"{f['text']}. {first}".strip(), fault_key=f["key"], mapped=f["mapped"])))
        for f in cleared:
            self.eventlog.write("fault_cleared", device=name, level="info", key=f.get("key"), severity=f.get("severity"), title=f.get("title"),
                                note="no longer reported by the miner")
            LOG.info("%s: fault cleared: %s", name, f.get("title"), extra={"nk_eventlog_skip": True})
            with self.alerts._lock:
                was = self.alerts._clear(name, "hw:" + str(f.get("key")))
            if was and f.get("severity") == "problem" and a["hardware_fault"]["enabled"]:
                out.append(self.emit(_event("hardware_fault", "info", name, f"{name}: fault cleared",
                                            f"{f.get('title')} is no longer reported.", sound=False, fault_key=f.get("key"))))
        return out

    def probe_faults(self, now: float) -> None:
        """Background: NerdQAxe+ v2 flags, Braiins error list/status, and Bitmain warnings."""
        if self.tuner is None or self.shared is None:
            return
        for d in list(self.tuner.config.devices):
            snap = self._snap(d.name)
            if not snap.get("online"):
                continue
            fam = self.device_family(snap)
            if fam not in {"nerd", "braiins", "bitmain", "other"}:
                continue
            prev = self.fault_probe.get(d.name) or {}
            pdata = prev.get("data") or {}
            if fam == "nerd" and pdata.get("_v2") is False and now - float(prev.get("ts", 0)) < 1800:
                continue  # v1.0.37.x without a v2 API: retry rarely
            if fam in {"bitmain", "other"} and not getattr(d, "web_password", "") and pdata.get("_port6060") is False and now - float(prev.get("ts", 0)) < 1800:
                continue
            try:
                data = self.tuner.probe_extra_faults(d.name, fam)
                self.fault_probe[d.name] = {"ts": now, "data": data, "error": ""}
            except Exception as exc:  # noqa: BLE001
                self.fault_probe[d.name] = {"ts": now, "data": {}, "error": str(exc)[:200]}
                LOG.debug("%s: fault probe failed: %s", d.name, exc)

    def _plan_for(self, name: str, s: dict[str, Any], now: float, d: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        d = d if d is not None else self._snap(name)
        faults = self.faults_now.get(name) or []
        temps = [t for t in (d.get("asic_temps") or []) if isinstance(t, (int, float))]
        asic = max(temps) if temps else d.get("hottest_asic_temp_c") or d.get("asic_temp_c")
        if d.get("type") == "antminer":
            chips = [t for t in (d.get("chip_temps") or []) if isinstance(t, (int, float))]
            asic = max(chips) if chips else asic
            asic_max = d.get("bos_chip_hot_c") or d.get("asic_max_c")
        else:
            asic_max = d.get("asic_max_c")
        dev = {"type": d.get("type"), "family": self.device_family(d), "online": bool(d.get("online")),
               "asic_c": asic, "vr_c": d.get("vr_temp_c"), "asic_max_c": asic_max, "vr_max_c": d.get("vr_max_c"),
               "has_web_password": bool(d.get("web_has_password")), "has_bos_password": bool(d.get("bos_has_password"))}
        recent = self.fault_history.actions_since(name, now - 86400)
        return nk_faults.reset_plan(faults, dev, s["faults"], recent, now)

    def fault_payload(self, d: dict[str, Any], s: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not s["faults"]["enabled"]:
            return None
        name = str(d.get("name") or "")
        now = self.clock()
        faults = list(self.faults_now.get(name) or [])
        probe = self.fault_probe.get(name) or {}
        ctx = self.fault_context.get(name)
        return {"active": faults, "count": sum(1 for f in faults if f["severity"] in {"problem", "warn"}),
                "worst": faults[0]["severity"] if faults else "",
                "reset": self._plan_for(name, s, now, d), "family": self.device_family(d),
                "probe_error": probe.get("error", ""), "log": self.log_meta.get(name),
                "recent": self.fault_history.list(name, 8),
                "last_vr_overheat": ctx.get("last_vr_overheat"),
                "pre_fault_snapshot": self.fault_snapshots.latest(name)}

    def clear_fault(self, name: str, action: str, confirm: bool) -> tuple[int, dict[str, Any]]:
        s = self.settings.get()
        now = self.clock()
        if self.tuner is None:
            return 503, {"ok": False, "error": "tuner not attached"}
        if not confirm:
            return 400, {"ok": False, "error": "confirmation required"}
        plan = self._plan_for(name, s, now)
        if not plan.get("available"):
            return 409, {"ok": False, "error": plan.get("reason") or "nothing to reset", "plan": plan}
        if action and action != plan["action"]:
            return 409, {"ok": False, "error": "the fault changed; refresh and try again", "plan": plan}
        if not plan["enabled"]:
            return 409, {"ok": False, "error": plan["reason"], "plan": plan}
        dry = bool(self.tuner._dry_run())
        keys = [f["key"] for f in self.faults_now.get(name) or []]
        if not dry and plan["action"] in {"restart", "braiins_restart", "bitmain_reboot"}:
            self.expect_transition(name, "nk_restart", f"{plan.get('label') or 'restart'} from the fault card")
        try:
            res = self.tuner.fault_action(name, plan["action"])
            ok = True
        except Exception as exc:  # noqa: BLE001
            res, ok = str(exc), False
        self.eventlog.write("user_clear_fault", device=name, level="info" if ok else "warn", action=plan["action"], label=plan.get("label"),
                            faults=keys, ok=ok, result=res, dry_run=dry,
                            active_faults=[{"key": f.get("key"), "title": f.get("title"), "text": f.get("text")} for f in (self.faults_now.get(name) or [])][:20])
        self.fault_history.add(name, "user_reset", now, action=plan["action"], text=res, ok=ok, dry_run=dry, faults=keys)
        self.log_tuning(name, "fault_reset", None, None, f"{plan['label']}: {res}", plan["action"], source="user", dry_run=dry)
        self.emit(_event("fault_reset", "info" if ok else "problem", name,
                         f"{name}: {'would ' + plan['label'].lower() if dry else plan['label']}" if ok else f"{name}: {plan['label']} failed",
                         res, sound=not ok))
        return (200 if ok else 502), {"ok": ok, "dry_run": dry, "action": plan["action"], "result": res}

    def log_snapshot(self, name: str) -> tuple[int, dict[str, Any]]:
        if self.tuner is None:
            return 503, {"ok": False, "error": "tuner not attached"}
        d = self._snap(name)
        if not d:
            return 404, {"ok": False, "error": f"unknown device {name}"}
        fam = self.device_family(d)
        try:
            src, text = self.tuner.fetch_device_log(name, fam)
        except Exception as exc:  # noqa: BLE001
            return 502, {"ok": False, "error": str(exc)[:300]}
        data = text.encode("utf-8", errors="replace")
        if len(data) > self.LOG_MAX_BYTES:
            data = data[-self.LOG_MAX_BYTES:]
            text = data.decode("utf-8", errors="replace")
        findings = nk_faults.parse_log_lines(text)
        now = self.clock()
        if self.log_dir is not None:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                (self.log_dir / (_safe_name(name)[:-5] + ".txt")).write_bytes(data)
            except OSError as exc:
                LOG.warning("Could not save log snapshot: %s", exc)
        meta = {"ts": now, "source": src, "bytes": len(data), "findings": findings[:20]}
        self.log_meta[name] = meta
        self.fault_history.add(name, "log_snapshot", now, text=f"{src}: {len(findings)} known issue(s)")
        return 200, {"ok": True, **meta}

    def diagnostics(self, device: str = "") -> dict[str, Any]:
        """Redacted diagnostic bundle: no pool users/wallets, SSIDs, MACs, tokens, passwords; IPs masked."""
        import autotune
        s = self.settings.get()
        names = [device] if device else (list(self.shared.snapshots.keys()) if self.shared is not None else [])
        devs = []
        for n in names:
            d = self._snap(n)
            if not d:
                continue
            d.pop("history", None)
            ctx = self.fault_context.get(n)
            safety = self.tuner.safety_store.get(n) if (self.tuner is not None and hasattr(self.tuner, "safety_store")) else {}
            coin_n = (self.coin_info.get(n) or {}).get("coin") or "BTC"
            pinfo = self.device_payout_info(n, "BTC" if coin_n == nk_coins.UNKNOWN else coin_n, s)
            devs.append({
                "device": d,
                "faults": self.faults_now.get(n) or [],
                "reset": self._plan_for(n, s, self.clock(), d),
                "fault_history": self.fault_history.list(n, 100),
                "log_snapshot": self.log_meta.get(n),
                "tuning_log": self.tuning_log.list(n, 100),
                "pre_fault_snapshot": self.fault_snapshots.latest(n),
                "pre_fault_snapshots": self.fault_snapshots.list(n),
                "last_vr_overheat": ctx.get("last_vr_overheat"),
                "safety": safety,
                "payout_check": {"source": pinfo["source"], "ready": pinfo["ready"], "note": pinfo["note"]},
                "fault_records": self.fault_records.list_records(n),
            })
        bundle = {"miner_hearth_diagnostics": 1, "app_version": autotune.APP_VERSION,
                  "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
                  "note": "Personal data removed: pool users/wallets, Wi-Fi names, MAC addresses, tokens and passwords; IP addresses masked.",
                  "settings": strip_secrets(s), "devices": devs}
        return nk_faults.redact(bundle)

    def restore_backup(self, data: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        ud = data.get("userdata") or {}
        app = data.get("app_settings") or {}
        cur = self.settings.get()
        if isinstance(app, dict):
            app = copy.deepcopy(app)
            for sec, key in SECRET_SETTING_PATHS:  # keep current secrets when the backup has none
                if not (isinstance(app.get(sec), dict) and app[sec].get(key)):
                    app.setdefault(sec, {})
                    if isinstance(app[sec], dict):
                        app[sec][key] = (cur.get(sec) or {}).get(key, "")
            self.settings.replace(merge_settings(default_app_settings(), app))
            self.apply_lan()
        summary: dict[str, Any] = {"devices": 0, "settings": True}
        if self.tuner is not None:
            ok, err, n = self.tuner.restore_devices(ud.get("devices") or [], ud.get("enabled") or {})
            if not ok:
                return False, err, {}
            summary["devices"] = n
            spec: dict[str, Any] = {}
            for k in ("display_fields", "global_tuner", "device_order"):
                if k in ud:
                    spec[k] = ud[k]
            if spec:
                self.tuner.update_settings(spec)
        LOG.info("Restored settings backup (%d device(s))", summary["devices"])
        return True, "", summary


def attach_feature_hub(tuner: Any, shared: Any, start: bool = True, fetch: Optional[nk_net.Fetcher] = None) -> FeatureHub:
    ud_dir = None
    try:
        if tuner is not None and tuner.config.userdata_file is not None:
            ud_dir = Path(tuner.config.userdata_file).parent
    except Exception:
        ud_dir = None
    hub = FeatureHub(ud_dir, fetch=fetch)
    hub.attach(tuner, shared)
    if start:
        hub.start()
    return hub
