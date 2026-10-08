def score_wallet(realized_pnl_pct: float, win_rate: float, trades_count: int) -> float:
    """Bounded 0-100 heuristic; rewards profitable, repeatable history, not volume alone."""
    pnl_component = max(0.0, min(40.0, 20.0 + realized_pnl_pct * 0.4))
    win_component = max(0.0, min(40.0, win_rate * 40.0))
    sample_component = min(20.0, max(0, trades_count) * 2.0)
    return round(min(100.0, pnl_component + win_component + sample_component), 1)
