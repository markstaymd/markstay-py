"""Opaque id generation (SPEC.md §6).

The reference write path mints "a short opaque generated id, not derived from the
block text," so a rewriting model has nothing to "improve." Generation is the only
randomness in the core; every write helper funnels its minting through an
injectable factory so the conformance/unit tests stay deterministic.

Port of the JavaScript reference (`impl/js/src/id.js`); the two are gated by the
shared conformance corpus.
"""

from __future__ import annotations

import os
import re
from typing import Callable

# Default id alphabet: base62, a strict subset of the §6 id charset
# [A-Za-z0-9_-]. `_` and `-` are legal in authored ids but omitted from
# *generated* ids so a minted id never begins with `-` (which reads as a CLI
# flag) and never collides with the marker delimiters.
DEFAULT_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

# 8 base62 chars ~= 47.6 bits: ample collision resistance for per-document
# coverage without the token weight of a UUID (§6 calls UUIDs too heavy).
DEFAULT_ID_LENGTH = 8

# The §6 id grammar: one or more of [A-Za-z0-9_-].
ID_CHARSET = re.compile(r"^[A-Za-z0-9_-]+$")


def mint_id(
    length: int = DEFAULT_ID_LENGTH,
    alphabet: str = DEFAULT_ALPHABET,
    random: Callable[[int], bytes] = os.urandom,
) -> str:
    """Mint one opaque id (SPEC.md §6).

    ``length``    id length in characters (default 8)
    ``alphabet``  characters to draw from (default base62)
    ``random``    ``n -> bytes`` source (default ``os.urandom``); injectable so
                  write helpers can be made deterministic in tests.

    Bytes are drawn with rejection sampling so the alphabet is unbiased even when
    its length does not divide 256.
    """
    if not isinstance(length, int) or isinstance(length, bool) or length < 1:
        raise ValueError(f"mint_id: length must be a positive integer, got {length!r}")
    n = len(alphabet)
    if n < 2:
        raise ValueError("mint_id: alphabet needs at least 2 characters")
    limit = 256 - (256 % n)  # largest unbiased byte threshold
    out: list[str] = []
    while len(out) < length:
        buf = random(length - len(out))
        for b in buf:
            if len(out) >= length:
                break
            if b < limit:
                out.append(alphabet[b % n])
    return "".join(out)
