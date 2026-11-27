"""MicroBT Whatsminer adapter (BTMiner v2 TCP :4028 + v3 TCP :4433).

Supports read-only monitoring across M20/M30/M50/M60 series via BTMiner v2 JSON RPC
(:4028) and BTMiner v3 JSON RPC (:4433), plus opt-in v3 token-authenticated controls
(power mode, power percentage, suspend/resume, reboot) when write_enabled is True.
NEVER tries vendor default passwords automatically.
"""

from __future__ import annotations

import hashlib
import json
import socket
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


def whatsminer_v3_request(
    ip: str,
    port: int,
    cmd: str,
    param: Any = None,
    *,
    ts: Optional[int] = None,
    account: str = "",
    token: str = "",
    timeout: float = 4.0,
) -> dict[str, Any]:
    """Send a JSON command to Whatsminer BTMiner v3 TCP API (port 4433)."""
    req: dict[str, Any] = {"cmd": cmd}
    if param is not None:
        req["param"] = param
    if ts is not None:
        req["ts"] = int(ts)
    if account:
        req["account"] = account
    if token:
        req["token"] = token
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
            cand = b"".join(chunks).replace(b"\x00", b"").decode("utf-8", errors="replace").lstrip()
            if cand.startswith("{"):
                try:
                    obj, end_idx = json.JSONDecoder().raw_decode(cand)
                    rest = cand[end_idx:].strip()
                    if isinstance(obj, dict) and (not rest or cand.rstrip().endswith("}")):
                        parsed = obj
                        break
                except json.JSONDecodeError:
                    pass
    if parsed is not None:
        return parsed
    raw = b"".join(chunks).replace(b"\x00", b"").decode("utf-8", errors="replace").strip()
    if not raw:
        raise ValueError(f"Whatsminer v3 {ip}:{port} {cmd} returned empty response")
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError(f"Whatsminer v3 {ip}:{port} {cmd} returned non-dict JSON")
    return obj


def make_whatsminer_v3_token(cmd: str, salt: str, password: str, ts: int, account: str = "super", param: Any = None) -> str:
    """Compute SHA-256 request signature for Whatsminer v3 write commands."""
    p_str = "" if param is None else (param if isinstance(param, str) else json.dumps(param, separators=(",", ":")))
    material = f"{cmd}{p_str}{account}{salt}{int(ts)}{password}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def is_whatsminer_payload(
    version_data: Optional[dict[str, Any]],
    summary_data: Optional[dict[str, Any]] = None,
    devs_data: Optional[dict[str, Any]] = None,
) -> bool:
    hay = f"{version_data or ''} {summary_data or ''} {devs_data or ''}".lower()
    return any(tok in hay for tok in ("btminer", "whatsminer", "m20", "m30", "m50", "m60", "m63", "m66"))


def parse_whatsminer_v2(
    version_data: Optional[dict[str, Any]],
    summary_data: Optional[dict[str, Any]],
    devs_data: Optional[dict[str, Any]] = None,
    pools_data: Optional[dict[str, Any]] = None,
    psu_data: Optional[dict[str, Any]] = None,
    error_data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Normalize BTMiner v2 :4028 payloads."""
    ver_list = version_data.get("VERSION") if isinstance(version_data, dict) and isinstance(version_data.get("VERSION"), list) else []
    v0 = ver_list[0] if ver_list and isinstance(ver_list[0], dict) else {}
    vmsg = version_data.get("Msg") if isinstance(version_data, dict) and isinstance(version_data.get("Msg"), dict) else {}
    sum_list = summary_data.get("SUMMARY") if isinstance(summary_data, dict) and isinstance(summary_data.get("SUMMARY"), list) else []
    s0 = sum_list[0] if sum_list and isinstance(sum_list[0], dict) else {}
    dev_list = devs_data.get("DEVS") if isinstance(devs_data, dict) and isinstance(devs_data.get("DEVS"), list) else []

    # Hashrate: BTMiner v2 'HS RT' and 'MHS 5s' / 'MHS av' are in MH/s.
    hs: Optional[float] = None
    if num(s0, "THS 5s", "THS av") is not None:
        hs = to_hs(num(s0, "THS 5s", "THS av"), "TH/s", "sha256d")
    elif num(s0, "GHS 5s", "GHS av") is not None:
        hs = to_hs(num(s0, "GHS 5s", "GHS av"), "GH/s", "sha256d")
    elif num(s0, "HS RT", "MHS 5s", "MHS av") is not None:
        hs = to_hs(num(s0, "HS RT", "MHS 5s", "MHS av"), "MH/s", "sha256d")

    chip_temps: list[float] = []
    board_temps: list[float] = []
    for d in dev_list:
        if not isinstance(d, dict):
            continue
        ct = num(d, "Chip Temp Max", "Chip Temp Avg", "Temperature")
        bt = num(d, "Temperature", "Board Temp")
        if ct is not None and 0 < ct < 150:
            chip_temps.append(ct)
        if bt is not None and 0 < bt < 150:
            board_temps.append(bt)

    max_chip = num(s0, "Chip Temp Max", "Chip Temp Avg", "Temperature")
    if max_chip is None and chip_temps:
        max_chip = max(chip_temps)
    if max_chip is None and board_temps:
        max_chip = max(board_temps)

    fans: list[int] = []
    for fk in ("Fan Speed In", "Fan Speed Out"):
        fv = opt_int(s0, fk)
        if fv is not None and fv >= 0:
            fans.append(fv)

    power_w = num(s0, "Power_RT", "Power", "Power Limit")
    if power_w is None and isinstance(psu_data, dict):
        pmsg = psu_data.get("Msg") if isinstance(psu_data.get("Msg"), dict) else psu_data
        power_w = num(pmsg, "pin", "power")

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

    errors: list[str] = []
    if isinstance(error_data, dict):
        emsg = error_data.get("Msg")
        if isinstance(emsg, dict) and isinstance(emsg.get("error_code"), list):
            for e in emsg["error_code"]:
                if isinstance(e, dict):
                    errors.extend(f"{k}: {v}" for k, v in e.items())
                else:
                    errors.append(str(e))

    model = str(v0.get("Miner") or vmsg.get("platform") or v0.get("Type") or "Whatsminer")
    version = str(vmsg.get("fw_ver") or v0.get("BTMiner") or v0.get("CompileTime") or "")
    return {
        "model": model,
        "firmware": "BTMiner (Whatsminer)",
        "version": version,
        "algorithm": "sha256d",
        "hashrate_hs": hs,
        "asic_temp_c": max_chip,
        "chip_temps": chip_temps,
        "board_temps": board_temps,
        "ambient_temp_c": num(s0, "Env Temp"),
        "power_w": power_w,
        "power_limit_w": num(s0, "Power Limit"),
        "fan_rpms": fans,
        "uptime_sec": opt_int(s0, "Elapsed"),
        "shares_accepted": opt_int(s0, "Accepted"),
        "shares_rejected": opt_int(s0, "Rejected"),
        "hw_errors": opt_int(s0, "Hardware Errors"),
        "tuner_mode": str(s0.get("Power Mode") or ""),
        "pool_url": pools[0]["url"] if pools else "",
        "pool_user": pools[0]["user"] if pools else "",
        "pools": pools,
        "errors": errors,
    }


def parse_whatsminer_v3(
    info_data: Optional[dict[str, Any]],
    summary_data: Optional[dict[str, Any]],
    pools_data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Normalize BTMiner v3 :4433 responses."""
    imsg = info_data.get("msg") if isinstance(info_data, dict) and isinstance(info_data.get("msg"), dict) else {}
    smsg = summary_data.get("msg") if isinstance(summary_data, dict) and isinstance(summary_data.get("msg"), dict) else {}
    ssum = smsg.get("summary") if isinstance(smsg.get("summary"), dict) else smsg

    miner_meta = imsg.get("miner") if isinstance(imsg.get("miner"), dict) else {}
    sys_meta = imsg.get("system") if isinstance(imsg.get("system"), dict) else {}

    # v3 hash-realtime is reported in TH/s unless unit says otherwise
    hr_rt = num(ssum, "hash-realtime", "hash_realtime", "hash-average")
    hs = to_hs(hr_rt, "TH/s", "sha256d") if hr_rt is not None else None
    fans = [f for f in (opt_int(ssum, "fan-speed-in", "fan_speed_in"), opt_int(ssum, "fan-speed-out", "fan_speed_out")) if f is not None and f >= 0]

    pools: list[dict[str, Any]] = []
    pmsg = pools_data.get("msg") if isinstance(pools_data, dict) else None
    plist = pmsg.get("pools") if isinstance(pmsg, dict) and isinstance(pmsg.get("pools"), list) else (pmsg if isinstance(pmsg, list) else [])
    for idx, p in enumerate(plist):
        if isinstance(p, dict):
            pools.append({
                "pool": idx,
                "url": str(p.get("url") or p.get("URL") or ""),
                "user": str(p.get("account") or p.get("user") or ""),
                "status": str(p.get("status") or ""),
                "stratum_active": bool(p.get("active")) or idx == 0,
            })

    return {
        "model": str(miner_meta.get("type") or miner_meta.get("model") or "Whatsminer"),
        "firmware": "BTMiner v3 (Whatsminer)",
        "version": str(sys_meta.get("fwversion") or sys_meta.get("api") or ""),
        "salt": str(imsg.get("salt") or ""),
        "algorithm": "sha256d",
        "hashrate_hs": hs,
        "asic_temp_c": num(ssum, "chip-temp-max", "chip-temp-avg", "board-temp-max"),
        "ambient_temp_c": num(ssum, "env-temp", "env_temp"),
        "power_w": num(ssum, "power-realtime", "power-limit", "power"),
        "power_limit_w": num(ssum, "power-limit"),
        "fan_rpms": fans,
        "uptime_sec": opt_int(ssum, "elapsed"),
        "shares_accepted": opt_int(ssum, "accepted"),
        "shares_rejected": opt_int(ssum, "rejected"),
        "tuner_mode": str(ssum.get("power-mode") or ""),
        "pool_url": pools[0]["url"] if pools else "",
        "pool_user": pools[0]["user"] if pools else "",
        "pools": pools,
    }


class WhatsminerAdapter:
    family_id = "whatsminer"
    display_name = "MicroBT Whatsminer (BTMiner v2/v3)"
    default_algorithms = ("sha256d",)
    capabilities = AdapterCapabilities(
        monitor=True,
        faults=True,
        restart=True,
        sleep_wake=True,
        workmode=True,
        power_target=True,
    )

    def probe(
        self,
        ip: str,
        http_port: int = 80,
        cgminer_port: int = 4028,
        v3_port: int = 4433,
        tcp_timeout: float = 1.0,
        http_timeout: float = 4.0,
    ) -> Optional[ProbeResult]:
        last_octet = ip.split(".")[-1]
        # 1. Try BTMiner v2 :4028
        if tcp_open(ip, cgminer_port, tcp_timeout):
            try:
                ver = cgminer_request(ip, cgminer_port, "version", timeout=http_timeout)
                summ = cgminer_request(ip, cgminer_port, "summary", timeout=http_timeout)
                if is_whatsminer_payload(ver, summ):
                    pools = None
                    try:
                        pools = cgminer_request(ip, cgminer_port, "pools", timeout=http_timeout)
                    except Exception:
                        pass
                    parsed = parse_whatsminer_v2(ver, summ, None, pools)
                    return ProbeResult(
                        family=self.family_id,
                        device_type="antminer",
                        ip=ip,
                        port=int(http_port),
                        cgminer_port=int(cgminer_port),
                        model=parsed["model"],
                        firmware=parsed["firmware"],
                        firmware_note="Monitor-only by default (opt-in BTMiner v3 :4433 controls)",
                        version=parsed["version"],
                        algorithm="sha256d",
                        hashrate_hs=parsed["hashrate_hs"],
                        hashrate_unit_kind="hash",
                        asic_temp_c=parsed["asic_temp_c"],
                        fan_rpms=parsed["fan_rpms"],
                        pool_url=parsed["pool_url"],
                        suggested_name=f"whatsminer-{last_octet}",
                        detection=f"BTMiner v2 :{cgminer_port}",
                        capabilities=self.capabilities,
                    )
            except Exception:
                pass
        # 2. Try BTMiner v3 :4433
        if tcp_open(ip, int(v3_port), tcp_timeout):
            try:
                info = whatsminer_v3_request(ip, int(v3_port), "get.device.info", timeout=http_timeout)
                if isinstance(info, dict) and info.get("code") == 0:
                    summ = whatsminer_v3_request(ip, int(v3_port), "get.miner.status", "summary", timeout=http_timeout)
                    parsed = parse_whatsminer_v3(info, summ)
                    return ProbeResult(
                        family=self.family_id,
                        device_type="antminer",
                        ip=ip,
                        port=int(http_port),
                        cgminer_port=int(cgminer_port),
                        model=parsed["model"],
                        firmware=parsed["firmware"],
                        firmware_note="Monitor-only by default (opt-in BTMiner v3 :4433 controls)",
                        version=parsed["version"],
                        algorithm="sha256d",
                        hashrate_hs=parsed["hashrate_hs"],
                        hashrate_unit_kind="hash",
                        asic_temp_c=parsed["asic_temp_c"],
                        fan_rpms=parsed["fan_rpms"],
                        pool_url=parsed["pool_url"],
                        suggested_name=f"whatsminer-{last_octet}",
                        detection="BTMiner v3 :4433",
                        capabilities=self.capabilities,
                    )
            except Exception:
                pass
        return None

    def fetch_telemetry(
        self,
        ip: str,
        cgminer_port: int = 4028,
        v3_port: int = 4433,
        timeout: float = 4.0,
    ) -> dict[str, Any]:
        """Fetch live telemetry from BTMiner v2 :4028 with v3 :4433 fallback."""
        if tcp_open(ip, cgminer_port, min(timeout, 1.5)):
            try:
                ver = cgminer_request(ip, cgminer_port, "version", timeout=timeout)
                summ = cgminer_request(ip, cgminer_port, "summary", timeout=timeout)
                if is_whatsminer_payload(ver, summ):
                    devs = pools = psu = errs = None
                    for cmd, tgt in (("devs", "d"), ("pools", "p"), ("get_psu", "u"), ("get_error_code", "e")):
                        try:
                            res = cgminer_request(ip, cgminer_port, cmd, timeout=timeout)
                            if tgt == "d":
                                devs = res
                            elif tgt == "p":
                                pools = res
                            elif tgt == "u":
                                psu = res
                            else:
                                errs = res
                        except Exception:
                            pass
                    out = parse_whatsminer_v2(ver, summ, devs, pools, psu, errs)
                    out["source"] = f"BTMiner v2 :{cgminer_port}"
                    return out
            except Exception:
                pass
        info = whatsminer_v3_request(ip, v3_port, "get.device.info", timeout=timeout)
        summ = whatsminer_v3_request(ip, v3_port, "get.miner.status", "summary", timeout=timeout)
        pools = None
        try:
            pools = whatsminer_v3_request(ip, v3_port, "get.miner.status", "pools", timeout=timeout)
        except Exception:
            pass
        out = parse_whatsminer_v3(info, summ, pools)
        out["source"] = f"BTMiner v3 :{v3_port}"
        return out

    def v3_write(
        self,
        ip: str,
        password: str,
        cmd: str,
        param: Any = None,
        *,
        account: str = "super",
        v3_port: int = 4433,
        timeout: float = 5.0,
    ) -> tuple[bool, str]:
        if not password:
            return False, "Whatsminer admin password is required for BTMiner v3 write commands"
        info = whatsminer_v3_request(ip, v3_port, "get.device.info", timeout=timeout)
        msg = info.get("msg") if isinstance(info, dict) and isinstance(info.get("msg"), dict) else {}
        salt = str(msg.get("salt") or "")
        if not salt:
            return False, "Whatsminer v3 get.device.info did not return a signing salt"
        ts = int(time.time())
        acct = account.strip() or "super"
        tok = make_whatsminer_v3_token(cmd, salt, password, ts, account=acct, param=param)
        res = whatsminer_v3_request(ip, v3_port, cmd, param, ts=ts, account=acct, token=tok, timeout=timeout)
        if isinstance(res, dict) and res.get("code") == 0:
            return True, f"Whatsminer {cmd} applied"
        return False, f"Whatsminer {cmd} failed: {res.get('desc') or res.get('msg') or res}"

    def set_power_mode(self, ip: str, password: str, mode: str, *, account: str = "super", v3_port: int = 4433, timeout: float = 5.0) -> tuple[bool, str]:
        m = str(mode or "").strip().lower()
        if m not in {"low", "normal", "high"}:
            return False, "Whatsminer power mode must be low, normal, or high"
        return self.v3_write(ip, password, "set.miner.power_mode", m, account=account, v3_port=v3_port, timeout=timeout)

    def set_power_pct(self, ip: str, password: str, pct: int, *, account: str = "super", v3_port: int = 4433, timeout: float = 5.0) -> tuple[bool, str]:
        p = max(10, min(110, int(pct)))
        return self.v3_write(ip, password, "set.miner.power_percent", str(p), account=account, v3_port=v3_port, timeout=timeout)

    def suspend(self, ip: str, password: str, suspend: bool, *, account: str = "super", v3_port: int = 4433, timeout: float = 5.0) -> tuple[bool, str]:
        return self.v3_write(ip, password, "set.miner.suspend", "true" if suspend else "false", account=account, v3_port=v3_port, timeout=timeout)

    def reboot(self, ip: str, password: str, *, account: str = "super", v3_port: int = 4433, timeout: float = 5.0) -> tuple[bool, str]:
        return self.v3_write(ip, password, "set.miner.service", "reboot", account=account, v3_port=v3_port, timeout=timeout)
