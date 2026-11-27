"""Miner Hearth v1.17.0: node monitoring (opt-in, stdlib only).

Watches the nodes behind your mining: Bitcoin Core / Knots (and other coins' nodes that speak the same JSON-RPC,
such as eCash / Bitcoin ABC or CheetahCoin), LND and Core Lightning (REST), Electrum servers (Fulcrum / Electrs,
Electrum protocol over TCP or SSL), and the computer Miner Hearth runs on (CPU, temperature, memory, disk).

* Off until turned on in Settings > Node Monitoring. Nothing is contacted while it is off.
* Node credentials (RPC password, cookie file path, LND macaroon, CLN rune) are kept in userdata/nodes.json on
  this computer. They are never sent to the dashboard (the browser only sees "password set"), never written to
  the event log, backups or diagnostics.
* On Umbrel, the Bitcoin node (and Electrs) Umbrel exports to apps (APP_BITCOIN_NODE_IP, APP_BITCOIN_RPC_*) is
  offered automatically; its credentials stay in memory only.
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import os
import re
import secrets
import shutil
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

LOG = logging.getLogger("autotune.minerhearth.nodes")

KINDS = {
    "bitcoind": "Bitcoin Core / Knots (JSON-RPC)",
    "lnd": "LND (REST)",
    "cln": "Core Lightning (REST)",
    "electrum": "Electrum server (Fulcrum / Electrs)",
}
DEFAULT_PORTS = {"bitcoind": 8332, "lnd": 8080, "cln": 3010, "electrum": 50001}
COINS = ("BTC", "XEC", "CHTA", "BCH", "BSV", "DGB", "FB", "OTHER")
SECRET_FIELDS = ("password", "cookie_path", "macaroon_hex", "rune")
MAX_NODES = 20
HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*$|^\d{1,3}(?:\.\d{1,3}){3}$|^\[?[0-9A-Fa-f:]+\]?$")
FAILS_BEFORE_ALERT = 3

HttpFn = Callable[[str, Optional[bytes], dict, float, bool], tuple]


class NodeError(Exception):
    pass


def default_settings() -> dict[str, Any]:
    return {"enabled": False, "poll_sec": 60, "alerts": True, "behind_blocks": 3, "behind_min": 20,
            "host_stats": False, "disk_path": "", "low_disk_pct": 10, "temp_max_c": 80, "use_umbrel": True}


def normalize_settings(raw: Any) -> dict[str, Any]:
    d = default_settings()
    r = raw if isinstance(raw, dict) else {}

    def num(k: str, lo: float, hi: float, cast: Callable = int) -> None:
        try:
            v = cast(r.get(k, d[k]))
        except (TypeError, ValueError):
            return
        d[k] = max(lo, min(hi, v))

    for k in ("enabled", "alerts", "host_stats", "use_umbrel"):
        if k in r:
            d[k] = bool(r[k]) if not isinstance(r[k], str) else r[k].strip().lower() in {"1", "true", "yes", "on"}
    num("poll_sec", 15, 3600)
    num("behind_blocks", 1, 1000)
    num("behind_min", 1, 1440)
    num("low_disk_pct", 1, 50)
    num("temp_max_c", 40, 110)
    d["disk_path"] = str(r.get("disk_path") or "").strip()[:260]
    return d


def _clean_host(v: Any) -> str:
    h = str(v or "").strip()
    h = re.sub(r"^[a-z]+://", "", h, flags=re.I).split("/")[0]
    if h.count(":") == 1:  # host:port typed into the host field
        h = h.split(":")[0]
    if not h or len(h) > 253 or not HOST_RE.match(h):
        raise NodeError("Enter the node's host name or IP address (no http://)")
    return h


def validate_node(raw: Any, existing: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise NodeError("expected an object")
    kind = str(raw.get("kind") or (existing or {}).get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise NodeError("Choose a node type")
    out: dict[str, Any] = {"kind": kind}
    out["name"] = (str(raw.get("name") or "").strip() or f"{KINDS[kind].split(' (')[0]}")[:60]
    coin = str(raw.get("coin") or ("BTC" if kind != "bitcoind" else (existing or {}).get("coin") or "BTC")).strip().upper()
    out["coin"] = coin if coin in COINS else "OTHER"
    out["host"] = _clean_host(raw.get("host"))
    try:
        port = int(raw.get("port") or DEFAULT_PORTS[kind] + (1 if kind == "electrum" and raw.get("ssl") else 0))
    except (TypeError, ValueError):
        raise NodeError("Port must be a number") from None
    if not 1 <= port <= 65535:
        raise NodeError("Port must be 1-65535")
    out["port"] = port
    out["https"] = bool(raw.get("https", kind in {"lnd", "cln"}))
    out["ssl"] = bool(raw.get("ssl", False)) if kind == "electrum" else False
    out["tls_insecure"] = bool(raw.get("tls_insecure", True))  # self-signed certificates on home nodes
    out["user"] = str(raw.get("user") or "").strip()[:120] if kind == "bitcoind" else ""
    out["compare_to"] = str(raw.get("compare_to") or "").strip()[:40] if kind == "electrum" else ""
    out["enabled"] = bool(raw.get("enabled", True))
    prev = existing or {}
    for f in SECRET_FIELDS:
        v = raw.get(f)
        if v is None or (isinstance(v, str) and v == ""):
            out[f] = prev.get(f, "") if raw.get("clear_" + f) is not True else ""
        else:
            out[f] = str(v).strip()[:4096]
    if out["macaroon_hex"] and not re.fullmatch(r"[0-9a-fA-F]+", out["macaroon_hex"]):
        try:  # pasted as base64
            out["macaroon_hex"] = base64.b64decode(out["macaroon_hex"], validate=True).hex()
        except (ValueError, TypeError):
            raise NodeError("The macaroon must be hex (xxd -p admin.macaroon) or base64") from None
    for f in SECRET_FIELDS:
        if kind == "bitcoind" and f in {"macaroon_hex", "rune"} or kind == "lnd" and f in {"password", "cookie_path", "rune"} \
                or kind == "cln" and f in {"password", "cookie_path", "macaroon_hex"} or kind == "electrum":
            out[f] = ""
    return out


def public_node(n: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in n.items() if k not in SECRET_FIELDS}
    for f in SECRET_FIELDS:
        out["has_" + f] = bool(n.get(f))
    return out


class NodeStore:
    """userdata/nodes.json (credentials stay in this file on this computer)."""

    def __init__(self, userdata_dir: Optional[Path]):
        self.path = (Path(userdata_dir) / "nodes.json") if userdata_dir else None
        self._lock = threading.RLock()
        self.nodes: list[dict[str, Any]] = []
        self.load_error = ""
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                for n in (data.get("nodes") if isinstance(data, dict) else []) or []:
                    try:
                        ent = validate_node(n, n)
                        ent["id"] = str(n.get("id") or self._new_id())[:40]
                        self.nodes.append(ent)
                    except NodeError:
                        continue
            except (OSError, ValueError) as exc:
                self.load_error = f"nodes.json could not be read: {exc}"

    @staticmethod
    def _new_id() -> str:
        return "n" + secrets.token_hex(4)

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name("nodes.json.tmp")
        tmp.write_text(json.dumps({"format": "miner-hearth-nodes", "version": 1, "nodes": self.nodes}, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def list_public(self) -> list[dict[str, Any]]:
        with self._lock:
            return [public_node(n) for n in self.nodes]

    def get(self, nid: str) -> Optional[dict[str, Any]]:
        with self._lock:
            for n in self.nodes:
                if n["id"] == nid:
                    return copy.deepcopy(n)
        return None

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self.nodes)

    def add(self, raw: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if len(self.nodes) >= MAX_NODES:
                raise NodeError(f"Up to {MAX_NODES} nodes")
            ent = validate_node(raw)
            ent["id"] = self._new_id()
            self.nodes.append(ent)
            self.save()
            return public_node(ent)

    def update(self, nid: str, raw: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for i, n in enumerate(self.nodes):
                if n["id"] == nid:
                    ent = validate_node(raw, n)
                    ent["id"] = nid
                    self.nodes[i] = ent
                    self.save()
                    return public_node(ent)
        raise NodeError("node not found")

    def delete(self, nid: str) -> bool:
        with self._lock:
            before = len(self.nodes)
            self.nodes = [n for n in self.nodes if n["id"] != nid]
            if len(self.nodes) != before:
                self.save()
                return True
        return False


def umbrel_nodes(env: Optional[dict[str, str]] = None) -> list[dict[str, Any]]:
    """Nodes Umbrel exports to apps through environment variables (credentials stay in memory)."""
    env = dict(os.environ) if env is None else env
    out: list[dict[str, Any]] = []
    ip = env.get("APP_BITCOIN_NODE_IP", "").strip()
    if ip:
        try:
            out.append({"id": "umbrel-bitcoin", "source": "umbrel", "kind": "bitcoind", "name": "Umbrel Bitcoin node", "coin": "BTC",
                        "host": _clean_host(ip), "port": int(env.get("APP_BITCOIN_RPC_PORT") or 8332), "https": False, "ssl": False,
                        "tls_insecure": True, "user": env.get("APP_BITCOIN_RPC_USER", ""), "password": env.get("APP_BITCOIN_RPC_PASS", ""),
                        "cookie_path": "", "macaroon_hex": "", "rune": "", "compare_to": "", "enabled": True})
        except (NodeError, ValueError):
            pass
    eip = env.get("APP_ELECTRS_NODE_IP", "").strip()
    if eip:
        try:
            out.append({"id": "umbrel-electrs", "source": "umbrel", "kind": "electrum", "name": "Umbrel Electrs", "coin": "BTC",
                        "host": _clean_host(eip), "port": int(env.get("APP_ELECTRS_NODE_PORT") or 50001), "https": False, "ssl": False,
                        "tls_insecure": True, "user": "", "password": "", "cookie_path": "", "macaroon_hex": "", "rune": "",
                        "compare_to": "umbrel-bitcoin" if ip else "", "enabled": True})
        except (NodeError, ValueError):
            pass
    return out


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
def default_http(url: str, body: Optional[bytes], headers: dict, timeout: float, insecure: bool) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=dict(headers, **{"User-Agent": "MinerHearth-nodes/1"}),
                                 method="POST" if body is not None else "GET")
    ctx = None
    if url.lower().startswith("https://"):
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:  # noqa: S310 (user-configured node)
            return int(r.status), r.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(64 * 1024) if exc.fp else b""


def _host_for_url(h: str) -> str:
    return f"[{h.strip('[]')}]" if ":" in h else h


def _read_cookie(path: str) -> tuple[str, str]:
    try:
        txt = Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise NodeError(f"cookie file not readable ({exc.__class__.__name__})") from None
    if ":" not in txt:
        raise NodeError("cookie file is not in user:password form")
    u, p = txt.split(":", 1)
    return u, p


class BitcoinRPC:
    def __init__(self, node: dict[str, Any], http: HttpFn = default_http, timeout: float = 8.0):
        self.node, self.http, self.timeout = node, http, timeout
        self._id = 0

    def call(self, method: str, params: Optional[list] = None) -> Any:
        n = self.node
        user, pw = n.get("user", ""), n.get("password", "")
        if n.get("cookie_path"):
            user, pw = _read_cookie(n["cookie_path"])
        self._id += 1
        body = json.dumps({"jsonrpc": "1.0", "id": f"mh{self._id}", "method": method, "params": params or []}).encode()
        hdr = {"Content-Type": "application/json"}
        if user or pw:
            hdr["Authorization"] = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
        url = f"{'https' if n.get('https') else 'http'}://{_host_for_url(n['host'])}:{n['port']}/"
        try:
            status, data = self.http(url, body, hdr, self.timeout, bool(n.get("tls_insecure", True)))
        except (OSError, urllib.error.URLError, socket.timeout, ValueError) as exc:
            raise NodeError(_net_reason(exc)) from None
        if status == 401:
            raise NodeError("RPC login refused (check the RPC user and password, or the cookie file)")
        if status == 403:
            raise NodeError("RPC refused this computer (add it to rpcallowip on the node)")
        try:
            obj = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            raise NodeError(f"not a JSON-RPC answer (HTTP {status})") from None
        if isinstance(obj, dict) and obj.get("error"):
            err = obj["error"]
            raise NodeError(f"RPC {method}: {err.get('message') if isinstance(err, dict) else err}")
        if status >= 400 or not isinstance(obj, dict):
            raise NodeError(f"HTTP {status}")
        return obj.get("result")


def _net_reason(exc: BaseException) -> str:
    r = getattr(exc, "reason", exc)
    s = str(r) or r.__class__.__name__
    if isinstance(r, socket.timeout) or "timed out" in s:
        return "no answer (timed out)"
    if "refused" in s.lower():
        return "connection refused (is the node running, and is RPC / REST enabled on this port?)"
    if "Name or service not known" in s or "getaddrinfo" in s or "nodename" in s:
        return "host name not found"
    return s[:160]


def poll_bitcoind(node: dict[str, Any], http: HttpFn = default_http) -> dict[str, Any]:
    rpc = BitcoinRPC(node, http)
    bc = rpc.call("getblockchaininfo") or {}
    m: dict[str, Any] = {
        "chain": bc.get("chain"), "blocks": bc.get("blocks"), "headers": bc.get("headers"),
        "progress_pct": round(float(bc.get("verificationprogress") or 0) * 100, 2) if bc.get("verificationprogress") is not None else None,
        "ibd": bool(bc.get("initialblockdownload", False)), "pruned": bool(bc.get("pruned", False)),
        "size_on_disk_gb": round(float(bc["size_on_disk"]) / 1e9, 1) if bc.get("size_on_disk") is not None else None,
        "tip_time": bc.get("time") or bc.get("mediantime"),
    }
    if bc.get("bestblockhash") and not bc.get("time"):
        try:
            hdr = rpc.call("getblockheader", [bc["bestblockhash"]]) or {}
            m["tip_time"] = hdr.get("time") or m["tip_time"]
        except NodeError:
            pass
    try:
        ni = rpc.call("getnetworkinfo") or {}
        m["peers"] = ni.get("connections")
        m["peers_in"], m["peers_out"] = ni.get("connections_in"), ni.get("connections_out")
        m["version"] = str(ni.get("subversion") or ni.get("version") or "").strip("/")[:60]
    except NodeError:
        m["peers"] = None
    try:
        mp = rpc.call("getmempoolinfo") or {}
        m["mempool_tx"] = mp.get("size")
        m["mempool_mb"] = round(float(mp.get("bytes") or 0) / 1e6, 2) if mp.get("bytes") is not None else None
        m["mempool_usage_mb"] = round(float(mp.get("usage") or 0) / 1e6, 1) if mp.get("usage") is not None else None
        m["mempool_minfee"] = mp.get("mempoolminfee")
    except NodeError:
        pass
    return m


def _rest(node: dict[str, Any], path: str, http: HttpFn, headers: dict, post: bool = False) -> dict[str, Any]:
    url = f"{'https' if node.get('https', True) else 'http'}://{_host_for_url(node['host'])}:{node['port']}{path}"
    try:
        status, data = http(url, b"{}" if post else None, dict(headers, **({"Content-Type": "application/json"} if post else {})),
                            8.0, bool(node.get("tls_insecure", True)))
    except (OSError, urllib.error.URLError, socket.timeout, ValueError) as exc:
        raise NodeError(_net_reason(exc)) from None
    if status in (401, 403):
        raise NodeError("REST login refused (check the macaroon / rune)")
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        raise NodeError(f"not a JSON answer (HTTP {status})") from None
    if status >= 400 or not isinstance(obj, dict):
        raise NodeError(f"HTTP {status}: {str(obj)[:120]}")
    return obj


def poll_lnd(node: dict[str, Any], http: HttpFn = default_http) -> dict[str, Any]:
    hdr = {"Grpc-Metadata-macaroon": node.get("macaroon_hex", "")} if node.get("macaroon_hex") else {}
    gi = _rest(node, "/v1/getinfo", http, hdr)
    return {"alias": str(gi.get("alias") or "")[:60], "version": str(gi.get("version") or "")[:40],
            "synced_to_chain": bool(gi.get("synced_to_chain")), "synced_to_graph": bool(gi.get("synced_to_graph")),
            "blocks": _int(gi.get("block_height")), "channels_active": _int(gi.get("num_active_channels")),
            "channels_inactive": _int(gi.get("num_inactive_channels")), "channels_pending": _int(gi.get("num_pending_channels")),
            "peers": _int(gi.get("num_peers"))}


def poll_cln(node: dict[str, Any], http: HttpFn = default_http) -> dict[str, Any]:
    hdr = {"Rune": node.get("rune", "")} if node.get("rune") else {}
    gi = _rest(node, "/v1/getinfo", http, hdr, post=True)
    warn = gi.get("warning_bitcoind_sync") or gi.get("warning_lightningd_sync") or ""
    return {"alias": str(gi.get("alias") or "")[:60], "version": str(gi.get("version") or "")[:40],
            "synced_to_chain": not bool(warn), "sync_warning": str(warn)[:160], "blocks": _int(gi.get("blockheight")),
            "channels_active": _int(gi.get("num_active_channels")), "channels_inactive": _int(gi.get("num_inactive_channels")),
            "channels_pending": _int(gi.get("num_pending_channels")), "peers": _int(gi.get("num_peers"))}


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


SockFn = Callable[[str, int, float, bool, bool], Any]


def default_sock(host: str, port: int, timeout: float, use_ssl: bool, insecure: bool) -> Any:
    s = socket.create_connection((host.strip("[]"), port), timeout=timeout)
    if use_ssl:
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        s = ctx.wrap_socket(s, server_hostname=host.strip("[]"))
    s.settimeout(timeout)
    return s


def poll_electrum(node: dict[str, Any], sock: SockFn = default_sock) -> dict[str, Any]:
    try:
        s = sock(node["host"], int(node["port"]), 8.0, bool(node.get("ssl")), bool(node.get("tls_insecure", True)))
    except (OSError, ssl.SSLError, socket.timeout) as exc:
        raise NodeError(_net_reason(exc)) from None
    try:
        req = (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "server.version", "params": ["MinerHearth", "1.4"]}) + "\n"
               + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "blockchain.headers.subscribe", "params": []}) + "\n")
        s.sendall(req.encode())
        buf, got, deadline = b"", {}, time.monotonic() + 8.0
        while len(got) < 2 and time.monotonic() < deadline:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    obj = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("id") in (1, 2):
                    got[obj["id"]] = obj
    except (OSError, socket.timeout) as exc:
        raise NodeError(_net_reason(exc)) from None
    finally:
        try:
            s.close()
        except OSError:
            pass
    if 2 not in got:
        raise NodeError("no answer to blockchain.headers.subscribe")
    if got[2].get("error"):
        raise NodeError(f"server error: {got[2]['error']}")
    res = got[2].get("result") or {}
    ver = (got.get(1) or {}).get("result")
    return {"blocks": _int(res.get("height") if isinstance(res, dict) else None),
            "version": (" ".join(str(x) for x in ver) if isinstance(ver, list) else str(ver or ""))[:60]}


# ---------------------------------------------------------------------------
# Host statistics (the computer Miner Hearth runs on)
# ---------------------------------------------------------------------------
class HostStats:
    def __init__(self, proc_root: str = "/proc", sys_root: str = "/sys"):
        self.proc_root, self.sys_root = Path(proc_root), Path(sys_root)
        self._last_cpu: Optional[tuple[float, float]] = None

    def _cpu_times(self) -> Optional[tuple[float, float]]:
        """(idle, total) since boot."""
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes

                idle, kern, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
                if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
                    return None
                f = lambda t: (t.dwHighDateTime << 32) | t.dwLowDateTime  # noqa: E731
                return float(f(idle)), float(f(kern) + f(user))  # kernel time includes idle time
            except Exception:
                return None
        try:
            parts = (self.proc_root / "stat").read_text().splitlines()[0].split()[1:]
            vals = [float(x) for x in parts[:8]]
            return vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals)
        except (OSError, IndexError, ValueError):
            return None

    def cpu_pct(self) -> Optional[float]:
        cur = self._cpu_times()
        if cur is None:
            return None
        last, self._last_cpu = self._last_cpu, cur
        if last is None:
            return None
        didle, dtot = cur[0] - last[0], cur[1] - last[1]
        return round(max(0.0, min(100.0, 100.0 * (1 - didle / dtot))), 1) if dtot > 0 else None

    def memory(self) -> dict[str, Any]:
        if os.name == "nt":
            try:
                import ctypes

                class MEMSTAT(ctypes.Structure):
                    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                                ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
                st = MEMSTAT()
                st.dwLength = ctypes.sizeof(MEMSTAT)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                    return {"ram_total_gb": round(st.ullTotalPhys / 1e9, 1), "ram_used_pct": float(st.dwMemoryLoad)}
            except Exception:
                pass
            return {}
        try:
            info = {}
            for line in (self.proc_root / "meminfo").read_text().splitlines():
                k, _, v = line.partition(":")
                info[k.strip()] = float(v.split()[0]) * 1024 if v.split() else 0.0
            tot, avail = info.get("MemTotal"), info.get("MemAvailable", info.get("MemFree"))
            if tot:
                return {"ram_total_gb": round(tot / 1e9, 1), "ram_used_pct": round(100.0 * (1 - (avail or 0) / tot), 1)}
        except (OSError, ValueError):
            pass
        return {}

    def temperature_c(self) -> Optional[float]:
        if os.name == "nt":
            return None  # needs WMI / vendor drivers; not read on Windows
        best = None
        try:
            for z in sorted((self.sys_root / "class" / "thermal").glob("thermal_zone*")):
                try:
                    v = float((z / "temp").read_text().strip()) / 1000.0
                except (OSError, ValueError):
                    continue
                if 0 < v < 150:
                    best = v if best is None else max(best, v)
        except OSError:
            return None
        return round(best, 1) if best is not None else None

    def disk(self, path: str) -> dict[str, Any]:
        try:
            u = shutil.disk_usage(path)
        except OSError:
            return {"disk_path": path, "disk_error": "not readable"}
        return {"disk_path": path, "disk_total_gb": round(u.total / 1e9, 1), "disk_free_gb": round(u.free / 1e9, 1),
                "disk_free_pct": round(100.0 * u.free / u.total, 1) if u.total else None}

    def snapshot(self, disk_path: str) -> dict[str, Any]:
        out: dict[str, Any] = {"cpu_pct": self.cpu_pct(), "temp_c": self.temperature_c(), "cpus": os.cpu_count()}
        out.update(self.memory())
        out.update(self.disk(disk_path))
        try:
            out["load1"] = round(os.getloadavg()[0], 2)
        except (OSError, AttributeError):
            pass
        return out


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------
class NodeMonitor:
    def __init__(self, userdata_dir: Optional[Path], settings: Callable[[], dict[str, Any]], emit: Callable[[str, str, str, bool], Any],
                 clock: Callable[[], float] = time.time, http: HttpFn = default_http, sock: SockFn = default_sock,
                 env: Optional[dict[str, str]] = None, network_height: Optional[Callable[[str], Optional[int]]] = None,
                 host: Optional[HostStats] = None, default_disk: str = ""):
        self.store = NodeStore(userdata_dir)
        self.settings, self.emit, self.clock = settings, emit, clock
        self.http, self.sock = http, sock
        self.umbrel = umbrel_nodes(env)
        self.network_height = network_height or (lambda coin: None)
        self.host = host or HostStats()
        self.default_disk = default_disk or str(userdata_dir or ".")
        self._lock = threading.RLock()
        self.status: dict[str, dict[str, Any]] = {}
        self.host_status: dict[str, Any] = {}
        self._alerts: dict[str, dict[str, Any]] = {}  # key -> {"since": ts, "sent": bool}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    # --- config ---
    def nodes(self) -> list[dict[str, Any]]:
        s = self.settings()
        out = list(self.store.all())
        if s.get("use_umbrel", True):
            out = [dict(n) for n in self.umbrel] + out
        return out

    def find(self, nid: str) -> Optional[dict[str, Any]]:
        for n in self.nodes():
            if n["id"] == nid:
                return n
        return None

    # --- polling ---
    def poll_node(self, n: dict[str, Any]) -> dict[str, Any]:
        k = n["kind"]
        if k == "bitcoind":
            return poll_bitcoind(n, self.http)
        if k == "lnd":
            return poll_lnd(n, self.http)
        if k == "cln":
            return poll_cln(n, self.http)
        if k == "electrum":
            return poll_electrum(n, self.sock)
        raise NodeError("unknown node type")

    def test(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Try a node from the Settings form (unsaved values; a blank secret uses the saved one)."""
        prev = self.find(str(raw.get("id") or "")) if raw.get("id") else None
        n = validate_node(raw, prev)
        try:
            m = self.poll_node(n)
            return {"ok": True, "metrics": m, "summary": summarize(n, m)}
        except NodeError as exc:
            return {"ok": False, "error": str(exc)}

    def tick(self, now: Optional[float] = None, force: bool = False) -> None:
        now = self.clock() if now is None else now
        s = self.settings()
        if not s.get("enabled"):
            return
        nodes = [n for n in self.nodes() if n.get("enabled", True)]
        ids = {n["id"] for n in nodes}
        with self._lock:
            for gone in [k for k in self.status if k not in ids]:
                self.status.pop(gone, None)
        for n in nodes:
            st = self.status.get(n["id"]) or {"fails": 0, "checked_ts": 0}
            if not force and now - float(st.get("checked_ts") or 0) < s["poll_sec"]:
                continue
            try:
                m = self.poll_node(n)
                st = {"ok": True, "error": "", "metrics": m, "checked_ts": now, "ok_ts": now, "fails": 0,
                      "summary": summarize(n, m)}
            except NodeError as exc:
                st = dict(st, ok=False, error=str(exc), checked_ts=now, fails=int(st.get("fails") or 0) + 1)
            except Exception as exc:  # never let one node stop the others
                LOG.debug("node poll failed", exc_info=True)
                st = dict(st, ok=False, error=f"{exc.__class__.__name__}", checked_ts=now, fails=int(st.get("fails") or 0) + 1)
            with self._lock:
                self.status[n["id"]] = st
        if s.get("host_stats"):
            if force or now - float(self.host_status.get("checked_ts") or 0) >= min(60, s["poll_sec"]):
                snap = self.host.snapshot(s.get("disk_path") or self.default_disk)
                snap["checked_ts"] = now
                with self._lock:
                    self.host_status = snap
        else:
            self.host_status = {}
        self.evaluate(now, s, nodes)

    # --- alerts ---
    def _cond(self, key: str, active: bool, now: float, hold_sec: float, title: str, msg: str, ok_title: str, ok_msg: str,
              alerts_on: bool) -> None:
        a = self._alerts.get(key)
        if active:
            if a is None:
                a = self._alerts[key] = {"since": now, "sent": False}
            if not a["sent"] and now - a["since"] >= hold_sec:
                a["sent"] = True
                a["title"] = title
                if alerts_on:
                    self.emit(title, msg, "problem", True)
        elif a is not None:
            self._alerts.pop(key, None)
            if a.get("sent") and alerts_on:
                self.emit(ok_title, ok_msg, "info", False)

    def evaluate(self, now: float, s: dict[str, Any], nodes: list[dict[str, Any]]) -> None:
        on = bool(s.get("alerts", True))
        hold = float(s["behind_min"]) * 60.0
        lagmax = int(s["behind_blocks"])
        by_id = {n["id"]: n for n in nodes}
        for n in nodes:
            st = self.status.get(n["id"]) or {}
            nm = n["name"]
            down = int(st.get("fails") or 0) >= FAILS_BEFORE_ALERT
            self._cond(f"{n['id']}:down", down, now, 0, f"Node unreachable: {nm}",
                       f"{nm} ({n['host']}:{n['port']}) has not answered {FAILS_BEFORE_ALERT} checks in a row: {st.get('error') or 'no answer'}",
                       f"Node back: {nm}", f"{nm} is answering again.", on)
            m = (st.get("metrics") or {}) if st.get("ok") else {}
            if not m:
                continue
            if n["kind"] == "bitcoind":
                ref = max([x for x in (m.get("headers"), self.network_height(n.get("coin", "BTC")) if n.get("coin", "BTC") == "BTC" and m.get("chain") in (None, "main") else None) if isinstance(x, int)] or [0])
                lag = (ref - int(m["blocks"])) if isinstance(m.get("blocks"), int) and ref else 0
                behind = lag >= lagmax and not m.get("ibd")
                self._cond(f"{n['id']}:behind", behind, now, hold, f"Node falling behind: {nm}",
                           f"{nm} is {lag} block(s) behind (at {m.get('blocks')}, network {ref}).", f"Node caught up: {nm}",
                           f"{nm} is at the chain tip again ({m.get('blocks')}).", on)
                self._cond(f"{n['id']}:peers", m.get("peers") == 0, now, max(hold / 2, 300), f"Node has no peers: {nm}",
                           f"{nm} has 0 peer connections, so it cannot hear about new blocks.", f"Node has peers again: {nm}",
                           f"{nm} has {m.get('peers')} peers again.", on)
            elif n["kind"] in {"lnd", "cln"}:
                self._cond(f"{n['id']}:sync", not m.get("synced_to_chain", True), now, hold, f"Lightning node not synced: {nm}",
                           f"{nm} reports it is not synced to the chain{(': ' + m['sync_warning']) if m.get('sync_warning') else ''}.",
                           f"Lightning node synced: {nm}", f"{nm} is synced to the chain again.", on)
            elif n["kind"] == "electrum":
                ref_node = by_id.get(n.get("compare_to") or "")
                rm = ((self.status.get(ref_node["id"]) or {}).get("metrics") or {}) if ref_node else {}
                lag = (int(rm["blocks"]) - int(m["blocks"])) if isinstance(rm.get("blocks"), int) and isinstance(m.get("blocks"), int) else 0
                self._cond(f"{n['id']}:behind", lag >= lagmax, now, hold, f"Electrum server behind its node: {nm}",
                           f"{nm} is at {m.get('blocks')}, {lag} block(s) behind {ref_node['name'] if ref_node else 'the node'} ({rm.get('blocks')}).",
                           f"Electrum server caught up: {nm}", f"{nm} is at the node's tip again.", on)
        h = self.host_status
        if s.get("host_stats") and h:
            fp = h.get("disk_free_pct")
            self._cond("host:disk", isinstance(fp, (int, float)) and fp < float(s["low_disk_pct"]), now, 0,
                       "Low disk space on the Miner Hearth computer",
                       f"Only {fp}% ({h.get('disk_free_gb')} GB) free on {h.get('disk_path')}.", "Disk space OK again",
                       f"{fp}% free on {h.get('disk_path')}.", on)
            t = h.get("temp_c")
            self._cond("host:temp", isinstance(t, (int, float)) and t >= float(s["temp_max_c"]), now, 300,
                       "Miner Hearth computer is running hot", f"CPU / board temperature {t} °C (alert at {s['temp_max_c']} °C).",
                       "Computer temperature OK again", f"Temperature {t} °C.", on)

    # --- output ---
    def public_status(self) -> dict[str, Any]:
        s = self.settings()
        out = []
        with self._lock:
            for n in self.nodes():
                st = self.status.get(n["id"]) or {}
                pub = public_node(n)
                pub.pop("user", None)
                pub["source"] = n.get("source", "user")
                pub.update({"ok": st.get("ok"), "error": st.get("error", ""), "checked_ts": st.get("checked_ts"),
                            "metrics": st.get("metrics") or {}, "summary": st.get("summary", ""),
                            "alerts": [a.get("title") for k, a in self._alerts.items() if k.startswith(n["id"] + ":") and a.get("sent")]})
                out.append(pub)
            host = dict(self.host_status)
            host_alerts = [a.get("title") for k, a in self._alerts.items() if k.startswith("host:") and a.get("sent")]
        return {"enabled": bool(s.get("enabled")), "nodes": out, "host": host, "host_alerts": host_alerts,
                "umbrel_detected": bool(self.umbrel), "kinds": KINDS, "coins": list(COINS)}

    # --- thread ---
    def start(self) -> "NodeMonitor":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="mh-nodes", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        time.sleep(2.0)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                LOG.exception("Node monitoring round failed")
            self._wake.wait(5.0)
            self._wake.clear()


def summarize(n: dict[str, Any], m: dict[str, Any]) -> str:
    k = n["kind"]
    if k == "bitcoind":
        parts = [f"height {m.get('blocks')}"]
        if m.get("ibd") or (m.get("progress_pct") is not None and m["progress_pct"] < 99.99):
            parts.append(f"syncing {m.get('progress_pct')}%")
        if m.get("peers") is not None:
            parts.append(f"{m['peers']} peers")
        if m.get("mempool_tx") is not None:
            parts.append(f"mempool {m['mempool_tx']} tx")
        return ", ".join(parts)
    if k in {"lnd", "cln"}:
        return f"height {m.get('blocks')}, {m.get('channels_active')} active channels{'' if m.get('synced_to_chain') else ', NOT synced'}"
    if k == "electrum":
        return f"height {m.get('blocks')}{(' (' + m['version'] + ')') if m.get('version') else ''}"
    return ""
