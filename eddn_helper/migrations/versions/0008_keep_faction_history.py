"""Keep faction history for ever: drop the 1-year retention policy on faction_history

Compression stays. PostgreSQL with TimescaleDB only; elsewhere this revision does nothing (SQLite and plain
PostgreSQL never had a policy, store.prune no longer deletes faction history).

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def _timescale(bind):
    if bind.dialect.name != "postgresql":
        return False
    return bind.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")).first() is not None


def upgrade() -> None:
    if _timescale(op.get_bind()):
        op.execute(sa.text("SELECT remove_retention_policy('faction_history', if_exists => true)"))


def downgrade() -> None:
    if _timescale(op.get_bind()):
        op.execute(sa.text("SELECT add_retention_policy('faction_history', INTERVAL '1 year', if_not_exists => true)"))
