from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from app.detector import detect_clusters
from app.scoring import score_wallet
from app.signal_lanes import backtest_return, detect_boost, detect_graduate, detect_hatchery, detect_kol_buys
from app.ml_scoring import train_logistic
from app.wallet_ingestion import _swap_changes
from app.paper_trading import analyze_candidate, exit_reason
from app.solana_providers import _normalize_alchemy_transaction


def trade(wallet, minute, side="buy", mint="mint-a"):
    return SimpleNamespace(wallet_address=wallet, token_mint=mint, side=side,
                           occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc)+timedelta(minutes=minute))


def test_detects_distinct_wallets_inside_rolling_window():
    result = detect_clusters([trade("a", 0), trade("b", 7), trade("c", 9)], 3, 10)
    assert len(result) == 1
    assert result[0]["wallet_addresses"] == ["a", "b", "c"]


def test_cluster_can_require_smart_money_wallets():
    buys = [trade("a", 0), trade("b", 2), trade("c", 4)]
    assert detect_clusters(buys, 3, 10, eligible_wallets={"a", "b"}) == []


def test_helius_swap_parser_uses_wallet_token_balance_delta():
    tx = {"type": "SWAP", "tokenBalanceChanges": [
        {"userAccount": "wallet-a", "mint": "token-x", "rawTokenAmount": {"tokenAmount": "2500000", "decimals": 6}},
        {"userAccount": "wallet-a", "mint": "token-y", "rawTokenAmount": {"tokenAmount": "-400000", "decimals": 5}},
        {"userAccount": "other-wallet", "mint": "token-z", "rawTokenAmount": {"tokenAmount": "1", "decimals": 0}},
    ]}
    assert _swap_changes(tx, "wallet-a") == [("token-x", "buy", 2.5), ("token-y", "sell", 4.0)]


def test_helius_parser_ignores_non_swap_transactions():
    assert _swap_changes({"type": "TRANSFER", "tokenTransfers": []}, "wallet-a") == []


def test_alchemy_full_transaction_normalizes_wallet_owned_token_deltas():
    address = "wallet-a"
    row = {"signature": "sig-a", "blockTime": 123,
        "transaction": {"signatures": ["sig-a"], "message": {"accountKeys": [address]}},
        "meta": {"preBalances": [1_000_000_000], "postBalances": [900_000_000],
            "preTokenBalances": [], "postTokenBalances": [
                {"owner": address, "mint": "mint-a", "uiTokenAmount": {"amount": "2500", "decimals": 2}}]}}
    normalized = _normalize_alchemy_transaction(row, address)
    assert normalized["type"] == "SWAP"
    assert normalized["timestamp"] == 123
    assert _swap_changes(normalized, address) == [("mint-a", "buy", 25.0)]


def test_alchemy_token_transfer_without_quote_or_sol_movement_is_not_a_swap():
    address = "wallet-a"
    row = {"signature": "sig-b", "transaction": {"message": {"accountKeys": [address]}},
        "meta": {"preBalances": [1_000], "postBalances": [1_000], "preTokenBalances": [],
            "postTokenBalances": [{"owner": address, "mint": "mint-a",
                "uiTokenAmount": {"amount": "2500", "decimals": 2}}]}}
    normalized = _normalize_alchemy_transaction(row, address)
    assert normalized["type"] == "TRANSFER"
    assert _swap_changes(normalized, address) == []


def test_paper_analysis_requires_liquidity_volume_activity_and_buy_pressure():
    account = SimpleNamespace(min_liquidity_usd=15000, min_volume_24h_usd=50000, min_activity_score=50)
    candidate = {"price_usd": 0.01, "liquidity_usd": 50000, "volume_24h_usd": 100000,
                 "activity_score": 70, "buys_24h": 900, "sells_24h": 500, "change_24h_pct": 12}
    assert analyze_candidate(candidate, account)[0] > 50
    assert analyze_candidate({**candidate, "liquidity_usd": 1000}, account) is None
    assert analyze_candidate({**candidate, "buys_24h": 20, "sells_24h": 200}, account) is None


def test_paper_take_profit_targets_net_double_after_simulated_fees():
    account = SimpleNamespace(fee_bps=100, target_multiple=2, stop_loss_pct=25)
    position = SimpleNamespace(quantity=98, invested_usd=100)
    reason, value, pnl_pct = exit_reason(position, 2.1, account)
    assert reason == "take_profit"
    assert value >= 200
    assert pnl_pct >= 100
    assert exit_reason(position, 0.7, account)[0] == "stop_loss"


def test_ignores_sells_duplicate_wallets_and_out_of_window_buys():
    result = detect_clusters([trade("a", 0), trade("a", 1), trade("b", 2), trade("c", 13),
                              trade("d", 3, side="sell")], 3, 10)
    assert result == []


def test_wallet_score_is_bounded_and_rewards_consistency():
    weak = score_wallet(-20, .3, 1)
    strong = score_wallet(100, .7, 15)
    assert 0 <= weak <= 100
    assert strong > weak
    assert score_wallet(10000, 1, 1000) == 100


def test_kol_detection_and_additional_lanes():
    kol_hits = detect_kol_buys([trade("kol-a", 0), trade("regular", 1), trade("kol-b", 2)], {"kol-a", "kol-b"})
    assert kol_hits[0]["wallet_count"] == 2
    assert detect_boost(2, 12, 30000, 10000, 50000).detected
    assert detect_graduate(True, "pool-1", 50000, 9000).detected
    assert detect_hatchery(datetime.now(timezone.utc) - timedelta(minutes=20), True, True, 8000).detected


def test_backtest_metrics_and_trainable_logistic_baseline():
    result = backtest_return(10, [12, 9, 15])
    assert result["return_pct"] == 50
    assert result["max_drawdown_pct"] == -25
    examples = []
    for i in range(12):
        examples.append({"features": {"win_rate": .75, "trade_count": 30, "realized_pnl_pct": 80}, "profitable": 1})
        examples.append({"features": {"win_rate": .25, "trade_count": 3, "realized_pnl_pct": -50}, "profitable": 0})
    model = train_logistic(examples)
    assert model.examples == 24
    assert model.predict(examples[0]["features"]) > model.predict(examples[1]["features"])
