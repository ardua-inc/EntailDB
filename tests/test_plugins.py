"""The claim under test: a new database or model provider is one dropped-in file.

Every test here works by *writing a module into the package directory at run
time* and asserting it becomes fully available — registered, buildable, and
offered by the settings page — without any existing file being edited. That is
the only honest way to test a plugin architecture. Asserting that the four
built-ins are present would prove nothing about the fifth.

Each test cleans up after itself, because a plugin left behind would leak into
every later test in the session.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from app import connectors, providers

DB_PLUGIN = '''
"""A fictional database, added only to prove the architecture."""

from typing import Any

from fidelity.profiler import SQLiteDialect

from .base import BaseConnector, register


@register
class WidgetDBConnector(BaseConnector):
    kind = "widgetdb"
    label = "WidgetDB"
    default_port = 9999

    @staticmethod
    def dsn_for(f: dict[str, Any]) -> dict[str, Any]:
        return {"endpoint": f["host"], "catalog": f["database"]}

    def open(self) -> Any:
        raise NotImplementedError("no server; registration is the point")

    def dialect(self):
        return SQLiteDialect()
'''

PROVIDER_PLUGIN = '''
"""A fictional model provider, added only to prove the architecture."""

from typing import Any, Iterator

from fidelity.runner import ToolSpec, Turn

from .base import RetryingProvider, register


@register
class WidgetAIProvider(RetryingProvider):
    kind = "widgetai"
    label = "WidgetAI"
    model_hint = "widget-1"
    wants_base_url = False

    def __init__(self, api_key: str = "", model: str = "widget-1",
                 base_url: str = "", max_tokens: int = 4096) -> None:
        self.model, self.max_tokens = model, max_tokens

    def _stream(self, system: str, transcript: list[Turn],
                tools: list[ToolSpec]) -> Iterator[tuple[str, Any]]:
        yield "text", "hello from the plugin"
        yield "assistant", Turn(role="assistant", text="hello from the plugin")
'''


@pytest.fixture
def dropped_in(request):
    """Write a module into a package, reload discovery, then remove it."""
    def _drop(package, source: str, name: str, kind: str):
        path = Path(package.__path__[0]) / f"{name}.py"
        path.write_text(source)

        def cleanup():
            path.unlink(missing_ok=True)
            package.REGISTRY.pop(kind, None)
            for cache in list(importlib.sys.modules):
                if cache.endswith(f".{name}"):
                    del importlib.sys.modules[cache]

        request.addfinalizer(cleanup)
        package._discover()
        return path

    return _drop


# ── a new database ────────────────────────────────────────────────────────

def test_a_dropped_in_database_registers_itself(dropped_in):
    assert "widgetdb" not in connectors.kinds()
    dropped_in(connectors, DB_PLUGIN, "widgetdb", "widgetdb")
    assert "widgetdb" in connectors.kinds()


def test_a_dropped_in_database_is_buildable(dropped_in):
    dropped_in(connectors, DB_PLUGIN, "widgetdb", "widgetdb")
    built = connectors.Connector("widgetdb", {"endpoint": "x"}, "cat")
    assert built.kind == "widgetdb"
    assert built.dialect() is not None


def test_a_dropped_in_database_supplies_its_own_dsn_shape(dropped_in):
    """The shapes used to be a chain of `if kind ==` in `config.py`, so adding
    a database edited a file that has nothing to do with it."""
    dropped_in(connectors, DB_PLUGIN, "widgetdb", "widgetdb")
    dsn = connectors.dsn_for("widgetdb", {
        "host": "widget.internal", "port": 9999, "database": "main",
        "user": "u", "password": "p"})
    assert dsn == {"endpoint": "widget.internal", "catalog": "main"}


def test_a_dropped_in_database_reaches_the_settings_page(dropped_in):
    """The part that makes "drop it in" true rather than nearly true: a plugin
    the UI cannot offer is a plugin nobody can use."""
    dropped_in(connectors, DB_PLUGIN, "widgetdb", "widgetdb")
    entry = next(d for d in connectors.catalogue() if d["kind"] == "widgetdb")
    assert entry["label"] == "WidgetDB"
    assert entry["default_port"] == 9999


def test_a_stored_connection_of_a_dropped_in_kind_resolves(dropped_in):
    from app import config

    dropped_in(connectors, DB_PLUGIN, "widgetdb", "widgetdb")
    conn = config.Connection(id="c", label="w", kind="widgetdb",
                             host="widget.internal", port=9999,
                             database="main", user="u")
    assert conn.dsn() == {"endpoint": "widget.internal", "catalog": "main"}


# ── a new model provider ──────────────────────────────────────────────────

def test_a_dropped_in_provider_registers_itself(dropped_in):
    assert "widgetai" not in providers.kinds()
    dropped_in(providers, PROVIDER_PLUGIN, "widgetai", "widgetai")
    assert "widgetai" in providers.kinds()


def test_a_dropped_in_provider_runs_a_turn(dropped_in):
    """End to end through the real runner: registration alone is not use."""
    from fidelity.runner import FidelityRunner

    dropped_in(providers, PROVIDER_PLUGIN, "widgetai", "widgetai")
    built = providers.build("widgetai", model="widget-1")
    events = list(FidelityRunner(built, tools=[]).run(
        [{"role": "user", "content": "hi"}]))
    assert "".join(e.text for e in events if e.type == "text") == \
        "hello from the plugin"


def test_a_dropped_in_provider_reaches_the_settings_page(dropped_in):
    dropped_in(providers, PROVIDER_PLUGIN, "widgetai", "widgetai")
    entry = next(p for p in providers.catalogue() if p["kind"] == "widgetai")
    assert entry["label"] == "WidgetAI"
    assert entry["model_hint"] == "widget-1"
    assert entry["wants_base_url"] is False


def test_a_dropped_in_provider_is_accepted_by_the_api(dropped_in, tmp_path,
                                                     monkeypatch):
    """The API validates against the registry, not a hard-coded tuple of kinds
    that a plugin author would have to find and edit."""
    from fastapi.testclient import TestClient

    from app import config, main

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "STORE", tmp_path / "connections.json")
    monkeypatch.setattr(config, "KEYFILE", tmp_path / "secret.key")

    dropped_in(providers, PROVIDER_PLUGIN, "widgetai", "widgetai")
    r = TestClient(main.app).post("/api/profiles", json={
        "label": "plugin", "kind": "widgetai", "model": "widget-1"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "widgetai"


def test_the_api_still_refuses_a_kind_nobody_installed(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import config, main

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "STORE", tmp_path / "connections.json")
    monkeypatch.setattr(config, "KEYFILE", tmp_path / "secret.key")

    r = TestClient(main.app).post("/api/profiles", json={
        "label": "x", "kind": "not-installed", "model": "m"})
    assert r.status_code == 400


# ── the registries refuse nonsense ────────────────────────────────────────

def test_a_provider_without_a_kind_is_refused():
    with pytest.raises(ValueError, match="must declare a `kind`"):
        providers.register(type("Nameless", (), {"kind": ""}))


def test_a_duplicate_database_kind_is_refused():
    """Two drivers claiming one name would make which-one-answers depend on
    import order."""
    with pytest.raises(ValueError, match="duplicate"):
        connectors.register(type("Clash", (), {"kind": "postgres"}))


def test_an_unknown_kind_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError, match="unsupported database kind"):
        connectors.Connector("oracle", {})
    with pytest.raises(ValueError, match="unsupported provider kind"):
        providers.build("gemini", model="m")


# ── the built-ins still arrive by the same route ──────────────────────────

def test_every_built_in_is_registered_by_discovery():
    assert set(connectors.kinds()) == {"postgres", "mysql", "sqlserver", "sqlite", "mongodb"}
    assert set(providers.kinds()) == {"anthropic", "openai", "openai_responses"}


def test_every_registered_kind_is_offered_by_the_settings_page():
    """Nothing installed may be invisible: that is how SQLite shipped three
    versions before the connection form ever listed it."""
    assert {d["kind"] for d in connectors.catalogue()} == set(connectors.kinds())
    assert {p["kind"] for p in providers.catalogue()} == set(providers.kinds())
