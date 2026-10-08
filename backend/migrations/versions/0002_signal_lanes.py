"""Add KOL labels, price samples, and Telegram delivery state."""
from alembic import op
import sqlalchemy as sa

revision = "0002_signal_lanes"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("kol_profiles",
        sa.Column("wallet_address", sa.String(64), sa.ForeignKey("wallets.address"), primary_key=True),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False))
    op.create_table("price_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_mint", sa.String(64), sa.ForeignKey("tokens.mint"), nullable=False),
        sa.Column("price_usd", sa.Float(), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("token_mint", "sampled_at", name="uq_price_sample_token_time"))
    op.create_index("ix_price_samples_token_mint", "price_samples", ["token_mint"])
    op.create_index("ix_price_samples_sampled_at", "price_samples", ["sampled_at"])
    op.create_table("telegram_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("signal_id", sa.Integer(), sa.ForeignKey("signals.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("signal_id", name="uq_telegram_signal_delivery"))
    op.create_index("ix_telegram_deliveries_signal_id", "telegram_deliveries", ["signal_id"])


def downgrade():
    op.drop_table("telegram_deliveries")
    op.drop_index("ix_price_samples_sampled_at", table_name="price_samples")
    op.drop_index("ix_price_samples_token_mint", table_name="price_samples")
    op.drop_table("price_samples")
    op.drop_table("kol_profiles")
