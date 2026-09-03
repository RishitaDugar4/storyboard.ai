"""UUIDv7: time-sortable identifiers.

Rows are created in roughly time order, so a v7 primary key clusters inserts
and makes `ORDER BY id` a usable proxy for `ORDER BY created_at` without a
second index. Implemented here rather than pulled in as a dependency -- it is
twelve lines of RFC 9562.
"""
from __future__ import annotations

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")
    # 48-bit timestamp | version 7 | 12 bits rand | variant 0b10 | 62 bits rand
    value = (ms << 80) | (0x7 << 76) | ((rand >> 62) & 0xFFF) << 64
    value |= (0b10 << 62) | (rand & ((1 << 62) - 1))
    return uuid.UUID(int=value)
