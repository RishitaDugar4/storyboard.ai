#!/usr/bin/env python3
"""Run the text pipeline over a corpus and report what came back.

M2's exit criterion is "feed three real stories; assert valid storyboards", but
validity is the floor, not the goal -- the schema already guarantees it or the
call fails. What this measures is the stuff a validator cannot: whether the
director kept the cast, paced the film to length, and marked enough shots as
worth animating.

    python tools/eval_storyboards.py                 # whole corpus
    python tools/eval_storyboards.py --fake          # zero spend
    python tools/eval_storyboards.py --only tuesday
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from app.ai.pacing import word_budget                      # noqa: E402
from app.ai.ports import AIError                           # noqa: E402
from app.ai.registry import get_text_port, reset           # noqa: E402
from app.schemas.ai import StoryAnalysis, Storyboard       # noqa: E402
from app.services.story_service import analyze_story       # noqa: E402
from app.services.storyboard_service import (StoryboardRequest,  # noqa: E402
                                             generate_storyboard)

STORIES = HERE / "tests" / "fixtures" / "ai" / "stories"
OUT = HERE / "out" / "eval"


@dataclass
class Result:
    name: str
    ok: bool
    error: str = ""
    cost_cents: float = 0.0
    seconds: float = 0.0
    repaired: bool = False
    repair_errors: list[str] = field(default_factory=list)
    analysis: StoryAnalysis | None = None
    storyboard: Storyboard | None = None

    # --- quality signals -------------------------------------------------
    @property
    def cast_retention(self) -> str:
        """Characters the analysis found vs. those the storyboard kept.

        A named character dropped between stages is a face missing from the
        film, and no validator can see it.
        """
        if not (self.analysis and self.storyboard):
            return "-"
        found = [c for c in self.analysis.characters if c.role != "incidental"]
        kept = len(self.storyboard.characters)
        return f"{kept}/{len(found)}"

    @property
    def dropped_characters(self) -> list[str]:
        if not (self.analysis and self.storyboard):
            return []
        kept = {c.name.lower() for c in self.storyboard.characters}
        return [c.name for c in self.analysis.characters
                if c.role in ("protagonist", "supporting")
                and not any(k in c.name.lower() or c.name.lower() in k for k in kept)]

    @property
    def motion_mix(self) -> dict[str, int]:
        if not self.storyboard:
            return {}
        mix = {"high": 0, "medium": 0, "low": 0}
        for sc in self.storyboard.scenes:
            for sh in sc.shots:
                mix[sh.motion_priority] += 1
        return mix

    @property
    def budget_use(self) -> float:
        """Mean fraction of each shot's word budget actually used.

        Very low means the film is mostly silence; near 1.0 means every line
        is one word from failing validation.
        """
        if not self.storyboard:
            return 0.0
        ratios = []
        for sc in self.storyboard.scenes:
            for sh in sc.shots:
                used = sum(n.word_count for n in sc.narration
                           if n.shot_local_index in (None, sh.local_index))
                b = word_budget(sh.target_duration_s)
                if b:
                    ratios.append(used / b)
        return sum(ratios) / len(ratios) if ratios else 0.0


async def run_one(path: Path, port, target_s: int) -> Result:
    started = time.perf_counter()
    r = Result(name=path.stem, ok=False)
    story = path.read_text()
    try:
        a = await analyze_story(story, port)
        r.analysis, r.cost_cents = a.value, a.usage.cost_cents
        sb = await generate_storyboard(
            StoryboardRequest(story_text=story, analysis=a.value,
                              target_length_s=target_s), port)
        r.storyboard = sb.value
        r.cost_cents += sb.usage.cost_cents
        r.repaired = sb.repaired or a.repaired
        r.repair_errors = sb.repair_errors or a.repair_errors
        r.ok = True
    except AIError as exc:
        r.error = f"{exc.kind}:{exc.code} {exc.detail}"
    except Exception as exc:                       # noqa: BLE001
        r.error = f"{type(exc).__name__}: {exc}"[:200]
    r.seconds = time.perf_counter() - started
    return r


def report(results: list[Result], target_s: int) -> None:
    print("\n" + "=" * 96)
    print(f"{'story':16} {'ok':3} {'scenes':>6} {'runtime':>8} {'cast':>6} "
          f"{'budget':>7} {'motion h/m/l':>13} {'rep':>4} {'cost':>7} {'time':>6}")
    print("-" * 96)
    for r in results:
        if not r.ok:
            print(f"{r.name:16} {'FAIL':3}")
            for line in r.error.splitlines():
                print(f"{'':19}{line}")
            continue
        sb = r.storyboard
        m = r.motion_mix
        drift = sb.total_target_duration_s - target_s
        print(f"{r.name:16} {'ok':3} {len(sb.scenes):>6} "
              f"{sb.total_target_duration_s:>5.0f}s{drift:>+3.0f} {r.cast_retention:>6} "
              f"{r.budget_use * 100:>6.0f}% "
              f"{m['high']:>4}/{m['medium']}/{m['low']:<5} "
              f"{'yes' if r.repaired else '-':>4} {r.cost_cents:>6.1f}c {r.seconds:>5.0f}s")
    print("-" * 96)
    ok = [r for r in results if r.ok]
    print(f"{len(ok)}/{len(results)} valid   "
          f"total {sum(r.cost_cents for r in results):.1f}c   "
          f"{sum(r.seconds for r in results):.0f}s")

    print("\nquality notes:")
    for r in ok:
        notes = []
        if dropped := r.dropped_characters:
            notes.append(f"dropped from cast: {', '.join(dropped)}")
        m = r.motion_mix
        total = sum(m.values())
        if total and m["high"] / total < 0.2:
            notes.append(f"only {m['high']}/{total} shots worth animating")
        if abs(r.storyboard.total_target_duration_s - target_s) > target_s * 0.2:
            notes.append(f"runtime {r.storyboard.total_target_duration_s:.0f}s "
                         f"vs {target_s}s target")
        if r.budget_use < 0.55:
            notes.append(f"narration only {r.budget_use * 100:.0f}% of budget "
                         "(film may feel silent)")
        if r.repair_errors:
            codes = {e.split(":")[0].strip("- ") for e in r.repair_errors}
            notes.append(f"repaired: {', '.join(sorted(codes))[:60]}")
        print(f"  {r.name:16} " + ("; ".join(notes) if notes else "clean"))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--only", help="run a single story by stem")
    ap.add_argument("--length", type=int, default=90)
    args = ap.parse_args()

    if args.fake:
        os.environ["AI_TEXT_PROVIDER"] = "fake"
    reset()
    port = get_text_port()

    paths = sorted(STORIES.glob("*.txt"))
    if args.only:
        paths = [p for p in paths if p.stem == args.only]
    if not paths:
        print(f"no stories in {STORIES}", file=sys.stderr)
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    print(f"running {len(paths)} stor{'y' if len(paths) == 1 else 'ies'} "
          f"at {args.length}s target "
          f"({os.getenv('AI_TEXT_PROVIDER', 'gemini')}/"
          f"{os.getenv('AI_TEXT_MODEL', 'default')})")

    results = await asyncio.gather(*(run_one(p, port, args.length) for p in paths))
    for r in results:
        if r.storyboard:
            (OUT / f"{r.name}.storyboard.json").write_text(
                r.storyboard.model_dump_json(indent=2))
        if r.analysis:
            (OUT / f"{r.name}.analysis.json").write_text(
                r.analysis.model_dump_json(indent=2))

    report(list(results), args.length)
    print(f"\noutputs in {OUT}")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
