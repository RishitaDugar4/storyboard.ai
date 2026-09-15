"""Verify the Veo adapter against the live API, cheapest evidence first.

    python tools/verify_veo.py                      # free
    python tools/verify_veo.py --yes                # ~$0.40: one generation
    python tools/verify_veo.py --yes --last-frame   # ~$0.80: adds continuity

Nothing in ``app/ai/adapters/veo_video.py`` has ever executed
(docs/adr/001-bakeoff-results.md: "0 calls ... its adapter has never
executed"). Every field in it is documentation-derived, and eleven separate
claims -- the model ids, the image field's name, where ``lastFrame`` lives,
the path to the video uri in the poll response -- each fail the call on their
own. This script checks them in order of what they cost to check.

It drives ``VeoVideoAdapter`` directly rather than the bake-off's copy, which
has since diverged: verifying that one would verify code the application does
not run. It needs no database and no project.

THE POINT OF THE LAST-FRAME STAGE. A 200 from Veo is not proof that
``lastFrame`` did anything. An unknown key inside ``instances[]`` may simply be
ignored, and what comes back is then a valid, fully-billed clip that does not
land where it was told to -- a failure indistinguishable from success at every
layer the application has. The job succeeds, ``validate_clip`` passes, the film
renders. So the closing frame of the clip is measured against the image the
clip was asked to end on, and the verdict rests on that comparison rather than
on the status code.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent.parent / "out" / "verify-veo"
#: The bake-off's real stills. Deliberately the default: verifying a video
#: model on a flat test pattern tells you nothing about what it does to art.
BAKEOFF_INPUTS = REPO_ROOT / "tools" / "bakeoff" / "inputs"

from app.ai.catalog import CATALOG, AudioBehavior            # noqa: E402
from app.ai.catalog import get as get_caps                   # noqa: E402
from app.ai.ports import AIError, VideoRequest               # noqa: E402
from app.render.ffmpeg import extract_tail_frame, probe      # noqa: E402

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp"}


# --------------------------------------------------------------------------- #
# Findings: every claim this script touches, and what actually happened to it.
# --------------------------------------------------------------------------- #
OK, BAD, UNKNOWN = "ok", "MISMATCH", "unverified"


@dataclass
class Finding:
    claim: str
    expected: str
    measured: str
    verdict: str


FINDINGS: list[Finding] = []

#: Set once a clip has actually come back. The free stages can confirm every
#: claim they touch and still leave the adapter unproven -- a model id that
#: resolves says nothing about whether the payload generates. Promotion to
#: ACTIVE requires this.
GENERATED = False


def record(claim: str, expected, measured, verdict: str) -> None:
    FINDINGS.append(Finding(claim, str(expected), str(measured), verdict))


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say(f"\n{title}\n" + "-" * 74)


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #
def load_env() -> str:
    """Read the same .env files the application's Settings reads.

    The registry takes its keys from ``os.environ`` directly, and nothing in
    the app puts a .env there -- under compose the env_file does it. A local
    operator running this script would otherwise get 'GEMINI_API_KEY is not
    set' while looking straight at the key in their .env.

    Shell wins over file, matching Settings' precedence.
    """
    loaded = []
    try:
        from dotenv import load_dotenv
    except ImportError:
        return "python-dotenv absent; using the shell environment only"
    for path in (REPO_ROOT / ".env", REPO_ROOT / "apps/api/.env"):
        if path.exists() and load_dotenv(path, override=False):
            loaded.append(str(path.relative_to(REPO_ROOT)))
    return ", ".join(loaded) or "shell environment only"


def elide(value, keep: int = 24):
    """Base64 payloads are megabytes and unreadable. Keep the shape, drop the
    bytes -- the shape is the thing under test."""
    if isinstance(value, dict):
        return {k: elide(v, keep) for k, v in value.items()}
    if isinstance(value, list):
        return [elide(v, keep) for v in value]
    if isinstance(value, str) and len(value) > keep:
        return f"<{len(value)} chars: {value[:keep]}...>"
    return value


# --------------------------------------------------------------------------- #
# Perceptual comparison
# --------------------------------------------------------------------------- #
def dhash(path: Path, size: int = 8) -> int:
    """A 64-bit difference hash: each bit is 'this pixel is brighter than the
    one to its right'.

    Robust to exactly what separates two encodings of the same picture --
    resolution, compression, small colour shifts -- and sensitive to what
    separates two different pictures. Chosen over a pixel diff because the
    frame under test has been through the provider's encoder and ours.
    """
    from PIL import Image

    img = Image.open(path).convert("L").resize((size + 1, size),
                                               Image.Resampling.LANCZOS)
    px = list(img.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | int(left > right)
    return bits


def distance(a: int, b: int) -> int:
    """Hamming distance: 0 identical, 64 maximally unlike."""
    return bin(a ^ b).count("1")


# --------------------------------------------------------------------------- #
# Stage 1 -- free. Does the key work, and do these model ids exist?
# --------------------------------------------------------------------------- #
async def stage_credentials(model_keys: list[str]) -> bool:
    """A GET against the models endpoint. A read: no generation, no charge.

    Kills the two likeliest failure modes -- a key that does not authenticate
    and a model id that does not exist -- before any money is at risk. Kling's
    model id was already wrong once, and the bake-off paid to find out.
    """
    import httpx

    from app.ai.adapters.veo_video import API_BASE, VeoVideoAdapter

    rule("STAGE 1  credentials and model ids     (free -- a read, not a generation)")
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
    try:
        VeoVideoAdapter(key)          # its own constructor holds the key rules
    except AIError as exc:
        say(f"  key           REFUSED  {exc.code}: {exc.detail}")
        record("GEMINI_API_KEY usable", "a durable AIza... key", exc.code, BAD)
        return False
    say(f"  key           looks durable ({len(key)} chars, starts {key[:4]!r})")

    ok = True
    async with httpx.AsyncClient(timeout=30.0,
                                 headers={"x-goog-api-key": key}) as client:
        for mk in model_keys:
            caps = get_caps(mk)
            r = await client.get(f"{API_BASE}/models/{caps.model_id}")
            if r.status_code == 200:
                body = r.json()
                methods = body.get("supportedGenerationMethods") or []
                say(f"  {caps.model_id:<32} 200  exists"
                    + (f"  methods={','.join(methods)}" if methods else ""))
                record(f"model_id {caps.model_id}", "exists", "200 OK", OK)
                # The adapter posts to :predictLongRunning. If the model
                # advertises its methods and that is not among them, the very
                # first paid call would 404 on a correct-looking request.
                if methods and "predictLongRunning" not in methods:
                    say(f"    ! does NOT advertise predictLongRunning "
                        f"-- the adapter posts to exactly that")
                    record("supports :predictLongRunning", "advertised",
                           ",".join(methods), BAD)
                    ok = False
            else:
                detail = r.text[:160].replace("\n", " ")
                say(f"  {caps.model_id:<32} {r.status_code}  {detail}")
                record(f"model_id {caps.model_id}", "exists",
                       f"HTTP {r.status_code}", BAD)
                ok = False
    return ok


# --------------------------------------------------------------------------- #
# Stage 2 -- free. What would actually go on the wire?
# --------------------------------------------------------------------------- #
def stage_payload(model_key: str, first: Path, last: Path | None,
                  duration_s: float, resolution: str,
                  person_generation: str) -> None:
    """Build the request without sending it.

    `build_payload` was split out of `submit` for this: the structure can be
    read and argued with before it is paid for.
    """
    from app.ai.adapters.veo_video import VeoVideoAdapter

    caps = get_caps(model_key)
    rule("STAGE 2  the payload, unsent           (free -- nothing leaves this process)")
    adapter = object.__new__(VeoVideoAdapter)
    adapter._person_generation = person_generation
    payload = adapter.build_payload(VideoRequest(
        model_key=caps.model_key, model_id=caps.model_id,
        first_frame=first.read_bytes(),
        first_frame_mime=MIME.get(first.suffix.lower(), "image/png"),
        prompt="A slow push in. The light shifts. Nothing else moves.",
        negative_prompt=None, reference_images=[],
        duration_s=duration_s, resolution=resolution, aspect_ratio="16:9",
        seed=7,
        last_frame=last.read_bytes() if last else None,
        last_frame_mime=MIME.get(last.suffix.lower(), "image/png") if last else None))
    say(json.dumps(elide(payload), indent=2, sort_keys=True))

    instance = payload["instances"][0]
    image_field = set((instance.get("image") or {}))
    say("\n  Claims in this payload that have never been confirmed:")
    say(f"    image keys        {sorted(image_field)}")
    say(f"                      ^ the single highest-risk field. The Python SDK "
        f"names this\n                        'imageBytes'; this adapter sends "
        f"'bytesBase64Encoded'. Wrong = 400 on\n                        every "
        f"call, so it is the first thing a 400 body will tell you.")
    say(f"    parameters        {sorted(payload['parameters'])}")
    if caps.supports_last_frame:
        present = caps.last_frame_field in instance
        say(f"    {caps.last_frame_field:<18}{'present' if present else 'ABSENT'} "
            f"in instances[0] "
            f"{'(alongside image, as documented)' if present else ''}")
        if last and not present:
            record(f"{caps.last_frame_field} reaches the payload", "present",
                   "absent", BAD)
    say(f"    seed              {'sent' if 'seed' in payload['parameters'] else 'filtered out'}"
        f"  (catalogue claims supports_seed={caps.supports_seed}; Veo's seed "
        f"support is\n                        poorly documented and this is a "
        f"guess)")


# --------------------------------------------------------------------------- #
# Stage 3 / 4 -- paid. One generation.
# --------------------------------------------------------------------------- #
async def generate(model_key: str, first: Path, last: Path | None, *,
                   prompt: str, duration_s: float, resolution: str,
                   person_generation: str, poll_s: float,
                   label: str) -> Path | None:
    """Submit, poll to completion, download, save. The real adapter throughout.

    The clip is written to disk the instant it arrives and before anything
    else can fail. It has been paid for, Veo deletes it after 48 hours, and
    re-running costs again.
    """
    from app.ai.adapters.veo_video import VeoVideoAdapter

    caps = get_caps(model_key)
    adapter = VeoVideoAdapter(
        os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", ""),
        person_generation=person_generation)
    started = time.monotonic()
    try:
        req = VideoRequest(
            model_key=caps.model_key, model_id=caps.model_id,
            first_frame=first.read_bytes(),
            first_frame_mime=MIME.get(first.suffix.lower(), "image/png"),
            prompt=prompt, negative_prompt=None, reference_images=[],
            duration_s=duration_s, resolution=resolution, aspect_ratio="16:9",
            seed=7,
            last_frame=last.read_bytes() if last else None,
            last_frame_mime=(MIME.get(last.suffix.lower(), "image/png")
                             if last else None))
        try:
            sub = await adapter.submit(req)
        except AIError as exc:
            say(f"  SUBMIT FAILED  {exc.kind}:{exc.code}")
            say(f"    {exc.detail[:400]}")
            say("\n    Nothing was generated, so nothing was billed -- Veo "
                "charges only on a\n    successful generation. Read the body "
                "above: it names the field it rejected,\n    which is the "
                "correction to make in the adapter or the catalogue.")
            record(f"{label}: submit accepted", "an operation name",
                   f"{exc.kind}:{exc.code}", BAD)
            return None
        say(f"  submitted     {sub.provider_job_id}")
        if sub.expires_at:
            say(f"  expires       {sub.expires_at}  (download is part of "
                f"generation, not a later step)")
        record(f"{label}: submit accepted", "an operation name",
               "accepted", OK)

        state = None
        while time.monotonic() - started < caps.max_wait_s:
            state = await adapter.poll(sub)
            waited = time.monotonic() - started
            if state.error is not None:
                e = state.error
                say(f"  OPERATION FAILED after {waited:.0f}s  {e.kind}:{e.code}")
                say(f"    {e.detail[:400]}")
                record(f"{label}: operation completed", "done",
                       f"{e.kind}:{e.code}", BAD)
                return None
            if state.done:
                say(f"  completed     after {waited:.0f}s")
                break
            say(f"  polling       {state.progress_hint or 'generating'} "
                f"({waited:.0f}s)")
            await asyncio.sleep(poll_s)
        else:
            say(f"  TIMED OUT after {caps.max_wait_s}s. The generation may "
                f"still finish and be\n    billed on their side -- check "
                f"before re-running.")
            record(f"{label}: finished within max_wait_s",
                   f"<= {caps.max_wait_s}s", "timed out", BAD)
            return None

        elapsed = time.monotonic() - started
        record(f"{label}: typical_latency_s", f"~{caps.typical_latency_s}s",
               f"{elapsed:.0f}s", OK if elapsed <= caps.max_wait_s else BAD)
        # The uri path through the poll response is itself an unverified
        # claim, and getting it wrong loses a clip that was already paid for.
        record("poll response: generatedSamples[0].video.uri",
               "a downloadable uri",
               "found" if state.video_uri else "NOT FOUND",
               OK if state.video_uri else BAD)
        if state.model_version:
            say(f"  modelVersion  {state.model_version}")

        try:
            result = await adapter.fetch(state)
        except AIError as exc:
            say(f"  DOWNLOAD FAILED  {exc.kind}:{exc.code}: {exc.detail[:200]}")
            say(f"    The clip was generated and billed. Its uri is:\n"
                f"      {state.video_uri}")
            record(f"{label}: download", "clip bytes",
                   f"{exc.kind}:{exc.code}", BAD)
            return None
    finally:
        await adapter.aclose()

    global GENERATED
    GENERATED = True
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / f"{label}.mp4"
    dest.write_bytes(result.data)
    say(f"  saved         {dest}  ({len(result.data):,} bytes)")
    return dest


def measure_against_catalogue(model_key: str, clip: Path, requested_s: float,
                              requested_res: str, label: str) -> None:
    """ffprobe the clip and check it against what the catalogue promised."""
    caps = get_caps(model_key)
    info = probe(clip)
    if not info.ok:
        say(f"  ffprobe could not read the clip: {info.note}")
        record(f"{label}: readable clip", "probe-able mp4", info.note, BAD)
        return

    drift_ms = (info.duration_ms or 0) - int(requested_s * 1000)
    say(f"  measured      {info.width}x{info.height}  {info.duration_ms}ms "
        f"({drift_ms:+d}ms)  {info.fps}fps  audio={info.has_audio}")

    record(f"{label}: duration grid honours {requested_s:g}s",
           f"{int(requested_s * 1000)}ms", f"{info.duration_ms}ms",
           OK if abs(drift_ms) <= 750 else BAD)

    want_h = int("".join(c for c in requested_res if c.isdigit()) or 0)
    record(f"{label}: resolution {requested_res}", f"height {want_h}",
           f"{info.width}x{info.height}", OK if info.height == want_h else BAD)

    # The renderer strips provider audio with -an on the strength of this
    # claim. If Veo does not in fact always return audio the claim is wrong,
    # even though nothing downstream would break.
    always_on = caps.audio is AudioBehavior.ALWAYS_ON
    record(f"{label}: audio={caps.audio.value}",
           "an audio stream" if always_on else "no audio stream",
           "present" if info.has_audio else "absent",
           OK if info.has_audio == always_on else BAD)


def stage_last_frame_verdict(clip: Path, first: Path, requested_last: Path) -> None:
    """Did `lastFrame` do anything?

    Three distances, because two would not be enough to tell "it worked" from
    "it was ignored and the clip happened to drift that way":

      baseline  the two input images, to each other -- the control. If they
                are not far apart the test cannot resolve anything and says so
                rather than returning a confident number.
      to_last   the clip's closing frame vs the image it was told to end on.
      to_first  the clip's closing frame vs the image it started from.

    Honoured means to_last is clearly smaller than to_first. Ignored means it
    is not -- the clip simply stayed near where it began, which is what an
    ordinary image-to-video generation does.
    """
    rule("VERDICT  did lastFrame actually do anything?")
    tail = OUT / "closing-frame-actual.png"
    try:
        extract_tail_frame(clip, tail)
    except Exception as exc:
        say(f"  could not read the clip's closing frame: {exc}")
        record("lastFrame honoured", "measurable", "unreadable", UNKNOWN)
        return

    h_tail, h_first, h_last = dhash(tail), dhash(first), dhash(requested_last)
    baseline = distance(h_first, h_last)
    to_last = distance(h_tail, h_last)
    to_first = distance(h_tail, h_first)

    say(f"  control: the two inputs are {baseline}/64 apart")
    say(f"  clip's closing frame -> requested closing image : {to_last}/64")
    say(f"  clip's closing frame -> opening image           : {to_first}/64")
    say(f"  actual closing frame saved to {tail}")

    if baseline < 12:
        say("\n  INCONCLUSIVE. The two input images are too alike for this "
            "test to separate\n  'landed where told' from 'never moved'. "
            "Re-run with two visibly different\n  stills via --first and "
            "--last.")
        record("lastFrame honoured", "a resolvable comparison",
               f"inputs only {baseline}/64 apart", UNKNOWN)
        return

    if to_last + 6 < to_first:
        say("\n  HONOURED. The clip ends materially closer to the image it was "
            "told to end on\n  than to the one it started from. Continuity is "
            "real on this model: set\n  motion_continuity='last_frame' (or "
            "'auto') and the joins will be seamless.")
        record("lastFrame honoured", "clip ends on the requested image",
               f"{to_last}/64 vs {to_first}/64 from the opening", OK)
    elif to_first + 6 < to_last:
        say("\n  IGNORED. The clip ends near where it STARTED, not where it "
            "was told to end.\n  Veo accepted the field and did nothing with "
            "it -- exactly the silent failure\n  this stage exists to catch. "
            "Clear last_frame_field on the Veo entries so the\n  application "
            "stops believing it, and chaining stays the only real mechanism.")
        record("lastFrame honoured", "clip ends on the requested image",
               f"IGNORED -- {to_first}/64 from the opening", BAD)
    else:
        say("\n  AMBIGUOUS. The closing frame is not clearly nearer to either "
            "input. Watch the\n  clip before concluding anything; the numbers "
            "do not settle it.")
        record("lastFrame honoured", "clip ends on the requested image",
               f"ambiguous ({to_last}/64 vs {to_first}/64)", UNKNOWN)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def report(model_key: str) -> int:
    caps = get_caps(model_key)
    rule("FINDINGS")
    if not FINDINGS:
        say("  nothing checked.")
        return 0
    width = max(len(f.claim) for f in FINDINGS)
    for f in FINDINGS:
        mark = {OK: "  ok  ", BAD: " FAIL ", UNKNOWN: "  ?   "}[f.verdict]
        say(f"  [{mark}] {f.claim:<{width}}  expected {f.expected} -> "
            f"got {f.measured}")

    bad = [f for f in FINDINGS if f.verdict == BAD]
    unknown = [f for f in FINDINGS if f.verdict == UNKNOWN]
    say(f"\n  {len(FINDINGS) - len(bad) - len(unknown)} confirmed, "
        f"{len(bad)} mismatched, {len(unknown)} unresolved.")

    rule("WHAT TO DO WITH THIS")
    if bad:
        say("  Do not promote this entry. Fix each MISMATCH above in "
            "app/ai/catalog.py or\n  app/ai/adapters/veo_video.py, then "
            "re-run. A mismatched field is a 400 or a\n  silently wrong clip, "
            "not a rounding error.")
    elif unknown:
        say("  Partially proven. The confirmed rows can be written down; the "
            "unresolved ones\n  still need a human to look at the clip in "
            f"{OUT}.")
    elif not GENERATED:
        say("  Every claim checked held -- but nothing has been generated, so "
            "the adapter is\n  still unproven. The free stages resolve model "
            "ids and payload shape; they\n  cannot tell you whether this "
            "request produces a clip. Re-run with --yes.")
    else:
        say(f"  Every claim checked held. {caps.display_name} can be promoted:")
        say(f"\n    app/ai/catalog.py, entry {caps.model_key!r}:")
        say(f"      status              {caps.status.value} -> active")
        lat = next((f for f in FINDINGS if "typical_latency_s" in f.claim), None)
        if lat:
            say(f"      typical_latency_s   {caps.typical_latency_s} -> "
                f"{lat.measured}  (one sample; widen it with a second run)")
        say(f"      CATALOG_VERSION     bump it -- entries changed")
        say("\n    Then write docs/adr/002-veo-verification.md recording what "
            "was measured,\n    what it cost, and what is still only a "
            "published figure. ADR 001 is the shape.")
    say("\n  Not checked by this script, and still unproven:")
    say(f"    retention_hours={caps.retention_hours}  -- needs a download "
        f"attempted >{caps.retention_hours}h later")
    say(f"    max_reference_images={caps.max_reference_images}  -- no "
        f"reference images were sent")
    say(f"    the 4/6/8s grid      -- only one duration was generated")
    say(f"    cost                 -- Gemini reports no per-call charge; "
        f"reconcile against a bill")
    return 1 if bad else 0


# --------------------------------------------------------------------------- #
def pick_inputs(args) -> tuple[Path, Path]:
    """The opening still, and the one the clip should close on.

    Defaults chosen to keep two different failures from being confused with
    each other: the opening is the establishing wide, which has no people in
    it, so a `personGeneration` policy refusal cannot masquerade as a broken
    adapter. The closing image is the character close-up, which is about as
    unlike the wide as the bake-off inputs get -- and the further apart they
    are, the less ambiguous the verdict.
    """
    if args.first and args.last:
        return Path(args.first), Path(args.last)
    pool = sorted(p for p in BAKEOFF_INPUTS.glob("*")
                  if p.suffix.lower() in MIME) if BAKEOFF_INPUTS.exists() else []
    if len(pool) < 2:
        raise SystemExit(
            f"need two input stills. Either pass --first and --last, or put "
            f"images in\n{BAKEOFF_INPUTS} (tools/bakeoff/make_placeholder_inputs.py "
            f"makes stand-ins).")
    wide = next((p for p in pool if "wide" in p.name or "establishing" in p.name),
                pool[-1])
    close = next((p for p in pool if p != wide and
                  ("closeup" in p.name or "character" in p.name)),
                 next(p for p in pool if p != wide))
    return Path(args.first) if args.first else wide, \
        Path(args.last) if args.last else close


async def run(args) -> int:
    caps = get_caps(args.model)
    duration_s = caps.durations.resolve(args.duration_s)
    resolution = caps.best_resolution(args.resolution)
    cost_cents = caps.pricing.cents(duration_s, resolution)
    generations = 2 if args.last_frame else 1

    say("=" * 74)
    say(f"  Verifying {caps.display_name}  [{caps.status.value}/{caps.tier}]")
    say(f"  model_id      {caps.model_id}")
    say(f"  adapter       app/ai/adapters/veo_video.py  (NOT the bake-off copy)")
    say(f"  env           {load_env()}")
    say(f"  cheapest run  {duration_s:g}s @ {resolution} = "
        f"{caps.pricing.describe(duration_s, resolution)} per generation")
    say("=" * 74)

    first, last = pick_inputs(args)
    for p in (first, last):
        if not p.exists():
            raise SystemExit(f"no such image: {p}")
    say(f"\n  opening still {first}")
    say(f"  closing still {last}"
        + ("" if args.last_frame else "   (stage 4 only; --last-frame)"))

    # Both Veo entries, not just the one being generated with: the check is
    # free and they share this adapter, so a wrong id in the other entry is
    # worth learning now rather than the first time someone selects it.
    veo_keys = sorted(k for k, c in CATALOG.items() if c.adapter == "veo")
    if not await stage_credentials(veo_keys or [args.model]):
        say("\n  Stopping: a paid call against a key or model id that does not "
            "resolve can only\n  waste time. Nothing was charged.")
        return report(args.model) or 2

    stage_payload(args.model, first, last if args.last_frame else None,
                  duration_s, resolution, args.person_generation)

    total_usd = cost_cents * generations / 100
    rule(f"SPEND  {generations} generation(s), ~${total_usd:.2f}")
    if total_usd > args.max_cost_usd:
        say(f"  REFUSED: ${total_usd:.2f} exceeds the --max-cost-usd ceiling of "
            f"${args.max_cost_usd:.2f}.\n  Raise it deliberately; the estimate "
            f"is never the authorization.")
        return report(args.model) or 2
    if not args.yes:
        say(f"  Nothing sent and nothing charged. The two free stages above "
            f"are the ones that\n  catch most of it -- read them first. To "
            f"generate, re-run with --yes"
            + ("" if args.last_frame else " (add --last-frame\n  to also prove "
               "continuity, for a second generation)") + ".")
        return report(args.model)

    rule(f"STAGE 3  one generation, no closing frame     (~${cost_cents / 100:.2f})")
    clip = await generate(
        args.model, first, None,
        prompt="A slow push in. The light shifts across the scene. "
               "Nothing else moves.",
        duration_s=duration_s, resolution=resolution,
        person_generation=args.person_generation, poll_s=args.poll_s,
        label="baseline")
    if clip is None:
        return report(args.model) or 2
    measure_against_catalogue(args.model, clip, duration_s, resolution,
                              "baseline")

    if not args.last_frame:
        say("\n  Continuity itself is still unproven -- re-run with "
            "--last-frame.")
        return report(args.model)

    if not caps.supports_last_frame:
        say(f"\n  {caps.display_name} declares no last_frame_field, so there is "
            f"nothing to test.")
        return report(args.model)

    rule(f"STAGE 4  one generation, ending on a given frame  "
         f"(~${cost_cents / 100:.2f})")
    clip2 = await generate(
        args.model, first, last,
        prompt="The scene transforms, settling into its final composition.",
        duration_s=duration_s, resolution=resolution,
        person_generation=args.person_generation, poll_s=args.poll_s,
        label="last-frame")
    if clip2 is None:
        return report(args.model) or 2
    measure_against_catalogue(args.model, clip2, duration_s, resolution,
                              "last-frame")
    stage_last_frame_verdict(clip2, first, last)
    return report(args.model)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="veo-3.1-fast-i2v",
                    help="catalogue key (default: the cheaper Veo entry)")
    ap.add_argument("--yes", action="store_true",
                    help="actually generate. THIS SPENDS MONEY.")
    ap.add_argument("--last-frame", action="store_true",
                    help="add a second generation that proves (or disproves) "
                         "lastFrame")
    ap.add_argument("--first", help="opening still")
    ap.add_argument("--last", help="the still the clip should close on")
    ap.add_argument("--duration-s", type=float, default=4.0,
                    help="snapped to the model's grid (default 4: cheapest)")
    ap.add_argument("--resolution", default="720p")
    ap.add_argument("--max-cost-usd", type=float, default=1.50,
                    help="authorization ceiling for the whole run")
    ap.add_argument("--person-generation",
                    default=os.getenv("VEO_PERSON_GENERATION", "allow_adult"))
    ap.add_argument("--poll-s", type=float, default=10.0)
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        say("\n  interrupted. A submitted generation may still complete and be "
            "billed.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
