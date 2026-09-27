"""Address validation and the per-address record key.

The key is an HMAC of the canonical address under a server secret: Redis
never holds a raw address, and without the secret nobody holding a Redis
dump can test whether a given address has a record (addresses are far too
guessable for a plain hash). Canonical means lower-cased, so case variants
share one record, and without a ``+tag``, so one inbox cannot mint itself a
fresh record per spelling. The mail still goes to the address as typed.
"""

from __future__ import annotations

import hashlib
import hmac
import re

MAX_LEN = 254  # RFC 5321 forward-path ceiling
MAX_LOCAL_LEN = 64
_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
# A dot-atom local part and a domain of at least two labels: "user@localhost"
# is not deliverable from here, and quoted local parts are not worth accepting.
_ADDRESS = re.compile(rf"^({_ATOM}(?:\.{_ATOM})*)@({_LABEL}(?:\.{_LABEL})+)$")


class InvalidEmail(ValueError):
    pass


def normalize(raw: object) -> tuple[str, str]:
    """(address to send to, canonical address) or InvalidEmail."""
    if not isinstance(raw, str):
        raise InvalidEmail()
    addr = raw.strip()
    if len(addr) > MAX_LEN:
        raise InvalidEmail()
    m = _ADDRESS.match(addr)
    if m is None or len(m.group(1)) > MAX_LOCAL_LEN:
        raise InvalidEmail()
    local, domain = m.groups()
    plus = local.find("+")
    if plus > 0:
        local = local[:plus]
    return addr, f"{local}@{domain}".lower()


def record_key(secret: bytes, canonical: str) -> str:
    return hmac.new(secret, canonical.encode(), hashlib.sha256).hexdigest()
