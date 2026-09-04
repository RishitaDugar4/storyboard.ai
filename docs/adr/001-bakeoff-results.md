# ADR 001 — Provider bake-off results

**Status:** partial. Enough to correct the catalogue and choose a workhorse;
not enough to choose on quality.
**Date:** 2026-09-04
**Supersedes the estimates in** `tools/bakeoff/catalog.py`

## What was actually measured

Three real generations, not the planned matrix. Everything else in the
catalogue is still a published figure rather than an observation, and this
document says so rather than implying a rigour that was not reached.

| Model | Calls | Latency | Requested → measured | Output | Notes |
|---|---|---|---|---|---|
| `kling-2.5-turbo-i2v` | 2 | **78.6s**, 78.6s | 5s → **5.042s** (+42ms) | **1920×1080** | Consistent to the millisecond across two runs |
| `hailuo-02-standard-i2v` | 1 | **90.7s** | 6s → **5.875s** (−125ms) | **1364×768** | Kept the input's aspect rather than a standard frame |
| `veo-3.1-*` | 0 | — | — | — | **Never called.** Its adapter has never executed. |

Cost per call was not observed: neither provider reports a per-request charge,
so `cost_source` stayed `estimated` on every record — exactly the case the
authorization design was built for.

## Decisions

**1. `kling-2.5-turbo-i2v` is the workhorse.** Faster than Hailuo (78.6s vs
90.7s), delivers full 1920×1080, and hit its requested duration within 42ms
across two runs. Hailuo is cheaper per clip but returned an off-standard
1364×768 and ran 15% slower.

**2. Duration drift is real but small.** Both providers missed their requested
length — one short, one long. Small enough to absorb with the existing
freeze-frame padding, large enough that the renderer must keep measuring with
`ffprobe` instead of trusting the request. It does.

**3. Output geometry cannot be assumed.** Hailuo returned 1364×768 — not a
standard frame, and its width is not divisible by 16. The renderer's
scale-and-pad normalisation handles it; a naïve concat would not have.

**4. Veo stays in the catalogue, unproven.** Its reference-image support is
still the only real answer to character drift, but nothing about its adapter
has been executed. Treat it as unverified code until a first call is made.

## Catalogue corrections applied

```
kling-2.5-turbo-i2v    typical_latency_s  120 → 79
hailuo-02-standard-i2v typical_latency_s  150 → 91
```

Pricing was already corrected from the providers' own pages during the audit
(see `catalog.py`); those figures are `VERIFIED` by source, not by invoice.
Reconcile against a real bill before trusting them.

## Corrections the bake-off forced earlier

These came out of building and running the harness, and each would have been a
failure during the real work:

- **Kling's model id was wrong.** `fal-ai/kling-video/v2.5-turbo/image-to-video`
  returns 404; the endpoint needs `/pro/`. Both successful calls above used the
  corrected id, so the fix is confirmed.
- **Hailuo Pro has no `duration` input.** The catalogue claimed 6s and 10s; the
  endpoint is fixed at 6s. Requesting 10s would have desynced the timeline by
  4s on every affected shot.
- **Neither endpoint accepts `resolution`, `aspect_ratio`, or (for Kling)
  `seed`.** The adapter was sending all three. Provider inputs are now a
  per-model allowlist in the catalogue, so sending an undeclared field is
  impossible by construction.
- **MiniMax rewrites prompts by default** (`prompt_optimizer: true`), which
  would silently replace the composed prompt and break the reproducibility of
  `input_hash`. Now explicitly disabled.
- **An exhausted fal balance returns 403**, which the adapter classified as
  `auth:unauthorized` — sending you to check credentials when the fix is a
  top-up.

## What remains unanswered

The bake-off exists to answer a question it did not get to: **which model looks
best on our art**. That needs real stills in the chosen style, four cases each,
and twenty minutes of honest hand-scoring — the contact sheet, not the latency
table.

Until then the workhorse choice above rests on speed, resolution and duration
fidelity. Those are the cheap questions. The expensive one is still open, and
with motion (M6) unlikely to fit before the deadline, it may stay that way —
which is an acceptable outcome, because the film works without it.

## Re-running

```bash
cd tools/bakeoff
./.venv/bin/python run.py            # price the matrix, spend nothing
./.venv/bin/python run.py --yes      # execute
```

Then update the table above, apply the latency values to `catalog.py`, and
**delete every model you will not use** — three understood models beat seven
guesses.
