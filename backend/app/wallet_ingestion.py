"""Recent tracked-wallet swap ingestion using Helius or Alchemy."""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.dexscreener import fetch_pair_metrics
from app.detector import detect_clusters
from app.models import Signal, Token, Trade, Wallet
from app.solana_providers import fetch_wallet_transactions, provider_status

WRAPPED_SOL = "So11111111111111111111111111111111111111112"
QUOTE_MINTS = {WRAPPED_SOL, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}


def _swap_changes(tx: dict, address: str) -> list[tuple[str, str, float]]:
    """Return (mint, side, token amount) entries for one wallet-oriented swap."""
    if str(tx.get("type", "")).upper() != "SWAP":
        return []
    deltas: dict[str, float] = {}
    for change in tx.get("tokenBalanceChanges") or []:
        if change.get("userAccount") != address:
            continue
        raw = change.get("rawTokenAmount") or {}
        try:
            amount = float(raw.get("tokenAmount", 0)) / (10 ** int(raw.get("decimals", 0)))
        except (TypeError, ValueError, OverflowError):
            continue
        mint = change.get("mint")
        if mint and mint not in QUOTE_MINTS and amount:
            deltas[mint] = deltas.get(mint, 0.0) + amount
    if not deltas:
        for transfer in tx.get("tokenTransfers") or []:
            mint = transfer.get("mint")
            if not mint or mint in QUOTE_MINTS:
                continue
            try:
                amount = float(transfer.get("tokenAmount", 0))
            except (TypeError, ValueError):
                continue
            if transfer.get("toUserAccount") == address:
                deltas[mint] = deltas.get(mint, 0.0) + amount
            if transfer.get("fromUserAccount") == address:
                deltas[mint] = deltas.get(mint, 0.0) - amount
    return [(mint, "buy" if amount > 0 else "sell", abs(amount))
            for mint, amount in deltas.items() if abs(amount) > 0]


async def _fetch_recent(address: str) -> list[dict]:
    return await fetch_wallet_transactions(address)


async def _ingest_wallet(address: str) -> int:
    db = SessionLocal()
    try:
        wallet = db.get(Wallet, address)
        if wallet is None or not wallet.tracked:
            return 0
        transactions = await _fetch_recent(address)
        fresh = []
        for tx in transactions:
            signature = tx.get("signature")
            if not signature:
                continue
            if wallet.last_signature and signature == wallet.last_signature:
                break
            fresh.append(tx)
        newest_signature = transactions[0].get("signature") if transactions else None
        parsed: list[tuple[dict, str, str, float]] = []
        for tx in fresh:
            for mint, side, amount in _swap_changes(tx, address):
                parsed.append((tx, mint, side, amount))
        prices = await fetch_pair_metrics([mint for _, mint, _, _ in parsed])
        added = 0
        for tx, mint, side, amount in parsed:
            info = prices.get(mint, {})
            token = db.get(Token, mint)
            if token is None:
                token = Token(mint=mint, symbol=info.get("symbol", "???"), name=info.get("name", "Unknown token"),
                              liquidity_usd=info.get("liquidity_usd", 0), market_cap_usd=info.get("market_cap_usd", 0))
                db.add(token)
                db.flush()
            elif info:
                token.symbol = info["symbol"]
                token.name = info["name"]
                token.liquidity_usd = info["liquidity_usd"]
                token.market_cap_usd = info["market_cap_usd"]
            signature = tx["signature"]
            if db.scalar(select(Trade.id).where(Trade.signature == signature,
                    Trade.wallet_address == address, Trade.token_mint == mint).limit(1)):
                continue
            stamp = tx.get("timestamp")
            occurred = datetime.fromtimestamp(float(stamp), timezone.utc) if stamp else datetime.now(timezone.utc)
            db.add(Trade(signature=signature, wallet_address=address, token_mint=mint,
                         side=side, amount_usd=amount * info.get("price_usd", 0), occurred_at=occurred))
            added += 1
        if newest_signature:
            wallet.last_signature = newest_signature
        if added:
            wallet.trades_count += added
        db.commit()
        return added
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def ingest_tracked_wallets() -> int:
    if not provider_status()["configured"]:
        return 0
    db = SessionLocal()
    try:
        addresses = db.scalars(select(Wallet.address).where(Wallet.tracked.is_(True),
                    ~Wallet.address.like("DemoWhale%"))).all()
    finally:
        db.close()
    total = 0
    for address in addresses:
        try:
            total += await _ingest_wallet(address)
        except Exception as exc:
            # Keep polling other wallets; errors are surfaced by the ingestion status route.
            _last_error[address] = _safe_provider_error(exc)
        else:
            _last_error.pop(address, None)
    _detect_recent_clusters()
    return total


_last_error: dict[str, str] = {}


def _safe_provider_error(exc: Exception) -> str:
    return str(exc)[:200]


def _detect_recent_clusters() -> None:
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc).timestamp() - settings.cluster_window_minutes * 60
        trades = db.scalars(select(Trade).where(Trade.side == "buy")).all()
        recent = [trade for trade in trades if (trade.occurred_at.replace(tzinfo=timezone.utc) if trade.occurred_at.tzinfo is None else trade.occurred_at).timestamp() >= cutoff]
        eligible = set(db.scalars(select(Wallet.address).where(Wallet.tracked.is_(True),
                            ~Wallet.address.like("DemoWhale%"))).all())
        now = datetime.now(timezone.utc)
        for cluster in detect_clusters(recent, settings.cluster_min_wallets,
                                       settings.cluster_window_minutes, eligible):
            mint = cluster["token_mint"]
            token = db.get(Token, mint)
            if token is None:
                continue
            previous = db.scalar(select(Signal).where(Signal.kind == "cluster", Signal.token_mint == mint,
                Signal.created_at >= now - timedelta(minutes=settings.cluster_window_minutes))
                .order_by(Signal.created_at.desc()).limit(1))
            title = f"${token.symbol}: {cluster['wallet_count']} tracked wallets bought within {settings.cluster_window_minutes} min"
            if previous:
                previous.wallet_count = cluster["wallet_count"]
                previous.wallet_addresses = ",".join(cluster["wallet_addresses"])
                previous.title = title
                previous.created_at = now
            else:
                db.add(Signal(kind="cluster", token_mint=mint, title=title,
                    wallet_count=cluster["wallet_count"], confidence=min(99, 55 + 10 * cluster["wallet_count"]),
                    created_at=now, wallet_addresses=",".join(cluster["wallet_addresses"])))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def poll_loop():
    while True:
        try:
            await ingest_tracked_wallets()
        except Exception as exc:
            _last_error["worker"] = _safe_provider_error(exc)
        await asyncio.sleep(max(15, settings.wallet_poll_seconds))
