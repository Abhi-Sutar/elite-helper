"""Player-faction flag: faction_info

* faction_info: one row per faction name with facts EDDN does not carry. Today that is is_player, filled from EDSM's
  /api-system-v1/factions. A faction with no row is unknown, not an NPC.

Additive only (a new table); nothing existing is altered.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "faction_info",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("is_player", sa.Boolean(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=16), server_default="edsm", nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_faction_info")))
    op.create_index("ix_faction_info_name_lower", "faction_info", [sa.text("lower(name)")], unique=True)


def downgrade() -> None:
    op.drop_index("ix_faction_info_name_lower", table_name="faction_info")
    op.drop_table("faction_info")
