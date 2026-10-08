"""Track Telegram delivery state for paper-trading events."""
from alembic import op
import sqlalchemy as sa

revision = "0006_telegram_trade_alerts"
down_revision = "0005_multichain_sniper"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("paper_trade_events", sa.Column("telegram_attempted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("paper_trade_events", sa.Column("telegram_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("paper_trade_events", sa.Column("telegram_error", sa.Text(), nullable=False, server_default=""))
    # Historical paper executions are already represented in the dashboard; never replay them as new alerts.
    op.execute("UPDATE paper_trade_events SET telegram_attempted_at = created_at, telegram_sent_at = created_at")


def downgrade():
    op.drop_column("paper_trade_events", "telegram_error")
    op.drop_column("paper_trade_events", "telegram_sent_at")
    op.drop_column("paper_trade_events", "telegram_attempted_at")
