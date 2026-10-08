"""Store a Helius transaction cursor for each tracked wallet."""
from alembic import op
import sqlalchemy as sa

revision = "0003_live_wallet_ingestion"
down_revision = "0002_signal_lanes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("wallets", sa.Column("last_signature", sa.String(100), nullable=True))


def downgrade():
    op.drop_column("wallets", "last_signature")
