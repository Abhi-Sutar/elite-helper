"""
Ship builds: mass, jump range, power budget and priorities, suggestions and a jump-range optimizer.

Ship and module numbers come from EDCD/coriolis-data (the dataset behind coriolis.io and edsy.org), downloaded once into
the data dir (`data.py`). A build comes from the commander's own journal Loadout or from pasted Loadout JSON
(`model.py`); `calc.py` is pure maths, `optimize.py` / `suggest.py` search, `api.py` returns plain dicts for the CLI and MCP.
"""
