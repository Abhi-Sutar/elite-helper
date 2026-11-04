"""Storage tuning for the tables the listener updates in place (PostgreSQL only)

commodity_prices is rewritten on every market message: leave room in each page so updates stay HOT (no index
maintenance), and vacuum after ~2 % dead rows instead of the default 20 % (which is ~1.6M rows at 8M). systems gets
the same autovacuum settings. Only the storage parameters change, no rewrite; pages already full benefit after
a later rewrite. SQLite: nothing to do.

Revision ID: 0011
Revises: 0010
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_AUTOVACUUM = ("autovacuum_vacuum_scale_factor", "autovacuum_vacuum_threshold", "autovacuum_analyze_scale_factor")


def _pg() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _pg():
        return
    av = "autovacuum_vacuum_scale_factor = 0.02, autovacuum_vacuum_threshold = 1000, " \
         "autovacuum_analyze_scale_factor = 0.02"
    op.execute(f"ALTER TABLE commodity_prices SET (fillfactor = 80, {av})")
    op.execute(f"ALTER TABLE systems SET ({av})")


def downgrade() -> None:
    if not _pg():
        return
    op.execute(f"ALTER TABLE commodity_prices RESET (fillfactor, {', '.join(_AUTOVACUUM)})")
    op.execute(f"ALTER TABLE systems RESET ({', '.join(_AUTOVACUUM)})")
