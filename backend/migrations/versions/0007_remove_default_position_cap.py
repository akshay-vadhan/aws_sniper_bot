"""Disable the default paper position-count cap."""
from alembic import op

revision = "0007_remove_position_cap"
down_revision = "0006_telegram_trade_alerts"
branch_labels = None
depends_on = None


def upgrade():
    # User requested no fixed position-count cap. Cash balance still limits buys.
    op.execute("UPDATE bot_accounts SET max_open_positions = 0")


def downgrade():
    op.execute("UPDATE bot_accounts SET max_open_positions = 3 WHERE max_open_positions = 0")
