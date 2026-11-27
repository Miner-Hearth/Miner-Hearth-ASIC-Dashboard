"""Elphapex Scrypt ASIC adapter (DG-Home1, DG1, DG1+).

Uses HTTP :80 /cgi-bin/stats.cgi, /cgi-bin/pools.cgi, and /cgi-bin/luci/getworkmode.cgi
with user-supplied HTTP Basic auth. Opt-in controls allow /cgi-bin/luci/setworkmode.cgi
and reboot when write_enabled=True. NEVER tries vendor default passwords automatically.
"""

from __future__ import annotations

from typing import Any, Optional

from .base import (
    AdapterCapabilities,
    ProbeResult,
    http_json,
    http_request_raw,
    num,
    opt_int,
    tcp_open,
    to_hs,
)


def parse_elphapex_stats(
    stats_cgi: Any,
    pools_cgi: Any = None,
    workmode_cgi: Any = None,
) -> dict[str, Any]:
    stc = stats_cgi if isinstance(stats_cgi, dict) else {}
    st_list = stc.get("STATS") if isinstance(stc.get("STATS"), list) else []
    st0 = st_list[0] if st_list and isinstance(st_list[0], dict) else stc

    info = stc.get("INFO") if isinstance(stc.get("INFO"), dict) else {}
    model = str(info.get("type") or st0.get("type") or st0.get("model") or "Elphapex DG")
    rate_unit = str(st0.get("rate_unit") or "MH/s")
    hr_raw = num(st0, "rate_5s", "rate_avg", "MHS 5s", "GHS 5s")
    hs = to_hs(hr_raw, rate_unit, "scrypt") if hr_raw is not None else None

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
        arr_pcb = ch.get("temp_pcb")
        if isinstance(arr_pcb, list):
            board_temps.extend(float(x) for x in arr_pcb if isinstance(x, (int, float)) and 0 < x < 150)

    fans: list[int] = []
    if isinstance(st0.get("fan"), list):
        fans = [int(round(float(x))) for x in st0["fan"] if isinstance(x, (int, float)) and x >= 0]

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
                "stratum_active": bool(p.get("stratum_active") or p.get("Stratum Active")) or idx == 0,
            })

    wm = ""
    if isinstance(workmode_cgi, dict):
        wm = str(workmode_cgi.get("workmode") or workmode_cgi.get("mode") or "")

    return {
        "model": model,
        "firmware": "Elphapex OS",
        "version": str(info.get("system_version") or info.get("firmware") or st0.get("version") or ""),
        "algorithm": "scrypt",
        "hashrate_unit_kind": "hash",
        "hashrate_hs": hs,
        "asic_temp_c": max(chip_temps) if chip_temps else (max(board_temps) if board_temps else None),
        "chip_temps": chip_temps,
        "board_temps": board_temps,
        "power_w": num(st0, "power", "power_w"),
        "fan_rpms": fans,
        "uptime_sec": opt_int(st0, "elapsed", "uptime"),
        "tuner_mode": wm,
        "pool_url": pools[0]["url"] if pools else "",
        "pool_user": pools[0]["user"] if pools else "",
        "pools": pools,
    }


class ElphapexAdapter:
    family_id = "elphapex"
    display_name = "Elphapex (DG-Home1 / DG1)"
    default_algorithms = ("scrypt",)
    capabilities = AdapterCapabilities(
        monitor=True,
        restart=True,
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
        if not tcp_open(ip, http_port, tcp_timeout):
            return None
        base = f"http://{ip}" if int(http_port) == 80 else f"http://{ip}:{http_port}"
        last_octet = ip.split(".")[-1]
        st, stats, rh, _ = http_json("GET", f"{base}/cgi-bin/stats.cgi", timeout=http_timeout)
        if st == 200 and isinstance(stats, dict):
            hay = str(stats).lower()
            if any(k in hay for k in ("elphapex", "dg-home", "dg1", "dg2")):
                _, pools, _, _ = http_json("GET", f"{base}/cgi-bin/pools.cgi", timeout=http_timeout)
                _, wm, _, _ = http_json("GET", f"{base}/cgi-bin/luci/getworkmode.cgi", timeout=http_timeout)
                parsed = parse_elphapex_stats(stats, pools, wm)
                return ProbeResult(
                    family=self.family_id,
                    device_type="antminer",
                    ip=ip,
                    port=int(http_port),
                    cgminer_port=int(cgminer_port),
                    model=parsed["model"],
                    firmware=parsed["firmware"],
                    firmware_note="Monitor-only by default (opt-in Elphapex workmode/reboot)",
                    version=parsed["version"],
                    algorithm="scrypt",
                    hashrate_hs=parsed["hashrate_hs"],
                    hashrate_unit_kind="hash",
                    asic_temp_c=parsed["asic_temp_c"],
                    fan_rpms=parsed["fan_rpms"],
                    pool_url=parsed["pool_url"],
                    suggested_name=f"elphapex-{last_octet}",
                    detection=f"HTTP {base}/cgi-bin/stats.cgi",
                    capabilities=self.capabilities,
                )
        if st == 401 and "basic" in (rh.get("www-authenticate") or "").lower():
            st_r, body_r, _ = http_request_raw("GET", f"{base}/", timeout=http_timeout)
            hay_r = (body_r or b"").decode("utf-8", errors="replace").lower()
            if "elphapex" in hay_r or "dg-home" in hay_r:
                return ProbeResult(
                    family=self.family_id,
                    device_type="antminer",
                    ip=ip,
                    port=int(http_port),
                    cgminer_port=int(cgminer_port),
                    model="Elphapex DG",
                    firmware="Elphapex OS",
                    firmware_note="HTTP Basic auth required (enter login on device card)",
                    algorithm="scrypt",
                    hashrate_unit_kind="hash",
                    auth_required=True,
                    suggested_name=f"elphapex-{last_octet}",
                    detection=f"HTTP 401 Basic Elphapex on :{http_port}",
                    capabilities=self.capabilities,
                )
        return None

    def fetch_telemetry(
        self,
        ip: str,
        port: int = 80,
        username: str = "",
        password: str = "",
        timeout: float = 4.0,
    ) -> dict[str, Any]:
        base = f"http://{ip}" if int(port) == 80 else f"http://{ip}:{port}"
        st, stats, _, _ = http_json(
            "GET", f"{base}/cgi-bin/stats.cgi",
            digest_user=username, digest_pass=password,
            basic_auth=bool(username), timeout=timeout,
        )
        if st != 200 or not isinstance(stats, dict):
            raise RuntimeError(f"Elphapex /cgi-bin/stats.cgi returned HTTP {st}")
        _, pools, _, _ = http_json(
            "GET", f"{base}/cgi-bin/pools.cgi",
            digest_user=username, digest_pass=password,
            basic_auth=bool(username), timeout=timeout,
        )
        _, wm, _, _ = http_json(
            "GET", f"{base}/cgi-bin/luci/getworkmode.cgi",
            digest_user=username, digest_pass=password,
            basic_auth=bool(username), timeout=timeout,
        )
        out = parse_elphapex_stats(stats, pools, wm)
        out["source"] = f"Elphapex HTTP :{port}"
        return out

    def set_workmode(self, ip: str, port: int, username: str, password: str, mode: str, timeout: float = 5.0) -> tuple[bool, str]:
        base = f"http://{ip}" if int(port) == 80 else f"http://{ip}:{port}"
        m = str(mode or "").strip()
        if m not in {"0", "1", "2", "normal", "low-power", "sleep"}:
            return False, "Elphapex workmode must be 0/1/2 (or normal/low-power/sleep)"
        st, data, _, _ = http_json(
            "POST", f"{base}/cgi-bin/luci/setworkmode.cgi",
            json_body={"workmode": m},
            digest_user=username, digest_pass=password,
            basic_auth=bool(username), timeout=timeout,
        )
        if 200 <= st < 300:
            return True, f"Elphapex workmode={m} applied"
        return False, f"Elphapex setworkmode failed: HTTP {st} {data or ''}".strip()

    def reboot(self, ip: str, port: int, username: str, password: str, timeout: float = 5.0) -> tuple[bool, str]:
        base = f"http://{ip}" if int(port) == 80 else f"http://{ip}:{port}"
        st, _, _ = http_request_raw(
            "GET", f"{base}/cgi-bin/reboot.cgi",
            digest_user=username, digest_pass=password,
            basic_auth=bool(username), timeout=timeout,
        )
        if 200 <= st < 300:
            return True, "Elphapex reboot command sent"
        return False, f"Elphapex reboot failed: HTTP {st}"
