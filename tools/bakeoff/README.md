# M0.5 — Provider bake-off harness

Runs the same standardized image-to-video experiment across several providers,
measures latency and cost, saves every clip with complete reproducibility
metadata, and produces a comparison report plus a contact sheet.

**Why it exists.** Every cost, latency, and capability number in
`docs/ARCHITECTURE.md` is a hypothesis until measured. This harness replaces
guesses with data, and tells you which two or three models are worth
integrating at all.

**It is not a throwaway.** `catalog.py`, `planning.py`, `prompts.py`,
`records.py` and `adapters/` graduate into `apps/api/app/ai/` unchanged at M6.
`results.jsonl` matches the future `motion_generations` table column for column
(see `records.GenerationRecord.to_motion_generations_row`). Standalone means no
database and no application imports — not lower standards.

---

## 1. Install

```bash
cd tools/bakeoff
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

**ffmpeg** is optional but recommended — it is used to measure generated clips
and to synthesize stand-in clips in `--fake` mode.

```bash
brew install ffmpeg                      # preferred: gives ffmpeg AND ffprobe
./.venv/bin/pip install imageio-ffmpeg   # fallback: static ffmpeg, no ffprobe
```

Without either, real generations still run and download correctly; only the
measured duration/fps/resolution columns are left null.

---

## 2. Configure API keys

Keys are read from the environment. **Configure only the providers you intend
to run** — `--preflight` reports anything missing, and a model whose adapter has
no credentials is refused rather than silently skipped.

```bash
export FAL_KEY="5c9728e1-27cd-4408-b90d-6862aac60453:6fe50c1029dc9b9ee13196b30b4b3901"           # fal.ai   → Kling, Hailuo, Wan  (Dashboard → Keys)
export GEMINI_API_KEY="AQ.Ab8RN6KDtqR5ZPTwWWudKqydLqiiXqEc-K3PL_qWwZOA1cOHqQ"    # Google   → Veo   (aistudio.google.com/apikey)

# only if you enable the EXPERIMENTAL task_api models:
export RUNWAY_API_KEY="..."
export LUMA_API_KEY="..."

# optional: Veo person-generation policy (default allow_adult)
export VEO_PERSON_GENERATION="allow_adult"
```

Put them in a shell profile or a local `.env` you never commit. Nothing in this
directory reads a `.env` file automatically, by design — an accidental key in
the repo is worse than typing an export.

Verify:

```bash
./.venv/bin/python run.py --preflight
```

---

## 3. Add the three test stills

Drop three images into `inputs/`. **Generate them with the image provider and
in the exact art style you plan to use.** Testing on stock photos or the
built-in placeholders tells you nothing about how your film will look — the
whole point is to see *your* art move.

| File | What it must contain | What it tests |
|---|---|---|
| `01-character-closeup.png` | one character, face prominent | identity drift — the failure everyone notices |
| `02-two-characters-medium.png` | two characters interacting | multi-subject coherence, hands |
| `03-establishing-wide.png` | landscape or interior, no people | atmosphere, parallax, whether "static" is obeyed |

Names are free-form; the stem becomes the row label in the contact sheet. Any
`.png/.jpg/.jpeg/.webp` in `inputs/` is picked up.

To rehearse the pipeline before you have art:

```bash
./.venv/bin/python make_placeholder_inputs.py
```

---

## 4. Choose the budget

Two independent controls in `bakeoff.yaml`, both enforced before anything is
submitted:

```yaml
budget_usd: 25                    # ceiling for the entire run
max_cost_per_generation_usd: 3.50 # authorization ceiling for ONE generation
```

- **`max_cost_per_generation_usd`** is the authorization gate. Every generation
  is refused unless an explicit cap is supplied and its estimate falls under it.
  The estimate is never the control — catalogue pricing may be `ESTIMATED`,
  stale, or wrong, so the cap is your actual risk decision.
- **`budget_usd`** refuses the whole run if the authorized total exceeds it.

Sizing guidance for the default matrix (3 images × 4 cases × 1 repeat, plus a
2-sample premium cap):

| Setting | Generations | Cost |
|---|---|---|
| `repeats: 1`, `durations: [6]` (default) | 38 | **$22.20** |
| `durations: [5]` (Kling drops 10s → 5s) | 38 | $18.00 |
| Drop `veo-3.1-standard-i2v` | 36 | $17.40 |
| Economy only (Kling + Hailuo Standard) | 24 | $11.64 |
| `repeats: 2` | 76 | $44.40 |

All prices are `VERIFIED` against the providers' own model pages, so these
figures are exact rather than indicative. Note the duration interaction: a
6s target rounds **up** to 10s on Kling ($0.70 vs $0.35), because the resolver
never returns a clip shorter than the shot's screen time.

Always dry-run first — it prices the exact matrix and spends nothing:

```bash
./.venv/bin/python run.py
```

---

## 5. Run it

```bash
# 1. free rehearsal: full pipeline, fake provider, zero spend
./.venv/bin/python run.py --fake --yes

# 2. price the real matrix, spend nothing
./.venv/bin/python run.py

# 3. execute
./.venv/bin/python run.py --yes
```

Useful flags:

| Flag | Effect |
|---|---|
| `--models kling-2.5-turbo-i2v` | override the config's model list |
| `--cases push-in,action` | run a subset of the standardized cases |
| `--repeats 2` | repeat each cell (consistency signal) |
| `--max-cost-per-generation 1.50` | tighten the per-generation authorization ceiling |
| `--budget 10` | tighten the whole-run budget ceiling |
| `--allow-experimental` | permit models whose request shape is unverified |
| `--report-only` | rebuild report and contact sheet from an existing run |
| `--preflight` | credentials, routing and tooling check |

`results.jsonl` is appended and flushed per generation, so a crash or a Ctrl-C
loses at most the in-flight clip. Re-running appends; `--report-only` rebuilds
the reports across everything recorded.

---

## 6. Read the results

Everything lands in `out/`:

| File | What it is |
|---|---|
| **`contact-sheet.html`** | **open this first** — every clip side by side, rows are input × case, columns are models |
| `report.md` | per-model latency, cost, duration drift, failures, catalogue corrections |
| `results.jsonl` | one full record per generation; the `motion_generations` shape |
| `scores.csv` | a stub you fill in by hand |
| `clips/<model>/…mp4` | the generated clips |

**Read them in this order.**

1. **`report.md` → latency p50 / p90.** p90 sets `max_wait_s` and the polling
   backoff in the app. A model whose p90 is triple its p50 will feel unreliable
   however good it looks.
2. **`report.md` → cost per finished second.** The honest comparison. A model
   that only produces 10s clips costs more per shot even at a lower per-second
   rate, because you buy seconds you do not use. Watch for this: a 6s target
   that resolves to 10s is a ~67% overcharge that no pricing page mentions.
3. **`report.md` → duration drift.** Near zero means the timeline can trust the
   request. Large drift means the renderer must always `ffprobe` (it does).
4. **`contact-sheet.html`.** Look for, in order of how much they will hurt:
   identity drift on the close-up; hands and faces on the two-character shot;
   invented motion on `static-subtle`; invented geometry on `pan`.
5. **`scores.csv`.** Fill in identity drift / motion quality / artifacts / style
   adherence, 1–5. Twenty minutes of honest scoring beats any metric you could
   automate in a week. The numbers pick the shortlist; only your eyes pick the
   winner.

### Then close the loop

Write `docs/adr/001-bakeoff-results.md` recording:

- the chosen **economy workhorse** and whether a premium model earns a slot
- measured `typical_latency_s` and `max_wait_s` per model
- corrected pricing, with `confidence` raised to `VERIFIED` and `verified_at`
  set once you have reconciled against a real invoice
- any capability in `catalog.py` that turned out to be wrong

Apply those to `catalog.py` — `report.md` prints the suggested latency values —
then **delete every model you will not use.** Three well-understood models beat
seven guesses.

---

## 7. How it is put together

```
run.py            orchestration: matrix, pricing, authorization, execution, reports
catalog.py        ← capability model + pricing provenance   (graduates to app/ai/)
planning.py       ← intent → capabilities, narration fit, cost authorization
prompts.py        ← prompt composition + the standardized cases
records.py        ← GenerationRecord ↔ motion_generations mapping
probe.py          ffprobe/ffmpeg measurement, with fallbacks
report.py         summary.json, report.md, contact-sheet.html, scores.csv
adapters/
  base.py         VideoPort protocol + error taxonomy
  fal.py          fal queue API — serves every fal-hosted model      [verified]
  veo.py          Gemini predictLongRunning — direct                 [verified]
  task_api.py     Runway/Luma shared task API                    [EXPERIMENTAL]
  fake.py         honours every capability, spends nothing
  __init__.py     registry: routes a catalogue entry to its adapter
```

Two rules hold here exactly as they do in the application:

- **Only `adapters/` may name a provider.** Everything else routes through
  `catalog.py` and `planning.py`. A `if provider == "veo"` anywhere else means
  the abstraction leaked and a capability field is missing.
- **Adapters contain no policy.** Duration snapping, reference truncation,
  pricing, tier rules and authorization all happen in `planning.py` before an
  adapter is called. That is why each adapter is ~80 lines.

### Adding another model

Add an entry to `CATALOG` in `catalog.py` and list its key under `models:` in
`bakeoff.yaml`. If it is served by an existing adapter (`fal` hosts most
things), there is no new code at all.

**Read the provider's OpenAPI schema first** and copy its input fields into
`request_fields`. fal publishes one per endpoint:

```bash
curl -s "https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=<model-id>" \
  | python3 -m json.tool | less
```

A 404 there means the model id is wrong — which is exactly how the original
Kling entry was caught. The adapter filters its payload through
`request_fields`, so a field the endpoint does not define can never be sent;
`extra_params` carries fixed values (e.g. `prompt_optimizer: False` to stop
MiniMax rewriting your composed prompt). Set `resolution_selectable` /
`aspect_selectable` to `False` where the endpoint has no such input — several
do not, and the output then follows the input image.

Until you have read the schema, set `status=ModelStatus.EXPERIMENTAL`; the
harness will refuse the model without `--allow-experimental`.
