#!/usr/bin/env python3
"""Miner Hearth v1.15.4: persistent, redacted full event / telemetry log (stdlib only).

Everything Miner Hearth does and regular snapshots from every connected miner are
appended to daily JSON Lines files under userdata/logs/:

    userdata/logs/nk-events-YYYY-MM-DD.jsonl      (today, plain text)
    userdata/logs/nk-events-YYYY-MM-DD.jsonl.gz   (earlier days, compressed)

One JSON object per line with at least: ts (local ISO time), t (epoch seconds),
type, level and device. Entries are run through the same redaction as the
diagnostics export *before* they touch the disk (pool users / wallets, Wi-Fi
names, MAC addresses, tokens, passwords, e-mail, Windows user names in paths;
IP addresses masked to a.b.c.x), so the folder and any export are safe to share.

Clearing or dismissing an alert never touches these files; the clear action is
itself recorded. Files rotate daily and are pruned by age (default 14 days) and
by a total size cap (default 200 MB), oldest first. Logging can never crash the
app: every public method swallows its own errors.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import platform
import re
import sys
import threading
import time
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

import nk_faults

LOG = logging.getLogger("autotune.minerhearth.eventlog")

FILE_PREFIX = "nk-events-"
SCHEMA = 1
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "snapshot_min": 5.0,
    "raw_snapshot_min": 60.0,
    "retention_days": 14,
    "max_mb": 200,
}
LIMITS = {"snapshot_min": (1.0, 120.0), "raw_snapshot_min": (0.0, 1440.0), "retention_days": (1, 365), "max_mb": (10, 5000)}
EXPORT_RANGES = {"24h": 86400.0, "7d": 7 * 86400.0, "30d": 30 * 86400.0, "all": None}
SNAPSHOT_TYPES = {"snapshot", "raw_snapshot", "heartbeat"}
LOG_DEDUP_SEC = 600.0
POLL_FAIL_REPEAT = 20          # re-log an ongoing comm failure every N failed polls
FLEET_WINDOW_SEC = 90.0        # comm losses this close together are flagged as one fleet event
SLOW_POLL_MS = 5000.0


def normalize_settings(raw: Any) -> dict[str, Any]:
    r = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    v = r.get("enabled")
    out["enabled"] = v if isinstance(v, bool) else (str(v).strip().lower() in {"1", "true", "yes", "on"} if v is not None else True)
    for k in ("snapshot_min", "raw_snapshot_min"):
        lo, hi = LIMITS[k]
        try:
            out[k] = float(min(hi, max(lo, float(r.get(k, DEFAULTS[k])))))
        except (TypeError, ValueError):
            out[k] = DEFAULTS[k]
    for k in ("retention_days", "max_mb"):
        lo, hi = LIMITS[k]
        try:
            out[k] = int(min(hi, max(lo, int(float(r.get(k, DEFAULTS[k]))))))
        except (TypeError, ValueError):
            out[k] = DEFAULTS[k]
    return out


# ---------------------------------------------------------------------------
# Redaction (diagnostics redaction + a few log-specific scrubs)
# ---------------------------------------------------------------------------

_EXTRA_SCRUB = [
    # wallet.worker -> keep the shape, drop the worker name
    (re.compile(r"\[address\]\.[\w.\-]+"), "[address].[worker]"),
    # Windows / Unix home folders carry the account name
    (re.compile(r"(?i)\b([A-Z]:[\\/]+(?:Users|Documents and Settings)[\\/]+)[^\\/\s\"']+"), r"\1[user]"),
    (re.compile(r"(/home/|/Users/)[^/\s\"']+"), r"\1[user]"),
    # key=value secrets inside free text / URLs
    (re.compile(r"(?i)\b(password|passwd|pwd|token|secret|apikey|api_key|pin|ssid|auth)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&\"'}]+)"), r"\1\2[redacted]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"), r"\1[redacted]"),
    (re.compile(r"(?i)(https?://(?:discord(?:app)?\.com|api\.telegram\.org)/\S*?)(?:/[\w\-]{20,}|bot\d+:[\w\-]+)"), r"\1/[redacted]"),
]


def _scrub(s: str, secrets: Iterable[str] = ()) -> str:
    out = nk_faults.scrub_text(s)
    for rx, rep in _EXTRA_SCRUB:
        out = rx.sub(rep, out)
    for sec in secrets:
        if sec and len(sec) >= 4 and sec in out:
            out = out.replace(sec, "[redacted]")
    return out


def redact_entry(obj: Any, secrets: Iterable[str] = (), depth: int = 0) -> Any:
    """Same rules as the diagnostics export (nk_faults.redact) plus log-specific scrubs."""
    secrets = tuple(sorted({s for s in secrets if isinstance(s, str) and len(s) >= 4}, key=len, reverse=True))
    return _redact(obj, secrets, depth)


_NAME_KEYS = {"device", "devices", "still_online", "old_name", "name"}


def _redact(obj: Any, secrets: tuple[str, ...], depth: int) -> Any:
    if depth > 14:
        return "[...]"
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            ks = str(k)
            if nk_faults.is_personal_key(ks):
                out[ks] = v if v in (None, "", [], {}) else ("[redacted]" if not isinstance(v, bool) else v)
            elif ks in _NAME_KEYS:  # the user's own machine labels: regex scrub only, never literal-secret replacement
                out[ks] = _redact(v, (), depth + 1)
            else:
                out[_scrub(ks, secrets) if not ks.isidentifier() else ks] = _redact(v, secrets, depth + 1)
        return out
    if isinstance(obj, (list, tuple, set)):
        return [_redact(v, secrets, depth + 1) for v in obj]
    if isinstance(obj, str):
        return _scrub(obj, secrets)
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
        return round(obj, 4)
    if obj is None or isinstance(obj, (bool, int)):
        return obj
    return _scrub(str(obj), secrets)


def iso_local(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds")


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------

class EventLog:
    """Append-only, daily-rotated, redacted JSON Lines log. Thread safe; never raises."""

    def __init__(self, directory: Optional[Path], clock: Callable[[], float] = time.time,
                 app_version: str = "", settings: Optional[dict[str, Any]] = None):
        self.dir = Path(directory) if directory else None
        self.clock = clock
        self.app_version = app_version
        self.cfg = normalize_settings(settings)
        self._lock = threading.RLock()
        self._secrets: set[str] = set()
        self._dev: dict[str, dict[str, Any]] = {}
        self._last_maint = 0.0
        self._cur_day = ""
        self._cap_hit_day = ""
        self._log_seen: dict[tuple[str, str, str], list[float]] = {}
        self._windows_active: dict[str, set[str]] = {}
        self._last_heartbeat = 0.0
        self.write_errors = 0
        self.last_error = ""
        self.state_path = (self.dir / "eventlog_state.json") if self.dir else None
        self._state: dict[str, Any] = self._load_state()

    # --- config / secrets --------------------------------------------------
    def configure(self, settings: Any) -> None:
        try:
            self.cfg = normalize_settings(settings)
        except Exception:  # noqa: BLE001
            pass

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled")) and self.dir is not None

    def add_secret(self, *values: Any) -> None:
        """Literal strings (pool users etc.) that must never appear, even in free text."""
        with self._lock:
            for v in values:
                s = str(v or "").strip()
                if len(s) >= 4:
                    self._secrets.add(s)
                    if "." in s:  # wallet.worker: the wallet part on its own too
                        head = s.split(".", 1)[0]
                        if len(head) >= 8:
                            self._secrets.add(head)

    _LEARN_TOKENS = {"ssid", "bssid", "user", "username", "worker", "wallet", "password", "passwd", "pass", "pwd", "token",
                     "secret", "payout", "apikey", "pin", "email", "mail"}

    def _learn_secrets(self, obj: Any, depth: int = 0) -> None:
        """Values seen under personal keys (Wi-Fi name, pool user, ...) are also scrubbed from free text everywhere."""
        if depth > 8:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str):
                    toks = nk_faults._key_tokens(str(k))
                    if "has" not in toks and any(t in self._LEARN_TOKENS for t in toks):
                        self.add_secret(v)
                elif isinstance(v, (dict, list)):
                    self._learn_secrets(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj[:200]:
                if isinstance(v, (dict, list)):
                    self._learn_secrets(v, depth + 1)

    # --- files -------------------------------------------------------------
    def _day(self, t: float) -> str:
        return datetime.fromtimestamp(t).strftime("%Y-%m-%d")

    def path_for(self, t: float) -> Optional[Path]:
        return (self.dir / f"{FILE_PREFIX}{self._day(t)}.jsonl") if self.dir else None

    def files(self) -> list[Path]:
        if not self.dir or not self.dir.exists():
            return []
        out = [p for p in self.dir.iterdir() if p.is_file() and p.name.startswith(FILE_PREFIX)
               and (p.name.endswith(".jsonl") or p.name.endswith(".jsonl.gz"))]
        return sorted(out, key=lambda p: p.name)

    @staticmethod
    def _file_day(p: Path) -> str:
        return p.name[len(FILE_PREFIX):len(FILE_PREFIX) + 10]

    def _load_state(self) -> dict[str, Any]:
        try:
            if self.state_path and self.state_path.exists():
                d = json.loads(self.state_path.read_text(encoding="utf-8"))
                return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except Exception:  # noqa: BLE001
            pass

    # --- writing -----------------------------------------------------------
    def write(self, etype: str, device: str = "", level: str = "info", t: Optional[float] = None, **data: Any) -> Optional[dict[str, Any]]:
        if not self.enabled:
            return None
        try:
            now = self.clock() if t is None else float(t)
            entry: dict[str, Any] = {"ts": iso_local(now), "t": round(now, 3), "type": str(etype), "level": str(level or "info"),
                                     "device": str(device or "")}
            entry.update(data)
            with self._lock:
                self._learn_secrets(data)
                day = self._day(now)
                if etype in SNAPSHOT_TYPES and self._cap_hit_day == day:
                    return None  # size cap reached with only today's file left: keep events, skip bulk snapshots
                red = redact_entry(entry, self._secrets)
                line = json.dumps(red, separators=(",", ":"), ensure_ascii=False, default=str) + "\n"
                path = self.path_for(now)
                assert path is not None
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line)
                if day != self._cur_day:
                    first = not self._cur_day
                    self._cur_day = day
                    if not first:
                        self._last_maint = 0.0
                if now - self._last_maint >= 600.0 or self._last_maint == 0.0:
                    self._last_maint = now
                    self.maintain(now)
            return red
        except Exception as exc:  # noqa: BLE001 - logging must never crash the app
            self.write_errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            return None

    def maintain(self, now: Optional[float] = None) -> dict[str, Any]:
        """Compress earlier days, prune by age and total size. Never touches today's file except to flag the cap."""
        res = {"compressed": 0, "deleted": 0}
        if not self.dir:
            return res
        try:
            now = self.clock() if now is None else now
            today = self._day(now)
            with self._lock:
                for p in self.files():
                    if p.name.endswith(".jsonl") and self._file_day(p) < today:
                        gz = p.with_name(p.name + ".gz")
                        try:
                            with open(p, "rb") as src, gzip.open(gz, "ab") as dst:
                                dst.write(src.read())
                            p.unlink()
                            res["compressed"] += 1
                        except OSError:
                            pass
                keep_days = int(self.cfg.get("retention_days") or DEFAULTS["retention_days"])
                cutoff = datetime.fromtimestamp(now - keep_days * 86400.0).strftime("%Y-%m-%d")
                for p in self.files():
                    if self._file_day(p) < cutoff:
                        try:
                            p.unlink()
                            res["deleted"] += 1
                        except OSError:
                            pass
                cap = int(self.cfg.get("max_mb") or DEFAULTS["max_mb"]) * 1024 * 1024
                files = self.files()
                total = sum(p.stat().st_size for p in files)
                while total > cap and files:
                    p = files.pop(0)
                    if self._file_day(p) >= today:
                        if self._cap_hit_day != today:
                            self._cap_hit_day = today
                            res["cap_hit"] = True
                        break
                    try:
                        total -= p.stat().st_size
                        p.unlink()
                        res["deleted"] += 1
                    except OSError:
                        break
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"maintain: {type(exc).__name__}: {exc}"[:200]
        return res

    # --- reading / export --------------------------------------------------
    def iter_entries(self, since: Optional[float] = None, until: Optional[float] = None) -> Iterator[dict[str, Any]]:
        with self._lock:
            files = self.files()
        since_day = datetime.fromtimestamp(since).strftime("%Y-%m-%d") if since else ""
        for p in files:
            if since_day and self._file_day(p) < since_day:
                continue
            try:
                opener = gzip.open if p.name.endswith(".gz") else open
                with opener(p, "rt", encoding="utf-8", errors="replace") as fh:  # type: ignore[operator]
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        t = _num(e.get("t")) or 0.0
                        if since is not None and t < since:
                            continue
                        if until is not None and t > until:
                            continue
                        yield e
            except (OSError, EOFError, gzip.BadGzipFile):
                continue

    def tail(self, limit: int = 200, types: Optional[set[str]] = None, device: str = "") -> list[dict[str, Any]]:
        from collections import deque
        out: deque = deque(maxlen=max(1, min(5000, int(limit))))
        since = self.clock() - 2 * 86400.0
        for e in self.iter_entries(since=since):
            if types and e.get("type") not in types:
                continue
            if device and e.get("device") != device:
                continue
            out.append(e)
        return list(out)

    def stats(self) -> dict[str, Any]:
        files = self.files()
        sizes = []
        for p in files:
            try:
                sizes.append(p.stat().st_size)
            except OSError:
                sizes.append(0)
        return {"enabled": self.enabled, "dir": _scrub(str(self.dir)) if self.dir else "", "files": len(files),
                "total_bytes": sum(sizes), "oldest_day": self._file_day(files[0]) if files else "",
                "newest_day": self._file_day(files[-1]) if files else "", "write_errors": self.write_errors,
                "last_error": self.last_error, "settings": dict(self.cfg)}

    def export_zip(self, rng: str = "24h", anonymize: bool = False, extra_files: Optional[dict[str, Any]] = None) -> tuple[bytes, dict[str, Any]]:
        """One redacted zip of the chosen range, ready to send for review."""
        now = self.clock()
        span = EXPORT_RANGES.get(rng, EXPORT_RANGES["24h"])
        if rng not in EXPORT_RANGES:
            rng = "24h"
        since = None if span is None else now - span
        names: dict[str, str] = {}

        def anon(n: Any) -> Any:
            if not anonymize or not isinstance(n, str) or not n:
                return n
            if n not in names:
                names[n] = f"Machine {len(names) + 1}"
            return names[n]

        counts: dict[str, int] = {}
        buf = io.StringIO()
        first_t = last_t = None
        n = 0
        for e in self.iter_entries(since=since):
            e = redact_entry(e, self._secrets)  # defence in depth: re-redact on the way out
            if anonymize:
                e["device"] = anon(e.get("device"))
                if isinstance(e.get("devices"), list):
                    e["devices"] = [anon(x) for x in e["devices"]]
                s = json.dumps(e, separators=(",", ":"), ensure_ascii=False)
                for real, fake in names.items():
                    if len(real) >= 3:
                        s = re.sub(r"(?<![\w])" + re.escape(real) + r"(?![\w])", fake, s)
                buf.write(s + "\n")
            else:
                buf.write(json.dumps(e, separators=(",", ":"), ensure_ascii=False) + "\n")
            counts[str(e.get("type"))] = counts.get(str(e.get("type")), 0) + 1
            t = _num(e.get("t"))
            if t is not None:
                first_t = t if first_t is None else min(first_t, t)
                last_t = t if last_t is None else max(last_t, t)
            n += 1
        manifest = {
            "miner_hearth_event_log": SCHEMA, "app_version": self.app_version, "generated": iso_local(now), "range": rng,
            "entries": n, "first": iso_local(first_t) if first_t else None, "last": iso_local(last_t) if last_t else None,
            "types": dict(sorted(counts.items())), "machine_names_anonymized": bool(anonymize),
            "note": "Personal data removed: pool users/wallets, Wi-Fi names, MAC addresses, tokens, passwords, e-mail and account names; IP addresses masked.",
        }
        zb = io.BytesIO()
        with zipfile.ZipFile(zb, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("nk-events.jsonl", buf.getvalue())
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            for fname, content in (extra_files or {}).items():
                try:
                    red = redact_entry(content, self._secrets)
                    txt = json.dumps(red, indent=2, ensure_ascii=False, default=str)
                    if anonymize:
                        for real, fake in names.items():
                            if len(real) >= 3:
                                txt = re.sub(r"(?<![\w])" + re.escape(real) + r"(?![\w])", fake, txt)
                    zf.writestr(fname, txt)
                except Exception:  # noqa: BLE001
                    continue
        return zb.getvalue(), manifest

    # --- app lifecycle -----------------------------------------------------
    def app_start(self, info: dict[str, Any]) -> None:
        prev = self._state.get("run") if isinstance(self._state.get("run"), dict) else None
        if prev and prev.get("running"):
            last = _num(prev.get("last_seen")) or 0.0
            self.write("unclean_shutdown_detected", level="warn", previous_version=prev.get("version"),
                       previous_start=iso_local(_num(prev.get("started")) or 0.0) if prev.get("started") else None,
                       last_seen=iso_local(last) if last else None,
                       note="The previous Miner Hearth run ended without a clean stop (crash, kill, power loss or Windows restart).")
        now = self.clock()
        self.write("app_start", level="info", version=self.app_version, python=sys.version.split()[0], os=platform.platform(),
                   frozen=bool(getattr(sys, "frozen", False)), pid=os.getpid(), **info)
        self._state["run"] = {"running": True, "started": now, "last_seen": now, "version": self.app_version}
        self._save_state()

    def app_stop(self, reason: str = "") -> None:
        self.write("app_stop", level="info", reason=reason, version=self.app_version)
        run = self._state.get("run") if isinstance(self._state.get("run"), dict) else {}
        run.update({"running": False, "last_seen": self.clock()})
        self._state["run"] = run
        self._save_state()

    def heartbeat(self, info: dict[str, Any], now: Optional[float] = None) -> None:
        """Once per snapshot interval: proves NK was alive, flags gaps (PC sleep / freeze / clock jump)."""
        if not self.enabled:
            return
        now = self.clock() if now is None else now
        interval = float(self.cfg["snapshot_min"]) * 60.0
        if self._last_heartbeat and now - self._last_heartbeat < interval:
            return
        if self._last_heartbeat and now - self._last_heartbeat > max(3 * interval, 900.0):
            self.write("nk_gap_detected", level="warn", gap_sec=round(now - self._last_heartbeat),
                       note="Miner Hearth did not run its background loop for this long (PC sleep, freeze or clock change).")
        self._last_heartbeat = now
        self.write("heartbeat", level="debug", threads=threading.active_count(), log=self.stats_brief(), **info)
        run = self._state.get("run") if isinstance(self._state.get("run"), dict) else {}
        run["last_seen"] = now
        self._state["run"] = run
        self._save_state()

    def stats_brief(self) -> dict[str, Any]:
        files = self.files()
        try:
            total = sum(p.stat().st_size for p in files)
        except OSError:
            total = 0
        return {"files": len(files), "mb": round(total / 1048576.0, 2), "write_errors": self.write_errors}

    # --- generic hub hooks -------------------------------------------------
    def alert(self, ev: dict[str, Any]) -> None:
        extra = {k: v for k, v in ev.items() if k not in {"ts", "kind", "level", "device", "title", "message", "sound", "id"}}
        self.write("alert", device=str(ev.get("device") or ""), level="warn" if ev.get("level") in {"problem"} else str(ev.get("level") or "info"),
                   kind=ev.get("kind"), title=ev.get("title"), message=ev.get("message"), alert_id=ev.get("id"), **extra)

    def alert_states(self, device: str, before: set[str], after: set[str], context: Optional[dict[str, Any]] = None) -> None:
        for k in sorted(after - before):
            self.write("alert_raised", device=device, level="warn", kind=k, **(context or {}))
        for k in sorted(before - after):
            self.write("alert_recovered", device=device, level="info", kind=k, **(context or {}))

    def tuning(self, entry: dict[str, Any]) -> None:
        e = dict(entry)
        dev = str(e.pop("device", "") or "")
        e.pop("ts", None)
        lvl = "warn" if str(e.get("source")) in {"emergency"} or str(e.get("action", "")).startswith("emergency") else "info"
        self.write("tuning", device=dev, level=lvl, **e)

    def settings_changed(self, old: dict[str, Any], new: dict[str, Any], source: str = "dashboard") -> None:
        diff = settings_diff(old, new)
        if diff:
            self.write("settings_changed", level="info", source=source, changes=diff[:200], count=len(diff))

    def windows(self, kind: str, active: dict[str, dict[str, Any]], now: Optional[float] = None) -> None:
        """TOU / quiet window enter / exit transitions. active: {window_name: details}."""
        prev = self._windows_active.get(kind)
        cur = set(active)
        if prev is None:
            self._windows_active[kind] = cur
            if cur:
                for w in sorted(cur):
                    self.write(f"{kind}_window_active_at_start", level="info", window=w, **(active.get(w) or {}))
            return
        for w in sorted(cur - prev):
            self.write(f"{kind}_window_enter", level="info", window=w, **(active.get(w) or {}))
        for w in sorted(prev - cur):
            self.write(f"{kind}_window_exit", level="info", window=w)
        self._windows_active[kind] = cur

    def exception(self, where: str, exc: BaseException, tb: Any = None) -> None:
        text = "".join(traceback.format_exception(type(exc), exc, tb if tb is not None else exc.__traceback__))[-8000:]
        self.write("uncaught_exception", level="error", where=where, exception_class=type(exc).__name__, error=str(exc)[:500], traceback=text)

    # --- per-device poll observation ----------------------------------------
    def _ds(self, name: str) -> dict[str, Any]:
        st = self._dev.get(name)
        if st is None:
            persisted = (self._state.get("devices") or {}).get(name) if isinstance(self._state.get("devices"), dict) else None
            st = {"online": None, "fails": 0, "fail_since": None, "first_error": "", "last_error": "", "last_ok": None,
                  "uptime": None, "uptime_ts": None, "pool": None, "fallback": None, "session_best": None,
                  "lat": [], "fails_window": 0, "polls_window": 0, "last_snap": 0.0, "last_raw": 0.0,
                  "last_slow": 0.0, "power_limit": None, "freq": None, "volt": None, "pool_states": None}
            if isinstance(persisted, dict):
                st["uptime"] = _num(persisted.get("uptime"))
                st["uptime_ts"] = _num(persisted.get("ts"))
            self._dev[name] = st
        return st

    def forget(self, name: str) -> None:
        with self._lock:
            self._dev.pop(name, None)
            devs = self._state.get("devices")
            if isinstance(devs, dict) and name in devs:
                devs.pop(name, None)
                self._save_state()

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            if old in self._dev:
                self._dev[new] = self._dev.pop(old)
            devs = self._state.get("devices")
            if isinstance(devs, dict) and old in devs:
                devs[new] = devs.pop(old)
                self._save_state()
        self.write("device_renamed", device=new, old_name=old)

    def observe_poll(self, name: str, reading: Any, now: Optional[float] = None, latency_ms: Optional[float] = None,
                     exc_class: str = "", context: Optional[dict[str, Any]] = None,
                     snapshot_extra: Optional[dict[str, Any]] = None, fleet: Optional[dict[str, dict[str, Any]]] = None) -> None:
        if not self.enabled:
            return
        try:
            self._observe(name, reading, self.clock() if now is None else now, latency_ms, exc_class, context or {}, snapshot_extra or {})
        except Exception as exc:  # noqa: BLE001
            self.write_errors += 1
            self.last_error = f"observe: {type(exc).__name__}: {exc}"[:200]

    def _observe(self, name: str, r: Any, now: float, latency_ms: Optional[float], exc_class: str,
                 context: dict[str, Any], extra: dict[str, Any]) -> None:
        g = lambda k, d=None: getattr(r, k, d)  # noqa: E731
        ok = bool(g("ok", False))
        reachable = bool(g("reachable", False))
        err = str(g("error", "") or "")
        with self._lock:
            st = self._ds(name)
            st["polls_window"] += 1
            if latency_ms is not None:
                st["lat"].append(float(latency_ms))
            lat = None if latency_ms is None else round(float(latency_ms), 1)
            if not ok:
                st["fails"] += 1
                st["fails_window"] += 1
                state = "unparsed" if reachable else "offline"
                if st["online"] is not False:
                    st["fail_since"] = now
                    st["first_error"] = err
                    st["last_error"] = err
                    was = st["online"]
                    st["online"] = False
                    self.write("comm_lost", device=name, level="warn", state=state, error=err, exception_class=exc_class,
                               reachable=reachable, latency_ms=lat, previous_state=("online" if was else "unknown (first poll)"),
                               last_ok=iso_local(st["last_ok"]) if st["last_ok"] else None, **context)
                    self._fleet_check(name, now)
                elif err != st["last_error"] or st["fails"] % POLL_FAIL_REPEAT == 0:
                    self.write("comm_still_failing", device=name, level="warn", state=state, error=err, exception_class=exc_class,
                               consecutive_failures=st["fails"], outage_sec=round(now - (st["fail_since"] or now)),
                               error_changed=err != st["last_error"], latency_ms=lat)
                    st["last_error"] = err
                self._maybe_snapshot(name, r, now, st, extra, offline=True)
                return
            # --- online ---
            up = _num(g("uptime_sec"))
            if st["online"] is False:
                outage = now - (st["fail_since"] or now)
                kept_running = None
                if up is not None:
                    kept_running = up > outage + 30.0
                self.write("comm_restored", device=name, level="info", consecutive_failures=st["fails"], outage_sec=round(outage),
                           first_error=st["first_error"], last_error=st["last_error"], latency_ms=lat, miner_uptime_sec=up,
                           miner_kept_running=kept_running,
                           verdict=("miner kept running; Miner Hearth lost contact (network / API)" if kept_running
                                    else ("miner rebooted during the outage" if kept_running is False else "unknown (no uptime reported)")))
            elif st["online"] is None:
                self.write("comm_first_ok", device=name, level="info", latency_ms=lat, miner_uptime_sec=up)
            st["online"] = True
            st["fails"] = 0
            st["fail_since"] = None
            st["last_ok"] = now
            if lat is not None and lat >= SLOW_POLL_MS and now - st["last_slow"] >= 600.0:
                st["last_slow"] = now
                self.write("poll_slow", device=name, level="warn", latency_ms=lat, note="API answered but slowly; close to the timeout.")
            # reboot detection (uptime going backwards)
            if up is not None:
                pu, pts = st["uptime"], st["uptime_ts"]
                if pu is not None and pts is not None:
                    elapsed = max(0.0, now - pts)
                    expected = pu + elapsed
                    # booted after we last saw it running (uptime went backwards vs. the wall clock)
                    if up + max(120.0, 0.01 * elapsed) < expected and (up < pu or elapsed > 600.0):
                        self.write("device_rebooted", device=name, level="warn", uptime_before_sec=round(pu), uptime_now_sec=round(up),
                                   last_uptime_seen=iso_local(pts), approx_boot_time=iso_local(now - up),
                                   seen_while_nk_running=bool(st["last_ok"] and pts >= now - 3600))
                st["uptime"], st["uptime_ts"] = up, now
            # pool switches / failover
            pool = _pool_sig(r)
            if st["pool"] is not None and pool != st["pool"]:
                self.write("pool_switch", device=name, level="warn", previous=st["pool"], current=pool)
            st["pool"] = pool
            fb = bool(g("using_fallback_stratum", False)) or any(
                isinstance(p, dict) and p.get("stratum_active") and str(p.get("pool")) not in {"0", "None", ""} for p in (g("pools") or []))
            if st["fallback"] is not None and fb != st["fallback"]:
                self.write("pool_failover" if fb else "pool_failback", device=name, level="warn" if fb else "info", current=pool)
            st["fallback"] = fb
            ps = _pool_states(r)
            if st["pool_states"] is not None and ps != st["pool_states"]:
                self.write("pool_status_change", device=name, level="info", previous=st["pool_states"], current=ps)
            st["pool_states"] = ps
            # session best reset
            sb = _num(g("best_session_diff"))
            if sb is not None:
                if st["session_best"] is not None and sb < st["session_best"]:
                    self.write("session_best_reset", device=name, level="info", previous=st["session_best"], current=sb)
                st["session_best"] = sb
            # power-limit / freq / voltage changes made outside NK show up here too
            for key, attr in (("power_limit", "power_limit_w"), ("freq", "frequency"), ("volt", "core_voltage")):
                v = _num(g(attr))
                if v is not None and st[key] is not None and abs(v - st[key]) > 1e-6:
                    self.write("setting_observed_change", device=name, level="info", field=attr, previous=st[key], current=v)
                if v is not None:
                    st[key] = v
            self._maybe_snapshot(name, r, now, st, extra, offline=False)

    def _fleet_check(self, name: str, now: float) -> None:
        down = sorted(n for n, s in self._dev.items() if s.get("online") is False and s.get("fail_since") and now - s["fail_since"] <= FLEET_WINDOW_SEC)
        if len(down) >= 2 and name in down:
            up = sorted(n for n, s in self._dev.items() if s.get("online") is True)
            self.write("multi_device_comm_loss", level="warn", devices=down, still_online=up,
                       note="Several machines lost contact within 90 s: look for a shared cause (switch, cable, router, this PC's network).")

    def _maybe_snapshot(self, name: str, r: Any, now: float, st: dict[str, Any], extra: dict[str, Any], offline: bool) -> None:
        interval = float(self.cfg["snapshot_min"]) * 60.0
        if st["last_snap"] and now - st["last_snap"] < interval:
            return
        st["last_snap"] = now
        lats = st["lat"]
        api = {"polls": st["polls_window"], "failed": st["fails_window"],
               "latency_ms": ({"min": round(min(lats), 1), "avg": round(sum(lats) / len(lats), 1), "max": round(max(lats), 1)} if lats else None)}
        st["lat"], st["polls_window"], st["fails_window"] = [], 0, 0
        if offline:
            self.write("snapshot", device=name, level="debug", online=False, api=api, consecutive_failures=st["fails"],
                       outage_sec=round(now - (st["fail_since"] or now)), last_error=st["last_error"], **extra)
        else:
            self.write("snapshot", device=name, level="debug", online=True, api=api, **reading_snapshot(r), **extra)
        devs = self._state.setdefault("devices", {})
        if isinstance(devs, dict) and st["uptime"] is not None:
            devs[name] = {"uptime": st["uptime"], "ts": st["uptime_ts"]}
            self._save_state()
        raw_iv = float(self.cfg["raw_snapshot_min"]) * 60.0
        if not offline and raw_iv > 0 and (not st["last_raw"] or now - st["last_raw"] >= raw_iv):
            st["last_raw"] = now
            self.write("raw_snapshot", device=name, level="debug", raw_fields=getattr(r, "raw_fields", None) or {},
                       fault_raw=getattr(r, "fault_raw", None) or {})


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

_CG_COUNTER_RX = re.compile(r"(?i)(get.?failures|remote.?failures|discarded|stale|rejected|accepted|hardware.?errors|device.?hardware|"
                            r"work.?utility|utility|diff1|getworks|pool.?rejected|pool.?stale|elapsed|found.?blocks|local.?work|network.?blocks)")


def _pool_sig(r: Any) -> dict[str, Any]:
    pools = [p for p in (getattr(r, "pools", None) or []) if isinstance(p, dict)]
    act = next((p for p in pools if p.get("stratum_active")), None)
    if act is not None:
        return {"index": act.get("pool"), "url": act.get("url")}
    fb = bool(getattr(r, "using_fallback_stratum", False))
    return {"index": "fallback" if fb else "primary",
            "url": (getattr(r, "fallback_pool_url", "") if fb else getattr(r, "pool_url", "")) or None}


def _pool_states(r: Any) -> list[str]:
    out = []
    for p in (getattr(r, "pools", None) or []):
        if isinstance(p, dict):
            out.append(f"{p.get('pool')}:{p.get('status') or '?'}{'*' if p.get('stratum_active') else ''}")
    return out


def reading_snapshot(r: Any) -> dict[str, Any]:
    g = lambda k, d=None: getattr(r, k, d)  # noqa: E731
    pools = []
    for p in (g("pools") or [])[:8]:
        if isinstance(p, dict):
            pools.append({k: p.get(k) for k in ("pool", "url", "status", "stratum_active", "priority", "accepted", "rejected", "stale",
                                                  "get_failures", "remote_failures", "best_share", "diff", "last_share_time", "user") if k in p})
    raw = g("raw_fields") or {}
    counters = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and _CG_COUNTER_RX.search(str(k)):
                counters[k] = v
                if len(counters) >= 60:
                    break
    return {
        "model": g("model"), "family": g("family"), "firmware": g("firmware") or g("version"), "source": g("source"),
        "uptime_sec": g("uptime_sec"),
        "hashrate_ghs": {"now": g("hashrate"), "1m": g("hashrate_1m"), "5m": g("hashrate_5m"), "15m": g("hashrate_15m"),
                         "1h": g("hashrate_1h"), "24h": g("hashrate_24h"), "avg": g("hashrate_avg"), "expected": g("expected_hashrate")},
        "power_w": g("power"), "power_limit_w": g("power_limit_w"), "input_voltage": g("input_voltage"), "input_current": g("input_current"),
        "temps_c": {"asic": g("asic_temp"), "asic_all": list(g("asic_temps") or []), "chip": list(g("chip_temps") or []),
                    "board": list(g("board_temps") or []), "vr": g("vr_temp"), "ambient": g("ambient_temp")},
        "fans": [dict(f) for f in (g("fans") or []) if isinstance(f, dict)] or list(g("fan_rpms") or []),
        "frequency_mhz": g("frequency"), "actual_frequency_mhz": g("actual_frequency"),
        "core_voltage_mv": g("core_voltage"), "core_voltage_actual_mv": g("core_voltage_actual"),
        "shares": {"accepted": g("shares_accepted"), "rejected": g("shares_rejected"), "stale": g("shares_stale"),
                   "rejected_pct": g("pool_rejected_pct"), "hw_errors": g("hw_errors"), "error_pct": g("error_percentage"),
                   "last_share_sec": g("last_share_seconds"), "reject_reasons": list(g("rejected_reasons") or [])[:10]},
        "best": {"all_time": g("best_diff"), "session": g("best_session_diff"), "pool_difficulty": g("pool_difficulty"),
                 "last_submitted": g("last_submitted_diff")},
        "pool": {"url": g("pool_url"), "port": g("pool_port"), "on_fallback": bool(g("using_fallback_stratum", False)),
                 "ping_ms": g("ping_rtt_ms"), "ping_loss": g("ping_loss"), "stratum_disconnects": g("stratum_disconnects")},
        "pools": pools,
        "wifi": {"rssi": g("wifi_rssi"), "disconnects": g("wifi_disconnects")} if g("wifi_rssi") is not None or g("wifi_disconnects") is not None else None,
        "boards": [dict(b) for b in (g("boards") or [])[:6] if isinstance(b, dict)],
        "tuner_mode": g("tuner_mode"), "tuner_chains": list(g("tuner_chain_statuses") or [])[:6],
        "cg_counters": counters or None,
    }


def settings_diff(old: Any, new: Any, path: str = "") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new), key=str):
            p = f"{path}.{k}" if path else str(k)
            if nk_faults.is_personal_key(str(k)):
                if old.get(k) != new.get(k):
                    out.append({"path": p, "old": "[redacted]", "new": "[redacted]"})
                continue
            out += settings_diff(old.get(k), new.get(k), p)
        return out
    if old != new:
        out.append({"path": path, "old": old, "new": new})
    return out


class EventLogHandler(logging.Handler):
    """Copies INFO+ application log lines into the event log, de-duplicating repeats (10 min per message)."""

    def __init__(self, elog: EventLog, level: int = logging.INFO):
        super().__init__(level)
        self.elog = elog
        self._seen: dict[tuple[str, str, str], list[float]] = {}
        self._busy = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "nk_eventlog_skip", False) or getattr(self._busy, "on", False):
            return
        if record.name.startswith("autotune.minerhearth.eventlog"):
            return
        self._busy.on = True
        try:
            tmpl = str(record.msg)
            first = str(record.args[0]) if isinstance(record.args, tuple) and record.args else ""
            key = (record.name, tmpl[:200], first[:80])
            now = record.created
            ent = self._seen.get(key)
            if ent is not None and now - ent[0] < LOG_DEDUP_SEC:
                ent[1] += 1
                return
            repeats = ent[1] if ent else 0
            self._seen[key] = [now, 0]
            if len(self._seen) > 2000:
                cutoff = now - LOG_DEDUP_SEC
                self._seen = {k: v for k, v in self._seen.items() if v[0] >= cutoff}
            try:
                msg = record.getMessage()
            except Exception:  # noqa: BLE001
                msg = tmpl
            data: dict[str, Any] = {"logger": record.name, "message": msg[:2000]}
            if repeats:
                data["suppressed_repeats"] = repeats
            if record.exc_info and record.exc_info[1] is not None:
                data["exception_class"] = type(record.exc_info[1]).__name__
                data["traceback"] = "".join(traceback.format_exception(*record.exc_info))[-6000:]
            lvl = {logging.WARNING: "warn", logging.ERROR: "error", logging.CRITICAL: "error"}.get(record.levelno, "info")
            dev = first if first and len(first) <= 64 and tmpl.startswith("%s:") else ""
            self.elog.write("log", device=dev, level=lvl, **data)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._busy.on = False
