"""PCM helpers.

Gemini TTS returns headerless PCM (`audio/L16;codec=pcm;rate=24000`), which
nothing downstream will open. Wrapping it in a WAV container is 44 bytes of
header and removes any dependency on ffmpeg for the narration path.

Because the payload is raw PCM, duration is arithmetic rather than a
measurement -- exact, and available before the file is even written.
"""
from __future__ import annotations

import re
import struct

DEFAULT_SAMPLE_RATE = 24_000
BITS_PER_SAMPLE = 16
CHANNELS = 1


def parse_pcm_mime(mime: str) -> tuple[int, int]:
    """Pull (sample_rate, bits) out of a mime like audio/L16;codec=pcm;rate=24000."""
    rate = DEFAULT_SAMPLE_RATE
    bits = BITS_PER_SAMPLE
    if m := re.search(r"rate=(\d+)", mime or ""):
        rate = int(m.group(1))
    if m := re.search(r"L(\d+)", mime or ""):
        bits = int(m.group(1))
    return rate, bits


def pcm_duration_ms(n_bytes: int, sample_rate: int = DEFAULT_SAMPLE_RATE,
                    bits: int = BITS_PER_SAMPLE, channels: int = CHANNELS) -> int:
    bytes_per_frame = (bits // 8) * channels
    if bytes_per_frame <= 0 or sample_rate <= 0:
        return 0
    return int(round(n_bytes / bytes_per_frame / sample_rate * 1000))


def pcm_to_wav(pcm: bytes, sample_rate: int = DEFAULT_SAMPLE_RATE,
               bits: int = BITS_PER_SAMPLE, channels: int = CHANNELS) -> bytes:
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    return b"".join([
        b"RIFF", struct.pack("<I", 36 + len(pcm)), b"WAVE",
        b"fmt ", struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                             byte_rate, block_align, bits),
        b"data", struct.pack("<I", len(pcm)), pcm,
    ])


def silence_wav(duration_ms: int, sample_rate: int = DEFAULT_SAMPLE_RATE) -> bytes:
    frames = int(sample_rate * duration_ms / 1000)
    return pcm_to_wav(b"\x00\x00" * frames, sample_rate)
