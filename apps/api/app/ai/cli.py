"""Run the text pipeline from the command line.

    python -m app.ai.cli analyze    tests/fixtures/ai/lighthouse.txt
    python -m app.ai.cli storyboard tests/fixtures/ai/lighthouse.txt --out sb.json
    python -m app.ai.cli storyboard story.txt --fake        # zero spend
    python -m app.ai.cli storyboard story.txt --model kling-2.5-turbo-i2v
    python -m app.ai.cli models                             # the catalogue

Reachable without the API or a database, so prompts can be tuned in a tight
loop -- which is the whole point of this milestone.

`storyboard` also prints the motion plan: the clip length each shot will
actually get on the chosen model's grid, the motion prompt it will be animated
with, and what the whole film would cost. That report is the dry run. It spends
nothing, and it is the thing to read before authorising a real generation.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from ..schemas.ai import StoryAnalysis
from ..services.motion_service import DEFAULT_MODEL_KEY, plan_film_durations
from ..services.story_service import analyze_story
from ..services.storyboard_service import (StoryboardRequest,
                                           generate_storyboard)
from .catalog import CATALOG, PriceConfidence
from .catalog import get as get_caps
from .pacing import required_seconds
from .ports import AIError
from .prompts.compose import compose_motion_prompt
from .registry import get_text_port, reset


def _port(fake: bool):
    if fake:
        os.environ["AI_TEXT_PROVIDER"] = "fake"
    reset()
    return get_text_port()


def _report(label: str, res) -> None:
    u = res.usage
    print(f"\n  {label}", file=sys.stderr)
    print(f"    model     {u.model}", file=sys.stderr)
    print(f"    tokens    {u.input_tokens} in / {u.output_tokens} out"
          + (f"  (cache read {u.cache_read_tokens})" if u.cache_read_tokens else ""),
          file=sys.stderr)
    print(f"    cost      {u.cost_cents:.2f}c", file=sys.stderr)
    print(f"    latency   {u.latency_ms / 1000:.1f}s", file=sys.stderr)
    if res.repaired:
        print(f"    REPAIRED  {len(res.repair_errors)} validator error(s) fed back:",
              file=sys.stderr)
        for e in res.repair_errors[:4]:
            print(f"      {e}", file=sys.stderr)


async def _run(args) -> int:
    story = Path(args.story).read_text()
    port = _port(args.fake)

    analysis_res = None
    if args.command == "analyze" or not args.analysis:
        analysis_res = await analyze_story(story, port, effort=args.effort)
        _report("story analysis", analysis_res)
        analysis = analysis_res.value
    else:
        analysis = StoryAnalysis.model_validate_json(Path(args.analysis).read_text())

    if args.command == "analyze":
        out = args.out or "-"
        payload = analysis.model_dump_json(indent=2)
        _emit(payload, out)
        print(f"\n  {analysis.title}: {len(analysis.characters)} characters, "
              f"{len(analysis.beats)} beats", file=sys.stderr)
        return 0

    res = await generate_storyboard(
        StoryboardRequest(story_text=story, analysis=analysis,
                          target_length_s=args.length,
                          aspect_ratio=args.aspect, notes=args.notes or "",
                          motion_model_key=args.model),
        port, effort=args.effort)
    _report("storyboard", res)
    sb = res.value
    _emit(sb.model_dump_json(indent=2), args.out or "-")
    print(f"\n  {sb.title}: {len(sb.scenes)} scenes, {sb.shot_count} shots, "
          f"{sb.total_target_duration_s:.0f}s target", file=sys.stderr)
    high = [f"{sc.local_index}" for sc in sb.scenes for sh in sc.shots
            if sh.motion_priority == "high"]
    print(f"  motion-priority high on {len(high)} shot(s): scenes {', '.join(high)}",
          file=sys.stderr)

    motion_report(sb, args.model, args.length)

    total = (analysis_res.usage.cost_cents if analysis_res else 0) + res.usage.cost_cents
    print(f"\n  text spend this run: {total:.2f}c "
          f"(the motion estimate above is NOT spent -- this command only "
          f"plans)", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# The motion report: what will be generated, how long each clip will be, and
# what the film costs. Printed before anything is bought, because a generated
# clip is roughly fourteen times the price of a still and the whole difference
# between a good plan and an expensive one is visible right here.
# --------------------------------------------------------------------------- #
def motion_report(sb, model_key: str, target_length_s: int,
                  stream=sys.stderr) -> dict:
    caps = get_caps(model_key)

    shots = []
    for sc in sb.scenes:
        for sh in sc.shots:
            # Scene-level narration with no shot attaches to the first shot,
            # the same rule the timeline builder uses -- otherwise the report
            # would under-count the words a shot has to carry.
            words = sum(
                n.word_count for n in sc.narration
                if n.shot_local_index == sh.local_index
                or (n.shot_local_index is None
                    and sh.local_index == sc.shots[0].local_index))
            shots.append((sc, sh, words))

    plan = plan_film_durations(
        [(f"{sc.local_index}:{sh.local_index}", sh.target_duration_s, w)
         for sc, sh, w in shots], caps, float(target_length_s))

    def out(line: str = "") -> None:
        print(line, file=stream)

    res = caps.resolutions[0]
    out()
    out(f"  Motion plan -- {caps.display_name}")
    out(f"    endpoint      {caps.model_id}")
    out(f"    clip lengths  {caps.durations.describe()}"
        + ("   (fixed resolution)" if not caps.resolution_selectable else ""))
    out(f"    price         "
        f"{caps.pricing.describe(caps.durations.resolve(5), res)}"
        f"  [{caps.pricing.confidence.value}]")
    out()

    total_cents = 0
    n = len(shots)
    for i, ((sc, sh, words), alloc) in enumerate(zip(shots, plan.allocations), 1):
        motion = compose_motion_prompt(
            subject_motion=sh.subject_motion,
            environment_motion=getattr(sh, "environment_motion", ""),
            camera_move=sh.camera_move,
            motion_pacing=getattr(sh, "motion_pacing", "slow"),
            supports_negative_prompt=caps.supports_negative_prompt)
        cents = caps.pricing.cents(alloc.resolved_s, res)
        total_cents += cents

        flag = ""
        if words and required_seconds(words) > alloc.resolved_s + 0.05:
            flag = (f"   ! {words}w needs {required_seconds(words):.1f}s "
                    f"-- the last frame will be held")
        elif not sh.has_motion_plan:
            flag = "   ! no motion direction; the model will invent it"

        out(f"  [{i:02d}/{n:02d}] {sc.title[:44]}")
        out(f"         duration: {alloc.resolved_s:g}s"
            f"   model: {caps.model_key}"
            f"   cost: {cents / 100:.2f} USD{flag}")
        out(f"         motion:   {motion.positive[:132]}"
            + ("..." if len(motion.positive) > 132 else ""))

    out()
    out(f"  {plan.summary()}")
    if abs(plan.drift_s) > target_length_s * 0.1:
        out(f"  ! {abs(plan.drift_s):.0f}s off the {target_length_s}s target. "
            f"The shortest film this")
        out(f"    model can make of this storyboard is "
            f"{plan.floor_total_s:.0f}s -- the narration sets")
        out(f"    that floor, not the model. Cut words or pick a finer grid.")
    out(f"  Estimated cost: {total_cents / 100:.2f} USD for {n} clips"
        + ("" if caps.pricing.confidence is PriceConfidence.VERIFIED
           else f"   [price is {caps.pricing.confidence.value};"
                f" indicative only]"))
    out(f"  Price source:   {caps.pricing.source}")
    return {"total_cents": total_cents, "planned_s": plan.planned_total_s,
            "shots": n}


def list_models(stream=sys.stderr) -> int:
    print("\n  Image-to-video catalogue   (* = default)\n", file=stream)
    for caps in CATALOG.values():
        mark = "*" if caps.model_key == DEFAULT_MODEL_KEY else " "
        five = caps.pricing.cents(caps.durations.resolve(5),
                                  caps.resolutions[0]) / 100
        print(f"  {mark} {caps.model_key:24} {caps.status.value:12} "
              f"{caps.durations.describe():10} {five:>5.2f} USD/5s  "
              f"{caps.tier.value}", file=stream)
    print("\n  Only ACTIVE entries can be generated with. EXPERIMENTAL ones "
          "have never had\n  their request shape verified against the live "
          "API, so the first call would\n  be the test.\n", file=stream)
    return 0


def _emit(payload: str, out: str) -> None:
    if out == "-":
        print(payload)
    else:
        Path(out).write_text(payload)
        print(f"\n  wrote {out}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.ai.cli", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["analyze", "storyboard", "models"])
    ap.add_argument("story", type=Path, nargs="?")
    ap.add_argument("--model", default=DEFAULT_MODEL_KEY,
                    choices=sorted(CATALOG),
                    help="image-to-video model the film will be animated with. "
                         "Its clip-length grid constrains the storyboard, so "
                         "it is chosen here rather than afterwards.")
    ap.add_argument("--analysis", type=Path,
                    help="reuse a saved analysis instead of re-reading the story")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    ap.add_argument("--length", type=int, default=90, help="target runtime (s)")
    ap.add_argument("--aspect", default="16:9")
    ap.add_argument("--notes", help="extra direction for the storyboard")
    ap.add_argument("--effort", default="high",
                    choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--fake", action="store_true", help="use fixtures, spend nothing")
    args = ap.parse_args(argv)

    if args.command == "models":
        return list_models()
    if args.story is None:
        ap.error("a story file is required for 'analyze' and 'storyboard'")

    try:
        return asyncio.run(_run(args))
    except AIError as exc:
        print(f"\nerror [{exc.kind}:{exc.code}] {exc.detail}", file=sys.stderr)
        if exc.raw:
            print(f"\nraw response (truncated):\n{exc.raw[:800]}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
