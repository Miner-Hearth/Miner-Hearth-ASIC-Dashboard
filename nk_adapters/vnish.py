"""VNish firmware adapter (:80 /api/v1/* + :4028 fallback; monitor-only by default)."""

from __future__ import annotations

from typing import Any, Optional

from .base import (
    AdapterCapabilities,
    ProbeResult,
    cgminer_request,
    http_json,
    num,
    opt_int,
    tcp_open,
    to_hs,
)


class VnishAdapter:
    family_id = "vnish"
    display_name = "VNish Firmware (Antminer S19/S21/L7)"
    default_algorithms = ("sha256d", "scrypt")
    capabilities = AdapterCapabilities(
        monitor=True,
        faults=True,
        logs=True,
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
        tcp_timeout: float = 1.0,
        http_timeout: float = 4.0,
    ) -> Optional[ProbeResult]:
        last_octet = ip.split(".")[-1]
        if tcp_open(ip, http_port, tcp_timeout):
            base = f"http://{ip}" if int(http_port) == 80 else f"http://{ip}:{http_port}"
            st, info, _, _ = http_json("GET", f"{base}/api/v1/info", timeout=http_timeout)
            if st == 200 and isinstance(info, dict) and (
                "vnish" in str(info).lower() or ("miner" in info and "fw_version" in info)
            ):
                _, summ, _, _ = http_json("GET", f"{base}/api/v1/summary", timeout=http_timeout)
                parsed = self.parse_http_payloads(info, summ if isinstance(summ, dict) else None)
                return ProbeResult(
                    family="vnish",
                    device_type="antminer",
                    ip=ip,
                    port=int(http_port),
                    cgminer_port=int(cgminer_port),
                    model=parsed["model"],
                    firmware="VNish",
                    firmware_note="Monitor-only by default (opt-in VNish /api/v1 presets/throttle with user password)",
                    version=parsed["version"],
                    algorithm=parsed["algorithm"],
                    hashrate_hs=parsed["hashrate_hs"],
                    hashrate_unit_kind="hash",
                    asic_temp_c=parsed["asic_temp_c"],
                    fan_rpms=parsed["fan_rpms"],
                    pool_url=parsed["pool_url"],
                    suggested_name=f"vnish-{last_octet}",
                    detection=f"VNish HTTP :{http_port}/api/v1/info",
                    capabilities=self.capabilities,
                )

        if tcp_open(ip, cgminer_port, tcp_timeout):
            try:
                ver = cgminer_request(ip, cgminer_port, "version", timeout=http_timeout)
                if "vnish" in str(ver).lower():
                    summ = cgminer_request(ip, cgminer_port, "summary", timeout=http_timeout)
                    s0 = (summ.get("SUMMARY") or [{}])[0] if isinstance(summ.get("SUMMARY"), list) else {}
                    v0 = (ver.get("VERSION") or [{}])[0] if isinstance(ver.get("VERSION"), list) else {}
                    ghs = num(s0, "GHS 5s", "GHS av")
                    hs = to_hs(ghs, "GH/s", "sha256d") if ghs is not None else to_hs(num(s0, "MHS 5s", "MHS av"), "MH/s", "sha256d")
                    return ProbeResult(
                        family="vnish",
                        device_type="antminer",
                        ip=ip,
                        port=int(http_port),
                        cgminer_port=int(cgminer_port),
                        model=str(v0.get("Miner") or "VNish Antminer"),
                        firmware="VNish",
                        firmware_note="Monitor-only by default (opt-in VNish REST control)",
                        version=str(v0.get("CGMiner") or ""),
                        algorithm="sha256d",
                        hashrate_hs=hs,
                        hashrate_unit_kind="hash",
                        suggested_name=f"vnish-{last_octet}",
                        detection=f"VNish CGMiner :{cgminer_port}",
                        capabilities=self.capabilities,
                    )
            except Exception:
                pass
        return None

    @staticmethod
    def parse_http_payloads(info: Optional[dict[str, Any]], summary: Optional[dict[str, Any]]) -> dict[str, Any]:
        inf = info if isinstance(info, dict) else {}
        sum_root = summary if isinstance(summary, dict) else {}
        miner = sum_root.get("miner") if isinstance(sum_root.get("miner"), dict) else sum_root

        algo_raw = str(inf.get("algorithm") or miner.get("algorithm") or "").lower()
        algo = "scrypt" if "scrypt" in algo_raw else "sha256d"

        model = str(inf.get("miner") or inf.get("model") or miner.get("miner_type") or "VNish Antminer")
        fw_ver = str(inf.get("fw_version") or inf.get("version") or "")

        # VNish /api/v1/summary typically exposes hr_realtime in GH/s (or MH/s on Scrypt) or hashrate_unit
        unit = str(miner.get("hashrate_unit") or ("MH/s" if algo == "scrypt" else "GH/s"))
        hr_val = num(miner, "hr_realtime", "hr_average", "hashrate")
        if hr_val is None:
            # Fallback to average_hashrate in TH/s if present
            th = num(miner, "average_hashrate", "instant_hashrate")
            hs = to_hs(th, "TH/s", algo) if th is not None else None
        else:
            hs = to_hs(hr_val, unit, algo)

        chip_temps: list[float] = []
        board_temps: list[float] = []
        chains = miner.get("chains") if isinstance(miner.get("chains"), list) else []
        for ch in chains:
            if not isinstance(ch, dict):
                continue
            ct = ch.get("temp_chip")
            if isinstance(ct, list):
                chip_temps.extend(float(x) for x in ct if isinstance(x, (int, float)) and x > 0)
            elif isinstance(ct, (int, float)) and ct > 0:
                chip_temps.append(float(ct))
            bt = ch.get("temp_pcb")
            if isinstance(bt, list):
                board_temps.extend(float(x) for x in bt if isinstance(x, (int, float)) and x > 0)
            elif isinstance(bt, (int, float)) and bt > 0:
                board_temps.append(float(bt))

        if not chip_temps:
            ct_top = num(miner.get("chip_temp") if isinstance(miner.get("chip_temp"), dict) else {}, "max", "avg")
            if ct_top is not None:
                chip_temps.append(ct_top)

        fan_rpms: list[int] = []
        cooling = miner.get("cooling") if isinstance(miner.get("cooling"), dict) else {}
        fans = cooling.get("fans") if isinstance(cooling.get("fans"), list) else (miner.get("fans") if isinstance(miner.get("fans"), list) else [])
        for f in fans:
            if isinstance(f, dict):
                rpm = opt_int(f, "rpm")
                if rpm is not None:
                    fan_rpms.append(rpm)

        power_w = num(miner, "power_consumption", "power_usage", "power")
        pools_raw = miner.get("pools") if isinstance(miner.get("pools"), list) else []
        pools = []
        for idx, p in enumerate(pools_raw):
            if isinstance(p, dict):
                pools.append({
                    "pool": idx,
                    "url": str(p.get("url") or ""),
                    "user": str(p.get("user") or ""),
                    "status": str(p.get("status") or ""),
                    "accepted": opt_int(p, "accepted") or 0,
                    "rejected": opt_int(p, "rejected") or 0,
                })

        status_obj = miner.get("miner_status") if isinstance(miner.get("miner_status"), dict) else {}
        return {
            "model": model,
            "firmware": "VNish",
            "version": fw_ver,
            "algorithm": algo,
            "hashrate_hs": hs,
            "asic_temp_c": max(chip_temps) if chip_temps else (max(board_temps) if board_temps else None),
            "chip_temps": chip_temps,
            "board_temps": board_temps,
            "fan_rpms": fan_rpms,
            "power_w": power_w,
            "uptime_sec": num(miner, "uptime") or num(sum_root, "uptime"),
            "pool_url": pools[0]["url"] if pools else "",
            "pool_user": pools[0]["user"] if pools else "",
            "pools": pools,
            "miner_state": str(status_obj.get("miner_state") or ""),
            "preset": str(miner.get("preset") or (miner.get("autotune") or {}).get("preset") or ""),
        }

    @staticmethod
    def unlock(ip: str, port: int, password: str, timeout: float = 5.0) -> tuple[bool, str]:
        if not password:
            return False, "VNish password is required (never uses default passwords)"
        base = f"http://{ip}" if int(port) == 80 else f"http://{ip}:{port}"
        st, data, _, _ = http_json("POST", f"{base}/api/v1/unlock", json_body={"pw": password}, timeout=timeout)
        if st == 200 and isinstance(data, dict) and data.get("token"):
            return True, str(data["token"])
        return False, f"VNish unlock failed (HTTP {st})"

    def set_throttle(self, ip: str, port: int, password: str, pct: int, timeout: float = 5.0) -> tuple[bool, str]:
        pct_i = int(pct)
        if pct_i < 20 or pct_i > 100 or pct_i % 5 != 0:
            return False, "VNish throttle must be 20–100% in 5% steps"
        ok, tok = self.unlock(ip, port, password, timeout=timeout)
        if not ok:
            return False, tok
        base = f"http://{ip}" if int(port) == 80 else f"http://{ip}:{port}"
        st, _, _, _ = http_json(
            "POST",
            f"{base}/api/v1/settings",
            headers={"Authorization": tok},
            json_body={"miner": {"overclock": {"throttle": pct_i}}},
            timeout=timeout,
        )
        return (200 <= st < 300), (f"VNish throttle set to {pct_i}%" if 200 <= st < 300 else f"VNish settings HTTP {st}")

    def set_preset(self, ip: str, port: int, password: str, preset: str, timeout: float = 5.0) -> tuple[bool, str]:
        if not str(preset or "").strip():
            return False, "VNish preset name is required"
        ok, tok = self.unlock(ip, port, password, timeout=timeout)
        if not ok:
            return False, tok
        base = f"http://{ip}" if int(port) == 80 else f"http://{ip}:{port}"
        st, _, _, _ = http_json(
            "POST",
            f"{base}/api/v1/settings",
            headers={"Authorization": tok},
            json_body={"miner": {"overclock": {"preset": str(preset).strip()}}},
            timeout=timeout,
        )
        return (200 <= st < 300), (f"VNish preset set to {preset}" if 200 <= st < 300 else f"VNish preset HTTP {st}")

    def action(self, ip: str, port: int, password: str, kind: str, timeout: float = 5.0) -> tuple[bool, str]:
        ep = {"pause": "mining/pause", "resume": "mining/resume", "restart": "mining/restart", "reboot": "system/reboot"}.get(kind)
        if not ep:
            return False, f"Unknown VNish action: {kind}"
        ok, tok = self.unlock(ip, port, password, timeout=timeout)
        if not ok:
            return False, tok
        base = f"http://{ip}" if int(port) == 80 else f"http://{ip}:{port}"
        st, _, _, _ = http_json("POST", f"{base}/api/v1/{ep}", headers={"Authorization": tok}, timeout=timeout)
        return (200 <= st < 300), (f"VNish {kind} HTTP {st}")
