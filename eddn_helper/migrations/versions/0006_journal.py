"""Commander journal history: journal_commander, journal_file, journal_event

* journal_file: one row per Journal.*.log with how far it has been ingested (size read, last final line), so ingest is
  incremental and a query can tell whether the database is behind the files on disk.
* journal_event: one row per journal line with the raw JSON plus the location stamped at ingest time.
* journal_commander: commanders (by FID) seen in the files.

Additive only (new tables); nothing existing is altered.

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    big = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    ts = sa.DateTime(timezone=True)
    js = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
    op.create_table(
        "journal_commander",
        sa.Column("fid", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=True),
        sa.Column("first_seen", ts, nullable=True),
        sa.Column("last_seen", ts, nullable=True),
        sa.PrimaryKeyConstraint("fid", name=op.f("pk_journal_commander")))
    op.create_table(
        "journal_file",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("fid", sa.String(length=40), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("lines_read", sa.Integer(), server_default="0", nullable=False),
        sa.Column("first_ts", ts, nullable=True),
        sa.Column("last_ts", ts, nullable=True),
        sa.Column("complete", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("ingested_at", ts, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_journal_file")),
        sa.UniqueConstraint("name", name=op.f("uq_journal_file_name")))
    op.create_table(
        "journal_event",
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("fid", sa.String(length=40), nullable=True),
        sa.Column("ts", ts, nullable=False),
        sa.Column("event", sa.String(length=60), nullable=False),
        sa.Column("system_id64", big, nullable=True),
        sa.Column("system_name", sa.String(length=120), nullable=True),
        sa.Column("body", sa.String(length=120), nullable=True),
        sa.Column("station", sa.String(length=120), nullable=True),
        sa.Column("market_id", big, nullable=True),
        sa.Column("data", js, nullable=True),
        sa.ForeignKeyConstraint(["file_id"], ["journal_file.id"], name=op.f("fk_journal_event_file_id_journal_file"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("file_id", "line_no", name=op.f("pk_journal_event")))
    op.create_index("ix_journal_event_fid_ts", "journal_event", ["fid", "ts"])
    op.create_index("ix_journal_event_fid_event_ts", "journal_event", ["fid", "event", "ts"])
    op.create_index("ix_journal_event_system_id64_ts", "journal_event", ["system_id64", "ts"])


def downgrade() -> None:
    op.drop_index("ix_journal_event_system_id64_ts", table_name="journal_event")
    op.drop_index("ix_journal_event_fid_event_ts", table_name="journal_event")
    op.drop_index("ix_journal_event_fid_ts", table_name="journal_event")
    op.drop_table("journal_event")
    op.drop_table("journal_file")
    op.drop_table("journal_commander")
