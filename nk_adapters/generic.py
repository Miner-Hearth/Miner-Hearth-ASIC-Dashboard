"""Generic Tier-2 fallback adapters (read-only universal safety net):
- generic-cgminer: Any CGMiner / BMMiner / SGMiner TCP :4028 miner
- generic-prometheus: Any Prometheus plain-text /metrics exporter
- generic-http-json: User-configured HTTP(S) GET JSON endpoint + dotted-key mapping
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .base import (
    AdapterCapabilities,
    ProbeResult,
    cgminer_request,
    http_json,
    http_request_raw,
    json_get_path,
    num,
    opt_int,
    tcp_open,
    to_hs,
    unit_kind_for_algorithm,
)
from .braiins import parse_prometheus_metrics


class GenericCGMinerAdapter:
    family_id = "generic-cgminer"
    display_name = "Generic CGMiner / BMMiner / SGMiner (:4028)"
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
            if not isinstance(ver, dict) or not isinstance(summ, dict):
                return None
            if "SUMMARY" not in summ and "VERSION" not in ver:
                return None
            v0 = (ver.get("VERSION") or [{}])[0] if isinstance(ver.get("VERSION"), list) else {}
            s0 = (summ.get("SUMMARY") or [{}])[0] if isinstance(summ.get("SUMMARY"), list) else {}
            hs = None
            if num(s0, "THS 5s", "THS av") is not None:
                hs = to_hs(num(s0, "THS 5s", "THS av"), "TH/s")
            elif num(s0, "GHS 5s", "GHS av") is not None:
                hs = to_hs(num(s0, "GHS 5s", "GHS av"), "GH/s")
            elif num(s0, "MHS 5s", "MHS av") is not None:
                hs = to_hs(num(s0, "MHS 5s", "MHS av"), "MH/s")
            elif num(s0, "KHS 5s", "KHS av") is not None:
                hs = to_hs(num(s0, "KHS 5s", "KHS av"), "kH/s")
            model = str(v0.get("Type") or v0.get("Miner") or "CGMiner Device")
            fw = str(v0.get("CGMiner") or v0.get("SGMiner") or v0.get("BMMiner") or "CGMiner")
            last_octet = ip.split(".")[-1]
            return ProbeResult(
                family=self.family_id,
                device_type="antminer",
                ip=ip,
                port=int(http_port),
                cgminer_port=int(cgminer_port),
                model=model,
                firmware="Generic CGMiner",
                firmware_note="Monitor-only generic CGMiner fallback",
                version=fw,
                algorithm="sha256d",
                hashrate_hs=hs,
                hashrate_unit_kind="hash",
                asic_temp_c=num(s0, "Temperature"),
                suggested_name=f"cgminer-{last_octet}",
                detection=f"Generic CGMiner :{cgminer_port}",
                capabilities=self.capabilities,
            )
        except Exception:
            return None


def parse_generic_prometheus(text: str, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Parse Prometheus exposition with optional custom metric names."""
    cfg = config if isinstance(config, dict) else {}
    base = parse_prometheus_metrics(text)
    hr_metric = str(cfg.get("hashrate_metric") or "").strip()
    hr_unit = str(cfg.get("hashrate_unit") or "H/s").strip()
    t_metric = str(cfg.get("temp_metric") or "").strip()
    p_metric = str(cfg.get("power_metric") or "").strip()
    f_metric = str(cfg.get("fan_metric") or "").strip()
    algo = str(cfg.get("algorithm") or "sha256d").strip().lower() or "sha256d"

    metrics: dict[str, list[float]] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(
            r'^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{[^}]*\})?\s+([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)$',
            line,
        )
        if m:
            try:
                metrics.setdefault(m.group(1), []).append(float(m.group(2)))
            except ValueError:
                pass

    hs = base.get("hashrate_hs")
    if hr_metric and hr_metric in metrics and metrics[hr_metric]:
        hs = to_hs(metrics[hr_metric][-1], hr_unit, algo)
    temps = list(base.get("temps") or [])
    if t_metric and t_metric in metrics:
        temps = [v for v in metrics[t_metric] if 0 < v < 200]
    power = base.get("power_w")
    if p_metric and p_metric in metrics and metrics[p_metric]:
        power = metrics[p_metric][-1]
    fans = list(base.get("fans") or [])
    if f_metric and f_metric in metrics:
        fans = [int(round(v)) for v in metrics[f_metric] if v >= 0]

    lbls = base.get("labels") or {}
    return {
        "model": str(lbls.get("model") or lbls.get("miner_model") or "Prometheus Miner"),
        "firmware": "Prometheus /metrics",
        "version": str(lbls.get("version") or ""),
        "algorithm": algo,
        "hashrate_unit_kind": unit_kind_for_algorithm(algo),
        "hashrate_hs": hs,
        "asic_temp_c": max(temps) if temps else None,
        "chip_temps": temps,
        "power_w": power,
        "fan_rpms": fans,
    }


class GenericPrometheusAdapter:
    family_id = "generic-prometheus"
    display_name = "Generic Prometheus (/metrics)"
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
        prom_port = 8081 if int(http_port) == 80 else int(http_port)
        if not tcp_open(ip, prom_port, tcp_timeout):
            return None
        try:
            st, body, _ = http_request_raw("GET", f"http://{ip}:{prom_port}/metrics", timeout=http_timeout)
            txt = body.decode("utf-8", errors="replace") if body else ""
            if st == 200 and ("# TYPE" in txt or "# HELP" in txt or "hashrate" in txt.lower()):
                pm = parse_generic_prometheus(txt)
                if pm["hashrate_hs"] is not None or pm["asic_temp_c"] is not None or pm["power_w"] is not None:
                    last_octet = ip.split(".")[-1]
                    return ProbeResult(
                        family=self.family_id,
                        device_type="antminer",
                        ip=ip,
                        port=prom_port,
                        cgminer_port=int(cgminer_port),
                        model=pm["model"],
                        firmware=pm["firmware"],
                        firmware_note="Monitor-only Prometheus /metrics scraper",
                        version=pm["version"],
                        algorithm="sha256d",
                        hashrate_hs=pm["hashrate_hs"],
                        hashrate_unit_kind="hash",
                        asic_temp_c=pm["asic_temp_c"],
                        fan_rpms=pm["fan_rpms"],
                        suggested_name=f"prom-{last_octet}",
                        detection=f"Prometheus :{prom_port}/metrics",
                        capabilities=self.capabilities,
                    )
        except Exception:
            pass
        return None

    def fetch_telemetry(
        self,
        ip: str,
        port: int = 8081,
        adapter_config: Optional[dict[str, Any]] = None,
        timeout: float = 4.0,
    ) -> dict[str, Any]:
        cfg = adapter_config if isinstance(adapter_config, dict) else {}
        path = str(cfg.get("metrics_path") or "/metrics").strip() or "/metrics"
        if not path.startswith("/"):
            path = "/" + path
        url = str(cfg.get("endpoint_url") or "").strip() or f"http://{ip}:{int(port or 8081)}{path}"
        st, body, _ = http_request_raw("GET", url, timeout=timeout)
        if st != 200 or not body:
            raise RuntimeError(f"Prometheus endpoint {url} returned HTTP {st}")
        out = parse_generic_prometheus(body.decode("utf-8", errors="replace"), cfg)
        out["source"] = f"Prometheus {url}"
        return out


def parse_generic_http_json(payload: Any, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Extract telemetry from an arbitrary JSON payload via dotted key paths."""
    cfg = config if isinstance(config, dict) else {}
    algo = str(cfg.get("algorithm") or "sha256d").strip().lower() or "sha256d"
    unit = str(cfg.get("hashrate_unit") or "GH/s").strip() or "GH/s"

    hr_raw = json_get_path(payload, str(cfg.get("hashrate_path") or "hashrate"))
    if hr_raw is None and isinstance(payload, dict):
        hr_raw = num(payload, "hashrate_ghs", "hashrate", "GHS 5s", "rate_5s")
    hs = None
    if hr_raw is not None:
        try:
            hs = to_hs(float(hr_raw), unit, algo)
        except (TypeError, ValueError):
            hs = None

    t_raw = json_get_path(payload, str(cfg.get("temp_path") or "temp"))
    if t_raw is None and isinstance(payload, dict):
        t_raw = num(payload, "asic_temp_c", "temp", "temperature")
    temp_c = None
    if t_raw is not None:
        try:
            temp_c = float(t_raw)
        except (TypeError, ValueError):
            temp_c = None

    p_raw = json_get_path(payload, str(cfg.get("power_path") or "power"))
    if p_raw is None and isinstance(payload, dict):
        p_raw = num(payload, "power_w", "power", "watts")
    power_w = None
    if p_raw is not None:
        try:
            power_w = float(p_raw)
        except (TypeError, ValueError):
            power_w = None

    f_raw = json_get_path(payload, str(cfg.get("fan_path") or "fans"))
    fans: list[int] = []
    if isinstance(f_raw, list):
        fans = [int(round(float(x))) for x in f_raw if isinstance(x, (int, float)) and x >= 0]
    elif isinstance(f_raw, (int, float)) and f_raw >= 0:
        fans = [int(round(float(f_raw)))]

    pool_raw = json_get_path(payload, str(cfg.get("pool_path") or "pool_url"))
    pool_url = str(pool_raw or "") if pool_raw is not None else ""
    model_raw = json_get_path(payload, str(cfg.get("model_path") or "model"))
    model = str(model_raw or "HTTP-JSON Miner")
    return {
        "model": model,
        "firmware": "Generic HTTP-JSON",
        "version": "",
        "algorithm": algo,
        "hashrate_unit_kind": unit_kind_for_algorithm(algo),
        "hashrate_hs": hs,
        "asic_temp_c": temp_c,
        "power_w": power_w,
        "fan_rpms": fans,
        "pool_url": pool_url,
    }


class GenericHTTPJsonAdapter:
    family_id = "generic-http-json"
    display_name = "Generic HTTP-JSON (Read-Only Custom Endpoint)"
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
        # User-configured endpoint; never auto-claimed during generic LAN scan.
        return None

    def fetch_telemetry(
        self,
        ip: str,
        port: int = 80,
        adapter_config: Optional[dict[str, Any]] = None,
        api_token: str = "",
        timeout: float = 4.0,
    ) -> dict[str, Any]:
        cfg = adapter_config if isinstance(adapter_config, dict) else {}
        endpoint = str(cfg.get("endpoint_url") or "").strip()
        if not endpoint:
            path = str(cfg.get("json_path") or "/status").strip() or "/status"
            if not path.startswith("/"):
                path = "/" + path
            base = f"http://{ip}" if int(port or 80) == 80 else f"http://{ip}:{int(port)}"
            endpoint = f"{base}{path}"
        hdrs: dict[str, str] = {}
        tok = str(api_token or cfg.get("bearer_token") or "").strip()
        if tok:
            hdrs["Authorization"] = f"Bearer {tok}"
        st, data, _, _ = http_json("GET", endpoint, headers=hdrs, timeout=timeout, allow_insecure_tls=True)
        if st != 200 or data is None:
            raise RuntimeError(f"Generic HTTP-JSON {endpoint} returned HTTP {st}")
        out = parse_generic_http_json(data, cfg)
        out["source"] = f"HTTP-JSON {endpoint}"
        return out
