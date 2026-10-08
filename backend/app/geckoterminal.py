"""Recent DEX pool discovery through GeckoTerminal's keyless public API."""
import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Any

import httpx

BASE_URL = "https://api.geckoterminal.com/api/v2"
NETWORKS = {"solana": "solana", "bsc": "bsc", "robinhood": "robinhood"}
_cache: dict[str, Any] = {"at": 0.0, "tokens": []}
_lock = asyncio.Lock()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


async def fetch_new_pools(force: bool = False) -> list[dict]:
    """Return recent pools and token metrics; the public service caches for 60s."""
    now = time.monotonic()
    if not force and now - _cache["at"] < 60:
        return list(_cache["tokens"])
    async with _lock:
        now = time.monotonic()
        if not force and now - _cache["at"] < 60:
            return list(_cache["tokens"])
        headers = {"Accept": "application/json;version=20230203"}
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            responses = await asyncio.gather(*(
                client.get(f"{BASE_URL}/networks/{network}/new_pools?page=1&include=base_token,quote_token")
                for network in NETWORKS.values()), return_exceptions=True)
        rows: list[dict] = []
        for (chain_id, network), response in zip(NETWORKS.items(), responses):
            if isinstance(response, Exception) or response.status_code >= 400:
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            included = {item.get("id"): item for item in payload.get("included", []) if isinstance(item, dict)}
            for pool in payload.get("data", []):
                if not isinstance(pool, dict):
                    continue
                attrs = pool.get("attributes") or {}
                rel = pool.get("relationships") or {}
                base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id")
                token = (included.get(base_id) or {}).get("attributes") or {}
                address = token.get("address")
                if not address:
                    continue
                created = attrs.get("pool_created_at")
                try:
                    created_at = datetime.fromisoformat(str(created).replace("Z", "+00:00")) if created else None
                    if created_at and created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                except ValueError:
                    created_at = None
                age_hours = max(0, int((datetime.now(timezone.utc) - created_at.astimezone(timezone.utc)).total_seconds() // 3600)) if created_at else None
                volume = attrs.get("volume_usd") or {}
                txns = attrs.get("transactions") or {}
                h24 = txns.get("h24") or {}
                price = _num(attrs.get("base_token_price_usd"))
                liquidity = _num(attrs.get("reserve_in_usd"))
                volume_24h = _num(volume.get("h24"))
                buys = int(_num(h24.get("buys")))
                sells = int(_num(h24.get("sells")))
                activity = 20 * math.log1p(buys + sells) + 14 * math.log1p(volume_24h / max(liquidity, 1))
                token_address = str(address)
                rows.append({
                    "mint": token_address, "chain_id": chain_id,
                    "symbol": str(token.get("symbol") or "???"),
                    "name": str(token.get("name") or attrs.get("name") or "Unknown token"),
                    "price_usd": price, "liquidity_usd": liquidity,
                    "market_cap_usd": _num(attrs.get("market_cap_usd"), _num(attrs.get("fdv_usd"))),
                    "volume_24h_usd": volume_24h, "volume_1h_usd": _num(volume.get("h1")),
                    "change_24h_pct": _num((attrs.get("price_change_percentage") or {}).get("h24")),
                    "buys_24h": buys, "sells_24h": sells, "boosts_active": 0,
                    "buys_1h": int(_num((txns.get("h1") or {}).get("buys"))),
                    "sells_1h": int(_num((txns.get("h1") or {}).get("sells"))),
                    "activity_score": round(max(0, activity), 1),
                    "pair_url": f"https://www.geckoterminal.com/{network}/pools/{attrs.get('address') or pool.get('id', '').split('_')[-1]}",
                    "dex_id": "GeckoTerminal", "age_hours": age_hours,
                    "first_seen_at": created_at.isoformat().replace("+00:00", "Z") if created_at else None,
                    "discovery_sources": ["new_pool"],
                })
        _cache["at"] = time.monotonic()
        _cache["tokens"] = rows
        return list(rows)
