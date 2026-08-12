"""Model adapters, discovered rather than enumerated.

Adding a provider is one file in this directory. It subclasses
`RetryingProvider`, declares `kind` and `label`, and decorates itself with
`@register`. Nothing else changes: not this file, not the API, not the settings
page — the page builds its provider list from `catalogue()`.

Discovery imports every module in the package, so a plugin that is present is a
plugin that is registered. That is deliberate over an explicit list: a list is
a second place to forget.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from fidelity.runner import ProviderError

from .base import (
    DEFAULT_MODEL,
    MAX_RETRIES,
    REGISTRY,
    STREAM_BACKOFF,
    STREAM_RETRIES,
    RetryingProvider,
    register,
)


def _discover() -> None:
    for module in pkgutil.iter_modules(__path__):
        if not module.name.startswith("_") and module.name != "base":
            importlib.import_module(f"{__name__}.{module.name}")


_discover()

# Re-exported so existing imports and tests keep working after the split.
from .anthropic import AnthropicProvider, _error_type, _translate  # noqa: E402
from .openai_chat import (  # noqa: E402
    OpenAICompatibleProvider,
    _translate_openai,
    _unsupported_param,
)
from .openai_responses import OpenAIResponsesProvider  # noqa: E402


def kinds() -> list[str]:
    return sorted(REGISTRY)


def catalogue() -> list[dict[str, Any]]:
    """What the settings page needs to offer every registered provider."""
    return [{
        "kind": cls.kind,
        "label": cls.label or cls.kind,
        "wants_base_url": cls.wants_base_url,
        "wants_api_key": cls.wants_api_key,
        "model_hint": cls.model_hint,
        "base_url_hint": cls.base_url_hint,
    } for cls in sorted(REGISTRY.values(), key=lambda c: c.label or c.kind)]


def build(kind: str, *, model: str, api_key: str = "", base_url: str = "",
          max_tokens: int = 4096) -> RetryingProvider:
    """The one place a profile's `kind` becomes an adapter.

    Unknown kinds are refused rather than defaulted — silently picking a
    provider is how a request goes somewhere nobody intended.
    """
    cls = REGISTRY.get(kind)
    if cls is None:
        raise ValueError(f"unsupported provider kind: {kind!r}")
    return cls(api_key=api_key, model=model, base_url=base_url,
               max_tokens=max_tokens)


__all__ = [
    "AnthropicProvider", "OpenAICompatibleProvider", "OpenAIResponsesProvider",
    "ProviderError", "RetryingProvider", "REGISTRY", "DEFAULT_MODEL",
    "MAX_RETRIES", "STREAM_RETRIES", "STREAM_BACKOFF",
    "build", "catalogue", "kinds", "register",
    "_translate", "_translate_openai", "_error_type", "_unsupported_param",
]
