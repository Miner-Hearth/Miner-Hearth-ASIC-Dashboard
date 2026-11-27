"""Concurrent LAN scanner for Bitaxe Gamma, NerdOCTAxe/NerdQAxePlus, and Antminer S19 Pro."""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from autotune import (
    classify_axeos_payload,
    detect_antminer_firmware,
    http_request_ex,
    looks_like_axeos,
    parse_antminer_readings,
    parse_gamma_info,
    parse_nerd_v1_info,
    parse_nerd_v2_dashboard,
    query_cgminer,
    _json_body,
    _looks_like_html,
    DeviceOffline,
)


def detect_local_subnet() -> str:
    """Return the local /24 IPv4 subnet (e.g. '192.168.1.0/24') without external dependencies."""
    candidates: list[str] = []
    for target in ("192.168.1.1", "10.0.0.1", "8.8.8.8"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.3)
            sock.connect((target, 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                candidates.append(ip)
                break
        except OSError:
            pass
        finally:
            sock.close()
    if not candidates:
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                ip = info[4][0]
                if ip and not ip.startswith("127."):
                    candidates.append(ip)
                    break
        except OSError:
            pass
    for ip in candidates:
        try:
            addr = ipaddress.IPv4Address(ip)
            if addr.is_private:
                net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
                return str(net)
        except ValueError:
            continue
    return "192.168.1.0/24"


def expand_scan_targets(target: str) -> list[str]:
    """Expand a CIDR, IP range ('a.b.c.1-a.b.c.50'), or comma-separated host list into IPs."""
    raw = (target or "").strip()
    if not raw:
        raw = detect_local_subnet()
    ips: list[str] = []
    seen: set[str] = set()
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        if "/" in part:
            net = ipaddress.IPv4Network(part, strict=False)
            hosts = list(net.hosts()) or [net.network_address]
            for h in hosts:
                s = str(h)
                if s not in seen:
                    seen.add(s)
                    ips.append(s)
        elif "-" in part:
            left, right = [x.strip() for x in part.split("-", 1)]
            start_ip = ipaddress.IPv4Address(left)
            if "." not in right:
                prefix = ".".join(left.split(".")[:3])
                end_ip = ipaddress.IPv4Address(f"{prefix}.{right}")
            else:
                end_ip = ipaddress.IPv4Address(right)
            lo = min(int(start_ip), int(end_ip))
            hi = max(int(start_ip), int(end_ip))
            if hi - lo > 1024:
                raise ValueError("IP range too large (max 1024 hosts per scan)")
            for val in range(lo, hi + 1):
                s = str(ipaddress.IPv4Address(val))
                if s not in seen:
                    seen.add(s)
                    ips.append(s)
        else:
            ip_only = part.split(":")[0]
            ipaddress.IPv4Address(ip_only)
            if part not in seen:
                seen.add(part)
                ips.append(part)
    return ips


def _tcp_open(host: str, port: int, timeout: float) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _http_get(url: str, timeout: float) -> tuple[int, dict[str, str], bytes]:
    """GET with ESP32-friendly timeouts: no redirects, Connection: close, one retry."""
    try:
        status, payload, headers = http_request_ex("GET", url, timeout=timeout, retries=1)
        return status, headers, payload
    except DeviceOffline:
        return 0, {}, b""
    except Exception:
        return 0, {}, b""


def _parse_json(payload: bytes) -> Optional[dict[str, Any]]:
    data = _json_body(payload)
    return data if isinstance(data, dict) else None


def probe_host(
    target: str,
    http_port: int = 80,
    cgminer_port: int = 4028,
    tcp_timeout: float = 1.0,
    http_timeout: float = 4.0,
) -> Optional[dict[str, Any]]:
    """Probe one IP (or ip:port override) for AxeOS, NerdQAxePlus, or CGMiner Antminer."""
    host = target
    override_http_port = http_port
    override_cg_port = cgminer_port
    if ":" in target:
        h, p_str = target.rsplit(":", 1)
        host = h
        try:
            p_num = int(p_str)
            override_http_port = p_num
            override_cg_port = p_num
        except ValueError:
            pass

    last_octet = host.split(".")[-1]

    # 1. Check CGMiner TCP port (4028) for Antminer S19 Pro / ASICs.
    if _tcp_open(host, override_cg_port, tcp_timeout):
        try:
            ver_res = query_cgminer(host, override_cg_port, "version", timeout=http_timeout)
            sum_res = query_cgminer(host, override_cg_port, "summary", timeout=http_timeout)
            stats_res = query_cgminer(host, override_cg_port, "stats", timeout=http_timeout)
            pools_res = None
            try:
                pools_res = query_cgminer(host, override_cg_port, "pools", timeout=http_timeout)
            except Exception:
                pools_res = None
            import nk_adapters
            from nk_adapters.avalon import is_avalon_payload, parse_avalon_readings
            lite_res = None
            if not stats_res or "MM ID" not in str(stats_res):
                try:
                    lite_res = query_cgminer(host, override_cg_port, "litestats", timeout=http_timeout)
                except Exception:
                    lite_res = None
            from nk_adapters.whatsminer import is_whatsminer_payload, parse_whatsminer_v2
            from nk_adapters.sealminer import is_sealminer_payload, parse_sealminer_readings
            from nk_adapters.iceriver import is_iceriver_cgminer, infer_iceriver_algorithm
            if is_whatsminer_payload(ver_res, sum_res):
                wm = parse_whatsminer_v2(ver_res, sum_res, None, pools_res)
                hs = wm.get("hashrate_hs")
                return {
                    "ip": host,
                    "port": override_http_port,
                    "cgminer_port": override_cg_port,
                    "type": "antminer",
                    "family": "whatsminer",
                    "algorithm": "sha256d",
                    "tunable": False,
                    "model": wm["model"],
                    "firmware": wm["firmware"],
                    "firmware_note": "Monitor-only by default (opt-in BTMiner v3 :4433 controls)",
                    "version": wm["version"],
                    "hashrate_hs": hs,
                    "hashrate_unit_kind": "hash",
                    "hashrate_ghs": nk_adapters.hs_to_ghs(hs),
                    "asic_temp_c": wm["asic_temp_c"],
                    "fan_rpms": wm["fan_rpms"],
                    "pool_url": wm["pool_url"],
                    "suggested_name": f"whatsminer-{last_octet}",
                    "detection": f"BTMiner v2 :{override_cg_port}",
                    "capabilities": nk_adapters.get_adapter("whatsminer").capabilities.to_dict(),
                }
            if is_sealminer_payload(ver_res, sum_res, stats_res):
                sm = parse_sealminer_readings(ver_res, sum_res, None, stats_res, pools_res)
                hs = sm.get("hashrate_hs")
                return {
                    "ip": host,
                    "port": override_http_port,
                    "cgminer_port": override_cg_port,
                    "type": "antminer",
                    "family": "sealminer",
                    "algorithm": "sha256d",
                    "tunable": False,
                    "model": sm["model"],
                    "firmware": sm["firmware"],
                    "firmware_note": "Monitor-only BDMiner TCP :4028 (non-closing socket safe)",
                    "version": sm["version"],
                    "hashrate_hs": hs,
                    "hashrate_unit_kind": "hash",
                    "hashrate_ghs": nk_adapters.hs_to_ghs(hs),
                    "asic_temp_c": sm["asic_temp_c"],
                    "fan_rpms": sm["fan_rpms"],
                    "pool_url": sm["pool_url"],
                    "suggested_name": f"sealminer-{last_octet}",
                    "detection": f"BDMiner TCP :{override_cg_port}",
                    "capabilities": nk_adapters.get_adapter("sealminer").capabilities.to_dict(),
                }
            if is_iceriver_cgminer(ver_res, sum_res):
                reading_ir = parse_antminer_readings(ver_res, sum_res, stats_res, pools_res)
                algo_ir, _ = infer_iceriver_algorithm(reading_ir.model)
                hs = reading_ir.hashrate * 1e9 if reading_ir.hashrate is not None else None
                return {
                    "ip": host,
                    "port": override_http_port,
                    "cgminer_port": override_cg_port,
                    "type": "antminer",
                    "family": "iceriver",
                    "algorithm": algo_ir,
                    "tunable": False,
                    "model": reading_ir.model or "IceRiver-OC",
                    "firmware": "IceRiver-OC",
                    "firmware_note": "Monitor-only by default (opt-in IceRiver-OC :443 controls)",
                    "version": reading_ir.version,
                    "hashrate_hs": hs,
                    "hashrate_unit_kind": "hash",
                    "hashrate_ghs": reading_ir.hashrate,
                    "asic_temp_c": reading_ir.asic_temp,
                    "fan_rpms": reading_ir.fan_rpms,
                    "pool_url": reading_ir.pool_url,
                    "suggested_name": f"iceriver-{last_octet}",
                    "detection": f"IceRiver-OC CGMiner :{override_cg_port}",
                    "capabilities": nk_adapters.get_adapter("iceriver").capabilities.to_dict(),
                }
            if is_avalon_payload(ver_res, stats_res, lite_res):
                av = parse_avalon_readings(ver_res, sum_res, stats_res, lite_res, None, pools_res)
                hs = av.get("hashrate_hs")
                return {
                    "ip": host,
                    "port": override_cg_port,
                    "cgminer_port": override_cg_port,
                    "type": "antminer",
                    "family": "avalon",
                    "algorithm": "sha256d",
                    "tunable": False,
                    "model": av["model"],
                    "firmware": av["firmware"],
                    "firmware_note": "Monitor-only by default (opt-in Avalon ascset workmode/sleep/reboot)",
                    "version": av["version"],
                    "hashrate_hs": hs,
                    "hashrate_unit_kind": "hash",
                    "hashrate_ghs": nk_adapters.hs_to_ghs(hs),
                    "asic_temp_c": av["asic_temp_c"],
                    "fan_rpms": av["fan_rpms"],
                    "pool_url": av["pool_url"],
                    "suggested_name": f"avalon-{last_octet}",
                    "detection": f"Canaan Avalon CGMiner :{override_cg_port}",
                    "capabilities": nk_adapters.get_adapter("avalon").capabilities.to_dict(),
                }

            reading = parse_antminer_readings(ver_res, sum_res, stats_res, pools_res)
            fw, fw_note = detect_antminer_firmware(ver_res, stats_res)
            model = reading.model or "Antminer"
            fw_low = fw.lower()
            if "braiins" in fw_low:
                fam = "braiins"
            elif "vnish" in fw_low:
                fam = "vnish"
            elif "luxos" in fw_low:
                fam = "luxos"
            elif "apollo" in fw_low or "futurebit" in fw_low:
                fam = "apollo"
            elif any(k in f"{ver_res} {stats_res or ''}".lower() for k in ("bmminer", "antminer", "bitmain")):
                fam = "bitmain-stock"
            else:
                fam = "generic-cgminer"
            algo, _ = nk_adapters.infer_bitmain_algorithm(model)
            ukind = nk_adapters.unit_kind_for_algorithm(algo)
            hs = (reading.hashrate * 1e3) if (reading.hashrate is not None and ukind == "sol") else (reading.hashrate * 1e9 if reading.hashrate is not None else None)
            if fam == "apollo":
                suggested = f"apollo-{last_octet}"
            elif "bmm" in model.lower():
                suggested = f"braiins-bmm-{last_octet}"
            elif "s19" in model.lower():
                suggested = f"antminer-s19-{last_octet}"
            else:
                suggested = f"antminer-{last_octet}"
            adp = nk_adapters.get_adapter(fam)
            return {
                "ip": host,
                "port": override_cg_port,
                "cgminer_port": override_cg_port,
                "type": "antminer",
                "family": fam,
                "algorithm": algo,
                "tunable": False,
                "model": model,
                "firmware": fw,
                "firmware_note": fw_note,
                "version": reading.version,
                "hashrate_hs": hs,
                "hashrate_unit_kind": ukind,
                "hashrate_ghs": reading.hashrate,
                "asic_temp_c": reading.asic_temp,
                "fan_rpms": reading.fan_rpms,
                "pool_url": reading.pool_url,
                "suggested_name": suggested,
                "detection": f"CGMiner TCP :{override_cg_port}",
                "capabilities": adp.capabilities.to_dict() if adp else {},
            }
        except Exception:
            pass

    # 2. Check HTTP port (80) for AxeOS / NerdQAxePlus.
    # Prefer /api/system/info first: it exists on v1.0.37.x LTS (NerdOCTAxe) and
    # stock ESP-Miner. /api/v2/dashboard is only on later NerdQAxePlus builds;
    # v1 firmware 302-redirects those paths to a large HTML UI.
    if not _tcp_open(host, override_http_port, tcp_timeout):
        import nk_adapters
        for adp in (nk_adapters.WhatsminerAdapter(), nk_adapters.IceRiverAdapter(), nk_adapters.GenericPrometheusAdapter()):
            try:
                pr = adp.probe(host, http_port=override_http_port, cgminer_port=override_cg_port, tcp_timeout=tcp_timeout, http_timeout=http_timeout)
                if pr is not None:
                    return pr.to_scan_dict()
            except Exception:
                continue
        return None

    base = f"http://{host}" if override_http_port == 80 else f"http://{host}:{override_http_port}"

    def _axeos_hit(dtype: str, reading, model: str, firmware: str, detection: str) -> dict[str, Any]:
        import nk_adapters
        prefix = "nerdoctaxe" if dtype == "nerdoctaxe" else ("gamma" if dtype == "gamma" else "axeos")
        hs = (reading.hashrate * 1e9) if reading.hashrate is not None else None
        return {
            "ip": host,
            "port": override_http_port,
            "type": dtype,
            "family": "axeos",
            "algorithm": "sha256d",
            "tunable": True,
            "model": model,
            "firmware": firmware,
            "version": reading.version,
            "hashrate_hs": hs,
            "hashrate_unit_kind": "hash",
            "hashrate_ghs": reading.hashrate,
            "asic_temp_c": reading.asic_temp,
            "vr_temp_c": reading.vr_temp,
            "frequency_mhz": reading.frequency,
            "core_voltage_mv": reading.core_voltage,
            "suggested_name": f"{prefix}-{last_octet}",
            "detection": detection,
            "capabilities": nk_adapters.get_adapter("axeos").capabilities.to_dict(),
        }

    # 2a. Legacy / widely available /api/system/info
    st, hdrs, body = _http_get(f"{base}/api/system/info", timeout=http_timeout)
    if st == 200 and body and not _looks_like_html(body, hdrs):
        data = _parse_json(body)
        if data and looks_like_axeos(data):
            # Pull lightweight identity endpoints if this is a v2 firmware.
            ident = None
            ident_st, _, ident_body = _http_get(f"{base}/api/v2/identify", timeout=min(http_timeout, 3.0))
            if ident_st == 200:
                ident = _parse_json(ident_body)
            dtype = classify_axeos_payload(data, ident) or "axeos-generic"
            reading = parse_nerd_v1_info(data) if dtype != "gamma" else parse_gamma_info(data)
            model = (
                reading.model
                or (str(ident.get("deviceModel") or "") if ident else "")
                or str(data.get("deviceModel") or data.get("ASICModel") or data.get("asicModel") or "")
                or ("NerdOCTAxe" if dtype == "nerdoctaxe" else "AxeOS")
            )
            return _axeos_hit(
                dtype,
                reading,
                model,
                str(data.get("axeOSVersion") or data.get("version") or "AxeOS"),
                f"HTTP {base}/api/system/info",
            )

    # 2b. Newer NerdQAxePlus v2 dashboard (added after v1.0.37.x)
    st_v2, hdrs_v2, body_v2 = _http_get(f"{base}/api/v2/dashboard", timeout=http_timeout)
    if st_v2 == 200 and body_v2 and not _looks_like_html(body_v2, hdrs_v2):
        data = _parse_json(body_v2)
        if data and ("thermal" in data or "performance" in data):
            reading = parse_nerd_v2_dashboard(data)
            ident = None
            sysinfo = None
            ident_st, _, ident_body = _http_get(f"{base}/api/v2/identify", timeout=min(http_timeout, 3.0))
            if ident_st == 200:
                ident = _parse_json(ident_body)
            sys_st, _, sys_body = _http_get(f"{base}/api/v2/system", timeout=min(http_timeout, 3.0))
            if sys_st == 200:
                sysinfo = _parse_json(sys_body)
            dtype = classify_axeos_payload(data, ident, sysinfo) or "nerdoctaxe"
            model = (
                reading.model
                or (str(ident.get("deviceModel") or "") if ident else "")
                or (str(sysinfo.get("deviceModel") or "") if sysinfo else "")
                or "NerdQAxePlus"
            )
            return _axeos_hit(
                dtype,
                reading,
                model,
                "ESP-Miner-NerdQAxePlus v2",
                f"HTTP {base}/api/v2/dashboard",
            )

    # 2b-vnish. VNish HTTP /api/v1/info (when CGMiner :4028 is disabled)
    import nk_adapters
    st_vn, hdrs_vn, body_vn = _http_get(f"{base}/api/v1/info", timeout=http_timeout)
    if st_vn == 200 and body_vn and not _looks_like_html(body_vn, hdrs_vn):
        vn_info = _parse_json(body_vn)
        if vn_info and ("vnish" in str(vn_info).lower() or ("miner" in vn_info and "fw_version" in vn_info)):
            _, _, body_vs = _http_get(f"{base}/api/v1/summary", timeout=http_timeout)
            vn_sum = _parse_json(body_vs) if body_vs else None
            parsed = nk_adapters.VnishAdapter.parse_http_payloads(vn_info, vn_sum)
            hs = parsed.get("hashrate_hs")
            return {
                "ip": host,
                "port": override_http_port,
                "cgminer_port": override_cg_port,
                "type": "antminer",
                "family": "vnish",
                "algorithm": parsed.get("algorithm") or "sha256d",
                "tunable": False,
                "model": parsed["model"],
                "firmware": "VNish",
                "firmware_note": "Monitor-only by default (opt-in VNish /api/v1 tuning)",
                "version": parsed["version"],
                "hashrate_hs": hs,
                "hashrate_unit_kind": "hash",
                "hashrate_ghs": nk_adapters.hs_to_ghs(hs),
                "asic_temp_c": parsed.get("asic_temp_c"),
                "fan_rpms": parsed.get("fan_rpms") or [],
                "pool_url": parsed.get("pool_url") or "",
                "suggested_name": f"vnish-{last_octet}",
                "detection": f"HTTP {base}/api/v1/info",
                "capabilities": nk_adapters.get_adapter("vnish").capabilities.to_dict(),
            }

    # 2c. Tier-2 HTTP miners (Goldshell /mcb/status, Elphapex CGI/LuCI, IceRiver /user/userpanel)
    for adp in (nk_adapters.GoldshellAdapter(), nk_adapters.ElphapexAdapter(), nk_adapters.IceRiverAdapter()):
        try:
            pr = adp.probe(host, http_port=override_http_port, cgminer_port=override_cg_port, tcp_timeout=tcp_timeout, http_timeout=http_timeout)
            if pr is not None:
                return pr.to_scan_dict()
        except Exception:
            continue

    # 2d. Stock Bitmain Antminer HTTP :80 Digest Auth fallback
    st_root, hdrs_root, _ = _http_get(f"{base}/", timeout=http_timeout)
    www_auth = (hdrs_root.get("www-authenticate") or hdrs.get("www-authenticate") or "").lower()
    if (st_root == 401 or st == 401) and "digest" in www_auth:
        return {
            "ip": host,
            "port": override_cg_port,
            "cgminer_port": override_cg_port,
            "type": "antminer",
            "family": "bitmain-stock",
            "tunable": False,
            "model": "Antminer (HTTP Digest Auth)",
            "firmware": "Stock Bitmain (CGMiner 4028 closed)",
            "firmware_note": "Detected HTTP 401 Digest Auth on :80, but CGMiner TCP 4028 did not answer.",
            "suggested_name": f"antminer-{last_octet}",
            "detection": f"HTTP 401 Digest on :{override_http_port}",
        }

    # 2e. Tier-2 secondary ports (Whatsminer v3 :4433, Generic Prometheus :8081)
    for adp in (nk_adapters.WhatsminerAdapter(), nk_adapters.GenericPrometheusAdapter()):
        try:
            pr = adp.probe(host, http_port=override_http_port, cgminer_port=override_cg_port, tcp_timeout=tcp_timeout, http_timeout=http_timeout)
            if pr is not None:
                return pr.to_scan_dict()
        except Exception:
            continue

    return None


def _unique_name(base: str, used: set[str]) -> str:
    if base not in used:
        used.add(base)
        return base
    idx = 2
    while f"{base}-{idx}" in used:
        idx += 1
    candidate = f"{base}-{idx}"
    used.add(candidate)
    return candidate


def scan_network(
    subnet: str = "",
    configured_ips: Optional[set[str]] = None,
    existing_names: Optional[set[str]] = None,
    http_port: int = 80,
    cgminer_port: int = 4028,
    tcp_timeout: float = 1.0,
    http_timeout: float = 4.0,
    max_workers: int = 48,
) -> dict[str, Any]:
    """Scan a LAN subnet concurrently for supported miners."""
    resolved_subnet = (subnet or "").strip() or detect_local_subnet()
    targets = expand_scan_targets(resolved_subnet)
    configured = set(configured_ips or set())
    used_names = set(existing_names or set())
    found: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(targets)))) as pool:
        futures = {
            pool.submit(
                probe_host,
                target,
                http_port=http_port,
                cgminer_port=cgminer_port,
                tcp_timeout=tcp_timeout,
                http_timeout=http_timeout,
            ): target
            for target in targets
        }
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception:
                res = None
            if res is None:
                continue
            res["already_configured"] = res["ip"] in configured
            if not res["already_configured"]:
                res["suggested_name"] = _unique_name(res["suggested_name"], used_names)
            found.append(res)

    def _sort_key(item: dict[str, Any]) -> tuple[int, tuple[int, ...]]:
        try:
            octets = tuple(int(x) for x in item["ip"].split("."))
        except Exception:
            octets = (999, 999, 999, 999)
        return (1 if item.get("already_configured") else 0, octets)

    found.sort(key=_sort_key)
    return {
        "subnet": resolved_subnet,
        "hosts_scanned": len(targets),
        "found": found,
    }
