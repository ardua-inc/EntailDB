"""Microsoft SQL Server."""

from __future__ import annotations

from typing import Any

from fidelity.profiler import Dialect, SQLServerDialect

from .base import BaseConnector, register


@register
class SQLServerConnector(BaseConnector):
    kind = "sqlserver"
    label = "SQL Server"
    default_port = 1433
    # SQL Server has no session read-only mode -- no `SET TRANSACTION READ
    # ONLY`, no driver flag. Postgres, MySQL and SQLite each have one, so this
    # is the only product where the statement gate stood alone. Every statement
    # now runs inside a transaction that is rolled back afterwards, so a write
    # that somehow gets past the gate does not survive it.
    #
    # This is mitigation, not the control. The control is a database principal
    # granted SELECT and nothing else, which is a deployment decision and is
    # documented in the README.
    rollback_after_query = True

    @staticmethod
    def dsn_for(f: dict[str, Any]) -> dict[str, Any]:
        return {"server": f["host"], "port": f["port"], "database": f["database"],
                "user": f["user"], "password": f["password"]}

    def open(self) -> Any:
        import pymssql
        # autocommit stays off: the implicit transaction is what makes
        # `rollback_after_query` able to undo anything.
        return pymssql.connect(**self.dsn, autocommit=False)

    def dialect(self) -> Dialect:
        return SQLServerDialect()
