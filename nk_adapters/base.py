"""Miner Hearth v1.13 pluggable miner adapter base types & transport helpers (stdlib only)."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import base64
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any, Optional, Protocol


@dataclass(frozen=True)
class AdapterCapabilities:
    """Declared read/write capabilities for a miner family adapter."""

    monitor: bool = True
    faults: bool = False
    logs: bool = False
    restart: bool = False
    sleep_wake: bool = False
    workmode: bool = False
    power_target: bool = False
    freq_tuning: bool = False
    voltage_tuning: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "monitor": bool(self.monitor),
            "faults": bool(self.faults),
            "logs": bool(self.logs),
            "restart": bool(self.restart),
            "sleep_wake": bool(self.sleep_wake),
            "workmode": bool(self.workmode),
            "power_target": bool(self.power_target),
            "freq_tuning": bool(self.freq_tuning),
            "voltage_tuning": bool(self.voltage_tuning),
        }


@dataclass
class ProbeResult:
    """Result returned by an adapter's LAN discovery probe."""

    family: str
    device_type: str
    ip: str
    port: int = 80
    cgminer_port: int = 4028
    model: str = ""
    firmware: str = ""
    firmware_note: str = ""
    version: str = ""
    algorithm: str = "sha256d"
    hashrate_hs: Optional[float] = None
    hashrate_unit_kind: str = "hash"
    asic_temp_c: Optional[float] = None
    vr_temp_c: Optional[float] = None
    fan_rpms: list[int] = field(default_factory=list)
    pool_url: str = ""
    frequency_mhz: Optional[float] = None
    core_voltage_mv: Optional[float] = None
    tunable: bool = False
    auth_required: bool = False
    suggested_name: str = ""
    detection: str = ""
    capabilities: AdapterCapabilities = field(default_factory=AdapterCapabilities)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_scan_dict(self) -> dict[str, Any]:
        hr_ghs = hs_to_ghs(self.hashrate_hs) if self.hashrate_hs is not None else None
        out: dict[str, Any] = {
            "ip": self.ip,
            "port": self.port,
            "cgminer_port": self.cgminer_port,
            "type": self.device_type,
            "family": self.family,
            "algorithm": self.algorithm,
            "tunable": bool(self.tunable),
            "model": self.model,
            "firmware": self.firmware,
            "firmware_note": self.firmware_note,
            "version": self.version,
            "hashrate_hs": self.hashrate_hs,
            "hashrate_unit_kind": self.hashrate_unit_kind,
            "hashrate_ghs": hr_ghs,
            "asic_temp_c": self.asic_temp_c,
            "vr_temp_c": self.vr_temp_c,
            "fan_rpms": list(self.fan_rpms),
            "pool_url": self.pool_url,
            "frequency_mhz": self.frequency_mhz,
            "core_voltage_mv": self.core_voltage_mv,
            "auth_required": bool(self.auth_required),
            "suggested_name": self.suggested_name,
            "detection": self.detection,
            "capabilities": self.capabilities.to_dict(),
        }
        if self.extra:
            out.update(self.extra)
        return out


class MinerAdapter(Protocol):
    family_id: str
    display_name: str
    default_algorithms: tuple[str, ...]
    capabilities: AdapterCapabilities

    def probe(
        self,
        ip: str,
        http_port: int = 80,
        cgminer_port: int = 4028,
        tcp_timeout: float = 1.0,
        http_timeout: float = 4.0,
    ) -> Optional[ProbeResult]: ...


# ---------------------------------------------------------------------------
# Canonical Hashrate / Sol/s Unit Helpers
# ---------------------------------------------------------------------------

_UNIT_MULTIPLIERS: dict[str, float] = {
    "h/s": 1.0,
    "hs": 1.0,
    "kh/s": 1e3,
    "khs": 1e3,
    "mh/s": 1e6,
    "mhs": 1e6,
    "gh/s": 1e9,
    "ghs": 1e9,
    "th/s": 1e12,
    "ths": 1e12,
    "ph/s": 1e15,
    "phs": 1e15,
    "eh/s": 1e18,
    "ehs": 1e18,
    "sol/s": 1.0,
    "sols": 1.0,
    "ksol/s": 1e3,
    "ksols": 1e3,
    "msol/s": 1e6,
    "msols": 1e6,
    "gsol/s": 1e9,
    "gsols": 1e9,
}


def unit_kind_for_algorithm(algorithm: str) -> str:
    algo = str(algorithm or "sha256d").strip().lower()
    if algo in {"equihash", "zec", "sol"}:
        return "sol"
    return "hash"


def unit_multiplier(unit_str: str, algorithm: str = "sha256d") -> float:
    u = re.sub(r"\s+", "", str(unit_str or "")).lower()
    if u in _UNIT_MULTIPLIERS:
        return _UNIT_MULTIPLIERS[u]
    return 1e3 if unit_kind_for_algorithm(algorithm) == "sol" else 1e9


def to_hs(value: Any, unit_str: str = "GH/s", algorithm: str = "sha256d") -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    return v * unit_multiplier(unit_str, algorithm)


def hs_to_ghs(hs: Optional[float]) -> Optional[float]:
    if hs is None:
        return None
    return float(hs) / 1e9


def format_hashrate_hs(hs: Optional[float], unit_kind: str = "hash") -> str:
    if hs is None:
        return "—"
    v = float(hs)
    if v < 0:
        return "—"
    if unit_kind == "sol":
        units = [("GSol/s", 1e9), ("MSol/s", 1e6), ("kSol/s", 1e3), ("Sol/s", 1.0)]
        for lbl, scale in units:
            if v >= scale or scale == 1.0:
                scaled = v / scale
                dec = 2 if scaled < 100 else 1
                return f"{scaled:.{dec}f} {lbl}"
    units = [
        ("EH/s", 1e18),
        ("PH/s", 1e15),
        ("TH/s", 1e12),
        ("GH/s", 1e9),
        ("MH/s", 1e6),
        ("kH/s", 1e3),
        ("H/s", 1.0),
    ]
    for lbl, scale in units:
        if v >= scale or scale == 1.0:
            scaled = v / scale
            dec = 2 if scaled < 100 else 1
            return f"{scaled:.{dec}f} {lbl}"
    return f"{v:.1f} H/s"


# ---------------------------------------------------------------------------
# Transport Helpers (CGMiner TCP :4028 + HTTP JSON/Digest, stdlib only)
# ---------------------------------------------------------------------------

def tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def cgminer_request(
    ip: str,
    port: int,
    command: str,
    parameter: str = "",
    timeout: float = 4.0,
) -> dict[str, Any]:
    """Send JSON command to a CGMiner-compatible TCP port.

    Supports:
    - NUL-terminated responses (standard CGMiner/BMMiner/BOSminer)
    - EOF-closed responses
    - Non-closing newline/JSON sockets (e.g. BDMiner/Sealminer) without waiting for timeout.
    """
    req: dict[str, Any] = {"command": command}
    if parameter != "":
        req["parameter"] = parameter
    payload = (json.dumps(req) + "\n").encode("utf-8")
    chunks: list[bytes] = []
    parsed: Optional[dict[str, Any]] = None
    with socket.create_connection((ip, int(port)), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload)
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
            if b"\x00" in data:
                break
            if b"}" in data or chunks:
                candidate = b"".join(chunks).replace(b"\x00", b"").decode("utf-8", errors="replace").lstrip()
                if candidate.startswith("{"):
                    try:
                        obj, end_idx = json.JSONDecoder().raw_decode(candidate)
                        rest = candidate[end_idx:].strip()
                        if isinstance(obj, dict) and (not rest or rest.startswith("\n") or candidate.rstrip().endswith("}")):
                            parsed = obj
                            break
                    except json.JSONDecodeError:
                        pass
    if parsed is not None:
        return parsed
    raw = b"".join(chunks).replace(b"\x00", b"").decode("utf-8", errors="replace").strip()
    if not raw:
        raise ValueError(f"CGMiner {ip}:{port} {command} returned empty response")
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError(f"CGMiner {ip}:{port} {command} returned non-dict JSON")
    return obj


def _parse_digest_challenge(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not header or "digest " not in header.lower():
        return out
    body = header.split(" ", 1)[1] if " " in header else ""
    for k, v1, v2 in re.findall(r'([A-Za-z0-9_-]+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', body):
        out[k.lower()] = v1 if v1 != "" else v2
    return out


def _digest_auth_header(
    method: str,
    uri: str,
    username: str,
    password: str,
    challenge: dict[str, str],
) -> str:
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    qop = challenge.get("qop", "")
    opaque = challenge.get("opaque", "")
    alg = challenge.get("algorithm", "MD5").upper()
    hfn = hashlib.sha256 if "SHA-256" in alg else hashlib.md5

    def _h(s: str) -> str:
        return hfn(s.encode("utf-8")).hexdigest()

    ha1 = _h(f"{username}:{realm}:{password}")
    if alg.endswith("-SESS"):
        cnonce_init = os.urandom(8).hex()
        ha1 = _h(f"{ha1}:{nonce}:{cnonce_init}")
    ha2 = _h(f"{method.upper()}:{uri}")

    parts = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
    ]
    if qop:
        qop_pick = "auth" if "auth" in qop.split(",") else qop.split(",")[0].strip()
        nc = "00000001"
        cnonce = os.urandom(8).hex()
        resp = _h(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop_pick}:{ha2}")
        parts.extend([f'qop={qop_pick}', f'nc={nc}', f'cnonce="{cnonce}"', f'response="{resp}"'])
    else:
        resp = _h(f"{ha1}:{nonce}:{ha2}")
        parts.append(f'response="{resp}"')
    if opaque:
        parts.append(f'opaque="{opaque}"')
    if alg:
        parts.append(f"algorithm={alg}")
    return "Digest " + ", ".join(parts)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def http_request_raw(
    method: str,
    url: str,
    headers: Optional[dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: float = 4.0,
    digest_user: str = "",
    digest_pass: str = "",
    basic_auth: bool = False,
    allow_insecure_tls: bool = False,
) -> tuple[int, bytes, dict[str, str]]:
    """Stdlib HTTP(S) request with optional user-supplied Digest/Basic auth (never auto-default)."""
    hdrs = {"Connection": "close", "Accept": "application/json, text/plain, */*"}
    if basic_auth and digest_user:
        hdrs["Authorization"] = _basic_auth_header(digest_user, digest_pass)
    if headers:
        hdrs.update(headers)
    handlers: list[Any] = [_NoRedirect()]
    if allow_insecure_tls and url.lower().startswith("https://"):
        ctx = ssl._create_unverified_context()
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)

    def _once(extra_hdrs: Optional[dict[str, str]] = None) -> tuple[int, bytes, dict[str, str]]:
        h = dict(hdrs)
        if extra_hdrs:
            h.update(extra_hdrs)
        req = urllib.request.Request(url, data=body, headers=h, method=method.upper())
        try:
            with opener.open(req, timeout=timeout) as resp:
                rh = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, resp.read(), rh
        except urllib.error.HTTPError as err:
            rh = {k.lower(): v for k, v in err.headers.items()} if err.headers else {}
            try:
                eb = err.read()
            except Exception:
                eb = b""
            return err.code, eb, rh

    st, payload, rh = _once()
    if st == 401 and digest_user:
        www_l = (rh.get("www-authenticate") or "").lower()
        if "digest" in www_l:
            challenge = _parse_digest_challenge(rh.get("www-authenticate", ""))
            if challenge.get("nonce"):
                from urllib.parse import urlsplit
                sp = urlsplit(url)
                uri = sp.path + (f"?{sp.query}" if sp.query else "")
                auth_val = _digest_auth_header(method, uri or "/", digest_user, digest_pass, challenge)
                st, payload, rh = _once({"Authorization": auth_val})
        elif "basic" in www_l and not basic_auth:
            st, payload, rh = _once({"Authorization": _basic_auth_header(digest_user, digest_pass)})
    return st, payload, rh


def http_json(
    method: str,
    url: str,
    headers: Optional[dict[str, str]] = None,
    json_body: Any = None,
    timeout: float = 4.0,
    digest_user: str = "",
    digest_pass: str = "",
    basic_auth: bool = False,
    allow_insecure_tls: bool = False,
) -> tuple[int, Optional[Any], dict[str, str], bytes]:
    raw_body = None
    req_hdrs = dict(headers or {})
    if json_body is not None:
        raw_body = json.dumps(json_body).encode("utf-8")
        req_hdrs.setdefault("Content-Type", "application/json")
    st, payload, rh = http_request_raw(
        method, url, headers=req_hdrs, body=raw_body, timeout=timeout,
        digest_user=digest_user, digest_pass=digest_pass,
        basic_auth=basic_auth, allow_insecure_tls=allow_insecure_tls,
    )
    parsed = None
    if payload:
        try:
            parsed = json.loads(payload.decode("utf-8", errors="replace"))
        except Exception:
            parsed = None
    return st, parsed, rh, payload


def json_get_path(obj: Any, path: str) -> Any:
    """Extract a value from nested dicts/lists via dotted path (e.g. 'SUMMARY.0.GHS 5s')."""
    cur = obj
    p = str(path or "").strip()
    if not p:
        return None
    for seg in p.split("."):
        if isinstance(cur, dict):
            if seg in cur:
                cur = cur[seg]
            else:
                return None
        elif isinstance(cur, list):
            if seg.isdigit():
                idx = int(seg)
                if 0 <= idx < len(cur):
                    cur = cur[idx]
                else:
                    return None
            else:
                return None
        else:
            return None
    return cur


def num(d: Any, *keys: str) -> Optional[float]:
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k] is not None and not isinstance(d[k], bool):
            s = str(d[k]).strip()
            if s == "":
                continue
            try:
                return float(s)
            except ValueError:
                continue
    return None


def opt_int(d: Any, *keys: str) -> Optional[int]:
    v = num(d, *keys)
    return int(round(v)) if v is not None else None
