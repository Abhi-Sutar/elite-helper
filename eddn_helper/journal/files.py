"""Locating and reading Elite Dangerous journal files. Stdlib only."""

import glob
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ENV_JOURNAL_DIR = "ELITE_JOURNAL_DIR"
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class JournalNotFound(Exception):
    pass


def default_journal_dir():
    """The standard Saved Games folder if it exists, else the current directory (kept from ed_credit_rate.py)."""
    win_path = os.path.join(os.path.expanduser("~"), "Saved Games", "Frontier Developments", "Elite Dangerous")
    return win_path if os.path.isdir(win_path) else os.getcwd()


def journal_dir(explicit=None):
    """Resolution order: explicit (--journal-dir) > ELITE_JOURNAL_DIR > the default Saved Games folder."""
    return str(explicit or os.environ.get(ENV_JOURNAL_DIR) or default_journal_dir())


def find_journal_files(directory):
    """Journal.<timestamp>.NN.log files, oldest first (the names sort chronologically)."""
    return sorted(glob.glob(os.path.join(directory, "Journal.*.log")))


def require_journal_files(directory):
    files = find_journal_files(directory)
    if not files:
        raise JournalNotFound(f"No journal files found in: {directory}. Pass --journal-dir or set {ENV_JOURNAL_DIR} "
                              f"if your journals live elsewhere.")
    return files


def parse_timestamp(ts_str):
    return datetime.strptime(ts_str, TS_FORMAT).replace(tzinfo=timezone.utc)


def iso(dt):
    return None if dt is None else dt.strftime(TS_FORMAT)


def read_events(path):
    """
    (datetime, event_dict) for one journal file, in order. Lines that are blank, not JSON, or lack a valid
    timestamp are skipped, so a half-written last line (the game is appending) is harmless.
    """
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = e.get("timestamp") if isinstance(e, dict) else None
            if not ts:
                continue
            try:
                dt = parse_timestamp(ts)
            except ValueError:
                continue
            out.append((dt, e))
    return out


def read_json_file(directory, name):
    """A companion file such as Status.json: (parsed dict, modified-time datetime) or (None, None) if unreadable."""
    path = Path(directory) / name
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return json.loads(path.read_text(encoding="utf-8")), mtime
    except (OSError, ValueError):
        return None, None
