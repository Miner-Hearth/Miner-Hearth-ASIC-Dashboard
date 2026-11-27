"""Miner Hearth v1.16.2: saved pools library (stdlib only).

A list of stratum pools kept in userdata/saved_pools.json, so it survives updates and restarts and never ships
with the program (the library starts empty). Each entry: name, stratum URL (tcp or ssl) + port, user (usually
"payout-address.worker"; "{worker}" is replaced by the machine's worker name when applied), password (default
"x", stored locally, never sent to LAN clients) and an optional coin (auto-detected from the host when blank).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

FILE_NAME = "saved_pools.json"
FORMAT = "miner-hearth-pools"
MAX_POOLS = 200
WORKER_PLACEHOLDER = "{worker}"
DEFAULT_PASSWORD = "x"
_SCHEMES = {"stratum+tcp": "tcp", "stratum": "tcp", "tcp": "tcp", "stratum+ssl": "ssl", "stratum+tls": "ssl", "ssl": "ssl", "tls": "ssl"}
_HOST_RE = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
_IPV4_RE = re.compile(r"(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)")


class PoolError(ValueError):
    pass


def _coins():
    import nk_coins  # lazy: keeps this module importable on its own
    return nk_coins


def parse_stratum(url: Any, port: Any = None) -> tuple[str, str, int]:
    """'stratum+tcp://host:3333', 'stratum+ssl://host:4333', 'host:3333' or 'host' + port -> (scheme, host, port).
    scheme is 'tcp' or 'ssl'. Raises PoolError with a plain-English message."""
    u = str(url or "").strip()
    if not u:
        raise PoolError("Enter the pool's stratum address, e.g. stratum+tcp://pool.example.com:3333")
    if re.search(r"[\s,;\"'<>\\]", u):
        raise PoolError("The stratum address can't contain spaces, commas, quotes or backslashes")
    scheme = "tcp"
    m = re.match(r"^([a-z0-9+.-]+)://(.*)$", u, re.I)
    if m:
        scheme = _SCHEMES.get(m.group(1).lower(), "")
        if not scheme:
            raise PoolError(f"Unsupported scheme '{m.group(1)}://' (use stratum+tcp:// or stratum+ssl://)")
        u = m.group(2)
    u = u.rstrip("/")
    if "/" in u or "@" in u or "?" in u or "#" in u:
        raise PoolError("Use only host and port (no path, user@ or ?query in the stratum address)")
    host, p = u, None
    if u.startswith("["):  # [IPv6]:port
        mm = re.fullmatch(r"\[([0-9a-fA-F:]+)\](?::(\d+))?", u)
        if not mm:
            raise PoolError("Invalid IPv6 address")
        host, p = "[" + mm.group(1).lower() + "]", mm.group(2)
    elif ":" in u:
        host, p = u.rsplit(":", 1)
        if not p.isdigit():
            raise PoolError("The port after ':' must be a number")
    host = host.lower()
    if not host.startswith("[") and not (_IPV4_RE.fullmatch(host) or _HOST_RE.fullmatch(host)):
        raise PoolError(f"'{host}' is not a valid host name or IP address")
    if p is None and port not in (None, ""):
        p = port
    if p is None or str(p).strip() == "":
        raise PoolError("Add the port, e.g. pool.example.com:3333")
    try:
        pn = int(str(p).strip())
    except ValueError:
        raise PoolError("The port must be a number") from None
    if not 1 <= pn <= 65535:
        raise PoolError("The port must be between 1 and 65535")
    pf = str(port if port is not None else "").strip()
    if pf and ":" in u.split("]")[-1]:
        if not pf.isdigit():
            raise PoolError("The port must be a number")
        if int(pf) != pn:
            raise PoolError(f"The address says port {pn} but the port field says {pf}")
    return scheme, host, pn


def stratum_url(e: dict[str, Any]) -> str:
    return f"stratum+{e.get('scheme') or 'tcp'}://{e['host']}:{int(e['port'])}"


def device_url(e: dict[str, Any]) -> str:
    """URL as the existing pool editor expects it: a bare host for plain stratum (port separate), the scheme kept for SSL."""
    return e["host"] if (e.get("scheme") or "tcp") == "tcp" else f"stratum+ssl://{e['host']}"


def pool_match(url_a: str, port_a: Any, url_b: str, port_b: Any) -> bool:
    """Same stratum host / port / scheme (the pool editor may send a bare host with the port separately)."""
    try:
        return parse_stratum(url_a, port_a) == parse_stratum(url_b, port_b)
    except (PoolError, ValueError):
        return False


def machine_worker(name: Any) -> str:
    w = re.sub(r"[^A-Za-z0-9_-]+", "", str(name or ""))[:32]
    return w or "worker"


def apply_worker(user: str, machine: Optional[str] = None, use_machine_worker: bool = False) -> str:
    """Fill the '{worker}' placeholder; with use_machine_worker, the part after the first '.' becomes the machine's name."""
    u = str(user or "")
    if machine is None:
        return u
    w = machine_worker(machine)
    if WORKER_PLACEHOLDER in u:
        return u.replace(WORKER_PLACEHOLDER, w)
    if use_machine_worker:
        base = u.split(".", 1)[0]
        return f"{base}.{w}" if base else w
    return u


def _clean_text(v: Any, field: str, max_len: int, required: bool = False) -> str:
    s = str(v if v is not None else "").strip()
    if required and not s:
        raise PoolError(f"{field} is required")
    if len(s) > max_len:
        raise PoolError(f"{field} is too long (max {max_len} characters)")
    if re.search(r"[\x00-\x1f\x7f]", s):
        raise PoolError(f"{field} can't contain control characters")
    return s


def validate_entry(raw: dict[str, Any], keep_password: Optional[str] = None) -> dict[str, Any]:
    """A clean entry (without id/order) from user input; raises PoolError."""
    if not isinstance(raw, dict):
        raise PoolError("expected a pool object")
    scheme, host, port = parse_stratum(raw.get("url") or raw.get("stratum_url") or raw.get("host"), raw.get("port"))
    user = _clean_text(raw.get("user") if raw.get("user") is not None else raw.get("username"), "Username", 200, required=True)
    if re.search(r"[\s,]", user):
        raise PoolError("The username can't contain spaces or commas")
    if user.count(WORKER_PLACEHOLDER) > 1:
        raise PoolError("Use {worker} at most once in the username")
    pw_raw = raw.get("password")
    if pw_raw is None or str(pw_raw) == "":
        password = keep_password if keep_password is not None else DEFAULT_PASSWORD
    else:
        password = _clean_text(pw_raw, "Password", 200)
        if "," in password:
            raise PoolError("The password can't contain commas")
    name = _clean_text(raw.get("name") or raw.get("label") or "", "Name", 60) or host
    coin = _clean_text(raw.get("coin") or "", "Coin", 12).upper()
    nkc = _coins()
    if coin and coin not in nkc.COINS:
        raise PoolError(f"Unknown coin '{coin}'")
    return {"name": name, "scheme": scheme, "host": host, "port": port, "user": user, "password": password, "coin": coin}


def dedupe_key(e: dict[str, Any]) -> str:
    return f"{e.get('scheme') or 'tcp'}://{str(e.get('host') or '').lower()}:{int(e.get('port') or 0)}|{str(e.get('user') or '')}"


def mask(pw: str) -> str:
    return "•" * min(max(len(pw or ""), 1), 12)


class PoolLibrary:
    def __init__(self, userdata: Optional[Path]):
        self.path = (Path(userdata) / FILE_NAME) if userdata else None
        self._lock = threading.RLock()
        self.pools: list[dict[str, Any]] = []
        self.load_error = ""
        self._load()

    # --- storage -------------------------------------------------------------
    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = raw.get("pools") if isinstance(raw, dict) else raw
            out = []
            for it in items or []:
                try:
                    e = validate_entry(it)
                except PoolError:
                    continue
                e["id"] = str(it.get("id") or "") or self._new_id()
                out.append(e)
            self.pools = out[:MAX_POOLS]
        except (OSError, ValueError, AttributeError) as exc:
            self.load_error = f"could not read {FILE_NAME}: {exc}"

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps({"format": FORMAT, "version": 1, "pools": self.pools}, indent=1, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".saved_pools.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _new_id(self) -> str:
        ids = {p.get("id") for p in self.pools}
        while True:
            i = "p" + secrets.token_hex(4)
            if i not in ids:
                return i

    # --- read ----------------------------------------------------------------
    def public(self, e: dict[str, Any]) -> dict[str, Any]:
        """An entry for the dashboard: the password is masked, never sent."""
        nkc = _coins()
        auto = nkc.host_hint(e["host"]) or ""
        return {"id": e["id"], "name": e["name"], "scheme": e["scheme"], "host": e["host"], "port": e["port"],
                "url": stratum_url(e), "device_url": device_url(e), "user": e["user"],
                "has_worker_placeholder": WORKER_PLACEHOLDER in e["user"],
                "password_masked": mask(e.get("password") or ""), "password_is_default": (e.get("password") or "") == DEFAULT_PASSWORD,
                "coin": e.get("coin") or "", "coin_auto": auto, "coin_shown": e.get("coin") or auto or ""}

    def list_public(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self.public(e) for e in self.pools]

    def get(self, pool_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return next((dict(e) for e in self.pools if e["id"] == str(pool_id)), None)

    def find_dupe(self, e: dict[str, Any], exclude_id: str = "") -> Optional[dict[str, Any]]:
        k = dedupe_key(e)
        return next((p for p in self.pools if p["id"] != exclude_id and dedupe_key(p) == k), None)

    # --- write ---------------------------------------------------------------
    def add(self, raw: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            e = validate_entry(raw)
            d = self.find_dupe(e)
            if d is not None:
                raise PoolError(f"Already saved as \"{d['name']}\" (same stratum address and username)")
            if len(self.pools) >= MAX_POOLS:
                raise PoolError(f"The library is full ({MAX_POOLS} pools)")
            e["id"] = self._new_id()
            self.pools.append(e)
            self.save()
            return self.public(e)

    def update(self, pool_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            cur = next((p for p in self.pools if p["id"] == str(pool_id)), None)
            if cur is None:
                raise PoolError("That saved pool no longer exists")
            e = validate_entry(raw, keep_password=cur.get("password"))  # blank password = keep the saved one
            d = self.find_dupe(e, exclude_id=cur["id"])
            if d is not None:
                raise PoolError(f"Already saved as \"{d['name']}\" (same stratum address and username)")
            e["id"] = cur["id"]
            self.pools[self.pools.index(cur)] = e
            self.save()
            return self.public(e)

    def delete(self, pool_id: str) -> bool:
        with self._lock:
            n = len(self.pools)
            self.pools = [p for p in self.pools if p["id"] != str(pool_id)]
            if len(self.pools) != n:
                self.save()
                return True
            return False

    def move(self, pool_id: str, delta: int) -> list[str]:
        with self._lock:
            idx = next((i for i, p in enumerate(self.pools) if p["id"] == str(pool_id)), None)
            if idx is None:
                raise PoolError("That saved pool no longer exists")
            j = max(0, min(len(self.pools) - 1, idx + int(delta)))
            if j != idx:
                self.pools.insert(j, self.pools.pop(idx))
                self.save()
            return [p["id"] for p in self.pools]

    def reorder(self, ids: list[Any]) -> list[str]:
        with self._lock:
            by = {p["id"]: p for p in self.pools}
            seen, out = set(), []
            for i in ids or []:
                i = str(i)
                if i in by and i not in seen:
                    out.append(by[i])
                    seen.add(i)
            out += [p for p in self.pools if p["id"] not in seen]
            self.pools = out
            self.save()
            return [p["id"] for p in self.pools]

    # --- machines ------------------------------------------------------------
    def resolve(self, pool_id: str, machine: Optional[str] = None, use_machine_worker: bool = False) -> dict[str, Any]:
        """What the pool editor sends to a machine for this saved pool (password included: server side only)."""
        e = self.get(pool_id)
        if e is None:
            raise PoolError("That saved pool no longer exists")
        return {"id": e["id"], "name": e["name"], "url": device_url(e), "port": int(e["port"]), "scheme": e["scheme"],
                "user": apply_worker(e["user"], machine, use_machine_worker), "password": e.get("password") or DEFAULT_PASSWORD}

    @staticmethod
    def entry_from_device(snap: dict[str, Any], slot: Any) -> dict[str, Any]:
        """A new-entry dict from a machine's current pool as its adapter reported it.
        slot: 'primary', 'fallback', or a number = position in the CGMiner pool list (Antminer / Braiins)."""
        url, port, user = "", None, ""
        if slot in ("primary", "", None):
            url, port, user = snap.get("pool_url"), snap.get("pool_port"), snap.get("pool_user")
        elif slot == "fallback":
            url, port, user = snap.get("fallback_pool_url"), snap.get("fallback_pool_port"), snap.get("fallback_pool_user")
        else:
            try:
                i = int(slot)
            except (TypeError, ValueError):
                raise PoolError("unknown pool slot") from None
            pools = [p for p in (snap.get("pools") or []) if isinstance(p, dict)]
            if not 0 <= i < len(pools):
                raise PoolError("That pool is not in the machine's pool list any more")
            url, user = pools[i].get("url"), pools[i].get("user")
        if not str(url or "").strip():
            raise PoolError("The machine doesn't report this pool")
        if not str(user or "").strip():
            raise PoolError("The machine doesn't report a username for this pool")
        u = str(url).strip()
        if port not in (None, "") and not re.search(r":\d+/?$", re.sub(r"^[a-z0-9+.-]+://", "", u, flags=re.I)):
            u = f"{u}:{int(port)}"
        return {"url": u, "user": str(user).strip()}

    # --- export / import -----------------------------------------------------
    def export(self, include_passwords: bool = False) -> dict[str, Any]:
        with self._lock:
            items = []
            for e in self.pools:
                it = {"name": e["name"], "url": stratum_url(e), "user": e["user"], "coin": e.get("coin") or ""}
                if include_passwords:
                    it["password"] = e.get("password") or DEFAULT_PASSWORD
                items.append(it)
            return {"format": FORMAT, "version": 1, "pools": items}

    def import_(self, data: Any, replace: bool = False) -> dict[str, Any]:
        if isinstance(data, (bytes, str)):
            try:
                data = json.loads(data)
            except ValueError:
                raise PoolError("That file is not JSON") from None
        items = data.get("pools") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise PoolError("No pool list found in that file")
        with self._lock:
            if replace:
                self.pools = []
            added = skipped = 0
            errors: list[str] = []
            for it in items[: MAX_POOLS * 2]:
                try:
                    e = validate_entry(it)
                except PoolError as exc:
                    errors.append(str(exc))
                    continue
                if self.find_dupe(e) is not None or len(self.pools) >= MAX_POOLS:
                    skipped += 1
                    continue
                e["id"] = self._new_id()
                self.pools.append(e)
                added += 1
            self.save()
            return {"added": added, "skipped": skipped, "invalid": len(errors), "errors": errors[:5]}
