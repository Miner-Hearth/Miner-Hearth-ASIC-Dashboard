#!/usr/bin/env python3
"""Mining economics and probability helpers for Miner Hearth v1.13 (stdlib only).

All "earned" numbers are EXPECTED VALUES (long-run averages), not guarantees.
Supports SHA-256d, Scrypt (incl. LTC+DOGE merged), kHeavyHash, Blake3, Eaglesong,
ETChash, Equihash (Sol/s), X11, and Handshake using canonical H/s (or Sol/s).
"""

from __future__ import annotations

import math
from typing import Any, Optional

SATS_PER_BTC = 100_000_000
HALVING_INTERVAL = 210_000
RETARGET_INTERVAL = 2016
TARGET_BLOCK_SEC = 600
TWO_32 = float(2 ** 32)
DAYS_PER_MONTH = 30.4375
DAYS_PER_YEAR = 365.25

DEFAULT_BLOCK_TIME_SEC: dict[str, float] = {
    "sha256d": 600.0,
    "scrypt": 150.0,
    "kheavyhash": 1.0,
    "blake3": 16.0,
    "eaglesong": 10.0,
    "etchash": 13.0,
    "equihash": 75.0,
    "x11": 150.0,
    "handshake": 600.0,
}


def block_subsidy_sats(height: Optional[int]) -> int:
    if height is None or height < 0:
        height = 0
    halvings = int(height) // HALVING_INTERVAL
    if halvings >= 64:
        return 0
    return (50 * SATS_PER_BTC) >> halvings


def blocks_per_day(hashrate_ghs: Optional[float], difficulty: Optional[float]) -> float:
    """Expected number of SHA-256d blocks found per day (Poisson rate lambda)."""
    if not hashrate_ghs or not difficulty or hashrate_ghs <= 0 or difficulty <= 0:
        return 0.0
    hs = float(hashrate_ghs) * 1e9
    return hs * 86400.0 / (float(difficulty) * TWO_32)


def blocks_per_day_algo(
    hashrate_hs: Optional[float],
    difficulty: Optional[float],
    algorithm: str = "sha256d",
    network_hashrate_hs: Optional[float] = None,
    avg_block_sec: Optional[float] = None,
) -> float:
    """Algorithm-aware expected blocks found per day from canonical H/s (or Sol/s)."""
    if not hashrate_hs or hashrate_hs <= 0:
        return 0.0
    hs = float(hashrate_hs)
    algo = str(algorithm or "sha256d").strip().lower()

    if algo == "etchash":
        if difficulty and difficulty > 0:
            return hs * 86400.0 / float(difficulty)
        if network_hashrate_hs and network_hashrate_hs > 0:
            bt = float(avg_block_sec or DEFAULT_BLOCK_TIME_SEC["etchash"])
            return (hs / float(network_hashrate_hs)) * (86400.0 / max(0.05, bt))
        return 0.0

    if algo in {"kheavyhash", "blake3", "eaglesong", "equihash"}:
        if network_hashrate_hs and network_hashrate_hs > 0:
            bt = float(avg_block_sec or DEFAULT_BLOCK_TIME_SEC.get(algo, 10.0))
            return (hs / float(network_hashrate_hs)) * (86400.0 / max(0.05, bt))
        if difficulty and difficulty > 0:
            if algo == "equihash":
                # Zcash Equihash difficulty scales by 2^13 (8192) vs 2^32
                return hs * 86400.0 / (float(difficulty) * 8192.0)
            if algo == "kheavyhash":
                # Kaspa diff approximates network_hashes_per_sec / 2
                return hs * 86400.0 / (float(difficulty) * 2.0)
            return hs * 86400.0 / (float(difficulty) * TWO_32)
        return 0.0

    # sha256d, scrypt, x11, handshake
    if difficulty and difficulty > 0:
        return hs * 86400.0 / (float(difficulty) * TWO_32)
    if network_hashrate_hs and network_hashrate_hs > 0:
        bt = float(avg_block_sec or DEFAULT_BLOCK_TIME_SEC.get(algo, 600.0))
        return (hs / float(network_hashrate_hs)) * (86400.0 / max(0.05, bt))
    return 0.0


def chance_within(rate_per_day: float, days: float) -> float:
    """P(at least one block within `days`) = 1 - e^(-lambda * t)."""
    if rate_per_day <= 0 or days <= 0:
        return 0.0
    return -math.expm1(-rate_per_day * days)


def block_odds(hashrate_ghs: Optional[float], difficulty: Optional[float]) -> dict[str, Any]:
    lam = blocks_per_day(hashrate_ghs, difficulty)
    return _odds_from_lambda(lam)


def block_odds_algo(
    hashrate_hs: Optional[float],
    difficulty: Optional[float],
    algorithm: str = "sha256d",
    network_hashrate_hs: Optional[float] = None,
    avg_block_sec: Optional[float] = None,
) -> dict[str, Any]:
    lam = blocks_per_day_algo(
        hashrate_hs,
        difficulty,
        algorithm=algorithm,
        network_hashrate_hs=network_hashrate_hs,
        avg_block_sec=avg_block_sec,
    )
    return _odds_from_lambda(lam)


def _odds_from_lambda(lam: float) -> dict[str, Any]:
    expected_days = (1.0 / lam) if lam > 0 else None
    return {
        "blocks_per_day": lam,
        "chance_day": chance_within(lam, 1.0),
        "chance_month": chance_within(lam, DAYS_PER_MONTH),
        "chance_year": chance_within(lam, DAYS_PER_YEAR),
        "expected_days_to_block": expected_days,
        "expected_years_to_block": (expected_days / DAYS_PER_YEAR) if expected_days else None,
        "one_in_n_per_day": (1.0 / chance_within(lam, 1.0)) if lam > 0 else None,
    }


def expected_sats_per_day(
    hashrate_ghs: Optional[float],
    difficulty: Optional[float],
    subsidy_sats: int,
    avg_fees_sats: float = 0.0,
) -> float:
    return blocks_per_day(hashrate_ghs, difficulty) * (float(subsidy_sats) + max(0.0, float(avg_fees_sats or 0.0)))


def energy_kwh_per_day(watts: Optional[float]) -> float:
    if not watts or watts <= 0:
        return 0.0
    return float(watts) * 24.0 / 1000.0


def cost_per_day(watts: Optional[float], rate_per_kwh: float) -> float:
    return energy_kwh_per_day(watts) * max(0.0, float(rate_per_kwh or 0.0))


def break_even_rate(revenue_per_day: float, watts: Optional[float]) -> Optional[float]:
    """Electricity price per kWh at which expected revenue equals power cost."""
    kwh = energy_kwh_per_day(watts)
    if kwh <= 0:
        return None
    return max(0.0, float(revenue_per_day)) / kwh


def economics(
    hashrate_ghs: Optional[float],
    watts: Optional[float],
    difficulty: Optional[float],
    height: Optional[int],
    rate_per_kwh: float,
    btc_price: Optional[float],
    avg_fees_sats: float = 0.0,
    algorithm: str = "sha256d",
    hashrate_hs: Optional[float] = None,
    network_hashrate_hs: Optional[float] = None,
    avg_block_sec: Optional[float] = None,
) -> dict[str, Any]:
    algo = str(algorithm or "sha256d").strip().lower()
    hs = float(hashrate_hs) if hashrate_hs is not None else ((float(hashrate_ghs) * 1e9) if hashrate_ghs else None)
    subsidy = block_subsidy_sats(height)
    sats = expected_sats_per_day(hashrate_ghs, difficulty, subsidy, avg_fees_sats) if algo == "sha256d" else 0.0
    revenue = (sats / SATS_PER_BTC * float(btc_price)) if (btc_price and algo == "sha256d") else None
    cost = cost_per_day(watts, rate_per_kwh)
    out: dict[str, Any] = {
        "kwh_per_day": energy_kwh_per_day(watts),
        "cost_per_day": cost,
        "cost_per_month": cost * DAYS_PER_MONTH,
        "est_sats_per_day": sats,
        "est_revenue_per_day": revenue,
        "est_profit_per_day": (revenue - cost) if revenue is not None else None,
        "break_even_rate": break_even_rate(revenue, watts) if revenue is not None else None,
        "subsidy_sats": subsidy,
        "avg_fees_sats": float(avg_fees_sats or 0.0),
    }
    out.update(
        block_odds_algo(
            hs,
            difficulty,
            algorithm=algo,
            network_hashrate_hs=network_hashrate_hs,
            avg_block_sec=avg_block_sec,
        )
    )
    return out


def merged_coin_odds(
    hashrate_hs: Optional[float],
    primary_stats: Optional[dict[str, Any]],
    merged_stats: Optional[dict[str, Any]],
    algorithm: str = "scrypt",
) -> dict[str, Any]:
    """Compute primary + merged (e.g. LTC + DOGE AuxPoW) expected block odds."""
    p_st = primary_stats or {}
    m_st = merged_stats or {}
    primary = block_odds_algo(
        hashrate_hs,
        p_st.get("difficulty"),
        algorithm=algorithm,
        network_hashrate_hs=p_st.get("network_hashrate_hs"),
        avg_block_sec=p_st.get("avg_block_sec"),
    ) if (p_st.get("difficulty") or p_st.get("network_hashrate_hs")) else None
    merged = block_odds_algo(
        hashrate_hs,
        m_st.get("difficulty"),
        algorithm=algorithm,
        network_hashrate_hs=m_st.get("network_hashrate_hs"),
        avg_block_sec=m_st.get("avg_block_sec"),
    ) if (m_st.get("difficulty") or m_st.get("network_hashrate_hs")) else None
    return {"primary": primary, "merged": merged}


def estimate_retarget(
    height: int,
    now_ts: float,
    epoch_start_ts: Optional[float],
) -> dict[str, Any]:
    """Fallback next-difficulty-adjustment estimate (when the explorer has no endpoint for it)."""
    blocks_in_epoch = height % RETARGET_INTERVAL
    remaining = RETARGET_INTERVAL - blocks_in_epoch
    out: dict[str, Any] = {
        "progress_pct": blocks_in_epoch / RETARGET_INTERVAL * 100.0,
        "remaining_blocks": remaining,
        "next_retarget_height": height + remaining,
        "change_pct": None,
        "eta_ts": None,
    }
    if epoch_start_ts and blocks_in_epoch > 0 and now_ts > epoch_start_ts:
        avg = (now_ts - epoch_start_ts) / blocks_in_epoch
        if avg > 0:
            out["change_pct"] = (TARGET_BLOCK_SEC / avg - 1.0) * 100.0
            out["eta_ts"] = now_ts + remaining * avg
            out["avg_block_sec"] = avg
    return out


def format_difficulty(v: Any) -> str:
    """v1.16: difficulty like the miners show best share: 2,345,678 -> '2.35M' (K, M, G, T, P, E)."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(x) or x < 0:
        return ""
    if x < 1000:
        if x == 0:
            return "0"
        txt = f"{x:.3g}" if x >= 0.001 else f"{x:.2e}"
        return txt
    units = ["K", "M", "G", "T", "P", "E"]
    i = -1
    while x >= 1000 and i < len(units) - 1:
        x /= 1000.0
        i += 1
    if round(x, 2) >= 1000 and i < len(units) - 1:
        x /= 1000.0
        i += 1
    dec = 2 if x < 10 else (1 if x < 100 else 0)
    txt = f"{x:.{dec}f}"
    if "." in txt:
        txt = txt.rstrip("0").rstrip(".")
    return txt + units[i]
