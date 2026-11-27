"""Miner Hearth v1.14 pluggable miner adapter registry (Tier 1 + Tier 2 + Generic)."""

from __future__ import annotations

from typing import Any, Optional

from .apollo import ApolloAdapter
from .avalon import AvalonAdapter
from .axeos import AxeOSAdapter
from .base import (
    AdapterCapabilities,
    MinerAdapter,
    ProbeResult,
    cgminer_request,
    format_hashrate_hs,
    hs_to_ghs,
    http_json,
    http_request_raw,
    json_get_path,
    tcp_open,
    to_hs,
    unit_kind_for_algorithm,
    unit_multiplier,
)
from .bitmain import BitmainStockAdapter, infer_bitmain_algorithm, parse_bitmain_cgi_stats
from .braiins import BraiinsAdapter, parse_prometheus_metrics
from .elphapex import ElphapexAdapter, parse_elphapex_stats
from .generic import (
    GenericCGMinerAdapter,
    GenericHTTPJsonAdapter,
    GenericPrometheusAdapter,
    parse_generic_http_json,
    parse_generic_prometheus,
)
from .goldshell import GoldshellAdapter, infer_goldshell_algorithm, parse_goldshell_status
from .iceriver import (
    IceRiverAdapter,
    infer_iceriver_algorithm,
    is_iceriver_cgminer,
    parse_iceriver_oc_overview,
    parse_iceriver_userpanel,
)
from .luxos import LuxOSAdapter
from .sealminer import SealminerAdapter, is_sealminer_payload, parse_sealminer_readings
from .vnish import VnishAdapter
from .whatsminer import (
    WhatsminerAdapter,
    is_whatsminer_payload,
    parse_whatsminer_v2,
    parse_whatsminer_v3,
    whatsminer_v3_request,
)

TIER1_ADAPTERS: tuple[Any, ...] = (
    BraiinsAdapter(),
    VnishAdapter(),
    LuxOSAdapter(),
    AvalonAdapter(),
    ApolloAdapter(),
    BitmainStockAdapter(),
    AxeOSAdapter(),
)

TIER2_ADAPTERS: tuple[Any, ...] = (
    WhatsminerAdapter(),
    SealminerAdapter(),
    IceRiverAdapter(),
    GoldshellAdapter(),
    ElphapexAdapter(),
)

GENERIC_ADAPTERS: tuple[Any, ...] = (
    GenericCGMinerAdapter(),
    GenericPrometheusAdapter(),
    GenericHTTPJsonAdapter(),
)

# Specificity order: Tier-1 specific -> Tier-2 specific -> BitmainStock/AxeOS -> Generic fallbacks
ADAPTERS: tuple[Any, ...] = (
    BraiinsAdapter(),
    VnishAdapter(),
    LuxOSAdapter(),
    AvalonAdapter(),
    ApolloAdapter(),
    WhatsminerAdapter(),
    SealminerAdapter(),
    IceRiverAdapter(),
    GoldshellAdapter(),
    ElphapexAdapter(),
    BitmainStockAdapter(),
    AxeOSAdapter(),
    GenericCGMinerAdapter(),
    GenericPrometheusAdapter(),
    GenericHTTPJsonAdapter(),
)

ADAPTER_BY_FAMILY: dict[str, Any] = {a.family_id: a for a in ADAPTERS}

# Alias map for backwards compatibility with v1.12/v1.13 family names
FAMILY_ALIASES: dict[str, str] = {
    "gamma": "axeos",
    "nerdoctaxe": "axeos",
    "axeos-generic": "axeos",
    "braiins os": "braiins",
    "braiins-plus": "braiins",
    "stock bitmain": "bitmain-stock",
    "antminer": "bitmain-stock",
    "btminer": "whatsminer",
    "microbt": "whatsminer",
    "bdminer": "sealminer",
    "bitdeer": "sealminer",
    "cgminer": "generic-cgminer",
    "prometheus": "generic-prometheus",
    "http-json": "generic-http-json",
}


# v1.16: adapters added by a live update (userdata/updates/current/plugins/*.py); replaced as a set on every reload
PLUGIN_ADAPTERS: list[Any] = []
_BUILTIN_BY_FAMILY: dict[str, Any] = dict(ADAPTER_BY_FAMILY)


def set_plugin_adapters(adapters: list[Any]) -> None:
    """Register plugin adapters (a plugin may also replace a built-in family by using its family_id)."""
    PLUGIN_ADAPTERS[:] = [a for a in adapters if getattr(a, "family_id", "")]
    ADAPTER_BY_FAMILY.clear()
    ADAPTER_BY_FAMILY.update(_BUILTIN_BY_FAMILY)
    for a in PLUGIN_ADAPTERS:
        ADAPTER_BY_FAMILY[a.family_id] = a


def _probe_order() -> tuple[Any, ...]:
    """Built-in specific adapters, then plugins, then the generic fallbacks; a plugin replaces a built-in of its family."""
    plug = {a.family_id for a in PLUGIN_ADAPTERS}
    generic = {a.family_id for a in GENERIC_ADAPTERS}
    specific = [a for a in ADAPTERS if a.family_id not in generic and a.family_id not in plug]
    return tuple(specific) + tuple(PLUGIN_ADAPTERS) + tuple(a for a in ADAPTERS if a.family_id in generic and a.family_id not in plug)


def get_adapter(family: str) -> Optional[Any]:
    key = str(family or "").strip().lower()
    if key in ADAPTER_BY_FAMILY:
        return ADAPTER_BY_FAMILY[key]
    alias = FAMILY_ALIASES.get(key)
    return ADAPTER_BY_FAMILY.get(alias) if alias else None


def probe_host_tier1(
    ip: str,
    http_port: int = 80,
    cgminer_port: int = 4028,
    tcp_timeout: float = 1.0,
    http_timeout: float = 4.0,
) -> Optional[ProbeResult]:
    """Run Tier-1 adapter probes in specificity order."""
    for adapter in TIER1_ADAPTERS:
        try:
            res = adapter.probe(
                ip,
                http_port=http_port,
                cgminer_port=cgminer_port,
                tcp_timeout=tcp_timeout,
                http_timeout=http_timeout,
            )
            if res is not None:
                return res
        except Exception:
            continue
    return None


def probe_host_all(
    ip: str,
    http_port: int = 80,
    cgminer_port: int = 4028,
    tcp_timeout: float = 1.0,
    http_timeout: float = 4.0,
) -> Optional[ProbeResult]:
    """Run all Tier-1, Tier-2, plugin (v1.16) and Generic fallback adapter probes in specificity order."""
    for adapter in _probe_order():
        try:
            res = adapter.probe(
                ip,
                http_port=http_port,
                cgminer_port=cgminer_port,
                tcp_timeout=tcp_timeout,
                http_timeout=http_timeout,
            )
            if res is not None:
                return res
        except Exception:
            continue
    return None


__all__ = [
    "ADAPTERS",
    "PLUGIN_ADAPTERS",
    "set_plugin_adapters",
    "ADAPTER_BY_FAMILY",
    "AdapterCapabilities",
    "ApolloAdapter",
    "AvalonAdapter",
    "AxeOSAdapter",
    "BitmainStockAdapter",
    "BraiinsAdapter",
    "ElphapexAdapter",
    "GENERIC_ADAPTERS",
    "GenericCGMinerAdapter",
    "GenericHTTPJsonAdapter",
    "GenericPrometheusAdapter",
    "GoldshellAdapter",
    "IceRiverAdapter",
    "LuxOSAdapter",
    "MinerAdapter",
    "ProbeResult",
    "SealminerAdapter",
    "TIER1_ADAPTERS",
    "TIER2_ADAPTERS",
    "VnishAdapter",
    "WhatsminerAdapter",
    "cgminer_request",
    "format_hashrate_hs",
    "get_adapter",
    "hs_to_ghs",
    "http_json",
    "http_request_raw",
    "infer_bitmain_algorithm",
    "infer_goldshell_algorithm",
    "infer_iceriver_algorithm",
    "is_iceriver_cgminer",
    "is_sealminer_payload",
    "is_whatsminer_payload",
    "json_get_path",
    "parse_bitmain_cgi_stats",
    "parse_elphapex_stats",
    "parse_generic_http_json",
    "parse_generic_prometheus",
    "parse_goldshell_status",
    "parse_iceriver_oc_overview",
    "parse_iceriver_userpanel",
    "parse_prometheus_metrics",
    "parse_sealminer_readings",
    "parse_whatsminer_v2",
    "parse_whatsminer_v3",
    "probe_host_all",
    "probe_host_tier1",
    "tcp_open",
    "to_hs",
    "unit_kind_for_algorithm",
    "unit_multiplier",
    "whatsminer_v3_request",
]
