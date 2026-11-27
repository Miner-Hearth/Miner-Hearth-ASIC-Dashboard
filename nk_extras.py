#!/usr/bin/env python3
"""Miner Hearth v1.9/v1.10/v1.11 building blocks (stdlib only).

  * SessionBestTracker  - session best share since the miner's last restart + all-time best
  * PoolHealth          - rejected/stale share rate per machine (rolling window) -> alert
  * PoolStats           - multi-pool comparison (accepted/rejected, latency, uptime) over time
  * probe_pool          - TCP connect latency to a pool, measured from this PC
  * ChipHealth          - best stable frequency/voltage over time + wear warning
  * quiet mode helpers  - scheduled fan cap; emergency always wins
  * RestartManager      - weekly / hung-miner restarts with rate limits (off by default)
  * Webhooks            - Discord / Telegram / ntfy alert delivery (off by default)
  * layout helpers      - card field / summary bar / section order
  * temperature helpers - Celsius <-> Fahrenheit for display (logic stays in Celsius)
"""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

LOG = logging.getLogger("autotune.minerhearth")


def _load(path: Optional[Path]) -> Any:
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(path: Optional[Path], data: Any) -> None:
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        LOG.warning("Could not save %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Temperature units (display only)
# ---------------------------------------------------------------------------

def c_to_f(c: Optional[float]) -> Optional[float]:
    return None if c is None else float(c) * 9.0 / 5.0 + 32.0


def f_to_c(f: Optional[float]) -> Optional[float]:
    return None if f is None else (float(f) - 32.0) * 5.0 / 9.0


def format_temp(c: Optional[float], unit: str = "C", digits: int = 1) -> str:
    if c is None:
        return "—"
    if str(unit).upper() == "F":
        return f"{c_to_f(c):.{digits}f} °F"
    return f"{float(c):.{digits}f} °C"


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

CARD_FIELD_KEYS = [
    "faults", "hashrate", "asic_temp", "vr_temp", "fans", "ambient_temp", "frequency", "core_voltage",
    "power_efficiency", "error_rate", "last_share", "shares_best_diff", "session_best", "pool_health",
    "profit", "block_odds", "chip_health", "wifi_system", "asic_domains", "s19_boards",
    "firmware_badge", "sparklines", "voltage_reason", "manual_control", "s19_tuner", "pool_info", "action_log",
]
SUMMARY_FIELD_KEYS = ["online", "hashrate", "power", "efficiency", "hottest_chip", "hottest_vr", "best_share"]
SECTION_KEYS = ["summary", "econ", "coins", "charts", "compare", "pools", "cards", "tuning_log", "fault_history", "logs"]
SECTION_LABELS = {
    "summary": "Fleet summary bar", "econ": "Power cost & odds bar", "coins": "Block odds per coin",
    "charts": "History charts", "compare": "Fleet comparison table", "pools": "Pool comparison",
    "cards": "Machine cards", "tuning_log": "Tuning log", "fault_history": "Fault history", "logs": "Recent log",
}


def normalize_order(raw: Any, known: list[str]) -> list[str]:
    out: list[str] = []
    for k in raw if isinstance(raw, list) else []:
        k = str(k)
        if k in known and k not in out:
            out.append(k)
    for k in known:  # new keys (from updates) go to their default slot
        if k not in out:
            idx = known.index(k)
            prev = [p for p in known[:idx] if p in out]
            pos = out.index(prev[-1]) + 1 if prev else 0
            out.insert(pos, k)
    return out


def default_layout() -> dict[str, list[str]]:
    return {"card_fields": list(CARD_FIELD_KEYS), "summary_fields": list(SUMMARY_FIELD_KEYS), "sections": list(SECTION_KEYS)}


def normalize_layout(raw: Any) -> dict[str, list[str]]:
    r = raw if isinstance(raw, dict) else {}
    return {
        "card_fields": normalize_order(r.get("card_fields"), CARD_FIELD_KEYS),
        "summary_fields": normalize_order(r.get("summary_fields"), SUMMARY_FIELD_KEYS),
        "sections": normalize_order(r.get("sections"), SECTION_KEYS),
    }


# ---------------------------------------------------------------------------
# Session best share (Antminer / Braiins)
# ---------------------------------------------------------------------------

class SessionBestTracker:
    """Session best share since the miner's last restart, plus an all-time best.

    CGMiner-style APIs (stock Antminer, Braiins OS+) report "Best Share" since the
    mining process started, i.e. a session value. When it is present it is used as is
    (source "api"). Otherwise the session best is derived from pool best-share /
    last-share difficulties since the last restart, detected by uptime going down.
    The all-time best is the highest value Miner Hearth has seen (persisted).
    """

    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        raw = _load(self.path)
        self.data: dict[str, dict[str, Any]] = raw.get("devices", {}) if isinstance(raw, dict) and isinstance(raw.get("devices"), dict) else {}
        self._dirty_ts = 0.0

    def save(self) -> None:
        with self._lock:
            _save(self.path, {"schema_version": 1, "devices": self.data})

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            if old in self.data:
                self.data[new] = self.data.pop(old)
                self.save()

    def remove(self, name: str) -> None:
        with self._lock:
            if name in self.data:
                self.data.pop(name, None)
                self.save()

    def get(self, name: str) -> Optional[dict[str, Any]]:
        with self._lock:
            d = self.data.get(name)
            return dict(d) if d else None

    def update(
        self,
        name: str,
        api_session_best: Optional[float],
        uptime_sec: Optional[float],
        share_diffs: list[Optional[float]],
        now: float,
        active_pool_key: str = "",
        active_pool_best: Optional[float] = None,
        post_switch_diffs: Optional[list[Optional[float]]] = None,
        all_time_hint: Optional[float] = None,
        prefer_active_pool_best: bool = False,
        has_active_pool_best: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            st = self.data.setdefault(name, {
                "session_best": None,
                "session_start_ts": None,
                "last_uptime": None,
                "all_time_best": None,
                "all_time_ts": None,
                "tracking_since": now,
                "source": "",
                "pool_source": "",
                "active_pool_key": "",
                "pool_switched": False,
                "api_best_baseline": None,
                "pool_best_baseline": None,
                "seen_pools": {},
            })
            if not isinstance(st.get("seen_pools"), dict):
                st["seen_pools"] = {}
            seen_pools: dict[str, float] = st["seen_pools"]
            changed = False
            restarted = False
            pool_switched_now = False

            # Track all-time hint (e.g. AxeOS NVS bestDiff or CGMiner summary Best Share) before any resets
            if all_time_hint is not None and all_time_hint > 0:
                if st.get("all_time_best") is None or float(all_time_hint) > float(st["all_time_best"]):
                    st["all_time_best"] = float(all_time_hint)
                    st["all_time_ts"] = now
                    changed = True

            if uptime_sec is not None:
                last = st.get("last_uptime")
                if last is not None and float(uptime_sec) + 60 < float(last):
                    restarted = True
                if restarted:
                    st["pool_switched"] = False
                    st["api_best_baseline"] = None
                    st["pool_best_baseline"] = None
                    st["pool_source"] = ""
                    seen_pools.clear()
                    st["session_best"] = None
                    st["session_start_ts"] = now - float(uptime_sec)
                    changed = True
                elif st.get("session_start_ts") is None:
                    st["session_start_ts"] = now - float(uptime_sec)
                    changed = True
                st["last_uptime"] = float(uptime_sec)

            pk = str(active_pool_key or "").strip()
            prev_pk = str(st.get("active_pool_key") or "").strip()
            if pk:
                if not prev_pk:
                    st["active_pool_key"] = pk
                    changed = True
                elif pk != prev_pk:
                    pool_switched_now = True
                    # Ensure previous pool is recorded in seen_pools for failback detection in this boot
                    if prev_pk not in seen_pools:
                        seen_pools[prev_pk] = float(st.get("session_best") or 0.0)
                    returning_to_seen = pk in seen_pools
                    st["active_pool_key"] = pk
                    st["pool_switched"] = True
                    st["session_start_ts"] = now
                    st["api_best_baseline"] = float(api_session_best) if (api_session_best is not None and api_session_best > 0) else None
                    st["source"] = "pool_switch"
                    if prefer_active_pool_best:
                        if returning_to_seen:
                            # Failback to a pool already seen this boot: Braiins POOLS[i]['Best Share']
                            # still retains pre-failover shares, so guard with the saved baseline.
                            saved_base = float(seen_pools.get(pk) or 0.0)
                            cur_base = float(active_pool_best) if (active_pool_best is not None and active_pool_best > 0) else 0.0
                            baseline = max(saved_base, cur_base)
                            st["pool_best_baseline"] = baseline if baseline > 0 else None
                            st["session_best"] = None
                            st["pool_source"] = "failback_reset"
                        else:
                            # Brand-new pool key: use baseline 0 so existing POOLS[i]['Best Share'] shows immediately.
                            st["pool_best_baseline"] = 0.0
                            if active_pool_best is not None and float(active_pool_best) > 0:
                                st["session_best"] = float(active_pool_best)
                                st["pool_best_baseline"] = float(active_pool_best)
                            else:
                                st["session_best"] = None
                            st["pool_source"] = "active_pool"
                    else:
                        st["pool_best_baseline"] = float(active_pool_best) if (active_pool_best is not None and active_pool_best > 0) else None
                        st["session_best"] = None
                    changed = True

            if st.get("pool_switched"):
                st["source"] = "pool_switch"
                if not pool_switched_now:
                    cands: list[float] = []
                    if prefer_active_pool_best and has_active_pool_best:
                        st["pool_source"] = "active_pool"
                        base_pool = float(st.get("pool_best_baseline") or 0.0)
                        if active_pool_best is not None and float(active_pool_best) > base_pool:
                            cands.append(float(active_pool_best))
                            st["pool_best_baseline"] = float(active_pool_best)
                            changed = True
                        if api_session_best is not None and api_session_best > 0:
                            base_api = st.get("api_best_baseline")
                            if base_api is not None and float(api_session_best) > float(base_api):
                                cands.append(float(api_session_best))
                                st["api_best_baseline"] = float(api_session_best)
                                changed = True
                    else:
                        if api_session_best is not None and api_session_best > 0:
                            base_api = st.get("api_best_baseline")
                            if base_api is None or float(api_session_best) > float(base_api):
                                cands.append(float(api_session_best))
                                st["api_best_baseline"] = float(api_session_best)
                                changed = True
                        if active_pool_best is not None and active_pool_best > 0:
                            base_pool = st.get("pool_best_baseline")
                            if base_pool is None or float(active_pool_best) > float(base_pool):
                                cands.append(float(active_pool_best))
                                st["pool_best_baseline"] = float(active_pool_best)
                                changed = True
                    for x in (post_switch_diffs or []):
                        if isinstance(x, (int, float)) and x > 0:
                            cands.append(float(x))
                    if cands:
                        m = max(cands)
                        if st.get("session_best") is None or m > float(st["session_best"]):
                            st["session_best"] = m
                            changed = True
            elif prefer_active_pool_best and has_active_pool_best:
                # Primary Braiins/CGMiner session best: active pool's POOLS[i]['Best Share'].
                new_src = "api" if (api_session_best is not None and api_session_best > 0) else "derived"
                new_val = float(active_pool_best) if (active_pool_best is not None and float(active_pool_best) > 0) else None
                if st.get("session_best") != new_val or st.get("source") != new_src or st.get("pool_source") != "active_pool":
                    st["session_best"] = new_val
                    st["source"] = new_src
                    st["pool_source"] = "active_pool"
                    changed = True
            elif api_session_best is not None and api_session_best > 0:
                if st.get("session_best") != float(api_session_best) or st.get("source") != "api":
                    changed = True
                st["session_best"] = float(api_session_best)
                st["source"] = "api"
            else:
                if restarted:
                    st["session_best"] = None
                    changed = True
                cands = [float(x) for x in share_diffs if isinstance(x, (int, float)) and x > 0]
                if cands:
                    m = max(cands)
                    if st.get("session_best") is None or m > float(st["session_best"]):
                        st["session_best"] = m
                        changed = True
                st["source"] = "derived"

            if pk:
                prev_seen = seen_pools.get(pk)
                cur_seen_val = max(
                    float(prev_seen or 0.0),
                    float(active_pool_best) if (active_pool_best is not None and float(active_pool_best) > 0) else 0.0,
                    float(st["session_best"]) if (st.get("session_best") is not None and float(st["session_best"]) > 0) else 0.0,
                )
                if prev_seen is None or cur_seen_val > float(prev_seen):
                    seen_pools[pk] = cur_seen_val
                    changed = True

            sb = st.get("session_best")
            if sb is not None and (st.get("all_time_best") is None or sb > float(st["all_time_best"])):
                st["all_time_best"] = sb
                st["all_time_ts"] = now
                changed = True
            if changed or restarted or pool_switched_now:
                self.save()
            out = dict(st)
            out["restarted"] = restarted
            out["pool_switched_now"] = pool_switched_now
            return out


# ---------------------------------------------------------------------------
# Pool health per machine
# ---------------------------------------------------------------------------

class PoolHealth:
    """Rolling rejected+stale share rate from cumulative counters (handles miner reboots)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.dev: dict[str, dict[str, Any]] = {}

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            if old in self.dev:
                self.dev[new] = self.dev.pop(old)

    def remove(self, name: str) -> None:
        with self._lock:
            self.dev.pop(name, None)

    def update(self, name: str, acc: Optional[int], rej: Optional[int], stale: Optional[int], now: float,
               window_sec: float = 1800.0) -> dict[str, Any]:
        """Returns the deltas since the last poll and the window totals."""
        with self._lock:
            st = self.dev.setdefault(name, {"last": None, "win": deque()})
            d_acc = d_rej = d_stale = 0
            if acc is not None:
                cur = (int(acc), int(rej or 0), int(stale or 0))
                last = st["last"]
                if last is not None and all(c >= l for c, l in zip(cur, last)):
                    d_acc, d_rej, d_stale = (cur[0] - last[0], cur[1] - last[1], cur[2] - last[2])
                    if d_acc or d_rej or d_stale:
                        st["win"].append((now, d_acc, d_rej, d_stale))
                st["last"] = cur
            while st["win"] and st["win"][0][0] < now - window_sec:
                st["win"].popleft()
            a = sum(x[1] for x in st["win"])
            r = sum(x[2] for x in st["win"])
            s = sum(x[3] for x in st["win"])
            tot = a + r + s
            return {"d_acc": d_acc, "d_rej": d_rej, "d_stale": d_stale, "win_acc": a, "win_rej": r, "win_stale": s,
                    "win_total": tot, "reject_pct": ((r + s) / tot * 100.0) if tot else None}


# ---------------------------------------------------------------------------
# Multi-pool comparison
# ---------------------------------------------------------------------------

def probe_pool(host_port: str, timeout: float = 3.0) -> tuple[bool, Optional[float]]:
    """TCP connect time to a stratum pool, from this PC (not from the miner)."""
    host, _, port = host_port.partition(":")
    try:
        p = int(port) if port else 3333
    except ValueError:
        p = 3333
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, p), timeout=timeout):
            return True, (time.perf_counter() - t0) * 1000.0
    except OSError:
        return False, None


class PoolStats:
    """Hourly buckets per pool ('host:port') kept for 7 days."""

    KEEP_SEC = 7 * 86400

    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        raw = _load(self.path)
        self.pools: dict[str, dict[str, Any]] = raw.get("pools", {}) if isinstance(raw, dict) and isinstance(raw.get("pools"), dict) else {}
        self._last_save = 0.0

    def _bucket(self, key: str, now: float) -> dict[str, Any]:
        p = self.pools.setdefault(key, {"buckets": {}, "devices": {}})
        h = str(int(now // 3600 * 3600))
        return p["buckets"].setdefault(h, {"acc": 0, "rej": 0, "stale": 0, "lat_sum": 0.0, "lat_n": 0, "up": 0, "n": 0, "mlat_sum": 0.0, "mlat_n": 0})

    def record_shares(self, key: str, device: str, d_acc: int, d_rej: int, d_stale: int, miner_ping_ms: Optional[float], now: float) -> None:
        if not key:
            return
        with self._lock:
            b = self._bucket(key, now)
            b["acc"] += int(d_acc)
            b["rej"] += int(d_rej)
            b["stale"] += int(d_stale)
            if miner_ping_ms is not None and miner_ping_ms > 0:
                b["mlat_sum"] += float(miner_ping_ms)
                b["mlat_n"] += 1
            self.pools[key]["devices"][device] = now
            self._maybe_save(now)

    def record_probe(self, key: str, ok: bool, latency_ms: Optional[float], now: float) -> None:
        with self._lock:
            b = self._bucket(key, now)
            b["n"] += 1
            if ok:
                b["up"] += 1
                if latency_ms is not None:
                    b["lat_sum"] += float(latency_ms)
                    b["lat_n"] += 1
            self._maybe_save(now)

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            for p in self.pools.values():
                if old in p.get("devices", {}):
                    p["devices"][new] = p["devices"].pop(old)

    def remove(self, name: str) -> None:
        changed = False
        with self._lock:
            for p in self.pools.values():
                devs = p.get("devices")
                if isinstance(devs, dict) and name in devs:
                    devs.pop(name, None)
                    changed = True
        if changed:
            self.save()

    def _maybe_save(self, now: float) -> None:
        if now - self._last_save >= 300:
            self.save(now)

    def save(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            cutoff = now - self.KEEP_SEC
            for k in list(self.pools):
                bk = self.pools[k]["buckets"]
                for h in [h for h in bk if float(h) < cutoff]:
                    del bk[h]
                if not bk:
                    del self.pools[k]
            self._last_save = now
            _save(self.path, {"schema_version": 1, "pools": self.pools})

    def active_keys(self, now: float, within_sec: float = 3600.0) -> list[str]:
        with self._lock:
            return [k for k, p in self.pools.items() if any(now - float(t) <= within_sec for t in p.get("devices", {}).values())]

    def summary(self, now: float) -> list[dict[str, Any]]:
        out = []
        with self._lock:
            for key, p in self.pools.items():
                row: dict[str, Any] = {"pool": key, "devices": sorted(n for n, t in p.get("devices", {}).items() if now - float(t) <= 86400)}
                for label, span in (("24h", 86400), ("7d", 7 * 86400)):
                    agg = {"acc": 0, "rej": 0, "stale": 0, "lat_sum": 0.0, "lat_n": 0, "up": 0, "n": 0, "mlat_sum": 0.0, "mlat_n": 0}
                    for h, b in p["buckets"].items():
                        if float(h) >= now - span:
                            for k2 in agg:
                                agg[k2] += b.get(k2, 0)
                    tot = agg["acc"] + agg["rej"] + agg["stale"]
                    row[label] = {
                        "accepted": agg["acc"], "rejected": agg["rej"], "stale": agg["stale"],
                        "reject_pct": ((agg["rej"] + agg["stale"]) / tot * 100.0) if tot else None,
                        "latency_ms": (agg["lat_sum"] / agg["lat_n"]) if agg["lat_n"] else None,
                        "miner_ping_ms": (agg["mlat_sum"] / agg["mlat_n"]) if agg["mlat_n"] else None,
                        "uptime_pct": (agg["up"] / agg["n"] * 100.0) if agg["n"] else None,
                        "probes": agg["n"],
                    }
                out.append(row)
        out.sort(key=lambda r: -(r["24h"]["accepted"] or 0))
        return out


# ---------------------------------------------------------------------------
# Chip health / wear
# ---------------------------------------------------------------------------

class ChipHealth:
    """Logs the lowest voltage that ran stable at each frequency, per day.

    A setting counts as stable after `stable_min` minutes at the same frequency and
    voltage with the rolling error rate inside the acceptable band and no emergency.
    Wear warning: at the same frequency, the recent stable voltage is `wear_mv` or more
    above the early baseline, with at least `min_days` between them.
    """

    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        raw = _load(self.path)
        self.devices: dict[str, dict[str, Any]] = raw.get("devices", {}) if isinstance(raw, dict) and isinstance(raw.get("devices"), dict) else {}
        self.cur: dict[str, dict[str, Any]] = {}

    def save(self) -> None:
        with self._lock:
            _save(self.path, {"schema_version": 1, "devices": self.devices})

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            if old in self.devices:
                self.devices[new] = self.devices.pop(old)
                self.save()
            if old in self.cur:
                self.cur[new] = self.cur.pop(old)

    def remove(self, name: str) -> None:
        with self._lock:
            changed = name in self.devices
            self.devices.pop(name, None)
            self.cur.pop(name, None)
            if changed:
                self.save()

    def observe(self, name: str, freq: Optional[float], mv: Optional[float], error_pct: Optional[float],
                error_high: float, hot: bool, now: float, stable_min: float = 30.0) -> bool:
        """Returns True when a stable point was recorded."""
        if freq is None or mv is None:
            self.cur.pop(name, None)
            return False
        f, v = int(round(float(freq))), int(round(float(mv)))
        with self._lock:
            c = self.cur.get(name)
            if c is None or c["f"] != f or abs(c["v"] - v) > 5 or hot:
                # New setting, or an emergency: the stability clock starts again.
                self.cur[name] = {"f": f, "v": v, "since": now}
                return False
            if error_pct is not None and float(error_pct) > float(error_high):
                c["since"] = now  # errors above the acceptable band restart the clock
                return False
            if now - c["since"] < stable_min * 60.0:
                return False
            day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
            dev = self.devices.setdefault(name, {"points": {}, "best": []})
            pts = dev["points"].setdefault(str(f), [])
            if pts and pts[-1][0] == day:
                if v < pts[-1][1]:
                    pts[-1][1] = v
                else:
                    return False
            else:
                pts.append([day, v])
                del pts[:-400]
            best = dev["best"]
            if not best or best[-1][0] != day:
                best.append([day, f, v])
                del best[:-400]
            elif f > best[-1][1] or (f == best[-1][1] and v < best[-1][2]):
                best[-1] = [day, f, v]
            self.save()
            return True

    def wear(self, name: str, wear_mv: float = 20.0, min_days: int = 14) -> Optional[dict[str, Any]]:
        with self._lock:
            dev = self.devices.get(name) or {}
            worst = None
            for f, pts in (dev.get("points") or {}).items():
                if len(pts) < 4:
                    continue
                base_pts, recent_pts = pts[:min(7, len(pts) // 2)], pts[-3:]
                d0 = datetime.strptime(base_pts[-1][0], "%Y-%m-%d")
                d1 = datetime.strptime(recent_pts[0][0], "%Y-%m-%d")
                if (d1 - d0).days < min_days:
                    continue
                base = sorted(p[1] for p in base_pts)[len(base_pts) // 2]
                recent = sorted(p[1] for p in recent_pts)[len(recent_pts) // 2]
                delta = recent - base
                if delta >= wear_mv and (worst is None or delta > worst["delta_mv"]):
                    worst = {"freq": int(f), "baseline_mv": base, "recent_mv": recent, "delta_mv": delta,
                             "since": base_pts[0][0], "latest": recent_pts[-1][0]}
            return worst

    def summary(self, name: str) -> dict[str, Any]:
        with self._lock:
            dev = self.devices.get(name) or {}
            best = list(dev.get("best") or [])
            c = self.cur.get(name)
        return {"best": best[-30:], "latest": best[-1] if best else None,
                "current": {"freq": c["f"], "mv": c["v"], "since": c["since"]} if c else None}


# ---------------------------------------------------------------------------
# Quiet mode (fan cap) — emergency always wins
# ---------------------------------------------------------------------------

def decide_quiet(applied_pct: Optional[int], wanted: Optional[dict[str, Any]], hot: bool, now: float,
                 suspended_until: float) -> tuple[str, Optional[int], str]:
    """Return (action, fan_pct, reason). action: 'cap' | 'release' | 'none'.

    hot = emergency temperatures / emergency decision. Releasing hands the fan back to
    the miner's automatic control, which is what protects the chips.
    """
    if hot:
        if applied_pct is not None:
            return "release", None, "emergency temperature: quiet mode suspended, fan back on automatic"
        return "none", None, ""
    if wanted and wanted.get("fan_max_pct") and now >= suspended_until:
        pct = int(wanted["fan_max_pct"])
        if applied_pct != pct:
            return "cap", pct, f"quiet mode '{', '.join(wanted.get('windows') or [])}': fan capped at {pct}%"
        return "none", pct, ""
    if applied_pct is not None and (not wanted or now < suspended_until):
        return "release", None, "quiet mode ended: fan back on automatic"
    return "none", None, ""


# ---------------------------------------------------------------------------
# Scheduled / hung restarts
# ---------------------------------------------------------------------------

class RestartManager:
    """Decides when to restart a miner. Off by default; every restart is rate-limited."""

    GRACE_SEC = 900  # ignore a machine for 15 min after a restart

    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        raw = _load(self.path)
        self.history: dict[str, list[float]] = raw.get("history", {}) if isinstance(raw, dict) and isinstance(raw.get("history"), dict) else {}
        self.weekly_done: dict[str, str] = raw.get("weekly_done", {}) if isinstance(raw, dict) and isinstance(raw.get("weekly_done"), dict) else {}
        self.zero_since: dict[str, float] = {}

    def save(self) -> None:
        with self._lock:
            _save(self.path, {"schema_version": 1, "history": self.history, "weekly_done": self.weekly_done})

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            for d in (self.history, self.weekly_done, self.zero_since):
                if old in d:
                    d[new] = d.pop(old)
            self.save()

    def remove(self, name: str) -> None:
        with self._lock:
            changed = (name in self.history) or (name in self.weekly_done)
            for d in (self.history, self.weekly_done, self.zero_since):
                d.pop(name, None)
            if changed:
                self.save()

    def allowed(self, name: str, cfg: dict[str, Any], now: float) -> tuple[bool, str]:
        hist = [t for t in self.history.get(name, []) if now - t < 86400]
        if len(hist) >= int(cfg.get("max_per_day", 2)):
            return False, f"limit of {int(cfg.get('max_per_day', 2))} restarts per 24 h reached"
        if hist and now - max(hist) < float(cfg.get("min_gap_min", 60)) * 60.0:
            return False, f"last restart was less than {float(cfg.get('min_gap_min', 60)):.0f} min ago"
        return True, ""

    def evaluate(self, name: str, reading: Any, cfg: dict[str, Any], now: float) -> Optional[dict[str, Any]]:
        """Return {'kind': 'hung'|'weekly', 'reason': ...} when a restart is due, else None."""
        if not cfg.get("weekly", {}).get("enabled") and not cfg.get("hung", {}).get("enabled"):
            self.zero_since.pop(name, None)
            return None
        devs = [str(x).strip().lower() for x in (cfg.get("devices") or []) if str(x).strip()]
        if devs and not any(x in {"all", "all machines", "all devices", "*"} for x in devs):
            if str(name or "").strip().lower() not in devs:
                return None
        with self._lock:
            hist = self.history.get(name, [])
            if hist and now - max(hist) < self.GRACE_SEC:
                self.zero_since.pop(name, None)
                return None
            due: Optional[dict[str, Any]] = None
            ok = bool(getattr(reading, "ok", False))
            hr = getattr(reading, "hashrate", None)
            hung = cfg.get("hung") or {}
            if hung.get("enabled") and ok and hr is not None and float(hr) <= 0.001:
                up = getattr(reading, "uptime_sec", None)
                self.zero_since.setdefault(name, now)
                mins = float(hung.get("minutes", 15))
                if now - self.zero_since[name] >= mins * 60.0 and (up is None or float(up) >= mins * 60.0):
                    due = {"kind": "hung", "reason": f"hashrate 0 for {mins:.0f}+ min while the API answers"}
            else:
                self.zero_since.pop(name, None)
            wk = cfg.get("weekly") or {}
            if due is None and wk.get("enabled") and ok:
                dt = datetime.fromtimestamp(now)
                hh, mm = (str(wk.get("time") or "04:00") + ":00").split(":")[:2]
                start = dt.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                key = dt.strftime("%Y-%m-%d")
                if dt.weekday() == int(wk.get("day", 6)) and start <= dt < start + timedelta(minutes=30) and self.weekly_done.get(name) != key:
                    due = {"kind": "weekly", "reason": f"weekly restart ({dt.strftime('%A')} {start.strftime('%H:%M')})", "key": key}
            if due is None:
                return None
            okk, why = self.allowed(name, cfg, now)
            if not okk:
                due["blocked"] = why
            return due

    def record(self, name: str, due: dict[str, Any], now: float) -> None:
        with self._lock:
            self.history.setdefault(name, []).append(now)
            self.history[name] = [t for t in self.history[name] if now - t < 7 * 86400][-50:]
            if due.get("kind") == "weekly" and due.get("key"):
                self.weekly_done[name] = due["key"]
            self.zero_since.pop(name, None)
            self.save()

    def mark_weekly_done(self, name: str, key: str) -> None:
        with self._lock:
            self.weekly_done[name] = key
            self.save()


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

WEBHOOK_TYPES = ["block_confirmed", "block_possible", "offline", "hashrate_drop", "fallback_pool", "no_shares",
                 "pool_rejects", "chip_wear", "restart", "recovered", "hardware_fault", "node"]
WEBHOOK_DEFAULT_ON = {"block_confirmed", "block_possible", "offline", "hashrate_drop", "fallback_pool", "no_shares", "pool_rejects", "chip_wear", "restart", "node"}
WEBHOOK_SECRET_KEYS = ("discord_url", "telegram_token", "ntfy_url", "ntfy_token")

Poster = Callable[[str, bytes, dict[str, str], float], Any]

DISCORD_WEBHOOK_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
)


def trim_webhook_url(url: str) -> str:
    return re.sub(r"[\r\n\t]+", "", str(url or "")).strip()


def validate_discord_webhook_url(url: str) -> tuple[bool, str]:
    u = trim_webhook_url(url)
    if not u:
        return True, ""
    low = u.lower()
    if any(low.startswith(pref) for pref in DISCORD_WEBHOOK_PREFIXES):
        return True, ""
    return (
        False,
        "Discord webhook URL must start with https://discord.com/api/webhooks/ or "
        "https://discordapp.com/api/webhooks/ (remove extra spaces or check the pasted URL).",
    )


def _extract_http_error_detail(raw_bytes: bytes) -> str:
    txt = (raw_bytes or b"").decode("utf-8", errors="replace").strip()
    if not txt:
        return ""
    try:
        parsed = json.loads(txt)
        if isinstance(parsed, dict):
            msg = str(parsed.get("message") or parsed.get("error") or "").strip()
            if msg:
                return msg[:180]
    except Exception:
        pass
    return re.sub(r"\s+", " ", txt)[:180]


def default_poster_detail(url: str, body: bytes, headers: dict[str, str], timeout: float = 10.0) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=dict(headers, **{"User-Agent": "MinerHearth/1.15"}), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user-configured https URL
            return int(resp.status), ""
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = _extract_http_error_detail(exc.read(4096))
        except Exception:
            detail = str(getattr(exc, "reason", "") or "").strip()
        return int(exc.code), detail
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(str(exc)) from exc


def default_poster(url: str, body: bytes, headers: dict[str, str], timeout: float = 10.0) -> int:
    return default_poster_detail(url, body, headers, timeout=timeout)[0]


def _valid_https(url: str) -> bool:
    return bool(re.match(r"^https?://[^\s/]+", trim_webhook_url(url)))


def build_webhook_requests(cfg: dict[str, Any], ev: dict[str, Any]) -> list[tuple[str, str, bytes, dict[str, str]]]:
    """[(service, url, body, headers)] for every configured service."""
    title = str(ev.get("title") or "Miner Hearth")
    msg = str(ev.get("message") or "")
    level = ev.get("level")
    out = []
    if _valid_https(cfg.get("discord_url", "")):
        kind = ev.get("kind")
        emoji = ("\u2705 " if kind == "block_confirmed" else "\u26CF\uFE0F " if kind == "block_possible" else
                 "\U0001F389 " if level == "block" else "\u26A0\uFE0F " if level == "problem" else "")
        body = json.dumps({"username": "Miner Hearth", "content": f"{emoji}**{title}**\n{msg}"[:1900]}).encode()
        out.append(("discord", cfg["discord_url"], body, {"Content-Type": "application/json"}))
    tok, chat = str(cfg.get("telegram_token") or "").strip(), str(cfg.get("telegram_chat_id") or "").strip()
    if tok and chat and re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", tok):
        body = json.dumps({"chat_id": chat, "text": f"{title}\n{msg}"[:4000], "disable_web_page_preview": True}).encode()
        out.append(("telegram", f"https://api.telegram.org/bot{tok}/sendMessage", body, {"Content-Type": "application/json"}))
    if _valid_https(cfg.get("ntfy_url", "")):
        found = ev.get("kind") == "block_possible"
        hdr = {"Title": title.encode("ascii", "replace").decode()[:200],
               "Priority": "5" if level == "block" or found else ("4" if level == "problem" else "3"),
               "Tags": ("white_check_mark" if ev.get("kind") == "block_confirmed" else "pick" if found else "tada" if level == "block"
                        else "warning" if level == "problem" else "information_source")}
        if cfg.get("ntfy_token"):
            hdr["Authorization"] = f"Bearer {cfg['ntfy_token']}"
        out.append(("ntfy", cfg["ntfy_url"], msg.encode("utf-8"), hdr))
    return out


def _retry_after_sec(detail: str, default: float = 2.0) -> float:
    m = re.search(r"retry[_ ]after\D{0,4}([0-9]+(?:\.[0-9]+)?)", str(detail or ""), re.I)
    try:
        return float(m.group(1)) if m else default
    except ValueError:
        return default


class Webhooks:
    MAX_RETRY_WAIT_SEC = 10.0

    def __init__(self, poster: Optional[Poster] = None, async_send: bool = True, sleep: Optional[Callable[[float], Any]] = None):
        self.poster = poster or default_poster_detail
        self.async_send = async_send
        self.sleep = sleep or time.sleep
        self.last: dict[str, dict[str, Any]] = {}
        self.last_by_kind: dict[str, dict[str, Any]] = {}

    def wants(self, cfg: dict[str, Any], ev: dict[str, Any]) -> bool:
        if not cfg.get("enabled"):
            return False
        if ev.get("test"):
            return True
        return bool((cfg.get("types") or {}).get(ev.get("kind")))

    def _post_once(self, svc: str, url: str, body: bytes, hdr: dict[str, str]) -> dict[str, Any]:
        try:
            res = self.poster(url, body, hdr, 10.0)
            if isinstance(res, tuple):
                code, detail = int(res[0]), str(res[1] or "").strip()
            else:
                code, detail = int(res), ""
            ok = 200 <= code < 300
            err_msg = "" if ok else (f"{code} {detail}".strip() if detail else f"HTTP {code}")
            return {"service": svc, "ok": ok, "status": code, "body": detail, "error": err_msg}
        except Exception as exc:  # noqa: BLE001 - never let a webhook break monitoring
            return {"service": svc, "ok": False, "status": None, "body": "", "error": str(exc)[:200]}

    def send(self, cfg: dict[str, Any], ev: dict[str, Any], force: bool = False,
             on_result: Optional[Callable[[dict[str, Any], list[dict[str, Any]]], Any]] = None) -> list[dict[str, Any]]:
        if not force and not self.wants(cfg, ev):
            return []
        reqs = build_webhook_requests(cfg, ev)
        if not reqs:
            return []

        def run() -> list[dict[str, Any]]:
            results = []
            for svc, url, body, hdr in reqs:
                r = self._post_once(svc, url, body, hdr)
                attempts = 1
                # v1.15.5: Discord / Telegram rate limits (429) and brief network errors get one more try,
                # so a block message is not lost to a burst of alerts just before it.
                if not r["ok"] and (r["status"] == 429 or r["status"] is None or (isinstance(r["status"], int) and r["status"] >= 500)):
                    wait = min(self.MAX_RETRY_WAIT_SEC, _retry_after_sec(r.get("body", ""), 2.0)) if r["status"] == 429 else 2.0
                    try:
                        self.sleep(wait)
                    except Exception:
                        pass
                    r = self._post_once(svc, url, body, hdr)
                    attempts = 2
                r["attempts"] = attempts
                results.append(r)
            for r in results:
                self.last[r["service"]] = dict(r, ts=time.time())
                self.last_by_kind[str(ev.get("kind") or "")] = dict(r, ts=time.time())
                if not r["ok"]:
                    LOG.warning("Webhook %s failed for %s: %s", r["service"], ev.get("kind"), r["error"])
            if on_result is not None:
                try:
                    on_result(ev, results)
                except Exception:  # noqa: BLE001
                    pass
            return results

        if self.async_send and not force:
            threading.Thread(target=run, name="nk-webhook", daemon=True).start()
            return [{"service": s, "queued": True} for s, *_ in reqs]
        return run()
