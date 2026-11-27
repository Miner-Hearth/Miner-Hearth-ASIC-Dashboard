"""Bitmain Antminer Stock Modern & Legacy adapter (S21/T21/S19*/L7/L9/KS/Z15/E9).

Supports CGMiner :4028 and user-supplied HTTP Digest Auth on :80 /cgi-bin/*.cgi.
NEVER tries vendor default credentials automatically.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .base import (
    AdapterCapabilities,
    ProbeResult,
    cgminer_request,
    http_json,
    http_request_raw,
    num,
    opt_int,
    tcp_open,
    to_hs,
    unit_kind_for_algorithm,
)


def infer_bitmain_algorithm(model_or_type: str, unit_hint: str = "") -> tuple[str, str]:
    """Infer (algorithm, default_rate_unit) for Bitmain stock ASICs."""
    m = str(model_or_type or "").lower()
    u = str(unit_hint or "").lower()
    if any(k in m for k in ("l7", "l9", "l3", "ltc", "scrypt")):
        return "scrypt", "MH/s"
    if any(k in m for k in ("ks3", "ks5", "ks7", "kas", "kheavy")):
        return "kheavyhash", "GH/s"
    if any(k in m for k in ("z15", "z11", "equihash")) or "sol" in u:
        return "equihash", "kSol/s"
    if any(k in m for k in ("e9", "e11", "etchash", "ethash")):
        return "etchash", "MH/s"
    if any(k in m for k in ("d7", "d9", "x11")):
        return "x11", "GH/s"
    if any(k in m for k in ("ka3", "k7", "eaglesong", "ckb")):
        return "eaglesong", "TH/s"
    if any(k in m for k in ("hs3", "hs5", "handshake")):
        return "handshake", "TH/s"
    return "sha256d", "GH/s"


def parse_bitmain_cgi_stats(
    miner_type: Optional[dict[str, Any]],
    sysinfo: Optional[dict[str, Any]],
    summary_cgi: Optional[dict[str, Any]],
    stats_cgi: Optional[dict[str, Any]],
    pools_cgi: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Normalize Bitmain modern/legacy HTTP /cgi-bin/*.cgi payloads."""
    mt = miner_type if isinstance(miner_type, dict) else {}
    si = sysinfo if isinstance(sysinfo, dict) else {}
    sc = summary_cgi if isinstance(summary_cgi, dict) else {}
    stc = stats_cgi if isinstance(stats_cgi, dict) else {}

    model = str(
        mt.get("miner_type")
        or si.get("minertype")
        or si.get("miner_type")
        or mt.get("type")
        or "Bitmain Antminer"
    )

    # Extract summary item from modern summary.cgi or stats.cgi
    sum_list = sc.get("SUMMARY") if isinstance(sc.get("SUMMARY"), list) else []
    s0 = sum_list[0] if sum_list and isinstance(sum_list[0], dict) else {}
    st_list = stc.get("STATS") if isinstance(stc.get("STATS"), list) else []
    st0 = st_list[0] if st_list and isinstance(st_list[0], dict) else {}

    rate_unit = str(s0.get("rate_unit") or st0.get("rate_unit") or "")
    algo, default_unit = infer_bitmain_algorithm(model, rate_unit)
    effective_unit = rate_unit or default_unit

    hr_raw = num(s0, "rate_5s", "rate_avg", "GHS 5s", "MHS 5s")
    if hr_raw is None:
        hr_raw = num(st0, "rate_5s", "rate_avg", "GHS 5s", "MHS 5s")
        if hr_raw is not None and not rate_unit and "MHS 5s" in st0:
            effective_unit = "MH/s"
    elif not rate_unit and "MHS 5s" in s0:
        effective_unit = "MH/s"

    hs = to_hs(hr_raw, effective_unit, algo)
    ukind = unit_kind_for_algorithm(algo)

    chip_temps: list[float] = []
    board_temps: list[float] = []
    chains = st0.get("chain") if isinstance(st0.get("chain"), list) else []
    for ch in chains:
        if not isinstance(ch, dict):
            continue
        for k in ("temp_chip", "temp_pic"):
            arr = ch.get(k)
            if isinstance(arr, list):
                chip_temps.extend(float(x) for x in arr if isinstance(x, (int, float)) and 0 < x < 150)
        for k in ("temp_pcb",):
            arr = ch.get(k)
            if isinstance(arr, list):
                board_temps.extend(float(x) for x in arr if isinstance(x, (int, float)) and 0 < x < 150)

    fan_rpms: list[int] = []
    if isinstance(st0.get("fan"), list):
        fan_rpms = [int(round(float(x))) for x in st0["fan"] if isinstance(x, (int, float)) and x >= 0]
    else:
        for fk in ("fan1", "fan2", "fan3", "fan4"):
            fv = opt_int(st0, fk)
            if fv is not None and fv >= 0:
                fan_rpms.append(fv)

    pools: list[dict[str, Any]] = []
    pc = pools_cgi if isinstance(pools_cgi, dict) else {}
    plist = pc.get("POOLS") if isinstance(pc.get("POOLS"), list) else []
    for idx, p in enumerate(plist):
        if isinstance(p, dict):
            pools.append({
                "pool": idx,
                "url": str(p.get("url") or p.get("URL") or ""),
                "user": str(p.get("user") or p.get("User") or ""),
                "status": str(p.get("status") or p.get("Status") or ""),
                "accepted": opt_int(p, "accepted", "Accepted") or 0,
                "rejected": opt_int(p, "rejected", "Rejected") or 0,
            })

    return {
        "model": model,
        "firmware": "Stock Bitmain",
        "version": str(si.get("firmware_version") or si.get("system_filesystem_version") or ""),
        "algorithm": algo,
        "hashrate_hs": hs,
        "hashrate_unit_kind": ukind,
        "asic_temp_c": max(chip_temps) if chip_temps else (max(board_temps) if board_temps else None),
        "chip_temps": chip_temps,
        "board_temps": board_temps,
        "fan_rpms": fan_rpms,
        "uptime_sec": num(s0, "elapsed") or num(st0, "elapsed"),
        "pool_url": pools[0]["url"] if pools else "",
        "pool_user": pools[0]["user"] if pools else "",
        "pools": pools,
        "is_legacy": not bool(sc),
    }


class BitmainStockAdapter:
    family_id = "bitmain-stock"
    display_name = "Bitmain Antminer Stock (S21/T21/S19/L7/L9/KS/Z15/E9)"
    default_algorithms = ("sha256d", "scrypt", "kheavyhash", "equihash", "etchash")
    capabilities = AdapterCapabilities(
        monitor=True,
        faults=True,
        logs=True,
        restart=True,
        sleep_wake=True,
        workmode=True,
    )

    def probe(
        self,
        ip: str,
        http_port: int = 80,
        cgminer_port: int = 4028,
        tcp_timeout: float = 1.0,
        http_timeout: float = 4.0,
    ) -> Optional[ProbeResult]:
        last_octet = ip.split(".")[-1]
        if tcp_open(ip, cgminer_port, tcp_timeout):
            try:
                ver = cgminer_request(ip, cgminer_port, "version", timeout=http_timeout)
                summ = cgminer_request(ip, cgminer_port, "summary", timeout=http_timeout)
                stats = None
                try:
                    stats = cgminer_request(ip, cgminer_port, "stats", timeout=http_timeout)
                except Exception:
                    pass
                v0 = (ver.get("VERSION") or [{}])[0] if isinstance(ver.get("VERSION"), list) else {}
                s0 = (summ.get("SUMMARY") or [{}])[0] if isinstance(summ.get("SUMMARY"), list) else {}
                hay_b = f"{ver} {stats or ''}".lower()
                if not any(k in hay_b for k in ("bmminer", "antminer", "bitmain")):
                    raise ValueError("not bitmain-stock")
                model = str(v0.get("Type") or v0.get("Miner") or "Antminer")
                algo, def_unit = infer_bitmain_algorithm(model)
                ghs = num(s0, "GHS 5s", "GHS av")
                mhs = num(s0, "MHS 5s", "MHS av")
                if algo == "equihash":
                    hs = to_hs(ghs if ghs is not None else mhs, "kSol/s", algo)
                elif mhs is not None and ghs is None:
                    hs = to_hs(mhs, "MH/s", algo)
                else:
                    hs = to_hs(ghs, "GH/s" if def_unit == "kSol/s" else def_unit, algo)
                return ProbeResult(
                    family="bitmain-stock",
                    device_type="antminer",
                    ip=ip,
                    port=int(cgminer_port),
                    cgminer_port=int(cgminer_port),
                    model=model,
                    firmware="Stock Bitmain",
                    firmware_note="Monitor-only by default (user-supplied HTTP Digest creds unlock logs/workmode/reboot)",
                    version=str(v0.get("BMMiner") or v0.get("CGMiner") or ""),
                    algorithm=algo,
                    hashrate_hs=hs,
                    hashrate_unit_kind=unit_kind_for_algorithm(algo),
                    suggested_name=f"antminer-{last_octet}",
                    detection=f"Bitmain CGMiner :{cgminer_port}",
                    capabilities=self.capabilities,
                )
            except Exception:
                pass

        if tcp_open(ip, http_port, tcp_timeout):
            base = f"http://{ip}" if int(http_port) == 80 else f"http://{ip}:{http_port}"
            st, _, rh = http_request_raw("GET", f"{base}/", timeout=http_timeout)
            if st == 401 and "digest" in (rh.get("www-authenticate") or "").lower():
                return ProbeResult(
                    family="bitmain-stock",
                    device_type="antminer",
                    ip=ip,
                    port=int(cgminer_port),
                    cgminer_port=int(cgminer_port),
                    model="Antminer (HTTP Digest Auth)",
                    firmware="Stock Bitmain (CGMiner 4028 closed)",
                    firmware_note="Detected HTTP 401 Digest Auth on :80; enter web credentials to read/control",
                    algorithm="sha256d",
                    auth_required=True,
                    suggested_name=f"antminer-{last_octet}",
                    detection=f"HTTP 401 Digest on :{http_port}",
                    capabilities=self.capabilities,
                )
        return None

    def fetch_cgi_telemetry(
        self,
        ip: str,
        http_port: int,
        username: str,
        password: str,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        if not username:
            raise ValueError("User-supplied Bitmain Digest username is required")
        base = f"http://{ip}" if int(http_port) == 80 else f"http://{ip}:{http_port}"
        _, mt, _, _ = http_json("GET", f"{base}/cgi-bin/miner_type.cgi", timeout=timeout, digest_user=username, digest_pass=password)
        _, si, _, _ = http_json("GET", f"{base}/cgi-bin/get_system_info.cgi", timeout=timeout, digest_user=username, digest_pass=password)
        st_sum, sc, _, _ = http_json("GET", f"{base}/cgi-bin/summary.cgi", timeout=timeout, digest_user=username, digest_pass=password)
        _, stc, _, _ = http_json("GET", f"{base}/cgi-bin/stats.cgi", timeout=timeout, digest_user=username, digest_pass=password)
        _, pc, _, _ = http_json("GET", f"{base}/cgi-bin/pools.cgi", timeout=timeout, digest_user=username, digest_pass=password)
        if st_sum == 401:
            raise PermissionError("Bitmain HTTP Digest authentication failed (401)")
        return parse_bitmain_cgi_stats(
            mt if isinstance(mt, dict) else None,
            si if isinstance(si, dict) else None,
            sc if isinstance(sc, dict) else None,
            stc if isinstance(stc, dict) else None,
            pc if isinstance(pc, dict) else None,
        )

    def set_workmode(
        self,
        ip: str,
        http_port: int,
        username: str,
        password: str,
        mode: str,
        timeout: float = 6.0,
    ) -> tuple[bool, str]:
        if not username or not password:
            return False, "Bitmain Digest username and password are required"
        mode_map = {"normal": "0", "0": "0", "sleep": "1", "1": "1", "low": "3", "low-power": "3", "lpm": "3", "3": "3"}
        code = mode_map.get(str(mode or "").strip().lower())
        if code is None:
            return False, f"Unsupported Bitmain workmode {mode!r} (expected normal, sleep, or low-power)"
        base = f"http://{ip}" if int(http_port) == 80 else f"http://{ip}:{http_port}"
        st, conf, _, _ = http_json(
            "GET",
            f"{base}/cgi-bin/get_miner_conf.cgi",
            timeout=timeout,
            digest_user=username,
            digest_pass=password,
        )
        if st == 401 or not isinstance(conf, dict):
            return False, f"Failed to read get_miner_conf.cgi (HTTP {st})"
        conf = dict(conf)
        conf["bitmain-work-mode"] = code
        st2, _, _, _ = http_json(
            "POST",
            f"{base}/cgi-bin/set_miner_conf.cgi",
            json_body=conf,
            timeout=timeout,
            digest_user=username,
            digest_pass=password,
        )
        return (200 <= st2 < 300), (f"Bitmain workmode set to {mode} ({code})" if 200 <= st2 < 300 else f"set_miner_conf HTTP {st2}")

    def reboot(self, ip: str, http_port: int, username: str, password: str, timeout: float = 6.0) -> tuple[bool, str]:
        if not username or not password:
            return False, "Bitmain Digest username and password are required"
        base = f"http://{ip}" if int(http_port) == 80 else f"http://{ip}:{http_port}"
        st, _, _ = http_request_raw(
            "GET",
            f"{base}/cgi-bin/reboot.cgi",
            timeout=timeout,
            digest_user=username,
            digest_pass=password,
        )
        return (200 <= st < 300), f"Bitmain reboot.cgi HTTP {st}"
