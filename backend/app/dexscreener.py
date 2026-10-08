"""Public DEX Screener market discovery across supported chains.

Discovery uses the documented latest-profile and boosted-token feeds, then loads
Solana pair metrics in batches. Ranking is our activity heuristic, not the site's
proprietary trending score.
"""
import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from app.geckoterminal import fetch_new_pools

BASE_URL = "https://api.dexscreener.com"
DISCOVERY_TTL_SECONDS = 60
SUPPORTED_CHAINS = {"solana", "bsc", "robinhood"}
_cache: dict[str, dict[str, Any]] = {}
_cache_lock = asyncio.Lock()


def _as_list(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        if "tokenAddress" in payload or "baseToken" in payload:
            return [payload]
        for key in ("pairs", "tokens", "profiles", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


async def _get(client: httpx.AsyncClient, path: str) -> Any:
    response = await client.get(f"{BASE_URL}{path}")
    response.raise_for_status()
    return response.json()


def _activity_score(pair: dict, active_boosts: float) -> float:
    volume = pair.get("volume") or {}
    txns = pair.get("txns") or {}
    changes = pair.get("priceChange") or {}
    liquidity = _num((pair.get("liquidity") or {}).get("usd"))
    volume_h1 = _num(volume.get("h1"))
    txns_h1 = txns.get("h1") or {}
    activity = _num(txns_h1.get("buys")) + _num(txns_h1.get("sells"))
    change_h1 = max(-20.0, min(20.0, _num(changes.get("h1"))))
    score = 20 * math.log1p(activity) + 14 * math.log1p(volume_h1 / max(liquidity, 1))
    score += 3 * min(active_boosts, 10) + change_h1
    return round(max(0.0, score), 1)


async def fetch_trending(limit: int = 30, force: bool = False, chain: str = "all") -> dict:
    """Fetch real token/pair metrics for Solana, BNB Chain and Robinhood Chain."""
    requested = chain.lower()
    chains = sorted(SUPPORTED_CHAINS) if requested == "all" else [requested]
    if any(value not in SUPPORTED_CHAINS for value in chains):
        raise ValueError(f"Unsupported chain '{chain}'. Choose all, solana, bsc, or robinhood.")
    now = time.monotonic()
    cache_key = ",".join(chains)
    cached = _cache.get(cache_key)
    if not force and cached is not None and now - cached["at"] < DISCOVERY_TTL_SECONDS:
        return {**cached["data"], "stale": False}

    async with _cache_lock:
        now = time.monotonic()
        cached = _cache.get(cache_key)
        if not force and cached is not None and now - cached["at"] < DISCOVERY_TTL_SECONDS:
            return {**cached["data"], "stale": False}
        try:
            async with httpx.AsyncClient(timeout=15, headers={"Accept": "application/json"}) as client:
                feeds = await asyncio.gather(
                    _get(client, "/token-profiles/latest/v1"),
                    _get(client, "/token-boosts/latest/v1"),
                    _get(client, "/token-boosts/top/v1"), return_exceptions=True)
                profiles, latest_boosts, top_boosts = [feed if not isinstance(feed, Exception) else [] for feed in feeds]

                discovery: dict[str, dict] = {}
                # Prioritize active boost feeds, then use latest profiles to fill the list.
                for source, rows in (("top_boost", _as_list(top_boosts)),
                                     ("latest_boost", _as_list(latest_boosts)),
                                     ("profile", _as_list(profiles))):
                    for row in rows:
                        row_chain = str(row.get("chainId", "")).lower()
                        if row_chain not in chains:
                            continue
                        mint = row.get("tokenAddress")
                        if not isinstance(mint, str) or not mint:
                            continue
                        key = (row_chain, mint)
                        entry = discovery.setdefault(key, {"sources": set(), "boosts": 0, "total_boosts": 0})
                        entry["sources"].add(source)
                        entry["boosts"] = max(entry["boosts"], _num(row.get("amount")))
                        entry["total_boosts"] = max(entry["total_boosts"], _num(row.get("totalAmount")))

                addresses_by_chain = {chain_id: [mint for discovered_chain, mint in discovery if discovered_chain == chain_id]
                                      for chain_id in chains}
                addresses_by_chain = {chain_id: addresses[:30] for chain_id, addresses in addresses_by_chain.items() if addresses}
                tokens = []
                if not addresses_by_chain:
                    pass
                else:
                    pair_results = await asyncio.gather(*(
                        _get(client, f"/tokens/v1/{chain_id}/{','.join(addresses)}")
                        for chain_id, addresses in addresses_by_chain.items()), return_exceptions=True)
                    best_pair: dict[tuple[str, str], dict] = {}
                    for pairs in pair_results:
                        if isinstance(pairs, Exception):
                            continue
                        for pair in _as_list(pairs):
                            row_chain = str(pair.get("chainId", "")).lower()
                            if row_chain not in chains:
                                continue
                            mint = (pair.get("baseToken") or {}).get("address")
                            key = (row_chain, mint)
                            if key not in discovery:
                                continue
                            current = best_pair.get(key)
                            pair_liquidity = _num((pair.get("liquidity") or {}).get("usd"))
                            if current is None or pair_liquidity > _num((current.get("liquidity") or {}).get("usd")):
                                best_pair[key] = pair

                    for (chain_id, mint), pair in best_pair.items():
                        base = pair.get("baseToken") or {}
                        boosts = pair.get("boosts") or {}
                        discovered = discovery[(chain_id, mint)]
                        active_boosts = max(_num(boosts.get("active")), discovered["boosts"])
                        market_cap = _num(pair.get("marketCap"), _num(pair.get("fdv")))
                        pair_created = pair.get("pairCreatedAt")
                        created_at = datetime.fromtimestamp(_num(pair_created) / 1000, timezone.utc) if pair_created else None
                        age_hours = max(0, int((datetime.now(timezone.utc) - created_at).total_seconds() // 3600)) if created_at else None
                        volume = pair.get("volume") or {}
                        txns = pair.get("txns") or {}
                        txns_h1 = txns.get("h1") or {}
                        tokens.append({
                            "mint": mint, "chain_id": chain_id, "symbol": str(base.get("symbol") or "???"),
                            "name": str(base.get("name") or "Unknown token"),
                            "liquidity_usd": _num((pair.get("liquidity") or {}).get("usd")),
                            "market_cap_usd": market_cap, "price_usd": _num(pair.get("priceUsd")),
                            "volume_24h_usd": _num(volume.get("h24")), "volume_1h_usd": _num(volume.get("h1")),
                            "change_24h_pct": _num((pair.get("priceChange") or {}).get("h24")),
                            "buys_24h": int(_num((txns.get("h24") or {}).get("buys"))),
                            "sells_24h": int(_num((txns.get("h24") or {}).get("sells"))),
                            "buys_1h": int(_num(txns_h1.get("buys"))),
                            "sells_1h": int(_num(txns_h1.get("sells"))),
                            "boosts_active": active_boosts,
                            "boosts_total": max(_num(boosts.get("active")), discovered["total_boosts"]),
                            "activity_score": _activity_score(pair, active_boosts),
                            "pair_url": pair.get("url"), "dex_id": pair.get("dexId"),
                            "first_seen_at": created_at.isoformat().replace("+00:00", "Z") if created_at else None,
                            "age_hours": age_hours, "discovery_sources": sorted(discovered["sources"]),
                        })
                pool_tokens = await fetch_new_pools(force=force)
                pool_tokens = [row for row in pool_tokens if row.get("chain_id") in chains]
                by_identity = {(row.get("chain_id"), row.get("mint")): row for row in tokens}
                for pool_token in pool_tokens:
                    key = (pool_token.get("chain_id"), pool_token.get("mint"))
                    current = by_identity.get(key)
                    if current is None:
                        by_identity[key] = pool_token
                    else:
                        current["discovery_sources"] = sorted(set(current.get("discovery_sources", [])) | {"new_pool"})
                        # Prefer pair market figures when present; use new-pool creation time as launch metadata.
                        if current.get("age_hours") is None or (pool_token.get("age_hours") is not None and pool_token["age_hours"] < current["age_hours"]):
                            current["age_hours"] = pool_token["age_hours"]
                            current["first_seen_at"] = pool_token.get("first_seen_at")
                tokens = list(by_identity.values())
                tokens.sort(key=lambda row: (row["activity_score"], row["volume_24h_usd"]), reverse=True)
                result = {"source": "DEX Screener + GeckoTerminal", "updated_at": datetime.now(timezone.utc).isoformat(),
                          "stale": False, "chains": chains, "tokens": tokens[:min(max(limit, 1), 90)]}
            _cache[cache_key] = {"at": time.monotonic(), "data": result}
            return result
        except (httpx.HTTPError, ValueError) as exc:
            if cached is not None:
                return {**cached["data"], "stale": True, "warning": str(exc)[:200]}
            raise RuntimeError(f"DEX Screener market data is unavailable: {exc}") from exc


async def fetch_solana_trending(limit: int = 30, force: bool = False) -> dict:
    """Backward-compatible Solana-only discovery helper."""
    return await fetch_trending(limit=limit, force=force, chain="solana")


async def fetch_pair_metrics(mints: list[str], chain: str = "solana") -> dict[str, dict]:
    """Return best available price/liquidity metadata for a small batch of Solana mints."""
    clean = list(dict.fromkeys(mint for mint in mints if isinstance(mint, str) and mint))[:30]
    if not clean:
        return {}
    try:
        async with httpx.AsyncClient(timeout=12, headers={"Accept": "application/json"}) as client:
            pairs = _as_list(await _get(client, f"/tokens/v1/{chain}/{','.join(clean)}"))
    except (httpx.HTTPError, ValueError):
        return {}
    result: dict[str, dict] = {}
    for pair in pairs:
        if str(pair.get("chainId", "")).lower() != chain.lower():
            continue
        mint = (pair.get("baseToken") or {}).get("address")
        if mint not in clean:
            continue
        if mint not in result or _num((pair.get("liquidity") or {}).get("usd")) > result[mint]["liquidity_usd"]:
            base = pair.get("baseToken") or {}
            result[mint] = {"symbol": str(base.get("symbol") or "???"),
                "name": str(base.get("name") or "Unknown token"),
                "price_usd": _num(pair.get("priceUsd")),
                "liquidity_usd": _num((pair.get("liquidity") or {}).get("usd")),
                "market_cap_usd": _num(pair.get("marketCap"), _num(pair.get("fdv"))),
                "pair_url": pair.get("url")}
    return result
