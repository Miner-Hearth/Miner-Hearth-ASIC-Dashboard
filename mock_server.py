#!/usr/bin/env python3
"""Minimal AxeOS / NerdOCTAxe HTTP mock + CGMiner TCP mock for autotune tests and dry practice."""

from __future__ import annotations

import argparse
import json
import socketserver
import struct
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlparse


# Captured GET /api/system/info from the user's NerdOCTAxe (NerdQAxePlus v1.0.37.1).
# This firmware has no lastSubmittedDiff / lastShareSeconds.
NERD_V10371_INFO = json.loads(
    r'''{"asicCount":8,"smallCoreCount":2040,"deviceModel":"NerdOCTAXE-γ","hostip":"192.168.1.23","macAddr":"02:00:00:00:00:01","wifiRSSI":-74,"power":259,"maxPower":300,"minPower":30,"maxVoltage":13,"minVoltage":11,"current":21562.5,"currentA":21.5625,"minCurrentA":0,"maxCurrentA":25,"temp":50.8125,"vrTemp":45.375,"vrTempInt":48.875,"hashRateTimestamp":87515560,"hashRate":12471.3,"hashRate_1m":12559.78,"hashRate_10m":12530.2,"hashRate_1h":12548.77,"hashRate_1d":13015.41,"coreVoltage":1210,"defaultCoreVoltage":1210,"coreVoltageActual":1208,"fanspeed":100,"manualFanSpeed":100,"fanrpm":2730,"fanrpm2":2732,"fanspeed2":100,"fanCount":2,"lastpingrtt":39.71428571,"recentpingloss":0.00952381,"shutdown":false,"duplicateHWNonces":0,"stratum":{"poolMode":0,"activePoolMode":0,"poolBalance":50,"totalBestDiff":20922595573744,"usingFallback":false,"pools":[{"connected":true,"poolDifficulty":12173,"networkDifficulty":6.085720995e9,"poolDiffErr":false,"accepted":22864,"rejected":1,"pingRtt":39.71428571,"pingLoss":0.00952381,"bestDiff":1198416595}]},"poolDifficulty":12173,"networkDifficulty":6.085720995e9,"foundBlocks":0,"totalFoundBlocks":3417,"sharesAccepted":22864,"sharesRejected":1,"bestDiff":20922595573744,"bestSessionDiff":1198416595,"asicTemps":[0,0,0,0,0,0,0,0],"pidTargetTemp":55,"pidP":6,"pidI":0.1,"pidD":10,"fans":[{"label":"M2 (Use a Y-splitter cable for multiple ASIC fans.)","mode":0,"manualSpeed":100,"overheatTemp":75,"rpm":2730,"speedPerc":100,"pid":{"targetTemp":55,"p":6,"i":0.1,"d":10}},{"label":"M1","mode":0,"manualSpeed":100,"overheatTemp":75,"rpm":2732,"speedPerc":100,"pid":{"targetTemp":65,"p":6,"i":0.1,"d":10}}],"hostname":"Axe1","ssid":"HomeWiFi","stratumURL":"xec.heliospool.com","stratumPort":3339,"stratumUser":"ecash:qz3h3f8er6dy7tfrzh5cxptueu2m6rm9u5cdj6rj7y.Axe1","stratumEnonceSubscribe":false,"stratumTLS":false,"fallbackStratumURL":"xec.heliospool.com","fallbackStratumPort":3339,"fallbackStratumUser":"ecash:qz3h3f8er6dy7tfrzh5cxptueu2m6rm9u5cdj6rj7y.Axe1","fallbackStratumEnonceSubscribe":false,"fallbackStratumTLS":false,"stratumProtocol":0,"fallbackStratumProtocol":0,"sv2AuthorityPubkey":"","fallbackSv2AuthorityPubkey":"","sv2ChannelType":0,"fallbackSv2ChannelType":0,"voltage":12000,"frequency":850,"defaultFrequency":700,"jobInterval":500,"stratumDifficulty":1000,"overheat_temp":75,"flipscreen":0,"invertscreen":0,"autoscreenoff":0,"invertfanpolarity":0,"autofanspeed":0,"stratum_keep":1,"vrFrequency":25011,"defaultVrFrequency":25011,"otp":false,"ASICModel":"BM1370","uptimeSeconds":87519,"lastResetReason":"SYSTEM.RESET_SOFTWARE","wifiStatus":"Connected!","freeHeap":7015064,"freeHeapInt":96948,"version":"v1.0.37.1","runningPartition":"ota_1","defaultTheme":"cosmic"}'''
)


@dataclass
class MinerState:
    name: str
    kind: str  # gamma | nerdoctaxe
    frequency: float = 525.0
    actual_frequency: float = 525.0
    temp: float = 55.0
    temp2: float = 0.0
    vr_temp: float = 60.0
    asic_temps: list[float] = field(default_factory=list)
    hashrate: float = 1000.0
    expected_hashrate: Optional[float] = None
    error_percentage: Optional[float] = 0.2
    shares_accepted: Optional[int] = 1000
    shares_rejected: Optional[int] = 2
    power: float = 18.0
    core_voltage: int = 1210
    version: str = "mock-1.0"
    model: str = ""
    require_otp: bool = False
    expected_totp: str = "123456"
    expected_session: str = "session-token"
    drop_connections: bool = False
    status_override: Optional[int] = None
    patches: list[dict[str, Any]] = field(default_factory=list)
    restarts: int = 0
    delay_get_sec: float = 0.0
    v2_enabled: bool = True
    custom_info: Optional[dict[str, Any]] = None
    fail_gets: int = 0
    stratum_url: str = "public-pool.io"
    stratum_port: int = 21496
    stratum_user: str = "bc1qmock.worker"
    stratum_password: str = "x"
    fallback_stratum_url: str = ""
    fallback_stratum_port: Optional[int] = None
    fallback_stratum_user: str = ""
    fallback_stratum_password: str = ""
    hexos_mode: bool = False
    board_temp: Optional[float] = None
    fanspeed: Optional[float] = None
    fanrpm: int = 4200
    wifi_rssi: Optional[int] = None
    best_diff: Any = None
    best_session_diff: Any = None
    last_submitted_diff: Any = None
    last_share_seconds: Optional[float] = None
    stratum_disconnects: Optional[int] = None
    wifi_disconnects: Optional[int] = None
    using_fallback_stratum: bool = False
    rejected_reasons: list[dict[str, Any]] = field(default_factory=list)
    domains: list[float] = field(default_factory=list)
    block_height: Optional[int] = None
    network_difficulty: Optional[float] = None
    # v1.10 fault scenarios
    overheat_mode: int = 0
    power_fault: str = ""
    hardware_fault: str = ""
    mining_paused: bool = False
    reset_reason: str = ""
    nominal_voltage: Optional[float] = None
    input_voltage_mv: Optional[float] = None
    board_error: int = 0
    shutdown: bool = False
    last_reset_reason: str = "RESET_POWERON"
    logs_text: Optional[str] = None
    resumes: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "frequency": self.frequency,
                "actual_frequency": self.actual_frequency,
                "temp": self.temp,
                "temp2": self.temp2,
                "vr_temp": self.vr_temp,
                "asic_temps": list(self.asic_temps),
                "hashrate": self.hashrate,
                "expected_hashrate": self.expected_hashrate,
                "error_percentage": self.error_percentage,
                "shares_accepted": self.shares_accepted,
                "shares_rejected": self.shares_rejected,
                "power": self.power,
                "core_voltage": self.core_voltage,
                "restarts": self.restarts,
                "resumes": self.resumes,
                "overheat_mode": self.overheat_mode,
                "patches": list(self.patches),
            }


def _json_response(handler: BaseHTTPRequestHandler, code: int, body: Any) -> None:
    data = b"" if body is None else json.dumps(body).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(data)


def make_handler(state: MinerState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _otp_ok(self) -> bool:
            if not state.require_otp:
                return True
            totp = self.headers.get("X-TOTP", "")
            session = self.headers.get("X-OTP-Session", "")
            return totp == state.expected_totp or session == state.expected_session

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return {}
            return data if isinstance(data, dict) else {}

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, PATCH, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()

        def do_GET(self) -> None:
            if state.drop_connections:
                try:
                    self.connection.close()
                except OSError:
                    pass
                return
            if state.status_override:
                _json_response(self, state.status_override, {"error": "override"})
                return
            delay = 0.0
            with state.lock:
                delay = float(state.delay_get_sec or 0.0)
                if state.fail_gets > 0:
                    state.fail_gets -= 1
                    try:
                        self.connection.close()
                    except OSError:
                        pass
                    return
            if delay > 0:
                threading.Event().wait(delay)
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                html = b"<!DOCTYPE html><html><body>AxeOS mock UI</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(html)
                return
            with state.lock:
                if (not state.v2_enabled) and path.startswith("/api/v2/"):
                    # v1.0.37.x firmware: unknown API paths 302 to the HTML UI.
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Connection", "close")
                    body = b"Redirect to the captive portal"
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
                    return
                if state.custom_info is not None and path == "/api/system/info":
                    _json_response(self, 200, state.custom_info)
                    return
                if state.kind == "nerdoctaxe" and path == "/api/v2/dashboard":
                    temps = list(state.asic_temps) or [state.temp]
                    perf: dict[str, Any] = {
                        "hashRate": state.hashrate,
                        "frequency": state.frequency,
                        "asicCount": max(1, len(temps)),
                    }
                    if state.expected_hashrate is not None:
                        perf["expectedHashrate"] = state.expected_hashrate
                    if state.error_percentage is not None:
                        perf["errorPercentage"] = state.error_percentage
                    if state.shares_accepted is not None:
                        perf["sharesAccepted"] = state.shares_accepted
                    if state.shares_rejected is not None:
                        perf["sharesRejected"] = state.shares_rejected
                    _json_response(
                        self,
                        200,
                        {
                            "deviceModel": state.model or "NerdOCTAXE-γ",
                            "system": {"uptime": 1000, "overheatTemp": 75, "vrOverheatTemp": 80,
                                       "shutdown": bool(state.shutdown), "boardError": int(state.board_error)},
                            "performance": perf,
                            "power": {
                                "watts": state.power,
                                "voltage": 12.1,
                                "coreVoltageActual": state.core_voltage / 1000.0,
                            },
                            "thermal": {
                                "asicTemp": max(temps),
                                "vrTemp": state.vr_temp,
                                "asicTemps": temps,
                                **({"fans": [{"rpm": state.fanrpm, "speed": state.fanspeed}]} if state.fanspeed is not None else {}),
                            },
                            "stratum": {
                                "pools": [
                                    {
                                        "host": state.stratum_url,
                                        "port": state.stratum_port,
                                        "user": state.stratum_user,
                                        "active": True,
                                        "connected": True,
                                    },
                                    *(
                                        [
                                            {
                                                "host": state.fallback_stratum_url,
                                                "port": state.fallback_stratum_port or state.stratum_port,
                                                "user": state.fallback_stratum_user or state.stratum_user,
                                                "active": False,
                                                "connected": False,
                                            }
                                        ]
                                        if state.fallback_stratum_url
                                        else []
                                    ),
                                ]
                            },
                        },
                    )
                    return
                if state.kind == "nerdoctaxe" and path == "/api/v2/system":
                    _json_response(self, 200, {"version": state.version, "lastResetReason": state.last_reset_reason,
                                               "hostname": "nerd-mock", "macAddr": "02:00:00:00:00:02", "ssid": "ExampleNet"})
                    return
                if path == "/api/system/logs" and state.logs_text is not None:
                    data = state.logs_text.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if state.kind == "nerdoctaxe" and path == "/api/v2/identify":
                    _json_response(
                        self,
                        200,
                        {"deviceModel": state.model or "NerdOCTAXE-γ", "asicModel": "BM1370"},
                    )
                    return
                if path == "/api/system/info":
                    body: dict[str, Any] = {
                        "power": state.power,
                        "temp": state.temp if state.kind == "gamma" else (
                            max(state.asic_temps) if state.asic_temps else state.temp
                        ),
                        "temp2": state.temp2,
                        "vrTemp": state.vr_temp,
                        "hashRate": state.hashrate,
                        "frequency": state.frequency,
                        "actualFrequency": state.actual_frequency,
                        "coreVoltage": state.core_voltage,
                        "coreVoltageActual": state.core_voltage,
                        "version": state.version,
                        "hostname": state.name,
                        "deviceModel": state.model or ("Gamma" if state.kind == "gamma" else "NerdOCTAXE-γ"),
                        "ASICModel": "BM1370",
                        "stratumURL": state.stratum_url,
                        "stratumPort": state.stratum_port,
                        "stratumUser": state.stratum_user,
                        "fallbackStratumURL": state.fallback_stratum_url,
                        "fallbackStratumPort": state.fallback_stratum_port,
                        "fallbackStratumUser": state.fallback_stratum_user,
                    }
                    if state.expected_hashrate is not None:
                        body["expectedHashrate"] = state.expected_hashrate
                    if state.error_percentage is not None:
                        body["errorPercentage"] = state.error_percentage
                    if state.shares_accepted is not None:
                        body["sharesAccepted"] = state.shares_accepted
                    if state.shares_rejected is not None:
                        body["sharesRejected"] = state.shares_rejected
                    if state.kind == "gamma":  # AxeOS fault fields (power_fault / hardware_fault only appear on a fault)
                        body["overheat_mode"] = int(state.overheat_mode)
                        body["miningPaused"] = bool(state.mining_paused)
                        if state.power_fault:
                            body["power_fault"] = state.power_fault
                        if state.hardware_fault:
                            body["hardware_fault"] = state.hardware_fault
                        if state.reset_reason:
                            body["resetReason"] = state.reset_reason
                        if state.nominal_voltage is not None:
                            body["nominalVoltage"] = state.nominal_voltage
                        if state.input_voltage_mv is not None:
                            body["voltage"] = state.input_voltage_mv
                    if state.kind == "nerdoctaxe" and state.asic_temps:
                        body["asicTemps"] = list(state.asic_temps)
                    if state.fanspeed is not None and not state.hexos_mode:
                        body["fanspeed"] = state.fanspeed
                        body["fanrpm"] = state.fanrpm
                    if state.hexos_mode:
                        body["version"] = state.version or "v2.13.1-hexos.1"
                        body["temp2"] = -1
                        body["boardTemp"] = state.board_temp if state.board_temp is not None else 42.5
                        body["voltage"] = 5080
                        body["current"] = 3.6
                        body["hashRate_1m"] = state.hashrate * 1.01
                        body["hashRate_10m"] = state.hashrate * 0.99
                        body["hashRate_1h"] = state.hashrate * 0.98
                        body["fanspeed"] = state.fanspeed if state.fanspeed is not None else 72
                        body["fanrpm"] = state.fanrpm
                        body["wifiRSSI"] = state.wifi_rssi if state.wifi_rssi is not None else -57
                        body["bestDiff"] = state.best_diff if state.best_diff is not None else "14.2M"
                        body["bestSessionDiff"] = state.best_session_diff if state.best_session_diff is not None else "2.4M"
                        body["poolDifficulty"] = 1000
                        body["lastSubmittedDiff"] = state.last_submitted_diff if state.last_submitted_diff is not None else 2160
                        body["lastShareSeconds"] = state.last_share_seconds if state.last_share_seconds is not None else 15
                        body["stratumDisconnects"] = state.stratum_disconnects if state.stratum_disconnects is not None else 1
                        body["wifiDisconnects"] = state.wifi_disconnects if state.wifi_disconnects is not None else 0
                        body["isUsingFallbackStratum"] = bool(state.using_fallback_stratum)
                        body["sharesRejectedReasons"] = list(state.rejected_reasons)
                        body["hashrateMonitor"] = {
                            "asics": [
                                {
                                    "total": state.hashrate,
                                    "errorCount": 0,
                                    "domains": list(state.domains) if state.domains else [285.0, 290.0, 288.0, 292.0],
                                }
                            ]
                        }
                        body["blockHeight"] = state.block_height if state.block_height is not None else 921450
                        body["networkDifficulty"] = state.network_difficulty if state.network_difficulty is not None else 101600000000000.0
                    _json_response(self, 200, body)
                    return
                if path == "/api/system/asic":
                    _json_response(
                        self,
                        200,
                        {
                            "ASICModel": "BM1370",
                            "deviceModel": state.model or "Gamma",
                            "asicCount": 1 if state.kind == "gamma" else 8,
                            "defaultFrequency": 525 if state.kind == "gamma" else 700,
                            "frequencyOptions": (
                                [400, 490, 525, 550, 600, 625, 700, 750, 800, 850]
                                if state.kind == "gamma"
                                else [525, 550, 575, 600, 625, 650, 675, 700, 750, 800, 850]
                            ),
                            "defaultVoltage": 1210,
                            "voltageOptions": [1150, 1170, 1190, 1210, 1230, 1250],
                        },
                    )
                    return
                if path == "/api/v2/settings":
                    _json_response(
                        self,
                        200,
                        {
                            "frequency": state.frequency,
                            "coreVoltage": state.core_voltage,
                            "deviceModel": state.model or "NerdOCTAXE-γ",
                            "asicModel": "BM1370",
                            "otp": state.require_otp,
                        },
                    )
                    return
            _json_response(self, 404, {"error": "not found", "path": path})

        def do_PATCH(self) -> None:
            if state.drop_connections:
                try:
                    self.connection.close()
                except OSError:
                    pass
                return
            if state.status_override:
                _json_response(self, state.status_override, {"error": "override"})
                return
            if not self._otp_ok():
                _json_response(self, 401, {"error": "OTP required"})
                return
            path = urlparse(self.path).path
            allowed = {"/api/system", "/api/v2/settings"}
            if path not in allowed:
                _json_response(self, 404, {"error": "not found", "path": path})
                return
            body = self._read_json()
            with state.lock:
                state.patches.append({"path": path, "body": body})
                if "frequency" in body:
                    try:
                        freq = float(body["frequency"])
                        state.frequency = freq
                        state.actual_frequency = freq
                    except (TypeError, ValueError):
                        pass
                if "coreVoltage" in body:
                    try:
                        state.core_voltage = int(body["coreVoltage"])
                    except (TypeError, ValueError):
                        pass
                if "overheat_mode" in body:
                    state.overheat_mode = int(body["overheat_mode"] or 0)
                if "stratumURL" in body:
                    state.stratum_url = str(body["stratumURL"] or "")
                if "stratumPort" in body and body["stratumPort"] is not None:
                    state.stratum_port = int(body["stratumPort"])
                if "stratumUser" in body:
                    state.stratum_user = str(body["stratumUser"] or "")
                if "stratumPassword" in body:
                    state.stratum_password = str(body["stratumPassword"] or "")
                if "fallbackStratumURL" in body:
                    state.fallback_stratum_url = str(body["fallbackStratumURL"] or "")
                if "fallbackStratumPort" in body:
                    state.fallback_stratum_port = int(body["fallbackStratumPort"]) if body["fallbackStratumPort"] is not None else None
                if "fallbackStratumUser" in body:
                    state.fallback_stratum_user = str(body["fallbackStratumUser"] or "")
                if "fallbackStratumPassword" in body:
                    state.fallback_stratum_password = str(body["fallbackStratumPassword"] or "")
                if isinstance(body.get("pools"), list):
                    plist = body["pools"]
                    if len(plist) >= 1 and isinstance(plist[0], dict):
                        if "url" in plist[0]:
                            state.stratum_url = str(plist[0]["url"] or "")
                        if "port" in plist[0] and plist[0]["port"] is not None:
                            state.stratum_port = int(plist[0]["port"])
                        if "user" in plist[0]:
                            state.stratum_user = str(plist[0]["user"] or "")
                        if "password" in plist[0]:
                            state.stratum_password = str(plist[0]["password"] or "")
                    if len(plist) >= 2 and isinstance(plist[1], dict):
                        state.fallback_stratum_url = str(plist[1].get("url") or "")
                        state.fallback_stratum_port = int(plist[1]["port"]) if plist[1].get("port") is not None else None
                        state.fallback_stratum_user = str(plist[1].get("user") or "")
            _json_response(self, 200, {"status": "success"})

        def do_POST(self) -> None:
            if state.drop_connections:
                try:
                    self.connection.close()
                except OSError:
                    pass
                return
            if not self._otp_ok():
                _json_response(self, 401, {"error": "OTP required"})
                return
            path = urlparse(self.path).path
            if path == "/api/system/restart":
                with state.lock:
                    state.restarts += 1
                    state.power_fault = ""  # latched faults clear at boot
                    state.hardware_fault = ""
                    state.board_error = 0
                    state.shutdown = False
                _json_response(self, 200, {"message": "System will restart shortly."})
                return
            if path == "/api/system/resume":
                with state.lock:
                    state.resumes += 1
                    state.mining_paused = False
                _json_response(self, 200, {"message": "Mining resumed."})
                return
            _json_response(self, 404, {"error": "not found", "path": path})

    return Handler


class MockMiner:
    def __init__(self, state: MinerState, host: str = "127.0.0.1", port: int = 0):
        self.state = state
        self.server = ThreadingHTTPServer((host, port), make_handler(state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "MockMiner":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@dataclass
class CGMinerState:
    miner_type: str = "Antminer S19 Pro"
    firmware_kind: str = "stock"  # stock | braiins | vnish | luxos
    ghs_5s: float = 110250.0
    ghs_av: float = 109800.0
    elapsed: int = 123456
    frequency: float = 675.0
    board_temps: list[float] = field(default_factory=lambda: [54.0, 56.0, 55.0])
    chip_temps: list[float] = field(default_factory=lambda: [62.0, 65.0, 63.0])
    fans: list[int] = field(default_factory=lambda: [6120, 6180, 6090, 6150])
    pool_url: str = "stratum+tcp://pool.example.com:3333"
    pool_user: str = "worker.s19pro"
    allow_privileged: bool = False
    extra_pools: list[dict[str, Any]] = field(default_factory=list)
    active_pool: int = 0
    when_ts: int = 1795260000
    power_w: float = 3120.0
    power_limit_w: float = 3250.0
    tuner_mode: str = "PowerTarget"
    commands_seen: list[str] = field(default_factory=list)
    chain_acn: list[int] = field(default_factory=list)  # v1.10: stock chain chip counts (empty = not reported)
    chain_acs: list[str] = field(default_factory=list)
    miner_count_override: Optional[int] = None
    rate_ideal_ghs: Optional[float] = None
    hw_pct: Optional[float] = None
    dev_statuses: list[str] = field(default_factory=list)
    pools_all_dead: bool = False
    tuner_chain_statuses: list[str] = field(default_factory=list)
    summary_status_err: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class _ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class MockCGMiner:
    """Fake CGMiner/BMMiner/BOSminer TCP 4028 API server for Antminer S19 Pro tests."""

    def __init__(self, state: CGMinerState, host: str = "127.0.0.1", port: int = 0):
        self.state = state

        class Handler(socketserver.BaseRequestHandler):
            def handle(self_h) -> None:
                raw = b""
                try:
                    self_h.request.settimeout(2.0)
                    while True:
                        chunk = self_h.request.recv(4096)
                        if not chunk:
                            break
                        raw += chunk
                        if b"\n" in raw or b"}" in raw:
                            break
                except OSError:
                    return
                try:
                    req = json.loads(raw.decode("utf-8").strip())
                    cmd = str(req.get("command") or "").strip().lower()
                except Exception:
                    return
                with state.lock:
                    state.commands_seen.append(cmd)
                    desc = "BOSer boser-openwrt 0.1.0, bosminer-plus-tuner 0.9.0" if state.firmware_kind == "braiins-plus" else "CGMiner mock"
                    st_hdr = {"STATUS": "S", "When": state.when_ts, "Description": desc}
                    if cmd == "version":
                        ver = {
                            "CGMiner": "4.11.1",
                            "API": "3.7",
                            "Type": state.miner_type,
                            "Miner": "16.8.1.3",
                        }
                        if state.firmware_kind == "stock":
                            ver["BMMiner"] = "2.0.0"
                        elif state.firmware_kind in {"braiins", "braiins-plus"}:
                            ver["BOSminer"] = "bosminer-plus-tuner 0.9.0" if state.firmware_kind == "braiins-plus" else "24.09"
                        elif state.firmware_kind == "vnish":
                            ver["Miner"] = "vnish 1.2.4"
                            ver["BMMiner"] = "2.0.0"
                        elif state.firmware_kind == "luxos":
                            ver["Miner"] = "LuxOS 2024.1"
                        resp = {"STATUS": [st_hdr], "VERSION": [ver]}
                    elif cmd == "summary":
                        if state.firmware_kind == "braiins-plus":
                            mhs = state.ghs_5s * 1000.0
                            mhs_av = state.ghs_av * 1000.0
                            resp = {
                                "STATUS": [st_hdr],
                                "SUMMARY": [
                                    {
                                        "Elapsed": state.elapsed,
                                        "MHS 5s": mhs,
                                        "MHS 1m": mhs * 0.995,
                                        "MHS 5m": mhs * 0.99,
                                        "MHS 15m": mhs * 0.985,
                                        "MHS 24h": mhs_av * 0.98,
                                        "MHS av": mhs_av,
                                        "Accepted": 45210,
                                        "Rejected": 112,
                                        "Stale": 18,
                                        "Hardware Errors": 37,
                                        "Best Share": 891234567,
                                        "Pool Rejected%": 0.25,
                                        "Found Blocks": 12,
                                    }
                                ],
                            }
                        else:
                            sum_d: dict[str, Any] = {
                                "Elapsed": state.elapsed,
                                "GHS 5s": state.ghs_5s,
                                "GHS av": state.ghs_av,
                            }
                            if state.hw_pct is not None:
                                sum_d["Device Hardware%"] = state.hw_pct
                            hdr = dict(st_hdr)
                            if state.summary_status_err:
                                hdr = {"STATUS": "W", "When": state.when_ts, "Msg": state.summary_status_err}
                            resp = {"STATUS": [hdr], "SUMMARY": [sum_d]}
                    elif cmd == "devs" and state.firmware_kind == "braiins-plus":
                        per_board = (state.ghs_5s * 1000.0) / 3.0
                        resp = {
                            "STATUS": [st_hdr],
                            "DEVS": [
                                {
                                    "ID": idx,
                                    "Status": state.dev_statuses[idx] if idx < len(state.dev_statuses) else "Alive",
                                    "Temperature": 0.0,
                                    "MHS 5s": per_board,
                                    "MHS 15m": per_board * 0.99,
                                    "MHS av": per_board * 0.985,
                                    "Nominal MHS": 36600000.0,
                                    "Hardware Errors": 10 + idx,
                                    "Last Share Difficulty": 65536,
                                    "Last Share Time": state.when_ts - (12 + idx * 5),
                                }
                                for idx in range(3)
                            ],
                        }
                    elif cmd == "temps" and state.firmware_kind == "braiins-plus":
                        resp = {
                            "STATUS": [st_hdr],
                            "TEMPS": [
                                {
                                    "ID": idx,
                                    "Board": state.board_temps[idx] if idx < len(state.board_temps) else 55.0,
                                    "Chip": state.chip_temps[idx] if idx < len(state.chip_temps) else 64.0,
                                }
                                for idx in range(3)
                            ],
                        }
                    elif cmd == "fans" and state.firmware_kind == "braiins-plus":
                        resp = {
                            "STATUS": [st_hdr],
                            "FANS": [
                                {"ID": idx, "RPM": rpm, "Speed": 68 + idx}
                                for idx, rpm in enumerate(state.fans[:4])
                            ],
                        }
                    elif cmd == "tunerstatus" and state.firmware_kind == "braiins-plus":
                        resp = {
                            "STATUS": [st_hdr],
                            "TUNERSTATUS": [
                                {
                                    "ApproximateMinerPowerConsumption": state.power_w,
                                    "PowerLimit": state.power_limit_w,
                                    "TunerMode": state.tuner_mode,
                                    "TunerChainStatus": [
                                        {"Chain": idx, "Status": state.tuner_chain_statuses[idx] if idx < len(state.tuner_chain_statuses) else ("Tuning" if idx == 2 else "Stable")}
                                        for idx in range(3)
                                    ],
                                }
                            ],
                        }
                    elif cmd == "stats":
                        stats_entry: dict[str, Any] = {
                            "Type": state.miner_type,
                            "frequency": state.frequency,
                            "temp_max": max(state.chip_temps or [0.0]),
                            "fan_num": len(state.fans),
                        }
                        for idx, bt in enumerate(state.board_temps, start=1):
                            stats_entry[f"temp_pcb{idx}"] = f"{int(bt)}-{int(bt)}"
                        for idx, ct in enumerate(state.chip_temps, start=1):
                            stats_entry[f"temp_chip{idx}"] = f"{int(ct)}-{int(ct)}"
                        for idx, rpm in enumerate(state.fans, start=1):
                            stats_entry[f"fan{idx}"] = rpm
                        if state.rate_ideal_ghs is not None:
                            stats_entry["total_rateideal"] = state.rate_ideal_ghs
                        if state.miner_count_override is not None:
                            stats_entry["miner_count"] = state.miner_count_override
                        elif state.chain_acn:
                            stats_entry["miner_count"] = len(state.chain_acn)
                        for idx, n in enumerate(state.chain_acn, start=1):
                            stats_entry[f"chain_acn{idx}"] = n
                            stats_entry[f"chain_acs{idx}"] = state.chain_acs[idx - 1] if idx - 1 < len(state.chain_acs) else " " + "o" * n
                        resp = {"STATUS": [{"STATUS": "S"}], "STATS": [{}, stats_entry]}
                    elif cmd == "pools":
                        if state.firmware_kind == "braiins-plus" and not state.extra_pools:
                            plist = [
                                {
                                    "POOL": idx,
                                    "URL": state.pool_url if idx == 0 else f"stratum+tcp://backup{idx}.pool.example.com:3333",
                                    "User": state.pool_user,
                                    "Status": "Dead" if state.pools_all_dead else ("Alive" if idx in {0, 1, 2} else "Dead"),
                                    "Stratum Active": False if state.pools_all_dead else (idx == 0),
                                    "Priority": idx,
                                    "Last Share Difficulty": 131072 if idx == 0 else 0,
                                    "Last Share Time": (state.when_ts - 9) if idx == 0 else 0,
                                    "Best Share": 891234567 if idx == 0 else 1024,
                                }
                                for idx in range(7)
                            ]
                            resp = {"STATUS": [st_hdr], "POOLS": plist}
                        else:
                            plist = [
                                {
                                    "POOL": 0,
                                    "URL": state.pool_url,
                                    "User": state.pool_user,
                                    "Status": "Dead" if state.pools_all_dead else ("Alive" if state.active_pool == 0 else "Dead"),
                                    "Stratum Active": False if state.pools_all_dead else (state.active_pool == 0),
                                    "Priority": 0,
                                }
                            ]
                            for idx, ep in enumerate(state.extra_pools, start=1):
                                plist.append(
                                    {
                                        "POOL": idx,
                                        "URL": ep["URL"],
                                        "User": ep["User"],
                                        "Status": "Alive" if state.active_pool == idx else "Dead",
                                        "Stratum Active": state.active_pool == idx,
                                        "Priority": idx,
                                    }
                                )
                            resp = {"STATUS": [st_hdr], "POOLS": plist}
                    elif cmd == "privileged":
                        if state.allow_privileged:
                            resp = {"STATUS": [{"STATUS": "S", "Msg": "Privileged access OK"}]}
                        else:
                            resp = {"STATUS": [{"STATUS": "E", "Msg": "Access denied to 'privileged' command"}]}
                    elif cmd == "addpool":
                        if not state.allow_privileged:
                            resp = {"STATUS": [{"STATUS": "E", "Msg": "Access denied"}]}
                        else:
                            param = str(req.get("parameter") or "")
                            parts = param.split(",", 2)
                            u = parts[0] if len(parts) > 0 else ""
                            usr = parts[1] if len(parts) > 1 else ""
                            state.extra_pools.append({"URL": u, "User": usr})
                            resp = {"STATUS": [{"STATUS": "S", "Msg": f"Added pool {len(state.extra_pools)}"}]}
                    elif cmd == "switchpool":
                        if not state.allow_privileged:
                            resp = {"STATUS": [{"STATUS": "E", "Msg": "Access denied"}]}
                        else:
                            try:
                                idx = int(req.get("parameter") or 0)
                            except (TypeError, ValueError):
                                idx = 0
                            state.active_pool = idx
                            if idx > 0 and idx <= len(state.extra_pools):
                                state.pool_url = state.extra_pools[idx - 1]["URL"]
                                state.pool_user = state.extra_pools[idx - 1]["User"]
                            resp = {"STATUS": [{"STATUS": "S", "Msg": f"Switched to pool {idx}"}]}
                    else:
                        resp = {"STATUS": [{"STATUS": "E", "Msg": "Invalid command"}]}
                payload = (json.dumps(resp) + "\x00").encode("utf-8")
                try:
                    self_h.request.sendall(payload)
                except OSError:
                    pass

        self.server = _ReusableTCPServer((host, port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def start(self) -> "MockCGMiner":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock AxeOS / NerdOCTAxe / CGMiner API")
    parser.add_argument("--gamma-port", type=int, default=8056)
    parser.add_argument("--nerd-port", type=int, default=8023)
    parser.add_argument("--antminer-port", type=int, default=4028)
    parser.add_argument("--faults", action="store_true", help="v1.10: start with example fault flags set")
    args = parser.parse_args()
    gamma = MockMiner(
        MinerState(name="gamma-mock", kind="gamma", frequency=525, core_voltage=1210, model="Gamma"),
        port=args.gamma_port,
    ).start()
    nerd = MockMiner(
        MinerState(
            name="nerd-mock",
            kind="nerdoctaxe",
            frequency=700,
            core_voltage=1210,
            asic_temps=[52, 53, 54, 51, 55, 56, 50, 53],
            model="NerdOCTAXE-γ",
        ),
        port=args.nerd_port,
    ).start()
    ant = MockCGMiner(CGMinerState(), port=args.antminer_port).start()
    if args.faults:
        gamma.state.overheat_mode = 1
        gamma.state.temp = 41.0
        gamma.state.vr_temp = 50.0
        gamma.state.reset_reason = "Reset due to brownout"
        gamma.state.logs_text = SAMPLE_AXEOS_LOG
        nerd.state.board_error = 3
        nerd.state.shutdown = True
        ant.state.chain_acn = [114, 114, 0]
        ant.state.fans = [6120, 0, 6090, 6150]
    print(f"Gamma mock     {gamma.url}  (GET /api/system/info)")
    print(f"NerdOCTAxe mock {nerd.url}  (GET /api/v2/dashboard)")
    print(f"Antminer mock  127.0.0.1:{ant.port}  (CGMiner TCP)")
    print("Ctrl+C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nStopping")
    finally:
        gamma.stop()
        nerd.stop()
        ant.stop()



@dataclass
class BraiinsMockState:
    username: str = "root"
    password: str = ""
    tuner_state: int = 2  # STABLE
    tuner_enabled: bool = True
    tuner_mode: int = 1  # POWER_TARGET
    power_target_w: int = 2800
    min_w: int = 1800
    max_w: int = 3400
    default_w: int = 2800
    token: str = "bos-mock-token-123"
    use_huffman: bool = True
    errors: list[dict[str, Any]] = field(default_factory=list)  # v1.10 REST /api/v1/miner/errors
    miner_status: str = "MINER_STATUS_NORMAL"
    log_text: str = ""
    actions: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class MockBraiinsGrpc:
    """Minimal HTTP/2 h2c gRPC server mocking braiins.bos.v1 on a local ephemeral port."""

    def __init__(self, state: Optional[BraiinsMockState] = None, host: str = "127.0.0.1", port: int = 0):
        import braiins_api as b
        self.b = b
        self.state = state or BraiinsMockState()
        self.host = host
        state_ref = self.state

        def _enc_hdrs(hdrs: list[tuple[str, str]]) -> bytes:
            out = bytearray()
            for k, v in hdrs:
                if k == ":status" and v == "200":
                    out.append(0x88)
                    continue
                kb = k.encode("utf-8")
                vb = v.encode("utf-8")
                if state_ref.use_huffman:
                    kh = b.hpack_huffman_encode(kb)
                    vh = b.hpack_huffman_encode(vb)
                    out.extend(b._hpack_encode_int(len(kh), 7, 0x80) if False else bytes([0x00]))
                    out.extend(b._hpack_encode_int(len(kh), 7, 0x80) + kh)
                    out.extend(b._hpack_encode_int(len(vh), 7, 0x80) + vh)
                else:
                    out.extend(b"\x00" + b._hpack_encode_str(k) + b._hpack_encode_str(v))
            return bytes(out)

        class GrpcHandler(socketserver.BaseRequestHandler):
            def handle(self_h) -> None:
                sock = self_h.request
                sock.settimeout(5.0)
                try:
                    preface = b._recv_exact(sock, len(b._H2_PREFACE))
                    if preface != b._H2_PREFACE:
                        return
                    sock.sendall(b._pack_frame(b._FRAME_SETTINGS, 0, 0, b""))
                    hpack_dec = b._HpackDecoder()
                    streams: dict[int, dict[str, Any]] = {}
                    while True:
                        try:
                            ftype, flags, sid, payload = b._read_frame(sock)
                        except (ConnectionError, OSError):
                            break
                        if ftype == b._FRAME_SETTINGS:
                            if not (flags & b._FLAG_ACK):
                                sock.sendall(b._pack_frame(b._FRAME_SETTINGS, b._FLAG_ACK, 0, b""))
                            continue
                        if ftype == b._FRAME_PING and not (flags & b._FLAG_ACK):
                            sock.sendall(b._pack_frame(b._FRAME_PING, b._FLAG_ACK, 0, payload))
                            continue
                        if ftype == b._FRAME_WINDOW_UPDATE:
                            continue
                        if ftype == b._FRAME_HEADERS:
                            st = streams.setdefault(sid, {"headers": {}, "data": bytearray()})
                            for k, v in hpack_dec.decode(payload):
                                st["headers"][k.lower()] = v
                        elif ftype == b._FRAME_DATA:
                            st = streams.setdefault(sid, {"headers": {}, "data": bytearray()})
                            st["data"].extend(payload)
                        if sid and (flags & b._FLAG_END_STREAM):
                            st = streams.pop(sid, {"headers": {}, "data": bytearray()})
                            raw = bytes(st["data"])
                            req_msg = raw[5:5 + struct.unpack("!I", raw[1:5])[0]] if len(raw) >= 5 else b""
                            path = st["headers"].get(":path", "")
                            auth = st["headers"].get("authorization", "")
                            grpc_status = 0
                            grpc_msg = ""
                            resp_msg = b""

                            with state_ref.lock:
                                state_ref.calls.append({"path": path, "auth": auth})
                                if path == "/braiins.bos.v1.AuthenticationService/Login":
                                    pf = b.parse_proto_fields(req_msg)
                                    u = b._first_str(pf, 1, "")
                                    p = b._first_str(pf, 2, "")
                                    if u != state_ref.username or p != state_ref.password:
                                        grpc_status = 16
                                        grpc_msg = "invalid credentials"
                                    else:
                                        resp_msg = b.encode_field_str(1, state_ref.token) + b.encode_field_varint(2, 3600)
                                elif auth != state_ref.token:
                                    grpc_status = 16
                                    grpc_msg = "unauthorized"
                                elif path == "/braiins.bos.v1.PerformanceService/GetTunerState":
                                    if state_ref.tuner_mode == 1:
                                        pt = b.encode_field_varint(1, 0) + b.encode_field_bytes(2, b.encode_field_varint(1, state_ref.power_target_w))
                                        resp_msg = b.encode_field_varint(1, state_ref.tuner_state) + b.encode_field_bytes(2, pt)
                                    else:
                                        resp_msg = b.encode_field_varint(1, state_ref.tuner_state) + b.encode_field_bytes(3, b"")
                                elif path == "/braiins.bos.v1.ConfigurationService/GetConstraints":
                                    ptc = (
                                        b.encode_field_bytes(1, b.encode_field_varint(1, state_ref.min_w))
                                        + b.encode_field_bytes(2, b.encode_field_varint(1, state_ref.max_w))
                                        + b.encode_field_bytes(3, b.encode_field_varint(1, state_ref.default_w))
                                    )
                                    resp_msg = b.encode_field_bytes(1, b.encode_field_bytes(1, ptc))
                                elif path == "/braiins.bos.v1.ConfigurationService/GetMinerConfiguration":
                                    tcfg = (
                                        b.encode_field_varint(1, 1 if state_ref.tuner_enabled else 0)
                                        + b.encode_field_varint(2, state_ref.tuner_mode)
                                        + b.encode_field_bytes(3, b.encode_field_varint(1, state_ref.power_target_w))
                                    )
                                    resp_msg = b.encode_field_bytes(3, tcfg)
                                elif path == "/braiins.bos.v1.PerformanceService/SetPowerTarget":
                                    pf = b.parse_proto_fields(req_msg)
                                    pw = b._decode_power_msg(b._first_bytes(pf, 2))
                                    if pw is None or pw < state_ref.min_w or pw > state_ref.max_w:
                                        grpc_status = 3
                                        grpc_msg = "power target out of range"
                                    else:
                                        state_ref.power_target_w = int(pw)
                                        resp_msg = b.encode_field_bytes(1, b.encode_field_varint(1, state_ref.power_target_w))
                                elif path == "/braiins.bos.v1.MinerService/GetErrors":
                                    chunks_e = []
                                    for e in (state_ref.errors or []):
                                        msg_e = str(e.get("message") or "")
                                        sub = b.encode_field_str(2, msg_e)
                                        for c in (e.get("error_codes") or []):
                                            cf = b.encode_field_str(1, str(c.get("code") or "")) + b.encode_field_str(2, str(c.get("name") or c.get("reason") or ""))
                                            sub += b.encode_field_bytes(3, cf)
                                        chunks_e.append(b.encode_field_bytes(1, sub))
                                    resp_msg = b"".join(chunks_e)
                                elif path == "/braiins.bos.v1.MinerService/GetMinerDetails":
                                    # stopped -> thermal_pause (field 7) with reason=1
                                    st_s = str(state_ref.miner_status or "").lower()
                                    if "thermal" in st_s:
                                        tp = b.encode_field_varint(1, 1)
                                        sr = b.encode_field_bytes(7, tp)
                                        ds = b.encode_field_bytes(1, sr)
                                        resp_msg = b.encode_field_bytes(18, ds)
                                    elif "dead_pools" in st_s:
                                        sr = b.encode_field_bytes(4, b"")
                                        ds = b.encode_field_bytes(1, sr)
                                        resp_msg = b.encode_field_bytes(18, ds)
                                    else:
                                        resp_msg = b.encode_field_varint(12, 2)
                                else:
                                    grpc_status = 12
                                    grpc_msg = f"unknown method {path}"

                            hdr_bytes = _enc_hdrs([(":status", "200"), ("content-type", "application/grpc")])
                            sock.sendall(b._pack_frame(b._FRAME_HEADERS, b._FLAG_END_HEADERS, sid, hdr_bytes))
                            if grpc_status == 0:
                                frame_body = struct.pack("!BI", 0, len(resp_msg)) + resp_msg
                                sock.sendall(b._pack_frame(b._FRAME_DATA, 0, sid, frame_body))
                            trl_list = [("grpc-status", str(grpc_status))]
                            if grpc_msg:
                                trl_list.append(("grpc-message", grpc_msg))
                            trl_bytes = _enc_hdrs(trl_list)
                            sock.sendall(b._pack_frame(b._FRAME_HEADERS, b._FLAG_END_HEADERS | b._FLAG_END_STREAM, sid, trl_bytes))
                except Exception:
                    pass

        self.server = _ReusableTCPServer((host, port), GrpcHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def start(self) -> "MockBraiinsGrpc":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class MockBraiinsRest:
    """Minimal HTTP/1.1 REST server mocking Braiins OS 25.07+ /api/v1 endpoints."""

    def __init__(self, state: Optional[BraiinsMockState] = None, host: str = "127.0.0.1", port: int = 0):
        self.state = state or BraiinsMockState()
        self.host = host
        state_ref = self.state

        class RestHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _read_json(self) -> dict[str, Any]:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0:
                    return {}
                raw = self.rfile.read(n)
                return json.loads(raw.decode("utf-8"))

            def _auth_ok(self) -> bool:
                hdr = self.headers.get("Authorization") or ""
                return hdr in {f"Bearer {state_ref.token}", state_ref.token}

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                if path == "/api/v1/auth/login":
                    body = self._read_json()
                    with state_ref.lock:
                        state_ref.calls.append({"path": path})
                        if body.get("username") == state_ref.username and body.get("password", "") == state_ref.password:
                            _json_response(self, 200, {"token": state_ref.token, "timeout_s": 3600})
                        else:
                            _json_response(self, 401, {"error": "unauthorized"})
                    return
                _json_response(self, 404, {"error": "not found"})

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if not self._auth_ok():
                    _json_response(self, 401, {"error": "unauthorized"})
                    return
                with state_ref.lock:
                    state_ref.calls.append({"path": path})
                    if path == "/api/v1/miner/errors":
                        _json_response(self, 200, {"errors": list(state_ref.errors)})
                        return
                    if path == "/api/v1/miner/details":
                        _json_response(self, 200, {"status": state_ref.miner_status, "hostname": "bos-mock", "mac_address": "02:00:00:00:00:03"})
                        return
                    if path.startswith("/api/v1/miner/log/"):
                        data = state_ref.log_text.encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/plain")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    if path == "/api/v1/performance/tuner-state":
                        _json_response(
                            self,
                            200,
                            {
                                "overall_tuner_state": state_ref.tuner_state,
                                "mode_state": {
                                    "power_target_mode_state": {
                                        "current_target": {"watt": state_ref.power_target_w}
                                    }
                                },
                            },
                        )
                        return
                    if path == "/api/v1/configuration/constraints":
                        _json_response(
                            self,
                            200,
                            {
                                "tuner_constraints": {
                                    "power_target": {
                                        "min": {"watt": state_ref.min_w},
                                        "max": {"watt": state_ref.max_w},
                                        "default": {"watt": state_ref.default_w},
                                    }
                                }
                            },
                        )
                        return
                    if path == "/api/v1/configuration/miner":
                        _json_response(
                            self,
                            200,
                            {
                                "tuner": {
                                    "enabled": state_ref.tuner_enabled,
                                    "tuner_mode": state_ref.tuner_mode,
                                    "power_target": {"watt": state_ref.power_target_w},
                                }
                            },
                        )
                        return
                _json_response(self, 404, {"error": "not found"})

            def do_PUT(self) -> None:
                path = urlparse(self.path).path
                if not self._auth_ok():
                    _json_response(self, 401, {"error": "unauthorized"})
                    return
                if path.startswith("/api/v1/actions/"):
                    act = path.rsplit("/", 1)[-1]
                    with state_ref.lock:
                        state_ref.actions.append(act)
                        if act == "resume":
                            state_ref.miner_status = "MINER_STATUS_NORMAL"
                    _json_response(self, 200 if act != "factory-reset" else 200, {})
                    return
                if path == "/api/v1/performance/power-target":
                    body = self._read_json()
                    w = int(body.get("watt") or 0)
                    with state_ref.lock:
                        state_ref.calls.append({"path": path, "watt": w})
                        if w < state_ref.min_w or w > state_ref.max_w:
                            _json_response(self, 400, {"error": "out of range"})
                        else:
                            state_ref.power_target_w = w
                            _json_response(self, 200, {"watt": w})
                    return
                _json_response(self, 404, {"error": "not found"})

        self.server = ThreadingHTTPServer((host, port), RestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def start(self) -> "MockBraiinsRest":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


# ---------------------------------------------------------------------------
# v1.10: stock Bitmain web UI (HTTP Digest) with /cgi-bin/log.cgi and /cgi-bin/reboot.cgi
# ---------------------------------------------------------------------------

@dataclass
class BitmainWebState:
    username: str = "root"
    password: str = "example-pass"
    realm: str = "antMiner Configuration"
    algorithm: str = "MD5"  # or SHA-256
    log_text: str = ""
    warning_json: Optional[dict[str, Any]] = None
    summary_json: Optional[dict[str, Any]] = None
    reboots: int = 0
    unauthorized: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class MockBitmainWeb:
    def __init__(self, state: Optional[BitmainWebState] = None, host: str = "127.0.0.1", port: int = 0):
        import hashlib
        import re as _re
        self.state = state or BitmainWebState()
        st = self.state
        nonce = "dcd98b7102dd2f0e8b11d0f600bfb0c093"

        def H(data: str) -> str:
            if st.algorithm.upper().startswith("SHA-256"):
                return hashlib.sha256(data.encode()).hexdigest()
            return hashlib.md5(data.encode()).hexdigest()  # noqa: S324

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _authed(self) -> bool:
                hdr = self.headers.get("Authorization") or ""
                if not hdr.startswith("Digest "):
                    return False
                f = {k.lower(): (a or b) for k, a, b in _re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', hdr[7:])}
                if f.get("username") != st.username or f.get("nonce") != nonce:
                    return False
                ha1 = H(f"{st.username}:{st.realm}:{st.password}")
                ha2 = H(f"{self.command}:{f.get('uri')}")
                want = H(f"{ha1}:{nonce}:{f.get('nc')}:{f.get('cnonce')}:auth:{ha2}")
                return f.get("response") == want

            def do_GET(self) -> None:
                if not self._authed():
                    with st.lock:
                        st.unauthorized += 1
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", f'Digest realm="{st.realm}", nonce="{nonce}", qop="auth", algorithm={st.algorithm}')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                path = urlparse(self.path).path
                if path == "/cgi-bin/log.cgi":
                    data = st.log_text.encode("utf-8")
                elif path == "/cgi-bin/warning.cgi" and st.warning_json is not None:
                    _json_response(self, 200, st.warning_json)
                    return
                elif path == "/cgi-bin/summary.cgi" and st.summary_json is not None:
                    _json_response(self, 200, st.summary_json)
                    return
                elif path == "/cgi-bin/reboot.cgi":
                    with st.lock:
                        st.reboots += 1
                    data = b"ok"
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def start(self) -> "MockBitmainWeb":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


SAMPLE_BITMAIN_LOG = """2026-01-01 00:00:01 driver-btm-api.c:  Chain[0]: find 76 asic, times 0
2026-01-01 00:00:02 driver-btm-api.c:  check_asic_number_with_power_on: Chain[2]: find 0 asic
2026-01-01 00:00:03 driver-btm-api.c:  will power off hash board 2
2026-01-01 00:00:09 thread.c:  ERROR_FAN_LOST: fan lost
"""

SAMPLE_AXEOS_LOG = """I (1234) bm1370Module: ASIC 0 not found
W (2345) power_management: Danger: High Temperature
I (3456) wifi: connected to ExampleNet, ip 192.168.1.50
"""


if __name__ == "__main__":
    main()
