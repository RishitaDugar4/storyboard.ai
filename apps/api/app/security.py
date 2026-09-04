"""Passphrase hashing.

scrypt from the standard library rather than a dependency: for a two-account
private app it is the right strength-to-complexity trade, and it removes a
supply-chain surface from something that guards the whole product.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

#: RFC 7914 interactive-login parameters. Costs ~100ms and ~16MB per check,
#: which is irrelevant at two logins and expensive at a billion guesses.
_N, _R, _P = 2 ** 15, 8, 1
#: 128 * N * r = 32MB exactly, which trips OpenSSL's default ceiling; give it
#: headroom rather than weakening the parameters.
_MAXMEM = 96 * 1024 * 1024
_SALT_BYTES = 16
_KEY_BYTES = 32
_SCHEME = "scrypt"


def hash_passphrase(passphrase: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    key = hashlib.scrypt(passphrase.encode(), salt=salt, n=_N, r=_R, p=_P,
                         maxmem=_MAXMEM, dklen=_KEY_BYTES)
    return f"{_SCHEME}${_N}${_R}${_P}${salt.hex()}${key.hex()}"


def verify_passphrase(passphrase: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, key_hex = stored.split("$")
        if scheme != _SCHEME:
            return False
        key = hashlib.scrypt(passphrase.encode(), salt=bytes.fromhex(salt_hex),
                             n=int(n), r=int(r), p=int(p), maxmem=_MAXMEM,
                             dklen=len(key_hex) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(key.hex(), key_hex)


def generate_passphrase(words: int = 4) -> str:
    """A readable passphrase for handing to someone in person."""
    alphabet = ("amber anchor autumn basil beacon birch cedar cinder clover "
                "cobalt copper coral dusk ember fern flint garnet harbor heron "
                "indigo ivory juniper kestrel lantern linen maple marsh mica "
                "moss nectar oak onyx opal pebble quartz reed rowan russet "
                "saffron sage slate sorrel spruce thistle tide umber velvet "
                "willow wren").split()
    return "-".join(secrets.choice(alphabet) for _ in range(words))
