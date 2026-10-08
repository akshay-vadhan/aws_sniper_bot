from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import KOLProfile, Signal, Token, Trade, Wallet
from app.scoring import score_wallet
from app.detector import detect_clusters
from app.config import settings
from app.signal_lanes import detect_kol_buys


def seed_demo(db: Session, refresh: bool = False, min_wallets: int | None = None,
              window_minutes: int | None = None, min_score: int | None = None) -> None:
    if db.scalar(select(Wallet).limit(1)) and not refresh:
        if db.scalar(select(KOLProfile).limit(1)) is None:
            demo_kols = db.scalars(select(Wallet).where(Wallet.label.in_(["Bluefin", "Mica"]))).all()
            db.add_all([KOLProfile(wallet_address=wallet.address,
                                   display_name=f"{wallet.label} Calls", source="demo", verified=wallet.label == "Bluefin")
                        for wallet in demo_kols])
            db.flush()
        if db.scalar(select(Signal).where(Signal.kind == "kol_watch").limit(1)) is None:
            kol_addresses = {row.wallet_address for row in db.scalars(select(KOLProfile)).all()}
            trades = db.scalars(select(Trade)).all()
            tokens = {token.mint: token for token in db.scalars(select(Token)).all()}
            for activity in detect_kol_buys(trades, kol_addresses):
                mint, addresses = activity["token_mint"], activity["wallet_addresses"]
                db.add(Signal(kind="kol_watch", token_mint=mint,
                              title=f"KOL wallet activity detected in ${tokens[mint].symbol}",
                              wallet_count=len(addresses), confidence=65 if len(addresses) == 1 else 82,
                              created_at=datetime.now(timezone.utc), wallet_addresses=",".join(addresses)))
        db.commit()
        return
    now = datetime.now(timezone.utc)
    if refresh:
        db.query(Signal).delete()
        db.query(Trade).delete()
        db.query(KOLProfile).delete()
        db.query(Token).delete()
        db.query(Wallet).delete()
    wallets = [
        Wallet(address=f"DemoWhale{i:02d}111111111111111111111111111111111", label=label,
               tracked=True, realized_pnl_pct=pnl, win_rate=win, trades_count=count)
        for i, label, pnl, win, count in [
            (1, "Northstar", 148, .72, 18), (2, "Bluefin", 82, .64, 15),
            (3, "Orbit", 56, .61, 12), (4, "Quiet Tide", 22, .55, 9),
            (5, "Mica", -8, .42, 7), (6, "Kestrel", 105, .68, 14),
        ]
    ]
    for wallet in wallets:
        wallet.score = score_wallet(wallet.realized_pnl_pct, wallet.win_rate, wallet.trades_count)
    tokens = [
        Token(mint="DemoMintBONK11111111111111111111111111111", symbol="BONKX", name="Bonk X", liquidity_usd=184000, market_cap_usd=2250000, first_seen_at=now-timedelta(hours=4)),
        Token(mint="DemoMintWIF111111111111111111111111111111", symbol="WIFX", name="Dog Wif Hat X", liquidity_usd=92000, market_cap_usd=840000, first_seen_at=now-timedelta(hours=2)),
        Token(mint="DemoMintPEPE11111111111111111111111111111", symbol="PEPEX", name="Pepe Sol", liquidity_usd=38000, market_cap_usd=415000, first_seen_at=now-timedelta(minutes=36)),
    ]
    db.add_all(wallets + tokens)
    db.flush()
    db.add_all([KOLProfile(wallet_address=wallets[1].address, display_name="Bluefin Calls", source="demo", verified=True),
                KOLProfile(wallet_address=wallets[4].address, display_name="Mica Watch", source="demo", verified=False)])
    trades = []
    # Three distinct high-scoring wallets buy BONKX inside the configured cluster window.
    for i in range(3):
        trades.append(Trade(signature=f"demo-sig-bonk-{i}", wallet_address=wallets[i].address,
                            token_mint=tokens[0].mint, side="buy", amount_usd=1250 + i*400,
                            occurred_at=now-timedelta(minutes=4-i*2)))
    for i, token_index in [(3, 1), (5, 1), (0, 2)]:
        trades.append(Trade(signature=f"demo-sig-{i}", wallet_address=wallets[i].address,
                            token_mint=tokens[token_index].mint, side="buy", amount_usd=760,
                            occurred_at=now-timedelta(minutes=12+i)))
    db.add_all(trades)
    db.flush()
    score_floor = settings.smart_money_min_score if min_score is None else min_score
    eligible = {wallet.address for wallet in wallets if wallet.tracked and wallet.score >= score_floor}
    clusters = detect_clusters(trades, min_wallets or settings.cluster_min_wallets,
                               window_minutes or settings.cluster_window_minutes, eligible_wallets=eligible)
    token_by_mint = {token.mint: token for token in tokens}
    for cluster in clusters:
        symbol = token_by_mint[cluster["token_mint"]].symbol
        db.add(Signal(kind="cluster", token_mint=cluster["token_mint"],
                      title=f"{cluster['wallet_count']} tracked wallets bought ${symbol}",
                      wallet_count=cluster["wallet_count"], confidence=min(99, 72 + cluster["wallet_count"]*5),
                      created_at=now, wallet_addresses=",".join(cluster["wallet_addresses"])))
    kol_wallets = {profile.wallet_address for profile in db.query(KOLProfile).all()}
    for activity in detect_kol_buys(trades, kol_wallets):
        mint, addresses = activity["token_mint"], activity["wallet_addresses"]
        symbol = token_by_mint[mint].symbol
        db.add(Signal(kind="kol_watch", token_mint=mint,
                      title=f"KOL wallet activity detected in ${symbol}", wallet_count=len(addresses),
                      confidence=65 if len(addresses) == 1 else 82, created_at=now,
                      wallet_addresses=",".join(sorted(addresses))))
    db.commit()
