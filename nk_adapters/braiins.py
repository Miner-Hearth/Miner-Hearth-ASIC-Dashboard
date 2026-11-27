"""Braiins OS+ adapter (S19, S21, Mini Miner BMM 100/101) with GetConstraints & Prometheus :8081."""

from __future__ import annotations

import re
from typing import Any, Optional

from .base import (
    AdapterCapabilities,
    ProbeResult,
    cgminer_request,
    http_request_raw,
    num,
    opt_int,
    tcp_open,
    to_hs,
)


def parse_prometheus_metrics(text: str) -> dict[str, Any]:
    """Parse Braiins OS+ :8081/metrics plain-text Prometheus exposition."""
    out: dict[str, Any] = {
        "labels": {},
        "hashrate_hs": None,
        "temps": [],
        "fans": [],
        "power_w": None,
        "client_status": None,
    }
    if not text:
        return out
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{([^}]*)\})?\s+([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|NaN|[+-]?Inf)$', line)
        if not m:
            continue
        metric, raw_lbls, raw_val = m.group(1), m.group(2) or "", m.group(3)
        try:
            val = float(raw_val)
        except ValueError:
            continue
        lbls: dict[str, str] = {}
        for lk, lv in re.findall(r'([A-Za-z_][A-Za-z0-9_]*)="((?:\\.|[^"\\])*)"', raw_lbls):
            lbls[lk] = lv
        low = metric.lower()
        if low == "miner_metadata" and lbls:
            out["labels"].update(lbls)
        elif low in {"bos_hashrate_hs", "miner_hashrate_hs", "hashrate_hs"} or "hashrate" in low:
            if "1m" in low or "5m" in low or out["hashrate_hs"] is None:
                # If metric name implies ghs/ths convert, else H/s
                unit_l = lbls.get("unit", "").lower()
                if "ghs" in low or "gh/s" in unit_l:
                    out["hashrate_hs"] = val * 1e9
                elif "ths" in low or "th/s" in unit_l:
                    out["hashrate_hs"] = val * 1e12
                else:
                    out["hashrate_hs"] = val
        elif "temperature" in low or low.endswith("_temp_c"):
            if 0.0 < val < 200.0:
                out["temps"].append(val)
        elif "fan_speed" in low or "fan_rpm" in low:
            if val >= 0:
                out["fans"].append(int(round(val)))
        elif "power_consumption" in low or low in {"bos_power_w", "bos_power_watts", "miner_power_w", "miner_power_watts"}:
            if val >= 0:
                out["power_w"] = val
        elif low == "client_status":
            out["client_status"] = val
    return out


class BraiinsAdapter:
    family_id = "braiins"
    display_name = "Braiins OS+ (S19/S21/BMM 100/101)"
    default_algorithms = ("sha256d",)
    capabilities = AdapterCapabilities(
        monitor=True,
        faults=True,
        logs=True,
        restart=True,
        sleep_wake=True,
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
        if tcp_open(ip, cgminer_port, tcp_timeout):
            try:
                ver = cgminer_request(ip, cgminer_port, "version", timeout=http_timeout)
                hay = str(ver).lower()
                if "boser" in hay or "bosminer" in hay or "braiins" in hay:
                    summ = cgminer_request(ip, cgminer_port, "summary", timeout=http_timeout)
                    s0 = (summ.get("SUMMARY") or [{}])[0] if isinstance(summ.get("SUMMARY"), list) else {}
                    v0 = (ver.get("VERSION") or [{}])[0] if isinstance(ver.get("VERSION"), list) else {}
                    ghs = num(s0, "MHS 5s", "MHS av")
                    if ghs is not None and "MHS 5s" in s0:
                        hs = to_hs(ghs, "MH/s", "sha256d")
                    else:
                        hs = to_hs(num(s0, "GHS 5s", "GHS av"), "GH/s", "sha256d")
                    model = str(v0.get("Miner") or v0.get("Type") or "Braiins OS+ Miner")
                    is_bmm = "bmm" in model.lower() or "mini" in model.lower()
                    sug = f"braiins-bmm-{last_octet}" if is_bmm else f"braiins-{last_octet}"
                    return ProbeResult(
                        family="braiins",
                        device_type="antminer",
                        ip=ip,
                        port=int(cgminer_port),
                        cgminer_port=int(cgminer_port),
                        model=model,
                        firmware="Braiins OS",
                        firmware_note="Braiins OS+ detected (gRPC power-target opt-in via GetConstraints)",
                        version=str(v0.get("BOSminer") or v0.get("CGMiner") or ""),
                        algorithm="sha256d",
                        hashrate_hs=hs,
                        hashrate_unit_kind="hash",
                        suggested_name=sug,
                        detection=f"Braiins BOSminer TCP :{cgminer_port}",
                        capabilities=self.capabilities,
                    )
            except Exception:
                pass

        # Fallback: Braiins OS+ Prometheus :8081/metrics
        if tcp_open(ip, 8081, tcp_timeout):
            try:
                st, body, _ = http_request_raw("GET", f"http://{ip}:8081/metrics", timeout=http_timeout)
                if st == 200 and body:
                    txt = body.decode("utf-8", errors="replace")
                    if "miner_metadata" in txt or "bos_" in txt:
                        parsed = parse_prometheus_metrics(txt)
                        lbls = parsed.get("labels") or {}
                        model = str(lbls.get("model") or lbls.get("miner_model") or "Braiins OS+ Miner")
                        return ProbeResult(
                            family="braiins",
                            device_type="antminer",
                            ip=ip,
                            port=int(http_port),
                            cgminer_port=int(cgminer_port),
                            model=model,
                            firmware="Braiins OS",
                            firmware_note="Braiins OS+ detected via Prometheus :8081/metrics",
                            algorithm="sha256d",
                            hashrate_hs=parsed.get("hashrate_hs"),
                            hashrate_unit_kind="hash",
                            asic_temp_c=max(parsed["temps"]) if parsed["temps"] else None,
                            fan_rpms=parsed["fans"],
                            suggested_name=f"braiins-{last_octet}",
                            detection="Braiins Prometheus :8081/metrics",
                            capabilities=self.capabilities,
                        )
            except Exception:
                pass
        return None
