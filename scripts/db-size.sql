-- Size of the live database and which databases currently have connections.
-- Run: $env:PGPASSFILE = 'C:\Users\<you>\AppData\Local\elite-helper\pgpass.conf'; psql -h localhost -U elite_reader -d elite -f scripts\db-size.sql
\pset pager off
\echo == database sizes
SELECT datname, pg_size_pretty(pg_database_size(datname)) AS size FROM pg_database
WHERE datname IN ('elite', 'elite_dev', 'elite_test') ORDER BY pg_database_size(datname) DESC;

\echo == live tables (hypertables include all chunks)
SELECT c.relname AS "table",
       pg_size_pretty(coalesce((SELECT hypertable_size(format('%I', c.relname)::regclass)
                                WHERE EXISTS (SELECT 1 FROM timescaledb_information.hypertables h WHERE h.hypertable_name = c.relname)),
                               pg_total_relation_size(c.oid))) AS size,
       (SELECT count(*) FROM timescaledb_information.hypertables h WHERE h.hypertable_name = c.relname) = 1 AS hypertable
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 12;

\echo == row counts and latest update in the live database
SELECT (SELECT count(*) FROM market_update) AS market_updates, (SELECT max(ts) FROM market_update) AS latest_market_update,
       (SELECT count(*) FROM system_update) AS system_updates, (SELECT count(*) FROM price_history) AS price_rows,
       (SELECT count(*) FROM faction_history) AS faction_rows;

\echo == connections per database (a running listener holds one)
SELECT datname, usename, count(*) AS connections, max(backend_start) AS newest FROM pg_stat_activity
WHERE datname IN ('elite', 'elite_dev', 'elite_test') GROUP BY 1, 2 ORDER BY 1, 2;
