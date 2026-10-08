"""Initial wallet, token, trade, and signal schema."""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("wallets", sa.Column("address", sa.String(64), primary_key=True), sa.Column("label", sa.String(120), nullable=False), sa.Column("tracked", sa.Boolean(), nullable=False), sa.Column("score", sa.Float(), nullable=False), sa.Column("realized_pnl_pct", sa.Float(), nullable=False), sa.Column("win_rate", sa.Float(), nullable=False), sa.Column("trades_count", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("tokens", sa.Column("mint", sa.String(64), primary_key=True), sa.Column("symbol", sa.String(24), nullable=False), sa.Column("name", sa.String(100), nullable=False), sa.Column("liquidity_usd", sa.Float(), nullable=False), sa.Column("market_cap_usd", sa.Float(), nullable=False), sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_tokens_symbol", "tokens", ["symbol"])
    op.create_table("trades", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("signature", sa.String(100), nullable=False), sa.Column("wallet_address", sa.String(64), sa.ForeignKey("wallets.address"), nullable=False), sa.Column("token_mint", sa.String(64), sa.ForeignKey("tokens.mint"), nullable=False), sa.Column("side", sa.String(8), nullable=False), sa.Column("amount_usd", sa.Float(), nullable=False), sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("signature", "wallet_address", "token_mint", name="uq_trade_identity"))
    for col in ("signature", "wallet_address", "token_mint", "occurred_at"): op.create_index(f"ix_trades_{col}", "trades", [col])
    op.create_table("signals", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("kind", sa.String(32), nullable=False), sa.Column("token_mint", sa.String(64), sa.ForeignKey("tokens.mint"), nullable=False), sa.Column("title", sa.String(180), nullable=False), sa.Column("wallet_count", sa.Integer(), nullable=False), sa.Column("confidence", sa.Float(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("wallet_addresses", sa.Text(), nullable=False))
    for col in ("kind", "token_mint", "created_at"): op.create_index(f"ix_signals_{col}", "signals", [col])

def downgrade():
    op.drop_table("signals")
    op.drop_table("trades")
    op.drop_index("ix_tokens_symbol", table_name="tokens")
    op.drop_table("tokens")
    op.drop_table("wallets")
