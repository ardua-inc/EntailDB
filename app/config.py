"""Connection settings, stored on disk with secrets encrypted at rest.

Stage 1 of the security posture in `DESIGN.md`: the app binds to loopback and
has no login, so this protects the file rather than the endpoint. That
distinction is worth being explicit about — encrypting the store does **not**
make the app safe to expose, and auth is the gate before it goes anywhere but
localhost.

The key lives beside the store in a `0600` file, generated on first use. That
defends against the realistic local threat (a config file in a backup, a synced
folder, a screen-share) and not against an attacker who already has the
account. Anything stronger needs a passphrase the user types, which is a real
feature and not this one.
"""

from __future__ import annotations

import base64
import json
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("FIDELITY_DATA", "/data"))
STORE = DATA_DIR / "connections.json"
KEYFILE = DATA_DIR / "secret.key"

SECRET_FIELDS = ("password", "api_key")
REDACTED = "••••••••"

DEFAULT_MODEL = "claude-sonnet-5"


def _key() -> bytes:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if KEYFILE.exists():
        return base64.urlsafe_b64decode(KEYFILE.read_bytes())
    key = secrets.token_bytes(32)
    KEYFILE.write_bytes(base64.urlsafe_b64encode(key))
    KEYFILE.chmod(0o600)
    return key


# ── secrets at rest ───────────────────────────────────────────────────────
#
# AES-GCM, keyed from the 0600 keyfile beside the store.
#
# What stood here was a keyed XOR, and it was worse than its own docstring
# admitted. The keystream was `key || nonce`, so for any secret shorter than
# the 32-byte key the nonce never entered the keystream at all: two identical
# secrets produced byte-identical ciphertext, and only the stored nonce prefix
# made the blobs look different. That leaks equality between secrets, and any
# known plaintext hands over that many bytes of the key directly. A test here
# asserted `encrypt(x) != encrypt(x)` and passed the whole time, because it was
# comparing the random prefix rather than the ciphertext.
#
# Old values are still readable and are rewritten on the next save, so an
# existing store keeps working without anyone re-entering a key.

_V2 = "v2:"


def encrypt(value: str) -> str:
    """Encrypt a secret for storage. Authenticated, and not deterministic."""
    nonce = secrets.token_bytes(12)
    sealed = AESGCM(_key()).encrypt(nonce, value.encode(), None)
    return _V2 + base64.urlsafe_b64encode(nonce + sealed).decode()


def decrypt(blob: str) -> str:
    """Decrypt a stored secret, in either format."""
    if not blob:
        return ""
    if blob.startswith(_V2):
        raw = base64.urlsafe_b64decode(blob[len(_V2):])
        return AESGCM(_key()).decrypt(raw[:12], raw[12:], None).decode()
    return _decrypt_legacy(blob)


def is_legacy(blob: str) -> bool:
    """True for a secret still stored under the superseded scheme."""
    return bool(blob) and not blob.startswith(_V2)


def _xor(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _legacy_encrypt_for_test(value: str) -> str:
    """Produce ciphertext in the superseded format. Tests only — kept here so
    the migration path is exercised against the real thing rather than a
    hand-rolled imitation of it."""
    nonce = secrets.token_bytes(8)
    return base64.urlsafe_b64encode(
        nonce + _xor(value.encode(), _key() + nonce)).decode()


def _decrypt_legacy(blob: str) -> str:
    """Read a secret written by the keyed-XOR scheme this replaced."""
    raw = base64.urlsafe_b64decode(blob)
    nonce, body = raw[:8], raw[8:]
    return _xor(body, _key() + nonce).decode()


@dataclass
class ModelProfile:
    """A named model, and how to reach it.

    The label is the user's own word — "strong", "private (local)", "cheap".
    Naming the *profile* rather than selecting a vendor model id everywhere is
    the one idea worth taking from the ardua-ai platform: what a request needs
    is a property of the request, and which model satisfies it should be
    changeable in one place without touching anything that asks.
    """

    id: str
    label: str
    kind: str                       # anthropic | openai | openai_responses
    model: str
    base_url: str = ""              # blank uses the provider default
    api_key: str = ""               # encrypted on disk
    max_tokens: int = 4096

    def key(self) -> str:
        return decrypt(self.api_key) if self.api_key else ""

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["api_key"] = REDACTED if self.api_key else ""
        # A local endpoint needs no key, and saying so beats an empty box that
        # looks like something is missing.
        d["local"] = bool(self.base_url) and not self.api_key
        return d


@dataclass
class Connection:
    id: str
    label: str
    kind: str                       # postgres | mysql | sqlserver | sqlite
    host: str
    port: int
    database: str
    user: str
    password: str = ""              # encrypted on disk
    facts: str = ""                 # generated profiler document
    # Blank inherits the default profile. Set, it pins this database to one
    # model -- which is how a sensitive database is kept on a local model so
    # its schema and rows never leave the machine, while other connections go
    # on using a frontier model.
    model_profile_id: str = ""

    def dsn(self) -> dict[str, Any]:
        """Connection arguments, in whatever shape this driver wants.

        The shapes used to live here as a chain of `if kind ==`, which meant
        adding a database edited this file. The driver owns its own shape now;
        this only supplies the stored fields.
        """
        from .connectors import dsn_for

        return dsn_for(self.kind, {
            "host": self.host, "port": self.port, "database": self.database,
            "user": self.user,
            "password": decrypt(self.password) if self.password else "",
        })

    def public(self) -> dict[str, Any]:
        """Safe to send to a browser: no secret, no generated facts blob."""
        d = asdict(self)
        d["password"] = REDACTED if self.password else ""
        d["facts"] = bool(self.facts)
        return d


@dataclass
class Settings:
    connections: list[Connection] = field(default_factory=list)
    profiles: list[ModelProfile] = field(default_factory=list)
    default_profile_id: str = ""
    anthropic_api_key: str = ""     # legacy; migrated into a profile on load
    model: str = DEFAULT_MODEL      # legacy; migrated into a profile on load

    def api_key(self) -> str:
        if self.anthropic_api_key:
            return decrypt(self.anthropic_api_key)
        return os.environ.get("ANTHROPIC_API_KEY", "")

    def profile(self, profile_id: str = "") -> ModelProfile | None:
        """Resolve a profile id, falling back to the default.

        The order is the whole point of per-connection binding: a connection's
        own choice wins, and only an unset one inherits.
        """
        wanted = profile_id or self.default_profile_id
        for p in self.profiles:
            if p.id == wanted:
                return p
        return self.profiles[0] if self.profiles else None

    def for_connection(self, connection: Connection) -> ModelProfile | None:
        return self.profile(connection.model_profile_id)


def load() -> Settings:
    if not STORE.exists():
        return Settings()
    raw = json.loads(STORE.read_text())
    settings = Settings(
        connections=[Connection(**c) for c in raw.get("connections", [])],
        profiles=[ModelProfile(**p) for p in raw.get("profiles", [])],
        default_profile_id=raw.get("default_profile_id", ""),
        anthropic_api_key=raw.get("anthropic_api_key", ""),
        model=raw.get("model", DEFAULT_MODEL),
    )
    _migrate(settings)
    if _upgrade_secrets(settings):
        # Rewritten once, on the first load after the scheme changed, so an
        # existing install stops holding ciphertext from the superseded one
        # without anyone re-entering a credential.
        save(settings)
    return settings


def _upgrade_secrets(settings: Settings) -> bool:
    """Re-encrypt anything still stored under the old scheme."""
    changed = False
    holders: list[tuple[Any, str]] = [(settings, "anthropic_api_key")]
    holders += [(c, "password") for c in settings.connections]
    holders += [(p, "api_key") for p in settings.profiles]
    for holder, field_name in holders:
        blob = getattr(holder, field_name, "")
        if not is_legacy(blob):
            continue
        try:
            setattr(holder, field_name, encrypt(_decrypt_legacy(blob)))
            changed = True
        except Exception:
            # An unreadable secret is left exactly as found. Overwriting it
            # with a re-encrypted guess would destroy the only copy.
            continue
    return changed


def _migrate(settings: Settings) -> None:
    """Turn a pre-profiles install into one with a profile.

    An existing configuration holds `anthropic_api_key` and `model` and nothing
    else. Left alone it would have no profiles and answer nothing, so a working
    setup would break on upgrade -- which is not an acceptable way to ship a
    refactor. The legacy fields are kept rather than deleted, so a downgrade
    still finds what it expects.
    """
    if settings.profiles:
        return
    if not (settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")):
        return
    settings.profiles = [ModelProfile(
        id="default", label=settings.model or DEFAULT_MODEL, kind="anthropic",
        model=settings.model or DEFAULT_MODEL,
        api_key=settings.anthropic_api_key,
    )]
    settings.default_profile_id = "default"


def save(settings: Settings) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps({
        "connections": [asdict(c) for c in settings.connections],
        "profiles": [asdict(p) for p in settings.profiles],
        "default_profile_id": settings.default_profile_id,
        "anthropic_api_key": settings.anthropic_api_key,
        "model": settings.model,
    }, indent=2))
    STORE.chmod(0o600)
