"""Miner Hearth v1.15.5: pool-reported found blocks (stdlib only).

Some solo pools publish every block they find, with the finder's address and worker name. For coins whose
difficulty is tiny and spiky (CheetahCoin), or firmware that does not count blocks, that is the most
reliable "your machine found a block" signal there is. Supported now: HeliosPool (CKPool), all coins it runs.

Only two small files are read per pool server: blocks.pending.json (found, waiting for confirmations) and
the last 128 KB of blocks.json (confirmed), via an HTTP Range request. Nothing is ever sent to the pool.
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Optional

RangeFetcher = Callable[[str, dict, float], bytes]

HELIOS_COINS = {"btc": "BTC", "bch": "BCH", "dgb": "DGB", "chta": "CHTA", "wjk": "WJK", "fix": "FIX", "xec": "XEC", "zec": "ZEC", "grlc": "GRLC"}
HELIOS_REGIONS = ("us-west", "us-east", "eu-west", "au-south")
TAIL_BYTES = 131072
USER_AGENT = "MinerHearth/1.15 (+block notifications)"


def default_range_fetch(url: str, headers: dict, timeout: float = 10.0) -> bytes:
    req = urllib.request.Request(url, headers=dict({"User-Agent": USER_AGENT, "Accept": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:  # noqa: S310 fixed https host
            return resp.read(TAIL_BYTES + 65536)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"{url}: {exc}") from exc


def helios_coin(pool_url: str) -> Optional[str]:
    """'stratum+tcp://chta.heliospool.com:3336' -> 'chta' (None for other pools)."""
    h = re.sub(r"^[a-z0-9+.-]+://", "", str(pool_url or "").strip().lower()).split("/", 1)[0].split("@")[-1].split(":", 1)[0]
    m = re.fullmatch(r"([a-z0-9]+)\.heliospool\.com", h)
    if m and m.group(1) in HELIOS_COINS:
        return m.group(1)
    return None


def _norm_addr(a: str) -> str:
    a = str(a or "").strip()
    low = a.lower()
    if low.startswith(("ecash:", "bitcoincash:")):
        return low.split(":", 1)[1]
    if re.fullmatch(r"[qp][02-9ac-hj-np-z]{41}", low) or low.startswith(("bc1", "tb1", "dgb1", "ltc1")):
        return low
    return a  # base58 is case sensitive


def split_pool_user(user: str) -> tuple[str, str]:
    u = str(user or "").strip()
    if "." in u:
        a, w = u.split(".", 1)
        return a.strip(), w.strip()
    return u, ""


def _parse_lines(raw: bytes, partial_first: bool) -> list[dict[str, Any]]:
    txt = raw.decode("utf-8", "replace")
    txt = txt.strip()
    if txt.startswith("["):
        try:
            d = json.loads(txt)
            return [x for x in d if isinstance(x, dict)]
        except json.JSONDecodeError:
            pass
    lines = txt.splitlines()
    if partial_first and lines:
        lines = lines[1:]  # Range request: the first line is usually cut
    out = []
    for ln in lines:
        ln = ln.strip().rstrip(",")
        if not ln.startswith("{"):
            continue
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _ts(v: Any) -> Optional[float]:
    try:
        return datetime.strptime(str(v), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


class PoolBlockFeed:
    """Polls pool block lists for the addresses this PC's machines mine to; returns new / confirmed / orphaned blocks."""

    POLL_SEC = 90.0
    FIRST_RUN_LOOKBACK_SEC = 2 * 3600.0

    def __init__(self, fetch: Optional[RangeFetcher] = None, clock: Callable[[], float] = time.time):
        self.fetch = fetch or default_range_fetch
        self.clock = clock
        self.last_poll: dict[str, float] = {}
        self.errors: dict[str, str] = {}
        self.started = clock()

    def poll(self, watch: dict[str, set[str]], seen: dict[str, str], now: Optional[float] = None, force: bool = False) -> list[dict[str, Any]]:
        """watch: helios coin id -> set of normalized addresses. seen: block key -> last state sent ('pending'/'confirmed'/'orphaned').
        Returns [{coin, key, state, height, hash, address, worker, device_ua, diff, netdiff, ts, source}] for new states only."""
        now = self.clock() if now is None else now
        out: list[dict[str, Any]] = []
        for hc, addrs in sorted(watch.items()):
            if not addrs or hc not in HELIOS_COINS:
                continue
            if not force and now - self.last_poll.get(hc, 0.0) < self.POLL_SEC:
                continue
            self.last_poll[hc] = now
            found: dict[str, dict[str, Any]] = {}
            errs = []
            for region in HELIOS_REGIONS:
                base = f"https://api-{hc}-{region}.heliospool.com/pool/"
                for fname, state, hdr in (("blocks.pending.json", "pending", {}),
                                          ("blocks.json", "confirmed", {"Range": f"bytes=-{TAIL_BYTES}"}),
                                          ("blocks.orphaned.json", "orphaned", {"Range": f"bytes=-{TAIL_BYTES // 4}"})):
                    try:
                        rows = _parse_lines(self.fetch(base + fname, hdr, 10.0), partial_first=bool(hdr))
                    except Exception as exc:  # noqa: BLE001 - a pool server being down never breaks monitoring
                        errs.append(f"{region}/{fname}: {str(exc)[:120]}")
                        continue
                    for r in rows:
                        if _norm_addr(r.get("address")) not in addrs:
                            continue
                        h = r.get("height")
                        try:
                            h = int(h)
                        except (TypeError, ValueError):
                            continue
                        key = f"{HELIOS_COINS[hc]}:{h}"
                        prev = found.get(key)
                        rank = {"pending": 1, "confirmed": 2, "orphaned": 3}[state]
                        if prev is None or rank > prev["_rank"]:
                            found[key] = {"coin": HELIOS_COINS[hc], "key": key, "state": state, "_rank": rank, "height": h,
                                          "hash": str(r.get("blockhash") or "").lower(), "address": str(r.get("address") or ""),
                                          "worker": str(r.get("worker") or ""), "device_ua": str(r.get("device") or ""),
                                          "diff": r.get("diff"), "netdiff": r.get("netdiff"), "ts": _ts(r.get("timestamp")),
                                          "region": region, "source": "HeliosPool"}
            if errs and not found:
                self.errors[hc] = "; ".join(errs[:3])
            else:
                self.errors.pop(hc, None)
            for key, b in found.items():
                prev_state = seen.get(key)
                if prev_state == b["state"] or (prev_state == "orphaned"):
                    continue
                if prev_state is None and b["ts"] and b["ts"] < min(self.started, now) - self.FIRST_RUN_LOOKBACK_SEC:
                    seen[key] = b["state"]  # old history: remember silently
                    continue
                if prev_state == "confirmed" and b["state"] == "pending":
                    continue
                b.pop("_rank", None)
                out.append(b)
        return sorted(out, key=lambda b: (b.get("ts") or 0, b["height"]))
