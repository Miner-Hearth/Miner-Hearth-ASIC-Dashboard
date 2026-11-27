#!/usr/bin/env python3
"""Miner Hearth v1.15.5: expected-transition / maintenance grace (stdlib only).

Normal maintenance such as a pool switch, a restart Miner Hearth issued itself, or the
user's own "pause alerts" should not raise fault alerts for the parts that are normal
while a miner restarts: going offline for a minute, 0 GH/s while the ASICs initialise,
"pool not connected", a -1 °C temperature before the sensor is read, a fan at 0 RPM in
the first seconds, and so on.

How it works (generic for AxeOS / NerdQAxe / Braiins / stock Bitmain):

* Every poll the configured pool set (primary + fallback URL, port and user, or the
  CGMiner pool list) is compared with the last one seen. A change of the *configured*
  pools, made in Miner Hearth or outside it (AxeOS web UI, app), is a pool change. A
  failover (same configured pools, the miner just uses its fallback) is not a change,
  so the fallback-pool alert keeps working for real failovers.
* Uptime going backwards is a reboot. A reboot that coincides with, or follows (within
  an hour), a pool change, or follows a restart Miner Hearth itself issued, opens a
  grace window (default 10 min).
* During grace, offline / hashrate-drop / no-shares / fallback-pool / pool-rejects
  alerts and the boot-transient faults are suppressed (logged as alert_suppressed).
  Genuine faults (overheat, VR / chip temperature, power fault, shutdown, abnormal reset
  reasons) always alert.
* Grace ends early once the miner is back: online, hashing (>= 50 % of expected when
  known), on its primary pool and with a new accepted share, for 2 polls in a row. Then
  alert baselines restart (the hashrate-drop baseline does not compare against the
  restart dip). If grace runs out first, one alert says what did not recover.
* Any reboot (even an unexplained one) gets a short boot-settle period (default 180 s of
  device uptime) where only the boot-transient faults are treated as informational.
* A manual "pause alerts for N minutes" per machine for other planned work.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULTS: dict[str, Any] = {
    "grace_enabled": True,          # detect pool switches / NK restarts and pause the normal restart alerts
    "grace_min": 10.0,              # longest grace window; ends early when the miner is back to normal
    "boot_settle_enabled": True,    # boot-transient faults are informational for the first seconds after any reboot
    "boot_settle_sec": 180.0,
    "max_pause_min": 720.0,
}
LIMITS = {"grace_min": (2.0, 60.0), "boot_settle_sec": (30.0, 900.0), "max_pause_min": (10.0, 1440.0)}

# Alert kinds that are normal while a miner restarts.
SUPPRESSIBLE_KINDS = {"offline", "hashrate_drop", "no_shares", "fallback_pool", "pool_rejects"}

# Faults that firmware reports while booting / initialising ASICs / connecting to a new pool.
BOOT_TRANSIENT_FAULTS = {
    "axeos:temp_sensor_error", "axeos:pool_disconnected", "axeos:zero_hashrate", "axeos:low_hashrate",
    "axeos:dead_asic", "axeos:fallback_stratum", "axeos:wifi_disconnected", "axeos:pool_diff_low", "axeos:wifi_weak",
    "nerd:stratum_disconnected", "nerd:asic_sensor_dead", "nerd:pool_diff_err", "nerd:verify_blocked",
    "braiins:cgminer:temp_sensor_error", "braiins:cgminer:pools_dead", "braiins:status:dead_pools",
    "braiins:status:starting_wait", "braiins:status:application_unavailable", "braiins:cgminer:dev_unhealthy",
    "bitmain:zero_hashrate", "bitmain:low_hashrate", "bitmain:chain_missing", "bitmain:chain_few_chips",
    "bitmain:cgminer_error", "bitmain:temp_sensor_error",
}
# Only in the very first seconds of uptime (a fan reading 0 RPM later is a real problem).
EARLY_BOOT_FAULTS = {"axeos:fan_stalled", "nerd:fan_stalled", "nerd:fan_low_rpm"}
EARLY_BOOT_SEC = 90.0

KIND_LABELS = {"pool_change": "pool switch", "nk_restart": "restart by Miner Hearth", "nk_pool_change": "pool change from Miner Hearth",
               "manual": "maintenance pause", "firmware_update": "firmware update", "settings_change": "settings change"}

RECOVER_POLLS = 2
PENDING_CHANGE_SEC = 3600.0


def _f(v: Any, d: float, lo: float, hi: float) -> float:
    try:
        return float(min(hi, max(lo, float(v))))
    except (TypeError, ValueError):
        return d


def normalize_settings(raw: Any) -> dict[str, Any]:
    r = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    for k in ("grace_enabled", "boot_settle_enabled"):
        v = r.get(k)
        out[k] = v if isinstance(v, bool) else (DEFAULTS[k] if v is None else str(v).strip().lower() in {"1", "true", "yes", "on"})
    for k, (lo, hi) in LIMITS.items():
        out[k] = _f(r.get(k, DEFAULTS[k]), DEFAULTS[k], lo, hi)
    return out


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _norm_url(u: Any) -> str:
    s = str(u or "").strip().lower()
    for pre in ("stratum+tcp://", "stratum+ssl://", "stratum2+tcp://", "stratum+tls://", "tcp://", "ssl://"):
        if s.startswith(pre):
            s = s[len(pre):]
    return s.rstrip("/")


def pool_signature(r: Any) -> Optional[dict[str, Any]]:
    """Configured pools (not which one is in use). None when the reading carries no pool config."""
    pools = [p for p in (getattr(r, "pools", None) or []) if isinstance(p, dict)]
    cfg: list[list[Any]] = []
    for p in pools:
        if str(p.get("status") or "").strip().lower() == "disabled" or p.get("enabled") is False:
            continue
        url = _norm_url(p.get("url"))
        if url:
            cfg.append([str(p.get("pool", "")), url, str(p.get("user") or "").strip()])
    if cfg:
        return {"kind": "cgminer", "pools": cfg}
    url = _norm_url(getattr(r, "pool_url", ""))
    if not url:
        return None
    return {"kind": "primary_fallback",
            "primary": [url, getattr(r, "pool_port", None), str(getattr(r, "pool_user", "") or "").strip()],
            "fallback": [_norm_url(getattr(r, "fallback_pool_url", "")), getattr(r, "fallback_pool_port", None),
                         str(getattr(r, "fallback_pool_user", "") or "").strip()]}


def describe_change(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """What changed, without pool users (they carry wallets / worker names)."""
    out: dict[str, Any] = {"primary_changed": False, "fallback_changed": False, "user_changed": False}
    if old.get("kind") != new.get("kind"):
        out["primary_changed"] = True
        return out
    if new.get("kind") == "cgminer":
        o = {(p[0]): p for p in old.get("pools") or []}
        n = {(p[0]): p for p in new.get("pools") or []}
        first_o = (old.get("pools") or [[None, None, None]])[0]
        first_n = (new.get("pools") or [[None, None, None]])[0]
        out["primary_changed"] = first_o[1] != first_n[1] or first_o[2] != first_n[2]
        out["fallback_changed"] = {k: v[1] for k, v in o.items()} != {k: v[1] for k, v in n.items()} and not out["primary_changed"]
        out["user_changed"] = any(o.get(k, [None, None, None])[2] != v[2] for k, v in n.items())
        out["from"] = first_o[1]
        out["to"] = first_n[1]
        return out
    op, np_ = old.get("primary") or [None] * 3, new.get("primary") or [None] * 3
    of, nf = old.get("fallback") or [None] * 3, new.get("fallback") or [None] * 3
    out["primary_changed"] = op != np_
    out["fallback_changed"] = of != nf
    out["user_changed"] = op[2] != np_[2] or of[2] != nf[2]
    out["from"] = f"{op[0]}:{op[1]}" if op[0] else None
    out["to"] = f"{np_[0]}:{np_[1]}" if np_[0] else None
    return out


class MaintenanceGrace:
    """Per-device grace windows and manual pauses. Thread safe; persisted to userdata/maintenance.json."""

    def __init__(self, path: Optional[Path], clock: Callable[[], float] = time.time,
                 log: Optional[Callable[..., Any]] = None):
        self.path = Path(path) if path else None
        self.clock = clock
        self.log = log or (lambda *a, **k: None)   # log(etype, device=..., level=..., **data) -> event log
        self._lock = threading.RLock()
        self.dev: dict[str, dict[str, Any]] = {}
        self._suppressed_logged: set[tuple[str, str, float]] = set()
        self._load()

    # --- persistence ---------------------------------------------------------
    def _load(self) -> None:
        try:
            if self.path and self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict) and isinstance(d.get("devices"), dict):
                    for n, st in d["devices"].items():
                        if isinstance(st, dict):
                            self.dev[n] = {k: st.get(k) for k in ("pool_sig", "uptime", "uptime_ts", "grace", "pause_until", "pause_reason",
                                                                   "pending_change_ts", "pending_change")}
        except Exception:  # noqa: BLE001
            self.dev = {}

    def save(self) -> None:
        if not self.path:
            return
        try:
            with self._lock:
                data = {"schema_version": 1, "devices": {n: {k: v for k, v in st.items() if k in {
                    "pool_sig", "uptime", "uptime_ts", "grace", "pause_until", "pause_reason", "pending_change_ts", "pending_change"}}
                    for n, st in self.dev.items()}}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001
            pass

    def _st(self, name: str) -> dict[str, Any]:
        st = self.dev.get(name)
        if st is None:
            st = {"pool_sig": None, "uptime": None, "uptime_ts": None, "grace": None, "pause_until": 0.0, "pause_reason": "",
                  "pending_change_ts": None, "pending_change": None}
            self.dev[name] = st
        return st

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            if old in self.dev:
                self.dev[new] = self.dev.pop(old)
        self.save()

    def remove(self, name: str) -> None:
        with self._lock:
            self.dev.pop(name, None)
        self.save()

    # --- opening / closing -----------------------------------------------------
    def _open(self, name: str, kind: str, reason: str, now: float, settings: dict[str, Any], extra: Optional[dict[str, Any]] = None,
              start: Optional[float] = None) -> dict[str, Any]:
        st = self._st(name)
        minutes = float(settings.get("grace_min", DEFAULTS["grace_min"]))
        g = st.get("grace")
        if g and now < float(g.get("until") or 0):
            g["until"] = max(float(g["until"]), now + minutes * 60.0)
            g["reasons"] = (list(g.get("reasons") or []) + [reason])[-6:]
            if kind in {"pool_change", "nk_pool_change"}:
                g["kind"] = kind
            g.update(extra or {})
            self.log("maintenance_grace_extended", device=name, level="info", kind=g["kind"], reason=reason,
                     until=g["until"], minutes=round((g["until"] - now) / 60.0, 1))
            return g
        g = {"id": round(now, 3), "kind": kind, "reason": reason, "reasons": [reason], "start": float(start if start is not None else now),
             "until": now + minutes * 60.0, "rebooted": False, "acc_ref": None, "healthy_polls": 0, "seen_ok_after": False}
        g.update(extra or {})
        st["grace"] = g
        self.log("maintenance_grace_start", device=name, level="info", kind=kind, reason=reason, minutes=minutes,
                 note="Alerts that are normal while a miner restarts are paused; genuine faults still alert.")
        return g

    def expect(self, name: str, kind: str, reason: str, settings: dict[str, Any], now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Miner Hearth itself is about to restart / reconfigure this miner."""
        s = normalize_settings(settings)
        if not s["grace_enabled"]:
            return None
        now = self.clock() if now is None else now
        with self._lock:
            st = self._st(name)
            if kind in {"nk_pool_change", "pool_change"}:
                st["pending_change_ts"] = now
                st["pending_change"] = {"by": "miner_hearth", "reason": reason}
            g = self._open(name, kind, reason, now, s)
        self.save()
        return dict(g)

    def pause(self, name: str, minutes: float, reason: str = "", settings: Optional[dict[str, Any]] = None,
              now: Optional[float] = None) -> float:
        s = normalize_settings(settings)
        now = self.clock() if now is None else now
        minutes = max(0.0, min(float(minutes or 0), s["max_pause_min"]))
        with self._lock:
            st = self._st(name)
            if minutes <= 0:
                was = float(st.get("pause_until") or 0) > now
                st["pause_until"], st["pause_reason"] = 0.0, ""
                if was:
                    self.log("maintenance_pause_end", device=name, level="info", reason="resumed by user")
            else:
                st["pause_until"] = now + minutes * 60.0
                st["pause_reason"] = (reason or "planned maintenance")[:200]
                self.log("maintenance_pause_start", device=name, level="info", minutes=minutes, reason=st["pause_reason"])
            until = float(st["pause_until"] or 0)
        self.save()
        return until

    # --- queries ---------------------------------------------------------------
    def active(self, name: str, now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """The grace window or manual pause in force now, if any."""
        now = self.clock() if now is None else now
        with self._lock:
            st = self.dev.get(name)
            if not st:
                return None
            if float(st.get("pause_until") or 0) > now:
                return {"kind": "manual", "reason": st.get("pause_reason") or "planned maintenance", "until": float(st["pause_until"]),
                        "id": "pause:" + str(st["pause_until"])}
            g = st.get("grace")
            if g and now < float(g.get("until") or 0):
                return dict(g)
        return None

    def suppress_alert(self, name: str, kind: str, now: Optional[float] = None) -> str:
        """Reason string when this alert kind is expected right now (empty = alert normally)."""
        if kind not in SUPPRESSIBLE_KINDS:
            return ""
        a = self.active(name, now)
        if not a:
            return ""
        return f"{KIND_LABELS.get(a['kind'], a['kind'])}: {a.get('reason') or ''}".strip(": ")

    def suppress_fault(self, name: str, key: str, uptime: Optional[float], settings: dict[str, Any],
                       now: Optional[float] = None) -> str:
        """Reason when a fault is a normal part of a restart (grace / pause / boot settle). Genuine faults: ''."""
        s = normalize_settings(settings)
        key = str(key or "")
        early = key in EARLY_BOOT_FAULTS
        if key not in BOOT_TRANSIENT_FAULTS and not early:
            return ""
        up = _num(uptime)
        if early and not (up is not None and up < EARLY_BOOT_SEC):
            return ""
        a = self.active(name, now)
        if a:
            return f"expected during {KIND_LABELS.get(a['kind'], a['kind'])}"
        if s["boot_settle_enabled"] and up is not None and up < float(s["boot_settle_sec"]):
            return f"expected while the miner boots (uptime {up:.0f} s)"
        return ""

    def note_suppressed(self, name: str, kind: str, reason: str, detail: str = "", now: Optional[float] = None) -> None:
        a = self.active(name, now)
        gid = float(a["id"]) if a and isinstance(a.get("id"), (int, float)) else (hash(a["id"]) if a else 0.0)
        key = (name, kind, gid)
        with self._lock:
            if key in self._suppressed_logged:
                return
            self._suppressed_logged.add(key)
            if len(self._suppressed_logged) > 5000:
                self._suppressed_logged.clear()
        self.log("alert_suppressed", device=name, level="info", kind=kind, reason=reason, detail=detail[:300])

    def card_info(self, name: str, now: Optional[float] = None) -> Optional[dict[str, Any]]:
        now = self.clock() if now is None else now
        a = self.active(name, now)
        if not a:
            return None
        mins = max(0, int(round((float(a["until"]) - now) / 60.0)))
        if a["kind"] == "manual":
            label = f"Maintenance: alerts paused for {mins} more min"
        elif a["kind"] in {"pool_change", "nk_pool_change"}:
            label = f"Pool switch detected{', restarting' if not a.get('seen_ok_after') else ''}; restart alerts paused up to {mins} min"
        else:
            label = f"{KIND_LABELS.get(a['kind'], 'Maintenance').capitalize()}; restart alerts paused up to {mins} min"
        return {"kind": a["kind"], "label": label, "reason": a.get("reason") or "", "until": float(a["until"]), "remaining_min": mins,
                "manual": a["kind"] == "manual"}

    # --- per-poll observation ----------------------------------------------------
    def observe(self, name: str, r: Any, settings: dict[str, Any], now: Optional[float] = None,
                on_fallback: Optional[bool] = None) -> dict[str, Any]:
        """Update pool / uptime tracking and the grace window. Returns
        {"ended": None|"recovered"|"expired"|"pause_ended", "expired": {...} when grace ran out, "grace": dict|None}."""
        s = normalize_settings(settings)
        now = self.clock() if now is None else now
        res: dict[str, Any] = {"ended": None, "expired": None, "grace": None, "opened": False}
        changed = False
        with self._lock:
            st = self._st(name)
            ok = bool(getattr(r, "ok", False))
            # manual pause expiry
            pu = float(st.get("pause_until") or 0)
            if pu and now >= pu:
                st["pause_until"], st["pause_reason"] = 0.0, ""
                self.log("maintenance_pause_end", device=name, level="info", reason="pause time is over")
                res["ended"] = "pause_ended"
                changed = True
            if ok:
                # reboot detection
                up = _num(getattr(r, "uptime_sec", None))
                rebooted = False
                if up is not None:
                    pu_, pts = _num(st.get("uptime")), _num(st.get("uptime_ts"))
                    if pu_ is not None and pts is not None:
                        elapsed = max(0.0, now - pts)
                        rebooted = up + max(120.0, 0.01 * elapsed) < pu_ + elapsed
                    st["uptime"], st["uptime_ts"] = up, now
                # pool config change detection
                sig = pool_signature(r)
                pool_changed = None
                if sig is not None:
                    prev = st.get("pool_sig")
                    if prev is not None and prev != sig:
                        pool_changed = describe_change(prev, sig)
                    st["pool_sig"] = sig
                    changed = changed or prev != sig
                if pool_changed and (pool_changed["primary_changed"] or pool_changed["fallback_changed"] or pool_changed["user_changed"]):
                    st["pending_change_ts"] = now
                    st["pending_change"] = {"by": "detected", **{k: v for k, v in pool_changed.items()}}
                    self.log("pool_change_detected", device=name, level="info", rebooted=rebooted, **pool_changed,
                             note="Configured pool changed (not a failover)." if pool_changed["primary_changed"] else "Fallback / worker settings changed.")
                    if s["grace_enabled"]:
                        g = self._open(name, "pool_change", "configured pool changed" + (" and the miner restarted" if rebooted else ""), now, s)
                        res["opened"] = True
                        if rebooted:
                            g["rebooted"] = True
                pend_ts = _num(st.get("pending_change_ts"))
                if rebooted:
                    self.log("maintenance_reboot_seen", device=name, level="info", uptime_sec=up,
                             during_grace=bool(st.get("grace") and now < float(st["grace"].get("until") or 0)),
                             after_pool_change=bool(pend_ts and now - pend_ts <= PENDING_CHANGE_SEC))
                    g = st.get("grace")
                    if g and now < float(g.get("until") or 0):
                        g["rebooted"] = True
                        g["acc_ref"] = None
                    elif s["grace_enabled"] and pend_ts and now - pend_ts <= PENDING_CHANGE_SEC:
                        g = self._open(name, "pool_change", "restart to apply a recent pool change", now, s)
                        g["rebooted"] = True
                        res["opened"] = True
                    st["pending_change_ts"], st["pending_change"] = None, None
                    changed = True
                elif pend_ts and now - pend_ts > PENDING_CHANGE_SEC:
                    st["pending_change_ts"], st["pending_change"] = None, None
            # grace progress
            g = st.get("grace")
            if g:
                if now >= float(g.get("until") or 0):
                    status = self._health(r, g, on_fallback)
                    st["grace"] = None
                    changed = True
                    if status:
                        res["ended"] = "expired"
                        res["expired"] = {"kind": g["kind"], "reason": g.get("reason"), "status": status[0], "detail": status[1],
                                          "minutes": round((now - float(g["start"])) / 60.0, 1)}
                        self.log("maintenance_grace_end", device=name, level="warn", result="expired_not_recovered", kind=g["kind"],
                                 status=status[0], detail=status[1])
                    else:
                        res["ended"] = "recovered"
                        self.log("maintenance_grace_end", device=name, level="info", result="recovered", kind=g["kind"],
                                 minutes=round((now - float(g["start"])) / 60.0, 1))
                else:
                    status = self._health(r, g, on_fallback)
                    if ok:
                        g["seen_ok_after"] = True
                    if status is None and (g["kind"] not in {"pool_change", "nk_pool_change", "nk_restart"} or g.get("rebooted")
                                           or g.get("got_share")):
                        g["healthy_polls"] = int(g.get("healthy_polls") or 0) + 1
                    else:
                        g["healthy_polls"] = 0
                    if g["healthy_polls"] >= RECOVER_POLLS:
                        st["grace"] = None
                        changed = True
                        res["ended"] = "recovered"
                        self.log("maintenance_grace_end", device=name, level="info", result="recovered", kind=g["kind"],
                                 minutes=round((now - float(g["start"])) / 60.0, 1),
                                 note="Back online, hashing and sharing on its primary pool; alert baselines restarted.")
            res["grace"] = dict(st["grace"]) if st.get("grace") else None
        if changed or res["opened"]:
            self.save()
        return res

    @staticmethod
    def _health(r: Any, g: dict[str, Any], on_fallback: Optional[bool]) -> Optional[tuple[str, str]]:
        """None when healthy, else (alert kind, detail) of what is still wrong."""
        if not bool(getattr(r, "ok", False)):
            err = str(getattr(r, "error", "") or "unreachable")
            return "offline", f"still offline ({err[:160]})"
        hr = _num(getattr(r, "hashrate", None))
        exp = _num(getattr(r, "expected_hashrate", None))
        up = _num(getattr(r, "uptime_sec", None))
        if hr is None or hr <= 0:
            return "hashrate_drop", "online but not hashing (0 GH/s)"
        if exp and exp > 0 and hr < 0.5 * exp:
            return "hashrate_drop", f"hashing at {hr:,.0f} GH/s, under half of the expected {exp:,.0f} GH/s"
        if on_fallback:
            return "fallback_pool", "mining on its fallback pool, not the new primary pool"
        acc = _num(getattr(r, "shares_accepted", None))
        if acc is not None:
            ref = _num(g.get("acc_ref"))
            if ref is None or acc < ref:
                g["acc_ref"] = acc   # first reading after the restart (counters start again)
                if not (g.get("rebooted") and acc > 0):
                    return "no_shares", "no share accepted since the restart"
            elif acc <= ref:
                return "no_shares", "no share accepted since the restart"
            g["got_share"] = True
        if up is not None and up < 60:
            return "hashrate_drop", "still starting up"
        return None
