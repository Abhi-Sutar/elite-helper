"""Keep the economy proportions of a market: markets.economy_shares

EDDN reports a station's economies with a proportion each ([{name, proportion}]); markets.economies only kept the names.
The new nullable column holds the full list. Additive; existing rows stay NULL until the station's next market report.

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("markets", sa.Column(
        "economy_shares", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("markets") as batch:
        batch.drop_column("economy_shares")
