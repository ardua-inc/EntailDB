"""SQLite — a file, not a server."""

from __future__ import annotations

from typing import Any

from fidelity.profiler import Dialect, SQLiteDialect

from .base import BaseConnector, register


@register
class SQLiteConnector(BaseConnector):
    kind = "sqlite"
    label = "SQLite (file)"
    default_port = 0
    # Host, port and credentials are meaningless for a file; the settings page
    # hides them rather than showing four boxes that do nothing.
    wants_credentials = False

    @staticmethod
    def dsn_for(f: dict[str, Any]) -> dict[str, Any]:
        return {"path": f["database"]}

    def open(self) -> Any:
        import sqlite3
        # `mode=ro` is enforced by the driver, below the SQL layer, so it holds
        # even if the statement gate is ever wrong. The server products have no
        # equivalent a connection string can express.
        return sqlite3.connect(f"file:{self.dsn['path']}?mode=ro",
                               uri=True, check_same_thread=False)

    def dialect(self) -> Dialect:
        return SQLiteDialect()
