"""Render a Timeline from the command line.

    python -m app.render.cli timeline.json out.mp4
    python -m app.render.cli timeline.json --preflight-only

The renderer is deliberately reachable without the API, the database or a
browser: it makes the pipeline testable from fixtures, and it is the escape
hatch for hand-assembling a film if anything upstream fails near a deadline.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .ffmpeg import capabilities
from .pipeline import render
from .preflight import preflight
from .timeline import Profile, Timeline


def _bar(msg: str, frac: float) -> None:
    width = 28
    filled = int(frac * width)
    sys.stderr.write(f"\r  [{'#' * filled}{'.' * (width - filled)}] "
                     f"{frac * 100:3.0f}%  {msg[:46]:<46}")
    sys.stderr.flush()
    if frac >= 1.0:
        sys.stderr.write("\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.render.cli", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("timeline", type=Path)
    ap.add_argument("output", type=Path, nargs="?")
    ap.add_argument("--profile", choices=[p.value for p in Profile],
                    help="override the timeline's profile")
    ap.add_argument("--preflight-only", action="store_true")
    ap.add_argument("--keep-workdir", action="store_true",
                    help="retain intermediates and ffmpeg logs")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    caps = capabilities()
    if not caps.ffmpeg:
        print("error: ffmpeg not found (macOS: brew install ffmpeg)", file=sys.stderr)
        return 2

    tl = Timeline.load(args.timeline)
    if args.profile:
        tl.profile = Profile(args.profile)

    if args.preflight_only:
        report = preflight(tl)
        print(f"preflight — {len(tl.clips)} clips, "
              f"{tl.total_duration_ms / 1000:.1f}s, profile={tl.profile}")
        print(report.render())
        return 0 if report.ok else 1

    if not args.output:
        ap.error("output path is required unless --preflight-only")

    try:
        res = render(
            tl, args.output,
            cache_dir=None if not args.no_cache else Path("/dev/null-disabled"),
            on_status=None if args.quiet else _bar,
            keep_workdir=args.keep_workdir,
        )
    except Exception as exc:
        sys.stderr.write("\n")
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if res.preflight and res.preflight.advisory:
        print("advisories:")
        for i in res.preflight.advisory:
            print(f"  warn [{i.code}] {i.where}: {i.message}")
    for w in res.warnings:
        print(f"  note: {w}")

    print(f"\n  {res.video}")
    print(f"  {res.width}x{res.height}  {res.duration_ms / 1000:.2f}s  "
          f"{res.bytes / 1e6:.1f} MB  (drift {res.drift_ms:+d}ms)")
    print(f"  cache {res.cache_hits} hit / {res.cache_misses} built   "
          f"rendered in {res.elapsed_s:.1f}s")
    for label, p in (("poster", res.poster), ("subs", res.srt), ("vtt", res.vtt)):
        if p:
            print(f"  {label:7} {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
