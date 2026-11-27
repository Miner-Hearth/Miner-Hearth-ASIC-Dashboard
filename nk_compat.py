"""Miner Hearth v1.13: Compatibility capture & strict PII redaction (stdlib only).

Runs read-only probes across all known Tier-1 miner endpoints for a device or
arbitrary IP, redacts all sensitive/personal data, writes a shareable .zip into
userdata/compat-captures/, and performs a post-save leak scan of the zip.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

import nk_adapters

# ---------------------------------------------------------------------------
# Redaction patterns & key sets
# ---------------------------------------------------------------------------

PASSWORD_KEYS = frozenset({
    "password", "pw", "pass", "pwd", "stratum_password", "stratumpassword",
    "fallbackstratumpassword", "fallback_stratum_password", "wifipass",
    "wifi_pass", "wifi_password", "secret", "totp_secret", "bos_password",
    "web_password", "ap_pass",
})

TOKEN_KEYS = frozenset({
    "token", "jwt", "bearer", "otp_session", "session", "sessionid",
    "session_id", "api_key", "apikey", "auth_token", "access_token",
    "refresh_token",
})

COOKIE_KEYS = frozenset({
    "cookie", "set-cookie", "x-otp-session",
})

AUTH_HEADER_KEYS = frozenset({
    "authorization", "www-authenticate", "x-auth-token", "x-api-key", "x-totp",
})

POOL_USER_KEYS = frozenset({
    "stratumuser", "stratum_user", "fallbackstratumuser", "fallback_stratum_user",
    "pool_user", "fallback_pool_user", "user", "worker", "worker_name",
    "workername", "account", "stratum_account",
})

SSID_KEYS = frozenset({
    "ssid", "wifissid", "wifi_ssid", "apssid", "ap_ssid", "wlan_ssid",
})

MAC_KEYS = frozenset({
    "mac", "macaddr", "mac_addr", "mac_address", "ethaddr", "wlan_mac", "bssid",
})

SERIAL_KEYS = frozenset({
    "serial", "serial_number", "serialnumber", "sn", "minersn", "miner_sn",
    "chipid", "chip_id", "uid", "board_sn", "psu_sn", "dna", "device_id",
})

HOSTNAME_KEYS = frozenset({
    "hostname", "host_name", "device_name", "miner_name", "mdns_host", "friendly_name",
})

GENERIC_HOSTNAMES = frozenset({
    "localhost", "bitaxe", "nerdqaxe", "nerdoctaxe", "antminer", "avalon",
    "braiins", "vnish", "luxos", "apollo", "miner", "esp-miner", "axeos",
})

# Regexes for value-level detection (JSON strings, logs, Prometheus metrics, etc.)
RE_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}\b")
RE_AUTH_SCHEME = re.compile(r"\b(?:Bearer|Basic|Digest)\s+[A-Za-z0-9+/=._:,\" -]{8,}", re.IGNORECASE)
RE_URL_CREDS = re.compile(r"([a-zA-Z0-9+.-]+://)([^:@/\s]+):([^@/\s]+)@")
RE_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}\b")
RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
RE_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b")
RE_IPV6 = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b")

# Wallet regexes covering BTC, BCH, XEC, LTC, DOGE, DGB, KAS, CKB, ETC, ZEC, DASH, HNS, etc.
RE_WALLET_PREFIXED = re.compile(
    r"\b(?:bitcoincash|ecash|bchtest|kaspa):[qp][02-9ac-hj-np-z]{38,65}\b",
    re.IGNORECASE,
)
RE_WALLET_BECH32 = re.compile(
    r"\b(?:bc|tb|bcrt|ltc|tltc|dgb|dgbt|ckb|hs|zs)1[02-9ac-hj-np-z]{20,85}\b",
    re.IGNORECASE,
)
RE_WALLET_EVM = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
RE_WALLET_CASHADDR_RAW = re.compile(r"\b[qp][02-9ac-hj-np-z]{41}\b")
RE_WALLET_B58 = re.compile(r"\b(?:[13LMXD][a-km-zA-HJ-NP-Z1-9]{25,35}|t[13][a-km-zA-HJ-NP-Z1-9]{31,35})\b")
RE_LOG_SERIAL = re.compile(r"\b(?:SN|Serial(?:\s*No\.?)?|ChipID)\s*[:=]\s*([A-Za-z0-9_-]{6,})", re.IGNORECASE)
RE_LOG_SSID = re.compile(r"\b(?:SSID|Wi-?Fi\s*SSID)\s*[:=]\s*['\"]?([^'\"\r\n,;]{2,40})['\"]?", re.IGNORECASE)
RE_LOG_LOCAL_HOST = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9_-]{1,32}\.local)\b", re.IGNORECASE)


def _is_public_ipv4(ip_str: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip_str)
    except ValueError:
        return False
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
        return False
    if ip_str == "255.255.255.255":
        return False
    return True


def _is_public_ipv6(ip_str: str) -> bool:
    try:
        addr = ipaddress.IPv6Address(ip_str)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified)


def _bump(counts: dict[str, int], kind: str, n: int = 1) -> None:
    if n > 0:
        counts[kind] = counts.get(kind, 0) + n


def redact_string(text: str, counts: Optional[dict[str, int]] = None) -> str:
    """Scrub sensitive tokens/wallets/MACs/emails/public IPs/serials/SSIDs from free text."""
    if not text:
        return text
    c = counts if counts is not None else {}
    s = str(text)

    # URL embedded credentials user:pass@host
    def _url_cred_sub(m: re.Match[str]) -> str:
        _bump(c, "passwords")
        _bump(c, "pool_users")
        return f"{m.group(1)}[REDACTED_USER]:[REDACTED_PASSWORD]@"

    s = RE_URL_CREDS.sub(_url_cred_sub, s)

    def _sub(pattern: re.Pattern[str], repl: str, kind: str) -> None:
        nonlocal s
        s, n = pattern.subn(repl, s)
        _bump(c, kind, n)

    _sub(RE_JWT, "[REDACTED_TOKEN]", "tokens")
    _sub(RE_AUTH_SCHEME, "[REDACTED_AUTH]", "auth_headers")
    _sub(RE_EMAIL, "[REDACTED_EMAIL]", "emails")
    _sub(RE_MAC, "[REDACTED_MAC]", "mac_addresses")
    _sub(RE_WALLET_PREFIXED, "[REDACTED_WALLET]", "wallet_addresses")
    _sub(RE_WALLET_BECH32, "[REDACTED_WALLET]", "wallet_addresses")
    _sub(RE_WALLET_EVM, "[REDACTED_WALLET]", "wallet_addresses")
    _sub(RE_WALLET_CASHADDR_RAW, "[REDACTED_WALLET]", "wallet_addresses")
    _sub(RE_WALLET_B58, "[REDACTED_WALLET]", "wallet_addresses")

    def _serial_sub(m: re.Match[str]) -> str:
        _bump(c, "serial_numbers")
        prefix = m.group(0)[: m.group(0).find(m.group(1))]
        return f"{prefix}[REDACTED_SERIAL]"

    s = RE_LOG_SERIAL.sub(_serial_sub, s)

    def _ssid_sub(m: re.Match[str]) -> str:
        _bump(c, "ssids")
        prefix = m.group(0)[: m.group(0).find(m.group(1))]
        return f"{prefix}[REDACTED_SSID]"

    s = RE_LOG_SSID.sub(_ssid_sub, s)

    def _host_sub(m: re.Match[str]) -> str:
        if m.group(1).lower().rstrip(".local") in GENERIC_HOSTNAMES:
            return m.group(0)
        _bump(c, "personal_hostnames")
        return "[REDACTED_HOSTNAME]"

    s = RE_LOG_LOCAL_HOST.sub(_host_sub, s)

    def _ipv4_sub(m: re.Match[str]) -> str:
        ip_s = m.group(0)
        if _is_public_ipv4(ip_s):
            _bump(c, "public_ips")
            return "[REDACTED_PUBLIC_IP]"
        return ip_s

    s = RE_IPV4.sub(_ipv4_sub, s)

    def _ipv6_sub(m: re.Match[str]) -> str:
        ip_s = m.group(0)
        if _is_public_ipv6(ip_s):
            _bump(c, "public_ips")
            return "[REDACTED_PUBLIC_IP]"
        return ip_s

    s = RE_IPV6.sub(_ipv6_sub, s)
    return s


def redact_value(val: Any, key_hint: str = "", counts: Optional[dict[str, int]] = None) -> Any:
    """Recursively redact sensitive keys & values in dicts/lists/strings."""
    c = counts if counts is not None else {}
    k = str(key_hint or "").strip().lower()

    if isinstance(val, dict):
        out: dict[str, Any] = {}
        for dk, dv in val.items():
            dk_s = str(dk)
            dk_low = dk_s.strip().lower()
            # Keep key name intact, redact sensitive value
            if dk_low in PASSWORD_KEYS or "password" in dk_low or dk_low.endswith("_pw"):
                if dv not in (None, ""):
                    _bump(c, "passwords")
                    out[dk_s] = "[REDACTED_PASSWORD]"
                else:
                    out[dk_s] = dv
            elif dk_low in TOKEN_KEYS or dk_low.endswith("_token"):
                if dv not in (None, ""):
                    _bump(c, "tokens")
                    out[dk_s] = "[REDACTED_TOKEN]"
                else:
                    out[dk_s] = dv
            elif dk_low in COOKIE_KEYS:
                if dv not in (None, ""):
                    _bump(c, "cookies")
                    out[dk_s] = "[REDACTED_COOKIE]"
                else:
                    out[dk_s] = dv
            elif dk_low in AUTH_HEADER_KEYS:
                if dv not in (None, ""):
                    _bump(c, "auth_headers")
                    out[dk_s] = "[REDACTED_AUTH]"
                else:
                    out[dk_s] = dv
            elif dk_low in POOL_USER_KEYS:
                if dv not in (None, ""):
                    # Also detect if it contained a wallet address before worker suffix
                    s_dv = str(dv)
                    if RE_WALLET_PREFIXED.search(s_dv) or RE_WALLET_BECH32.search(s_dv) or RE_WALLET_EVM.search(s_dv) or RE_WALLET_B58.search(s_dv):
                        _bump(c, "wallet_addresses")
                    _bump(c, "pool_users")
                    out[dk_s] = "[REDACTED_POOL_USER]"
                else:
                    out[dk_s] = dv
            elif dk_low in SSID_KEYS or "ssid" in dk_low:
                if dv not in (None, ""):
                    _bump(c, "ssids")
                    out[dk_s] = "[REDACTED_SSID]"
                else:
                    out[dk_s] = dv
            elif dk_low in MAC_KEYS or dk_low.endswith("_mac"):
                if dv not in (None, ""):
                    _bump(c, "mac_addresses")
                    out[dk_s] = "[REDACTED_MAC]"
                else:
                    out[dk_s] = dv
            elif dk_low in SERIAL_KEYS or "serial" in dk_low:
                if dv not in (None, ""):
                    _bump(c, "serial_numbers")
                    out[dk_s] = "[REDACTED_SERIAL]"
                else:
                    out[dk_s] = dv
            elif dk_low in HOSTNAME_KEYS:
                s_dv = str(dv or "").strip()
                if s_dv and s_dv.lower() not in GENERIC_HOSTNAMES:
                    _bump(c, "personal_hostnames")
                    out[dk_s] = "[REDACTED_HOSTNAME]"
                else:
                    out[dk_s] = dv
            else:
                out[dk_s] = redact_value(dv, key_hint=dk_s, counts=c)
        return out

    if isinstance(val, list):
        return [redact_value(item, key_hint=k, counts=c) for item in val]

    if isinstance(val, str):
        return redact_string(val, counts=c)

    return val


def scan_text_for_leaks(text: str) -> list[dict[str, str]]:
    """Scan serialized capture text to verify no unredacted personal data remains."""
    leaks: list[dict[str, str]] = []
    if not text:
        return leaks
    checks: list[tuple[str, re.Pattern[str]]] = [
        ("tokens", RE_JWT),
        ("auth_headers", RE_AUTH_SCHEME),
        ("emails", RE_EMAIL),
        ("mac_addresses", RE_MAC),
        ("wallet_addresses", RE_WALLET_PREFIXED),
        ("wallet_addresses", RE_WALLET_BECH32),
        ("wallet_addresses", RE_WALLET_EVM),
        ("wallet_addresses", RE_WALLET_CASHADDR_RAW),
        ("wallet_addresses", RE_WALLET_B58),
    ]
    for kind, pat in checks:
        for m in pat.finditer(text):
            leaks.append({"type": kind, "sample": m.group(0)[:16] + "..."})
    for m in RE_URL_CREDS.finditer(text):
        leaks.append({"type": "passwords", "sample": m.group(1) + "***@"})
    for m in RE_IPV4.finditer(text):
        if _is_public_ipv4(m.group(0)):
            leaks.append({"type": "public_ips", "sample": m.group(0)})
    for m in RE_IPV6.finditer(text):
        if _is_public_ipv6(m.group(0)):
            leaks.append({"type": "public_ips", "sample": m.group(0)})
    return leaks


def verify_redacted_zip(zip_path: Path) -> dict[str, Any]:
    """Re-open saved capture zip and verify zero sensitive leaks remain in any entry."""
    leaks: list[dict[str, str]] = []
    file_count = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            file_count += 1
            raw = zf.read(name).decode("utf-8", errors="replace")
            for lk in scan_text_for_leaks(raw):
                leaks.append({"file": name, **lk})
    return {
        "after_scan_clean": len(leaks) == 0,
        "leak_count": len(leaks),
        "leaks": leaks[:20],
        "files_scanned": file_count,
    }


# ---------------------------------------------------------------------------
# Read-only Multi-API Probe Collector
# ---------------------------------------------------------------------------

CGMINER_READ_COMMANDS = (
    "version", "summary", "stats", "devs", "pools", "fans", "temps",
    "estats", "litestats", "tunerstatus", "config", "asccount", "coin", "profiles",
    "get_version", "get_miner_info", "get_psu", "get_error_code",
)

AXEOS_READ_PATHS = (
    "/api/system/info",
    "/api/system/asic",
    "/api/system/statistics",
    "/api/v2/dashboard",
    "/api/v2/system",
    "/api/v2/identify",
    "/api/system/logs",
)

VENDOR_HTTP_READ_PATHS = (
    "/api/v1/info",
    "/api/v1/summary",
    "/api/v1/status",
    "/api/v1/presets",
    "/api/v1/miner/log/errors",
)

BITMAIN_CGI_READ_PATHS = (
    "/cgi-bin/miner_type.cgi",
    "/cgi-bin/get_system_info.cgi",
    "/cgi-bin/summary.cgi",
    "/cgi-bin/stats.cgi",
    "/cgi-bin/pools.cgi",
    "/cgi-bin/get_kernel_log.cgi",
)

TIER2_HTTP_READ_PATHS = (
    "/mcb/status",
    "/mcb/pools",
    "/mcb/setting",
    "/cgi-bin/luci/getworkmode.cgi",
)


def collect_and_save_capture(
    ip: str,
    out_dir: Path,
    *,
    label: str = "",
    http_port: int = 80,
    cgminer_port: int = 4028,
    web_username: str = "",
    web_password: str = "",
    tcp_timeout: float = 1.2,
    http_timeout: float = 3.5,
    app_version: str = "1.15",
) -> dict[str, Any]:
    """Run read-only multi-protocol compatibility probes, redact, save zip, and scan zip."""
    host = str(ip or "").strip()
    if not host:
        raise ValueError("Target IP is required")
    if ":" in host and not host.startswith("["):
        h_part, p_part = host.rsplit(":", 1)
        if p_part.isdigit():
            host = h_part
            http_port = int(p_part)

    counts: dict[str, int] = {}
    files: dict[str, Any] = {}
    reachable_ports: list[int] = []

    # 1. CGMiner TCP :4028 read-only commands
    if nk_adapters.tcp_open(host, cgminer_port, tcp_timeout):
        reachable_ports.append(int(cgminer_port))
        cg_out: dict[str, Any] = {}
        for cmd in CGMINER_READ_COMMANDS:
            try:
                cg_out[cmd] = nk_adapters.cgminer_request(host, cgminer_port, cmd, timeout=http_timeout)
            except Exception as exc:
                cg_out[cmd] = {"_probe_error": str(exc)}
        files[f"cgminer_{cgminer_port}.json"] = redact_value(cg_out, counts=counts)

    # 2. HTTP :80 (AxeOS + VNish/Braiins REST + root headers + optional user-auth Bitmain CGI)
    if nk_adapters.tcp_open(host, http_port, tcp_timeout):
        reachable_ports.append(int(http_port))
        base = f"http://{host}" if int(http_port) == 80 else f"http://{host}:{http_port}"

        # Root banner headers (never full HTML body)
        try:
            st, body, rh = nk_adapters.http_request_raw("GET", f"{base}/", timeout=http_timeout)
            title_m = re.search(rb"<title[^>]*>(.*?)</title>", body[:4096], re.IGNORECASE | re.DOTALL)
            title = title_m.group(1).decode("utf-8", errors="replace").strip()[:120] if title_m else ""
            files["http_root.json"] = redact_value({"status": st, "headers": rh, "title": title}, counts=counts)
        except Exception as exc:
            files["http_root.json"] = {"_probe_error": str(exc)}

        # AxeOS endpoints
        axe_out: dict[str, Any] = {}
        for path in AXEOS_READ_PATHS:
            try:
                st, payload, rh = nk_adapters.http_request_raw("GET", f"{base}{path}", timeout=http_timeout)
                if st == 200 and payload:
                    txt = payload.decode("utf-8", errors="replace")
                    try:
                        parsed = json.loads(txt)
                    except Exception:
                        parsed = txt[:65536]
                    axe_out[path] = {"status": st, "body": parsed}
                else:
                    axe_out[path] = {"status": st}
            except Exception as exc:
                axe_out[path] = {"_probe_error": str(exc)}
        files["http_axeos.json"] = redact_value(axe_out, counts=counts)

        # Vendor /api/v1/* endpoints (VNish / Braiins REST)
        vend_out: dict[str, Any] = {}
        for path in VENDOR_HTTP_READ_PATHS:
            try:
                st, payload, rh = nk_adapters.http_request_raw("GET", f"{base}{path}", timeout=http_timeout)
                if st == 200 and payload:
                    txt = payload.decode("utf-8", errors="replace")
                    try:
                        parsed = json.loads(txt)
                    except Exception:
                        parsed = txt[:65536]
                    vend_out[path] = {"status": st, "body": parsed}
                else:
                    vend_out[path] = {"status": st}
            except Exception as exc:
                vend_out[path] = {"_probe_error": str(exc)}
        files["http_vendor_v1.json"] = redact_value(vend_out, counts=counts)

        # Bitmain /cgi-bin/*.cgi (ONLY uses user-supplied credentials, never defaults)
        bm_out: dict[str, Any] = {}
        for path in BITMAIN_CGI_READ_PATHS:
            try:
                st, payload, rh = nk_adapters.http_request_raw(
                    "GET",
                    f"{base}{path}",
                    timeout=http_timeout,
                    digest_user=web_username if web_username else "",
                    digest_pass=web_password if web_username else "",
                )
                if st == 200 and payload:
                    txt = payload.decode("utf-8", errors="replace")
                    try:
                        parsed = json.loads(txt)
                    except Exception:
                        parsed = txt[:65536]
                    bm_out[path] = {"status": st, "body": parsed}
                else:
                    bm_out[path] = {"status": st, "www_authenticate": rh.get("www-authenticate", "")}
            except Exception as exc:
                bm_out[path] = {"_probe_error": str(exc)}
        files["http_bitmain_cgi.json"] = redact_value(bm_out, counts=counts)

    # 3. LuxOS :8080/api read-only check
    if nk_adapters.tcp_open(host, 8080, tcp_timeout):
        reachable_ports.append(8080)
        lx_out: dict[str, Any] = {}
        for cmd in ("version", "summary", "profiles"):
            try:
                st, data, _, _ = nk_adapters.http_json("POST", f"http://{host}:8080/api", json_body={"command": cmd}, timeout=http_timeout)
                lx_out[cmd] = {"status": st, "body": data}
            except Exception as exc:
                lx_out[cmd] = {"_probe_error": str(exc)}
        files["http_luxos_8080.json"] = redact_value(lx_out, counts=counts)

    # 4. Prometheus :8081/metrics check
    if nk_adapters.tcp_open(host, 8081, tcp_timeout):
        reachable_ports.append(8081)
        try:
            st, body, _ = nk_adapters.http_request_raw("GET", f"http://{host}:8081/metrics", timeout=http_timeout)
            txt = body.decode("utf-8", errors="replace")[:131072] if body else ""
            files["prometheus_8081.txt"] = redact_string(txt, counts=counts)
        except Exception as exc:
            files["prometheus_8081.txt"] = f"# probe error: {exc}"

    # 5. Tier-2 probes (Goldshell /mcb/*, Elphapex getworkmode, IceRiver /user/userpanel post=4)
    if nk_adapters.tcp_open(host, http_port, tcp_timeout):
        base = f"http://{host}" if int(http_port) == 80 else f"http://{host}:{http_port}"
        t2_out: dict[str, Any] = {}
        for path in TIER2_HTTP_READ_PATHS:
            try:
                st, payload, rh = nk_adapters.http_request_raw(
                    "GET",
                    f"{base}{path}",
                    timeout=http_timeout,
                    digest_user=web_username if web_username else "",
                    digest_pass=web_password if web_username else "",
                    basic_auth=bool(web_username),
                )
                if st == 200 and payload:
                    txt = payload.decode("utf-8", errors="replace")
                    try:
                        parsed = json.loads(txt)
                    except Exception:
                        parsed = txt[:65536]
                    t2_out[path] = {"status": st, "body": parsed}
                else:
                    t2_out[path] = {"status": st}
            except Exception as exc:
                t2_out[path] = {"_probe_error": str(exc)}
        # IceRiver stock POST /user/userpanel (post=4)
        try:
            st_ir, pl_ir, _ = nk_adapters.http_request_raw(
                "POST", f"{base}/user/userpanel",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                body=b"post=4",
                timeout=http_timeout,
            )
            if st_ir == 200 and pl_ir:
                try:
                    t2_out["POST /user/userpanel"] = {"status": st_ir, "body": json.loads(pl_ir.decode("utf-8", errors="replace"))}
                except Exception:
                    t2_out["POST /user/userpanel"] = {"status": st_ir}
            else:
                t2_out["POST /user/userpanel"] = {"status": st_ir}
        except Exception as exc:
            t2_out["POST /user/userpanel"] = {"_probe_error": str(exc)}
        files["http_tier2.json"] = redact_value(t2_out, counts=counts)

    # 6. Whatsminer BTMiner v3 :4433 read-only probe
    if nk_adapters.tcp_open(host, 4433, tcp_timeout):
        reachable_ports.append(4433)
        wm_out: dict[str, Any] = {}
        for cmd, param in (("get.device.info", None), ("get.miner.status", "summary"), ("get.miner.status", "pools")):
            try:
                wm_out[f"{cmd}:{param or ''}"] = nk_adapters.whatsminer_v3_request(host, 4433, cmd, param, timeout=http_timeout)
            except Exception as exc:
                wm_out[f"{cmd}:{param or ''}"] = {"_probe_error": str(exc)}
        files["whatsminer_v3_4433.json"] = redact_value(wm_out, counts=counts)

    # 7. IceRiver-OC HTTPS :443 /api/overview check
    if nk_adapters.tcp_open(host, 443, tcp_timeout):
        reachable_ports.append(443)
        try:
            st443, d443, _, _ = nk_adapters.http_json("GET", f"https://{host}/api/overview", timeout=http_timeout, allow_insecure_tls=True)
            files["iceriver_oc_443.json"] = redact_value({"status": st443, "body": d443}, counts=counts)
        except Exception as exc:
            files["iceriver_oc_443.json"] = {"_probe_error": str(exc)}

    safe_lbl = re.sub(r"[^A-Za-z0-9._-]+", "_", (label or host)).strip("_") or "miner"
    ts_str = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_name = f"compat-{safe_lbl}-{ts_str}.zip"
    zip_path = out_dir / zip_name

    # Redact target IP in manifest if public
    safe_target = redact_string(host, counts=counts)
    manifest = {
        "app_version": app_version,
        "captured_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "target": safe_target,
        "label": redact_string(label or safe_lbl, counts=counts),
        "reachable_ports": reachable_ports,
        "redaction_summary": dict(counts),
        "total_redacted": sum(counts.values()),
    }
    files["manifest.json"] = manifest

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fname, content in files.items():
            if isinstance(content, str):
                zf.writestr(fname, content)
            else:
                zf.writestr(fname, json.dumps(content, indent=2, sort_keys=True))

    # Final verification scan of the written zip file
    scan_res = verify_redacted_zip(zip_path)
    if not scan_res["after_scan_clean"]:
        # Fail-safe: do not expose an unredacted archive
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"Compatibility capture blocked by post-save redaction scan: {scan_res['leaks'][:3]}")

    return {
        "ok": True,
        "filename": zip_name,
        "path": str(zip_path),
        "size_bytes": zip_path.stat().st_size,
        "reachable_ports": reachable_ports,
        "before_counts": dict(counts),
        "redacted_total": sum(counts.values()),
        "after_scan_clean": scan_res["after_scan_clean"],
        "files_scanned": scan_res["files_scanned"],
        "leak_count": scan_res["leak_count"],
    }
