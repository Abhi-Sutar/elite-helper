-- One-time PostgreSQL setup for elite-helper. Run as the postgres superuser:
--     psql -U postgres -f scripts\pg-setup.sql
-- Safe to re-run. It creates roles, databases and the TimescaleDB extension, but sets NO passwords:
-- afterwards set them yourself (hidden prompt) with
--     psql -U postgres -c "\password elite_writer"
--     psql -U postgres -c "\password elite_reader"
-- and then run scripts\create-pgpass.ps1 so the apps can log in without a password in any URL or task.
--
--   elite_writer : owns the databases; used by the listener, migrations and copy-data
--   elite_reader : SELECT only; used by the MCP server / read-only queries
--   elite        : live data        elite_dev : experiments        elite_test : unit tests (wiped by the tests!)

\set ON_ERROR_STOP on

SELECT 'CREATE ROLE elite_writer LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'elite_writer') \gexec
SELECT 'CREATE ROLE elite_reader LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'elite_reader') \gexec

SELECT 'CREATE DATABASE elite OWNER elite_writer' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'elite') \gexec
SELECT 'CREATE DATABASE elite_dev OWNER elite_writer' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'elite_dev') \gexec
SELECT 'CREATE DATABASE elite_test OWNER elite_writer' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'elite_test') \gexec

\connect elite
CREATE EXTENSION IF NOT EXISTS timescaledb;
GRANT USAGE ON SCHEMA public TO elite_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO elite_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE elite_writer IN SCHEMA public GRANT SELECT ON TABLES TO elite_reader;

\connect elite_dev
CREATE EXTENSION IF NOT EXISTS timescaledb;
GRANT USAGE ON SCHEMA public TO elite_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO elite_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE elite_writer IN SCHEMA public GRANT SELECT ON TABLES TO elite_reader;

\connect elite_test
CREATE EXTENSION IF NOT EXISTS timescaledb;

\connect postgres
\echo
\echo === roles and databases
SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname LIKE 'elite_%' ORDER BY 1;
SELECT datname, pg_get_userbyid(datdba) AS owner FROM pg_database WHERE datname LIKE 'elite%' ORDER BY 1;
\echo
\echo Next: set the two passwords (see the header of this file), then run the create-pgpass.ps1 script
