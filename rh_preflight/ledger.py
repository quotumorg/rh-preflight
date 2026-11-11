"""SQLite history containing only the minimum check metadata."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid


def data_dir():
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir("rh-preflight")


class Ledger:
    def __init__(self, directory=None):
        # Resolve the active profile on every operation, not once at import time.
        self.directory = Path(directory) if directory is not None else None

    def _connect(self):
        directory = self.directory if self.directory is not None else data_dir()
        directory.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(directory / "checks.db", timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS checks (
            id TEXT PRIMARY KEY, time TEXT NOT NULL, session TEXT NOT NULL,
            tool_name TEXT NOT NULL, targets TEXT NOT NULL, cards TEXT NOT NULL,
            rules TEXT NOT NULL, decision TEXT, mode TEXT NOT NULL, disposition TEXT NOT NULL
        )""")
        return conn

    def add(self, session, tool_name, cards, rules, mode, disposition):
        check_id = uuid.uuid4().hex
        conn = self._connect()
        try:
            with conn:
                conn.execute("INSERT INTO checks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                    check_id, datetime.now(timezone.utc).isoformat(), session or "",
                    tool_name, json.dumps([c.target for c in cards]),
                    json.dumps([c.public() for c in cards]), json.dumps(rules), None, mode, disposition))
        finally:
            conn.close()
        return check_id

    def decide(self, check_id, choice):
        if choice not in {"once", "session", "always", "deny", "timeout", "cancelled"}:
            return
        conn = self._connect()
        try:
            with conn:
                conn.execute("UPDATE checks SET decision=? WHERE id=? AND disposition='ask'", (choice, check_id))
        finally:
            conn.close()

    def rows(self, limit=100):
        conn = self._connect()
        try:
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM checks ORDER BY time DESC LIMIT ?", (min(max(int(limit), 1), 500),))]
        finally:
            conn.close()
        for row in rows:
            for key in ("targets", "cards", "rules"):
                row[key] = json.loads(row[key])
        return rows

    def today(self):
        conn = self._connect()
        try:
            row = conn.execute("""SELECT COUNT(*) AS checked,
                COALESCE(SUM(disposition='ask'), 0) AS asked FROM checks WHERE time >= ?""",
                (datetime.now(timezone.utc).date().isoformat(),)).fetchone()
            return dict(row)
        finally:
            conn.close()
