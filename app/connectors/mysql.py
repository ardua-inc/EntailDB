"""MySQL and MariaDB."""

from __future__ import annotations

from typing import Any

from fidelity.profiler import Dialect, MySQLDialect

from .base import BaseConnector, register


@register
class MySQLConnector(BaseConnector):
    kind = "mysql"
    label = "MySQL / MariaDB"
    default_port = 3306

    @staticmethod
    def dsn_for(f: dict[str, Any]) -> dict[str, Any]:
        return {"host": f["host"], "port": f["port"], "database": f["database"],
                "user": f["user"], "password": f["password"]}

    def open(self) -> Any:
        import pymysql
        conn = pymysql.connect(**self.dsn)
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
        return conn

    def dialect(self) -> Dialect:
        # MySQL conflates schema and database, so the dialect needs to know
        # which database it is qualifying against.
        return MySQLDialect(self.database)
