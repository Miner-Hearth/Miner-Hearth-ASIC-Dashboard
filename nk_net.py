#!/usr/bin/env python3
"""Public-internet lookups for Miner Hearth (stdlib only, all failures are quiet).

- Esplora-compatible block explorers: mempool.space (default), blockstream.info
  (fallback), or the user's own mempool / Esplora instance (custom base URL).
- BTC price: mempool.space /api/v1/prices, fallback Coinbase spot price.
- Firmware release checks: GitHub releases (ESP-Miner, NerdQAxePlus) and the
  public Braiins OS feed index (feeds.braiins-os.com).
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from nk_math import RETARGET_INTERVAL, estimate_retarget

USER_AGENT = "MinerHearth/1.11 (+https://github.com/; miner dashboard)"
MEMPOOL_BASE = "https://mempool.space"
BLOCKSTREAM_BASE = "https://blockstream.info"

FIRMWARE_SOURCES: dict[str, dict[str, str]] = {
    "esp-miner": {
        "label": "ESP-Miner (AxeOS / Bitaxe)",
        "url": "https://api.github.com/repos/bitaxeorg/ESP-Miner/releases/latest",
        "page": "https://github.com/bitaxeorg/ESP-Miner/releases",
        "kind": "github",
    },
    "nerdqaxe": {
        "label": "NerdQAxePlus (NerdQAxe / NerdOCTAxe)",
        "url": "https://api.github.com/repos/shufps/ESP-Miner-NerdQAxePlus/releases/latest",
        "page": "https://github.com/shufps/ESP-Miner-NerdQAxePlus/releases",
        "kind": "github",
    },
    "braiins": {
        "label": "Braiins OS",
        "url": "https://feeds.braiins-os.com/",
        "page": "https://braiins.com/os-firmware/download",
        "kind": "braiins-feed",
    },
}


class NetError(Exception):
    pass


Fetcher = Callable[[str, float], bytes]


def default_fetch(url: str, timeout: float = 8.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310 (fixed https/http URLs)
            return resp.read(4 * 1024 * 1024)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise NetError(f"{url}: {exc}") from exc


def _json(fetch: Fetcher, url: str, timeout: float) -> Any:
    raw = fetch(url, timeout)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NetError(f"{url}: invalid JSON") from exc


def _text(fetch: Fetcher, url: str, timeout: float) -> str:
    return fetch(url, timeout).decode("utf-8", "replace").strip()


def normalize_base_url(url: str) -> str:
    """'umbrel.local:3006/api/' -> 'http://umbrel.local:3006'. Empty stays empty."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    url = url.rstrip("/")
    if url.lower().endswith("/api"):
        url = url[:-4]
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def explorer_bases(source: str, custom_base: str = "", allow_public_fallback: bool = True) -> list[tuple[str, str]]:
    """Ordered (name, base_url) list: the chosen source first, then fallbacks."""
    custom = normalize_base_url(custom_base)
    order: list[tuple[str, str]] = []
    if source == "custom" and custom:
        order.append(("custom", custom))
        if not allow_public_fallback:
            return order
    if source == "blockstream":
        order += [("blockstream", BLOCKSTREAM_BASE), ("mempool", MEMPOOL_BASE)]
    else:
        order += [("mempool", MEMPOOL_BASE), ("blockstream", BLOCKSTREAM_BASE)]
    if custom and source != "custom":
        order.append(("custom", custom))
    seen: set[str] = set()
    out = []
    for name, base in order:
        if base not in seen:
            seen.add(base)
            out.append((name, base))
    return out


class ExplorerClient:
    """Esplora REST client with ordered fallback across base URLs."""

    def __init__(self, bases: list[tuple[str, str]], fetch: Optional[Fetcher] = None, timeout: float = 8.0):
        self.bases = list(bases)
        self.fetch = fetch or default_fetch
        self.timeout = float(timeout)
        self.last_source = ""
        self.last_error = ""

    def _try(self, fn: Callable[[str], Any]) -> Any:
        errors = []
        for name, base in self.bases:
            try:
                val = fn(base)
                self.last_source = name
                self.last_error = ""
                return val
            except (NetError, KeyError, TypeError, ValueError, IndexError) as exc:
                errors.append(f"{name}: {exc}")
        self.last_error = "; ".join(errors) or "no explorer configured"
        raise NetError(self.last_error)

    def tip_height(self) -> int:
        return int(self._try(lambda b: int(_text(self.fetch, f"{b}/api/blocks/tip/height", self.timeout))))

    def block_hash_at(self, height: int) -> str:
        def fn(b: str) -> str:
            h = _text(self.fetch, f"{b}/api/block-height/{int(height)}", self.timeout)
            if not re.fullmatch(r"[0-9a-fA-F]{64}", h):
                raise ValueError("bad block hash")
            return h.lower()
        return self._try(fn)

    def block(self, block_hash: str) -> dict[str, Any]:
        def fn(b: str) -> dict[str, Any]:
            d = _json(self.fetch, f"{b}/api/block/{block_hash}", self.timeout)
            if not isinstance(d, dict):
                raise ValueError("bad block")
            return d
        return self._try(fn)

    def recent_blocks(self) -> list[dict[str, Any]]:
        def fn(b: str) -> list[dict[str, Any]]:
            d = _json(self.fetch, f"{b}/api/blocks", self.timeout)
            if not isinstance(d, list) or not d:
                raise ValueError("bad blocks list")
            return d
        return self._try(fn)

    def coinbase_addresses(self, block_hash: str) -> list[str]:
        """Addresses paid by the block's coinbase transaction (tx index 0)."""
        def fn(b: str) -> list[str]:
            txid = _text(self.fetch, f"{b}/api/block/{block_hash}/txid/0", self.timeout)
            if not re.fullmatch(r"[0-9a-fA-F]{64}", txid):
                raise ValueError("bad coinbase txid")
            tx = _json(self.fetch, f"{b}/api/tx/{txid}", self.timeout)
            vin = tx.get("vin") or []
            if vin and not vin[0].get("is_coinbase", True):
                raise ValueError("tx 0 is not a coinbase")
            addrs = []
            for vout in tx.get("vout") or []:
                a = vout.get("scriptpubkey_address")
                if a:
                    addrs.append(str(a))
            return addrs
        return self._try(fn)

    def difficulty_adjustment(self) -> Optional[dict[str, Any]]:
        """mempool.space-style /api/v1/difficulty-adjustment (None if the source lacks it)."""
        for name, base in self.bases:
            try:
                d = _json(self.fetch, f"{base}/api/v1/difficulty-adjustment", self.timeout)
                if isinstance(d, dict) and "progressPercent" in d:
                    return {
                        "progress_pct": float(d.get("progressPercent") or 0.0),
                        "change_pct": float(d["difficultyChange"]) if d.get("difficultyChange") is not None else None,
                        "remaining_blocks": int(d.get("remainingBlocks") or 0),
                        "next_retarget_height": int(d.get("nextRetargetHeight") or 0) or None,
                        "eta_ts": (float(d["estimatedRetargetDate"]) / 1000.0) if d.get("estimatedRetargetDate") else None,
                        "source": name,
                    }
            except (NetError, KeyError, TypeError, ValueError):
                continue
        return None

    def avg_fees_sats(self, blocks: int = 144) -> Optional[float]:
        for name, base in self.bases:
            try:
                d = _json(self.fetch, f"{base}/api/v1/mining/reward-stats/{int(blocks)}", self.timeout)
                total_fee = float(d.get("totalFee"))
                start, end = int(d.get("startBlock")), int(d.get("endBlock"))
                n = max(1, end - start + 1)
                return total_fee / n
            except (NetError, KeyError, TypeError, ValueError, AttributeError):
                continue
        return None

    def network_snapshot(self, now: Optional[float] = None) -> dict[str, Any]:
        """Tip height, difficulty and next-adjustment estimate (raises NetError if all sources fail)."""
        now = time.time() if now is None else now
        blocks = self.recent_blocks()
        tip = blocks[0]
        height = int(tip.get("height"))
        difficulty = float(tip.get("difficulty"))
        source = self.last_source
        adj = self.difficulty_adjustment()
        if adj is None:
            epoch_start_h = height - (height % RETARGET_INTERVAL)
            epoch_ts: Optional[float] = None
            try:
                epoch_ts = float(self.block(self.block_hash_at(epoch_start_h)).get("timestamp"))
            except (NetError, TypeError, ValueError):
                epoch_ts = None
            adj = estimate_retarget(height, now, epoch_ts)
            adj["source"] = source + " (estimated)"
        return {
            "height": height,
            "difficulty": difficulty,
            "tip_hash": str(tip.get("id") or ""),
            "tip_ts": float(tip.get("timestamp") or 0.0),
            "adjustment": adj,
            "source": source,
            "updated_ts": now,
        }


def fetch_btc_price(currency: str, bases: list[tuple[str, str]], fetch: Optional[Fetcher] = None, timeout: float = 8.0) -> Optional[dict[str, Any]]:
    fetch = fetch or default_fetch
    cur = (currency or "USD").strip().upper()
    for name, base in bases:
        if name == "blockstream":
            continue  # Esplora has no price endpoint
        try:
            d = _json(fetch, f"{base}/api/v1/prices", timeout)
            if isinstance(d, dict) and d.get(cur):
                return {"price": float(d[cur]), "currency": cur, "source": name}
        except (NetError, TypeError, ValueError):
            continue
    try:
        d = _json(fetch, f"https://api.coinbase.com/v2/prices/BTC-{urllib.parse.quote(cur)}/spot", timeout)
        amt = float(d["data"]["amount"])
        return {"price": amt, "currency": cur, "source": "coinbase"}
    except (NetError, KeyError, TypeError, ValueError):
        return None


# --- Firmware versions ---------------------------------------------------------

def parse_version_tuple(text: str) -> Optional[tuple[int, ...]]:
    """'v2.4.2-dirty' -> (2, 4, 2); 'bosminer 26.10.1' -> (26, 10, 1); None if no x.y."""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?", text or "")
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def braiins_release_from_text(text: str) -> Optional[tuple[int, ...]]:
    """Braiins OS release tags look like YY.MM or YY.MM.N (e.g. '...-26.10-plus')."""
    m = re.search(r"(?<![\d.])(2\d)\.(0[1-9]|1[0-2])(?:\.(\d+))?(?![\d])", text or "")
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def version_is_older(device_ver: Optional[tuple[int, ...]], latest: Optional[tuple[int, ...]]) -> bool:
    if not device_ver or not latest:
        return False
    n = max(len(device_ver), len(latest))
    a = tuple(list(device_ver) + [0] * (n - len(device_ver)))
    b = tuple(list(latest) + [0] * (n - len(latest)))
    return a < b


def fetch_latest_release(source_key: str, fetch: Optional[Fetcher] = None, timeout: float = 10.0) -> Optional[dict[str, Any]]:
    fetch = fetch or default_fetch
    src = FIRMWARE_SOURCES.get(source_key)
    if not src:
        return None
    try:
        if src["kind"] == "github":
            d = _json(fetch, src["url"], timeout)
            tag = str(d.get("tag_name") or d.get("name") or "")
            ver = parse_version_tuple(tag)
            if not ver:
                return None
            return {"tag": tag, "version": list(ver), "url": str(d.get("html_url") or src["page"]), "published": str(d.get("published_at") or "")}
        html = _text(fetch, src["url"], timeout)
        best: Optional[tuple[int, ...]] = None
        for m in re.finditer(r'href="(\d{2}\.\d{2}(?:\.\d+)?)/"', html):
            v = tuple(int(x) for x in m.group(1).split("."))
            if best is None or v > best:
                best = v
        if not best:
            return None
        return {"tag": ".".join(str(x) for x in best), "version": list(best), "url": src["page"], "published": ""}
    except (NetError, KeyError, TypeError, ValueError, AttributeError):
        return None


def firmware_source_for(dtype: str, firmware: str, model: str, version: str) -> Optional[str]:
    dtype = (dtype or "").lower()
    blob = f"{firmware} {model} {version}".lower()
    if dtype == "antminer":
        return "braiins" if "braiins" in blob or "bosminer" in blob or "bos" == (firmware or "").lower() else None
    if dtype == "nerdoctaxe" or "nerd" in blob or "qaxe" in blob or "octaxe" in blob:
        return "nerdqaxe"
    if dtype in {"gamma", "axeos-generic"}:
        return "esp-miner"
    return None
