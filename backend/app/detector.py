from collections import defaultdict
from datetime import datetime, timedelta, timezone
from app.models import Trade


def detect_clusters(trades: list[Trade], min_wallets: int = 3, window_minutes: int = 10,
                    eligible_wallets: set[str] | None = None) -> list[dict]:
    """Return token windows where distinct tracked wallets bought within the rolling window."""
    buys_by_token: dict[str, list[Trade]] = defaultdict(list)
    for trade in trades:
        if trade.side.lower() == "buy" and (eligible_wallets is None or trade.wallet_address in eligible_wallets):
            buys_by_token[trade.token_mint].append(trade)

    found = []
    window = timedelta(minutes=window_minutes)
    for mint, buys in buys_by_token.items():
        buys.sort(key=lambda item: item.occurred_at)
        left = 0
        for right, trade in enumerate(buys):
            current_time = trade.occurred_at
            if current_time.tzinfo is None:
                current_time = current_time.replace(tzinfo=timezone.utc)
            while left <= right:
                left_time = buys[left].occurred_at
                if left_time.tzinfo is None:
                    left_time = left_time.replace(tzinfo=timezone.utc)
                if current_time - left_time <= window:
                    break
                left += 1
            members = {item.wallet_address for item in buys[left:right + 1]}
            if len(members) >= min_wallets:
                found.append({"token_mint": mint, "wallet_addresses": sorted(members),
                              "wallet_count": len(members), "window_start": buys[left].occurred_at,
                              "window_end": trade.occurred_at})
                break
    return found
