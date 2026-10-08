import httpx
from app.config import settings
from app.models import BotAccount, PaperPosition, PaperTradeEvent, Signal, Token
from sqlalchemy import select


def _chain_label(chain_id: str) -> str:
    return {"solana": "Solana", "bsc": "BNB Chain", "robinhood": "Robinhood Chain"}.get(chain_id, chain_id)


def _money(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


async def send_text(text: str) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in backend/.env")
    url = f"{settings.telegram_api_base}/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, json={"chat_id": settings.telegram_chat_id,
            "text": text[:4000], "disable_web_page_preview": True})
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError("Telegram rejected the message")


def _position_pnl(position: PaperPosition) -> float:
    if position.status == "closed":
        return position.realized_pnl_usd
    return position.realized_pnl_usd + position.current_value_usd - position.remaining_cost_usd


def _trade_message(db, event: PaperTradeEvent) -> str:
    position = db.get(PaperPosition, event.position_id)
    if position is None:
        return f"🟡 PAPER TRADE\n{event.side.upper()} ${event.symbol} · {_chain_label(event.chain_id)}\nReason: {event.reason.replace('_', ' ')}"
    pnl = _position_pnl(position)
    title = "🟢 PAPER ENTRY" if event.side == "buy" else "🟠 PAPER EXIT"
    lines = [title, f"${event.symbol} · {_chain_label(event.chain_id)}",
        f"Price: ${event.price_usd:.10g}",
        f"Size: {event.quantity:.8g} tokens · {'spent' if event.side == 'buy' else 'sold'} {_money(abs(event.proceeds_usd))}",
        f"Reason: {event.reason.replace('_', ' ')}",
        f"Position P&L: {_money(pnl)} ({position.realized_pnl_pct:+.1f}%)",
        f"Position: {'OPEN' if position.status == 'open' else 'CLOSED'} · remaining ${position.current_value_usd:,.2f}"]
    if event.side == "sell":
        lines.append(f"This exit realized: {_money(event.realized_pnl_usd)}")
    if position.status == "closed":
        closed = db.scalars(select(PaperPosition).where(PaperPosition.status == "closed").order_by(PaperPosition.realized_pnl_usd.desc())).all()
        account = db.get(BotAccount, 1)
        wins = sum(1 for row in closed if row.realized_pnl_usd > 0)
        win_rate = wins / len(closed) * 100 if closed else 0
        total_pnl = (account.cash_balance_usd + sum(row.current_value_usd for row in db.scalars(select(PaperPosition).where(PaperPosition.status == "open")).all()) - account.starting_balance_usd) if account else 0
        best = closed[0] if closed else None
        worst = closed[-1] if closed else None
        lines.extend([f"Account P&L: {_money(total_pnl)} · win rate {win_rate:.1f}% ({len(closed)} closed)"])
        if best and worst:
            lines.append(f"Best: ${best.symbol} {_money(best.realized_pnl_usd)} · worst: ${worst.symbol} {_money(worst.realized_pnl_usd)}")
    return "\n".join(lines)


async def send_signal(signal: Signal, token: Token) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable delivery")
    text = (f"📡 Smart Money Radar | {signal.kind.replace('_', ' ').title()}\n"
            f"${token.symbol}: {signal.title}\n"
            f"Wallets: {signal.wallet_count} · Confidence: {signal.confidence:.0f}%\n"
            f"Mint: {token.mint}")
    await send_text(text)


async def send_paper_trade(event: PaperTradeEvent, db) -> None:
    await send_text(_trade_message(db, event))


async def send_test_message() -> None:
    await send_text("✅ Smart Money Radar Telegram connection is working. Paper sniper trade alerts are enabled.")
