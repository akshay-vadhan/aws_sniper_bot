"""Add multi-chain paper sniper position state and event history."""
from alembic import op
import sqlalchemy as sa

revision = "0005_multichain_sniper"
down_revision = "0004_paper_trading"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tokens", sa.Column("chain_id", sa.String(20), nullable=False, server_default="solana"))
    op.create_index("ix_tokens_chain_id", "tokens", ["chain_id"])
    # Move only untouched defaults from the previous non-sniper strategy.
    op.execute("UPDATE bot_accounts SET min_liquidity_usd = 5000, min_volume_24h_usd = 0, min_activity_score = 10, stop_loss_pct = 60 WHERE min_liquidity_usd = 15000 AND min_volume_24h_usd = 50000 AND min_activity_score = 50 AND stop_loss_pct = 25")
    # Require an explicit start for the new strategy after upgrading.
    op.execute("UPDATE bot_accounts SET enabled = false")
    op.add_column("paper_positions", sa.Column("chain_id", sa.String(20), nullable=False, server_default="solana"))
    op.add_column("paper_positions", sa.Column("initial_quantity", sa.Float(), nullable=False, server_default="0"))
    op.add_column("paper_positions", sa.Column("remaining_cost_usd", sa.Float(), nullable=False, server_default="0"))
    op.add_column("paper_positions", sa.Column("peak_price_usd", sa.Float(), nullable=False, server_default="0"))
    op.add_column("paper_positions", sa.Column("reached_2x", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("paper_positions", sa.Column("sold_2x", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("paper_positions", sa.Column("sold_3x", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("paper_positions", sa.Column("sold_5x", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("paper_positions", sa.Column("sold_10x", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_paper_positions_chain_id", "paper_positions", ["chain_id"])
    op.execute("UPDATE paper_positions SET initial_quantity = quantity, remaining_cost_usd = invested_usd, peak_price_usd = current_price_usd")
    op.create_table("paper_trade_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("position_id", sa.Integer(), sa.ForeignKey("paper_positions.id"), nullable=False),
        sa.Column("chain_id", sa.String(20), nullable=False),
        sa.Column("token_mint", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(24), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("price_usd", sa.Float(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("proceeds_usd", sa.Float(), nullable=False),
        sa.Column("realized_pnl_usd", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    for col in ("position_id", "chain_id", "token_mint", "created_at"):
        op.create_index(f"ix_paper_trade_events_{col}", "paper_trade_events", [col])


def downgrade():
    for col in ("created_at", "token_mint", "chain_id", "position_id"):
        op.drop_index(f"ix_paper_trade_events_{col}", table_name="paper_trade_events")
    op.drop_table("paper_trade_events")
    op.drop_index("ix_paper_positions_chain_id", table_name="paper_positions")
    for col in ("sold_10x", "sold_5x", "sold_3x", "sold_2x", "reached_2x", "peak_price_usd", "remaining_cost_usd", "initial_quantity", "chain_id"):
        op.drop_column("paper_positions", col)
    op.drop_index("ix_tokens_chain_id", table_name="tokens")
    op.drop_column("tokens", "chain_id")
