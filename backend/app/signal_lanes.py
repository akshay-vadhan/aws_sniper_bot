from dataclasses import dataclass
from datetime import datetime, timezone
from collections import defaultdict


@dataclass
class LaneResult:
    detected: bool
    kind: str
    confidence: float
    reason: str


def detect_kol_buys(trades: list, kol_wallets: set[str]) -> list[dict]:
    by_token: dict[str, set[str]] = defaultdict(set)
    for trade in trades:
        if trade.side.lower() == "buy" and trade.wallet_address in kol_wallets:
            by_token[trade.token_mint].add(trade.wallet_address)
    return [{"token_mint": mint, "wallet_addresses": sorted(addresses), "wallet_count": len(addresses)}
            for mint, addresses in sorted(by_token.items())]


def detect_boost(paid_placements: int, unique_buyers: int, volume_usd: float,
                 baseline_volume_usd: float, liquidity_usd: float,
                 min_buyers: int = 8, min_liquidity_usd: float = 10_000,
                 min_volume_multiple: float = 1.5) -> LaneResult:
    organic = unique_buyers >= min_buyers and liquidity_usd >= min_liquidity_usd
    volume_multiple = volume_usd / max(baseline_volume_usd, 1.0)
    detected = paid_placements > 0 and organic and volume_multiple >= min_volume_multiple
    confidence = min(99.0, 40 + min(paid_placements, 4) * 5 + min(unique_buyers, 30) + min(volume_multiple * 8, 20))
    reason = "Paid promotion coincides with buyer and liquidity traction" if detected else "Promotion plus organic traction thresholds not met"
    return LaneResult(detected, "boost_radar", round(confidence, 1), reason)


def detect_graduate(curve_complete: bool, pool_address: str | None, liquidity_usd: float,
                    volume_5m_usd: float, min_liquidity_usd: float = 25_000,
                    min_volume_usd: float = 5_000) -> LaneResult:
    detected = curve_complete and bool(pool_address) and liquidity_usd >= min_liquidity_usd and volume_5m_usd >= min_volume_usd
    return LaneResult(detected, "graduate", 90.0 if detected else 0.0,
                      "Bonding curve completion has a live pool and sufficient liquidity/volume" if detected else "Graduation conditions not met")


def detect_hatchery(minted_at: datetime, mint_authority_revoked: bool,
                    freeze_authority_revoked: bool, liquidity_usd: float,
                    now: datetime | None = None, max_age_minutes: int = 60,
                    min_liquidity_usd: float = 5_000) -> LaneResult:
    now = now or datetime.now(timezone.utc)
    if minted_at.tzinfo is None:
        minted_at = minted_at.replace(tzinfo=timezone.utc)
    age_minutes = (now - minted_at).total_seconds() / 60
    detected = 0 <= age_minutes <= max_age_minutes and mint_authority_revoked and freeze_authority_revoked and liquidity_usd >= min_liquidity_usd
    return LaneResult(detected, "hatchery", 85.0 if detected else 0.0,
                      "Fresh mint has revoked authorities and minimum liquidity" if detected else "Mint age, authority, or liquidity safety conditions not met")


def backtest_return(entry_price: float, observed_prices: list[float]) -> dict:
    """Compute simple long-only return and max drawdown from chronological observations."""
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    peak = entry_price
    max_drawdown = 0.0
    for price in observed_prices:
        if price <= 0:
            continue
        peak = max(peak, price)
        max_drawdown = min(max_drawdown, (price / peak - 1) * 100)
    last_price = observed_prices[-1] if observed_prices else entry_price
    return {"entry_price": entry_price, "exit_price": last_price,
            "return_pct": round((last_price / entry_price - 1) * 100, 2),
            "max_drawdown_pct": round(max_drawdown, 2), "observations": len(observed_prices)}
