"""Rule-based paper trader. It never signs or submits blockchain transactions."""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from app.config import settings
from app.database import SessionLocal
from app.dexscreener import fetch_pair_metrics, fetch_trending
from app.models import BotAccount, PaperPosition, PaperTradeEvent, Token
from app.telegram import send_paper_trade

_last_cycle: str | None = None
_last_error: str | None = None
_cycle_lock = asyncio.Lock()


def runtime_status() -> dict:
    return {"last_cycle_at": _last_cycle, "last_error": _last_error,
            "interval_seconds": max(15, settings.paper_bot_interval_seconds)}


def ensure_account(db) -> BotAccount:
    account = db.get(BotAccount, 1)
    if account is None:
        account = BotAccount(id=1, mode="paper", enabled=False, starting_balance_usd=1000,
            cash_balance_usd=1000, position_size_usd=10, target_multiple=2,
            stop_loss_pct=60, min_liquidity_usd=5000, min_volume_24h_usd=0,
            min_activity_score=10, max_open_positions=0, fee_bps=100)
        db.add(account)
        db.commit()
        db.refresh(account)
    return account


def analyze_candidate(token: dict, account: BotAccount) -> tuple[float, str] | None:
    """Use transparent market activity/liquidity gates before paper entry."""
    price = float(token.get("price_usd") or 0)
    liquidity = float(token.get("liquidity_usd") or 0)
    volume = float(token.get("volume_24h_usd") or 0)
    activity = float(token.get("activity_score") or 0)
    buys = int(token.get("buys_24h") or 0)
    sells = int(token.get("sells_24h") or 0)
    if price <= 0 or liquidity < account.min_liquidity_usd or volume < account.min_volume_24h_usd:
        return None
    if activity < account.min_activity_score or buys < max(1, sells):
        return None
    if float(token.get("change_24h_pct") or 0) < -20:
        return None
    buyer_share = buys / max(1, buys + sells)
    liquidity_score = min(100, liquidity / max(account.min_liquidity_usd, 1) * 50)
    volume_score = min(100, volume / max(account.min_volume_24h_usd, 1) * 35)
    score = round(min(100, activity * 0.45 + liquidity_score * 0.2 + volume_score * 0.2 + buyer_share * 100 * 0.15), 1)
    reason = f"Activity {activity:.0f}, liquidity ${liquidity:,.0f}, 24h volume ${volume:,.0f}, buyers {buyer_share:.0%}"
    return score, reason


def exit_reason(position: PaperPosition, price_usd: float, account: BotAccount) -> tuple[str | None, float, float]:
    """Return exit reason and fee-adjusted net proceeds/PnL at a market price."""
    fee = max(0, account.fee_bps) / 10_000
    value = position.quantity * price_usd * (1 - fee)
    pnl = value - position.invested_usd
    pnl_pct = pnl / position.invested_usd * 100 if position.invested_usd else 0
    if value >= position.invested_usd * account.target_multiple:
        return "take_profit", value, pnl_pct
    if pnl_pct <= -account.stop_loss_pct:
        return "stop_loss", value, pnl_pct
    return None, value, pnl_pct


def _upsert_token(db, item: dict) -> Token:
    mint = item["mint"]
    token = db.get(Token, mint)
    if token is None:
        token = Token(mint=mint, symbol=str(item.get("symbol") or "???")[:24],
            chain_id=str(item.get("chain_id") or "solana")[:20],
            name=str(item.get("name") or "Unknown token")[:100],
            liquidity_usd=float(item.get("liquidity_usd") or 0),
            market_cap_usd=float(item.get("market_cap_usd") or 0))
        db.add(token)
    else:
        token.chain_id = str(item.get("chain_id") or token.chain_id or "solana")[:20]
        token.symbol = str(item.get("symbol") or token.symbol)[:24]
        token.name = str(item.get("name") or token.name)[:100]
        token.liquidity_usd = float(item.get("liquidity_usd") or 0)
        token.market_cap_usd = float(item.get("market_cap_usd") or 0)
    return token


def analyze_sniper_candidate(token: dict, account: BotAccount) -> tuple[float, str] | None:
    """Gate entries to young, liquid pools with early buy pressure."""
    price = float(token.get("price_usd") or 0)
    liquidity = float(token.get("liquidity_usd") or 0)
    age = token.get("age_hours")
    volume_1h = float(token.get("volume_1h_usd") or 0)
    activity = float(token.get("activity_score") or 0)
    buys = int(token.get("buys_1h", token.get("buys_24h", 0)) or 0)
    sells = int(token.get("sells_1h", token.get("sells_24h", 0)) or 0)
    if price <= 0 or age is None or float(age) > 24:
        return None
    if liquidity < account.min_liquidity_usd or volume_1h < account.min_volume_24h_usd:
        return None
    if activity < account.min_activity_score or buys < max(3, sells):
        return None
    buyer_share = buys / max(1, buys + sells)
    score = round(min(100, activity * 0.5 + min(100, liquidity / max(account.min_liquidity_usd, 1) * 40) * 0.2 + buyer_share * 30), 1)
    reason = f"{token.get('chain_id', 'solana')} new pool · age {float(age):.1f}h · liquidity ${liquidity:,.0f} · 1h buyers {buys}:{sells}"
    return score, reason


def _record_event(db, position: PaperPosition, side: str, reason: str,
                  price: float, quantity: float, proceeds: float = 0, realized: float = 0):
    db.add(PaperTradeEvent(position_id=position.id, chain_id=position.chain_id,
        token_mint=position.token_mint, symbol=position.symbol, side=side,
        reason=reason, price_usd=price, quantity=quantity, proceeds_usd=proceeds,
        realized_pnl_usd=realized, created_at=datetime.now(timezone.utc)))


def _sell_quantity(db, position: PaperPosition, account: BotAccount, price: float,
                   quantity: float, reason: str) -> None:
    before = max(position.quantity, 0)
    sold = min(max(quantity, 0), before)
    if sold <= 0 or before <= 0:
        return
    fee = max(0, account.fee_bps) / 10_000
    basis = position.remaining_cost_usd * sold / before
    proceeds = sold * price * (1 - fee)
    pnl = proceeds - basis
    position.quantity = max(0, before - sold)
    position.remaining_cost_usd = max(0, position.remaining_cost_usd - basis)
    position.realized_pnl_usd += pnl
    account.cash_balance_usd += proceeds
    _record_event(db, position, "sell", reason, price, sold, proceeds, pnl)


def manage_sniper_position(db, position: PaperPosition, account: BotAccount,
                           price: float, now: datetime) -> None:
    """Apply scale-outs, the post-2x trailing stop, pre-2x hard stop, and time exit."""
    if price <= 0 or position.status != "open":
        return
    if position.opened_at.tzinfo is None:
        opened = position.opened_at.replace(tzinfo=timezone.utc)
    else:
        opened = position.opened_at.astimezone(timezone.utc)
    position.current_price_usd = price
    position.peak_price_usd = max(position.peak_price_usd or position.entry_price_usd, price)
    position.reached_2x = position.reached_2x or price >= position.entry_price_usd * 2
    closure_reason = None

    if now.astimezone(timezone.utc) - opened >= timedelta(days=3):
        closure_reason = "max_hold_3d"
        _sell_quantity(db, position, account, price, position.quantity, "max_hold_3d")
    elif not position.reached_2x and price <= position.entry_price_usd * 0.4:
        closure_reason = "stop_loss_60"
        _sell_quantity(db, position, account, price, position.quantity, "stop_loss_60")
    else:
        for multiple, fraction, flag, reason in (
            (2, 1 / 3, "sold_2x", "take_profit_2x"),
            (3, 0.15, "sold_3x", "take_profit_3x"),
            (5, 0.15, "sold_5x", "take_profit_5x"),
            (10, 0.15, "sold_10x", "take_profit_10x"),
        ):
            if not getattr(position, flag) and price >= position.entry_price_usd * multiple:
                setattr(position, flag, True)
                # Model standing take-profit limits at their named multiple, even if a poll skips over a level.
                _sell_quantity(db, position, account, position.entry_price_usd * multiple,
                               position.initial_quantity * fraction, reason)
        if position.reached_2x and position.quantity > 0 and price <= position.peak_price_usd * 0.7:
            closure_reason = "trailing_stop_30"
            _sell_quantity(db, position, account, price, position.quantity, "trailing_stop_30")

    fee = max(0, account.fee_bps) / 10_000
    position.current_value_usd = position.quantity * price * (1 - fee)
    position.realized_pnl_pct = (position.realized_pnl_usd + position.current_value_usd - position.remaining_cost_usd) / position.invested_usd * 100 if position.invested_usd else 0
    if position.quantity <= max(position.initial_quantity * 1e-12, 1e-18):
        position.quantity = 0
        position.remaining_cost_usd = 0
        position.current_value_usd = 0
        position.status = "closed"
        position.exit_price_usd = price
        position.closed_at = now
        position.exit_reason = closure_reason or "all_targets_sold"
    elif position.status == "open" and (position.exit_reason or "") == "":
        if closure_reason:
            position.exit_reason = closure_reason
    if position.status == "closed":
        position.realized_pnl_pct = position.realized_pnl_usd / position.invested_usd * 100 if position.invested_usd else 0


async def dispatch_pending_trade_notifications(db=None) -> int:
    """Send queued paper-trade events and persist delivery status for safe retries."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return 0
    owned_session = db is None
    if owned_session:
        db = SessionLocal()
    sent = 0
    try:
        retry_before = datetime.now(timezone.utc) - timedelta(minutes=5)
        pending = db.scalars(select(PaperTradeEvent).where(
            PaperTradeEvent.telegram_sent_at.is_(None),
            or_(PaperTradeEvent.telegram_attempted_at.is_(None), PaperTradeEvent.telegram_attempted_at < retry_before)
        ).order_by(PaperTradeEvent.created_at.asc()).limit(25)).all()
        for event in pending:
            event.telegram_attempted_at = datetime.now(timezone.utc)
            event.telegram_error = ""
            db.commit()
            try:
                await send_paper_trade(event, db)
            except Exception as exc:
                # HTTP exception strings may include Telegram's secret bot-token URL.
                event.telegram_error = type(exc).__name__
                db.commit()
                continue
            event.telegram_sent_at = datetime.now(timezone.utc)
            event.telegram_error = ""
            db.commit()
            sent += 1
        return sent
    finally:
        if owned_session:
            db.close()


async def run_cycle() -> int:
    async with _cycle_lock:
        return await _run_cycle()


async def _run_cycle() -> int:
    """Refresh open marks/exits, then open eligible paper positions."""
    global _last_cycle, _last_error
    if settings.data_mode.lower() == "mock":
        return 0
    probe = SessionLocal()
    try:
        current_account = ensure_account(probe)
        has_open_positions = probe.scalar(select(PaperPosition.id).where(PaperPosition.status == "open").limit(1)) is not None
        if current_account.mode != "paper" or (not current_account.enabled and not has_open_positions):
            await dispatch_pending_trade_notifications()
            _last_cycle = datetime.now(timezone.utc).isoformat()
            _last_error = None
            return 0
    finally:
        probe.close()
    market = await fetch_trending(limit=90)
    market_tokens = market.get("tokens", [])
    prices = {(item.get("chain_id") or "solana", item["mint"]): float(item.get("price_usd") or 0) for item in market_tokens if item.get("mint")}
    db = SessionLocal()
    opened = 0
    try:
        account = ensure_account(db)
        if account.mode != "paper":
            _last_cycle = datetime.now(timezone.utc).isoformat()
            _last_error = None
            return 0
        open_positions = db.scalars(select(PaperPosition).where(PaperPosition.status == "open")).all()
        missing_by_chain: dict[str, list[str]] = {}
        for row in open_positions:
            key = (row.chain_id or "solana", row.token_mint)
            if key not in prices:
                missing_by_chain.setdefault(key[0], []).append(row.token_mint)
        for chain_id, mints in missing_by_chain.items():
            extra = await fetch_pair_metrics(mints, chain=chain_id)
            prices.update({(chain_id, mint): float(item.get("price_usd") or 0) for mint, item in extra.items()})
        for position in open_positions:
            price = prices.get((position.chain_id or "solana", position.token_mint)) or position.current_price_usd
            manage_sniper_position(db, position, account, price, datetime.now(timezone.utc))
        db.flush()
        current_open = db.scalars(select(PaperPosition).where(PaperPosition.status == "open")).all()
        if not account.enabled:
            db.commit()
            await dispatch_pending_trade_notifications(db)
            _last_cycle = datetime.now(timezone.utc).isoformat()
            _last_error = None
            return 0
        held_mints = {(row.chain_id or "solana", row.token_mint) for row in current_open}
        open_count = len(current_open)
        candidates = sorted(market_tokens, key=lambda item: (float(item.get("age_hours") if item.get("age_hours") is not None else 999), -float(item.get("activity_score") or 0)))
        for item in candidates:
            if account.max_open_positions > 0 and open_count >= account.max_open_positions:
                break
            mint = item.get("mint")
            chain_id = str(item.get("chain_id") or "solana")
            identity = (chain_id, mint)
            if not mint or identity in held_mints:
                continue
            scored = analyze_sniper_candidate(item, account)
            if not scored:
                continue
            previously_traded = db.scalar(select(PaperPosition.id).where(
                PaperPosition.token_mint == mint, PaperPosition.chain_id == chain_id
            ).limit(1))
            if previously_traded:
                continue
            size = min(account.position_size_usd, account.cash_balance_usd)
            price = float(item.get("price_usd") or 0)
            if size <= 0 or price <= 0:
                continue
            fee = max(0, account.fee_bps) / 10_000
            quantity = size * (1 - fee) / price
            score, reason = scored
            _upsert_token(db, item)
            db.flush()
            position = PaperPosition(token_mint=mint, chain_id=chain_id, symbol=str(item.get("symbol") or "???")[:24],
                status="open", entry_price_usd=price, current_price_usd=price,
                quantity=quantity, initial_quantity=quantity, invested_usd=size, remaining_cost_usd=size,
                current_value_usd=quantity * price * (1 - fee), peak_price_usd=price,
                analysis_score=score, analysis_reason=reason, opened_at=datetime.now(timezone.utc))
            db.add(position)
            account.cash_balance_usd -= size
            opened += 1
            held_mints.add(identity)
            open_count += 1
            db.flush()
            _record_event(db, position, "buy", "sniper_entry", price, quantity, -size, 0)
        db.commit()
        await dispatch_pending_trade_notifications(db)
        _last_cycle = datetime.now(timezone.utc).isoformat()
        _last_error = None
        return opened
    except Exception as exc:
        db.rollback()
        _last_error = type(exc).__name__
        raise
    finally:
        db.close()


async def paper_bot_loop():
    while True:
        try:
            await run_cycle()
        except Exception:
            # Provider failures are kept in status without exposing request URLs/keys.
            pass
        await asyncio.sleep(max(15, settings.paper_bot_interval_seconds))
