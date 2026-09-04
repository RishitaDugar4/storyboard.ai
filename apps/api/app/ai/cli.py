"""Run the text pipeline from the command line.

    python -m app.ai.cli analyze    tests/fixtures/ai/lighthouse.txt
    python -m app.ai.cli storyboard tests/fixtures/ai/lighthouse.txt --out sb.json
    python -m app.ai.cli storyboard story.txt --fake        # zero spend

Reachable without the API or a database, so prompts can be tuned in a tight
loop -- which is the whole point of this milestone.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from ..schemas.ai import StoryAnalysis
from ..services.story_service import analyze_story
from ..services.storyboard_service import (StoryboardRequest,
                                           generate_storyboard)
from .ports import AIError
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
                          aspect_ratio=args.aspect, notes=args.notes or ""),
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
    total = (analysis_res.usage.cost_cents if analysis_res else 0) + res.usage.cost_cents
    print(f"  total spend this run: {total:.2f}c", file=sys.stderr)
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
    ap.add_argument("command", choices=["analyze", "storyboard"])
    ap.add_argument("story", type=Path)
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
