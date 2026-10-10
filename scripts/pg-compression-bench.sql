-- Compare TimescaleDB compression layouts on REAL data, without touching the production tables.
-- Copies recent rows of price_history / faction_history into scratch hypertables (bench_*), one per candidate
-- layout, compresses them, and prints size, compression ratio and query timings.
--
-- Run it only after the listener has collected at least a day or two of full-mode data (with less data every
-- market/commodity group has one row and nothing compresses, as the 3.5-minute test showed):
--     psql -h localhost -U elite_writer -d elite -f scripts\pg-compression-bench.sql
-- Options (before -f, as -v name=value):  -v span="2 days"   price_history window to copy (default 1 day)
--                                         -v fspan="14 days"  faction_history window (default 7 days)
--                                         -v keep=1           keep the bench_* tables afterwards
-- PGPASSFILE must point at your pgpass.conf (see scripts\create-pgpass.ps1).

\set ON_ERROR_STOP on
\if :{?span}
\else
  \set span '1 day'
\endif
\if :{?fspan}
\else
  \set fspan '7 days'
\endif

\echo === source data
SELECT count(*) AS price_rows, min(ts) AS first, max(ts) AS last, count(DISTINCT market_id) AS markets,
       count(DISTINCT (market_id, commodity_id)) AS market_commodity_groups,
       round(count(*)::numeric / NULLIF(count(DISTINCT (market_id, commodity_id)), 0), 1) AS rows_per_group
FROM price_history WHERE ts >= now() - :'span'::interval;
SELECT count(*) AS faction_rows, min(ts) AS first, max(ts) AS last, count(DISTINCT system_id64) AS systems,
       round(count(*)::numeric / NULLIF(count(DISTINCT (system_id64, faction)), 0), 1) AS rows_per_system_faction
FROM faction_history WHERE ts >= now() - :'fspan'::interval;

-- the busiest market and its busiest commodity, used for the query timings below
SELECT market_id AS hub_market FROM price_history WHERE ts >= now() - :'span'::interval
GROUP BY market_id ORDER BY count(*) DESC LIMIT 1 \gset
SELECT commodity_id AS hub_commodity FROM price_history
WHERE ts >= now() - :'span'::interval AND market_id = :hub_market
GROUP BY commodity_id ORDER BY count(*) DESC LIMIT 1 \gset
SELECT system_id64 AS hub_system FROM faction_history WHERE ts >= now() - :'fspan'::interval
GROUP BY system_id64 ORDER BY count(*) DESC LIMIT 1 \gset

DROP TABLE IF EXISTS bench_ph_a, bench_ph_b, bench_ph_c, bench_ph_d, bench_fh_a, bench_fh_b CASCADE;

-- price_history candidates: A segment market+commodity, B segment market (ordered by commodity, ts),
-- C = A with 7-day chunks, D = B with 7-day chunks
CREATE TABLE bench_ph_a (LIKE price_history INCLUDING DEFAULTS);
CREATE TABLE bench_ph_b (LIKE price_history INCLUDING DEFAULTS);
CREATE TABLE bench_ph_c (LIKE price_history INCLUDING DEFAULTS);
CREATE TABLE bench_ph_d (LIKE price_history INCLUDING DEFAULTS);
SELECT create_hypertable('bench_ph_a', 'ts', chunk_time_interval => INTERVAL '1 day',  create_default_indexes => false);
SELECT create_hypertable('bench_ph_b', 'ts', chunk_time_interval => INTERVAL '1 day',  create_default_indexes => false);
SELECT create_hypertable('bench_ph_c', 'ts', chunk_time_interval => INTERVAL '7 days', create_default_indexes => false);
SELECT create_hypertable('bench_ph_d', 'ts', chunk_time_interval => INTERVAL '7 days', create_default_indexes => false);
ALTER TABLE bench_ph_a SET (timescaledb.compress, timescaledb.compress_segmentby = 'market_id, commodity_id', timescaledb.compress_orderby = 'ts DESC');
ALTER TABLE bench_ph_b SET (timescaledb.compress, timescaledb.compress_segmentby = 'market_id',               timescaledb.compress_orderby = 'commodity_id, ts DESC');
ALTER TABLE bench_ph_c SET (timescaledb.compress, timescaledb.compress_segmentby = 'market_id, commodity_id', timescaledb.compress_orderby = 'ts DESC');
ALTER TABLE bench_ph_d SET (timescaledb.compress, timescaledb.compress_segmentby = 'market_id',               timescaledb.compress_orderby = 'commodity_id, ts DESC');

-- faction_history candidates: A segment system+faction, B segment system (ordered by faction, ts); 7-day chunks
CREATE TABLE bench_fh_a (LIKE faction_history INCLUDING DEFAULTS);
CREATE TABLE bench_fh_b (LIKE faction_history INCLUDING DEFAULTS);
SELECT create_hypertable('bench_fh_a', 'ts', chunk_time_interval => INTERVAL '7 days', create_default_indexes => false);
SELECT create_hypertable('bench_fh_b', 'ts', chunk_time_interval => INTERVAL '7 days', create_default_indexes => false);
ALTER TABLE bench_fh_a SET (timescaledb.compress, timescaledb.compress_segmentby = 'system_id64, faction', timescaledb.compress_orderby = 'ts DESC');
ALTER TABLE bench_fh_b SET (timescaledb.compress, timescaledb.compress_segmentby = 'system_id64',          timescaledb.compress_orderby = 'faction, ts DESC');

\echo === copying data (this can take a while)
INSERT INTO bench_ph_a SELECT * FROM price_history WHERE ts >= now() - :'span'::interval;
INSERT INTO bench_ph_b SELECT * FROM bench_ph_a;
INSERT INTO bench_ph_c SELECT * FROM bench_ph_a;
INSERT INTO bench_ph_d SELECT * FROM bench_ph_a;
INSERT INTO bench_fh_a SELECT * FROM faction_history WHERE ts >= now() - :'fspan'::interval;
INSERT INTO bench_fh_b SELECT * FROM bench_fh_a;

\echo === compressing
SELECT count(compress_chunk(c, if_not_compressed => true)) AS chunks_a FROM show_chunks('bench_ph_a') c;
SELECT count(compress_chunk(c, if_not_compressed => true)) AS chunks_b FROM show_chunks('bench_ph_b') c;
SELECT count(compress_chunk(c, if_not_compressed => true)) AS chunks_c FROM show_chunks('bench_ph_c') c;
SELECT count(compress_chunk(c, if_not_compressed => true)) AS chunks_d FROM show_chunks('bench_ph_d') c;
SELECT count(compress_chunk(c, if_not_compressed => true)) AS chunks_fa FROM show_chunks('bench_fh_a') c;
SELECT count(compress_chunk(c, if_not_compressed => true)) AS chunks_fb FROM show_chunks('bench_fh_b') c;

\echo === RESULT: sizes and compression ratio (higher ratio = smaller; ratio below 1 = worse than uncompressed)
SELECT 'price A: segment market+commodity, 1-day chunks'  AS layout, pg_size_pretty(before_compression_total_bytes) AS before, pg_size_pretty(after_compression_total_bytes) AS after, round(before_compression_total_bytes::numeric / NULLIF(after_compression_total_bytes, 0), 1) AS ratio FROM hypertable_compression_stats('bench_ph_a')
UNION ALL SELECT 'price B: segment market, order commodity,ts, 1-day', pg_size_pretty(before_compression_total_bytes), pg_size_pretty(after_compression_total_bytes), round(before_compression_total_bytes::numeric / NULLIF(after_compression_total_bytes, 0), 1) FROM hypertable_compression_stats('bench_ph_b')
UNION ALL SELECT 'price C: segment market+commodity, 7-day chunks', pg_size_pretty(before_compression_total_bytes), pg_size_pretty(after_compression_total_bytes), round(before_compression_total_bytes::numeric / NULLIF(after_compression_total_bytes, 0), 1) FROM hypertable_compression_stats('bench_ph_c')
UNION ALL SELECT 'price D: segment market, order commodity,ts, 7-day', pg_size_pretty(before_compression_total_bytes), pg_size_pretty(after_compression_total_bytes), round(before_compression_total_bytes::numeric / NULLIF(after_compression_total_bytes, 0), 1) FROM hypertable_compression_stats('bench_ph_d')
UNION ALL SELECT 'faction A: segment system+faction, 7-day', pg_size_pretty(before_compression_total_bytes), pg_size_pretty(after_compression_total_bytes), round(before_compression_total_bytes::numeric / NULLIF(after_compression_total_bytes, 0), 1) FROM hypertable_compression_stats('bench_fh_a')
UNION ALL SELECT 'faction B: segment system, order faction,ts, 7-day', pg_size_pretty(before_compression_total_bytes), pg_size_pretty(after_compression_total_bytes), round(before_compression_total_bytes::numeric / NULLIF(after_compression_total_bytes, 0), 1) FROM hypertable_compression_stats('bench_fh_b');

\echo === RESULT: query timings (psql prints "Time:" after each statement) - compare A/B/C/D and the two faction layouts
\timing on
\echo -- Q1: one commodity at the busiest station, whole window
SELECT count(*), avg(buy_price) FROM bench_ph_a WHERE market_id = :hub_market AND commodity_id = :hub_commodity;
SELECT count(*), avg(buy_price) FROM bench_ph_b WHERE market_id = :hub_market AND commodity_id = :hub_commodity;
SELECT count(*), avg(buy_price) FROM bench_ph_c WHERE market_id = :hub_market AND commodity_id = :hub_commodity;
SELECT count(*), avg(buy_price) FROM bench_ph_d WHERE market_id = :hub_market AND commodity_id = :hub_commodity;
\echo -- Q2: all commodities at the busiest station, last day
SELECT count(*) FROM bench_ph_a WHERE market_id = :hub_market AND ts >= now() - INTERVAL '1 day';
SELECT count(*) FROM bench_ph_b WHERE market_id = :hub_market AND ts >= now() - INTERVAL '1 day';
SELECT count(*) FROM bench_ph_c WHERE market_id = :hub_market AND ts >= now() - INTERVAL '1 day';
SELECT count(*) FROM bench_ph_d WHERE market_id = :hub_market AND ts >= now() - INTERVAL '1 day';
\echo -- Q3: one faction in the busiest system, whole window
SELECT count(*), avg(influence) FROM bench_fh_a WHERE system_id64 = :hub_system AND faction = (SELECT faction FROM bench_fh_a WHERE system_id64 = :hub_system LIMIT 1);
SELECT count(*), avg(influence) FROM bench_fh_b WHERE system_id64 = :hub_system AND faction = (SELECT faction FROM bench_fh_b WHERE system_id64 = :hub_system LIMIT 1);
\timing off

\if :{?keep}
  \echo bench_* tables kept (drop them with: DROP TABLE bench_ph_a, bench_ph_b, bench_ph_c, bench_ph_d, bench_fh_a, bench_fh_b)
\else
  DROP TABLE bench_ph_a, bench_ph_b, bench_ph_c, bench_ph_d, bench_fh_a, bench_fh_b;
  \echo bench_* tables dropped. Production tables were only read.
\endif
