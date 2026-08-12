"""PostgreSQL."""

from __future__ import annotations

from typing import Any

from fidelity.profiler import Dialect, PostgresDialect

from .base import BaseConnector, register


@register
class PostgresConnector(BaseConnector):
    kind = "postgres"
    label = "PostgreSQL"
    default_port = 5432

    @staticmethod
    def dsn_for(f: dict[str, Any]) -> dict[str, Any]:
        return {"host": f["host"], "port": f["port"], "dbname": f["database"],
                "user": f["user"], "password": f["password"]}

    def open(self) -> Any:
        import psycopg
        conn = psycopg.connect(**self.dsn)
        conn.read_only = True
        return conn

    def dialect(self) -> Dialect:
        return PostgresDialect()
