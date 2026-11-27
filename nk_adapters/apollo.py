"""FutureBit Apollo (Apollo BTC / Apollo II) CGMiner :4028 monitor-first adapter."""

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


def parse_apollo_readings(
    version_data: Optional[dict[str, Any]],
    summary_data: Optional[dict[str, Any]],
    devs_data: Optional[dict[str, Any]] = None,
    pools_data: Optional[dict[str, Any]] = None,
    stats_data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    v0 = (version_data or {}).get("VERSION")
    ver_item = v0[0] if isinstance(v0, list) and v0 and isinstance(v0[0], dict) else {}
    s0 = (summary_data or {}).get("SUMMARY")
    sum_item = s0[0] if isinstance(s0, list) and s0 and isinstance(s0[0], dict) else {}

    mhs_5s = num(sum_item, "MHS 5s", "MHS av")
    ghs_5s = num(sum_item, "GHS 5s", "GHS av")
    hs = to_hs(ghs_5s, "GH/s", "sha256d") if ghs_5s is not None else to_hs(mhs_5s, "MH/s", "sha256d")

    temps: list[float] = []
    fans: list[int] = []
    if isinstance(devs_data, dict) and isinstance(devs_data.get("DEVS"), list):
        for d in devs_data["DEVS"]:
            if not isinstance(d, dict):
                continue
            t = num(d, "Temperature", "Temp")
            if t is not None and 0 < t < 150:
                temps.append(t)
            fr = opt_int(d, "Fan Speed", "Fan RPM")
            if fr is not None and fr >= 0:
                fans.append(fr)

    if isinstance(stats_data, dict) and isinstance(stats_data.get("STATS"), list):
        for st in stats_data["STATS"]:
            if not isinstance(st, dict):
                continue
            for k in ("temp", "temp1", "temp2", "Temperature"):
                tv = num(st, k)
                if tv is not None and 0 < tv < 150:
                    temps.append(tv)
            for k in ("fan1", "fan2", "Fan"):
                fv = opt_int(st, k)
                if fv is not None and fv >= 0:
                    fans.append(fv)

    pools: list[dict[str, Any]] = []
    if isinstance(pools_data, dict) and isinstance(pools_data.get("POOLS"), list):
        for idx, p in enumerate(pools_data["POOLS"]):
            if isinstance(p, dict):
                pools.append({
                    "pool": idx,
                    "url": str(p.get("URL") or ""),
                    "user": str(p.get("User") or ""),
                    "status": str(p.get("Status") or ""),
                    "accepted": opt_int(p, "Accepted") or 0,
                    "rejected": opt_int(p, "Rejected") or 0,
                })

    model = str(ver_item.get("Miner") or ver_item.get("Type") or "FutureBit Apollo")
    return {
        "model": model,
        "firmware": "Apollo OS / CGMiner",
        "version": str(ver_item.get("CGMiner") or ""),
        "hashrate_hs": hs,
        "asic_temp_c": max(temps) if temps else None,
        "asic_temps": temps,
        "fan_rpms": fans,
        "uptime_sec": num(sum_item, "Elapsed"),
        "shares_accepted": opt_int(sum_item, "Accepted"),
        "shares_rejected": opt_int(sum_item, "Rejected"),
        "hw_errors": opt_int(sum_item, "Hardware Errors"),
        "pool_url": pools[0]["url"] if pools else "",
        "pool_user": pools[0]["user"] if pools else "",
        "pools": pools,
    }


class ApolloAdapter:
    family_id = "apollo"
    display_name = "FutureBit Apollo (BTC / II)"
    default_algorithms = ("sha256d",)
    capabilities = AdapterCapabilities(monitor=True, faults=True)

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
            devs = None
            try:
                devs = cgminer_request(ip, cgminer_port, "devs", timeout=http_timeout)
            except Exception:
                pass
            hay = (str(ver) + " " + str(devs or "")).lower()
            if "futurebit" not in hay and "apollo" not in hay:
                return None
            summ = cgminer_request(ip, cgminer_port, "summary", timeout=http_timeout)
            pools = None
            try:
                pools = cgminer_request(ip, cgminer_port, "pools", timeout=http_timeout)
            except Exception:
                pass
            parsed = parse_apollo_readings(ver, summ, devs, pools)
            last_octet = ip.split(".")[-1]
            return ProbeResult(
                family="apollo",
                device_type="antminer",
                ip=ip,
                port=int(cgminer_port),
                cgminer_port=int(cgminer_port),
                model=parsed["model"],
                firmware=parsed["firmware"],
                firmware_note="Monitor-only: FutureBit Apollo CGMiner telemetry",
                version=parsed["version"],
                algorithm="sha256d",
                hashrate_hs=parsed["hashrate_hs"],
                hashrate_unit_kind="hash",
                asic_temp_c=parsed["asic_temp_c"],
                fan_rpms=parsed["fan_rpms"],
                pool_url=parsed["pool_url"],
                suggested_name=f"apollo-{last_octet}",
                detection=f"FutureBit Apollo CGMiner :{cgminer_port}",
                capabilities=self.capabilities,
            )
        except Exception:
            return None
