"""Deterministic stand-ins. The whole pipeline must run with zero spend.

FakeText can also be told to fail its cross-field validators once, so the
repair loop is exercised in CI rather than discovered in production.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ..ports import AIError, AIErrorKind, StructuredResult, Usage

T = TypeVar("T", bound=BaseModel)

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "ai"


class FakeTextAdapter:
    """Returns a golden fixture chosen by the requested schema."""

    def __init__(self, *, fixtures_dir: Path | None = None,
                 repair_once: bool = False, refuse: bool = False) -> None:
        self._dir = fixtures_dir or FIXTURES
        self._repair_once = repair_once
        self._refuse = refuse
        self.calls = 0

    async def generate_structured(
        self, *, schema: type[T], system: str, user: str,
        max_tokens: int = 16000, effort: str = "high",
        cache_prefix: str | None = None,
    ) -> StructuredResult[T]:
        self.calls += 1
        if self._refuse:
            raise AIError(AIErrorKind.REFUSAL, "provider_refused",
                          "FakeTextAdapter simulated a safety refusal")

        path = self._dir / f"{_fixture_name(schema)}.json"
        if not path.exists():
            raise AIError(AIErrorKind.INVALID, "missing_fixture", str(path))
        data = json.loads(path.read_text())

        repaired, repair_errors = False, []
        if self._repair_once and self.calls == 1:
            # Genuinely break the one rule JSON Schema cannot express, then
            # validate it, so the recorded errors are real validator output --
            # not a flag we set by hand. A fake that only *claims* to have
            # repaired something gives false confidence.
            broken = _break_referential_integrity(data)
            try:
                schema.model_validate(broken)
            except ValidationError as exc:
                repaired = True
                repair_errors = [
                    f"- {'.'.join(str(x) for x in e.get('loc', ())) or '(document)'}: "
                    f"{e['msg'].replace('Value error, ', '')}"
                    for e in exc.errors()
                ]
            else:  # pragma: no cover - the fixture should always break
                raise AIError(AIErrorKind.INVALID, "fixture_not_breakable",
                              "repair simulation did not produce invalid data")

        value = schema.model_validate(data)
        return StructuredResult(
            value=value,
            usage=Usage(model="fake", input_tokens=1200, output_tokens=900,
                        latency_ms=5, cost_cents=0.0),
            repaired=repaired, repair_errors=repair_errors,
            raw_text=json.dumps(data)[:400],
        )


def _fixture_name(schema: type[BaseModel]) -> str:
    return {"StoryAnalysis": "story_analysis",
            "Storyboard": "storyboard"}.get(schema.__name__, schema.__name__.lower())


def _break_referential_integrity(data: dict) -> dict:
    """Point a shot at a character that was never defined."""
    broken = copy.deepcopy(data)
    broken["scenes"][0]["shots"][0]["subject_slugs"] = ["does-not-exist"]
    return broken


class FakeImageAdapter:
    """Deterministic stand-in stills.

    Generates a real PNG whose colour derives from the prompt hash, so two
    different shots look different and the same shot looks the same. Enough to
    exercise storage, selection, freshness and the renderer without spending
    anything.
    """

    provider = "fake"
    model = "fake-image"

    def __init__(self, *, fail: bool = False,
                 cost_per_image_cents: float = 0.0) -> None:
        self._fail = fail
        #: Explicitly zero, not unknown: the fake really is free, and the
        #: budget gate must be able to tell those apart.
        self.cost_per_image_cents = cost_per_image_cents
        self.calls = 0

    async def generate(self, *, positive: str, negative: str, size: str,
                       seed=None, n: int = 1):
        from ..ports import AIError, AIErrorKind, ImageResult, Usage
        self.calls += 1
        if self._fail:
            raise AIError(AIErrorKind.REFUSAL, "provider_refused",
                          "FakeImageAdapter simulated a content refusal")
        w, h = (int(x) for x in size.lower().split("x"))
        out = []
        for i in range(n):
            digest = hashlib.sha256(f"{positive}|{seed}|{i}".encode()).digest()
            rgb = (digest[0] // 2 + 40, digest[1] // 2 + 40, digest[2] // 2 + 40)
            out.append(ImageResult(data=_solid_png(w, h, rgb), mime="image/png",
                                   width=w, height=h, seed=seed))
        return out, Usage(model=self.model, latency_ms=8, cost_cents=0.0)


def _solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A valid PNG without pulling in an image library."""
    import struct
    import zlib
    row = bytes(rgb) * width
    raw = b"".join(b"\x00" + row for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


class FakeSpeechAdapter:
    """Silence of the right length.

    Not speech, but the timing machinery only cares about duration, and it is
    derived from the same words-per-second model the storyboard was written
    against -- so a film assembled from fakes has the pacing the real one will.
    """

    provider = "fake"
    model = "fake-tts"

    def __init__(self, *, fail: bool = False, words_per_second: float = 2.5) -> None:
        self._fail = fail
        self._wps = words_per_second
        self.calls = 0

    def voices(self) -> list[str]:
        return ["Kore", "Puck", "Zephyr", "Leda"]

    async def synthesize(self, *, text: str, voice: str = "Kore",
                         style: str | None = None):
        from ..audio import DEFAULT_SAMPLE_RATE, silence_wav
        from ..ports import AIError, AIErrorKind, SpeechResult, Usage
        self.calls += 1
        if self._fail:
            raise AIError(AIErrorKind.REFUSAL, "provider_refused",
                          "FakeSpeechAdapter simulated a refusal")
        words = max(1, len(text.split()))
        duration_ms = int(words / self._wps * 1000)
        return (
            SpeechResult(data=silence_wav(duration_ms), mime="audio/wav",
                         duration_ms=duration_ms,
                         sample_rate=DEFAULT_SAMPLE_RATE, voice=voice),
            Usage(model=self.model, latency_ms=5, cost_cents=0.0),
        )


class FakeVideoAdapter:
    """A real, probe-able MP4 -- produced locally, costing nothing.

    It must be a genuine video file rather than a stub: the whole point of the
    validation stage is to measure what came back with ffprobe, and a fake that
    cannot be probed would leave that path untested until the first paid run.

    What it produces is a synthetic test pattern of the right length, not an
    animation of the keyframe: it stands in for a provider, it does not imitate
    one. The registry hands it out only when AI_VIDEO_PROVIDER is explicitly
    `fake`, and nothing can reach it as a fallback from a real generation -- a
    failed clip fails (see handlers/motion.py).
    """

    name = "fake"
    provider = "fake"

    def __init__(self, *, fail: bool = False, ticks_pending: int = 0) -> None:
        self._fail = fail
        #: How many polls report "not done" before completing, so the polling
        #: and backoff path is exercised rather than short-circuited.
        self._ticks_pending = ticks_pending
        self._polls: dict[str, int] = {}
        self.submissions = 0

    def serves(self, adapter_name: str) -> bool:
        return True

    async def submit(self, req):
        from ..ports import AIError, AIErrorKind, Submission
        if self._fail:
            raise AIError(AIErrorKind.REFUSAL, "provider_refused",
                          "FakeVideoAdapter simulated a refusal")
        self.submissions += 1
        job_id = f"fake-{self.submissions:04d}"
        return Submission(
            provider_job_id=job_id, endpoint="fake://video",
            raw={"duration_s": req.duration_s, "prompt": req.prompt,
                 "first_frame": req.first_frame.hex()[:16]})

    async def poll(self, sub):
        from ..ports import OperationState
        n = self._polls.get(sub.provider_job_id, 0)
        self._polls[sub.provider_job_id] = n + 1
        if n < self._ticks_pending:
            return OperationState(done=False, progress_hint=f"tick {n + 1}")
        return OperationState(done=True, video_uri="fake://video/ready",
                              raw=sub.raw)

    async def fetch(self, state):
        from ..ports import AIError, AIErrorKind, VideoResult
        data = _synth_mp4(float(state.raw.get("duration_s", 5.0)))
        if data is None:
            raise AIError(
                AIErrorKind.TRANSIENT, "no_ffmpeg",
                "the fake video adapter needs ffmpeg to produce a probe-able "
                "clip. Install ffmpeg, or run the planning stages only.")
        return VideoResult(data=data, mime="video/mp4")

    async def aclose(self) -> None:
        return None


def _synth_mp4(duration_s: float) -> bytes | None:
    """A real H.264 file of the requested length, or None without ffmpeg."""
    import subprocess
    import tempfile
    from pathlib import Path

    from ...render.ffmpeg import FFMPEG
    if not FFMPEG:
        return None
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "fake.mp4"
        subprocess.run(
            [FFMPEG, "-y", "-v", "error",
             "-f", "lavfi", "-i",
             f"testsrc2=size=1280x720:rate=24:duration={duration_s:.3f}",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             str(dest)],
            capture_output=True, timeout=120, check=True)
        return dest.read_bytes()
