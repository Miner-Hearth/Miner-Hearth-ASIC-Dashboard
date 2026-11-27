"""Bitdeer Sealminer adapter (A2, A3, A4 — BDMiner CGMiner TCP :4028).

Uses incremental balanced-JSON parsing in cgminer_request() so non-closing
BDMiner sockets return immediately without waiting for EOF or NUL.
Strictly read-only monitoring.
"""

from __future__ import annotations

from typing import Any, Optional

from .base import (
    AdapterCapabilities,
    ProbeResult,
    cgminer_request,
    num,
    opt_int,
    tcp_open,
    to_hs,
)


def is_sealminer_payload(
    version_data: Any,
    summary_data: Any = None,
    stats_data: Any = None,
) -> bool:
    hay = f"{version_data or ''} {summary_data or ''} {stats_data or ''}".lower()
    return any(tok in hay for tok in ("bdminer", "sealminer", "bitdeer", "seal-"))


def parse_sealminer_readings(
    version_data: Optional[dict[str, Any]],
    summary_data: Optional[dict[str, Any]],
    devs_data: Optional[dict[str, Any]] = None,
    stats_data: Optional[dict[str, Any]] = None,
    pools_data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    ver_list = version_data.get("VERSION") if isinstance(version_data, dict) and isinstance(version_data.get("VERSION"), list) else []
    v0 = ver_list[0] if ver_list and isinstance(ver_list[0], dict) else {}
    sum_list = summary_data.get("SUMMARY") if isinstance(summary_data, dict) and isinstance(summary_data.get("SUMMARY"), list) else []
    s0 = sum_list[0] if sum_list and isinstance(sum_list[0], dict) else {}
    dev_list = devs_data.get("DEVS") if isinstance(devs_data, dict) and isinstance(devs_data.get("DEVS"), list) else []
    st_list = stats_data.get("STATS") if isinstance(stats_data, dict) and isinstance(stats_data.get("STATS"), list) else []
    st0 = st_list[0] if st_list and isinstance(st_list[0], dict) else {}

    hs: Optional[float] = None
    if num(s0, "THS 5s", "THS av") is not None:
        hs = to_hs(num(s0, "THS 5s", "THS av"), "TH/s", "sha256d")
    elif num(s0, "GHS 5s", "GHS av") is not None:
        hs = to_hs(num(s0, "GHS 5s", "GHS av"), "GH/s", "sha256d")
    elif num(s0, "MHS 5s", "MHS av") is not None:
        hs = to_hs(num(s0, "MHS 5s", "MHS av"), "MH/s", "sha256d")

    chip_temps: list[float] = []
    board_temps: list[float] = []
    for d in dev_list:
        if not isinstance(d, dict):
            continue
        t = num(d, "Temperature", "Chip Temp", "Chip Temp Max")
        if t is not None and 0 < t < 150:
            chip_temps.append(t)
        bt = num(d, "Board Temp", "PCB Temp")
        if bt is not None and 0 < bt < 150:
            board_temps.append(bt)

    for i in range(1, 5):
        t = num(st0, f"temp_chip{i}", f"temp{i}")
        if t is not None and 0 < t < 150:
            chip_temps.append(t)

    fans: list[int] = []
    for fk in ("fan1", "fan2", "fan3", "fan4"):
        fv = opt_int(st0, fk)
        if fv is not None and fv >= 0:
            fans.append(fv)

    pools: list[dict[str, Any]] = []
    plist = pools_data.get("POOLS") if isinstance(pools_data, dict) and isinstance(pools_data.get("POOLS"), list) else []
    for idx, p in enumerate(plist):
        if isinstance(p, dict):
            pools.append({
                "pool": idx,
                "url": str(p.get("URL") or ""),
                "user": str(p.get("User") or ""),
                "status": str(p.get("Status") or ""),
                "stratum_active": bool(p.get("Stratum Active")) or idx == 0,
            })

    model = str(v0.get("Type") or v0.get("Miner") or "Sealminer")
    version = str(v0.get("BDMiner") or v0.get("CGMiner") or v0.get("CompileTime") or "")
    return {
        "model": model,
        "firmware": "BDMiner (Sealminer)",
        "version": version,
        "algorithm": "sha256d",
        "hashrate_unit_kind": "hash",
        "hashrate_hs": hs,
        "asic_temp_c": max(chip_temps) if chip_temps else num(s0, "Temperature"),
        "chip_temps": chip_temps,
        "board_temps": board_temps,
        "power_w": num(s0, "Power", "Power_RT") or num(st0, "power", "Power"),
        "fan_rpms": fans,
        "uptime_sec": opt_int(s0, "Elapsed"),
        "shares_accepted": opt_int(s0, "Accepted"),
        "shares_rejected": opt_int(s0, "Rejected"),
        "hw_errors": opt_int(s0, "Hardware Errors"),
        "pool_url": pools[0]["url"] if pools else "",
        "pool_user": pools[0]["user"] if pools else "",
        "pools": pools,
    }


class SealminerAdapter:
    family_id = "sealminer"
    display_name = "Bitdeer Sealminer (A2/A3/A4 BDMiner)"
    default_algorithms = ("sha256d",)
    capabilities = AdapterCapabilities(monitor=True)

    def probe(
        self,
        ip: str,
        http_port: int = 80,
        cgminer_port: int = 4028,
        tcp_timeout: float = 1.0,
        http_timeout: float = 4.0,
    ) -> Optional[ProbeResult]:
        if not tcp_open(ip, cgminer_port, tcp_timeout):
            return None
        try:
            ver = cgminer_request(ip, cgminer_port, "version", timeout=http_timeout)
            summ = cgminer_request(ip, cgminer_port, "summary", timeout=http_timeout)
            stats = None
            try:
                stats = cgminer_request(ip, cgminer_port, "stats", timeout=http_timeout)
            except Exception:
                pass
            if not is_sealminer_payload(ver, summ, stats):
                return None
            devs = pools = None
            try:
                devs = cgminer_request(ip, cgminer_port, "devs", timeout=http_timeout)
            except Exception:
                pass
            try:
                pools = cgminer_request(ip, cgminer_port, "pools", timeout=http_timeout)
            except Exception:
                pass
            out = parse_sealminer_readings(ver, summ, devs, stats, pools)
            last_octet = ip.split(".")[-1]
            return ProbeResult(
                family=self.family_id,
                device_type="antminer",
                ip=ip,
                port=int(http_port),
                cgminer_port=int(cgminer_port),
                model=out["model"],
                firmware=out["firmware"],
                firmware_note="Monitor-only BDMiner TCP :4028 (non-closing socket safe)",
                version=out["version"],
                algorithm="sha256d",
                hashrate_hs=out["hashrate_hs"],
                hashrate_unit_kind="hash",
                asic_temp_c=out["asic_temp_c"],
                fan_rpms=out["fan_rpms"],
                pool_url=out["pool_url"],
                suggested_name=f"sealminer-{last_octet}",
                detection=f"BDMiner TCP :{cgminer_port}",
                capabilities=self.capabilities,
            )
        except Exception:
            return None

    def fetch_telemetry(self, ip: str, cgminer_port: int = 4028, timeout: float = 4.0) -> dict[str, Any]:
        ver = cgminer_request(ip, cgminer_port, "version", timeout=timeout)
        summ = cgminer_request(ip, cgminer_port, "summary", timeout=timeout)
        devs = stats = pools = None
        for cmd in ("devs", "stats", "pools"):
            try:
                res = cgminer_request(ip, cgminer_port, cmd, timeout=timeout)
                if cmd == "devs":
                    devs = res
                elif cmd == "stats":
                    stats = res
                else:
                    pools = res
            except Exception:
                pass
        out = parse_sealminer_readings(ver, summ, devs, stats, pools)
        out["source"] = f"BDMiner :{cgminer_port}"
        return out
