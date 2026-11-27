"""Canaan Avalon adapter (Nano 3, Nano 3S, Avalon Q, Mini 3, Avalon Home) via CGMiner :4028 + ascset."""

from __future__ import annotations

import re
import time
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


def parse_avalon_mm_id(raw: str) -> dict[str, Any]:
    """Parse Canaan Avalon 'MM ID0' bracketed key-value string."""
    out: dict[str, Any] = {}
    if not raw or not isinstance(raw, str):
        return out
    for key, val in re.findall(r"([A-Za-z0-9_]+)\[([^\]]*)\]", raw):
        out[key] = val.strip()
    return out


def _first_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(val))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _all_floats(val: Any) -> list[float]:
    if val is None:
        return []
    out: list[float] = []
    for m in re.findall(r"-?\d+(?:\.\d+)?", str(val)):
        try:
            out.append(float(m))
        except ValueError:
            pass
    return out


def is_avalon_payload(*payloads: Optional[dict[str, Any]]) -> bool:
    hay = " ".join(str(p or "") for p in payloads).lower()
    return any(tok in hay for tok in ("nano3", "nano 3", "avalon", "mm id0", "workmode[", "ghsmm[", "litestats"))


def parse_avalon_readings(
    version_data: Optional[dict[str, Any]],
    summary_data: Optional[dict[str, Any]],
    stats_data: Optional[dict[str, Any]] = None,
    litestats_data: Optional[dict[str, Any]] = None,
    devs_data: Optional[dict[str, Any]] = None,
    pools_data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    v0 = (version_data or {}).get("VERSION")
    ver_item = v0[0] if isinstance(v0, list) and v0 and isinstance(v0[0], dict) else {}
    s0 = (summary_data or {}).get("SUMMARY")
    sum_item = s0[0] if isinstance(s0, list) and s0 and isinstance(s0[0], dict) else {}

    mm: dict[str, Any] = {}
    stats_model = ""
    for src in (stats_data, litestats_data):
        if not isinstance(src, dict):
            continue
        items = src.get("STATS")
        if not isinstance(items, list):
            continue
        for entry in items:
            if not isinstance(entry, dict):
                continue
            if not stats_model and (entry.get("ID") or entry.get("Type")):
                stats_model = str(entry.get("Type") or entry.get("ID") or "")
            for k, v in entry.items():
                if str(k).startswith("MM ID") and isinstance(v, str):
                    mm.update(parse_avalon_mm_id(v))
                elif k in {"ITemp", "OTemp", "TMax", "TAvg", "FanR", "WORKMODE", "GHSmm", "GHSavg", "MPO", "PS"}:
                    mm[k] = v

    # Hashrate: prefer GHSmm / GHSavg from MM ID, then SUMMARY GHS 5s/MHS av
    ghs_mm = _first_float(mm.get("GHSmm"))
    ghs_avg = _first_float(mm.get("GHSavg"))
    ghs_5s = num(sum_item, "GHS 5s")
    mhs_5s = num(sum_item, "MHS 5s", "MHS av")
    if ghs_mm is not None:
        hs = to_hs(ghs_mm, "GH/s", "sha256d")
    elif ghs_5s is not None:
        hs = to_hs(ghs_5s, "GH/s", "sha256d")
    elif mhs_5s is not None:
        hs = to_hs(mhs_5s, "MH/s", "sha256d")
    elif ghs_avg is not None:
        hs = to_hs(ghs_avg, "GH/s", "sha256d")
    else:
        hs = None

    hs_avg = to_hs(ghs_avg, "GH/s", "sha256d") if ghs_avg is not None else to_hs(num(sum_item, "MHS av"), "MH/s", "sha256d")

    intake_t = _first_float(mm.get("ITemp"))
    outlet_t = _first_float(mm.get("OTemp"))
    tmax = _first_float(mm.get("TMax"))
    tavg = _first_float(mm.get("TAvg"))
    if tmax is None and isinstance(devs_data, dict) and isinstance(devs_data.get("DEVS"), list) and devs_data["DEVS"]:
        d0 = devs_data["DEVS"][0]
        if isinstance(d0, dict):
            tmax = num(d0, "Temperature")

    fan_rpms: list[int] = []
    for k in ("Fan1", "Fan2", "Fan3", "Fan4"):
        fv = _first_float(mm.get(k))
        if fv is not None and fv >= 0:
            fan_rpms.append(int(round(fv)))
    fan_pct = _first_float(mm.get("FanR"))

    # PS[...] format on Avalon often has [vout, vin, watts_or_cur, ..., watts] or single watts
    power_w = _first_float(mm.get("MPO"))
    if "PS" in mm:
        ps_nums = _all_floats(mm["PS"])
        # On Nano 3 / 3S / Q, PS[0 1212 1200 130 140] -> last or MPO often reports Watts; pick sane W if MPO absent
        if power_w is None and ps_nums:
            # Take positive plausible wattage from PS if single or 5-element tuple
            if len(ps_nums) >= 5 and ps_nums[4] > 0:
                power_w = ps_nums[4]
            elif len(ps_nums) == 1 and ps_nums[0] > 0:
                power_w = ps_nums[0]

    workmode_raw = mm.get("WORKMODE")
    wm_int = int(_first_float(workmode_raw)) if _first_float(workmode_raw) is not None else None
    wm_label = {0: "Eco/Low", 1: "Standard/Mid", 2: "Super/High"}.get(wm_int, str(workmode_raw or ""))

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

    model = str(mm.get("Ver") or ver_item.get("Miner") or stats_model or ver_item.get("Type") or "Canaan Avalon")
    if "nano3s" in model.lower():
        model_label = "Avalon Nano 3S"
    elif "nano3" in model.lower():
        model_label = "Avalon Nano 3"
    elif "avalonq" in model.lower() or "q-" in model.lower():
        model_label = "Avalon Q"
    elif "mini3" in model.lower():
        model_label = "Avalon Mini 3"
    else:
        model_label = model

    errs: list[str] = []
    for ek in ("ECHU", "ECMM"):
        ev = _first_float(mm.get(ek))
        if ev is not None and int(ev) != 0:
            errs.append(f"{ek}={int(ev)}")

    return {
        "model": model_label,
        "firmware": "Canaan Avalon CGMiner",
        "version": str(mm.get("Ver") or ver_item.get("CGMiner") or ""),
        "hashrate_hs": hs,
        "hashrate_avg_hs": hs_avg,
        "asic_temp_c": tmax if tmax is not None else tavg,
        "ambient_temp_c": intake_t,
        "board_temps": [v for v in (intake_t, outlet_t, tavg, tmax) if v is not None],
        "fan_rpms": fan_rpms,
        "fan_pct": fan_pct,
        "power_w": power_w,
        "workmode": wm_label,
        "workmode_code": wm_int,
        "uptime_sec": num(sum_item, "Elapsed"),
        "shares_accepted": opt_int(sum_item, "Accepted"),
        "shares_rejected": opt_int(sum_item, "Rejected"),
        "hw_errors": opt_int(sum_item, "Hardware Errors"),
        "pool_url": pools[0]["url"] if pools else "",
        "pool_user": pools[0]["user"] if pools else "",
        "pools": pools,
        "errors": errs,
        "mm_raw": mm,
    }


def _check_cgminer_ok(res: dict[str, Any]) -> tuple[bool, str]:
    st = res.get("STATUS")
    if isinstance(st, list) and st and isinstance(st[0], dict):
        code = str(st[0].get("STATUS") or "").upper()
        msg = str(st[0].get("Msg") or "")
        if code in {"S", "I"}:
            return True, msg or "OK"
        return False, f"{code}: {msg}"
    return True, "OK"


class AvalonAdapter:
    family_id = "avalon"
    display_name = "Canaan Avalon (Nano 3/3S, Q, Mini 3, Home)"
    default_algorithms = ("sha256d",)
    capabilities = AdapterCapabilities(
        monitor=True,
        faults=True,
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
        if not tcp_open(ip, cgminer_port, tcp_timeout):
            return None
        try:
            ver = cgminer_request(ip, cgminer_port, "version", timeout=http_timeout)
            stats = None
            lite = None
            try:
                stats = cgminer_request(ip, cgminer_port, "stats", timeout=http_timeout)
            except Exception:
                pass
            if not stats or "MM ID" not in str(stats):
                try:
                    lite = cgminer_request(ip, cgminer_port, "litestats", timeout=http_timeout)
                except Exception:
                    pass
            if not is_avalon_payload(ver, stats, lite):
                return None
            summ = cgminer_request(ip, cgminer_port, "summary", timeout=http_timeout)
            devs = None
            pools = None
            try:
                devs = cgminer_request(ip, cgminer_port, "devs", timeout=http_timeout)
            except Exception:
                pass
            try:
                pools = cgminer_request(ip, cgminer_port, "pools", timeout=http_timeout)
            except Exception:
                pass
            parsed = parse_avalon_readings(ver, summ, stats, lite, devs, pools)
            last_octet = ip.split(".")[-1]
            return ProbeResult(
                family="avalon",
                device_type="antminer",
                ip=ip,
                port=int(cgminer_port),
                cgminer_port=int(cgminer_port),
                model=parsed["model"],
                firmware=parsed["firmware"],
                firmware_note="Monitor-only by default (opt-in Avalon ascset workmode/sleep/reboot)",
                version=parsed["version"],
                algorithm="sha256d",
                hashrate_hs=parsed["hashrate_hs"],
                hashrate_unit_kind="hash",
                asic_temp_c=parsed["asic_temp_c"],
                fan_rpms=parsed["fan_rpms"],
                pool_url=parsed["pool_url"],
                suggested_name=f"avalon-{last_octet}",
                detection=f"Canaan Avalon CGMiner :{cgminer_port}",
                capabilities=self.capabilities,
            )
        except Exception:
            return None

    def set_workmode(self, ip: str, port: int, mode: str, timeout: float = 4.0) -> tuple[bool, str]:
        mode_map = {"0": 0, "eco": 0, "low": 0, "1": 1, "standard": 1, "normal": 1, "mid": 1, "2": 2, "super": 2, "high": 2, "turbo": 2}
        code = mode_map.get(str(mode or "").strip().lower())
        if code is None:
            return False, f"Invalid Avalon workmode {mode!r} (expected 0/eco, 1/standard, 2/super)"
        res = cgminer_request(ip, port, "ascset", f"0,workmode,set,{code}", timeout=timeout)
        return _check_cgminer_ok(res)

    def soft_off(self, ip: str, port: int, timeout: float = 4.0) -> tuple[bool, str]:
        ts = int(time.time()) + 5
        res = cgminer_request(ip, port, "ascset", f"0,softoff,1:{ts}", timeout=timeout)
        return _check_cgminer_ok(res)

    def soft_on(self, ip: str, port: int, timeout: float = 4.0) -> tuple[bool, str]:
        ts = int(time.time()) + 5
        res = cgminer_request(ip, port, "ascset", f"0,softon,1:{ts}", timeout=timeout)
        return _check_cgminer_ok(res)

    def reboot(self, ip: str, port: int, timeout: float = 4.0) -> tuple[bool, str]:
        res = cgminer_request(ip, port, "ascset", "0,reboot,0", timeout=timeout)
        return _check_cgminer_ok(res)
