"""Generate ONE real clip, end to end, and measure what came back.

    python tools/motion_smoke.py --keyframe frame.png            # plan only
    python tools/motion_smoke.py --keyframe frame.png --yes      # spends money

The point is to spend the price of a single clip before spending the price of
a film. It uses the same catalogue, the same adapter and the same validator as
the job handlers -- so if this works, the pipeline works, and if it fails it
fails on 35 cents instead of on a whole storyboard.

No database and no worker: the failure modes worth finding first are the
provider's, not ours.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.catalog import CATALOG, PriceConfidence            # noqa: E402
from app.ai.catalog import get as get_caps                     # noqa: E402
from app.ai.ports import AIError, VideoRequest                 # noqa: E402
from app.ai.prompts.compose import compose_motion_prompt       # noqa: E402
from app.ai.registry import get_video_port                     # noqa: E402
from app.jobs.handlers.motion import ClipInvalid, validate_clip  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "out" / "motion-smoke"

DEFAULT_SUBJECT = ("The woman slowly turns her head toward the door and takes "
                   "one cautious step backward, blinking once")
DEFAULT_ENVIRONMENT = "Her hair lifts slightly as a cold draught crosses the room"


def log(msg: str = "") -> None:
    print(msg, file=sys.stderr, flush=True)


async def run(args) -> int:
    caps = get_caps(args.model)
    keyframe = Path(args.keyframe)
    if not keyframe.exists():
        log(f"no such keyframe: {keyframe}")
        return 2

    motion = compose_motion_prompt(
        subject_motion=args.subject, environment_motion=args.environment,
        camera_move=args.camera, motion_pacing=args.pacing,
        supports_negative_prompt=caps.supports_negative_prompt)
    duration = caps.durations.resolve(args.duration)
    resolution = caps.best_resolution(args.resolution)
    cents = caps.pricing.cents(duration, resolution)

    log()
    log(f"  model      {caps.display_name}  ({caps.model_key})")
    log(f"  endpoint   {caps.model_id}")
    log(f"  duration   {args.duration:g}s requested -> {duration:g}s "
        f"(this model produces {caps.durations.describe()})")
    log(f"  resolution {resolution}"
        + ("" if caps.resolution_selectable else "  (not selectable)"))
    log(f"  keyframe   {keyframe}  ({keyframe.stat().st_size / 1024:.0f} KB)")
    log(f"  fields     {sorted(caps.request_fields)}")
    log()
    log(f"  prompt     {motion.positive}")
    if motion.negative:
        log(f"  negative   {motion.negative[:110]}...")
    log()
    log(f"  COST       {cents / 100:.2f} USD"
        + ("" if caps.pricing.confidence is PriceConfidence.VERIFIED
           else f"   [{caps.pricing.confidence.value} -- indicative only]"))
    log(f"  source     {caps.pricing.source}")
    log()

    if not args.yes:
        log("  Planning only. Nothing was sent and nothing was charged.")
        log("  Re-run with --yes to actually generate this clip.")
        return 0

    port = get_video_port(caps.adapter)
    started = time.perf_counter()
    try:
        log("  submitting...")
        sub = await port.submit(VideoRequest(
            model_key=caps.model_key, model_id=caps.model_id,
            first_frame=keyframe.read_bytes(),
            first_frame_mime="image/png" if keyframe.suffix == ".png"
            else "image/jpeg",
            prompt=motion.positive,
            negative_prompt=motion.negative or None,
            reference_images=[], duration_s=duration, resolution=resolution,
            aspect_ratio=args.aspect, seed=None))
        log(f"  provider job {sub.provider_job_id}")

        deadline = time.monotonic() + caps.max_wait_s
        while time.monotonic() < deadline:
            await asyncio.sleep(args.poll_s)
            state = await port.poll(sub)
            elapsed = time.perf_counter() - started
            if state.error is not None:
                log(f"  FAILED after {elapsed:.0f}s: "
                    f"[{state.error.kind}:{state.error.code}] {state.error.detail}")
                return 1
            if state.done:
                log(f"  ready after {elapsed:.0f}s "
                    f"(catalogue says ~{caps.typical_latency_s}s)")
                break
            log(f"    {state.progress_hint or 'rendering'}  ({elapsed:.0f}s)")
        else:
            log(f"  TIMEOUT after {caps.max_wait_s}s. The generation may still "
                f"complete and be billed -- check before resubmitting.")
            return 1

        log("  downloading...")
        result = await port.fetch(state)
        latency = time.perf_counter() - started
    except AIError as exc:
        log(f"  FAILED [{exc.kind}:{exc.code}] {exc.detail}")
        return 1
    finally:
        await port.aclose()

    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / f"{caps.model_key}-{int(time.time())}.mp4"
    dest.write_bytes(result.data)

    try:
        measured = validate_clip(result.data, duration)
    except ClipInvalid as exc:
        # Keep the file regardless: it was paid for, and looking at a clip
        # that failed validation is how you find out why.
        log(f"\n  VALIDATION FAILED: {exc}")
        log(f"  the clip was kept anyway at {dest}")
        return 1

    log()
    log(f"  wrote {dest}  ({dest.stat().st_size / 1_000_000:.1f} MB)")
    log(f"  measured: {measured['duration_ms']}ms  "
        f"{measured['width']}x{measured['height']}  "
        f"{measured['fps']}fps  audio={measured['has_audio']}")
    drift = measured["duration_ms"] - int(duration * 1000)
    log(f"  duration drift: {drift:+d}ms against the {duration:g}s requested")
    log(f"  latency: {latency:.0f}s")
    log()
    log(f"  Charged approximately {cents / 100:.2f} USD. Watch the clip before "
        f"generating a whole film:")
    log(f"    open {dest}")

    (OUT / "last-run.json").write_text(json.dumps({
        "model_key": caps.model_key, "model_id": caps.model_id,
        "prompt": motion.positive, "requested_duration_s": duration,
        "measured": measured, "latency_s": round(latency, 1),
        "estimated_cost_cents": cents, "file": str(dest),
    }, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keyframe", required=True, type=Path,
                    help="the still to animate; it becomes the first frame")
    ap.add_argument("--model", default="kling-2.5-turbo-i2v",
                    choices=sorted(CATALOG))
    ap.add_argument("--duration", type=float, default=5.0)
    ap.add_argument("--resolution", default="1080p")
    ap.add_argument("--aspect", default="16:9")
    ap.add_argument("--camera", default="push_in")
    ap.add_argument("--pacing", default="slow",
                    choices=["still", "slow", "steady", "brisk"])
    ap.add_argument("--subject", default=DEFAULT_SUBJECT)
    ap.add_argument("--environment", default=DEFAULT_ENVIRONMENT)
    ap.add_argument("--poll-s", type=float, default=10.0)
    ap.add_argument("--yes", action="store_true",
                    help="actually spend money. Without it this only plans.")
    return asyncio.run(run(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
