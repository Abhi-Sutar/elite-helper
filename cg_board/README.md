# CG supply board (local)

Run from the repo root with the elite-helper virtualenv (it has sqlalchemy etc.):

    .venv/Scripts/python.exe cg_board/cg_board.py --open

then open http://127.0.0.1:8765/ (--open does it for you).

- Data comes from the eddn_helper package (queries.find_commodity, the same code as the MCP server), so it
  works with SQLite or PostgreSQL. config.json "helper_dir" defaults to the repo root, "database_url" is optional
  (default = whatever eddn_helper uses, or the ELITE_HELPER_DATABASE_URL env var).
- If it says the database schema is out of date, run `python -m eddn_helper migrate upgrade` in the repo
  (the eddn_helper code expects schema 0004; the listener also migrates on start).
- config.json controls everything: target system, ship ranges and hold, station types, and one entry per tab.
- To track a new commodity or CG, add a tab: key, label, commodity (name as find_commodity takes it, e.g.
  "superconductors"), cg_price, max_jumps (or max_ly), optional min_stock / station_types / max_age_hours.
- To change the delivery target, edit "target" (name and exact system name) and the "ship" numbers.
- python cg_board/cg_board.py once          write data.json one time
- python cg_board/cg_board.py reset [tab]   restart the "Change" baseline
- Distance to star (Ls) is not in the database. It is fetched from EDSM once per system and cached in ls_cache.json.
- Score (0-100) orders stations inside each jump section: loads/(loads+25), divided by (1 + Ls/1500), halved for every 12 h of data age beyond 6 h. The constants are at the top of the script in index.html.
