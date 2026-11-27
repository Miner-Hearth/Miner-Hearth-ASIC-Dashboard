"""LuxOS (LUXMiner) adapter (:4028 TCP + :8080 POST /api; logon/SessionID/logoff mutex)."""

from __future__ import annotations

import threading
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

# LuxOS allows only ONE active session per miner at a time.
_SESSION_LOCK = threading.Lock()


class LuxOSAdapter:
    family_id = "luxos"
    display_name = "LuxOS (LUXMiner)"
    default_algorithms = ("sha256d",)
    capabilities = AdapterCapabilities(
        monitor=True,
        faults=True,
        restart=True,
        sleep_wake=True,
        workmode=True,
        power_target=True,
    )

    @staticmethod
    def query(ip: str, cgminer_port: int, command: str, parameter: str = "", timeout: float = 4.0, http_port: int = 8080) -> dict[str, Any]:
        if tcp_open(ip, cgminer_port, min(timeout, 1.5)):
            return cgminer_request(ip, cgminer_port, command, parameter=parameter, timeout=timeout)
        body: dict[str, Any] = {"command": command}
        if parameter != "":
            body["parameter"] = parameter
        st, data, _, _ = http_json("POST", f"http://{ip}:{http_port}/api", json_body=body, timeout=timeout)
        if st == 200 and isinstance(data, dict):
            return data
        raise ValueError(f"LuxOS {ip} {command} HTTP {st}")

    def probe(
        self,
        ip: str,
        http_port: int = 80,
        cgminer_port: int = 4028,
        tcp_timeout: float = 1.0,
        http_timeout: float = 4.0,
    ) -> Optional[ProbeResult]:
        last_octet = ip.split(".")[-1]
        ver = None
        via = ""
        if tcp_open(ip, cgminer_port, tcp_timeout):
            try:
                ver = cgminer_request(ip, cgminer_port, "version", timeout=http_timeout)
                via = f"LuxOS TCP :{cgminer_port}"
            except Exception:
                ver = None
        if ver is None and tcp_open(ip, 8080, tcp_timeout):
            try:
                st, data, _, _ = http_json("POST", f"http://{ip}:8080/api", json_body={"command": "version"}, timeout=http_timeout)
                if st == 200 and isinstance(data, dict):
                    ver = data
                    via = "LuxOS HTTP :8080/api"
            except Exception:
                ver = None
        if not ver:
            return None
        hay = str(ver).lower()
        if "luxminer" not in hay and "luxos" not in hay:
            return None
        v0 = (ver.get("VERSION") or [{}])[0] if isinstance(ver.get("VERSION"), list) else {}
        try:
            summ = self.query(ip, cgminer_port, "summary", timeout=http_timeout)
        except Exception:
            summ = {}
        s0 = (summ.get("SUMMARY") or [{}])[0] if isinstance(summ.get("SUMMARY"), list) else {}
        ghs = num(s0, "GHS 5s", "GHS av")
        hs = to_hs(ghs, "GH/s", "sha256d") if ghs is not None else to_hs(num(s0, "MHS 5s", "MHS av"), "MH/s", "sha256d")
        return ProbeResult(
            family="luxos",
            device_type="antminer",
            ip=ip,
            port=int(cgminer_port),
            cgminer_port=int(cgminer_port),
            model=str(v0.get("Miner") or v0.get("Type") or "LuxOS Miner"),
            firmware="LuxOS",
            firmware_note="Monitor-only by default (opt-in LuxOS logon/profileset/logoff)",
            version=str(v0.get("LUXminer") or v0.get("CGMiner") or ""),
            algorithm="sha256d",
            hashrate_hs=hs,
            hashrate_unit_kind="hash",
            suggested_name=f"luxos-{last_octet}",
            detection=via,
            capabilities=self.capabilities,
        )

    @staticmethod
    def _extract_session_id(res: dict[str, Any]) -> str:
        sess = res.get("SESSION")
        if isinstance(sess, list) and sess and isinstance(sess[0], dict):
            return str(sess[0].get("SessionID") or "")
        return ""

    @staticmethod
    def _status_ok(res: dict[str, Any]) -> tuple[bool, str]:
        st = res.get("STATUS")
        if isinstance(st, list) and st and isinstance(st[0], dict):
            code = str(st[0].get("STATUS") or "").upper()
            msg = str(st[0].get("Msg") or "")
            return code in {"S", "I"}, msg or code
        return True, "OK"

    def run_session_command(
        self,
        ip: str,
        cgminer_port: int,
        command: str,
        param_tail: str = "",
        timeout: float = 5.0,
    ) -> tuple[bool, str]:
        """Execute a privileged LuxOS session command with guaranteed logoff in finally."""
        with _SESSION_LOCK:
            sid = ""
            try:
                logon_res = self.query(ip, cgminer_port, "logon", timeout=timeout)
                sid = self._extract_session_id(logon_res)
                if not sid:
                    ok, msg = self._status_ok(logon_res)
                    return False, f"LuxOS logon did not return SessionID ({msg})"
                param = f"{sid},{param_tail}" if param_tail != "" else sid
                cmd_res = self.query(ip, cgminer_port, command, parameter=param, timeout=timeout)
                return self._status_ok(cmd_res)
            finally:
                if sid:
                    try:
                        self.query(ip, cgminer_port, "logoff", parameter=sid, timeout=min(timeout, 3.0))
                    except Exception:
                        pass

    def set_profile(self, ip: str, cgminer_port: int, profile: str, timeout: float = 5.0) -> tuple[bool, str]:
        prof = str(profile or "").strip()
        if not prof:
            return False, "LuxOS profile name is required"
        return self.run_session_command(ip, cgminer_port, "profileset", prof, timeout=timeout)

    def list_profiles(self, ip: str, cgminer_port: int, timeout: float = 4.0) -> list[dict[str, Any]]:
        res = self.query(ip, cgminer_port, "profiles", timeout=timeout)
        items = res.get("PROFILES")
        return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
