"""TimescaleDB: make price_history a compressed hypertable with a 30-day retention policy

On anything other than PostgreSQL with the timescaledb extension installed this revision does nothing, so
SQLite databases (and plain PostgreSQL) simply move to the next revision number.

price_history's primary key (market_id, commodity_id, ts) already contains the partitioning column, which
TimescaleDB requires for unique indexes. Foreign keys from the hypertable to the regular `markets` and
`commodities` tables are kept.

Revision ID: 0002
Revises: 0001
"""
import logging

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

CHUNK_INTERVAL = "1 day"
COMPRESS_AFTER = "2 days"
RETAIN_FOR = "30 days"


def _timescale_available(bind):
    if bind.dialect.name != "postgresql":
        return False
    found = bind.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")).first()
    if found is None:
        log.warning("timescaledb extension is not installed in this database; price_history stays a plain table")
    return found is not None


def upgrade() -> None:
    bind = op.get_bind()
    if not _timescale_available(bind):
        return
    op.execute(sa.text(
        "SELECT create_hypertable('price_history', 'ts', "
        f"chunk_time_interval => INTERVAL '{CHUNK_INTERVAL}', create_default_indexes => false, "
        "migrate_data => true, if_not_exists => true)"))
    op.execute(sa.text(
        "ALTER TABLE price_history SET (timescaledb.compress, "
        "timescaledb.compress_segmentby = 'market_id, commodity_id', "
        "timescaledb.compress_orderby = 'ts DESC')"))
    op.execute(sa.text(
        f"SELECT add_compression_policy('price_history', INTERVAL '{COMPRESS_AFTER}', if_not_exists => true)"))
    op.execute(sa.text(
        f"SELECT add_retention_policy('price_history', INTERVAL '{RETAIN_FOR}', if_not_exists => true)"))


def downgrade() -> None:
    bind = op.get_bind()
    if not _timescale_available(bind):
        return
    # Removing the policies is safe; turning the hypertable back into a plain table would need every chunk
    # decompressed and copied, so it is deliberately not attempted here.
    op.execute(sa.text("SELECT remove_retention_policy('price_history', if_exists => true)"))
    op.execute(sa.text("SELECT remove_compression_policy('price_history', if_exists => true)"))
