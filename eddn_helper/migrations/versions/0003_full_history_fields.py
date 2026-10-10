"""Richer time series: mean price + brackets on price_history; happiness, states and left-markers on faction_history

All dialects: add nullable columns (existing rows keep NULL there; EDDN cannot be replayed to backfill them).
PostgreSQL with TimescaleDB only: also
  * switch price_history to the starting compression layout (segment by market, order by commodity then time) -
    the first layout compressed a real 3.5-minute sample to 0.9x (bigger) and this one to 8.8x. This is a
    starting point; the layout is re-checked on real data with scripts/pg-compression-bench.sql.
  * make faction_history a compressed hypertable (7-day chunks) with a 1-year retention policy.

Revision ID: 0003
Revises: 0002
"""
import logging

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

FACTION_CHUNK = "7 days"
FACTION_COMPRESS_AFTER = "14 days"
FACTION_RETAIN = "1 year"


def _timescale(bind):
    if bind.dialect.name != "postgresql":
        return False
    return bind.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")).first() is not None


def upgrade() -> None:
    bind = op.get_bind()
    json_type = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(astext_type=sa.Text()), "postgresql")

    with op.batch_alter_table("price_history") as batch:
        batch.add_column(sa.Column("mean_price", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("stock_bracket", sa.SmallInteger(), nullable=True))
        batch.add_column(sa.Column("demand_bracket", sa.SmallInteger(), nullable=True))
    with op.batch_alter_table("faction_history") as batch:
        batch.add_column(sa.Column("happiness", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("active_states", json_type, nullable=True))
        batch.add_column(sa.Column("pending_states", json_type, nullable=True))
        batch.add_column(sa.Column("recovering_states", json_type, nullable=True))
        batch.add_column(sa.Column("present", sa.Boolean(), server_default=sa.true(), nullable=False))

    if not _timescale(bind):
        return
    op.execute(sa.text(
        "ALTER TABLE price_history SET (timescaledb.compress, timescaledb.compress_segmentby = 'market_id', "
        "timescaledb.compress_orderby = 'commodity_id, ts DESC')"))
    op.execute(sa.text(
        f"SELECT create_hypertable('faction_history', 'ts', chunk_time_interval => INTERVAL '{FACTION_CHUNK}', "
        "create_default_indexes => false, migrate_data => true, if_not_exists => true)"))
    op.execute(sa.text(
        "ALTER TABLE faction_history SET (timescaledb.compress, timescaledb.compress_segmentby = 'system_id64', "
        "timescaledb.compress_orderby = 'faction, ts DESC')"))
    op.execute(sa.text(
        f"SELECT add_compression_policy('faction_history', INTERVAL '{FACTION_COMPRESS_AFTER}', if_not_exists => true)"))
    op.execute(sa.text(
        f"SELECT add_retention_policy('faction_history', INTERVAL '{FACTION_RETAIN}', if_not_exists => true)"))


def downgrade() -> None:
    bind = op.get_bind()
    if _timescale(bind):
        op.execute(sa.text("SELECT remove_retention_policy('faction_history', if_exists => true)"))
        op.execute(sa.text("SELECT remove_compression_policy('faction_history', if_exists => true)"))
        log.warning("faction_history stays a hypertable and the new columns are kept on PostgreSQL; "
                    "dropping columns from compressed hypertables is not attempted")
        return
    with op.batch_alter_table("faction_history") as batch:
        for col in ("present", "recovering_states", "pending_states", "active_states", "happiness"):
            batch.drop_column(col)
    with op.batch_alter_table("price_history") as batch:
        for col in ("demand_bracket", "stock_bracket", "mean_price"):
            batch.drop_column(col)
