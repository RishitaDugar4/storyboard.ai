#!/usr/bin/env python3
"""Provider bake-off harness (M0.5).

Runs the same standardized experiment across configured image-to-video models,
measures latency and cost, saves every clip with complete reproducibility
metadata, and produces a comparison report and contact sheet.

Standalone: no database, no FastAPI, no application imports. Its catalogue,
planning and adapter modules graduate into ``apps/api/app/ai/`` unchanged.

    python run.py --fake                 # zero-spend smoke test
    python run.py --preflight            # credential + endpoint check
    python run.py                        # plan and price only (no spend)
    python run.py --yes                  # execute
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import adapters                                            # noqa: E402
import report as reporting                                 # noqa: E402
from adapters.base import (ErrorKind, ProviderError,        # noqa: E402
                           VideoRequest)
from catalog import CATALOG_VERSION, ModelStatus            # noqa: E402
from catalog import get as get_caps                         # noqa: E402
from planning import (CostAuthorizationError, GenerationPlan,  # noqa: E402
                      authorize, plan_motion)
from probe import HAVE_FFMPEG, HAVE_FFPROBE, probe_video, sha256_file  # noqa: E402
from prompts import CASES_BY_ID, COMPOSER_VERSION, Case, compose_motion_prompt  # noqa: E402
from records import RECORD_SCHEMA_VERSION, GenerationRecord, iso, utcnow  # noqa: E402

DEFAULT_CONFIG = HERE / "bakeoff.yaml"


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict:
    if not path.exists():
        die(f"config not found: {path}")
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ModuleNotFoundError:
            die("PyYAML is required for .yaml config.\n"
                "  pip install -r tools/bakeoff/requirements.txt\n"
                "(or pass a .json config with --config)")
        return yaml.safe_load(text) or {}
    return json.loads(text)


def die(msg: str, code: int = 2) -> None:
    print(f"\n error: {msg}\n", file=sys.stderr)
    raise SystemExit(code)


# --------------------------------------------------------------------------- #
# matrix
# --------------------------------------------------------------------------- #
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def discover_images(inputs_dir: Path) -> list[Path]:
    if not inputs_dir.exists():
        die(f"inputs directory not found: {inputs_dir}")
    imgs = sorted(p for p in inputs_dir.iterdir()
                  if p.suffix.lower() in IMAGE_EXTS and not p.name.startswith("."))
    if not imgs:
        die(f"no images in {inputs_dir}. Add three stills (see README.md), or run\n"
            "  python make_placeholder_inputs.py")
    return imgs


class Item:
    """One cell of the experiment matrix."""

    __slots__ = ("image", "case", "model_key", "repeat", "duration_s", "plan")

    def __init__(self, image: Path, case: Case, model_key: str, repeat: int,
                 duration_s: float) -> None:
        self.image, self.case = image, case
        self.model_key, self.repeat, self.duration_s = model_key, repeat, duration_s
        self.plan: GenerationPlan | None = None

    @property
    def image_id(self) -> str:
        return self.image.stem

    @property
    def label(self) -> str:
        return f"{self.image_id}/{self.case.case_id}/{self.model_key}#{self.repeat}"

    def output_path(self, out: Path) -> Path:
        return (out / "clips" / self.model_key /
                f"{self.image_id}--{self.case.case_id}--{self.duration_s:g}s"
                f"--{self.repeat}.mp4")


def build_matrix(cfg: dict, images: list[Path], model_keys: list[str],
                 cases: list[Case], repeats: int) -> list[Item]:
    limits: dict[str, int] = {
        k: int(v) for k, v in (cfg.get("sample_limits") or {}).items()
    }
    durations = [float(d) for d in cfg.get("durations", [6])]
    items: list[Item] = []
    for model_key in model_keys:
        made = 0
        cap = limits.get(model_key)
        for image in images:
            for case in cases:
                for d in durations:
                    for rep in range(repeats):
                        if cap is not None and made >= cap:
                            break
                        items.append(Item(image, case, model_key, rep, d))
                        made += 1
    return items


def make_plan(item: Item, cfg: dict, *, allow_experimental: bool) -> GenerationPlan:
    caps = get_caps(item.model_key)
    prompt = compose_motion_prompt(
        caps=caps,
        subject_motion=item.case.subject_motion,
        camera_move=item.case.camera_move,
        motion_language=cfg.get("motion_language",
                                "Gentle, unhurried camera movement."),
        ambient_sound=item.case.ambient_sound,
    )
    return plan_motion(
        model_key=item.model_key,
        prompt=prompt.positive,
        negative_prompt_terms=(prompt.negative.split(", ") if prompt.negative else []),
        first_frame_sha256=sha256_file(item.image),
        reference_sha256=[],            # bake-off ships no locked characters
        target_duration_s=item.duration_s,
        preferred_resolution=cfg.get("resolution", "1080p"),
        aspect_ratio=cfg.get("aspect_ratio", "16:9"),
        seed=cfg.get("seed"),
        composer_version=COMPOSER_VERSION,
        narration_word_count=item.case.narration_word_count,
        allow_premium=True,             # the bake-off exists to price premium
        allow_experimental=allow_experimental,
    )


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
async def run_one(item: Item, cfg: dict, registry, out: Path, run_id: str,
                  max_cost_cents: int, sem: asyncio.Semaphore,
                  time_scale: float = 1.0) -> GenerationRecord:
    plan = item.plan
    caps = get_caps(item.model_key)
    created = utcnow()

    def record(**kw) -> GenerationRecord:
        base = dict(
            record_schema_version=RECORD_SCHEMA_VERSION, run_id=run_id,
            image_id=item.image_id, case_id=item.case.case_id,
            repeat_index=item.repeat,
            adapter=caps.adapter, provider=caps.provider, model_key=caps.model_key,
            model_id=caps.model_id, model_version=None, provider_job_id=None,
            provider_endpoint=None, catalog_version=CATALOG_VERSION,
            composer_version=COMPOSER_VERSION,
            prompt=plan.prompt, negative_prompt=plan.negative_prompt,
            first_frame_path=str(item.image), first_frame_sha256=plan.first_frame_sha256,
            reference_sha256=plan.reference_sha256, seed=plan.seed,
            requested_duration_s=plan.requested_duration_s,
            resolved_duration_s=plan.resolved_duration_s,
            requested_resolution=plan.requested_resolution,
            resolved_resolution=plan.resolved_resolution,
            aspect_ratio=plan.aspect_ratio,
            capability_warnings=[{"code": w.code, "message": w.message}
                                 for w in plan.warnings],
            narration_text=item.case.narration,
            narration_word_count=item.case.narration_word_count,
            narration_fit_status=(str(plan.narration_fit.status)
                                  if plan.narration_fit else None),
            narration_word_budget=(plan.narration_fit.word_budget
                                   if plan.narration_fit else None),
            narration_slack_s=(plan.narration_fit.slack_s
                               if plan.narration_fit else None),
            estimated_cost_cents=plan.estimated_cost_cents, actual_cost_cents=None,
            cost_source="estimated", price_confidence=str(plan.price_confidence),
            price_source=plan.price_source,
            price_verified_at=(plan.price_verified_at.isoformat()
                               if plan.price_verified_at else None),
            max_authorized_cost_cents=max_cost_cents,
            status="failed", error_code=None, error_detail=None,
            created_at=iso(created), submitted_at=None, completed_at=None,
            downloaded_at=None, expires_at=None, latency_ms=None, poll_count=0,
            input_hash=plan.input_hash, output_path=None, output_sha256=None,
            output_bytes=None, measured_duration_ms=None, measured_fps=None,
            measured_width=None, measured_height=None, measured_has_audio=None,
            probe_ok=False,
        )
        base.update(kw)
        return GenerationRecord(**base)

    async with sem:
        port = adapters.port_for(registry, item.model_key)
        req = VideoRequest(
            model_key=plan.model_key, model_id=plan.model_id,
            first_frame_path=item.image, prompt=plan.prompt,
            negative_prompt=plan.negative_prompt, reference_paths=[],
            duration_s=plan.resolved_duration_s, resolution=plan.resolved_resolution,
            aspect_ratio=plan.aspect_ratio, seed=plan.seed,
        )
        try:
            sub = await port.submit(req)
        except ProviderError as exc:
            print(f"  ✗ {item.label}: submit failed [{exc.code}]")
            return record(status="failed", error_code=f"{exc.kind}:{exc.code}",
                          error_detail=exc.detail)

        print(f"  → {item.label}: submitted {sub.provider_job_id[:40]}")
        # First poll lands a little before the model is typically ready, then
        # backs off. `time_scale` compresses the schedule in fake mode so the
        # rehearsal is fast and its measured latency stays meaningful; real runs
        # always use 1.0.
        polls = 0
        delay = max(1.0, caps.typical_latency_s * 0.4 / time_scale)
        max_delay = max(2.0, 30.0 / time_scale)
        state = None
        deadline = asyncio.get_event_loop().time() + caps.max_wait_s / time_scale
        while True:
            await asyncio.sleep(delay)
            polls += 1
            try:
                state = await port.poll(sub)
            except ProviderError as exc:
                if not exc.retryable:
                    return record(status="failed", provider_job_id=sub.provider_job_id,
                                  provider_endpoint=sub.endpoint, poll_count=polls,
                                  submitted_at=iso(sub.submitted_at),
                                  error_code=f"{exc.kind}:{exc.code}",
                                  error_detail=exc.detail)
                delay = min(delay * 1.5, max_delay)
                continue
            if state.done:
                break
            if asyncio.get_event_loop().time() > deadline:
                return record(status="failed", provider_job_id=sub.provider_job_id,
                              provider_endpoint=sub.endpoint, poll_count=polls,
                              submitted_at=iso(sub.submitted_at),
                              error_code="transient:timeout",
                              error_detail=f"exceeded max_wait_s={caps.max_wait_s}")
            delay = min(delay * 1.3, max_delay)

        completed = utcnow()
        common = dict(provider_job_id=sub.provider_job_id,
                      provider_endpoint=sub.endpoint, poll_count=polls,
                      submitted_at=iso(sub.submitted_at),
                      expires_at=iso(sub.expires_at),
                      model_version=state.model_version)

        if state.error is not None:
            print(f"  ✗ {item.label}: {state.error.code}")
            return record(status="failed", completed_at=iso(completed), **common,
                          error_code=f"{state.error.kind}:{state.error.code}",
                          error_detail=state.error.detail)

        dest = item.output_path(out)
        try:
            fetched = await port.fetch(state, dest)
        except ProviderError as exc:
            return record(status="expired" if exc.kind is ErrorKind.EXPIRED else "failed",
                          completed_at=iso(completed), **common,
                          error_code=f"{exc.kind}:{exc.code}", error_detail=exc.detail)

        downloaded = utcnow()
        pr = probe_video(dest)
        actual = state.reported_cost_cents
        latency_ms = int((completed - sub.submitted_at).total_seconds() * 1000)
        print(f"  ✓ {item.label}: {latency_ms / 1000:.0f}s, "
              f"{fetched.bytes_written / 1e6:.1f}MB"
              + (f", measured {pr.duration_ms / 1000:.2f}s" if pr.duration_ms else ""))
        return record(
            status="ready", completed_at=iso(completed), downloaded_at=iso(downloaded),
            latency_ms=latency_ms, actual_cost_cents=actual,
            cost_source="provider" if actual is not None else "estimated",
            output_path=str(dest), output_sha256=fetched.sha256,
            output_bytes=fetched.bytes_written, measured_duration_ms=pr.duration_ms,
            measured_fps=pr.fps, measured_width=pr.width, measured_height=pr.height,
            measured_has_audio=pr.has_audio, probe_ok=pr.ok, **common)


# --------------------------------------------------------------------------- #
async def preflight(registry, model_keys: list[str]) -> int:
    print("\nPreflight — credentials and adapter routing\n" + "-" * 62)
    bad = 0
    for key in model_keys:
        caps = get_caps(key)
        try:
            adapters.port_for(registry, key)
            status = "ok"
        except ProviderError as exc:
            status, bad = f"UNAVAILABLE ({exc.code})", bad + 1
        flag = "" if caps.status is ModelStatus.ACTIVE else f"  [{caps.status}]"
        print(f"  {caps.display_name:<24} adapter={caps.adapter:<9} {status}{flag}")
        print(f"    model_id={caps.model_id}")
        print(f"    price={caps.pricing.confidence} src={caps.pricing.source[:52]}")
    print("-" * 62)
    print(f"  ffmpeg: {'yes' if HAVE_FFMPEG else 'NO (fake clips degrade)'}   "
          f"ffprobe: {'yes' if HAVE_FFPROBE else 'NO (no measured durations)'}")
    if bad:
        print(f"\n  {bad} model(s) unavailable — set the missing keys (README.md).")
    return 1 if bad else 0


def print_plan_table(items: list[Item]) -> tuple[int, list[Item]]:
    print("\nPlanned matrix\n" + "=" * 78)
    total, runnable, by_model = 0, [], defaultdict(list)
    for it in items:
        by_model[it.model_key].append(it)
    for model_key, group in by_model.items():
        caps = get_caps(model_key)
        ok = [i for i in group if i.plan.ok]
        cost = sum(i.plan.estimated_cost_cents for i in ok)
        total += cost
        runnable.extend(ok)
        print(f"\n  {caps.display_name}  [{caps.tier}/{caps.provider}]")
        print(f"    {len(ok)}/{len(group)} runnable   est. ${cost / 100:.2f}   "
              f"price={caps.pricing.confidence}")
        p = group[0].plan
        if p.requested_duration_s != p.resolved_duration_s:
            print(f"    duration {p.requested_duration_s:g}s → "
                  f"{p.resolved_duration_s:g}s ({caps.durations.describe()})")
        seen = set()
        for i in group:
            for w in i.plan.warnings:
                if w.code not in seen:
                    seen.add(w.code)
                    print(f"    ! {w.message}")
            for b in i.plan.blocking:
                if b.code not in seen:
                    seen.add(b.code)
                    print(f"    ✗ BLOCKED: {b.message}")
    print("\n" + "=" * 78)
    print(f"  {len(runnable)} generations   estimated total ${total / 100:.2f}")
    return total, runnable


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--models", help="comma-separated model_keys (overrides config)")
    ap.add_argument("--cases", help="comma-separated case ids")
    ap.add_argument("--repeats", type=int, default=None)
    ap.add_argument("--max-cost-per-generation", type=float, default=None,
                    metavar="USD",
                    help="override the per-generation authorization ceiling")
    ap.add_argument("--budget", type=float, default=None, metavar="USD",
                    help="override the whole-run budget ceiling")
    ap.add_argument("--yes", action="store_true",
                    help="actually spend money and run the matrix")
    ap.add_argument("--fake", action="store_true",
                    help="use FakeVideoAdapter: full pipeline, zero spend")
    ap.add_argument("--allow-experimental", action="store_true",
                    help="permit models whose request shape is unverified")
    ap.add_argument("--preflight", action="store_true",
                    help="check credentials and routing, then exit")
    ap.add_argument("--report-only", action="store_true",
                    help="rebuild report/contact sheet from an existing results.jsonl")
    args = ap.parse_args()

    cfg = load_config(args.config)
    out = args.out or (HERE / cfg.get("output_dir", "out"))
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "results.jsonl"

    if args.report_only:
        if not results_path.exists():
            die(f"no results at {results_path}")
        recs = [GenerationRecord(**json.loads(l))
                for l in results_path.read_text().splitlines() if l.strip()]
        summary = reporting.write_summary(recs, out)
        reporting.write_report(recs, summary, out, cfg)
        reporting.write_contact_sheet(recs, out)
        reporting.write_scores_stub(recs, out)
        print(f"rebuilt reports from {len(recs)} records in {out}")
        return 0

    model_keys = ([m.strip() for m in args.models.split(",")] if args.models
                  else list(cfg.get("models", [])))
    if not model_keys:
        die("no models selected (config 'models:' or --models)")
    for k in model_keys:
        try:
            get_caps(k)
        except KeyError as exc:
            die(str(exc).strip('"'))

    registry = adapters.build_registry(
        fake=args.fake,
        fake_speed=float(cfg.get("fake_speed", 20.0)),
        fake_failure_rate=float(cfg.get("fake_failure_rate", 0.0)),
    )
    if args.preflight:
        return await preflight(registry, model_keys)

    case_ids = ([c.strip() for c in args.cases.split(",")] if args.cases
                else cfg.get("cases") or list(CASES_BY_ID))
    cases = [CASES_BY_ID[c] for c in case_ids]
    images = discover_images(HERE / cfg.get("inputs_dir", "inputs"))
    repeats = args.repeats if args.repeats is not None else int(cfg.get("repeats", 1))

    items = build_matrix(cfg, images, model_keys, cases, repeats)
    for it in items:
        it.plan = make_plan(it, cfg, allow_experimental=args.allow_experimental)

    total_cents, runnable = print_plan_table(items)

    budget_usd = args.budget if args.budget is not None else cfg.get("budget_usd", 25)
    per_gen_usd = (args.max_cost_per_generation
                   if args.max_cost_per_generation is not None
                   else cfg.get("max_cost_per_generation_usd", 1.0))
    budget_cents = int(round(float(budget_usd) * 100))
    max_cost_cents = int(round(float(per_gen_usd) * 100))
    print(f"  budget ${budget_cents / 100:.2f}   "
          f"per-generation authorization ${max_cost_cents / 100:.2f}")

    if not runnable:
        die("nothing runnable — every generation is blocked (see above)")

    # Per-generation authorization. The estimate is never the control; this is.
    authorized, rejected = [], []
    for it in runnable:
        try:
            authorize(it.plan, max_cost_cents)
            authorized.append(it)
        except CostAuthorizationError as exc:
            rejected.append((it, exc))
    if rejected:
        print(f"\n  {len(rejected)} generation(s) exceed the authorized maximum:")
        for it, exc in rejected[:5]:
            print(f"    ✗ {it.label}: {exc}")
        print("    Raise max_cost_per_generation_usd only if you mean it.")
    if not authorized:
        die("every generation was refused by cost authorization")

    auth_total = sum(i.plan.estimated_cost_cents for i in authorized)
    if auth_total > budget_cents:
        die(f"authorized matrix costs ${auth_total / 100:.2f}, over the "
            f"${budget_cents / 100:.2f} budget. Lower repeats/durations/models, "
            f"or raise budget_usd deliberately.")

    if args.fake:
        print("\n  FAKE MODE — no provider is called and nothing is charged.")
    if not args.yes:
        print(f"\n  Dry run. {len(authorized)} generations would cost about "
              f"${auth_total / 100:.2f}.\n  Re-run with --yes to execute"
              + (" (or --fake --yes for a free rehearsal)." if not args.fake else "."))
        return 0

    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    print(f"\n  run_id={run_id}   writing {results_path}\n")

    sems = {p: asyncio.Semaphore(int(n)) for p, n in
            (cfg.get("max_concurrent") or {"fal": 4, "veo": 2, "task_api": 2}).items()}
    default_sem = asyncio.Semaphore(2)

    records: list[GenerationRecord] = []
    with results_path.open("a") as fh:          # append: a crash loses nothing
        time_scale = float(cfg.get("fake_speed", 20.0)) if args.fake else 1.0

        async def wrapped(it: Item) -> None:
            sem = sems.get(get_caps(it.model_key).adapter, default_sem)
            rec = await run_one(it, cfg, registry, out, run_id, max_cost_cents,
                                sem, time_scale)
            records.append(rec)
            fh.write(rec.to_json() + "\n")
            fh.flush()

        await asyncio.gather(*(wrapped(it) for it in authorized))

    for port in set(registry.values()):
        await port.aclose()

    summary = reporting.write_summary(records, out)
    reporting.write_report(records, summary, out, cfg)
    reporting.write_contact_sheet(records, out)
    reporting.write_scores_stub(records, out)

    ok = summary["succeeded"]
    print(f"\n{'=' * 78}\n  {ok}/{len(records)} succeeded   "
          f"estimated spend ${summary['total_estimated_cost_cents'] / 100:.2f}")
    print(f"  results     {results_path}")
    print(f"  report      {out / 'report.md'}")
    print(f"  contact     {out / 'contact-sheet.html'}   ← open this")
    print(f"  score me    {out / 'scores.csv'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
