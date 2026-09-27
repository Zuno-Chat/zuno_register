import hashlib
import hmac

import pytest

from zuno_register.email import InvalidEmail, normalize, record_key


def test_address_is_returned_as_typed_and_canonical_is_folded():
    assert normalize(" Alice+news@Example.com ") == ("Alice+news@Example.com", "alice@example.com")
    assert normalize("ALICE@EXAMPLE.COM")[1] == "alice@example.com"
    assert normalize("a.b@sub.example.co.uk")[1] == "a.b@sub.example.co.uk"


def test_a_leading_plus_is_kept():
    assert normalize("+x@example.com")[1] == "+x@example.com"


def test_record_key_is_a_keyed_hash():
    expected = hmac.new(b"s", b"alice@example.com", hashlib.sha256).hexdigest()
    assert record_key(b"s", "alice@example.com") == expected
    assert record_key(b"other", "alice@example.com") != expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        5,
        "",
        "   ",
        "alice",
        "alice@",
        "@example.com",
        "alice@localhost",
        "alice@example..com",
        "alice@-example.com",
        "al ice@example.com",
        "Alice <alice@example.com>",
        "ali\nce@example.com",
        '"quoted"@example.com',
        "a" * 65 + "@example.com",
        "a@" + "b" * 250 + ".com",
    ],
)
def test_bad_addresses_are_rejected(raw):
    with pytest.raises(InvalidEmail):
        normalize(raw)
