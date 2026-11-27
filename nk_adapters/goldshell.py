"""Goldshell ASIC adapter (Mini Doge, KA Box, AL Box, KD Box, LT5/LT6).

Uses HTTP :80 /user/login (JWT Bearer token) + /mcb/* REST API.
Monitor-only by default; opt-in controls require user-supplied credentials and
write_enabled=True. NEVER tries vendor default passwords automatically.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional
from urllib.parse import urlencode

from .base import (
    AdapterCapabilities,
    ProbeResult,
    http_json,
    http_request_raw,
    num,
    opt_int,
    tcp_open,
    to_hs,
    unit_kind_for_algorithm,
)


def infer_goldshell_algorithm(model_or_algo: str) -> tuple[str, str]:
    s = str(model_or_algo or "").lower()
    if any(k in s for k in ("ka", "kheavy", "kas")):
        return "kheavyhash", "GH/s"
    if any(k in s for k in ("al", "aleph", "blake3")):
        return "blake3", "GH/s"
    if any(k in s for k in ("doge", "lt", "scrypt", "dg")):
        return "scrypt", "MH/s"
    if any(k in s for k in ("kd", "kadena", "blake2s")):
        return "sha256d", "TH/s"
    return "scrypt", "MH/s"


def parse_goldshell_status(
    status_data: Any,
    pools_data: Any = None,
    setting_data: Any = None,
    devs_data: Any = None,
) -> dict[str, Any]:
    st = status_data if isinstance(status_data, dict) else {}
    setd = setting_data if isinstance(setting_data, dict) else {}

    model = str(st.get("model") or st.get("name") or setd.get("model") or "Goldshell Miner")
    algo_hint = str(st.get("algo") or st.get("algorithm") or model)
    algo, default_unit = infer_goldshell_algorithm(algo_hint)
    ukind = unit_kind_for_algorithm(algo)

    hr_raw = num(st, "hashrate", "hash_rate", "rate_5s", "avghashrate", "realtime_hashrate")
    unit = str(st.get("unit") or st.get("hashrate_unit") or "")
    if hr_raw is None and isinstance(devs_data, dict):
        dlist = devs_data.get("DEVS") if isinstance(devs_data.get("DEVS"), list) else []
        if dlist:
            hr_raw = sum(num(d, "MHS 5s", "MHS av") or 0.0 for d in dlist if isinstance(d, dict))
            unit = unit or "MH/s"
    if hr_raw is not None:
        eff_unit = unit or ("H/s" if float(hr_raw) >= 1e7 else default_unit)
        hs = to_hs(hr_raw, eff_unit, algo)
    else:
        hs = None

    temps: list[float] = []
    t_top = num(st, "temp", "max_temp", "temperature")
    if t_top is not None and 0 < t_top < 150:
        temps.append(t_top)
    if isinstance(st.get("boards"), list):
        for b in st["boards"]:
            if isinstance(b, dict):
                bt = num(b, "temp", "chip_temp", "max_temp")
                if bt is not None and 0 < bt < 150:
                    temps.append(bt)

    fans: list[int] = []
    if isinstance(st.get("fans"), list):
        for f in st["fans"]:
            if isinstance(f, dict):
                fv = opt_int(f, "speed", "rpm")
            elif isinstance(f, (int, float)):
                fv = int(round(f))
            else:
                fv = None
            if fv is not None and fv >= 0:
                fans.append(fv)

    pools: list[dict[str, Any]] = []
    plist = pools_data if isinstance(pools_data, list) else (
        pools_data.get("pools") if isinstance(pools_data, dict) and isinstance(pools_data.get("pools"), list) else []
    )
    for idx, p in enumerate(plist):
        if isinstance(p, dict):
            pools.append({
                "pool": idx,
                "url": str(p.get("url") or p.get("URL") or ""),
                "user": str(p.get("user") or p.get("User") or ""),
                "status": str(p.get("status") or ("Alive" if p.get("active") else "")),
                "stratum_active": bool(p.get("active")) or idx == 0,
            })

    workmode = str(setd.get("level") if setd.get("level") is not None else (setd.get("power_mode") or st.get("workmode") or ""))
    return {
        "model": model,
        "firmware": "Goldshell OS",
        "version": str(st.get("firmware") or st.get("version") or st.get("softver") or ""),
        "algorithm": algo,
        "hashrate_unit_kind": ukind,
        "hashrate_hs": hs,
        "asic_temp_c": max(temps) if temps else None,
        "chip_temps": temps,
        "power_w": num(st, "power", "power_w", "watt"),
        "fan_rpms": fans,
        "uptime_sec": opt_int(st, "uptime", "elapsed"),
        "tuner_mode": workmode,
        "pool_url": pools[0]["url"] if pools else "",
        "pool_user": pools[0]["user"] if pools else "",
        "pools": pools,
    }


class GoldshellAdapter:
    family_id = "goldshell"
    display_name = "Goldshell (/mcb/* JWT REST)"
    default_algorithms = ("scrypt", "kheavyhash", "blake3")
    capabilities = AdapterCapabilities(
        monitor=True,
        restart=True,
        workmode=True,
    )

    def login(self, ip: str, port: int, username: str, password: str, timeout: float = 4.0) -> str:
        """Authenticate with user-supplied Goldshell credentials and return JWT token."""
        if not username or not password:
            return ""
        base = f"http://{ip}" if int(port) == 80 else f"http://{ip}:{port}"
        for cipher, pw in (("false", password), ("true", hashlib.md5(password.encode("utf-8")).hexdigest())):
            qs = urlencode({"username": username, "password": pw, "cipher": cipher})
            st, data, _, _ = http_json("GET", f"{base}/user/login?{qs}", timeout=timeout)
            if st == 200 and isinstance(data, dict):
                tok = str(data.get("JWT Token") or data.get("token") or data.get("jwt") or "").strip()
                if tok:
                    return tok
        return ""

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
        st, data, rh, _ = http_json("GET", f"{base}/mcb/status", timeout=http_timeout)
        if st == 200 and isinstance(data, dict) and any(k in data for k in ("model", "hashrate", "algo", "boards", "power")):
            _, pools, _, _ = http_json("GET", f"{base}/mcb/pools", timeout=http_timeout)
            _, setting, _, _ = http_json("GET", f"{base}/mcb/setting", timeout=http_timeout)
            parsed = parse_goldshell_status(data, pools, setting)
            return ProbeResult(
                family=self.family_id,
                device_type="antminer",
                ip=ip,
                port=int(http_port),
                cgminer_port=int(cgminer_port),
                model=parsed["model"],
                firmware=parsed["firmware"],
                firmware_note="Monitor-only by default (opt-in Goldshell /mcb/setting workmode)",
                version=parsed["version"],
                algorithm=parsed["algorithm"],
                hashrate_hs=parsed["hashrate_hs"],
                hashrate_unit_kind=parsed["hashrate_unit_kind"],
                asic_temp_c=parsed["asic_temp_c"],
                fan_rpms=parsed["fan_rpms"],
                pool_url=parsed["pool_url"],
                suggested_name=f"goldshell-{last_octet}",
                detection=f"HTTP {base}/mcb/status",
                capabilities=self.capabilities,
            )
        if st in {401, 403}:
            st_r, body_r, _ = http_request_raw("GET", f"{base}/", timeout=http_timeout)
            if st_r == 200 and b"goldshell" in (body_r or b"").lower():
                return ProbeResult(
                    family=self.family_id,
                    device_type="antminer",
                    ip=ip,
                    port=int(http_port),
                    cgminer_port=int(cgminer_port),
                    model="Goldshell Miner",
                    firmware="Goldshell OS",
                    firmware_note="Goldshell web auth required (enter login on device card)",
                    algorithm="scrypt",
                    hashrate_unit_kind="hash",
                    auth_required=True,
                    suggested_name=f"goldshell-{last_octet}",
                    detection=f"HTTP 401 Goldshell on :{http_port}",
                    capabilities=self.capabilities,
                )
        return None

    def fetch_telemetry(
        self,
        ip: str,
        port: int = 80,
        username: str = "",
        password: str = "",
        token: str = "",
        timeout: float = 4.0,
    ) -> dict[str, Any]:
        base = f"http://{ip}" if int(port) == 80 else f"http://{ip}:{port}"
        hdrs: dict[str, str] = {}
        tok = token or (self.login(ip, port, username, password, timeout=timeout) if username and password else "")
        if tok:
            hdrs["Authorization"] = f"Bearer {tok}"
        st, status_data, _, _ = http_json("GET", f"{base}/mcb/status", headers=hdrs, timeout=timeout)
        if st in {401, 403} and username and password and not token:
            tok = self.login(ip, port, username, password, timeout=timeout)
            if tok:
                hdrs["Authorization"] = f"Bearer {tok}"
                st, status_data, _, _ = http_json("GET", f"{base}/mcb/status", headers=hdrs, timeout=timeout)
        if st != 200 or not isinstance(status_data, dict):
            raise RuntimeError(f"Goldshell /mcb/status returned HTTP {st}")
        _, pools, _, _ = http_json("GET", f"{base}/mcb/pools", headers=hdrs, timeout=timeout)
        _, setting, _, _ = http_json("GET", f"{base}/mcb/setting", headers=hdrs, timeout=timeout)
        _, devs, _, _ = http_json("GET", f"{base}/mcb/cgminer?cgminercmd=devs", headers=hdrs, timeout=timeout)
        out = parse_goldshell_status(status_data, pools, setting, devs)
        out["source"] = f"Goldshell HTTP :{port}"
        return out

    def set_workmode(self, ip: str, port: int, username: str, password: str, level: str, timeout: float = 5.0) -> tuple[bool, str]:
        tok = self.login(ip, port, username, password, timeout=timeout)
        if not tok:
            return False, "Goldshell login failed (check web username and password)"
        base = f"http://{ip}" if int(port) == 80 else f"http://{ip}:{port}"
        val: Any = int(level) if str(level).strip().isdigit() else str(level).strip()
        st, data, _, _ = http_json(
            "PUT", f"{base}/mcb/setting",
            headers={"Authorization": f"Bearer {tok}"},
            json_body={"level": val},
            timeout=timeout,
        )
        if 200 <= st < 300:
            return True, f"Goldshell workmode level={val} applied"
        return False, f"Goldshell PUT /mcb/setting failed: HTTP {st} {data or ''}".strip()

    def reboot(self, ip: str, port: int, username: str, password: str, timeout: float = 5.0) -> tuple[bool, str]:
        tok = self.login(ip, port, username, password, timeout=timeout)
        if not tok:
            return False, "Goldshell login failed (check web username and password)"
        base = f"http://{ip}" if int(port) == 80 else f"http://{ip}:{port}"
        st, _, _, _ = http_json("PUT", f"{base}/mcb/restart", headers={"Authorization": f"Bearer {tok}"}, json_body={}, timeout=timeout)
        if 200 <= st < 300:
            return True, "Goldshell restart command sent"
        return False, f"Goldshell restart failed: HTTP {st}"
