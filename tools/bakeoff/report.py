"""Bake-off outputs: summary.json, report.md, contact-sheet.html, scores.csv.

The contact sheet is the actual deliverable. Latency and cost decide the
shortlist; your eyes decide the winner, and they need every clip side by side.
"""
from __future__ import annotations

import html
import json
import os
import statistics
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from catalog import get as get_caps
from records import GenerationRecord


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return s[idx]


def _money(cents: int | None) -> str:
    return "-" if cents is None else f"${cents / 100:.2f}"


def summarize(records: list[GenerationRecord]) -> dict:
    by_model: dict[str, list[GenerationRecord]] = defaultdict(list)
    for r in records:
        by_model[r.model_key].append(r)

    models = {}
    for key, rs in sorted(by_model.items()):
        caps = get_caps(key)
        ok = [r for r in rs if r.status == "ready"]
        lat = [r.latency_ms / 1000 for r in ok if r.latency_ms is not None]
        est = sum(r.estimated_cost_cents for r in ok)
        act = [r.actual_cost_cents for r in ok if r.actual_cost_cents is not None]
        finished_s = sum((r.measured_duration_ms or 0) / 1000 for r in ok)
        adherence = [
            round((r.measured_duration_ms / 1000) - r.resolved_duration_s, 3)
            for r in ok if r.measured_duration_ms
        ]
        errors: dict[str, int] = defaultdict(int)
        for r in rs:
            if r.status != "ready" and r.error_code:
                errors[r.error_code] += 1
        models[key] = {
            "display_name": caps.display_name,
            "provider": caps.provider,
            "adapter": caps.adapter,
            "tier": str(caps.tier),
            "model_id": caps.model_id,
            "attempted": len(rs),
            "succeeded": len(ok),
            "success_rate": round(len(ok) / len(rs), 3) if rs else 0.0,
            "latency_p50_s": round(_pct(lat, 0.50), 1) if lat else None,
            "latency_p90_s": round(_pct(lat, 0.90), 1) if lat else None,
            "latency_min_s": round(min(lat), 1) if lat else None,
            "latency_max_s": round(max(lat), 1) if lat else None,
            "estimated_cost_cents": est,
            "actual_cost_cents": sum(act) if act else None,
            "cost_source": "provider" if act else "estimated",
            "price_confidence": str(caps.pricing.confidence),
            "cost_per_finished_second_cents": (
                round(est / finished_s, 2) if finished_s else None),
            "duration_adherence_s": {
                "median": round(statistics.median(adherence), 3) if adherence else None,
                "worst": max(adherence, key=abs) if adherence else None,
            },
            "audio_present": sorted({bool(r.measured_has_audio) for r in ok
                                     if r.measured_has_audio is not None}),
            "errors": dict(errors),
            "suggested_typical_latency_s": int(round(_pct(lat, 0.50))) if lat else None,
        }

    total_est = sum(r.estimated_cost_cents for r in records if r.status == "ready")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generations": len(records),
        "succeeded": sum(1 for r in records if r.status == "ready"),
        "total_estimated_cost_cents": total_est,
        "models": models,
    }


def write_summary(records: list[GenerationRecord], out: Path) -> dict:
    data = summarize(records)
    (out / "summary.json").write_text(json.dumps(data, indent=2))
    return data


def write_report(records: list[GenerationRecord], summary: dict, out: Path,
                 config: dict) -> None:
    L: list[str] = []
    a = L.append
    a("# Provider bake-off results\n")
    a(f"Generated {summary['generated_at']}  \n")
    a(f"{summary['succeeded']}/{summary['generations']} generations succeeded  \n")
    a(f"Total estimated spend: **{_money(summary['total_estimated_cost_cents'])}**\n")
    a("\n> Estimated costs come from the catalogue and may be ESTIMATED rather than")
    a("> VERIFIED. Reconcile against your provider invoice before trusting them.\n")

    a("\n## Per model\n")
    a("| Model | Tier | OK | Latency p50 / p90 | Est. cost | Cost/finished sec | "
      "Duration drift | Audio | Price confidence |")
    a("|---|---|---|---|---|---|---|---|---|")
    for key, m in summary["models"].items():
        lat = (f"{m['latency_p50_s']}s / {m['latency_p90_s']}s"
               if m["latency_p50_s"] is not None else "-")
        drift = m["duration_adherence_s"]["median"]
        audio = ("yes" if m["audio_present"] == [True]
                 else "no" if m["audio_present"] == [False]
                 else "mixed" if m["audio_present"] else "-")
        cps = m["cost_per_finished_second_cents"]
        a(f"| **{m['display_name']}** | {m['tier']} | "
          f"{m['succeeded']}/{m['attempted']} | {lat} | "
          f"{_money(m['estimated_cost_cents'])} | "
          f"{('%.2f' % cps) + 'c' if cps is not None else '-'} | "
          f"{('%+.2fs' % drift) if drift is not None else '-'} | {audio} | "
          f"{m['price_confidence']} |")

    a("\n## Catalogue corrections to apply\n")
    a("Update `catalog.py` with these measured values, then re-run:\n")
    a("```python")
    for key, m in summary["models"].items():
        if m["suggested_typical_latency_s"]:
            a(f'# {key}: typical_latency_s={m["suggested_typical_latency_s"]}  '
              f'# was {get_caps(key).typical_latency_s}')
    a("```")

    problems = [(k, m) for k, m in summary["models"].items() if m["errors"]]
    if problems:
        a("\n## Failures\n")
        a("| Model | Error | Count | Meaning |")
        a("|---|---|---|---|")
        meaning = {
            "invalid": "catalogue bug -- wrong model_id or unsupported field",
            "auth": "credentials missing or rejected",
            "quota": "provider concurrency/daily cap",
            "refusal": "content policy -- not retryable as-is",
            "transient": "retry with backoff",
            "expired": "media deleted before download",
        }
        for key, m in problems:
            for code, n in sorted(m["errors"].items()):
                kind = code.split(":")[0] if ":" in code else code
                a(f"| {m['display_name']} | `{code}` | {n} | "
                  f"{meaning.get(kind, 'see results.jsonl')} |")

    a("\n## Narration fit observed\n")
    a("Word budgets are evaluated against the **provider-resolved** duration, "
      "so the same narration passes on one model and overflows on another.\n")
    a("| Model | Resolved duration | Word budget | Cases overflowing |")
    a("|---|---|---|---|")
    seen: set[tuple[str, float]] = set()
    for r in records:
        k = (r.model_key, r.resolved_duration_s)
        if k in seen or r.narration_word_budget is None:
            continue
        seen.add(k)
        over = sum(1 for x in records
                   if x.model_key == r.model_key
                   and x.narration_fit_status == "overflow")
        a(f"| {get_caps(r.model_key).display_name} | {r.resolved_duration_s:g}s | "
          f"{r.narration_word_budget} words | {over} |")

    a("\n## How to read this\n")
    a("1. **Latency p90** sets `max_wait_s` and the polling backoff. A model "
      "whose p90 is triple its p50 will feel unreliable however good it looks.\n")
    a("2. **Cost per finished second** is the honest comparison -- a model that "
      "only produces 10s clips costs more per shot even at a lower per-second "
      "rate, because you buy seconds you do not use.\n")
    a("3. **Duration drift** near zero means you can trust the timeline. Large "
      "drift means the renderer must always `ffprobe` (it does anyway).\n")
    a("4. **Open `contact-sheet.html` and score `scores.csv` by hand.** The "
      "numbers pick the shortlist; only your eyes pick the winner.\n")

    (out / "report.md").write_text("\n".join(L) + "\n")


def write_contact_sheet(records: list[GenerationRecord], out: Path) -> None:
    models = sorted({r.model_key for r in records})
    rows: dict[tuple[str, str], dict[str, GenerationRecord]] = defaultdict(dict)
    for r in records:
        rows[(r.image_id, r.case_id)].setdefault(r.model_key, r)

    H: list[str] = []
    a = H.append
    a("<!doctype html><meta charset='utf-8'>")
    a("<title>Bake-off contact sheet</title>")
    a("""<style>
:root{color-scheme:light dark;--bg:#fff;--fg:#111;--mut:#666;--line:#e3e3e3;--card:#fafafa}
@media (prefers-color-scheme:dark){:root{--bg:#131313;--fg:#eee;--mut:#999;--line:#2c2c2c;--card:#1b1b1b}}
body{background:var(--bg);color:var(--fg);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif;margin:24px}
h1{font-size:20px;margin:0 0 4px}p.sub{color:var(--mut);margin:0 0 24px}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid var(--line);padding:8px;vertical-align:top;text-align:left}
th{background:var(--card);position:sticky;top:0;font-size:13px}
td.k{white-space:nowrap;font-weight:600;background:var(--card)}
video{width:280px;max-width:100%;border-radius:6px;display:block;background:#000}
.meta{color:var(--mut);font-size:12px;margin-top:6px}
.bad{color:#c0392b;font-weight:600}
.warn{color:#b7791f}
code{font-size:11px}
.wrap{overflow-x:auto}
</style>""")
    a("<h1>Provider bake-off contact sheet</h1>")
    a(f"<p class='sub'>{len(records)} generations &middot; "
      f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} &middot; "
      "same input image and same prompt across every column</p>")
    a("<div class='wrap'><table><tr><th>Input &times; case</th>")
    for k in models:
        c = get_caps(k)
        a(f"<th>{html.escape(c.display_name)}<div class='meta'>{c.tier} &middot; "
          f"{html.escape(c.provider)}<br>{html.escape(' &middot; '.join(c.capability_chips()))}"
          f"</div></th>")
    a("</tr>")

    for (image_id, case_id), by_model in sorted(rows.items()):
        a(f"<tr><td class='k'>{html.escape(image_id)}<br>"
          f"<span class='meta'>{html.escape(case_id)}</span></td>")
        for k in models:
            r = by_model.get(k)
            if r is None:
                a("<td class='meta'>not run</td>")
                continue
            if r.status != "ready":
                a(f"<td><span class='bad'>{html.escape(r.status)}</span>"
                  f"<div class='meta'><code>{html.escape(r.error_code or '')}</code><br>"
                  f"{html.escape((r.error_detail or '')[:160])}</div></td>")
                continue
            rel = os.path.relpath(r.output_path, out) if r.output_path else ""
            playable = (r.output_path or "").endswith(".mp4")
            cell = (f"<video controls preload='metadata' src='{html.escape(rel)}'></video>"
                    if playable else
                    f"<div class='warn'>no playable file</div>")
            meas = (f"{r.measured_duration_ms / 1000:.2f}s measured"
                    if r.measured_duration_ms else "not measured")
            fit = (f" &middot; fit <b>{r.narration_fit_status}</b>"
                   if r.narration_fit_status else "")
            a(f"<td>{cell}<div class='meta'>"
              f"{r.resolved_duration_s:g}s requested &middot; {meas}<br>"
              f"{_money(r.estimated_cost_cents)} est"
              f"{' &middot; ' + _money(r.actual_cost_cents) + ' actual' if r.actual_cost_cents is not None else ''}"
              f" &middot; {r.latency_ms / 1000:.0f}s{fit}</div></td>")
        a("</tr>")
    a("</table></div>")
    (out / "contact-sheet.html").write_text("\n".join(H))


def write_scores_stub(records: list[GenerationRecord], out: Path) -> None:
    """Twenty minutes of honest hand-scoring beats any metric you could build."""
    path = out / "scores.csv"
    if path.exists():                    # never clobber scoring already done
        return
    lines = ["image_id,case_id,model_key,identity_drift_1_5,motion_quality_1_5,"
             "artifacts_1_5,style_adherence_1_5,notes"]
    for r in sorted(records, key=lambda x: (x.image_id, x.case_id, x.model_key)):
        if r.status == "ready":
            lines.append(f"{r.image_id},{r.case_id},{r.model_key},,,,,")
    path.write_text("\n".join(lines) + "\n")
