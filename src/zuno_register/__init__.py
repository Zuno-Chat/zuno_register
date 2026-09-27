"""Synapse module: registration by emailed single-use token for Zuno Chat.

homeserver.yaml:

    modules:
      - module: zuno_register.ZunoRegister
        config: {...}   # see docs/design.md
"""

from __future__ import annotations

from typing import Any

from synapse.module_api import ModuleApi

from . import synapse_private
from .config import Config, parse_config
from .mail import Mailer
from .ratelimit import RateLimiter
from .redis import Records
from .registrar import Registrar
from .resource import RegisterResource
from .upstream import Upstream

PATH = "/_synapse/client/zuno/register/token"


class ZunoRegister:
    def __init__(self, config: Config, api: ModuleApi) -> None:
        if config.brevo is None:
            return
        records = Records(
            synapse_private.redis_connection(api),
            f"zuno_register:{api.server_name}:",
            config.redis_timeout,
        )
        mailer = Mailer(config.brevo, Upstream(config.brevo.timeout))
        registrar = Registrar(config, synapse_private.token_store(api), records, mailer)
        limiter = RateLimiter(config.rate_limit.per_second, config.rate_limit.burst)
        api.register_web_resource(
            PATH,
            RegisterResource(limiter, registrar, record_secret=synapse_private.record_secret(api)),
        )

    @staticmethod
    def parse_config(config: dict[str, Any]) -> Config:
        return parse_config(config)
