"""The emailed code. The app mirrors the alphabet in its input filter: change both together."""

import secrets

# No I, O, 0 or 1, so a code read off an email cannot be mistyped. 32 symbols
# divide 256 exactly, which keeps the modulo selection unbiased.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LENGTH = 10  # ~50 bits, still short enough to type


def new_code() -> str:
    return "".join(ALPHABET[b % len(ALPHABET)] for b in secrets.token_bytes(LENGTH))
