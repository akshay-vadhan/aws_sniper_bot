from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class Wallet(Base):
    __tablename__ = "wallets"
    address: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    tracked: Mapped[bool] = mapped_column(Boolean, default=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    realized_pnl_pct: Mapped[float] = mapped_column(Float, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0)
    trades_count: Mapped[int] = mapped_column(Integer, default=0)
    last_signature: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    trades: Mapped[list["Trade"]] = relationship(back_populates="wallet")


class Token(Base):
    __tablename__ = "tokens"
    mint: Mapped[str] = mapped_column(String(64), primary_key=True)
    chain_id: Mapped[str] = mapped_column(String(20), default="solana", index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    liquidity_usd: Mapped[float] = mapped_column(Float, default=0)
    market_cap_usd: Mapped[float] = mapped_column(Float, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    trades: Mapped[list["Trade"]] = relationship(back_populates="token")


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (UniqueConstraint("signature", "wallet_address", "token_mint", name="uq_trade_identity"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signature: Mapped[str] = mapped_column(String(100), index=True)
    wallet_address: Mapped[str] = mapped_column(ForeignKey("wallets.address"), index=True)
    token_mint: Mapped[str] = mapped_column(ForeignKey("tokens.mint"), index=True)
    side: Mapped[str] = mapped_column(String(8))
    amount_usd: Mapped[float] = mapped_column(Float, default=0)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    wallet: Mapped[Wallet] = relationship(back_populates="trades")
    token: Mapped[Token] = relationship(back_populates="trades")


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    token_mint: Mapped[str] = mapped_column(ForeignKey("tokens.mint"), index=True)
    title: Mapped[str] = mapped_column(String(180))
    wallet_count: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    wallet_addresses: Mapped[str] = mapped_column(Text, default="")


class KOLProfile(Base):
    __tablename__ = "kol_profiles"
    wallet_address: Mapped[str] = mapped_column(ForeignKey("wallets.address"), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    source: Mapped[str] = mapped_column(String(80), default="manual")
    verified: Mapped[bool] = mapped_column(Boolean, default=False)


class PriceSample(Base):
    __tablename__ = "price_samples"
    __table_args__ = (UniqueConstraint("token_mint", "sampled_at", name="uq_price_sample_token_time"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_mint: Mapped[str] = mapped_column(ForeignKey("tokens.mint"), index=True)
    price_usd: Mapped[float] = mapped_column(Float)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class TelegramDelivery(Base):
    __tablename__ = "telegram_deliveries"
    __table_args__ = (UniqueConstraint("signal_id", name="uq_telegram_signal_delivery"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error: Mapped[str] = mapped_column(Text, default="")
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BotAccount(Base):
    __tablename__ = "bot_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(String(12), default="paper")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    starting_balance_usd: Mapped[float] = mapped_column(Float, default=1000)
    cash_balance_usd: Mapped[float] = mapped_column(Float, default=1000)
    position_size_usd: Mapped[float] = mapped_column(Float, default=10)
    target_multiple: Mapped[float] = mapped_column(Float, default=2)
    stop_loss_pct: Mapped[float] = mapped_column(Float, default=25)
    min_liquidity_usd: Mapped[float] = mapped_column(Float, default=5000)
    min_volume_24h_usd: Mapped[float] = mapped_column(Float, default=0)
    min_activity_score: Mapped[float] = mapped_column(Float, default=10)
    # Zero disables the count cap; available paper cash still bounds entries.
    max_open_positions: Mapped[int] = mapped_column(Integer, default=0)
    fee_bps: Mapped[int] = mapped_column(Integer, default=100)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PaperPosition(Base):
    __tablename__ = "paper_positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_mint: Mapped[str] = mapped_column(ForeignKey("tokens.mint"), index=True)
    chain_id: Mapped[str] = mapped_column(String(20), default="solana", index=True)
    symbol: Mapped[str] = mapped_column(String(24), default="???")
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)
    entry_price_usd: Mapped[float] = mapped_column(Float)
    current_price_usd: Mapped[float] = mapped_column(Float)
    exit_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[float] = mapped_column(Float)
    initial_quantity: Mapped[float] = mapped_column(Float, default=0)
    invested_usd: Mapped[float] = mapped_column(Float)
    remaining_cost_usd: Mapped[float] = mapped_column(Float, default=0)
    current_value_usd: Mapped[float] = mapped_column(Float)
    peak_price_usd: Mapped[float] = mapped_column(Float, default=0)
    reached_2x: Mapped[bool] = mapped_column(Boolean, default=False)
    sold_2x: Mapped[bool] = mapped_column(Boolean, default=False)
    sold_3x: Mapped[bool] = mapped_column(Boolean, default=False)
    sold_5x: Mapped[bool] = mapped_column(Boolean, default=False)
    sold_10x: Mapped[bool] = mapped_column(Boolean, default=False)
    realized_pnl_usd: Mapped[float] = mapped_column(Float, default=0)
    realized_pnl_pct: Mapped[float] = mapped_column(Float, default=0)
    analysis_score: Mapped[float] = mapped_column(Float, default=0)
    analysis_reason: Mapped[str] = mapped_column(Text, default="")
    exit_reason: Mapped[str] = mapped_column(String(32), default="")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PaperTradeEvent(Base):
    __tablename__ = "paper_trade_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("paper_positions.id"), index=True)
    chain_id: Mapped[str] = mapped_column(String(20), default="solana", index=True)
    token_mint: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(24), default="???")
    side: Mapped[str] = mapped_column(String(8))
    reason: Mapped[str] = mapped_column(String(32))
    price_usd: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    proceeds_usd: Mapped[float] = mapped_column(Float, default=0)
    realized_pnl_usd: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    telegram_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    telegram_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    telegram_error: Mapped[str] = mapped_column(Text, default="")
