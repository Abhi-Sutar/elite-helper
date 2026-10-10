-- Smoke test for PostgreSQL + TimescaleDB on this machine. Creates a throwaway database, exercises exactly the
-- features the market time series needs (hypertable, compression, compression policy, retention policy),
-- prints the results, then drops it again. Run from a normal terminal:
--     psql -U postgres -f scripts\pg-smoke-test.sql
-- Any error message tells us which feature is missing; send it back unchanged.

\set ON_ERROR_STOP on
\echo === versions
SELECT version();

DROP DATABASE IF EXISTS ts_smoke;
CREATE DATABASE ts_smoke;
\connect ts_smoke

CREATE EXTENSION IF NOT EXISTS timescaledb;
SELECT extname, extversion FROM pg_extension WHERE extname = 'timescaledb';
\dx

\echo === hypertable shaped like price_history (primary key includes the time column)
CREATE TABLE m (
    market_id    bigint      NOT NULL,
    commodity_id integer     NOT NULL,
    ts           timestamptz NOT NULL,
    buy_price    integer,
    stock        bigint,
    PRIMARY KEY (market_id, commodity_id, ts)
);
SELECT create_hypertable('m', 'ts', chunk_time_interval => INTERVAL '1 day');

INSERT INTO m
SELECT (g % 20) + 1, (g % 50) + 1, now() - (g || ' seconds')::interval - INTERVAL '3 days', 1000 + (g % 7), g
FROM generate_series(1, 200000) g
ON CONFLICT DO NOTHING;
SELECT count(*) AS rows_inserted, count(DISTINCT tableoid) AS chunks FROM m;

\echo === compression (segment by market and commodity)
ALTER TABLE m SET (timescaledb.compress,
                   timescaledb.compress_segmentby = 'market_id, commodity_id',
                   timescaledb.compress_orderby = 'ts DESC');
SELECT compress_chunk(c, if_not_compressed => true) FROM show_chunks('m', older_than => INTERVAL '1 day') c;
SELECT pg_size_pretty(before_compression_total_bytes) AS before,
       pg_size_pretty(after_compression_total_bytes)  AS after,
       round(before_compression_total_bytes::numeric / NULLIF(after_compression_total_bytes, 0), 1) AS ratio
FROM hypertable_compression_stats('m');

\echo === policies
SELECT add_compression_policy('m', INTERVAL '2 days');
SELECT add_retention_policy('m', INTERVAL '30 days');
SELECT proc_name, schedule_interval FROM timescaledb_information.jobs WHERE hypertable_name = 'm';

\echo === cleanup
\connect postgres
DROP DATABASE ts_smoke;
\echo SMOKE TEST PASSED
