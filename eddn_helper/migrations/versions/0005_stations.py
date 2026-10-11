"""EDSM stations with their controlling faction: stations + station_sync

* stations: one row per station / outpost / surface settlement of a system (fleet carriers are not stored), replaced as a
  whole whenever the system is refreshed from EDSM. Carries the controlling faction so "which stations does faction X
  control" and "which of these stations belong to the faction that is about to expand" can be answered.
* station_sync: one row per system we have asked EDSM about, so "this system has no stations" is different from "we have not
  looked yet".

Additive only (new tables); nothing existing is altered.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    big = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    ts = sa.DateTime(timezone=True)
    op.create_table(
        "stations",
        sa.Column("system_id64", big, nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("market_id", big, nullable=True),
        sa.Column("station_type", sa.String(length=40), nullable=True),
        sa.Column("body", sa.String(length=120), nullable=True),
        sa.Column("distance_to_arrival_ls", sa.Float(), nullable=True),
        sa.Column("economy", sa.String(length=40), nullable=True),
        sa.Column("second_economy", sa.String(length=40), nullable=True),
        sa.Column("allegiance", sa.String(length=40), nullable=True),
        sa.Column("government", sa.String(length=40), nullable=True),
        sa.Column("controlling_faction", sa.String(length=120), nullable=True),
        sa.Column("has_market", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("has_shipyard", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("has_outfitting", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("services", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=True),
        sa.Column("info_updated_at", ts, nullable=True),
        sa.Column("fetched_at", ts, nullable=False),
        sa.Column("source", sa.String(length=16), server_default="edsm", nullable=False),
        sa.ForeignKeyConstraint(["system_id64"], ["systems.id64"], name=op.f("fk_stations_system_id64_systems"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("system_id64", "name", name=op.f("pk_stations")))
    op.create_index("ix_stations_market_id", "stations", ["market_id"])
    op.create_index("ix_stations_controlling_faction_lower", "stations", [sa.text("lower(controlling_faction)")])

    op.create_table(
        "station_sync",
        sa.Column("system_id64", big, nullable=False),
        sa.Column("fetched_at", ts, nullable=False),
        sa.Column("station_count", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("source", sa.String(length=16), server_default="edsm", nullable=False),
        sa.ForeignKeyConstraint(["system_id64"], ["systems.id64"], name=op.f("fk_station_sync_system_id64_systems"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("system_id64", name=op.f("pk_station_sync")))


def downgrade() -> None:
    op.drop_table("station_sync")
    op.drop_index("ix_stations_controlling_faction_lower", table_name="stations")
    op.drop_index("ix_stations_market_id", table_name="stations")
    op.drop_table("stations")
