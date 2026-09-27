"""The private Synapse handles the module needs, in one place.

Everything else in ``src`` uses only the public module API. These three
attributes are not covered by Synapse's module-API compatibility promise, so
a Synapse upgrade checks this file (and the dev pin in pyproject.toml).
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Protocol

from synapse.module_api import ModuleApi
from synapse.module_api.errors import ConfigError


class TokenStore(Protocol):
    """The registration-token slice of synapse.storage.databases.main.registration."""

    async def create_registration_token(
        self, token: str, uses_allowed: int | None, expiry_time: int | None
    ) -> bool: ...

    async def registration_token_is_valid(self, token: str) -> bool: ...

    async def delete_registration_token(self, token: str) -> bool: ...


def token_store(api: ModuleApi) -> TokenStore:
    store: TokenStore = api._hs.get_datastores().main
    return store


def record_secret(api: ModuleApi) -> bytes:
    """The key for per-address records, derived from the macaroon secret.

    Synapse always has one (it falls back to the signing key), so there is no
    extra secret to provision. Derived, never used directly, so a leak of a
    Redis dump plus this key still says nothing about macaroons.
    """
    secret: bytes = api._hs.config.key.macaroon_secret_key
    return hmac.new(secret, b"zuno_register/record", hashlib.sha256).digest()


def redis_connection(api: ModuleApi) -> Any:
    """Synapse's own txredisapi ConnectionHandler; the same one replication uses."""
    hs = api._hs
    if not hs.config.redis.redis_enabled:
        raise ConfigError("zuno_register: needs redis.enabled: true in homeserver.yaml")
    return hs.get_outbound_redis_connection()
