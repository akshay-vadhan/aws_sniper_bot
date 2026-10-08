"""Add persistent paper-trading account and positions."""
from alembic import op
import sqlalchemy as sa

revision = "0004_paper_trading"
down_revision = "0003_live_wallet_ingestion"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("bot_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mode", sa.String(12), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("starting_balance_usd", sa.Float(), nullable=False),
        sa.Column("cash_balance_usd", sa.Float(), nullable=False),
        sa.Column("position_size_usd", sa.Float(), nullable=False),
        sa.Column("target_multiple", sa.Float(), nullable=False),
        sa.Column("stop_loss_pct", sa.Float(), nullable=False),
        sa.Column("min_liquidity_usd", sa.Float(), nullable=False),
        sa.Column("min_volume_24h_usd", sa.Float(), nullable=False),
        sa.Column("min_activity_score", sa.Float(), nullable=False),
        sa.Column("max_open_positions", sa.Integer(), nullable=False),
        sa.Column("fee_bps", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("paper_positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_mint", sa.String(64), sa.ForeignKey("tokens.mint"), nullable=False),
        sa.Column("symbol", sa.String(24), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("entry_price_usd", sa.Float(), nullable=False),
        sa.Column("current_price_usd", sa.Float(), nullable=False),
        sa.Column("exit_price_usd", sa.Float(), nullable=True),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("invested_usd", sa.Float(), nullable=False),
        sa.Column("current_value_usd", sa.Float(), nullable=False),
        sa.Column("realized_pnl_usd", sa.Float(), nullable=False),
        sa.Column("realized_pnl_pct", sa.Float(), nullable=False),
        sa.Column("analysis_score", sa.Float(), nullable=False),
        sa.Column("analysis_reason", sa.Text(), nullable=False),
        sa.Column("exit_reason", sa.String(32), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_paper_positions_token_mint", "paper_positions", ["token_mint"])
    op.create_index("ix_paper_positions_status", "paper_positions", ["status"])
    op.create_index("ix_paper_positions_opened_at", "paper_positions", ["opened_at"])


def downgrade():
    op.drop_index("ix_paper_positions_opened_at", table_name="paper_positions")
    op.drop_index("ix_paper_positions_status", table_name="paper_positions")
    op.drop_index("ix_paper_positions_token_mint", table_name="paper_positions")
    op.drop_table("paper_positions")
    op.drop_table("bot_accounts")
