"""Database drivers, discovered rather than enumerated.

Adding a database is one file in this directory: subclass `BaseConnector`,
declare `kind`, `label`, `default_port`, a DSN shape, how to open a connection
and which dialect to use, then decorate with `@register`. Nothing else changes
— not this file, not the API, not the settings page, which builds its list of
database types from `catalogue()`.

The dialect is imported from `fidelity.profiler` for the four built-ins, but a
new driver is free to define its own inline: the profiler depends on the
`Dialect` protocol, not on the library's implementations of it.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from .base import (
    PREVIEW_ROWS,
    REGISTRY,
    BaseConnector,
    QueryResult,
    register,
)


def _discover() -> None:
    for module in pkgutil.iter_modules(__path__):
        if not module.name.startswith("_") and module.name != "base":
            importlib.import_module(f"{__name__}.{module.name}")


_discover()


def kinds() -> list[str]:
    return sorted(REGISTRY)


def catalogue() -> list[dict[str, Any]]:
    """What the settings page needs to offer every registered database."""
    return [{
        "kind": cls.kind,
        "label": cls.label or cls.kind,
        "default_port": cls.default_port,
        "wants_credentials": cls.wants_credentials,
    } for cls in sorted(REGISTRY.values(), key=lambda c: c.label or c.kind)]


def dsn_for(kind: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Connection arguments for a stored connection's fields."""
    cls = _class_for(kind)
    return cls.dsn_for(fields)


def _class_for(kind: str) -> type[BaseConnector]:
    cls = REGISTRY.get(kind)
    if cls is None:
        raise ValueError(f"unsupported database kind: {kind!r}")
    return cls


def Connector(kind: str, dsn: dict[str, Any],
              database: str | None = None) -> BaseConnector:
    """Build the driver for a kind.

    Named as a callable rather than a class so existing call sites read
    unchanged; unknown kinds are refused rather than defaulted, which is how a
    query ends up somewhere nobody intended.
    """
    return _class_for(kind)(kind, dsn, database)


__all__ = ["BaseConnector", "Connector", "QueryResult", "PREVIEW_ROWS",
           "REGISTRY", "catalogue", "dsn_for", "kinds", "register"]
