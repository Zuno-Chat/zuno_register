from zuno_register.code import ALPHABET, LENGTH, new_code


def test_codes_are_ten_symbols_from_the_alphabet_and_vary():
    codes = {new_code() for _ in range(200)}
    assert len(codes) == 200
    for code in codes:
        assert len(code) == LENGTH and set(code) <= set(ALPHABET)


def test_alphabet_has_no_lookalikes_and_divides_256():
    assert not set("IO01") & set(ALPHABET)
    assert 256 % len(ALPHABET) == 0
