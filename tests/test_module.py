import hashlib
import hmac
from typing import cast

import pytest
from conftest import FakeModuleApi, as_api
from synapse.module_api.errors import ConfigError

from zuno_register import PATH, ZunoRegister
from zuno_register.registrar import Registrar
from zuno_register.resource import RegisterResource

CONFIG = {"brevo": {"api_key": "k", "sender_email": "noreply@zuno.chat"}}


def test_path():
    assert PATH == "/_synapse/client/zuno/register/token"


def test_registers_the_resource_on_the_store_and_synapses_redis():
    api = FakeModuleApi()
    ZunoRegister(ZunoRegister.parse_config(CONFIG), as_api(api))
    ((path, res),) = api.registered
    assert path == PATH
    assert isinstance(res, RegisterResource) and res.isLeaf
    registrar = cast(Registrar, res._registrar)
    assert registrar._store is api.store
    assert registrar._records._conn is api.connection
    assert registrar._records._prefix == "zuno_register:zuno.test:"
    derived = hmac.new(b"macaroon", b"zuno_register/record", hashlib.sha256).digest()
    assert res._record_secret == derived


def test_without_brevo_section_nothing_is_registered():
    api = FakeModuleApi()
    ZunoRegister(ZunoRegister.parse_config({}), as_api(api))
    assert api.registered == []


def test_redis_disabled_is_a_config_error():
    api = FakeModuleApi(redis_enabled=False)
    with pytest.raises(ConfigError, match="redis.enabled"):
        ZunoRegister(ZunoRegister.parse_config(CONFIG), as_api(api))
