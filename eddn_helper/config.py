"""Runtime settings: where the database and runtime files live."""

import os
from pathlib import Path

ENV_URL = "ELITE_HELPER_DATABASE_URL"


def data_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "elite-helper"


def default_url():
    return f"sqlite:///{(data_dir() / 'elite.sqlite').as_posix()}"


def database_url(explicit=None):
    """Resolution order: explicit (--db-url) > env var > default SQLite file in the data dir."""
    url = explicit or os.environ.get(ENV_URL) or default_url()
    if "://" not in url:  # bare path
        url = f"sqlite:///{Path(url).as_posix()}"
    return url


def sqlite_path(url):
    """Filesystem path for a sqlite URL, or None for other backends / in-memory."""
    from sqlalchemy.engine import make_url
    u = make_url(url)
    if u.get_backend_name() != "sqlite" or not u.database or u.database == ":memory:":
        return None
    return Path(u.database)


def runtime_dir(url):
    """Directory for the lock file and log: next to the SQLite file, else the default data dir."""
    p = sqlite_path(url)
    return p.parent if p else data_dir()
