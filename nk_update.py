"""Miner Hearth v1.16: live updates (opt-in, stdlib only).

The user points Miner Hearth at an update package (a .zip file they picked or a URL they typed). Nothing is
ever downloaded or applied on its own, and there is no built-in update server.

Package layout (see tools/make_update.py, which builds and signs one):

    manifest.json   {"format": "miner-hearth-update", "format_version": 1, "version": "1.16.1",
                     "min_app_version": "1.16.0", "max_app_version": "", "title": "...", "notes": "...",
                     "files": {"<path>": {"sha256": "<hex>", "size": <bytes>}, ...}}
    manifest.sig    optional: {"public_key": "<ed25519 hex>", "signature": "<hex>"} over manifest.json's exact bytes
    coins.json      coin data (hot reload)                 plugins/<name>.py   miner adapter plugins (hot reload)
    ui/<file>       dashboard CSS / JS / images (hot reload) app/<module>.py     program modules (quick restart)
    NOTES.md / CHANGES.md   shown to the user

Every file must be listed in the manifest with its SHA-256 and size, and nothing unlisted may be present.
A signature is checked against the user's trusted public keys; Settings can require one.
Applied packages live in userdata/updates/current; the one before is kept in userdata/updates/previous
for a one-click rollback.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import importlib.abc
import importlib.util
import io
import json
import logging
import os
import re
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

LOG = logging.getLogger("autotune")

FORMAT = "miner-hearth-update"
import nk_legacy  # noqa: E402

ACCEPTED_FORMATS = (FORMAT, nk_legacy.LEGACY_UPDATE_FORMAT)  # v1.17.0: packages made before the rename still read
FORMAT_VERSION = 1
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_FILES = 500
RESTART_STATE_MAX_AGE_SEC = 15 * 60
BOOT_ATTEMPTS_LIMIT = 2

_APP_MODULE = r"(?:autotune|dashboard|scanner|braiins_api|miner_hearth|nk_(?!update\b)[a-z0-9_]+)"
_PATH_RES = [
    (re.compile(rf"^app/{_APP_MODULE}\.py$"), "app"),
    (re.compile(r"^app/nk_adapters/(?!__init__\.py$)[a-z_][a-z0-9_]*\.py$"), "app"),
    (re.compile(r"^plugins/[a-z_][a-z0-9_]*\.py$"), "plugin"),
    (re.compile(r"^coins\.json$"), "coins"),
    (re.compile(r"^ui/[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}\.(?:css|js|html|png|svg|json|txt)$"), "ui"),
    (re.compile(r"^(?:NOTES|CHANGES)\.md$"), "notes"),
]
HOT_KINDS = {"plugin", "coins", "ui", "notes"}


class UpdateError(Exception):
    pass


# ---------------------------------------------------------------------------
# Ed25519 (RFC 8032), pure Python; only used for one manifest signature per package
# ---------------------------------------------------------------------------
_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


_D = -121665 * _inv(121666) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _add(P1: tuple, P2: tuple) -> tuple:
    A = (P1[1] - P1[0]) * (P2[1] - P2[0]) % _P
    B = (P1[1] + P1[0]) * (P2[1] + P2[0]) % _P
    C = 2 * P1[3] * P2[3] * _D % _P
    Dd = 2 * P1[2] * P2[2] % _P
    E, F, G, H = B - A, Dd - C, Dd + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _mul(s: int, P1: tuple) -> tuple:
    Qp = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Qp = _add(Qp, P1)
        P1 = _add(P1, P1)
        s >>= 1
    return Qp


def _eq(P1: tuple, P2: tuple) -> bool:
    return (P1[0] * P2[2] - P2[0] * P1[2]) % _P == 0 and (P1[1] * P2[2] - P2[1] * P1[2]) % _P == 0


def _recover_x(y: int, sign: int) -> Optional[int]:
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * _inv(5) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(P1: tuple) -> bytes:
    zi = _inv(P1[2])
    x, y = P1[0] * zi % _P, P1[1] * zi % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(b: bytes) -> Optional[tuple]:
    if len(b) != 32:
        return None
    y = int.from_bytes(b, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def _expand(secret: bytes) -> tuple[int, bytes]:
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def _h_modq(b: bytes) -> int:
    return int.from_bytes(hashlib.sha512(b).digest(), "little") % _Q


def ed25519_public_key(secret: bytes) -> bytes:
    return _compress(_mul(_expand(secret)[0], _G))


def ed25519_sign(secret: bytes, msg: bytes) -> bytes:
    a, prefix = _expand(secret)
    A = _compress(_mul(a, _G))
    r = _h_modq(prefix + msg)
    Rs = _compress(_mul(r, _G))
    s = (r + _h_modq(Rs + A + msg) * a) % _Q
    return Rs + int.to_bytes(s, 32, "little")


def ed25519_verify(public: bytes, msg: bytes, sig: bytes) -> bool:
    if len(public) != 32 or len(sig) != 64:
        return False
    A = _decompress(public)
    R = _decompress(sig[:32])
    if A is None or R is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _Q:
        return False
    h = _h_modq(sig[:32] + public + msg)
    return _eq(_mul(s, _G), _add(R, _mul(h, A)))


# ---------------------------------------------------------------------------
# Package verification
# ---------------------------------------------------------------------------

def version_tuple(v: Any) -> tuple[int, ...]:
    nums = re.findall(r"\d+", str(v or ""))[:4]
    return tuple(int(n) for n in nums) + (0,) * (4 - len(nums)) if nums else (0, 0, 0, 0)


def normalize_key(k: Any) -> str:
    k = re.sub(r"\s+", "", str(k or "")).lower()
    return k if re.fullmatch(r"[0-9a-f]{64}", k) else ""


def classify(path: str) -> Optional[str]:
    for rx, kind in _PATH_RES:
        if rx.match(path):
            return kind
    return None


_COIN_ID = re.compile(r"^[A-Z0-9]{2,10}$")
_COIN_FIELDS = {"label": str, "unit": str, "algo": str, "unit_kind": str, "explorer": str, "block_url": str,
                "spiky_difficulty": bool, "merged_with": str, "bases": list}


def validate_coins(data: Any, adapters: Optional[set[str]] = None) -> dict[str, Any]:
    """coins.json: {"coins": {ID: {...}}, "pool_host_hints": [[needle, ID]], "block_time_sec": {ID: s}, "heliospool_coins": {sub: ID}}."""
    if not isinstance(data, dict):
        raise UpdateError("coins.json must be a JSON object")
    out: dict[str, Any] = {"coins": {}, "pool_host_hints": [], "block_time_sec": {}, "heliospool_coins": {}}
    for cid, meta in (data.get("coins") or {}).items():
        if not _COIN_ID.match(str(cid)) or not isinstance(meta, dict):
            raise UpdateError(f"coins.json: bad coin entry {str(cid)[:20]!r}")
        m: dict[str, Any] = {}
        for k, v in meta.items():
            if k not in _COIN_FIELDS or not isinstance(v, _COIN_FIELDS[k]):
                raise UpdateError(f"coins.json: {cid}.{k} is not allowed or has the wrong type")
            if k == "bases":
                bases = []
                for b in v:
                    if not (isinstance(b, list) and len(b) == 2 and all(isinstance(x, str) for x in b) and re.match(r"^https?://", b[1])):
                        raise UpdateError(f"coins.json: {cid}.bases entries are [label, http(s) URL]")
                    bases.append((b[0], b[1].rstrip("/")))
                v = bases
            if k == "block_url" and not re.match(r"^https?://", v):
                raise UpdateError(f"coins.json: {cid}.block_url must be an http(s) URL")
            if k == "explorer" and adapters is not None and v not in adapters:
                raise UpdateError(f"coins.json: {cid}.explorer '{v}' is not a known explorer type")
            m[k] = v
        out["coins"][str(cid)] = m
    for h in data.get("pool_host_hints") or []:
        if not (isinstance(h, list) and len(h) == 2 and isinstance(h[0], str) and 2 <= len(h[0]) <= 40 and _COIN_ID.match(str(h[1]))):
            raise UpdateError("coins.json: pool_host_hints entries are [host substring, COIN]")
        out["pool_host_hints"].append((h[0].lower(), h[1]))
    for cid, sec in (data.get("block_time_sec") or {}).items():
        if not _COIN_ID.match(str(cid)) or not isinstance(sec, (int, float)) or not 0 < sec <= 86400:
            raise UpdateError("coins.json: block_time_sec values are seconds")
        out["block_time_sec"][str(cid)] = float(sec)
    for sub, cid in (data.get("heliospool_coins") or {}).items():
        if not re.fullmatch(r"[a-z0-9]{2,10}", str(sub)) or not _COIN_ID.match(str(cid)):
            raise UpdateError("coins.json: heliospool_coins maps a host prefix to a COIN")
        out["heliospool_coins"][str(sub)] = str(cid)
    return out


def _adapter_names() -> Optional[set[str]]:
    try:
        import nk_coins
        return set(getattr(nk_coins, "_ADAPTERS", {}) or {})
    except Exception:
        return None


def inspect_package(data: bytes, app_version: str, trusted_keys: Optional[list[str]] = None,
                    require_signature: bool = False, expected_sha256: str = "") -> dict[str, Any]:
    """Verify a package completely without executing or extracting anything. Raises UpdateError."""
    if not data or len(data) > MAX_PACKAGE_BYTES:
        raise UpdateError("The package is empty or larger than 64 MB.")
    pkg_sha = hashlib.sha256(data).hexdigest()
    exp = re.sub(r"\s+", "", str(expected_sha256 or "")).lower()
    if exp and exp != pkg_sha:
        raise UpdateError(f"Package SHA-256 does not match the one you entered (package is {pkg_sha}).")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UpdateError("Not a valid .zip update package.") from exc
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_FILES + 2:
        raise UpdateError("Too many files in the package.")
    if sum(i.file_size for i in infos) > MAX_PACKAGE_BYTES * 2:
        raise UpdateError("The package unpacks to more than 128 MB.")
    names = [i.filename for i in infos]
    if len(set(names)) != len(names):
        raise UpdateError("The package lists a file twice.")
    for i in infos:
        n = i.filename
        if n.startswith(("/", "\\")) or "\\" in n or ".." in n.split("/") or re.match(r"^[A-Za-z]:", n):
            raise UpdateError(f"Unsafe path in package: {n[:80]}")
        if (i.external_attr >> 16) & 0o170000 == 0o120000:
            raise UpdateError(f"Links are not allowed in a package: {n[:80]}")
    if "manifest.json" not in names:
        raise UpdateError("manifest.json is missing.")
    man_bytes = zf.read("manifest.json")
    try:
        man = json.loads(man_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("manifest.json is not valid JSON.") from exc
    if not isinstance(man, dict) or man.get("format") not in ACCEPTED_FORMATS or int(man.get("format_version") or 0) != FORMAT_VERSION:
        raise UpdateError("This is not a Miner Hearth update package (or a newer package format).")
    version = str(man.get("version") or "").strip()
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+){0,2}[A-Za-z0-9.+-]{0,20}", version):
        raise UpdateError("manifest.json: version is missing or invalid.")
    av = version_tuple(app_version)
    if man.get("min_app_version") and av < version_tuple(man["min_app_version"]):
        raise UpdateError(f"This update needs Miner Hearth {man['min_app_version']} or newer (this is {app_version}).")
    if man.get("max_app_version") and av > version_tuple(man["max_app_version"]):
        raise UpdateError(f"This update is for Miner Hearth up to {man['max_app_version']} (this is {app_version}).")
    files = man.get("files")
    if not isinstance(files, dict) or not files:
        raise UpdateError("manifest.json lists no files.")
    listed = set(files)
    present = set(names) - {"manifest.json", "manifest.sig"}
    if present - listed:
        raise UpdateError("Files not listed in the manifest: " + ", ".join(sorted(present - listed)[:5]))
    if listed - present:
        raise UpdateError("Files listed in the manifest are missing: " + ", ".join(sorted(listed - present)[:5]))
    kinds: dict[str, list[str]] = {}
    for path in sorted(listed):
        meta = files[path]
        kind = classify(path)
        if kind is None:
            raise UpdateError(f"This kind of file can't be updated: {path[:80]}")
        if not isinstance(meta, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(meta.get("sha256") or "")):
            raise UpdateError(f"manifest.json: no SHA-256 for {path}")
        body = zf.read(path)
        if hashlib.sha256(body).hexdigest() != meta["sha256"]:
            raise UpdateError(f"SHA-256 mismatch for {path}: the package is damaged or was changed.")
        if meta.get("size") is not None and int(meta["size"]) != len(body):
            raise UpdateError(f"Size mismatch for {path}.")
        if path.endswith(".py"):
            try:
                compile(body.decode("utf-8"), path, "exec")
            except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
                raise UpdateError(f"{path} is not valid Python: {exc}") from exc
        if kind == "coins":
            try:
                validate_coins(json.loads(body.decode("utf-8")), _adapter_names())
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise UpdateError("coins.json is not valid JSON.") from exc
        kinds.setdefault(kind, []).append(path)
    sig_state, signer = "unsigned", ""
    trusted = {normalize_key(k) for k in (trusted_keys or []) if normalize_key(k)}
    if "manifest.sig" in names:
        try:
            sig = json.loads(zf.read("manifest.sig").decode("utf-8"))
            pub = normalize_key(sig.get("public_key"))
            sigb = bytes.fromhex(re.sub(r"\s+", "", str(sig.get("signature") or "")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, AttributeError) as exc:
            raise UpdateError("manifest.sig is unreadable.") from exc
        if not pub or not ed25519_verify(bytes.fromhex(pub), man_bytes, sigb):
            raise UpdateError("The package signature is invalid: the package was changed after it was signed.")
        signer = pub
        sig_state = "trusted" if pub in trusted else "untrusted"
    if require_signature and sig_state != "trusted":
        raise UpdateError("Settings require a signature from one of your trusted keys, and this package "
                          + ("is not signed." if sig_state == "unsigned" else "is signed by a key you haven't trusted."))
    notes = str(man.get("notes") or "")
    for n in ("NOTES.md", "CHANGES.md"):
        if n in names and not notes:
            notes = zf.read(n).decode("utf-8", "replace")
    return {
        "version": version, "title": str(man.get("title") or "")[:200], "notes": notes[:4000],
        "min_app_version": str(man.get("min_app_version") or ""), "max_app_version": str(man.get("max_app_version") or ""),
        "package_sha256": pkg_sha, "size": len(data), "files": sorted(listed), "kinds": {k: v for k, v in kinds.items()},
        "needs_restart": "app" in kinds, "hot": sorted(k for k in kinds if k in HOT_KINDS),
        "signature": sig_state, "signer": signer,
    }


# ---------------------------------------------------------------------------
# Hot reload: coin data, adapter plugins (UI assets are served from disk on every request)
# ---------------------------------------------------------------------------
_BASE: dict[str, Any] = {}


def _snapshot_base() -> None:
    if _BASE:
        return
    import nk_coins
    import nk_poolfeed
    _BASE["COINS"] = copy.deepcopy(nk_coins.COINS)
    _BASE["HINTS"] = list(nk_coins.POOL_HOST_HINTS)
    _BASE["HELIOS"] = dict(nk_poolfeed.HELIOS_COINS)
    nkk = sys.modules.get("miner_hearth")
    if nkk is not None:
        _BASE["BLOCK_TIME"] = dict(nkk.BLOCK_TIME_SEC)


def apply_coins(overlay: Optional[dict[str, Any]]) -> list[str]:
    """Reset coin data to what shipped, then layer the update's coins.json on top (in place, so every module sees it)."""
    import nk_coins
    import nk_poolfeed
    _snapshot_base()
    nk_coins.COINS.clear()
    nk_coins.COINS.update(copy.deepcopy(_BASE["COINS"]))
    nk_coins.POOL_HOST_HINTS[:] = list(_BASE["HINTS"])
    nk_poolfeed.HELIOS_COINS.clear()
    nk_poolfeed.HELIOS_COINS.update(_BASE["HELIOS"])
    nkk = sys.modules.get("miner_hearth")
    if nkk is not None and "BLOCK_TIME" in _BASE:
        nkk.BLOCK_TIME_SEC.clear()
        nkk.BLOCK_TIME_SEC.update(_BASE["BLOCK_TIME"])
    changed: list[str] = []
    if overlay:
        for cid, meta in overlay["coins"].items():
            base = nk_coins.COINS.get(cid, {})
            nk_coins.COINS[cid] = dict(base, **meta)
            changed.append(cid)
        nk_coins.POOL_HOST_HINTS[:0] = list(overlay["pool_host_hints"])
        nk_poolfeed.HELIOS_COINS.update(overlay["heliospool_coins"])
        if nkk is not None:
            nkk.BLOCK_TIME_SEC.update(overlay["block_time_sec"])
    nk_coins.COIN_IDS = tuple(nk_coins.COINS)
    return changed


def load_plugins(plugin_dir: Optional[Path]) -> list[str]:
    """Import plugins/<name>.py; each may define ADAPTERS = [adapter instances]. Returns the family ids registered."""
    try:
        import nk_adapters
    except Exception:
        return []
    adapters: list[Any] = []
    if plugin_dir and plugin_dir.is_dir():
        for f in sorted(plugin_dir.glob("*.py")):
            name = f"nk_update_plugin_{f.stem}"
            spec = importlib.util.spec_from_file_location(name, f)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(mod)
            except Exception:
                LOG.exception("Update plugin %s failed to load; skipped", f.name)
                continue
            sys.modules[name] = mod
            for a in getattr(mod, "ADAPTERS", []) or []:
                if getattr(a, "family_id", "") and callable(getattr(a, "probe", None)):
                    adapters.append(a)
    if hasattr(nk_adapters, "set_plugin_adapters"):
        nk_adapters.set_plugin_adapters(adapters)
    return [a.family_id for a in adapters]


# ---------------------------------------------------------------------------
# Restart-required app modules: an import hook that prefers userdata/updates/current/app/*.py
# ---------------------------------------------------------------------------
class OverlayFinder(importlib.abc.MetaPathFinder):
    def __init__(self, app_dir: Path):
        self.app_dir = Path(app_dir)

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        parts = fullname.split(".")
        if len(parts) == 1:
            f = self.app_dir / f"{parts[0]}.py"
        elif len(parts) == 2 and parts[0] == "nk_adapters":
            f = self.app_dir / "nk_adapters" / f"{parts[1]}.py"
        else:
            return None
        if fullname == "nk_update" or not f.is_file():
            return None
        return importlib.util.spec_from_file_location(fullname, f)


def _read_state(ud: Path) -> dict[str, Any]:
    try:
        d = json.loads((ud / "updates" / "state.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(ud: Path, st: dict[str, Any]) -> None:
    p = ud / "updates" / "state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def install_overlay(userdata_dir: Path, app_version: str = "") -> dict[str, Any]:
    """Called at start-up before the program modules are imported. Crash guard: if the app modules of an update
    failed to start twice in a row, they are skipped (coin data / plugins / UI files still load) until changed.

    v1.17.0: program files of an update older than the program itself (e.g. left in userdata by a version
    installed before this one) are never loaded over the newer program."""
    ud = Path(userdata_dir)
    st = _read_state(ud)
    app = ud / "updates" / "current" / "app"
    info = {"enabled": bool(st.get("enabled")), "app_overlay": False, "skipped": ""}
    if not st.get("enabled") or not app.is_dir() or not any(app.rglob("*.py")):
        return info
    cur_v = str((st.get("current") or {}).get("version") or "")
    if app_version and (not cur_v or version_tuple(cur_v) < version_tuple(app_version)):
        info["skipped"] = f"the program files of update {cur_v or '?'} are older than this program ({app_version}); not loaded"
        return info
    boot = st.setdefault("boot", {})
    if boot.get("disabled_for") == (st.get("current") or {}).get("package_sha256"):
        info["skipped"] = "the program files of this update failed to start twice; skipped"
        return info
    boot["attempts"] = int(boot.get("attempts") or 0) + 1
    if boot["attempts"] > BOOT_ATTEMPTS_LIMIT:
        boot["disabled_for"] = (st.get("current") or {}).get("package_sha256")
        boot["attempts"] = 0
        _write_state(ud, st)
        info["skipped"] = "the program files of this update failed to start twice; skipped"
        return info
    _write_state(ud, st)
    sys.meta_path.insert(0, OverlayFinder(app))
    info["app_overlay"] = True
    return info


def mark_boot_ok(userdata_dir: Path) -> None:
    ud = Path(userdata_dir)
    st = _read_state(ud)
    if (st.get("boot") or {}).get("attempts"):
        st["boot"]["attempts"] = 0
        _write_state(ud, st)


# ---------------------------------------------------------------------------
# Quick restart: tuner state survives
# ---------------------------------------------------------------------------
def _jsonable(v: Any) -> Any:
    if isinstance(v, deque):
        return {"__deque__": [_jsonable(x) for x in v]}
    if isinstance(v, tuple):
        return [_jsonable(x) for x in v]
    if isinstance(v, (list,)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    raise TypeError


def save_restart_state(userdata_dir: Path, tuner: Any, now: Optional[float] = None) -> int:
    """Per-machine tuner state (step timers, stable frequency, error window, lockouts ...) for the next start."""
    devices: dict[str, Any] = {}
    for name, stt in (getattr(tuner, "states", {}) or {}).items():
        rec: dict[str, Any] = {}
        if dataclasses.is_dataclass(stt):
            for f in dataclasses.fields(stt):
                if f.name == "cfg":
                    continue
                try:
                    rec[f.name] = _jsonable(getattr(stt, f.name))
                except TypeError:
                    continue
        devices[str(name)] = rec
    p = Path(userdata_dir) / "restart_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"saved_ts": time.time() if now is None else now, "devices": devices}), encoding="utf-8")
    return len(devices)


def restore_restart_state(userdata_dir: Path, tuner: Any, now: Optional[float] = None) -> int:
    p = Path(userdata_dir) / "restart_state.json"
    if not p.exists():
        return 0
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    try:
        p.unlink()
    except OSError:
        pass
    now = time.time() if now is None else now
    if not isinstance(raw, dict) or now - float(raw.get("saved_ts") or 0) > RESTART_STATE_MAX_AGE_SEC:
        return 0
    n = 0
    for name, rec in (raw.get("devices") or {}).items():
        stt = (getattr(tuner, "states", {}) or {}).get(name)
        if stt is None or not isinstance(rec, dict):
            continue
        for k, v in rec.items():
            if not hasattr(stt, k) or k == "cfg":
                continue
            cur = getattr(stt, k)
            if isinstance(v, dict) and "__deque__" in v:
                items = [tuple(x) if isinstance(x, list) else x for x in v["__deque__"]]
                setattr(stt, k, deque(items, maxlen=cur.maxlen if isinstance(cur, deque) else None))
            elif isinstance(cur, deque):
                continue
            else:
                setattr(stt, k, v)
        n += 1
    return n


# ---------------------------------------------------------------------------
# Update manager (used by the dashboard routes)
# ---------------------------------------------------------------------------
def fetch_url(url: str, timeout: float = 30.0) -> bytes:
    u = str(url or "").strip()
    if not re.match(r"^https://[^\s/]+", u):
        raise UpdateError("Only https:// URLs can be used for updates (or pick a file).")
    req = urllib.request.Request(u, headers={"User-Agent": "MinerHearth-update/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:  # noqa: S310
            data = resp.read(MAX_PACKAGE_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError(f"Download failed: {exc}") from exc
    if len(data) > MAX_PACKAGE_BYTES:
        raise UpdateError("The download is larger than 64 MB.")
    return data


class UpdateManager:
    def __init__(self, userdata_dir: Optional[Path], app_version: str, fetch: Optional[Callable[[str], bytes]] = None,
                 clock: Callable[[], float] = time.time):
        self.ud = Path(userdata_dir) if userdata_dir else None
        self.app_version = app_version
        self.fetch = fetch or fetch_url
        self.clock = clock
        self.root = (self.ud / "updates") if self.ud else None
        self.boot_info: dict[str, Any] = {}
        self.hot: dict[str, Any] = {"coins": [], "plugins": [], "loaded_sha": ""}
        self.restart_pending = False

    # paths / state
    def _dir(self, name: str) -> Path:
        if self.root is None:
            raise UpdateError("No userdata folder: live updates are unavailable.")
        return self.root / name

    def state(self) -> dict[str, Any]:
        return _read_state(self.ud) if self.ud else {}

    def _save(self, st: dict[str, Any]) -> None:
        if self.ud:
            _write_state(self.ud, st)

    def set_enabled(self, enabled: bool) -> None:
        if not self.ud:
            return
        st = self.state()
        if bool(st.get("enabled")) != bool(enabled):
            st["enabled"] = bool(enabled)
            self._save(st)
            self.hot_reload(enabled)

    def status(self) -> dict[str, Any]:
        st = self.state()
        cur, prev = st.get("current"), st.get("previous")
        ui = []
        if st.get("enabled") and self.root and (self.root / "current" / "ui").is_dir():
            for f in sorted((self.root / "current" / "ui").iterdir()):
                if f.is_file():
                    ui.append({"name": f.name, "v": (cur or {}).get("package_sha256", "")[:12]})
        return {"enabled": bool(st.get("enabled")), "app_version": self.app_version, "current": cur, "previous": prev,
                "restart_pending": bool(self.restart_pending or st.get("restart_pending")), "boot": self.boot_info,
                "hot": {"coins": self.hot["coins"], "plugins": self.hot["plugins"]}, "ui_assets": ui,
                "staged": [p.stem for p in sorted(self._dir("incoming").glob("*.zip"))] if self.root and self._dir("incoming").is_dir() else []}

    # check -> apply
    def check(self, settings: dict[str, Any], data: Optional[bytes] = None, url: str = "", path: str = "",
              expected_sha256: str = "") -> dict[str, Any]:
        if data is None:
            if url:
                data = self.fetch(url)
            elif path:
                pth = Path(path.strip().strip('"'))
                if not pth.is_file() or pth.suffix.lower() != ".zip":
                    raise UpdateError("Pick an existing .zip update package.")
                if pth.stat().st_size > MAX_PACKAGE_BYTES:
                    raise UpdateError("The package is larger than 64 MB.")
                data = pth.read_bytes()
            else:
                raise UpdateError("Choose a package file or enter its URL.")
        rep = inspect_package(data, self.app_version, settings.get("trusted_keys") or [], bool(settings.get("require_signature")),
                              expected_sha256)
        inc = self._dir("incoming")
        inc.mkdir(parents=True, exist_ok=True)
        for old in inc.glob("*.zip"):
            old.unlink()
        (inc / f"{rep['package_sha256']}.zip").write_bytes(data)
        rep["token"] = rep["package_sha256"]
        rep["source"] = "url" if url else ("file" if path or data is not None else "")
        return rep

    def apply(self, settings: dict[str, Any], token: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{64}", str(token or "")):
            raise UpdateError("Check the package first.")
        src = self._dir("incoming") / f"{token}.zip"
        if not src.is_file():
            raise UpdateError("The checked package is gone; check it again.")
        data = src.read_bytes()
        rep = inspect_package(data, self.app_version, settings.get("trusted_keys") or [], bool(settings.get("require_signature")), token)
        stage = self._dir(f"staging-{int(self.clock() * 1000)}")
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for path in rep["files"]:
                dest = stage / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(path))
            (stage / "manifest.json").write_bytes(zf.read("manifest.json"))
        st = self.state()
        cur_dir, prev_dir = self._dir("current"), self._dir("previous")
        old_cur = st.get("current")
        if prev_dir.exists():
            shutil.rmtree(prev_dir)
        if cur_dir.exists():
            os.replace(cur_dir, prev_dir)
            st["previous"] = old_cur
        else:
            st["previous"] = None
        os.replace(stage, cur_dir)
        src.unlink()
        rec = {k: rep[k] for k in ("version", "title", "package_sha256", "kinds", "needs_restart", "signature", "signer")}
        rec["applied_ts"] = self.clock()
        st["current"] = rec
        st.setdefault("boot", {})["attempts"] = 0
        st["boot"].pop("disabled_for", None)
        restart = bool(rep["needs_restart"] or (old_cur or {}).get("needs_restart"))
        st["restart_pending"] = restart
        st["history"] = (st.get("history") or [])[-19:] + [{"action": "apply", "version": rep["version"], "ts": rec["applied_ts"],
                                                            "sha256": rep["package_sha256"]}]
        self._save(st)
        self.restart_pending = restart
        hot = self.hot_reload(bool(st.get("enabled")))
        return {"applied": rep["version"], "restart_required": restart, "hot_reloaded": hot, "report": rep}

    def rollback(self) -> dict[str, Any]:
        st = self.state()
        cur_dir, prev_dir = self._dir("current"), self._dir("previous")
        cur, prev = st.get("current"), st.get("previous")
        if not cur_dir.exists() and not prev_dir.exists():
            raise UpdateError("No update is applied, so there is nothing to roll back.")
        tmp = self._dir("rollback-tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        if cur_dir.exists():
            os.replace(cur_dir, tmp)
        if prev_dir.exists():
            os.replace(prev_dir, cur_dir)
        if tmp.exists():
            os.replace(tmp, prev_dir)
        st["current"], st["previous"] = prev, cur
        restart = bool((cur or {}).get("needs_restart") or (prev or {}).get("needs_restart"))
        st["restart_pending"] = restart
        st.setdefault("boot", {})["attempts"] = 0
        st["history"] = (st.get("history") or [])[-19:] + [{"action": "rollback", "to": (prev or {}).get("version") or "shipped", "ts": self.clock()}]
        self._save(st)
        self.restart_pending = restart
        hot = self.hot_reload(bool(st.get("enabled")))
        return {"now": (prev or {}).get("version") or f"{self.app_version} (as shipped)", "restart_required": restart, "hot_reloaded": hot}

    def remove_all(self) -> dict[str, Any]:
        st = self.state()
        restart = bool((st.get("current") or {}).get("needs_restart"))
        for name in ("current", "previous", "incoming"):
            d = self._dir(name)
            if d.exists():
                shutil.rmtree(d)
        st.update(current=None, previous=None, restart_pending=restart)
        st["history"] = (st.get("history") or [])[-19:] + [{"action": "remove_all", "ts": self.clock()}]
        self._save(st)
        self.restart_pending = restart
        hot = self.hot_reload(bool(st.get("enabled")))
        return {"now": f"{self.app_version} (as shipped)", "restart_required": restart, "hot_reloaded": hot}

    def hot_reload(self, enabled: Optional[bool] = None) -> dict[str, Any]:
        """Re-apply coin data and plugins from the current update (or the shipped data when off / none)."""
        if enabled is None:
            enabled = bool(self.state().get("enabled"))
        cur = self.root / "current" if self.root else None
        coins = None
        if enabled and cur and (cur / "coins.json").is_file():
            try:
                coins = validate_coins(json.loads((cur / "coins.json").read_text(encoding="utf-8")), _adapter_names())
            except (UpdateError, OSError, ValueError) as exc:
                LOG.warning("Update coin data ignored: %s", exc)
        try:
            self.hot["coins"] = apply_coins(coins)
        except Exception:
            LOG.exception("Could not apply update coin data")
        try:
            self.hot["plugins"] = load_plugins(cur / "plugins" if (enabled and cur) else None)
        except Exception:
            LOG.exception("Could not load update plugins")
        return {"coins": self.hot["coins"], "plugins": self.hot["plugins"],
                "ui": bool(enabled and cur and (cur / "ui").is_dir())}

    def ui_file(self, name: str) -> Optional[tuple[bytes, str]]:
        if not self.root or not self.state().get("enabled"):
            return None
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}\.(css|js|html|png|svg|json|txt)", name or ""):
            return None
        f = self.root / "current" / "ui" / name
        if not f.is_file():
            return None
        ctype = {"css": "text/css", "js": "application/javascript", "html": "text/html", "png": "image/png", "svg": "image/svg+xml",
                 "json": "application/json", "txt": "text/plain"}[name.rsplit(".", 1)[1]]
        return f.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/j")) else "")


# ---------------------------------------------------------------------------
# Building packages (tools/make_update.py wraps this)
# ---------------------------------------------------------------------------
def build_package(src_dir: Path, out_zip: Path, version: str, min_app_version: str = "", max_app_version: str = "",
                  title: str = "", notes: str = "", secret_key_hex: str = "") -> dict[str, Any]:
    src = Path(src_dir)
    files: dict[str, dict[str, Any]] = {}
    bodies: dict[str, bytes] = {}
    for f in sorted(src.rglob("*")):
        if not f.is_file() or "__pycache__" in f.parts:
            continue
        rel = f.relative_to(src).as_posix()
        if rel in ("manifest.json", "manifest.sig"):
            continue
        if classify(rel) is None:
            raise UpdateError(f"{rel}: not an updatable file (see nk_update.py for the allowed layout)")
        b = f.read_bytes()
        bodies[rel] = b
        files[rel] = {"sha256": hashlib.sha256(b).hexdigest(), "size": len(b)}
    if not files:
        raise UpdateError("Nothing to package.")
    man = {"format": FORMAT, "format_version": FORMAT_VERSION, "version": version, "min_app_version": min_app_version,
           "max_app_version": max_app_version, "title": title, "notes": notes, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "files": files}
    man_bytes = json.dumps(man, indent=2, sort_keys=True).encode("utf-8")
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", man_bytes)
        if secret_key_hex:
            sk = bytes.fromhex(secret_key_hex.strip())
            sig = {"public_key": ed25519_public_key(sk).hex(), "signature": ed25519_sign(sk, man_bytes).hex()}
            zf.writestr("manifest.sig", json.dumps(sig))
        for rel, b in bodies.items():
            zf.writestr(rel, b)
    return {"files": len(files), "sha256": hashlib.sha256(Path(out_zip).read_bytes()).hexdigest()}
