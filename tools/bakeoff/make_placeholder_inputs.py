#!/usr/bin/env python3
"""Generate stand-in input stills so a --fake run works before you have art.

These are NOT a substitute for real inputs. Placeholders tell you nothing about
how your film will look -- generate the three real stills with your chosen image
provider, in your chosen art style, before spending anything. See README.md.

Pure standard library: no ffmpeg, no Pillow.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
INPUTS = HERE / "inputs"
W, H = 1920, 1080

# name, base RGB, accent RGB, number of marker bars
PLACEHOLDERS = [
    ("01-character-closeup", (43, 58, 85), (232, 197, 143), 1),
    ("02-two-characters-medium", (74, 59, 47), (168, 208, 224), 2),
    ("03-establishing-wide", (47, 74, 59), (240, 226, 178), 3),
]


def _png(path: Path, rows: list[bytearray]) -> None:
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def build(base: tuple[int, int, int], accent: tuple[int, int, int],
          bars: int) -> list[bytearray]:
    rows: list[bytearray] = []
    for y in range(H):
        # vertical gradient so motion models have something to track
        t = y / (H - 1)
        r = int(base[0] + (255 - base[0]) * 0.35 * (1 - t))
        g = int(base[1] + (255 - base[1]) * 0.35 * (1 - t))
        b = int(base[2] + (255 - base[2]) * 0.35 * (1 - t))
        row = bytearray()
        for x in range(W):
            # accent bars: distinct, countable, easy to spot drifting
            in_bar = False
            for i in range(bars):
                cx = W * (i + 1) // (bars + 1)
                if abs(x - cx) < 90 and H * 0.28 < y < H * 0.72:
                    in_bar = True
                    break
            row += bytes(accent if in_bar else (r, g, b))
        rows.append(row)
    return rows


def main() -> int:
    INPUTS.mkdir(parents=True, exist_ok=True)
    for name, base, accent, bars in PLACEHOLDERS:
        dest = INPUTS / f"{name}.png"
        if dest.exists():
            print(f"  keeping existing {dest.name}")
            continue
        _png(dest, build(base, accent, bars))
        print(f"  wrote {dest.name}  ({dest.stat().st_size / 1024:.0f} KB)")
    print("\n  Placeholders only. Replace with real stills in your art style "
          "before any paid run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
