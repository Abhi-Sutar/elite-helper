"""Keep the last update-log row per market and system: drop the 1-year retention policy on the event logs

A retention policy drops whole chunks, so it cannot spare one row per key. store.prune() now deletes rows older
than a year except each market's / system's newest. Compression stays. PostgreSQL with TimescaleDB only.

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TABLES = ("market_update", "system_update")


def _timescale(bind):
    if bind.dialect.name != "postgresql":
        return False
    return bind.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")).first() is not None


def upgrade() -> None:
    if _timescale(op.get_bind()):
        for table in TABLES:
            op.execute(sa.text(f"SELECT remove_retention_policy('{table}', if_exists => true)"))


def downgrade() -> None:
    if _timescale(op.get_bind()):
        for table in TABLES:
            op.execute(sa.text(f"SELECT add_retention_policy('{table}', INTERVAL '1 year', if_not_exists => true)"))
