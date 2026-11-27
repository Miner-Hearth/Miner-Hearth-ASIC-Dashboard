#!/usr/bin/env python3
"""Miner Hearth v1.13: Multi-algorithm coin detection, per-coin chain data and payout matching (stdlib only).

Supported coins:
  - SHA-256d: BTC, BCH, BSV, XEC, DGB, FB
  - Scrypt: LTC (+ DOGE merged/AuxPoW)
  - kHeavyHash: KAS
  - Blake3: ALPH
  - Eaglesong: CKB
  - ETChash: ETC
  - Equihash: ZEC (Sol/s)
  - X11: DASH
  - Handshake: HNS

Detection never guesses silently. When signals disagree or are missing the coin is "unknown".
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Optional

import nk_net

Fetcher = Callable[..., bytes]

# ---------------------------------------------------------------------------
# Coin table
# ---------------------------------------------------------------------------

COINS: dict[str, dict[str, Any]] = {
    "BTC": {
        "label": "Bitcoin", "unit": "BTC", "algo": "sha256d", "unit_kind": "hash",
        "explorer": "esplora",
        "bases": [("mempool.space", "https://mempool.space"), ("blockstream.info", "https://blockstream.info")],
        "block_url": "https://mempool.space/block/{hash}",
    },
    "BCH": {
        "label": "Bitcoin Cash", "unit": "BCH", "algo": "sha256d", "unit_kind": "hash",
        "explorer": "haskoin",
        "bases": [("haskoin", "https://api.haskoin.com/bch")],
        "block_url": "https://blockchair.com/bitcoin-cash/block/{height}",
    },
    "BSV": {
        "label": "Bitcoin SV", "unit": "BSV", "algo": "sha256d", "unit_kind": "hash",
        "explorer": "woc",
        "bases": [("whatsonchain", "https://api.whatsonchain.com/v1/bsv/main")],
        "block_url": "https://whatsonchain.com/block-height/{height}",
    },
    "XEC": {
        "label": "eCash", "unit": "XEC", "algo": "sha256d", "unit_kind": "hash",
        "explorer": "chronik",
        "bases": [("chronik.e.cash", "https://chronik.e.cash")],
        "block_url": "https://explorer.e.cash/block-height/{height}",
    },
    "CHTA": {
        # v1.15.5: CheetahCoin, SHA-256, very low and spiky ("randomSpike") difficulty; Iquidus explorer (plain HTTP).
        "label": "CheetahCoin", "unit": "CHTA", "algo": "sha256d", "unit_kind": "hash",
        "explorer": "iquidus",
        "bases": [("chtaexplorer.mooo.com", "http://chtaexplorer.mooo.com:3002")],
        "block_url": "http://chtaexplorer.mooo.com:3002/block/{hash}",
        "spiky_difficulty": True,
    },
    "DGB": {
        "label": "DigiByte (SHA-256)", "unit": "DGB", "algo": "sha256d", "unit_kind": "hash",
        "explorer": "esplora-dgb",
        "bases": [("digiexplorer", "https://digiexplorer.info")],
        "block_url": "https://digiexplorer.info/block/{hash}",
    },
    "FB": {
        "label": "Fractal Bitcoin", "unit": "FB", "algo": "sha256d", "unit_kind": "hash",
        "explorer": "esplora",
        "bases": [("mempool.fractalbitcoin.io", "https://mempool.fractalbitcoin.io")],
        "block_url": "https://mempool.fractalbitcoin.io/block/{hash}",
    },
    "LTC": {
        "label": "Litecoin", "unit": "LTC", "algo": "scrypt", "unit_kind": "hash",
        "merged_with": "DOGE",
        "explorer": "esplora",
        "bases": [("litecoinspace.org", "https://litecoinspace.org")],
        "block_url": "https://litecoinspace.org/block/{hash}",
    },
    "DOGE": {
        "label": "Dogecoin", "unit": "DOGE", "algo": "scrypt", "unit_kind": "hash",
        "explorer": "dogechain",
        "bases": [("dogechain.com", "https://dogechain.com/api/v1"), ("sochain.com", "https://sochain.com/api/v3")],
        "block_url": "https://dogechain.com/block/{hash}",
    },
    "KAS": {
        "label": "Kaspa", "unit": "KAS", "algo": "kheavyhash", "unit_kind": "hash",
        "explorer": "kaspa",
        "bases": [("api.kaspa.org", "https://api.kaspa.org")],
        "block_url": "https://kas.fyi/block/{hash}",
    },
    "ALPH": {
        "label": "Alephium", "unit": "ALPH", "algo": "blake3", "unit_kind": "hash",
        "explorer": "alephium",
        "bases": [("backend.mainnet.alephium.org", "https://backend.mainnet.alephium.org")],
        "block_url": "https://explorer.alephium.org/blocks/{hash}",
    },
    "CKB": {
        "label": "Nervos CKB", "unit": "CKB", "algo": "eaglesong", "unit_kind": "hash",
        "explorer": "nervos",
        "bases": [("explorer.nervos.org", "https://mainnet-api.explorer.nervos.org/api/v1")],
        "block_url": "https://explorer.nervos.org/block/{hash}",
    },
    "ETC": {
        "label": "Ethereum Classic", "unit": "ETC", "algo": "etchash", "unit_kind": "hash",
        "explorer": "blockscout",
        "bases": [("etc.blockscout.com", "https://etc.blockscout.com/api/v2")],
        "block_url": "https://etc.blockscout.com/block/{height}",
    },
    "ZEC": {
        "label": "Zcash", "unit": "ZEC", "algo": "equihash", "unit_kind": "sol",
        "explorer": "zcash",
        "bases": [("api.zcha.in", "https://api.zcha.in/v2/mainnet"), ("sochain.com", "https://sochain.com/api/v3")],
        "block_url": "https://zcashblockexplorer.com/blocks/{height}",
    },
    "DASH": {
        "label": "Dash", "unit": "DASH", "algo": "x11", "unit_kind": "hash",
        "explorer": "insight",
        "bases": [("insight.dash.org", "https://insight.dash.org/insight-api")],
        "block_url": "https://insight.dash.org/insight/block/{hash}",
    },
    "HNS": {
        "label": "Handshake", "unit": "HNS", "algo": "handshake", "unit_kind": "hash",
        "explorer": "handshake",
        "bases": [("api.hnsnetwork.com", "https://api.hnsnetwork.com")],
        "block_url": "https://explorer.hns.au/block/{height}",
    },
}
COIN_IDS = tuple(COINS)
UNKNOWN = "unknown"

# Pool host hints (substring of the host name -> coin). Only unambiguous names.
POOL_HOST_HINTS: list[tuple[str, str]] = [
    ("bch.", "BCH"), ("-bch", "BCH"), ("bch-", "BCH"), ("bitcoincash", "BCH"),
    ("bsv.", "BSV"), ("-bsv", "BSV"), ("bsv-", "BSV"),
    ("xec.", "XEC"), ("-xec", "XEC"), ("ecash", "XEC"),
    ("chta.", "CHTA"), ("-chta", "CHTA"), ("cheetah", "CHTA"),
    ("dgb", "DGB"), ("digibyte", "DGB"),
    ("fractal", "FB"), ("fb.", "FB"), ("-fb.", "FB"),
    ("litecoinpool", "LTC"), ("ltc.", "LTC"), ("-ltc", "LTC"), ("ltc-", "LTC"),
    ("doge.", "DOGE"), ("-doge", "DOGE"), ("dogecoin", "DOGE"),
    ("kas.", "KAS"), ("kaspa", "KAS"), ("kaspapool", "KAS"),
    ("alph.", "ALPH"), ("alephium", "ALPH"),
    ("ckb.", "CKB"), ("nervos", "CKB"),
    ("etc.", "ETC"), ("etchash", "ETC"),
    ("zec.", "ZEC"), ("zcash", "ZEC"),
    ("dash.", "DASH"), ("-dash", "DASH"),
    ("hns.", "HNS"), ("handshake", "HNS"), ("hnspool", "HNS"),
    ("solo.ckpool.org", "BTC"), ("public-pool.io", "BTC"), ("ocean.xyz", "BTC"), ("braiins.com", "BTC"),
    ("slushpool", "BTC"), ("nerdminers.org", "BTC"), ("btc.", "BTC"), ("-btc", "BTC"), ("btc-", "BTC"),
    ("antpool.com", "BTC"), ("foundryusapool", "BTC"), ("solo.cat", "BTC"),
]


def pool_host(url: str) -> str:
    u = str(url or "").strip().lower()
    u = re.sub(r"^[a-z0-9+.-]+://", "", u)
    u = u.split("/", 1)[0].split("@")[-1]
    return u.split(":", 1)[0]


def pool_key(url: str, port: Any = None) -> str:
    """Normalized 'host:port' used to group pool statistics."""
    u = str(url or "").strip().lower()
    u2 = re.sub(r"^[a-z0-9+.-]+://", "", u).split("/", 1)[0].split("@")[-1]
    host = u2.split(":", 1)[0]
    p = None
    if ":" in u2:
        try:
            p = int(u2.split(":", 1)[1])
        except ValueError:
            p = None
    if p is None and port not in (None, ""):
        try:
            p = int(port)
        except (TypeError, ValueError):
            p = None
    if not host:
        return ""
    return f"{host}:{p}" if p else host


def host_hint(url: str) -> Optional[str]:
    h = pool_host(url)
    if not h:
        return None
    for needle, coin in POOL_HOST_HINTS:
        if needle in h:
            return coin
    return None


# ---------------------------------------------------------------------------
# Address codecs -> scriptPubKey hex & account-address matching
# ---------------------------------------------------------------------------

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def b58check_encode(body: bytes) -> str:
    """Version byte + payload -> Base58Check (used by tests to build synthetic addresses)."""
    raw = bytes(body) + hashlib.sha256(hashlib.sha256(bytes(body)).digest()).digest()[:4]
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + out


def _b58check_decode(s: str) -> Optional[bytes]:
    n = 0
    for ch in s:
        i = _B58.find(ch)
        if i < 0:
            return None
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    raw = b"\x00" * (len(s) - len(s.lstrip("1"))) + raw
    if len(raw) < 5:
        return None
    body, chk = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] != chk:
        return None
    return body


def _bech32_polymod(values: list[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def _convertbits(data: list[int], frombits: int, tobits: int, pad: bool) -> Optional[list[int]]:
    acc = bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    for v in data:
        if v < 0 or v >> frombits:
            return None
        acc = (acc << frombits) | v
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def _segwit_script(addr: str) -> Optional[str]:
    a = addr.lower()
    if "1" not in a:
        return None
    hrp, data_part = a.rsplit("1", 1)
    if not hrp or len(data_part) < 7 or any(c not in _B32 for c in data_part):
        return None
    data = [_B32.index(c) for c in data_part]
    hrp_exp = [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]
    const = _bech32_polymod(hrp_exp + data)
    if const not in (1, 0x2BC830A3):
        return None
    witver = data[0]
    prog = _convertbits(data[1:-6], 5, 8, False)
    if prog is None or not (2 <= len(prog) <= 40) or witver > 16:
        return None
    if (witver == 0) != (const == 1):
        return None
    op = 0 if witver == 0 else 0x50 + witver
    return bytes([op, len(prog)] + prog).hex()


def _cashaddr_polymod(values: list[int]) -> int:
    gens = [0x98F2BC8E61, 0x79B76D99E2, 0xF33E5FB3C4, 0xAE2EABE2A8, 0x1E4F43E470]
    c = 1
    for d in values:
        c0 = c >> 35
        c = ((c & 0x07FFFFFFFF) << 5) ^ d
        for i in range(5):
            if (c0 >> i) & 1:
                c ^= gens[i]
    return c ^ 1


def _cashaddr_script(addr: str, default_prefix: str) -> Optional[str]:
    a = addr.strip().lower()
    if ":" in a:
        prefix, payload = a.split(":", 1)
    else:
        prefix, payload = default_prefix, a
    if not payload or any(c not in _B32 for c in payload):
        return None
    data = [_B32.index(c) for c in payload]
    if _cashaddr_polymod([ord(c) & 31 for c in prefix] + [0] + data) != 0:
        return None
    conv = _convertbits(data[:-8], 5, 8, False)
    if not conv:
        return None
    version, h = conv[0], bytes(conv[1:])
    kind = version >> 3
    if len(h) != 20:
        return None
    if kind == 0:
        return "76a914" + h.hex() + "88ac"
    if kind == 1:
        return "a914" + h.hex() + "87"
    return None


# P2PKH: BTC/BCH/BSV/FB (0x00), CHTA (0x1C, "C..."), DGB/DOGE (0x1E), LTC (0x30), DASH (0x4C), testnet (0x6F)
_P2PKH_VERSIONS = {0x00, 0x1C, 0x1E, 0x30, 0x4C, 0x6F}
# P2SH: BTC/BCH/BSV (0x05), DASH (0x10), DOGE (0x16), LTC (0x32), DGB "S" (0x3F), testnet (0xC4)
_P2SH_VERSIONS = {0x05, 0x10, 0x16, 0x32, 0x3F, 0xC4}


def address_to_script(addr: str) -> Optional[str]:
    """Return the scriptPubKey (hex) for UTXO addresses, or None if not UTXO-scriptable."""
    a = str(addr or "").strip()
    if not a:
        return None
    low = a.lower()
    if low.startswith(("bitcoincash:", "ecash:", "bchtest:")) or re.fullmatch(r"[qp][02-9ac-hj-np-z]{41}", low):
        pfx = "ecash" if low.startswith("ecash:") else ("bchtest" if low.startswith("bchtest:") else "bitcoincash")
        s = _cashaddr_script(a, pfx)
        if s is None and ":" not in low:
            s = _cashaddr_script(a, "ecash")
        return s
    if re.match(r"^(bc|tb|bcrt|dgb|dgbt|ltc|tltc|hs)1", low):
        return _segwit_script(a)
    body = _b58check_decode(a)
    if body is None:
        return None
    if len(body) == 21:
        ver, h = body[0], body[1:]
        if ver in _P2PKH_VERSIONS:
            return "76a914" + h.hex() + "88ac"
        if ver in _P2SH_VERSIONS:
            return "a914" + h.hex() + "87"
    if len(body) == 22:
        # 2-byte version prefix (e.g., Zcash transparent t1=0x1CB8, t3=0x1CBD)
        ver2, h = body[:2], body[2:]
        if ver2 == b"\x1c\xb8":
            return "76a914" + h.hex() + "88ac"
        if ver2 == b"\x1c\xbd":
            return "a914" + h.hex() + "87"
    return None


def is_valid_account_address(addr: str) -> bool:
    """Validate non-UTXO-script formats (ETC 0x..., Kaspa kaspa:..., Nervos ckb1..., Alephium)."""
    a = str(addr or "").strip()
    low = a.lower()
    if re.fullmatch(r"0x[0-9a-f]{40}", low):
        return True
    if low.startswith("kaspa:") and len(low) >= 60 and all(c in _B32 for c in low.split(":", 1)[1]):
        return True
    if low.startswith("ckb1") and len(low) >= 40 and all(c in _B32 for c in low[4:]):
        return True
    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{40,60}", a) and len(a) in range(42, 56):
        # Alephium Base58 address format check
        return all(c in _B58 for c in a)
    return False


def is_valid_coin_address(addr: str) -> bool:
    return bool(address_to_script(addr) or is_valid_account_address(addr))


def address_coins(addr: str) -> set[str]:
    """Which coins an address format can belong to (for detection hints)."""
    a = str(addr or "").strip()
    low = a.lower()
    if not low:
        return set()
    if low.startswith("ecash:"):
        return {"XEC"}
    if low.startswith("bitcoincash:"):
        return {"BCH"}
    if re.fullmatch(r"[qp][02-9ac-hj-np-z]{41}", low):
        return {"BCH", "XEC"}
    if low.startswith("kaspa:"):
        return {"KAS"}
    if low.startswith("ckb1"):
        return {"CKB"}
    if re.fullmatch(r"0x[0-9a-f]{40}", low):
        return {"ETC"}
    if low.startswith("hs1"):
        return {"HNS"}
    if low.startswith("ltc1") or a[:1] in {"L", "M"}:
        return {"LTC"}
    if a[:1] in {"D", "S"} and not low.startswith("dgb1"):
        # 'D' can be DGB or DOGE; 'S' can be DGB P2SH
        if a[:1] == "D":
            return {"DGB", "DOGE"}
        return {"DGB"}
    if low.startswith("dgb1"):
        return {"DGB"}
    if a[:1] == "X":
        return {"DASH"}
    if a[:1] == "C" and (_b58check_decode(a) or b"\xff")[:1] == b"\x1c":
        return {"CHTA"}
    if a[:2] in {"t1", "t3"}:
        return {"ZEC"}
    if low.startswith("bc1"):
        return {"BTC", "FB"}
    if a[:1] in {"1", "3"}:
        return {"BTC", "BCH", "BSV", "FB"}
    return set()


def pool_user_address(user: str) -> str:
    """Pool user is usually '<address>.<worker>'; cashaddr/kaspa may carry ':'."""
    u = str(user or "").strip()
    return u.split(".", 1)[0].strip() if u else ""


def extract_pool_payout_address(pool_user: str, coin: Optional[str] = None) -> dict[str, Any]:
    """Extract and validate a coinbase payout address from a miner's pool username."""
    raw = str(pool_user or "").strip()
    if not raw:
        return {"address": "", "status": "empty", "coins": [], "reason": "no pool username reported"}
    candidate = pool_user_address(raw)
    if not candidate:
        return {"address": "", "status": "empty", "coins": [], "reason": "no pool username reported"}
    script = address_to_script(candidate)
    acct_ok = is_valid_account_address(candidate) if not script else False
    if not script and not acct_ok:
        return {
            "address": "",
            "status": "pool_account_ignored",
            "coins": [],
            "reason": "pool username before '.' is not a valid coinbase address (pool account name ignored)",
        }
    coins = sorted(address_coins(candidate))
    want = str(coin or "").strip().upper()
    if want and want != UNKNOWN and coins and want not in coins:
        return {
            "address": "",
            "status": "coin_mismatch",
            "coins": coins,
            "reason": f"pool username address is for {'/'.join(coins)}, not {want}",
        }
    return {
        "address": candidate,
        "status": "pool_user",
        "script": script or "",
        "coins": coins,
        "reason": "extracted from pool username",
    }


def normalize_coin_addresses(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = re.split(r"[\s,;]+", raw)
    out: list[str] = []
    for x in raw or []:
        a = str(x or "").strip()
        if a and is_valid_coin_address(a) and a not in out:
            out.append(a)
    return out[:20]


def coinbase_tokens(addresses: list[str], scripts: list[str]) -> set[str]:
    toks = {str(a).strip().lower() for a in addresses if a}
    toks |= {"script:" + str(s).lower() for s in scripts if s}
    return toks


def target_tokens(payout_addresses: list[str]) -> set[str]:
    toks: set[str] = set()
    for a in payout_addresses or []:
        a = str(a).strip()
        if not a:
            continue
        toks.add(a.lower())
        s = address_to_script(a)
        if s:
            toks.add("script:" + s)
    return toks


# ---------------------------------------------------------------------------
# Chain adapters (BTC/BCH/BSV/XEC/DGB/FB + LTC/DOGE/KAS/ALPH/CKB/ETC/ZEC/DASH/HNS)
# ---------------------------------------------------------------------------

def nbits_to_difficulty(bits: Any) -> Optional[float]:
    try:
        b = int(bits, 16) if isinstance(bits, str) else int(bits)
    except (TypeError, ValueError):
        return None
    exp, mant = b >> 24, b & 0xFFFFFF
    if mant == 0:
        return None
    target = mant * (256 ** (exp - 3)) if exp >= 3 else mant >> (8 * (3 - exp))
    if target <= 0:
        return None
    return (0xFFFF * 256 ** (0x1D - 3)) / target


def _get_json(fetch: Fetcher, url: str, timeout: float) -> Any:
    raw = fetch(url, timeout=timeout)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise nk_net.NetError(f"{url}: not JSON ({exc})") from exc


class ChainAdapter:
    coin = "BTC"

    def __init__(self, coin: str, base: str, fetch: Optional[Fetcher] = None, timeout: float = 8.0):
        self.coin = coin
        self.base = base.rstrip("/")
        self.fetch = fetch or nk_net.default_fetch
        self.timeout = timeout

    def j(self, path: str) -> Any:
        return _get_json(self.fetch, self.base + path, self.timeout)

    def t(self, path: str) -> str:
        return self.fetch(self.base + path, timeout=self.timeout).decode("utf-8", "replace").strip()

    def tip_height(self) -> int: raise NotImplementedError
    def block_hash_at(self, height: int) -> str: raise NotImplementedError
    def block(self, block_hash: str) -> dict[str, Any]: raise NotImplementedError
    def coinbase_addresses(self, block_hash: str) -> list[str]: raise NotImplementedError
    def stats(self) -> dict[str, Any]: raise NotImplementedError


class EsploraChain(ChainAdapter):
    """mempool.space / litecoinspace.org / Esplora (BTC, LTC, Fractal). DigiByte filters SHA-256 blocks."""

    sha_only = False

    def tip_height(self) -> int:
        return int(self.t("/api/blocks/tip/height"))

    def block_hash_at(self, height: int) -> str:
        h = self.t(f"/api/block-height/{int(height)}")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", h):
            raise ValueError("bad block hash")
        return h.lower()

    def block(self, block_hash: str) -> dict[str, Any]:
        d = self.j(f"/api/block/{block_hash}")
        if not isinstance(d, dict):
            raise ValueError("bad block")
        return {"timestamp": d.get("timestamp"), "height": d.get("height"), "hash": block_hash}

    def coinbase_addresses(self, block_hash: str) -> list[str]:
        try:
            txid = self.t(f"/api/block/{block_hash}/txid/0")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", txid):
                raise ValueError("bad txid")
        except Exception:
            txid = (self.j(f"/api/block/{block_hash}/txids") or [""])[0]
        tx = self.j(f"/api/tx/{txid}")
        addrs, scripts = [], []
        for v in tx.get("vout") or []:
            if v.get("scriptpubkey_address"):
                addrs.append(v["scriptpubkey_address"])
            if v.get("scriptpubkey"):
                scripts.append(v["scriptpubkey"])
        return sorted(coinbase_tokens(addrs, scripts))

    def stats(self) -> dict[str, Any]:
        blocks = self.j("/api/blocks")
        if not isinstance(blocks, list) or not blocks:
            raise ValueError("no blocks")
        tip = blocks[0]
        diff = None
        if self.sha_only:
            for b in blocks:
                if (int(b.get("version") or 0) & 0x0F00) == 0x0200:
                    diff = b.get("difficulty")
                    break
            if diff is None:
                for b in self.j(f"/api/blocks/{int(blocks[-1]['height']) - 1}") or []:
                    if (int(b.get("version") or 0) & 0x0F00) == 0x0200:
                        diff = b.get("difficulty")
                        break
        else:
            ref = tip
            if self.coin == "FB":
                ref = max(blocks, key=lambda b: float(b.get("difficulty") or 0))
            diff = ref.get("difficulty")
            if not diff or float(diff) <= 1.0:
                diff = nbits_to_difficulty(ref.get("bits"))
        return {"height": int(tip["height"]), "difficulty": float(diff) if diff else None, "tip_ts": tip.get("timestamp"), "tip_hash": tip.get("id")}


class DigiByteChain(EsploraChain):
    sha_only = True


class HaskoinChain(ChainAdapter):
    """api.haskoin.com (Bitcoin Cash)."""

    def tip_height(self) -> int:
        return int(self.j("/block/best?notx=true")["height"])

    def _at(self, height: int) -> dict[str, Any]:
        d = self.j(f"/block/height/{int(height)}?notx=false")
        if isinstance(d, list):
            d = next((x for x in d if x.get("mainchain", True)), d[0] if d else None)
        if not isinstance(d, dict):
            raise ValueError("bad block")
        return d

    def block_hash_at(self, height: int) -> str:
        return str(self._at(height)["hash"]).lower()

    def block(self, block_hash: str) -> dict[str, Any]:
        d = self.j(f"/block/{block_hash}?notx=true")
        return {"timestamp": d.get("time"), "height": d.get("height"), "hash": block_hash}

    def coinbase_addresses(self, block_hash: str) -> list[str]:
        d = self.j(f"/block/{block_hash}?notx=false")
        txid = (d.get("tx") or [None])[0]
        if not txid:
            raise ValueError("block without transactions")
        tx = self.j(f"/transaction/{txid}")
        addrs, scripts = [], []
        for o in tx.get("outputs") or []:
            if o.get("address"):
                addrs.append(o["address"])
                addrs.append(str(o["address"]).split(":", 1)[-1])
            if o.get("pkscript"):
                scripts.append(o["pkscript"])
        return sorted(coinbase_tokens(addrs, scripts))

    def stats(self) -> dict[str, Any]:
        d = self.j("/block/best?notx=true")
        return {"height": int(d["height"]), "difficulty": nbits_to_difficulty(d.get("bits")), "tip_ts": d.get("time"), "tip_hash": d.get("hash")}


class WocChain(ChainAdapter):
    """WhatsOnChain (Bitcoin SV)."""

    _last = 0.0

    def j(self, path: str) -> Any:
        import time as _t
        wait = 0.4 - (_t.monotonic() - WocChain._last)
        if wait > 0 and self.fetch is nk_net.default_fetch:
            _t.sleep(wait)
        WocChain._last = _t.monotonic()
        return super().j(path)

    def tip_height(self) -> int:
        return int(self.j("/chain/info")["blocks"])

    def block_hash_at(self, height: int) -> str:
        return str(self.j(f"/block/height/{int(height)}")["hash"]).lower()

    def block(self, block_hash: str) -> dict[str, Any]:
        d = self.j(f"/block/hash/{block_hash}")
        return {"timestamp": d.get("time"), "height": d.get("height"), "hash": block_hash}

    def coinbase_addresses(self, block_hash: str) -> list[str]:
        d = self.j(f"/block/hash/{block_hash}")
        txid = (d.get("tx") or [None])[0]
        if not txid:
            raise ValueError("block without transactions")
        tx = self.j(f"/tx/hash/{txid}")
        addrs, scripts = [], []
        for v in tx.get("vout") or []:
            spk = v.get("scriptPubKey") or {}
            addrs.extend(spk.get("addresses") or [])
            if spk.get("hex"):
                scripts.append(spk["hex"])
        return sorted(coinbase_tokens(addrs, scripts))

    def stats(self) -> dict[str, Any]:
        d = self.j("/chain/info")
        return {"height": int(d["blocks"]), "difficulty": float(d["difficulty"]) if d.get("difficulty") else None,
                "tip_ts": d.get("mediantime"), "tip_hash": d.get("bestblockhash")}


def _proto(buf: bytes) -> dict[int, list[tuple[int, Any]]]:
    import braiins_api
    return braiins_api.parse_proto_fields(buf)


def _pf(fields: dict[int, list[tuple[int, Any]]], num: int, default: Any = None) -> Any:
    v = fields.get(num)
    return v[0][1] if v else default


class ChronikChain(ChainAdapter):
    """chronik.e.cash (eCash)."""

    def _p(self, path: str) -> dict[int, list[tuple[int, Any]]]:
        return _proto(self.fetch(self.base + path, timeout=self.timeout))

    def tip_height(self) -> int:
        return int(_pf(self._p("/blockchain-info"), 2, 0))

    def _info(self, ref: Any) -> dict[str, Any]:
        blk = self._p(f"/block/{ref}")
        info = _proto(_pf(blk, 1, b""))
        h = _pf(info, 1, b"")
        return {"hash": bytes(h)[::-1].hex(), "height": int(_pf(info, 3, 0)), "bits": int(_pf(info, 4, 0)), "timestamp": int(_pf(info, 5, 0))}

    def block_hash_at(self, height: int) -> str:
        return self._info(int(height))["hash"]

    def block(self, block_hash: str) -> dict[str, Any]:
        i = self._info(block_hash)
        return {"timestamp": i["timestamp"], "height": i["height"], "hash": block_hash}

    def coinbase_addresses(self, block_hash: str) -> list[str]:
        page = self._p(f"/block-txs/{block_hash}?page=0&page_size=1")
        txs = page.get(1) or []
        if not txs:
            raise ValueError("block without transactions")
        tx = _proto(txs[0][1])
        scripts = []
        for _wt, out_raw in tx.get(4) or []:
            o = _proto(out_raw)
            s = _pf(o, 2, b"")
            if s:
                scripts.append(bytes(s).hex())
        return sorted(coinbase_tokens([], scripts))

    def stats(self) -> dict[str, Any]:
        tip = self.tip_height()
        i = self._info(tip)
        return {"height": tip, "difficulty": nbits_to_difficulty(i["bits"]), "tip_ts": i["timestamp"], "tip_hash": i["hash"]}


class DogechainChain(ChainAdapter):
    """dogechain.com/api/v1 & sochain.com/api/v3 fallback for Dogecoin."""

    def _is_sochain(self) -> bool:
        return "sochain.com" in self.base

    def tip_height(self) -> int:
        if self._is_sochain():
            d = self.j(f"/network_info/{self.coin}")
            return int((d.get("data") or {}).get("blocks") or 0)
        return int(self.j("/getblockcount"))

    def block_hash_at(self, height: int) -> str:
        if self._is_sochain():
            d = self.j(f"/block_hash/{self.coin}/{int(height)}")
            return str((d.get("data") or {}).get("block_hash") or "").lower()
        d = self.j(f"/block/{int(height)}")
        return str((d.get("block") or {}).get("hash") or "").lower()

    def block(self, block_hash: str) -> dict[str, Any]:
        if self._is_sochain():
            d = self.j(f"/block/{self.coin}/{block_hash}")
            b = d.get("data") or {}
            return {"timestamp": b.get("time"), "height": b.get("block_no"), "hash": block_hash}
        d = self.j(f"/block/{block_hash}")
        b = d.get("block") or {}
        return {"timestamp": b.get("time"), "height": b.get("height"), "hash": block_hash}

    def coinbase_addresses(self, block_hash: str) -> list[str]:
        if self._is_sochain():
            d = self.j(f"/block/{self.coin}/{block_hash}")
            txs = (d.get("data") or {}).get("txs") or []
            if not txs:
                raise ValueError("no txs")
            tx = self.j(f"/transaction/{self.coin}/{txs[0]}")
            addrs = [o.get("address") for o in ((tx.get("data") or {}).get("outputs") or []) if o.get("address")]
            return sorted(coinbase_tokens(addrs, []))
        d = self.j(f"/block/{block_hash}")
        txs = (d.get("block") or {}).get("txs") or []
        if not txs:
            raise ValueError("no txs")
        tx = self.j(f"/transaction/{txs[0]}")
        addrs = [o.get("address") for o in ((tx.get("transaction") or {}).get("outputs") or []) if o.get("address")]
        return sorted(coinbase_tokens(addrs, []))

    def stats(self) -> dict[str, Any]:
        if self._is_sochain():
            d = (self.j(f"/network_info/{self.coin}")).get("data") or {}
            return {"height": int(d.get("blocks") or 0), "difficulty": float(d["mining_difficulty"]) if d.get("mining_difficulty") else None}
        h = int(self.j("/getblockcount"))
        diff = float(self.j("/getdifficulty"))
        return {"height": h, "difficulty": diff}


class KaspaChain(ChainAdapter):
    """https://api.kaspa.org REST API (kHeavyHash)."""

    def tip_height(self) -> int:
        d = self.j("/info/blockdag")
        return int(d.get("virtualDaaScore") or d.get("blockCount") or 0)

    def block_hash_at(self, height: int) -> str:
        # Kaspa is a BlockDAG; query recent blocks at blueScore/DAA or return empty
        raise ValueError("Kaspa BlockDAG uses block hash lookup rather than linear height")

    def block(self, block_hash: str) -> dict[str, Any]:
        d = self.j(f"/blocks/{block_hash}")
        hdr = d.get("header") or {}
        return {"timestamp": int(hdr.get("timestamp") or 0) // 1000, "height": int(hdr.get("daaScore") or 0), "hash": block_hash}

    def coinbase_addresses(self, block_hash: str) -> list[str]:
        d = self.j(f"/blocks/{block_hash}")
        txs = d.get("transactions") or []
        if not txs:
            return []
        addrs = []
        for o in (txs[0].get("outputs") or []):
            sp = o.get("verboseData") or {}
            if sp.get("scriptPublicKeyAddress"):
                addrs.append(sp["scriptPublicKeyAddress"])
        return sorted(coinbase_tokens(addrs, []))

    def stats(self) -> dict[str, Any]:
        dag = self.j("/info/blockdag")
        hr_data = {}
        try:
            hr_data = self.j("/info/hashrate")
        except Exception:
            pass
        net_ths = float(hr_data.get("hashrate") or 0)
        return {
            "height": int(dag.get("virtualDaaScore") or 0),
            "difficulty": float(dag["difficulty"]) if dag.get("difficulty") else None,
            "network_hashrate_hs": (net_ths * 1e12) if net_ths > 0 else None,
            "avg_block_sec": 1.0,
        }


class AlephiumChain(ChainAdapter):
    """https://backend.mainnet.alephium.org (Blake3)."""

    def tip_height(self) -> int:
        d = self.j("/infos/heights")
        if isinstance(d, list) and d:
            return max(int(x.get("height") or 0) for x in d if isinstance(x, dict))
        return int(d.get("height") or 0)

    def block_hash_at(self, height: int) -> str:
        raise ValueError("Alephium sharded chains require hash lookup")

    def block(self, block_hash: str) -> dict[str, Any]:
        d = self.j(f"/blocks/{block_hash}")
        return {"timestamp": int(d.get("timestamp") or 0) // 1000, "height": int(d.get("height") or 0), "hash": block_hash}

    def coinbase_addresses(self, block_hash: str) -> list[str]:
        d = self.j(f"/blocks/{block_hash}")
        txs = d.get("transactions") or []
        if not txs:
            return []
        addrs = [o.get("address") for o in (txs[-1].get("outputs") or []) if o.get("address")]
        return sorted(coinbase_tokens(addrs, []))

    def stats(self) -> dict[str, Any]:
        h = self.tip_height()
        diff_data = self.j("/infos/current-difficulty")
        hr_data = {}
        try:
            hr_data = self.j("/infos/hashrate")
        except Exception:
            pass
        diff_val = float(diff_data.get("difficulty") or 0) if isinstance(diff_data, dict) else 0.0
        net_hs = float(hr_data.get("hashrate") or 0) if isinstance(hr_data, dict) else 0.0
        return {
            "height": h,
            "difficulty": diff_val if diff_val > 0 else None,
            "network_hashrate_hs": net_hs if net_hs > 0 else None,
            "avg_block_sec": 16.0,
        }


class NervosChain(ChainAdapter):
    """https://mainnet-api.explorer.nervos.org/api/v1 (Eaglesong)."""

    def tip_height(self) -> int:
        d = self.j("/statistics")
        attrs = (d.get("data") or {}).get("attributes") or d
        return int(attrs.get("tip_block_number") or 0)

    def block_hash_at(self, height: int) -> str:
        d = self.j(f"/blocks/{int(height)}")
        attrs = (d.get("data") or {}).get("attributes") or d
        return str(attrs.get("block_hash") or "").lower()

    def block(self, block_hash: str) -> dict[str, Any]:
        d = self.j(f"/blocks/{block_hash}")
        attrs = (d.get("data") or {}).get("attributes") or d
        return {"timestamp": int(attrs.get("timestamp") or 0) // 1000, "height": int(attrs.get("number") or 0), "hash": block_hash}

    def coinbase_addresses(self, block_hash: str) -> list[str]:
        d = self.j(f"/blocks/{block_hash}")
        attrs = (d.get("data") or {}).get("attributes") or d
        miner_hash = attrs.get("miner_hash")
        return sorted(coinbase_tokens([miner_hash] if miner_hash else [], []))

    def stats(self) -> dict[str, Any]:
        d = self.j("/statistics")
        attrs = (d.get("data") or {}).get("attributes") or d
        hr = float(attrs.get("hash_rate") or 0)
        bt_ms = float(attrs.get("average_block_time") or 10000)
        return {
            "height": int(attrs.get("tip_block_number") or 0),
            "difficulty": float(attrs["current_epoch_difficulty"]) if attrs.get("current_epoch_difficulty") else None,
            "network_hashrate_hs": hr * 1000.0 if hr > 0 else None,  # CKB explorer reports kH/s or H/ms
            "avg_block_sec": max(1.0, bt_ms / 1000.0),
        }


class BlockscoutChain(ChainAdapter):
    """https://etc.blockscout.com/api/v2 (ETChash)."""

    def tip_height(self) -> int:
        blks = (self.j("/blocks") or {}).get("items") or []
        return int(blks[0]["height"]) if blks else 0

    def block_hash_at(self, height: int) -> str:
        d = self.j(f"/blocks/{int(height)}")
        return str(d.get("hash") or "").lower()

    def block(self, block_hash: str) -> dict[str, Any]:
        d = self.j(f"/blocks/{block_hash}")
        return {"timestamp": d.get("timestamp"), "height": int(d.get("height") or 0), "hash": block_hash}

    def coinbase_addresses(self, block_hash: str) -> list[str]:
        d = self.j(f"/blocks/{block_hash}")
        miner = (d.get("miner") or {}).get("hash")
        return sorted(coinbase_tokens([miner] if miner else [], []))

    def stats(self) -> dict[str, Any]:
        blks = (self.j("/blocks") or {}).get("items") or []
        if not blks:
            raise ValueError("no ETC blocks")
        tip = blks[0]
        return {
            "height": int(tip.get("height") or 0),
            "difficulty": float(tip["difficulty"]) if tip.get("difficulty") else None,
            "tip_hash": tip.get("hash"),
        }


class ZcashChain(ChainAdapter):
    """api.zcha.in/v2/mainnet & sochain fallback (Equihash Sol/s)."""

    def _is_sochain(self) -> bool:
        return "sochain.com" in self.base

    def tip_height(self) -> int:
        if self._is_sochain():
            return int(((self.j("/network_info/ZEC")).get("data") or {}).get("blocks") or 0)
        return int((self.j("/network") or {}).get("blockNumber") or 0)

    def block_hash_at(self, height: int) -> str:
        if self._is_sochain():
            return str(((self.j(f"/block_hash/ZEC/{int(height)}")).get("data") or {}).get("block_hash") or "").lower()
        blks = self.j(f"/blocks?limit=1&offset=0&sort=height&direction=descending")
        if isinstance(blks, list) and blks:
            return str(blks[0].get("hash") or "").lower()
        raise ValueError("ZEC block hash lookup failed")

    def block(self, block_hash: str) -> dict[str, Any]:
        if self._is_sochain():
            b = (self.j(f"/block/ZEC/{block_hash}")).get("data") or {}
            return {"timestamp": b.get("time"), "height": b.get("block_no"), "hash": block_hash}
        d = self.j(f"/blocks/{block_hash}")
        return {"timestamp": d.get("timestamp"), "height": d.get("height"), "hash": block_hash}

    def coinbase_addresses(self, block_hash: str) -> list[str]:
        if self._is_sochain():
            txs = ((self.j(f"/block/ZEC/{block_hash}")).get("data") or {}).get("txs") or []
            if not txs:
                return []
            tx = (self.j(f"/transaction/ZEC/{txs[0]}")).get("data") or {}
            return sorted(coinbase_tokens([o.get("address") for o in (tx.get("outputs") or []) if o.get("address")], []))
        d = self.j(f"/blocks/{block_hash}")
        miner = d.get("miner")
        return sorted(coinbase_tokens([miner] if miner else [], []))

    def stats(self) -> dict[str, Any]:
        if self._is_sochain():
            d = (self.j("/network_info/ZEC")).get("data") or {}
            return {"height": int(d.get("blocks") or 0), "difficulty": float(d["mining_difficulty"]) if d.get("mining_difficulty") else None, "avg_block_sec": 75.0}
        d = self.j("/network") or {}
        return {
            "height": int(d.get("blockNumber") or 0),
            "difficulty": float(d["difficulty"]) if d.get("difficulty") else None,
            "network_hashrate_hs": float(d["hashrate"]) if d.get("hashrate") else None,
            "avg_block_sec": 75.0,
        }


class InsightChain(ChainAdapter):
    """insight.dash.org/insight-api (Dash X11)."""

    def tip_height(self) -> int:
        return int((self.j("/status?q=getInfo").get("info") or {}).get("blocks") or 0)

    def block_hash_at(self, height: int) -> str:
        return str(self.j(f"/block-index/{int(height)}").get("blockHash") or "").lower()

    def block(self, block_hash: str) -> dict[str, Any]:
        d = self.j(f"/block/{block_hash}")
        return {"timestamp": d.get("time"), "height": d.get("height"), "hash": block_hash}

    def coinbase_addresses(self, block_hash: str) -> list[str]:
        d = self.j(f"/block/{block_hash}")
        txs = d.get("tx") or []
        if not txs:
            return []
        tx = self.j(f"/tx/{txs[0]}")
        addrs = []
        for v in tx.get("vout") or []:
            spk = v.get("scriptPubKey") or {}
            addrs.extend(spk.get("addresses") or [])
        return sorted(coinbase_tokens(addrs, []))

    def stats(self) -> dict[str, Any]:
        info = self.j("/status?q=getInfo").get("info") or {}
        return {"height": int(info.get("blocks") or 0), "difficulty": float(info["difficulty"]) if info.get("difficulty") else None}


class HandshakeChain(ChainAdapter):
    """api.hnsnetwork.com (Handshake)."""

    def tip_height(self) -> int:
        d = self.j("/summary")
        return int(d.get("height") or 0)

    def block_hash_at(self, height: int) -> str:
        d = self.j(f"/blocks/{int(height)}")
        return str(d.get("hash") or "").lower()

    def block(self, block_hash: str) -> dict[str, Any]:
        d = self.j(f"/blocks/{block_hash}")
        return {"timestamp": d.get("time"), "height": d.get("height"), "hash": block_hash}

    def coinbase_addresses(self, block_hash: str) -> list[str]:
        d = self.j(f"/blocks/{block_hash}")
        txs = d.get("txs") or []
        if not txs:
            return []
        addrs = [o.get("address") for o in (txs[0].get("outputs") or []) if o.get("address")]
        return sorted(coinbase_tokens(addrs, []))

    def stats(self) -> dict[str, Any]:
        d = self.j("/summary")
        return {"height": int(d.get("height") or 0), "difficulty": float(d["difficulty"]) if d.get("difficulty") else None}


class IquidusChain(ChainAdapter):
    """Iquidus / eIquidus explorers (CheetahCoin and other small SHA-256 coins): bitcoind JSON over /api/*."""

    def tip_height(self) -> int:
        return int(float(self.t("/api/getblockcount")))

    def block_hash_at(self, height: int) -> str:
        h = self.t(f"/api/getblockhash?index={int(height)}").strip().strip('"')
        if not re.fullmatch(r"[0-9a-fA-F]{64}", h):
            raise ValueError("bad block hash")
        return h.lower()

    def _block(self, block_hash: str) -> dict[str, Any]:
        d = self.j(f"/api/getblock?hash={block_hash}")
        if not isinstance(d, dict) or not d.get("tx"):
            raise ValueError("bad block")
        return d

    def block(self, block_hash: str) -> dict[str, Any]:
        d = self._block(block_hash)
        return {"timestamp": d.get("time"), "height": d.get("height"), "hash": block_hash, "difficulty": d.get("difficulty")}

    def coinbase_addresses(self, block_hash: str) -> list[str]:
        txid = str(self._block(block_hash)["tx"][0])
        tx = self.j(f"/api/getrawtransaction?txid={txid}&decrypt=1")
        addrs, scripts = [], []
        for v in (tx or {}).get("vout") or []:
            spk = v.get("scriptPubKey") or {}
            addrs += [a for a in (spk.get("addresses") or ([spk["address"]] if spk.get("address") else [])) if a]
            if spk.get("hex"):
                scripts.append(spk["hex"])
        return sorted(coinbase_tokens(addrs, scripts))

    def stats(self) -> dict[str, Any]:
        tip = self.tip_height()
        raw = self.t("/api/getdifficulty")
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            d = raw
        if isinstance(d, dict):
            d = d.get("proof-of-work") or d.get("difficulty")
        return {"height": tip, "difficulty": float(d) if d not in (None, "") else None}


_ADAPTERS = {
    "iquidus": IquidusChain,
    "esplora": EsploraChain,
    "esplora-dgb": DigiByteChain,
    "haskoin": HaskoinChain,
    "woc": WocChain,
    "chronik": ChronikChain,
    "dogechain": DogechainChain,
    "kaspa": KaspaChain,
    "alephium": AlephiumChain,
    "nervos": NervosChain,
    "blockscout": BlockscoutChain,
    "zcash": ZcashChain,
    "insight": InsightChain,
    "handshake": HandshakeChain,
}


class MultiChain:
    """Tries each base URL of a coin in order."""

    def __init__(self, coin: str, fetch: Optional[Fetcher] = None, bases: Optional[list[tuple[str, str]]] = None, timeout: float = 8.0):
        meta = COINS[coin]
        cls = _ADAPTERS[meta["explorer"]]
        self.coin = coin
        self.adapters = [(name, cls(coin, base, fetch=fetch, timeout=timeout)) for name, base in (bases or meta["bases"])]
        self.last_source = ""

    def _try(self, meth: str, *args: Any) -> Any:
        errs = []
        for name, a in self.adapters:
            try:
                v = getattr(a, meth)(*args)
                self.last_source = name
                return v
            except Exception as exc:
                errs.append(f"{name}: {exc}")
        raise nk_net.NetError("; ".join(errs) or "no explorer")

    def tip_height(self) -> int: return self._try("tip_height")
    def block_hash_at(self, h: int) -> str: return self._try("block_hash_at", h)
    def block(self, bh: str) -> dict[str, Any]: return self._try("block", bh)
    def coinbase_addresses(self, bh: str) -> list[str]: return self._try("coinbase_addresses", bh)
    def stats(self) -> dict[str, Any]:
        s = self._try("stats")
        s["source"] = self.last_source
        return s


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

HEIGHT_TOLERANCE = 3
DIFF_TOLERANCE = 0.12


def chain_matches(height: Optional[int], difficulty: Optional[float], stats: dict[str, dict[str, Any]]) -> list[str]:
    """Coins whose live tip matches the miner's reported job height and/or network difficulty."""
    out = []
    for coin, st in stats.items():
        if not st:
            continue
        ok_h = ok_d = None
        if height and st.get("height"):
            ok_h = abs(int(height) - int(st["height"])) <= HEIGHT_TOLERANCE
        if difficulty and st.get("difficulty") and not (COINS.get(coin) or {}).get("spiky_difficulty"):
            ok_d = abs(float(difficulty) / float(st["difficulty"]) - 1.0) <= DIFF_TOLERANCE
        if ok_h is False or ok_d is False:
            continue
        if ok_h or ok_d:
            out.append(coin)
    return out


def coins_for_algorithm(algorithm: str) -> set[str]:
    algo = str(algorithm or "").strip().lower()
    if not algo:
        return set(COINS)
    return {cid for cid, meta in COINS.items() if meta.get("algo") == algo}


def detect_coin(info: dict[str, Any], stats: dict[str, dict[str, Any]], override: str = "") -> dict[str, Any]:
    """info: pool_url, pool_user, block_height, network_difficulty, algorithm."""
    ov = str(override or "").upper()
    if ov in COINS:
        return {"coin": ov, "confidence": "manual", "source": "manual", "reason": "set manually in Settings"}

    algo = str(info.get("algorithm") or "").strip().lower()
    algo_coins = coins_for_algorithm(algo) if algo else set(COINS)
    if not algo_coins:
        algo_coins = set(COINS)

    reasons = []
    scoped_stats = {k: v for k, v in (stats or {}).items() if k in algo_coins}
    chain = chain_matches(info.get("block_height"), info.get("network_difficulty"), scoped_stats) if (info.get("block_height") or info.get("network_difficulty")) else []
    hint = host_hint(info.get("pool_url") or "")
    if hint and hint not in algo_coins:
        hint = None
    addr_set = address_coins(pool_user_address(info.get("pool_user") or "")) & algo_coins

    if len(chain) == 1:
        c = chain[0]
        why = f"miner's job matches the {COINS[c]['label']} chain tip"
        if hint and hint != c:
            why += f" (pool name suggests {hint})"
        return {"coin": c, "confidence": "high", "source": "chain-match", "reason": why}

    candidates = (set(chain) if chain else set(algo_coins))
    if addr_set:
        candidates &= addr_set
        reasons.append("payout address format")
    if hint and hint in candidates:
        if len(candidates) == 1 or not addr_set or hint in addr_set:
            return {"coin": hint, "confidence": "medium", "source": "pool-hint", "reason": f"pool host name ({pool_host(info.get('pool_url') or '')})" + (" + address format" if addr_set else "")}
    if len(candidates) == 1 and (addr_set or chain or algo):
        c = next(iter(candidates))
        return {"coin": c, "confidence": "medium", "source": "address" if addr_set else ("chain-match" if chain else "algorithm"), "reason": " + ".join(reasons) or ("algorithm match" if algo else "chain match")}
    why = "not enough information"
    if chain and len(chain) > 1:
        why = "job matches several chains"
    elif addr_set and len(candidates) > 1:
        why = "address format fits several coins: " + ", ".join(sorted(candidates))
    elif not candidates:
        why = "signals disagree"
    return {"coin": UNKNOWN, "confidence": "none", "source": "", "reason": why}


def cashaddr_encode(prefix: str, kind: int, h: bytes) -> str:
    """Encode a cashaddr (used by tests and to show addresses in a canonical form)."""
    payload = _convertbits([(kind << 3) | 0] + list(h), 8, 5, True) or []
    poly = _cashaddr_polymod([ord(c) & 31 for c in prefix] + [0] + payload + [0] * 8)
    checksum = [(poly >> 5 * (7 - i)) & 31 for i in range(8)]
    return prefix + ":" + "".join(_B32[d] for d in payload + checksum)
