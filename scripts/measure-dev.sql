-- Change-rate measurement on elite_dev (changes mode). Run: psql -U elite_writer -d elite_dev -f scripts\measure-dev.sql
\pset pager off
\echo == market updates since the fixed listener started
SELECT count(*) AS updates, count(DISTINCT market_id) AS stations, sum(commodities) AS readings, sum(changed) AS rows_written,
       round(100.0 * sum(changed) / nullif(sum(commodities), 0), 2) AS pct_written,
       round(100.0 * count(*) FILTER (WHERE uploader = '') / count(*), 1) AS pct_blank_uploader,
       count(DISTINCT uploader) AS uploaders, min(ts) AS first_ts, max(ts) AS last_ts
FROM market_update WHERE ts >= '2026-10-10 23:33:30+02';

\echo == first vs repeat update of a station within the window
WITH u AS (SELECT *, row_number() OVER (PARTITION BY market_id ORDER BY ts) AS n
           FROM market_update WHERE ts >= '2026-10-10 23:33:30+02')
SELECT CASE WHEN n = 1 THEN 'first' ELSE 'repeat' END AS kind, count(*) AS updates, sum(commodities) AS readings,
       sum(changed) AS rows_written, round(100.0 * sum(changed) / nullif(sum(commodities), 0), 2) AS pct_written,
       round(avg(commodities), 0) AS avg_items
FROM u GROUP BY 1 ORDER BY 1;

\echo == repeat updates: share that wrote nothing, and size of partial reports
WITH u AS (SELECT *, row_number() OVER (PARTITION BY market_id ORDER BY ts) AS n
           FROM market_update WHERE ts >= '2026-10-10 23:33:30+02')
SELECT count(*) AS repeat_updates, count(*) FILTER (WHERE changed = 0) AS wrote_nothing,
       count(*) FILTER (WHERE commodities < 20) AS partial_lt20
FROM u WHERE n > 1;

\echo == top 5 stations by updates
SELECT m.station, m.system_name, count(*) AS updates, sum(u.changed) AS rows_written, count(DISTINCT u.uploader) AS uploaders
FROM market_update u JOIN markets m USING (market_id)
WHERE u.ts >= '2026-10-10 23:33:30+02' GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 5;

\echo == system updates (factions)
SELECT count(*) AS updates, count(DISTINCT system_id64) AS systems, sum(factions) AS faction_readings, sum(changed) AS rows_written,
       round(100.0 * sum(changed) / nullif(sum(factions), 0), 2) AS pct_written
FROM system_update WHERE ts >= '2026-10-10 23:33:30+02';

\echo == history rows written in the window
SELECT (SELECT count(*) FROM price_history WHERE ts >= '2026-10-10 23:33:30+02') AS price_rows,
       (SELECT count(*) FROM faction_history WHERE ts >= '2026-10-10 23:33:30+02') AS faction_rows;

\echo == table sizes
SELECT relname, pg_size_pretty(pg_total_relation_size(oid)) AS size
FROM pg_class WHERE relname IN ('price_history', 'faction_history', 'market_update', 'system_update', 'commodity_prices', 'factions')
ORDER BY pg_total_relation_size(oid) DESC;
