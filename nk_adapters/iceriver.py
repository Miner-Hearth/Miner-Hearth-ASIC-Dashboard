"""IceRiver ASIC adapter (KS0..KS5L Kaspa + AL0/AL3 Alephium).

Supports:
1. Stock firmware HTTP :80 POST /user/userpanel (post=4) JSON telemetry.
2. Community IceRiver-OC firmware: CGMiner TCP :4028 + HTTPS :443 /api/overview
   (Bearer token auth) with opt-in controls (fan/OC/reboot) when write_enabled=True.
NEVER tries vendor default passwords automatically.
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import urlencode

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
)


def infer_iceriver_algorithm(model: str) -> tuple[str, str]:
    s = str(model or "").lower()
    if any(k in s for k in ("al0", "al1", "al2", "al3", "aleph", "blake3")):
        return "blake3", "GH/s"
    return "kheavyhash", "GH/s"


def is_iceriver_cgminer(version_data: Any, summary_data: Any = None, devs_data: Any = None) -> bool:
    hay = f"{version_data or ''} {summary_data or ''} {devs_data or ''}".lower()
    return any(tok in hay for tok in ("iceriver", "ks0", "ks1", "ks2", "ks3", "ks5", "al0", "al3"))


def _parse_pow_value(raw_pow: Any, raw_unit: str, default_unit: str, algo: str) -> Optional[float]:
    if raw_pow is None:
        return None
    if isinstance(raw_pow, (int, float)):
        return to_hs(float(raw_pow), raw_unit or default_unit, algo)
    s = str(raw_pow).strip()
    if not s:
        return None
    parts = s.split()
    try:
        val = float(parts[0])
    except ValueError:
        return None
    unit = parts[1] if len(parts) > 1 else (raw_unit or default_unit)
    return to_hs(val, unit, algo)


def parse_iceriver_userpanel(payload: Any) -> dict[str, Any]:
    """Parse stock IceRiver POST /user/userpanel (post=4) JSON payload."""
    root = payload if isinstance(payload, dict) else {}
    data = root.get("data") if isinstance(root.get("data"), dict) else root
    model = str(data.get("model") or data.get("type") or "IceRiver Miner")
    algo, def_unit = infer_iceriver_algorithm(model)
    unit = str(data.get("unit") or data.get("pow_unit") or def_unit)

    hs = _parse_pow_value(data.get("pow") or data.get("rtpow") or data.get("hashrate"), unit, def_unit, algo)
    if hs is None:
        hs = _parse_pow_value(data.get("avgpow"), unit, def_unit, algo)

    chip_temps: list[float] = []
    board_temps: list[float] = []
    boards = data.get("boards") if isinstance(data.get("boards"), list) else []
    for b in boards:
        if not isinstance(b, dict):
            continue
        for tk in ("outtmp", "outtemp", "chip_temp", "temp"):
            tv = num(b, tk)
            if tv is not None and 0 < tv < 150:
                chip_temps.append(tv)
        it = num(b, "intmp", "intemp", "board_temp")
        if it is not None and 0 < it < 150:
            board_temps.append(it)

    fans: list[int] = []
    raw_fans = data.get("fans")
    if isinstance(raw_fans, list):
        for f in raw_fans:
            if isinstance(f, dict):
                fv = opt_int(f, "speed", "rpm")
            elif isinstance(f, (int, float, str)):
                try:
                    fv = int(round(float(f)))
                except ValueError:
                    fv = None
            else:
                fv = None
            if fv is not None and fv >= 0:
                fans.append(fv)

    pools: list[dict[str, Any]] = []
    plist = data.get("pools") if isinstance(data.get("pools"), list) else []
    for idx, p in enumerate(plist):
        if isinstance(p, dict):
            pools.append({
                "pool": idx,
                "url": str(p.get("addr") or p.get("url") or ""),
                "user": str(p.get("user") or ""),
                "status": str(p.get("state") or p.get("status") or ""),
                "stratum_active": str(p.get("connect") or p.get("state") or "").lower() in {"1", "true", "alive", "connected"} or idx == 0,
            })

    return {
        "model": model,
        "firmware": "IceRiver Stock",
        "version": str(data.get("softver") or data.get("version") or ""),
        "algorithm": algo,
        "hashrate_unit_kind": "hash",
        "hashrate_hs": hs,
        "asic_temp_c": max(chip_temps) if chip_temps else (max(board_temps) if board_temps else num(data, "temp")),
        "chip_temps": chip_temps,
        "board_temps": board_temps,
        "power_w": num(data, "power", "power_w"),
        "fan_rpms": fans,
        "uptime_sec": opt_int(data, "runtime", "uptime"),
        "pool_url": pools[0]["url"] if pools else "",
        "pool_user": pools[0]["user"] if pools else "",
        "pools": pools,
    }


def parse_iceriver_oc_overview(payload: Any) -> dict[str, Any]:
    """Parse IceRiver-OC HTTPS :443 /api/overview JSON payload."""
    d = payload if isinstance(payload, dict) else {}
    model = str(d.get("model") or d.get("miner") or "IceRiver-OC")
    algo, def_unit = infer_iceriver_algorithm(model)
    hr = num(d, "hashrate_ghs", "hashrate", "rtpow")
    unit = str(d.get("unit") or ("GH/s" if "hashrate_ghs" in d else def_unit))
    hs = to_hs(hr, unit, algo) if hr is not None else None
    temps: list[float] = []
    if isinstance(d.get("temps"), list):
        temps = [float(x) for x in d["temps"] if isinstance(x, (int, float)) and 0 < x < 150]
    elif num(d, "max_temp", "temp") is not None:
        temps = [float(num(d, "max_temp", "temp"))]
    fans = [int(round(float(x))) for x in (d.get("fans") or []) if isinstance(x, (int, float)) and x >= 0] if isinstance(d.get("fans"), list) else []
    pools: list[dict[str, Any]] = []
    for idx, p in enumerate(d.get("pools") or [] if isinstance(d.get("pools"), list) else []):
        if isinstance(p, dict):
            pools.append({
                "pool": idx,
                "url": str(p.get("url") or p.get("addr") or ""),
                "user": str(p.get("user") or ""),
                "status": str(p.get("status") or ""),
                "stratum_active": bool(p.get("active")) or idx == 0,
            })
    return {
        "model": model,
        "firmware": "IceRiver-OC",
        "version": str(d.get("version") or d.get("fw_version") or ""),
        "algorithm": algo,
        "hashrate_unit_kind": "hash",
        "hashrate_hs": hs,
        "asic_temp_c": max(temps) if temps else None,
        "chip_temps": temps,
        "power_w": num(d, "power_w", "power"),
        "fan_rpms": fans,
        "uptime_sec": opt_int(d, "uptime", "runtime"),
        "tuner_mode": str(d.get("preset") or d.get("fan_mode") or ""),
        "pool_url": pools[0]["url"] if pools else "",
        "pool_user": pools[0]["user"] if pools else "",
        "pools": pools,
    }


class IceRiverAdapter:
    family_id = "iceriver"
    display_name = "IceRiver (KS0–KS5L / AL0 / IceRiver-OC)"
    default_algorithms = ("kheavyhash", "blake3")
    capabilities = AdapterCapabilities(
        monitor=True,
        restart=True,
        workmode=True,
        freq_tuning=True,
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
        # 1. IceRiver-OC CGMiner :4028
        if tcp_open(ip, cgminer_port, tcp_timeout):
            try:
                ver = cgminer_request(ip, cgminer_port, "version", timeout=http_timeout)
                summ = cgminer_request(ip, cgminer_port, "summary", timeout=http_timeout)
                if is_iceriver_cgminer(ver, summ):
                    v0 = (ver.get("VERSION") or [{}])[0] if isinstance(ver.get("VERSION"), list) else {}
                    s0 = (summ.get("SUMMARY") or [{}])[0] if isinstance(summ.get("SUMMARY"), list) else {}
                    model = str(v0.get("Miner") or v0.get("Type") or "IceRiver-OC")
                    algo, def_unit = infer_iceriver_algorithm(model)
                    ghs = num(s0, "GHS 5s", "GHS av")
                    mhs = num(s0, "MHS 5s", "MHS av")
                    hs = to_hs(ghs, "GH/s", algo) if ghs is not None else to_hs(mhs, "MH/s", algo)
                    return ProbeResult(
                        family=self.family_id,
                        device_type="antminer",
                        ip=ip,
                        port=int(http_port),
                        cgminer_port=int(cgminer_port),
                        model=model,
                        firmware="IceRiver-OC",
                        firmware_note="Monitor-only by default (opt-in IceRiver-OC :443 Bearer controls)",
                        version=str(v0.get("CompileTime") or ""),
                        algorithm=algo,
                        hashrate_hs=hs,
                        hashrate_unit_kind="hash",
                        suggested_name=f"iceriver-{last_octet}",
                        detection=f"IceRiver-OC CGMiner :{cgminer_port}",
                        capabilities=self.capabilities,
                    )
            except Exception:
                pass

        # 2. Stock IceRiver HTTP :80 POST /user/userpanel (post=4)
        if tcp_open(ip, http_port, tcp_timeout):
            base = f"http://{ip}" if int(http_port) == 80 else f"http://{ip}:{http_port}"
            try:
                st, raw_b, _ = http_request_raw(
                    "POST",
                    f"{base}/user/userpanel",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    body=b"post=4",
                    timeout=http_timeout,
                )
                if st == 200 and raw_b:
                    parsed_raw = json.loads(raw_b.decode("utf-8", errors="replace"))
                    if isinstance(parsed_raw, dict) and (parsed_raw.get("error") == 0 or "data" in parsed_raw):
                        data_obj = parsed_raw.get("data") if isinstance(parsed_raw.get("data"), dict) else parsed_raw
                        if any(k in data_obj for k in ("pow", "boards", "softver")):
                            out = parse_iceriver_userpanel(parsed_raw)
                            return ProbeResult(
                                family=self.family_id,
                                device_type="antminer",
                                ip=ip,
                                port=int(http_port),
                                cgminer_port=int(cgminer_port),
                                model=out["model"],
                                firmware=out["firmware"],
                                firmware_note="Monitor-only by default (opt-in IceRiver controls)",
                                version=out["version"],
                                algorithm=out["algorithm"],
                                hashrate_hs=out["hashrate_hs"],
                                hashrate_unit_kind="hash",
                                asic_temp_c=out["asic_temp_c"],
                                fan_rpms=out["fan_rpms"],
                                pool_url=out["pool_url"],
                                suggested_name=f"iceriver-{last_octet}",
                                detection=f"HTTP POST {base}/user/userpanel",
                                capabilities=self.capabilities,
                            )
            except Exception:
                pass

        # 3. IceRiver-OC HTTPS :443 /api/overview check
        if tcp_open(ip, 443, tcp_timeout):
            try:
                st443, data443, _, _ = http_json(
                    "GET", f"https://{ip}/api/overview", timeout=http_timeout, allow_insecure_tls=True
                )
                if st443 in {200, 401, 403}:
                    if st443 == 200 and isinstance(data443, dict):
                        out = parse_iceriver_oc_overview(data443)
                        return ProbeResult(
                            family=self.family_id,
                            device_type="antminer",
                            ip=ip,
                            port=443,
                            cgminer_port=int(cgminer_port),
                            model=out["model"],
                            firmware=out["firmware"],
                            firmware_note="Monitor-only by default (opt-in IceRiver-OC :443 controls)",
                            version=out["version"],
                            algorithm=out["algorithm"],
                            hashrate_hs=out["hashrate_hs"],
                            hashrate_unit_kind="hash",
                            asic_temp_c=out["asic_temp_c"],
                            fan_rpms=out["fan_rpms"],
                            pool_url=out["pool_url"],
                            suggested_name=f"iceriver-{last_octet}",
                            detection="HTTPS :443 /api/overview",
                            capabilities=self.capabilities,
                        )
            except Exception:
                pass
        return None

    def fetch_telemetry(
        self,
        ip: str,
        port: int = 80,
        cgminer_port: int = 4028,
        api_token: str = "",
        username: str = "",
        password: str = "",
        timeout: float = 4.0,
    ) -> dict[str, Any]:
        # Prefer IceRiver-OC HTTPS :443 when bearer token is provided
        if api_token:
            st, data, _, _ = http_json(
                "GET", f"https://{ip}/api/overview",
                headers={"Authorization": f"Bearer {api_token}"},
                timeout=timeout, allow_insecure_tls=True,
            )
            if st == 200 and isinstance(data, dict):
                out = parse_iceriver_oc_overview(data)
                out["source"] = "IceRiver-OC HTTPS :443"
                return out

        base = f"http://{ip}" if int(port) == 80 else f"http://{ip}:{port}"
        hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
        if username and password:
            login_body = urlencode({"user": username, "pwd": password}).encode("utf-8")
            _, _, lhdrs = http_request_raw("POST", f"{base}/user/login", headers=hdrs, body=login_body, timeout=timeout)
            cookie = lhdrs.get("set-cookie", "")
            if cookie:
                hdrs["Cookie"] = cookie.split(";", 1)[0]
        st, raw_b, _ = http_request_raw("POST", f"{base}/user/userpanel", headers=hdrs, body=b"post=4", timeout=timeout)
        if st == 200 and raw_b:
            parsed = json.loads(raw_b.decode("utf-8", errors="replace"))
            out = parse_iceriver_userpanel(parsed)
            out["source"] = f"IceRiver HTTP :{port}"
            return out
        raise RuntimeError(f"IceRiver /user/userpanel returned HTTP {st}")

    def oc_apply_settings(
        self,
        ip: str,
        api_token: str,
        payload: dict[str, Any],
        timeout: float = 5.0,
    ) -> tuple[bool, str]:
        if not api_token:
            return False, "IceRiver-OC Bearer token is required for :443 write controls"
        st, data, _, _ = http_json(
            "POST", f"https://{ip}/api/settings",
            headers={"Authorization": f"Bearer {api_token}"},
            json_body=payload,
            timeout=timeout, allow_insecure_tls=True,
        )
        if 200 <= st < 300:
            return True, f"IceRiver-OC settings updated ({payload})"
        return False, f"IceRiver-OC /api/settings failed: HTTP {st} {data or ''}".strip()

    def reboot(
        self,
        ip: str,
        port: int = 80,
        api_token: str = "",
        timeout: float = 5.0,
    ) -> tuple[bool, str]:
        if api_token:
            st, _, _, _ = http_json(
                "POST", f"https://{ip}/api/reboot",
                headers={"Authorization": f"Bearer {api_token}"},
                json_body={},
                timeout=timeout, allow_insecure_tls=True,
            )
            if 200 <= st < 300:
                return True, "IceRiver-OC reboot command sent"
            return False, f"IceRiver-OC reboot failed: HTTP {st}"
        base = f"http://{ip}" if int(port) == 80 else f"http://{ip}:{port}"
        st, _, _ = http_request_raw(
            "POST", f"{base}/user/userpanel",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=b"post=5",
            timeout=timeout,
        )
        if 200 <= st < 300:
            return True, "IceRiver reboot command sent"
        return False, f"IceRiver reboot failed: HTTP {st}"
