"""AxeOS & Bitaxe-fork adapter (Gamma, NerdQAxe/OCTAxe, hexOS, Lucky Miner LV06-08, Bitaxe Touch, QAxe)."""

from __future__ import annotations

import re
from typing import Any, Optional

from .base import (
    AdapterCapabilities,
    ProbeResult,
    http_json,
    num,
    opt_int,
    tcp_open,
    to_hs,
)

AXEOS_ASIC_WHITELIST = frozenset({
    "BM1362",
    "BM1366",
    "BM1368",
    "BM1370",
    "BM1397",
    "BM1398",
})

BITAXE_FORK_HINTS = (
    "hexos",
    "lucky miner",
    "luckyminer",
    "lv06",
    "lv07",
    "lv08",
    "bitaxe touch",
    "qaxe",
    "nerd",
    "octaxe",
    "bitaxe",
    "gamma",
    "supra",
    "ultra",
    "max",
)


class AxeOSAdapter:
    family_id = "axeos"
    display_name = "AxeOS / Bitaxe Forks"
    default_algorithms = ("sha256d",)
    capabilities = AdapterCapabilities(
        monitor=True,
        faults=True,
        logs=True,
        restart=True,
        freq_tuning=True,
        voltage_tuning=True,
    )

    @staticmethod
    def is_supported_asic(asic_model: str) -> bool:
        s = str(asic_model or "").strip().upper()
        if not s:
            return False
        return any(chip in s for chip in AXEOS_ASIC_WHITELIST)

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
        st, data, _, _ = http_json("GET", f"{base}/api/system/info", timeout=http_timeout)
        if st == 200 and isinstance(data, dict) and (
            "hashRate" in data
            or "ASICModel" in data
            or "asicModel" in data
            or "axeOSVersion" in data
            or "temp" in data
        ):
            _, ident, _, _ = http_json("GET", f"{base}/api/v2/identify", timeout=min(http_timeout, 2.5))
            return self._build_probe(ip, http_port, data, ident if isinstance(ident, dict) else None, None, f"HTTP {base}/api/system/info")

        st2, dash, _, _ = http_json("GET", f"{base}/api/v2/dashboard", timeout=http_timeout)
        if st2 == 200 and isinstance(dash, dict) and ("thermal" in dash or "performance" in dash):
            _, ident, _, _ = http_json("GET", f"{base}/api/v2/identify", timeout=min(http_timeout, 2.5))
            _, sysinfo, _, _ = http_json("GET", f"{base}/api/v2/system", timeout=min(http_timeout, 2.5))
            return self._build_probe(
                ip,
                http_port,
                dash,
                ident if isinstance(ident, dict) else None,
                sysinfo if isinstance(sysinfo, dict) else None,
                f"HTTP {base}/api/v2/dashboard",
            )
        return None

    @staticmethod
    def _build_probe(
        ip: str,
        http_port: int,
        data: dict[str, Any],
        ident: Optional[dict[str, Any]],
        sysinfo: Optional[dict[str, Any]],
        detection: str,
    ) -> ProbeResult:
        blobs = []
        asic_count = 0
        for src in (data, ident, sysinfo):
            if not isinstance(src, dict):
                continue
            for k in ("deviceModel", "boardVersion", "ASICModel", "asicModel", "hostname", "version", "axeOSVersion", "boardName"):
                v = src.get(k)
                if v:
                    blobs.append(str(v))
            c = opt_int(src, "asicCount")
            if c:
                asic_count = max(asic_count, c)
        hint = " ".join(blobs).casefold()
        gamma_board = False
        for src in (data, ident, sysinfo):
            if isinstance(src, dict):
                bv = src.get("boardVersion")
                if bv is not None and re.match(r"^60[0-9](?:[._-].*)?$", str(bv).strip()):
                    gamma_board = True
                    break
        if any(t in hint for t in ("nerd", "qaxe", "octaxe", "nerdaxe", "nerdhaxe", "nerdeko")) or asic_count > 1:
            dtype = "nerdoctaxe"
            prefix = "nerdoctaxe"
        elif not any(t in hint for t in ("lucky", "lv06", "lv07", "lv08")) and ("bitaxe" in hint or "gamma" in hint or gamma_board):
            dtype = "gamma"
            prefix = "gamma"
        else:
            dtype = "axeos-generic"
            prefix = "axeos"
        last_octet = ip.split(".")[-1]
        perf = data.get("performance") if isinstance(data.get("performance"), dict) else {}
        therm = data.get("thermal") if isinstance(data.get("thermal"), dict) else {}
        hr_ghs = num(data, "hashRate") or num(perf, "hashRate", "hashrate")
        asic_t = num(data, "temp") or num(therm, "asicTemp", "temp")
        vr_t = num(data, "vrTemp") or num(therm, "vrTemp")
        freq = num(data, "frequency") or num(perf, "frequency")
        mv = num(data, "coreVoltage") or num(perf, "coreVoltage")
        model = (
            str((ident or {}).get("deviceModel") or "")
            or str((sysinfo or {}).get("deviceModel") or "")
            or str(data.get("deviceModel") or data.get("ASICModel") or data.get("asicModel") or "")
            or ("NerdQAxePlus" if dtype == "nerdoctaxe" else "AxeOS Miner")
        )
        fw = str(data.get("axeOSVersion") or data.get("version") or "AxeOS")
        return ProbeResult(
            family="axeos",
            device_type=dtype,
            ip=ip,
            port=int(http_port),
            cgminer_port=4028,
            model=model,
            firmware=fw,
            version=str(data.get("version") or ""),
            algorithm="sha256d",
            hashrate_hs=to_hs(hr_ghs, "GH/s", "sha256d"),
            hashrate_unit_kind="hash",
            asic_temp_c=asic_t,
            vr_temp_c=vr_t,
            pool_url=str(data.get("stratumURL") or ""),
            frequency_mhz=freq,
            core_voltage_mv=mv,
            tunable=True,
            suggested_name=f"{prefix}-{last_octet}",
            detection=detection,
            capabilities=AxeOSAdapter.capabilities,
        )
