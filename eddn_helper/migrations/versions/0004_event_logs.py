"""Update log (traffic) + change-log support: market_update, system_update, logged_at on the snapshots

* market_update / system_update: one small row per accepted EDDN message (who, when, how many items, how many
  history rows it produced). This is the exact update rate of a station or system, and it lets price/faction history
  store only changes while every update time stays known.
* commodity_prices.logged_at / factions.logged_at: when a history row was last written for that item, so the
  writer can decide "changed / anchor due" without reading the large history tables.
* commodity_prices.seen_at: the last message that contained the commodity. EDDN reports can be partial (the same
  station is reported with 368 items by most uploaders and with 4 by another), so a commodity is never treated as
  gone just because one message omitted it; readers judge freshness per commodity from seen_at instead.

PostgreSQL with TimescaleDB only: both event tables become compressed hypertables (7-day chunks) with a 1-year
retention policy.

Revision ID: 0004
Revises: 0003
"""
import logging

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

CHUNK = "7 days"
COMPRESS_AFTER = "14 days"
RETAIN = "1 year"


def _timescale(bind):
    if bind.dialect.name != "postgresql":
        return False
    return bind.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")).first() is not None


def upgrade() -> None:
    bind = op.get_bind()
    big = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    ts = sa.DateTime(timezone=True)

    op.create_table(
        "market_update",
        sa.Column("market_id", big, nullable=False),
        sa.Column("ts", ts, nullable=False),
        sa.Column("uploader", sa.String(length=64), server_default="", nullable=False),
        sa.Column("software", sa.String(length=40), server_default="", nullable=False),
        sa.Column("commodities", sa.SmallInteger(), nullable=True),
        sa.Column("changed", sa.SmallInteger(), nullable=True),
        sa.ForeignKeyConstraint(["market_id"], ["markets.market_id"],
                                name=op.f("fk_market_update_market_id_markets"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("market_id", "ts", "uploader", name=op.f("pk_market_update")))
    op.create_index("ix_market_update_ts", "market_update", ["ts"])

    op.create_table(
        "system_update",
        sa.Column("system_id64", big, nullable=False),
        sa.Column("ts", ts, nullable=False),
        sa.Column("uploader", sa.String(length=64), server_default="", nullable=False),
        sa.Column("software", sa.String(length=40), server_default="", nullable=False),
        sa.Column("factions", sa.SmallInteger(), nullable=True),
        sa.Column("changed", sa.SmallInteger(), nullable=True),
        sa.ForeignKeyConstraint(["system_id64"], ["systems.id64"],
                                name=op.f("fk_system_update_system_id64_systems"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("system_id64", "ts", "uploader", name=op.f("pk_system_update")))
    op.create_index("ix_system_update_ts", "system_update", ["ts"])

    with op.batch_alter_table("commodity_prices") as batch:
        batch.add_column(sa.Column("logged_at", ts, nullable=True))
        batch.add_column(sa.Column("seen_at", ts, nullable=True))
    op.execute(sa.text("UPDATE commodity_prices SET seen_at = "
                       "(SELECT m.updated_at FROM markets m WHERE m.market_id = commodity_prices.market_id)"))
    with op.batch_alter_table("factions") as batch:
        batch.add_column(sa.Column("logged_at", ts, nullable=True))

    if not _timescale(bind):
        return
    for table, key in (("market_update", "market_id"), ("system_update", "system_id64")):
        op.execute(sa.text(
            f"SELECT create_hypertable('{table}', 'ts', chunk_time_interval => INTERVAL '{CHUNK}', "
            "create_default_indexes => false, migrate_data => true, if_not_exists => true)"))
        op.execute(sa.text(
            f"ALTER TABLE {table} SET (timescaledb.compress, timescaledb.compress_segmentby = '{key}', "
            "timescaledb.compress_orderby = 'ts DESC')"))
        op.execute(sa.text(
            f"SELECT add_compression_policy('{table}', INTERVAL '{COMPRESS_AFTER}', if_not_exists => true)"))
        op.execute(sa.text(f"SELECT add_retention_policy('{table}', INTERVAL '{RETAIN}', if_not_exists => true)"))


def downgrade() -> None:
    bind = op.get_bind()
    if _timescale(bind):
        for table in ("market_update", "system_update"):
            op.execute(sa.text(f"SELECT remove_retention_policy('{table}', if_exists => true)"))
            op.execute(sa.text(f"SELECT remove_compression_policy('{table}', if_exists => true)"))
    with op.batch_alter_table("factions") as batch:
        batch.drop_column("logged_at")
    with op.batch_alter_table("commodity_prices") as batch:
        batch.drop_column("seen_at")
        batch.drop_column("logged_at")
    op.drop_table("system_update")
    op.drop_table("market_update")
