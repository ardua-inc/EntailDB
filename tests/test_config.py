"""Tests for the settings store.

The property that matters most here is negative: a secret must not appear in
plaintext anywhere it could be read by accident — not in the file on disk, not
in the JSON sent to the browser. Those are asserted by searching for the actual
secret value rather than by checking a flag, because a flag can be right while
the value leaks through some other field.

`encrypt()` is deliberately weak and says so in its own docstring. The tests
below assert what it actually provides — no plaintext at rest — and do not
pretend it provides confidentiality against someone holding the account.
"""

from __future__ import annotations

import json
import stat

import pytest

from app import config


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Redirect the module-level paths at a temp directory."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "STORE", tmp_path / "connections.json")
    monkeypatch.setattr(config, "KEYFILE", tmp_path / "secret.key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


def a_connection(**kw):
    base = dict(id="c1", label="Warehouse", kind="postgres", host="db.internal",
                port=5432, database="analytics", user="reader")
    base.update(kw)
    return config.Connection(**base)


# ── secrets at rest ───────────────────────────────────────────────────────

def test_a_secret_round_trips():
    assert config.decrypt(config.encrypt("hunter2")) == "hunter2"


def test_encryption_is_not_deterministic():
    """Two encryptions of one value must not share ciphertext.

    The version of this test that shipped compared whole blobs and passed while
    the scheme was badly broken: the old keystream was `key || nonce`, so a
    secret shorter than the 32-byte key never mixed the nonce in at all, and
    only the stored random prefix differed. Identical secrets had identical
    ciphertext. Comparing the *bodies* is what the property actually is.
    """
    import base64

    a, b = config.encrypt("same"), config.encrypt("same")
    assert a != b
    body_a = base64.urlsafe_b64decode(a[len(config._V2):])[12:]
    body_b = base64.urlsafe_b64decode(b[len(config._V2):])[12:]
    assert body_a != body_b


def test_ciphertext_is_authenticated():
    """A tampered secret must fail loudly rather than decrypt to noise."""
    import base64

    blob = config.encrypt("hunter2")
    raw = bytearray(base64.urlsafe_b64decode(blob[len(config._V2):]))
    raw[-1] ^= 0x01
    tampered = config._V2 + base64.urlsafe_b64encode(bytes(raw)).decode()
    with pytest.raises(Exception):
        config.decrypt(tampered)


def test_a_secret_written_by_the_old_scheme_is_still_readable():
    """An existing install must keep working across the change."""
    legacy = config._legacy_encrypt_for_test("hunter2")
    assert config.is_legacy(legacy)
    assert config.decrypt(legacy) == "hunter2"


def test_old_secrets_are_rewritten_on_first_load(store):
    conn = a_connection(password=config._legacy_encrypt_for_test("pw"))
    settings = config.Settings(
        connections=[conn],
        anthropic_api_key=config._legacy_encrypt_for_test("sk-ant-old"))
    config.save(settings)

    loaded = config.load()
    assert not config.is_legacy(loaded.connections[0].password)
    assert not config.is_legacy(loaded.anthropic_api_key)
    assert loaded.api_key() == "sk-ant-old"
    assert loaded.connections[0].dsn()["password"] == "pw"
    # ...and the rewrite reached disk, not just this object.
    assert "v2:" in (store / "connections.json").read_text()


@pytest.mark.parametrize("value", ["", "a", "pä55wörd — ünicode", "x" * 500])
def test_awkward_values_round_trip(value):
    assert config.decrypt(config.encrypt(value)) == value


def test_the_keyfile_is_not_world_readable(store):
    config.encrypt("x")
    mode = stat.S_IMODE((store / "secret.key").stat().st_mode)
    assert mode == 0o600


def test_the_key_is_reused_across_calls(store):
    first = config.encrypt("x")
    key_before = (store / "secret.key").read_bytes()
    assert config.decrypt(first) == "x"
    assert (store / "secret.key").read_bytes() == key_before


def test_no_plaintext_password_reaches_the_file(store):
    settings = config.Settings(
        connections=[a_connection(password=config.encrypt("s3cr3t-pw"))],
        anthropic_api_key=config.encrypt("sk-ant-secret-key"),
    )
    config.save(settings)
    raw = (store / "connections.json").read_text()
    assert "s3cr3t-pw" not in raw
    assert "sk-ant-secret-key" not in raw


def test_the_store_is_not_world_readable(store):
    config.save(config.Settings())
    mode = stat.S_IMODE((store / "connections.json").stat().st_mode)
    assert mode == 0o600


def test_settings_survive_a_save_and_load(store):
    original = config.Settings(
        connections=[a_connection(password=config.encrypt("pw"), facts="# facts")],
        anthropic_api_key=config.encrypt("sk-ant-x"),
        model="claude-opus-5",
    )
    config.save(original)
    loaded = config.load()
    assert loaded.model == "claude-opus-5"
    assert loaded.api_key() == "sk-ant-x"
    assert loaded.connections[0].dsn()["password"] == "pw"
    assert loaded.connections[0].facts == "# facts"


def test_a_missing_store_loads_as_empty():
    assert config.load().connections == []


# ── what the browser is allowed to see ────────────────────────────────────

def test_the_public_view_carries_no_secret():
    conn = a_connection(password=config.encrypt("s3cr3t-pw"))
    public = conn.public()
    body = json.dumps(public)
    assert "s3cr3t-pw" not in body
    assert conn.password not in body          # nor the ciphertext
    assert public["password"] == config.REDACTED


def test_an_absent_password_is_not_shown_as_redacted():
    """A blank field must read as blank, or the settings page implies a
    password is set when none is."""
    assert a_connection(password="").public()["password"] == ""


def test_the_public_view_reports_facts_as_a_flag_not_a_blob():
    conn = a_connection(facts="# a long generated document\n" * 200)
    assert conn.public()["facts"] is True
    assert a_connection().public()["facts"] is False


# ── DSN shapes ────────────────────────────────────────────────────────────

def test_each_driver_gets_the_keys_it_expects():
    pwd = config.encrypt("pw")
    assert set(a_connection(kind="postgres", password=pwd).dsn()) == {
        "host", "port", "dbname", "user", "password"}
    assert set(a_connection(kind="mysql", password=pwd).dsn()) == {
        "host", "port", "database", "user", "password"}
    assert set(a_connection(kind="sqlserver", password=pwd).dsn()) == {
        "server", "port", "database", "user", "password"}


def test_sqlite_is_a_path_and_carries_no_credentials():
    dsn = a_connection(kind="sqlite", database="/data/local.sqlite3").dsn()
    assert dsn == {"path": "/data/local.sqlite3"}


def test_an_unknown_kind_is_refused_rather_than_defaulted():
    """It used to return the SQL Server shape for anything unrecognised,
    which would have handed a driver keys it does not take."""
    with pytest.raises(ValueError):
        a_connection(kind="oracle").dsn()


# ── the API key ───────────────────────────────────────────────────────────

def test_the_environment_supplies_the_key_when_the_store_has_none(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    assert config.Settings().api_key() == "sk-ant-from-env"


def test_the_stored_key_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    settings = config.Settings(anthropic_api_key=config.encrypt("sk-ant-stored"))
    assert settings.api_key() == "sk-ant-stored"


def test_no_key_anywhere_is_empty_not_an_error():
    assert config.Settings().api_key() == ""


# ── model profiles ────────────────────────────────────────────────────────

def a_profile(**kw):
    base = dict(id="p1", label="strong", kind="anthropic", model="claude-sonnet-5")
    base.update(kw)
    return config.ModelProfile(**base)


def test_a_profile_round_trips(store):
    settings = config.Settings(
        profiles=[a_profile(api_key=config.encrypt("sk-secret-model-key"))],
        default_profile_id="p1",
    )
    config.save(settings)
    loaded = config.load()
    assert loaded.default_profile_id == "p1"
    assert loaded.profiles[0].key() == "sk-secret-model-key"


def test_no_plaintext_model_key_reaches_the_file(store):
    config.save(config.Settings(
        profiles=[a_profile(api_key=config.encrypt("sk-secret-model-key"))]))
    assert "sk-secret-model-key" not in (store / "connections.json").read_text()


def test_the_public_profile_carries_no_key():
    profile = a_profile(api_key=config.encrypt("sk-secret-model-key"))
    public = profile.public()
    assert public["api_key"] == config.REDACTED
    assert "sk-secret-model-key" not in json.dumps(public)


def test_a_keyless_endpoint_is_flagged_local():
    """A local model needs no key; an empty box otherwise reads as something
    missing rather than something not required."""
    assert a_profile(kind="openai", base_url="http://localhost:11434/v1").public()["local"]
    assert not a_profile(api_key=config.encrypt("k")).public()["local"]


# ── resolution order ──────────────────────────────────────────────────────

def test_a_connection_without_a_pin_uses_the_default():
    settings = config.Settings(
        profiles=[a_profile(id="p1"), a_profile(id="p2", label="local")],
        default_profile_id="p2",
    )
    assert settings.for_connection(a_connection()).id == "p2"


def test_a_pinned_connection_overrides_the_default():
    """The privacy case: one database stays on a local model while the rest
    use a frontier one."""
    settings = config.Settings(
        profiles=[a_profile(id="cloud"), a_profile(id="local", kind="openai",
                                                   base_url="http://localhost:11434/v1")],
        default_profile_id="cloud",
    )
    pinned = a_connection(model_profile_id="local")
    assert settings.for_connection(pinned).id == "local"
    assert settings.for_connection(a_connection()).id == "cloud"


def test_a_pin_to_a_deleted_profile_falls_back_rather_than_failing():
    settings = config.Settings(profiles=[a_profile(id="p1")], default_profile_id="p1")
    assert settings.for_connection(a_connection(model_profile_id="gone")).id == "p1"


def test_no_profiles_at_all_resolves_to_none():
    assert config.Settings().for_connection(a_connection()) is None


# ── migration from the pre-profiles shape ─────────────────────────────────

def test_a_legacy_install_gains_a_profile_on_load(store):
    """A working configuration must keep working across this refactor. Left
    alone it would have no profiles and answer nothing."""
    (store / "connections.json").write_text(json.dumps({
        "connections": [],
        "anthropic_api_key": config.encrypt("sk-ant-existing"),
        "model": "claude-sonnet-5",
    }))
    loaded = config.load()
    assert len(loaded.profiles) == 1
    assert loaded.profiles[0].kind == "anthropic"
    assert loaded.profiles[0].model == "claude-sonnet-5"
    assert loaded.profiles[0].key() == "sk-ant-existing"
    assert loaded.default_profile_id == loaded.profiles[0].id


def test_migration_does_not_overwrite_real_profiles(store):
    (store / "connections.json").write_text(json.dumps({
        "connections": [],
        "profiles": [{"id": "mine", "label": "mine", "kind": "openai",
                      "model": "qwen3.6", "base_url": "http://localhost:11434/v1"}],
        "default_profile_id": "mine",
        "anthropic_api_key": config.encrypt("sk-ant-existing"),
        "model": "claude-sonnet-5",
    }))
    loaded = config.load()
    assert [p.id for p in loaded.profiles] == ["mine"]


def test_migration_keeps_the_legacy_fields(store):
    """Not deleted, so a downgrade still finds what it expects."""
    (store / "connections.json").write_text(json.dumps({
        "connections": [], "anthropic_api_key": config.encrypt("sk-x"),
        "model": "claude-sonnet-5"}))
    config.save(config.load())
    raw = json.loads((store / "connections.json").read_text())
    assert raw["anthropic_api_key"]
    assert raw["model"] == "claude-sonnet-5"


def test_an_empty_install_gets_no_invented_profile(store):
    """Nothing configured must stay nothing, not a profile with no key."""
    assert config.load().profiles == []
