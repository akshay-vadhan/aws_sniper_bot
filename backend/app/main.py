import asyncio
import base64
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import settings
from app.database import Base, engine, get_db
from app.models import BotAccount, KOLProfile, PaperPosition, PaperTradeEvent, PriceSample, Signal, TelegramDelivery, Token, Trade, Wallet
from app.sample_data import seed_demo
from app.signal_lanes import backtest_return, detect_boost, detect_graduate, detect_hatchery
from app.telegram import send_signal, send_test_message
from app.ml_scoring import LogisticWalletModel, train_logistic
from app.dexscreener import fetch_pair_metrics, fetch_solana_trending, fetch_trending
from app.wallet_ingestion import _last_error, ingest_tracked_wallets, poll_loop
from app.solana_providers import configured_providers, provider_status, solana_rpc
from app.paper_trading import ensure_account, paper_bot_loop, run_cycle as run_paper_cycle, runtime_status

Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(_app):
    worker = None
    paper_worker = None
    if settings.data_mode.lower() != "mock" and configured_providers():
        worker = asyncio.create_task(poll_loop())
    if settings.data_mode.lower() != "mock":
        paper_worker = asyncio.create_task(paper_bot_loop())
    yield
    for task in (worker, paper_worker):
        if not task:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Solana Smart Money Radar API", version="0.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in settings.cors_origins.split(",")],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


class ConfigUpdate(BaseModel):
    cluster_min_wallets: int = Field(ge=2, le=20)
    cluster_window_minutes: int = Field(ge=1, le=120)
    smart_money_min_score: int = Field(ge=0, le=100)


class BoostInput(BaseModel):
    token_mint: str | None = None
    paid_placements: int = Field(ge=0)
    unique_buyers: int = Field(ge=0)
    volume_usd: float = Field(ge=0)
    baseline_volume_usd: float = Field(ge=0)
    liquidity_usd: float = Field(ge=0)


class GraduateInput(BaseModel):
    token_mint: str | None = None
    curve_complete: bool
    pool_address: str | None = None
    liquidity_usd: float = Field(ge=0)
    volume_5m_usd: float = Field(ge=0)


class HatcheryInput(BaseModel):
    token_mint: str | None = None
    minted_at: datetime
    mint_authority_revoked: bool
    freeze_authority_revoked: bool
    liquidity_usd: float = Field(ge=0)


class BacktestInput(BaseModel):
    entry_price: float = Field(gt=0)
    observed_prices: list[float] = Field(default_factory=list)


class LabeledWalletExample(BaseModel):
    win_rate: float = Field(ge=0, le=1)
    trade_count: int = Field(ge=0)
    realized_pnl_pct: float
    profitable: bool


class ModelTrainInput(BaseModel):
    examples: list[LabeledWalletExample] = Field(min_length=20, max_length=10000)


class KOLProfileInput(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    source: str = Field(default="manual", min_length=1, max_length=80)
    verified: bool = False


class WalletInput(BaseModel):
    address: str = Field(min_length=32, max_length=44)
    label: str = Field(default="", max_length=120)


class TradingConfigUpdate(BaseModel):
    position_size_usd: float = Field(gt=0, le=10000)
    target_multiple: float = Field(ge=1.1, le=20)
    stop_loss_pct: float = Field(gt=0, le=90)
    min_liquidity_usd: float = Field(ge=0, le=100_000_000)
    min_volume_24h_usd: float = Field(ge=0, le=1_000_000_000)
    min_activity_score: float = Field(ge=0, le=100)
    max_open_positions: int = Field(ge=0, le=500)
    fee_bps: int = Field(ge=0, le=500)


runtime_config = {"cluster_min_wallets": settings.cluster_min_wallets,
                  "cluster_window_minutes": settings.cluster_window_minutes,
                  "smart_money_min_score": settings.smart_money_min_score}
wallet_model: LogisticWalletModel | None = None


def ensure_seed(db: Session):
    if settings.data_mode.lower() == "mock":
        seed_demo(db, min_wallets=runtime_config["cluster_min_wallets"],
                  window_minutes=runtime_config["cluster_window_minutes"],
                  min_score=runtime_config["smart_money_min_score"])


def wallet_dict(wallet: Wallet):
    return {"address": wallet.address, "label": wallet.label, "score": wallet.score,
            "realized_pnl_pct": wallet.realized_pnl_pct, "win_rate": wallet.win_rate,
            "trades_count": wallet.trades_count, "tracked": wallet.tracked}


def datetime_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def token_dict(token: Token):
    return {"mint": token.mint, "symbol": token.symbol, "name": token.name,
            "liquidity_usd": token.liquidity_usd, "market_cap_usd": token.market_cap_usd,
            "first_seen_at": datetime_utc_iso(token.first_seen_at),
            "age_hours": max(0, int((datetime.now(timezone.utc) - (token.first_seen_at.replace(tzinfo=timezone.utc) if token.first_seen_at.tzinfo is None else token.first_seen_at.astimezone(timezone.utc))).total_seconds() // 3600))}


def _paper_position_dict(position: PaperPosition):
    return {"id": position.id, "token_mint": position.token_mint, "symbol": position.symbol,
        "status": position.status, "entry_price_usd": position.entry_price_usd,
        "current_price_usd": position.current_price_usd, "exit_price_usd": position.exit_price_usd,
        "quantity": position.quantity, "invested_usd": position.invested_usd,
        "initial_quantity": position.initial_quantity, "remaining_cost_usd": position.remaining_cost_usd,
        "chain_id": position.chain_id, "peak_price_usd": position.peak_price_usd,
        "reached_2x": position.reached_2x,
        "scale_outs": {"2x": position.sold_2x, "3x": position.sold_3x,
                       "5x": position.sold_5x, "10x": position.sold_10x},
        "current_value_usd": position.current_value_usd,
        "unrealized_pnl_usd": (position.current_value_usd - position.remaining_cost_usd) if position.status == "open" else 0,
        "total_pnl_usd": position.realized_pnl_usd + position.current_value_usd - position.remaining_cost_usd if position.status == "open" else position.realized_pnl_usd,
        "realized_pnl_usd": position.realized_pnl_usd, "realized_pnl_pct": position.realized_pnl_pct,
        "analysis_score": position.analysis_score, "analysis_reason": position.analysis_reason,
        "exit_reason": position.exit_reason, "opened_at": datetime_utc_iso(position.opened_at),
        "closed_at": datetime_utc_iso(position.closed_at) if position.closed_at else None}


def _paper_status(db: Session):
    account = ensure_account(db)
    open_positions = db.scalars(select(PaperPosition).where(PaperPosition.status == "open")).all()
    recent_positions = db.scalars(select(PaperPosition).order_by(PaperPosition.opened_at.desc()).limit(20)).all()
    unrealized = sum(row.current_value_usd - row.remaining_cost_usd for row in open_positions)
    realized = db.query(PaperPosition).with_entities(PaperPosition.realized_pnl_usd).all()
    realized_total = sum(row[0] for row in realized)
    closed_positions = db.scalars(select(PaperPosition).where(PaperPosition.status == "closed")).all()
    win_rate = (sum(1 for row in closed_positions if row.realized_pnl_usd > 0) / len(closed_positions) * 100) if closed_positions else 0
    events = db.scalars(select(PaperTradeEvent).order_by(PaperTradeEvent.created_at.desc()).limit(30)).all()
    market_value = sum(row.current_value_usd for row in open_positions)
    equity = account.cash_balance_usd + market_value
    return {"mode": account.mode, "enabled": account.enabled,
        "starting_balance_usd": account.starting_balance_usd, "cash_balance_usd": account.cash_balance_usd,
        "equity_usd": equity, "realized_pnl_usd": realized_total, "unrealized_pnl_usd": unrealized,
        "total_pnl_usd": equity - account.starting_balance_usd,
        "win_rate_pct": round(win_rate, 1), "completed_trades": len(closed_positions),
        "telegram_configured": bool(settings.telegram_bot_token and settings.telegram_chat_id),
        "telegram_pending_count": db.query(PaperTradeEvent).filter(PaperTradeEvent.telegram_sent_at.is_(None)).count(),
        "open_positions_count": len(open_positions),
        "config": {"position_size_usd": account.position_size_usd, "target_multiple": 2,
            "stop_loss_pct": 60, "min_liquidity_usd": account.min_liquidity_usd,
            "min_volume_24h_usd": account.min_volume_24h_usd,
            "min_activity_score": account.min_activity_score,
            "max_open_positions": account.max_open_positions, "fee_bps": account.fee_bps},
        "positions": [_paper_position_dict(row) for row in recent_positions],
        "recent_trades": [{"id": row.id, "position_id": row.position_id, "chain_id": row.chain_id,
            "token_mint": row.token_mint, "symbol": row.symbol, "side": row.side,
            "reason": row.reason, "price_usd": row.price_usd, "quantity": row.quantity,
            "proceeds_usd": row.proceeds_usd, "realized_pnl_usd": row.realized_pnl_usd,
            "telegram_sent": row.telegram_sent_at is not None, "telegram_error": row.telegram_error,
            "created_at": datetime_utc_iso(row.created_at)} for row in events], **runtime_status()}


def signal_dict(signal: Signal, token: Token | None = None):
    return {"id": signal.id, "kind": signal.kind, "token_mint": signal.token_mint,
            "symbol": token.symbol if token else signal.token_mint[:8], "title": signal.title,
            "wallet_count": signal.wallet_count, "confidence": signal.confidence,
            "created_at": datetime_utc_iso(signal.created_at), "wallet_addresses": signal.wallet_addresses.split(",") if signal.wallet_addresses else []}


def record_lane_signal(db: Session, token_mint: str | None, result) -> int | None:
    if not result.detected or not token_mint:
        return None
    token = db.get(Token, token_mint)
    if token is None:
        raise HTTPException(status_code=404, detail="Token mint is not in the token table")
    now = datetime.now(timezone.utc)
    duplicate = db.scalar(select(Signal).where(Signal.kind == result.kind,
        Signal.token_mint == token_mint, Signal.created_at >= now - timedelta(minutes=10))
        .order_by(Signal.created_at.desc()).limit(1))
    if duplicate:
        duplicate.title = f"${token.symbol}: {result.reason}"
        duplicate.confidence = result.confidence
        duplicate.created_at = now
        db.commit()
        return duplicate.id
    signal = Signal(kind=result.kind, token_mint=token_mint,
                    title=f"${token.symbol}: {result.reason}", wallet_count=0,
                    confidence=result.confidence, created_at=now)
    db.add(signal)
    db.commit()
    return signal.id


@app.get("/api/health")
def health():
    return {"status": "ok", "mode": settings.data_mode, "time": datetime.now(timezone.utc)}


@app.get("/api/dashboard")
async def dashboard(db: Session = Depends(get_db)):
    live_mode = settings.data_mode.lower() != "mock"
    market = None
    market_error = None
    if not live_mode:
        ensure_seed(db)
    else:
        try:
            market = await fetch_trending(limit=30)
        except RuntimeError as exc:
            market_error = str(exc)
    day_ago = datetime.now(timezone.utc) - timedelta(hours=24)
    signals_query = select(Signal).where(Signal.created_at >= day_ago)
    wallets_query = select(Wallet).where(Wallet.tracked.is_(True))
    if live_mode:
        # Hide old fixture rows if this database was previously run in sample mode.
        signals_query = signals_query.where(~Signal.token_mint.like("DemoMint%"))
        wallets_query = wallets_query.where(~Wallet.address.like("DemoWhale%"))
    recent_query = db.query(Signal).filter(Signal.created_at >= day_ago)
    if live_mode:
        recent_query = recent_query.filter(~Signal.token_mint.like("DemoMint%"))
    recent_signal_count = recent_query.count()
    signals = db.scalars(signals_query.order_by(Signal.created_at.desc()).limit(12)).all()
    wallets = db.scalars(wallets_query.order_by(Wallet.score.desc()).limit(10)).all()
    if live_mode:
        tokens = market["tokens"] if market else []
        token_map = {}
    else:
        db_tokens = db.scalars(select(Token).order_by(Token.liquidity_usd.desc()).limit(12)).all()
        tokens = [token_dict(token) for token in db_tokens]
        token_map = {token.mint: token for token in db.scalars(select(Token)).all()}
    real_trades = db.query(Trade).join(Wallet).filter(~Wallet.address.like("DemoWhale%")).count() if live_mode else db.query(Trade).count()
    tracked_wallets = db.query(Wallet).filter(Wallet.tracked.is_(True))
    if live_mode:
        tracked_wallets = tracked_wallets.filter(~Wallet.address.like("DemoWhale%"))
    return {"summary": {"tracked_wallets": tracked_wallets.count(),
                         "signals_24h": recent_signal_count, "tokens_watched": len(tokens) if live_mode else db.query(Token).count(),
                         "trades_ingested": real_trades},
            "signals": [signal_dict(item, token_map.get(item.token_mint)) for item in signals],
            "wallets": [wallet_dict(item) for item in wallets], "tokens": tokens,
            "config": runtime_config, "mode": settings.data_mode,
            "source": market["source"] if market else ("DEX Screener" if live_mode else "Sample data"),
            "market_updated_at": market["updated_at"] if market else None,
            "market_stale": market["stale"] if market else False,
            "market_error": market_error,
            "telegram_configured": bool(settings.telegram_bot_token and settings.telegram_chat_id)}


@app.get("/api/market/trending")
async def market_trending(limit: int = Query(30, ge=1, le=90), force: bool = False,
                          chain: str = Query("all", pattern="^(all|solana|bsc|robinhood)$"),
                          db: Session = Depends(get_db)):
    if settings.data_mode.lower() == "mock":
        ensure_seed(db)
        tokens = db.scalars(select(Token).order_by(Token.liquidity_usd.desc()).limit(limit)).all()
        return {"source": "Sample data", "stale": False, "tokens": [token_dict(token) for token in tokens]}
    try:
        return await fetch_trending(limit, force=force, chain=chain)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/wallets/kols")
def list_kols(db: Session = Depends(get_db)):
    ensure_seed(db)
    rows = db.scalars(select(KOLProfile).order_by(KOLProfile.verified.desc(), KOLProfile.display_name)).all()
    wallets = {wallet.address: wallet for wallet in db.scalars(select(Wallet)).all()}
    return [{"wallet_address": row.wallet_address, "display_name": row.display_name,
             "source": row.source, "verified": row.verified,
             "score": wallets[row.wallet_address].score if row.wallet_address in wallets else None} for row in rows]


@app.put("/api/wallets/{address}/kol")
def label_kol(address: str, payload: KOLProfileInput, db: Session = Depends(get_db)):
    wallet = db.get(Wallet, address)
    if wallet is None:
        raise HTTPException(status_code=404, detail="Tracked wallet not found")
    profile = db.get(KOLProfile, address)
    if profile is None:
        profile = KOLProfile(wallet_address=address)
        db.add(profile)
    profile.display_name = payload.display_name
    profile.source = payload.source
    profile.verified = payload.verified
    db.commit()
    return {"wallet_address": address, "display_name": profile.display_name,
            "source": profile.source, "verified": profile.verified}


@app.get("/api/alerts/status")
def alert_status():
    return {"provider": "telegram", "configured": bool(settings.telegram_bot_token and settings.telegram_chat_id)}


@app.post("/api/alerts/dispatch")
async def dispatch_alerts(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise HTTPException(status_code=409, detail="Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first")
    already_sent = select(TelegramDelivery.signal_id).where(TelegramDelivery.status == "sent")
    signals = db.scalars(select(Signal).where(Signal.id.not_in(already_sent)).order_by(Signal.created_at.desc()).limit(limit)).all()
    results = []
    for signal in signals:
        delivery = db.scalar(select(TelegramDelivery).where(TelegramDelivery.signal_id == signal.id))
        if delivery is None:
            delivery = TelegramDelivery(signal_id=signal.id, status="pending")
            db.add(delivery)
        delivery.status = "pending"
        delivery.error = ""
        delivery.attempted_at = datetime.now(timezone.utc)
        try:
            token = db.get(Token, signal.token_mint)
            if token is None:
                raise RuntimeError("Signal token was not found")
            await send_signal(signal, token)
            delivery.status = "sent"
            results.append({"signal_id": signal.id, "status": "sent"})
        except Exception as exc:
            delivery.status = "failed"
            delivery.error = str(exc)[:500]
            results.append({"signal_id": signal.id, "status": "failed", "error": delivery.error})
        db.commit()
    return {"attempted": len(results), "results": results}


@app.post("/api/detect/boost")
def boost_radar(payload: BoostInput, db: Session = Depends(get_db)):
    values = payload.model_dump(exclude={"token_mint"})
    result = detect_boost(**values)
    return {**result.__dict__, "signal_id": record_lane_signal(db, payload.token_mint, result)}


@app.post("/api/detect/graduate")
def graduate_radar(payload: GraduateInput, db: Session = Depends(get_db)):
    result = detect_graduate(**payload.model_dump(exclude={"token_mint"}))
    return {**result.__dict__, "signal_id": record_lane_signal(db, payload.token_mint, result)}


@app.post("/api/detect/hatchery")
def hatchery_radar(payload: HatcheryInput, db: Session = Depends(get_db)):
    result = detect_hatchery(**payload.model_dump(exclude={"token_mint"}))
    return {**result.__dict__, "signal_id": record_lane_signal(db, payload.token_mint, result)}


@app.post("/api/backtest/evaluate")
def evaluate_backtest(payload: BacktestInput):
    return backtest_return(payload.entry_price, payload.observed_prices)


@app.get("/api/backtest")
def historical_backtest(horizon_hours: int = Query(1, ge=1, le=168),
                        limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    """Evaluate completed signal horizons where a price feed has supplied snapshots."""
    now = datetime.now(timezone.utc)
    signals = db.scalars(select(Signal).order_by(Signal.created_at.desc()).limit(limit)).all()
    rows = []
    pending = 0
    for signal in signals:
        signal_at = signal.created_at
        if signal_at.tzinfo is None:
            signal_at = signal_at.replace(tzinfo=timezone.utc)
        horizon_end = signal_at.timestamp() + horizon_hours * 3600
        if now.timestamp() < horizon_end:
            pending += 1
            continue
        samples = db.scalars(select(PriceSample).where(
            PriceSample.token_mint == signal.token_mint,
            PriceSample.sampled_at >= signal.created_at,
            PriceSample.sampled_at <= datetime.fromtimestamp(horizon_end, timezone.utc)
        ).order_by(PriceSample.sampled_at)).all()
        if len(samples) < 2:
            continue
        stats = backtest_return(samples[0].price_usd, [sample.price_usd for sample in samples[1:]])
        rows.append({"signal_id": signal.id, "kind": signal.kind, "token_mint": signal.token_mint,
                     "created_at": signal.created_at, "horizon_hours": horizon_hours, **stats})
    return {"horizon_hours": horizon_hours, "evaluated": len(rows), "pending": pending,
            "results": rows, "note": "Only signals with stored price snapshots covering the full horizon are scored."}


@app.get("/api/ml/status")
def model_status():
    return {"trained": wallet_model is not None,
            "examples": wallet_model.examples if wallet_model else 0,
            "features": ["win_rate", "trade_count", "realized_pnl_pct"],
            "note": "Train with time-safe, labeled historical outcomes before using probabilities for decisions."}


@app.post("/api/ml/train")
def train_wallet_model(payload: ModelTrainInput):
    global wallet_model
    examples = [{"features": {"win_rate": row.win_rate, "trade_count": row.trade_count,
                               "realized_pnl_pct": row.realized_pnl_pct},
                 "profitable": row.profitable} for row in payload.examples]
    try:
        wallet_model = train_logistic(examples)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return model_status()


@app.post("/api/ml/score")
def score_wallet_model(payload: LabeledWalletExample):
    if wallet_model is None:
        raise HTTPException(status_code=409, detail="Train the model with labeled historical outcomes first")
    probability = wallet_model.predict({"win_rate": payload.win_rate, "trade_count": payload.trade_count,
                                        "realized_pnl_pct": payload.realized_pnl_pct})
    return {"profitability_probability": round(probability, 4), "training_examples": wallet_model.examples,
            "model_type": "logistic_regression_baseline"}


@app.get("/api/signals")
def list_signals(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    ensure_seed(db)
    query = select(Signal).order_by(Signal.created_at.desc())
    if settings.data_mode.lower() != "mock":
        query = query.where(~Signal.token_mint.like("DemoMint%"))
    rows = db.scalars(query.limit(limit)).all()
    token_map = {token.mint: token for token in db.scalars(select(Token)).all()}
    return [signal_dict(row, token_map.get(row.token_mint)) for row in rows]


@app.get("/api/wallets")
def list_wallets(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    ensure_seed(db)
    query = select(Wallet).where(Wallet.tracked.is_(True)).order_by(Wallet.score.desc())
    if settings.data_mode.lower() != "mock":
        query = query.where(~Wallet.address.like("DemoWhale%"))
    return [wallet_dict(row) for row in db.scalars(query.limit(limit)).all()]


@app.post("/api/wallets", status_code=201)
async def add_wallet(payload: WalletInput, db: Session = Depends(get_db)):
    if settings.data_mode.lower() != "mock" and not configured_providers():
        raise HTTPException(status_code=503, detail="Set HELIUS_API_KEY or ALCHEMY_API_KEY in backend/.env before tracking live wallets")
    address = payload.address.strip()
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address):
        raise HTTPException(status_code=422, detail="Enter a valid Solana wallet address")
    wallet = db.get(Wallet, address)
    if wallet:
        wallet.tracked = True
        if payload.label.strip():
            wallet.label = payload.label.strip()
    else:
        wallet = Wallet(address=address, label=payload.label.strip() or address[:6] + "…" + address[-4:], tracked=True)
        db.add(wallet)
    db.commit()
    if settings.data_mode.lower() != "mock" and configured_providers():
        try:
            await ingest_tracked_wallets()
        except Exception:
            pass
    return wallet_dict(wallet)


@app.delete("/api/wallets/{address}")
def untrack_wallet(address: str, db: Session = Depends(get_db)):
    wallet = db.get(Wallet, address)
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    wallet.tracked = False
    db.commit()
    return {"status": "untracked", "address": address}


@app.get("/api/ingestion/status")
def ingestion_status(db: Session = Depends(get_db)):
    tracked = db.query(Wallet).filter(Wallet.tracked.is_(True), ~Wallet.address.like("DemoWhale%")).count()
    return {"provider": "Helius Enhanced (primary), Alchemy RPC (fallback)", **provider_status(),
            "tracked_wallets": tracked, "poll_seconds": max(15, settings.wallet_poll_seconds),
            "errors": dict(_last_error)}


@app.get("/api/trading/status")
def trading_status(db: Session = Depends(get_db)):
    return _paper_status(db)


@app.get("/api/trading/positions")
def trading_positions(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    rows = db.scalars(select(PaperPosition).order_by(PaperPosition.opened_at.desc()).limit(limit)).all()
    return [_paper_position_dict(row) for row in rows]


@app.put("/api/trading/config")
def update_trading_config(payload: TradingConfigUpdate, db: Session = Depends(get_db)):
    account = ensure_account(db)
    if account.enabled:
        raise HTTPException(status_code=409, detail="Stop new paper entries before changing strategy settings")
    for key, value in payload.model_dump().items():
        setattr(account, key, value)
    db.commit()
    return _paper_status(db)


@app.post("/api/trading/start")
def start_paper_bot(db: Session = Depends(get_db)):
    if settings.data_mode.lower() == "mock":
        raise HTTPException(status_code=409, detail="Set DATA_MODE=live to use real market data for paper trading")
    account = ensure_account(db)
    account.mode = "paper"
    account.enabled = True
    db.commit()
    return {"status": "started", **_paper_status(db)}


@app.post("/api/trading/stop")
def stop_paper_bot(db: Session = Depends(get_db)):
    account = ensure_account(db)
    account.enabled = False
    db.commit()
    return {"status": "stopped_new_entries", "open_positions_are_still_managed": True, **_paper_status(db)}


@app.post("/api/trading/run-once")
async def run_paper_bot_once(db: Session = Depends(get_db)):
    account = ensure_account(db)
    if not account.enabled:
        raise HTTPException(status_code=409, detail="Start paper trading before running a cycle")
    try:
        opened = await run_paper_cycle()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"opened_positions": opened, **_paper_status(db)}


@app.post("/api/trading/telegram/test")
async def test_telegram_trade_alerts():
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise HTTPException(status_code=503, detail="Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in backend/.env, then restart the backend")
    try:
        await send_test_message()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Telegram test failed ({type(exc).__name__}); check bot token, chat ID, and bot permissions") from exc
    return {"status": "sent"}


async def _helius_rpc(method: str, params: list):
    try:
        return await solana_rpc(method, params)
    except RuntimeError as exc:
        if not configured_providers():
            raise HTTPException(status_code=503, detail="Set HELIUS_API_KEY or ALCHEMY_API_KEY in backend/.env to inspect on-chain token data") from exc
        raise HTTPException(status_code=502, detail="Solana RPC providers could not fetch token data") from exc


@app.get("/api/tokens/{mint}/safety")
async def token_safety(mint: str):
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", mint):
        raise HTTPException(status_code=422, detail="Invalid Solana token mint")
    try:
        account, supply, largest, pair = await asyncio.gather(
            _helius_rpc("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}]),
            _helius_rpc("getTokenSupply", [mint, {"commitment": "confirmed"}]),
            _helius_rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}]),
            fetch_pair_metrics([mint]))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not fetch token data from Solana RPC provider") from exc
    value = (account or {}).get("value")
    if not value:
        raise HTTPException(status_code=404, detail="Mint account was not found on Solana")
    parsed_info = (((value.get("data") or {}).get("parsed") or {}).get("info") or {})
    mint_authority = parsed_info.get("mintAuthority")
    freeze_authority = parsed_info.get("freezeAuthority")
    if not parsed_info:
        raw_data = value.get("data")
        try:
            mint_bytes = base64.b64decode(raw_data[0]) if isinstance(raw_data, list) else b""
        except (ValueError, TypeError):
            mint_bytes = b""
        if len(mint_bytes) < 82:
            raise HTTPException(status_code=502, detail="Solana RPC provider could not parse the token mint account")
        mint_authority = "active" if int.from_bytes(mint_bytes[0:4], "little") == 1 else None
        freeze_authority = "active" if int.from_bytes(mint_bytes[46:50], "little") == 1 else None
    extensions = parsed_info.get("extensions") or []
    extension_names = [str(item.get("extension") or item.get("type") or "unknown")
                       for item in extensions if isinstance(item, dict)]
    flags = []
    if mint_authority:
        flags.append({"severity": "high", "label": "Mint authority is active", "detail": "The authority can create additional tokens."})
    if freeze_authority:
        flags.append({"severity": "medium", "label": "Freeze authority is active", "detail": "The authority can freeze token accounts."})
    ext_lower = " ".join(extension_names).lower()
    if value.get("owner") == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb" and not extension_names:
        flags.append({"severity": "info", "label": "Token-2022 extensions not parsed", "detail": "The RPC response did not expose extension details; check them in a Token-2022-aware explorer."})
    if "permanentdelegate" in ext_lower or "permanent_delegate" in ext_lower:
        flags.append({"severity": "high", "label": "Permanent delegate extension", "detail": "A delegate may control token accounts under this extension."})
    if "pausable" in ext_lower:
        flags.append({"severity": "medium", "label": "Pausable token extension", "detail": "Transfers may be paused by the configured authority."})
    if "defaultaccountstate" in ext_lower or "default_account_state" in ext_lower:
        flags.append({"severity": "medium", "label": "Default account-state extension", "detail": "New accounts may have restricted default state."})
    if "transferfee" in ext_lower or "transfer_fee" in ext_lower:
        flags.append({"severity": "low", "label": "Transfer fee extension", "detail": "Transfers may incur a configured token fee."})
    try:
        total = float(((supply or {}).get("value") or {}).get("uiAmountString") or 0)
    except (TypeError, ValueError):
        total = 0.0
    largest_rows = ((largest or {}).get("value") or [])
    top10 = 0.0
    for item in largest_rows:
        try:
            top10 += float(item.get("uiAmountString") or 0)
        except (TypeError, ValueError):
            continue
    if total > 0 and largest_rows:
        flags.append({"severity": "medium" if top10 / total >= 0.5 else "info",
            "label": "Top 20 token accounts concentration", "detail": f"Largest accounts hold {top10 / total * 100:.1f}% of reported supply; token accounts may share an owner."})
    if pair.get(mint, {}).get("liquidity_usd", 0) < 10_000:
        flags.append({"severity": "medium", "label": "Low or unavailable DEX liquidity", "detail": "Reported liquidity is below $10,000 or no matching pair was found."})
    return {"mint": mint, "program": value.get("owner"), "mint_authority_active": bool(mint_authority),
            "freeze_authority_active": bool(freeze_authority), "mint_authority": mint_authority,
            "freeze_authority": freeze_authority, "token_extensions": extension_names or None,
            "supply": total, "top_accounts_share_pct": round(top10 / total * 100, 2) if total else None,
            "liquidity_usd": pair.get(mint, {}).get("liquidity_usd"), "flags": flags,
            "note": "Risk checks are indicators, not an audit or guarantee of safety."}


@app.get("/api/tokens")
async def list_tokens(limit: int = Query(30, ge=1, le=90), chain: str = Query("all", pattern="^(all|solana|bsc|robinhood)$"), db: Session = Depends(get_db)):
    if settings.data_mode.lower() != "mock":
        try:
            return (await fetch_trending(limit, chain=chain))["tokens"]
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    ensure_seed(db)
    return [token_dict(row) for row in db.scalars(select(Token).order_by(Token.liquidity_usd.desc()).limit(limit)).all()]


@app.get("/api/config")
def get_config():
    return runtime_config


@app.put("/api/config")
def update_config(payload: ConfigUpdate):
    runtime_config.update(payload.model_dump())
    return runtime_config


@app.post("/api/demo/refresh")
async def refresh_demo(db: Session = Depends(get_db)):
    if settings.data_mode.lower() != "mock":
        try:
            market = await fetch_solana_trending(limit=30, force=True)
            return {"status": "refreshed", "mode": "live", **market}
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    seed_demo(db, refresh=True, min_wallets=runtime_config["cluster_min_wallets"],
              window_minutes=runtime_config["cluster_window_minutes"],
              min_score=runtime_config["smart_money_min_score"])
    return {"status": "refreshed"}
