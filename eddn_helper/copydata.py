"""Copy every table from one database to another with the same schema (SQLite -> PostgreSQL, or back)."""

import sys

from sqlalchemy import func, inspect, select, text

from . import database, migrate
from .models import Base

BATCH = 20000


def _progress(msg):
    print(msg, file=sys.stderr, flush=True)


def copy_data(source_url, dest_url, batch=BATCH, progress=_progress):
    """
    Copy all rows, parents before children, committing per batch. Safe to re-run: rows that already exist in the
    destination are skipped (ON CONFLICT DO NOTHING). Both databases must already be at the current schema
    revision. Stop the listener that writes to the source first, or rows written during the copy are missed.
    Returns {table: {"source": rows_in_source, "dest": rows_in_dest_afterwards}}.
    """
    src_engine = database.make_engine(source_url)
    dst_engine = database.make_engine(dest_url)
    try:
        migrate.ensure_current(src_engine)
        migrate.ensure_current(dst_engine)
        if src_engine.url == dst_engine.url:
            raise ValueError("source and destination are the same database")
        insert = database.insert_for(dst_engine)
        src_tables = set(inspect(src_engine).get_table_names())
        result = {}
        with src_engine.connect() as src, dst_engine.connect() as dst:
            for table in Base.metadata.sorted_tables:
                if table.name not in src_tables:
                    continue
                total = src.execute(select(func.count()).select_from(table)).scalar_one()
                done = 0
                progress(f"{table.name}: {total} rows")
                stream = src.execution_options(stream_results=True).execute(select(table))
                for part in stream.partitions(batch):
                    dst.execute(insert(table).on_conflict_do_nothing(), [dict(r._mapping) for r in part])
                    dst.commit()
                    done += len(part)
                    if total > batch:
                        progress(f"  {table.name}: {done}/{total}")
                src.rollback()
                result[table.name] = {"source": total,
                                      "dest": dst.execute(select(func.count()).select_from(table)).scalar_one()}
            if dst_engine.dialect.name == "postgresql":
                dst.execute(text("SELECT setval(pg_get_serial_sequence('commodities', 'id'), "
                                 "COALESCE((SELECT MAX(id) FROM commodities), 1))"))
                dst.commit()
        return result
    finally:
        src_engine.dispose()
        dst_engine.dispose()
