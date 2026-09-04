# Story → Video: MVP Architecture

**Status:** design, pre-implementation
**Audience:** solo developer, hard deadline (birthday)
**Version:** 3.0 — provider-agnostic motion, cost-optimised
**Changed from v2.0:** Veo is demoted from foundation to *one entry in a model
catalogue*. Motion is now optional, per-shot, and served by any of several
interchangeable image-to-video providers selected through a capability model.
The Ken Burns renderer is the default preview **and** the default final visual
for shots you choose not to animate. Provider-specific constraints (Veo's
`{4,6,8}` duration grid) are removed from the domain schema.

---

## 0. Decision log (read this first)

| # | Decision | Rationale | What it rules out |
|---|---|---|---|
| **D1** | **Motion is optional and per-shot.** The default visual for a shot is its approved still with a Ken Burns move; generated motion is an opt-in upgrade | Turns video generation from a fixed cost of the product into a discretionary spend per shot. Animating 5 of 14 shots costs a third of animating all of them and often reads better. | "Every shot must be a generated clip" |
| **D2** | **No foundational video provider.** Veo, Kling, Hailuo, Luma, and Runway-compatible models are interchangeable entries in a **model catalogue** behind one `VideoPort` | Prices, quality, and availability in this market move monthly. Betting the architecture on one vendor is the expensive mistake. | Provider-specific code paths outside `ai/adapters/` |
| **D3** | **A capability model, not provider conditionals.** Each catalogue entry declares durations, resolutions, aspect ratios, reference-image limits, audio behaviour, and pricing; the app resolves a shot's *intent* against those capabilities at submit time | Keeps provider facts as data. Adding a model is a catalogue row, not a code change or a migration. | `Literal[4,6,8]` or any vendor constant in the domain schema |
| **D4** | **Image-to-video from an approved still**, always | Preserved from v2.0 and now doubly valuable: a good still is a complete deliverable on its own (Ken Burns), *and* the first frame if you later animate it. Nothing is wasted. | Text-to-video |
| **D5** | **Economy-first.** The default model is the cheapest catalogue entry that meets a shot's needs; premium models require an explicit per-shot choice and a confirmed cost | Optimises the common case for money without removing the ability to spend where it matters. | A single global quality setting |
| **D6** | **The LLM fills structured slots; the application composes prompts** | Two prompts per shot (still + motion) from one canon, rendered per provider's prompt conventions. | "Ask the model for a good prompt" |
| **D7** | **Storyboard JSON is an immutable artifact, then materialised into normalised rows** | Unchanged. | Editing a giant JSONB blob in place |
| **D8** | **Content-hash staleness and idempotency** | Unchanged; the motion hash includes the model key, so switching provider correctly marks a clip stale. | Manual dirty flags |
| **D9** | **Three queues**: `ai`, `motion` (poll-heavy, quota-limited), `render` (CPU, concurrency 1) | Every video provider is an async long-running operation; none may block a worker or compete with FFmpeg. | A single generic worker pool |
| **D10** | **The final render is a hybrid by default** — Ken Burns shots and generated clips concatenated in one timeline | Falls out of the Timeline abstraction for free, and is the mechanism that makes D1 and D5 real. | Two separate "stills film" and "motion film" products |
| **D11** | **Provider bake-off before application development (M0.5)** | Every cost, latency, and quality number in this document is a guess until measured. The harness that measures them is standalone and disposable. | Choosing providers from marketing pages |
| **D12** | **Generated audio from any provider is discarded by default** | Several models (Veo among them) emit audio that cannot be disabled and will invent dialogue colliding with the narrator. Ours is the only soundtrack. | Relying on providers for sound |
| **D13** | **The event bus is Redis pub/sub whenever the queue is `arq`**, in-process only for the inline queue | Publisher and subscriber are *different processes*: handlers run in the worker, SSE subscribers in the API. An in-process bus published into a process nobody was listening to, so live updates never arrived in any real deployment. It went unnoticed because the inline queue used by most tests runs handlers inside the API, where a local bus happens to work. | A single in-process bus |

---

## 1. Product requirements

### 1.1 What it is

A private web app where you paste a written story and walk it through a
supervised pipeline that produces a narrated illustrated video, with generated
motion on the shots where you decide it is worth paying for.

### 1.2 Users

Single tenant, one or two accounts. Auth is a gate, not a product surface.

### 1.3 Functional requirements (MVP)

**FR-1 Story intake.** Paste or type up to ~8,000 words. Versioned.

**FR-2 Story parsing.** Validated `StoryAnalysis` JSON.

**FR-3 Storyboard generation.** Validated `Storyboard` JSON: style bible,
characters, locations, scenes, shots, narration. Shots carry a **target
duration in seconds as a float** — an authorial intent, not a provider grid.

**FR-4 Character management.** Edit and **lock** characters: structured
appearance, canonical prompt fragment, voice, seed, reference portrait. Locked
portraits are passed as reference images to providers that support them.

**FR-5 Scene & shot editing.** Reorder, add, delete, merge, split. Edit action,
camera move, subject motion, target duration.

**FR-6 Still generation.** N candidates per shot (default 2), pick one. **The
approved still is a complete deliverable**: it is what renders under Ken Burns
if the shot is never animated, and the first frame if it is.

**FR-7 Free preview render.** Ken Burns over approved stills with narration and
music. Costs nothing beyond the stills. This is a real, watchable, shareable
video and the default output of the product.

**FR-8 Motion generation (optional, per shot).** Choose a provider/model per
shot from a catalogue, see an estimated cost **before** submitting, generate,
review, re-roll or discard. Motion never happens implicitly.

**FR-9 Provider catalogue & capabilities.** The app exposes available
models with their capabilities, indicative pricing, measured latency, and tier
(economy / standard / premium), and resolves each shot's intent against the
selected model's capabilities at submit time.

**FR-10 Generation records.** Every motion generation persists: provider, model
key, **provider job id**, requested duration and resolution, resolved duration
and resolution, estimated cost, **actual cost when the provider reports it**,
and status through its lifecycle.

**FR-11 Narration generation.** Per-line TTS with per-character voices, exact
durations, regeneration, upload override.

**FR-12 Final render.** One timeline mixing Ken Burns shots and generated clips,
with narration, music bed, optional subtitles, title and end cards.

**FR-13 Progress, cost & budget.** Every long operation is a job with progress,
log, cost, retry. A hard per-project budget blocks enqueue. Premium spend
requires explicit confirmation.

**FR-14 Watch & share.** In-app playback, MP4 download, one unguessable link.

### 1.4 Non-functional requirements

- **Cost:** the product must be *usable and complete* at near-zero motion spend.
  Default budget $60; the free preview path costs only stills (~$2/pass).
- **Latency targets:** parse < 30s; storyboard < 90s; still < 45s; **preview
  render < 90s**; motion clip 30s–5 min (provider-dependent, measured at M0.5);
  final render < 5 min.
- **Provider independence:** adding or removing a model must not require a
  schema migration or a change outside `ai/`.
- **Durability:** several providers expire generated media (Veo: 2 days).
  Download is part of the generation job, never a later step.
- **Data:** private gift; no public listing; deletion cascades to blobs.

### 1.5 Explicit non-goals for MVP

Real-time collaboration, mobile app, multi-tenant billing, lip-sync, LoRA
training, in-browser timeline editing, i18n, generated music, 4K, clip
extension, first/last-frame interpolation, automated provider quality scoring.

### 1.6 Content, likeness, and provenance

Do not feed photographs of a real person to any of these providers; several
enforce person-generation policies and refuse or ban. Design around stylised
characters described in prose with a model-generated reference portrait. Assume
**every** generated frame carries provenance watermarking (Veo uses SynthID;
others vary) and never build a feature that depends on removing it.

---

## 2. User flows

### 2.1 Primary flow

```
1.  Sign in
2.  New Project → title, aspect ratio, art style preset, target length
3.  Paste story → Save
4.  [Analyze]                 ──job──▶ StoryAnalysis
5.  [Generate storyboard]     ──job──▶ Storyboard (validated)
        └─ materialize into scenes/shots/narration rows
6.  CHARACTERS gate — edit, portrait, approve, LOCK
7.  STORYBOARD gate — reorder/edit; set per-shot target duration
8.  [Generate all stills]     ──fan-out──▶ 2 candidates per shot     (~$2)
        └─ APPROVE a still per shot                    ← the checkpoint
9.  [Generate narration]      ──fan-out──▶ audio per line            (cents)
10. [PREVIEW RENDER]          ──job──▶ Ken Burns cut, 720p           (FREE)
        └─ watch the whole film. Iterate here until the story works.
        ─────────────────────────────────────────────────────────────
        ►  AT THIS POINT YOU HAVE A COMPLETE, SENDABLE GIFT.
        ─────────────────────────────────────────────────────────────
11. MOTION (optional, per shot)
        └─ pick the 4–6 shots that would gain most from movement
        └─ per shot: choose model from catalogue → see estimate → confirm
        └─ review clip; keep, re-roll, switch provider, or discard
12. [FINAL RENDER]            ──job──▶ hybrid timeline, 1080p
13. Watch / Download / Share
```

Step 10 is the product's centre of gravity. Everything after it is an upgrade
you buy one shot at a time.

### 2.2 Secondary flows

**Selective animation.** The shot list shows a "motion" column: none (Ken
Burns), generated (with model badge and cost), or manual upload. Sorting by
"impact" — shots with the longest screen time or the most subject motion in
their description — helps you spend where it shows.

**Provider comparison on a single shot.** Generate the same shot on two models,
view them side by side, keep one. The generation records persist both, with
their costs, so the comparison is repeatable and auditable.

**Capability mismatch.** If a shot requests 3 reference images and the selected
model supports 2, the estimate response returns a **warning** (not an error)
naming what will be dropped or adjusted. Blocking only happens when there is no
legal resolution at all.

**Edit propagation.** Editing shot action ⇒ still stale ⇒ clip stale. Editing
motion prompt or switching model ⇒ clip stale only. Editing narration ⇒ audio
stale ⇒ timeline stale. **Clips are never regenerated without an explicit
click**, because each one costs money.

**Narration overflow.** If a line's measured audio exceeds its shot's duration
minus padding, the UI offers: shorten the line (one cheap LLM call), raise the
shot's target duration, or accept a freeze-frame tail. For Ken Burns shots the
duration is free to change; for animated shots, raising it past the model's
resolved duration requires a re-roll, and the UI says so with the price.

**Character re-lock.** Warns with counts of affected stills *and* clips plus the
dollar cost of regenerating the clips.

**Manual override.** Upload a still, an audio file, or a finished clip at any
point. `source='manual'` assets are permanently fresh.

**Panic path.** Export storyboard JSON, hand-edit, re-import; drop files into a
folder keyed by shot id; render from the CLI.

---

## 3. System architecture

### 3.1 Component view

```
┌──────────────────────────────────────────────────────────────────────┐
│  Next.js (App Router, TS)                                            │
│  RSC reads · TanStack Query · EventSource(SSE) · cost gates          │
└───────────────┬──────────────────────────────────────────────────────┘
                │ HTTPS, session cookie
┌───────────────▼──────────────────────────────────────────────────────┐
│  FastAPI (Python 3.12, async)                                        │
│  routers/ · services/ (8 domains) · schemas/ · ai/ · jobs/ · storage/│
│                        ai/catalog.py ← provider capabilities as DATA │
└──────┬─────────────────────────┬──────────────────────┬──────────────┘
       │ SQLAlchemy              │ arq enqueue          │ presigned URLs
┌──────▼──────────┐   ┌──────────▼───────────┐   ┌──────▼──────────────┐
│  PostgreSQL 16  │   │  Redis 7             │   │  Blob storage       │
└─────────────────┘   └──────────┬───────────┘   └─────────────────────┘
                                 │
      ┌──────────────────────────┼──────────────────────────┐
┌─────▼──────────────┐  ┌────────▼───────────────┐  ┌───────▼──────────┐
│ worker: ai         │  │ worker: motion         │  │ worker: render   │
│ concurrency 8      │  │ concurrency 4 (quota)  │  │ concurrency 1    │
│ LLM · image · TTS  │  │ submit/poll/download   │  │ FFmpeg           │
│                    │  │ ANY video provider     │  │                  │
└────────────────────┘  └────────────────────────┘  └──────────────────┘
```

### 3.2 The eight domains

| Domain | Input | Output | Job kinds |
|---|---|---|---|
| **1. Story parsing** | raw text | `StoryAnalysis` | `story.analyze` |
| **2. Storyboard generation** | analysis + settings | `Storyboard` → rows | `storyboard.generate`, `storyboard.regenerate_scene` |
| **3. Character management** | storyboard chars + edits | locked canon + reference portrait | `character.portrait` |
| **4. Scene generation** | scene row + context | shots, narration drafts | `scene.expand` |
| **5. Still generation** | shot + canon + style bible | image asset (deliverable + first frame) | `asset.image` |
| **6. Motion generation** | approved still + motion prompt + model key | video asset | `motion.submit`, `motion.poll`, `motion.download` |
| **7. Narration generation** | line + voice | audio asset + duration | `narration.tts` |
| **8. Video rendering** | Timeline JSON | MP4 + poster + subs | `render.preview`, `render.final` |

Domain 6 is the only one that knows a video provider exists, and even it knows
only `VideoPort` plus a catalogue lookup.

### 3.3 Deployment shape

One 4-vCPU/8GB VPS running compose, R2 for blobs, Caddy in front. Provision
50GB+ disk (clips plus render scratch). Serve media via presigned URLs, never
proxied through the API.

---

## 4. The AI output contract

### 4.1 Principles

1. Every AI call returns a validated Pydantic model.
2. Schemas are versioned; `schema_version` travels with each document.
3. Validation failure → one repair attempt with errors fed back → then fail and
   store the raw response.
4. The model never writes a final prompt string.
5. **The domain schema contains no provider constants.** Shot durations are
   authorial intent in seconds; legality is resolved against a capability model
   at submit time (§9.3).

### 4.2 Schemas (abridged; full definitions in `schemas/ai/`)

```python
# --- Stage 1: story.analyze ------------------------------------------------
class StoryAnalysis(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    title: str
    logline: str = Field(max_length=240)
    tone: list[str] = Field(max_length=5)
    setting_summary: str
    characters: list[DetectedCharacter] = Field(min_length=1, max_length=12)
    locations: list[DetectedLocation] = Field(max_length=12)
    beats: list[Beat] = Field(min_length=3, max_length=40)

# --- Stage 2: storyboard.generate -----------------------------------------
class StyleBible(BaseModel):
    art_style: str
    palette: list[str]
    lighting: str
    camera_language: str
    line_and_texture: str
    motion_language: str          # "gentle, unhurried camera; no whip pans"
    negative: list[str] = []      # used only by backends that accept one

class CharacterCanon(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9-]{2,32}$")
    name: str; role: str
    age_impression: str; build: str; hair: str; eyes: str; skin: str
    distinguishing_features: list[str] = Field(max_length=4)
    default_wardrobe: str
    voice: VoiceProfile

# ---- app-level pacing constants (NOT provider constants) -----------------
WORDS_PER_SECOND = 2.5        # measured narration pace; calibrate at M2
PAD_S            = 0.9        # 0.3 lead-in + 0.6 tail
MIN_SHOT_S, MAX_SHOT_S = 2.5, 12.0

def word_budget(target_duration_s: float) -> int:
    return max(1, int((target_duration_s - PAD_S) * WORDS_PER_SECOND))

class Shot(BaseModel):
    local_index: int
    shot_type: Literal["establishing","wide","medium","close_up","insert","over_shoulder","pov"]
    subject_slugs: list[str] = []          # capped by CAPABILITY, not schema
    action: str                            # visible action, present tense
    composition_note: str = ""             # framing of the still
    camera_move: Literal["static","push_in","pull_out","pan_left","pan_right",
                         "tilt_up","tilt_down","orbit","handheld"] = "push_in"
    subject_motion: str = ""               # what moves; used if animated
    motion_priority: Literal["low","medium","high"] = "low"   # NEW: spend guidance
    target_duration_s: float = Field(ge=MIN_SHOT_S, le=MAX_SHOT_S, default=6.0)
    ambient_sound: str = ""                # steers providers whose audio is forced on

class NarrationLine(BaseModel):
    local_index: int
    shot_local_index: int | None
    speaker: str
    text: str = Field(min_length=1, max_length=400)
    delivery: Literal["neutral","warm","wistful","excited","tense","playful"] = "neutral"

class Scene(BaseModel):
    local_index: int
    title: str; summary: str
    location_slug: str | None
    present_slugs: list[str] = []
    time_of_day: Literal["dawn","day","dusk","night","unspecified"] = "unspecified"
    mood: str
    shots: list[Shot] = Field(min_length=1, max_length=3)
    narration: list[NarrationLine] = Field(min_length=1, max_length=4)

class Storyboard(BaseModel):
    schema_version: Literal["3.0"] = "3.0"
    title: str; logline: str
    style_bible: StyleBible
    characters: list[CharacterCanon] = Field(min_length=1, max_length=10)
    locations: list[LocationCanon] = Field(max_length=12)
    scenes: list[Scene] = Field(min_length=4, max_length=20)

    @model_validator(mode="after")
    def _referential_integrity(self):
        slugs = {c.slug for c in self.characters}
        locs  = {l.slug for l in self.locations}
        for sc in self.scenes:
            if sc.location_slug and sc.location_slug not in locs:
                raise ValueError(f"scene {sc.local_index}: unknown location")
            for sh in sc.shots:
                if bad := set(sh.subject_slugs) - slugs:
                    raise ValueError(f"scene {sc.local_index} shot {sh.local_index}: unknown {bad}")
            for n in sc.narration:
                if n.speaker != "narrator" and n.speaker not in slugs:
                    raise ValueError(f"unknown speaker {n.speaker}")
                if n.shot_local_index is not None and \
                   n.shot_local_index not in {s.local_index for s in sc.shots}:
                    raise ValueError("narration points at a nonexistent shot")
        return self

    @model_validator(mode="after")
    def _narration_fits_the_shot(self):
        """Pacing, expressed against authorial duration — no provider grid."""
        for sc in self.scenes:
            by_shot: dict[int, int] = {}
            for n in sc.narration:
                idx = n.shot_local_index if n.shot_local_index is not None \
                      else sc.shots[0].local_index
                by_shot[idx] = by_shot.get(idx, 0) + len(n.text.split())
            for sh in sc.shots:
                used, budget = by_shot.get(sh.local_index, 0), word_budget(sh.target_duration_s)
                if used > budget:
                    raise ValueError(
                        f"scene {sc.local_index} shot {sh.local_index}: {used} words "
                        f"exceed the {budget}-word budget for a {sh.target_duration_s}s shot. "
                        f"Shorten the narration or raise target_duration_s.")
        return self
```

The difference from v2.0 is small on the page and large in consequence:
`target_duration_s` is a **float in an app-level range**, and the word budget is
computed rather than looked up in a vendor's table. A Ken Burns shot honours it
exactly; an animated shot resolves it to whatever the chosen model can actually
produce (§9.3), and the timeline uses the measured result.

### 4.3 Prompt composition

Two prompts per shot, from one canon. The motion prompt is rendered through a
small per-provider **dialect** so that provider quirks stay in `ai/`, not in the
storyboard:

```python
def compose_image_prompt(shot, scene, project) -> tuple[str, str]:
    sb = project.style_bible
    parts = [
        f"{sb.art_style}.",
        f"{shot.shot_type.replace('_',' ')} shot.",
        shot.action,
        *(c.appearance_prompt for c in shot.locked_subjects),
        scene.location.prompt_fragment if scene.location else "",
        f"{scene.time_of_day} light, {sb.lighting}.",
        f"Palette: {', '.join(sb.palette)}.",
        sb.line_and_texture, shot.composition_note,
    ]
    negative = sb.negative + ["text","watermark","signature","extra limbs","distorted hands"]
    return " ".join(p for p in parts if p), ", ".join(negative)

CAMERA_PHRASE = {
    "static": "The camera is locked off and does not move.",
    "push_in": "The camera pushes in slowly toward the subject.",
    "pull_out": "The camera pulls back slowly, revealing more of the scene.",
    "pan_left": "The camera pans smoothly to the left.",
    "pan_right": "The camera pans smoothly to the right.",
    "tilt_up": "The camera tilts slowly upward.",
    "tilt_down": "The camera tilts slowly downward.",
    "orbit": "The camera orbits slowly around the subject.",
    "handheld": "Subtle handheld movement.",
}

def compose_motion_prompt(shot, scene, project, caps: VideoModelCaps) -> MotionPrompt:
    """Returns positive (+ optional negative, only if the model accepts one)."""
    sb = project.style_bible
    parts = [
        "Animate this image.",
        shot.subject_motion or shot.action,
        CAMERA_PHRASE[shot.camera_move],
        sb.motion_language,
        "The art style, character design and colour palette stay identical to the source image.",
    ]
    if caps.audio is AudioBehavior.ALWAYS_ON:
        # D12: we cannot turn it off, so steer it away from speech.
        parts.append("No spoken dialogue, no voices, no on-screen text or captions.")
        if shot.ambient_sound:
            parts.append(f"Ambient sound: {shot.ambient_sound}.")
    exclusions = ["text", "captions", "watermark", "morphing faces", "extra limbs"]
    if caps.supports_negative_prompt:
        return MotionPrompt(positive=" ".join(parts), negative=", ".join(exclusions))
    # No negative-prompt support → fold exclusions into the positive, positively.
    parts.append("Clean frame with no text or captions; faces and hands stay stable.")
    return MotionPrompt(positive=" ".join(parts), negative=None)

def motion_input_hash(shot, scene, project, plan: GenerationPlan) -> str:
    return sha256_hex(json.dumps({
        "prompt": plan.prompt.positive, "negative": plan.prompt.negative,
        "first_frame": shot.selected_image_checksum,   # still change ⇒ clip stale
        "refs": plan.reference_checksums,
        "model_key": plan.model_key,                   # model change ⇒ clip stale
        "duration": plan.resolved_duration_s,
        "resolution": plan.resolved_resolution,
        "seed": plan.seed, "v": COMPOSER_VERSION,
    }, sort_keys=True))
```

### 4.4 Duration reconciliation

Three quantities must agree: authorial intent, what the model can produce, and
measured narration.

```
Ken Burns shot (no motion):
    duration_ms = max(target_duration_s*1000, narration_ms + PAD)
    → always satisfiable; Ken Burns stretches for free.

Animated shot:
    resolved_duration_s = caps.resolve_duration(target_duration_s)   # §9.3
    clip_ms   = ffprobe(actual clip)          ← measured, never assumed
    required  = narration_ms + PAD
    duration  = max(clip_ms, required)
    tail_freeze_ms = max(0, required - clip_ms)     ← hold last frame
```

Never retime a generated clip to fit audio; padding with a held frame is far
less visible than wrong-speed motion. Preflight blocks `tail_freeze_ms > 1500`
and offers: shorten the line, raise the target duration and re-roll, or drop the
shot back to Ken Burns (free, always available — a useful escape).

### 4.5 Character consistency

1. **Frozen textual canon** embedded byte-for-byte in every still prompt.
2. **The approved still is the first frame** — the strongest lever, and it
   applies identically across every provider.
3. **Reference images** where supported, capped by
   `caps.max_reference_images` at plan time rather than by the schema.
4. **Explicit style-hold phrasing** in every motion prompt.
5. **Short clips** and **per-shot re-roll**.
6. **Stylised art direction** — still a legitimate engineering mitigation, and
   more important now that quality varies by provider.

---

## 5. Database schema

PostgreSQL 16, UUIDv7 ids, `timestamptz`, native enums, JSONB for sub-documents
never queried by field.

**The model catalogue lives in code, not in a table** (§9.2). Generations store
a `model_key` text column validated against the catalogue at write time. This is
deliberate: adding a provider must not require a migration, and a catalogue row
is a typed, version-controlled, reviewable object — not a row someone edited in
psql at 2am.

```sql
-- ─── identity & project ──────────────────────────────────────────────────
CREATE TABLE users (
  id uuid PRIMARY KEY, email citext UNIQUE NOT NULL,
  display_name text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TYPE project_stage AS ENUM
  ('draft','analyzed','storyboarded','characters_locked','stills','narration',
   'previewed','motion','rendered');

CREATE TABLE projects (
  id uuid PRIMARY KEY,
  owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title text NOT NULL,
  stage project_stage NOT NULL DEFAULT 'draft',
  aspect_ratio text NOT NULL DEFAULT '16:9',
  image_size text NOT NULL DEFAULT '1920x1080',
  style_preset text NOT NULL DEFAULT 'storybook_gouache',
  style_bible jsonb,
  narrator_voice_id text,
  music_track_key text,
  default_model_key text,             -- catalogue key; NULL ⇒ cheapest capable
  allow_premium boolean NOT NULL DEFAULT false,   -- premium needs opt-in (D5)
  budget_cents integer NOT NULL DEFAULT 6000,     -- $60; preview path is ~free
  spent_cents integer NOT NULL DEFAULT 0,
  composer_version integer NOT NULL DEFAULT 1,
  share_token text UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON projects (owner_id, updated_at DESC);

-- ─── story / storyboard (unchanged from v2) ──────────────────────────────
CREATE TABLE story_inputs (
  id uuid PRIMARY KEY,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  version integer NOT NULL, raw_text text NOT NULL,
  text_hash text NOT NULL, word_count integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, version)
);

CREATE TABLE story_analyses (
  id uuid PRIMARY KEY,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  story_input_id uuid NOT NULL REFERENCES story_inputs(id) ON DELETE CASCADE,
  schema_version text NOT NULL, document jsonb NOT NULL,
  model text NOT NULL, input_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE storyboards (
  id uuid PRIMARY KEY,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  story_analysis_id uuid REFERENCES story_analyses(id) ON DELETE SET NULL,
  version integer NOT NULL, schema_version text NOT NULL,
  document jsonb NOT NULL, model text NOT NULL, input_hash text NOT NULL,
  applied_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, version)
);

-- ─── characters & locations ──────────────────────────────────────────────
CREATE TABLE characters (
  id uuid PRIMARY KEY,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  slug text NOT NULL, name text NOT NULL, role text NOT NULL,
  appearance jsonb NOT NULL,
  appearance_prompt text NOT NULL,       -- FROZEN while locked
  voice jsonb NOT NULL, seed bigint NOT NULL,
  reference_asset_id uuid,
  locked_at timestamptz, sort_order integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, slug)
);

CREATE TABLE locations (
  id uuid PRIMARY KEY,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  slug text NOT NULL, name text NOT NULL,
  description text NOT NULL, prompt_fragment text NOT NULL,
  UNIQUE (project_id, slug)
);

-- ─── scenes & shots ──────────────────────────────────────────────────────
CREATE TABLE scenes (
  id uuid PRIMARY KEY,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  location_id uuid REFERENCES locations(id) ON DELETE SET NULL,
  sort_order integer NOT NULL,
  title text NOT NULL, summary text NOT NULL,
  time_of_day text NOT NULL DEFAULT 'unspecified',
  mood text NOT NULL DEFAULT '',
  present_slugs jsonb NOT NULL DEFAULT '[]',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON scenes (project_id, sort_order);

CREATE TYPE shot_type AS ENUM
  ('establishing','wide','medium','close_up','insert','over_shoulder','pov');
CREATE TYPE camera_move AS ENUM
  ('static','push_in','pull_out','pan_left','pan_right','tilt_up','tilt_down','orbit','handheld');
CREATE TYPE motion_mode AS ENUM ('kenburns','generated','manual');

CREATE TABLE shots (
  id                uuid PRIMARY KEY,
  scene_id          uuid NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
  project_id        uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  sort_order        integer NOT NULL,
  shot_type         shot_type NOT NULL DEFAULT 'medium',
  action            text NOT NULL,
  composition_note  text NOT NULL DEFAULT '',
  camera_move       camera_move NOT NULL DEFAULT 'push_in',
  subject_motion    text NOT NULL DEFAULT '',
  ambient_sound     text NOT NULL DEFAULT '',
  motion_priority   text NOT NULL DEFAULT 'low',
  -- authorial intent, NOT a provider grid:
  target_duration_s numeric(4,1) NOT NULL DEFAULT 6.0
                      CHECK (target_duration_s BETWEEN 2.5 AND 12.0),
  -- how this shot renders in the FINAL cut:
  motion_mode       motion_mode NOT NULL DEFAULT 'kenburns',
  preferred_model_key text,              -- per-shot override; NULL ⇒ project default
  subject_slugs     jsonb NOT NULL DEFAULT '[]',
  prompt_override   text,
  motion_override   text,
  seed              bigint NOT NULL,
  selected_image_id uuid,                -- the approved still (deliverable + first frame)
  selected_clip_id  uuid,
  image_input_hash  text,
  motion_input_hash text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON shots (scene_id, sort_order);
CREATE INDEX ON shots (project_id);
CREATE INDEX ON shots (project_id, motion_mode);

-- ─── narration ───────────────────────────────────────────────────────────
CREATE TABLE narration_lines (
  id uuid PRIMARY KEY,
  scene_id uuid NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  shot_id uuid REFERENCES shots(id) ON DELETE SET NULL,
  sort_order integer NOT NULL,
  speaker_slug text NOT NULL DEFAULT 'narrator',
  character_id uuid REFERENCES characters(id) ON DELETE SET NULL,
  text text NOT NULL, delivery text NOT NULL DEFAULT 'neutral',
  audio_asset_id uuid, duration_ms integer, input_hash text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON narration_lines (scene_id, sort_order);

-- ─── assets ──────────────────────────────────────────────────────────────
CREATE TYPE asset_kind AS ENUM ('image','clip','audio','video','subtitle','poster');
CREATE TYPE asset_source AS ENUM ('generated','manual','derived');

CREATE TABLE assets (
  id uuid PRIMARY KEY,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind asset_kind NOT NULL,
  source asset_source NOT NULL DEFAULT 'generated',
  storage_key text NOT NULL, mime text NOT NULL,
  bytes bigint NOT NULL, checksum text NOT NULL,
  width integer, height integer, duration_ms integer, fps numeric(5,2),
  has_audio boolean,
  provider text, model text,
  input_hash text, params jsonb NOT NULL DEFAULT '{}',
  cost_cents integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON assets (project_id, kind, created_at DESC);
CREATE INDEX ON assets (input_hash) WHERE input_hash IS NOT NULL;

ALTER TABLE shots           ADD FOREIGN KEY (selected_image_id) REFERENCES assets(id) ON DELETE SET NULL;
ALTER TABLE shots           ADD FOREIGN KEY (selected_clip_id)  REFERENCES assets(id) ON DELETE SET NULL;
ALTER TABLE narration_lines ADD FOREIGN KEY (audio_asset_id)    REFERENCES assets(id) ON DELETE SET NULL;
ALTER TABLE characters      ADD FOREIGN KEY (reference_asset_id) REFERENCES assets(id) ON DELETE SET NULL;

-- ─── motion generations (FR-10) ──────────────────────────────────────────
CREATE TYPE motion_status AS ENUM
  ('planned','queued','submitted','running','downloading','ready',
   'failed','expired','cancelled');

CREATE TABLE motion_generations (
  id                   uuid PRIMARY KEY,
  project_id           uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  shot_id              uuid NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
  status               motion_status NOT NULL DEFAULT 'planned',

  -- WHO ran it -----------------------------------------------------------
  provider             text NOT NULL,        -- 'veo' | 'kling' | 'hailuo' | 'luma' | 'runway' | 'fal'
  model_key            text NOT NULL,        -- catalogue key, e.g. 'kling-2.5-turbo-i2v'
  model_id             text NOT NULL,        -- provider-native id actually sent
  provider_job_id      text,                 -- LRO name / request id / task id
  provider_endpoint    text,                 -- direct vs aggregator, for debugging

  -- WHAT was asked for ---------------------------------------------------
  requested_duration_s numeric(4,1) NOT NULL,   -- the shot's authorial intent
  resolved_duration_s  numeric(4,1) NOT NULL,   -- what capabilities allowed
  requested_resolution text NOT NULL,
  resolved_resolution  text NOT NULL,
  aspect_ratio         text NOT NULL,
  first_frame_asset_id uuid REFERENCES assets(id) ON DELETE SET NULL,
  reference_asset_ids  jsonb NOT NULL DEFAULT '[]',
  motion_prompt        text NOT NULL,           -- exact positive sent
  negative_prompt      text,                    -- NULL when unsupported
  seed                 bigint,
  capability_warnings  jsonb NOT NULL DEFAULT '[]',  -- what got clamped, and why

  -- WHAT it cost ---------------------------------------------------------
  estimated_cost_cents integer NOT NULL,
  actual_cost_cents    integer,               -- NULL when the provider is silent
  cost_source          text,                  -- 'provider' | 'estimated' | 'metered'

  -- RESULT ---------------------------------------------------------------
  asset_id             uuid REFERENCES assets(id) ON DELETE SET NULL,
  measured_duration_ms integer,               -- ffprobe truth
  latency_ms           integer,               -- submit → ready
  poll_count           integer NOT NULL DEFAULT 0,
  error_code text, error_detail text,
  submitted_at timestamptz, expires_at timestamptz, downloaded_at timestamptz,
  input_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON motion_generations (shot_id, created_at DESC);
CREATE INDEX ON motion_generations (project_id, created_at DESC);
CREATE INDEX ON motion_generations (status)
  WHERE status IN ('submitted','running','downloading');
CREATE INDEX ON motion_generations (expires_at) WHERE downloaded_at IS NULL;
CREATE INDEX ON motion_generations (provider, model_key);   -- cost/quality reporting

-- ─── jobs, renders, audit (as v2) ────────────────────────────────────────
CREATE TYPE job_status AS ENUM
  ('queued','running','awaiting_provider','succeeded','failed','cancelled','skipped');

CREATE TABLE jobs (
  id uuid PRIMARY KEY,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind text NOT NULL, target_type text, target_id uuid,
  parent_job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
  status job_status NOT NULL DEFAULT 'queued',
  attempt integer NOT NULL DEFAULT 0, max_attempts integer NOT NULL DEFAULT 3,
  idempotency_key text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}', result jsonb,
  error_code text, error_detail text,
  progress smallint NOT NULL DEFAULT 0,
  next_poll_at timestamptz,
  queued_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz, finished_at timestamptz,
  UNIQUE (idempotency_key)
);
CREATE INDEX ON jobs (project_id, queued_at DESC);
CREATE INDEX ON jobs (status) WHERE status IN ('queued','running','awaiting_provider');

CREATE TABLE job_events (
  id bigserial PRIMARY KEY,
  job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  at timestamptz NOT NULL DEFAULT now(),
  level text NOT NULL DEFAULT 'info', message text NOT NULL, data jsonb
);
CREATE INDEX ON job_events (job_id, at);

CREATE TYPE render_profile AS ENUM ('preview','final');

CREATE TABLE renders (
  id uuid PRIMARY KEY,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  profile render_profile NOT NULL,
  timeline jsonb NOT NULL, timeline_hash text NOT NULL,
  status job_status NOT NULL DEFAULT 'queued',
  video_asset_id uuid REFERENCES assets(id) ON DELETE SET NULL,
  poster_asset_id uuid REFERENCES assets(id) ON DELETE SET NULL,
  subtitle_asset_id uuid REFERENCES assets(id) ON DELETE SET NULL,
  duration_ms integer, ffmpeg_log_key text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON renders (project_id, created_at DESC);

CREATE TABLE ai_calls (
  id uuid PRIMARY KEY,
  project_id uuid REFERENCES projects(id) ON DELETE CASCADE,
  job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
  capability text NOT NULL,             -- 'text'|'image'|'video'|'speech'
  provider text NOT NULL, model text NOT NULL,
  input_tokens integer, output_tokens integer, units numeric(10,3),
  cost_cents integer NOT NULL DEFAULT 0, latency_ms integer NOT NULL,
  ok boolean NOT NULL, error_code text, raw_response_key text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON ai_calls (project_id, created_at DESC);
```

### 5.1 Staleness, derived not stored

```sql
SELECT s.id, s.motion_mode,
       (img.id IS NOT NULL AND (img.source='manual' OR img.input_hash = s.image_input_hash))
         AS still_fresh,
       (s.motion_mode <> 'generated'
         OR (clip.id IS NOT NULL
             AND (clip.source='manual' OR clip.input_hash = s.motion_input_hash)))
         AS motion_fresh
FROM shots s
LEFT JOIN assets img  ON img.id  = s.selected_image_id
LEFT JOIN assets clip ON clip.id = s.selected_clip_id
WHERE s.project_id = $1;
```

`motion_input_hash` includes the first frame checksum **and the model key**, so
approving a different still or switching provider both mark the clip stale, with
no extra bookkeeping. A `kenburns` shot is never motion-stale — a useful
property, since it means the free path can never be "out of date".

---

## 6. API specification

FastAPI, JSON, cookie session, 202 + job for long operations, `:action` suffix
for non-CRUD verbs, RFC 9457 errors. Domain codes: `validation_failed`,
`ai_schema_invalid`, `provider_refused`, `budget_exceeded`,
`stage_precondition_failed`, `character_locked`, `narration_overflow`,
`capability_unsatisfiable`, `premium_not_enabled`, `clip_expired`,
`quota_exhausted`.

```
AUTH / PROJECTS
  POST   /api/v1/auth/session · DELETE /api/v1/auth/session · GET /api/v1/me
  GET    /api/v1/projects
  POST   /api/v1/projects
  GET    /api/v1/projects/{pid}
  PATCH  /api/v1/projects/{pid}   {title?, style_bible?, default_model_key?,
                                   allow_premium?, budget_cents?}
  DELETE /api/v1/projects/{pid}
  GET    /api/v1/projects/{pid}/cost   → {spent_cents, budget_cents,
                                          by_capability[], by_provider[]}

1 · STORY        (unchanged)
  PUT/GET /api/v1/projects/{pid}/story · POST …/story:analyze · GET …/analysis
2 · STORYBOARD   (unchanged)
  POST …/storyboard:generate · GET …/storyboards[/{sbid}] · POST …:apply
  GET  …/storyboard · POST …/storyboard:export · POST …/storyboard:import
3 · CHARACTERS   (unchanged)
  GET/POST …/characters · PATCH/DELETE /characters/{cid}
  POST /characters/{cid}/portrait:generate · :select · :lock · :unlock
  GET  /api/v1/voices

4 · SCENES & SHOTS
  GET/POST  …/scenes · PATCH/DELETE /scenes/{scid} · POST …/scenes:reorder
  POST      /scenes/{scid}:regenerate
  POST      /scenes/{scid}/shots · PATCH/DELETE /shots/{shid}
  PATCH     /shots/{shid}   {action?, camera_move?, subject_motion?,
                             target_duration_s?, motion_mode?,
                             preferred_model_key?, motion_priority?}
  POST      /scenes/{scid}/shots:reorder
  GET       /shots/{shid}/prompt      → both composed prompts (debug gold)
  GET       /shots/{shid}/fit         → narration fit against target duration

5 · STILLS
  POST /shots/{shid}/image:generate {n?=2, prompt_override?}   → 202
  GET  /shots/{shid}/images
  POST /shots/{shid}/image:select {asset_id}      ← THE APPROVAL CHECKPOINT
  POST /shots/{shid}/image:upload (multipart)
  POST …/images:generate_all {only_stale?=true}   → 202 parent

6 · MODEL CATALOGUE  (NEW)
  GET  /api/v1/video-models
        → [{model_key, provider, display_name, tier, capabilities{…},
            pricing{…}, measured{p50_latency_s, samples}, available, notes}]
  GET  /api/v1/video-models/{model_key}
  POST /api/v1/shots/{shid}/motion:plan   {model_key?, target_duration_s?}
        → {model_key, resolved_duration_s, resolved_resolution,
           reference_images_used, estimated_cost_cents,
           warnings[{code, message}], blocking[{code, message}]}
        # PURE + FREE. This is what the UI calls to show a price before submit.

7 · MOTION  (the paid stage; nothing here happens implicitly)
  POST /api/v1/shots/{shid}/motion:generate
        {model_key?, motion_override?, confirm_cost_cents}      → 202
        # 409 no approved still · 402 over budget
        # 403 premium_not_enabled · 409 confirm_cost_cents mismatch
  GET  /api/v1/shots/{shid}/motion            → all generations for the shot,
                                                 with provider/model/cost/status
  POST /api/v1/shots/{shid}/motion:select     {asset_id}
  POST /api/v1/shots/{shid}/motion:upload     (multipart) → motion_mode='manual'
  POST /api/v1/shots/{shid}/motion:clear      → back to motion_mode='kenburns'
  POST /api/v1/motion-generations/{mgid}:cancel
  POST /api/v1/projects/{pid}/motion:plan_all {model_key?, only:[shot_ids]?}
        → {items[], total_estimated_cost_cents, est_wall_clock_s}
  POST /api/v1/projects/{pid}/motion:generate_all
        {model_key?, only:[shot_ids], confirm_cost_cents}       → 202 parent
        # `only` is REQUIRED and explicit — there is no "animate everything" verb

8 · NARRATION
  POST /narration-lines/{nid}/audio:generate · :upload
  PATCH /narration-lines/{nid}
  POST /narration-lines/{nid}:shorten {target_words}   → 202 (cheap LLM fix)
  POST …/narration:generate_all {only_stale?=true}     → 202 parent

9 · RENDER
  POST /api/v1/projects/{pid}/preflight {profile}
  POST /api/v1/projects/{pid}/renders  {profile, subtitles?, music?}  → 202
  GET  /api/v1/projects/{pid}/renders · GET /renders/{rid} · /renders/{rid}/timeline

JOBS / MEDIA / OPS
  GET  /jobs/{jid} · GET …/jobs · POST /jobs/{jid}:retry · :cancel
  GET  /api/v1/projects/{pid}/events        → text/event-stream
  GET  /api/v1/assets/{aid}/content         → 302 presigned
  GET  /s/{share_token} · GET /healthz · GET /readyz
```

Two API details doing real work:

- **`motion:plan` is a free, pure, side-effect-free endpoint** that returns the
  resolved parameters, the warnings, and the price. The UI calls it on every
  model-picker change. Planning and executing are separate verbs, which is what
  makes "show an estimate before submission" trivial instead of a special case.
- **`confirm_cost_cents` must echo the server's own estimate**; a mismatch is a
  409. A stale tab or a double-click cannot spend money. And
  `motion:generate_all` requires an explicit `only: [shot_ids]` list — there is
  deliberately no verb that animates everything.

### 6.1 SSE event shape

```jsonc
event: job
data: {"job_id":"…","kind":"motion.poll","target_id":"…","status":"awaiting_provider",
       "progress":50,"message":"kling-2.5-turbo: generating (1m10s elapsed)"}
event: entity
data: {"type":"shot","id":"…","reason":"motion_ready"}
event: cost
data: {"spent_cents":840,"budget_cents":6000}
```

Events carry invalidations, never entity bodies.

---

## 7. Frontend architecture

Next.js 15 App Router, TypeScript strict, Tailwind + shadcn/ui, TanStack Query,
`zustand` for ephemeral UI only.

### 7.1 Routes

```
app/
  (auth)/login/page.tsx
  (app)/layout.tsx
  (app)/projects/page.tsx
  (app)/projects/[pid]/layout.tsx        ← StageStepper · EventsProvider · CostMeter
  (app)/projects/[pid]/page.tsx          ← readiness dashboard + next action
  (app)/projects/[pid]/story/page.tsx
  (app)/projects/[pid]/storyboard/page.tsx
  (app)/projects/[pid]/characters/page.tsx
  (app)/projects/[pid]/scenes/[scid]/page.tsx
  (app)/projects/[pid]/stills/page.tsx   ← generate + APPROVE stills
  (app)/projects/[pid]/narration/page.tsx
  (app)/projects/[pid]/preview/page.tsx  ← the free Ken Burns cut (default output)
  (app)/projects/[pid]/motion/page.tsx   ← per-shot upgrade table + provider picker
  (app)/projects/[pid]/render/page.tsx
  (app)/projects/[pid]/watch/page.tsx
  (public)/s/[token]/page.tsx
```

### 7.2 Component inventory

```
layout/      AppShell · ProjectSidebar · StageStepper · JobDrawer · CostMeter
story/       StoryEditor · WordCountBadge · AnalysisSummary · BeatList
storyboard/  StoryboardTree · SceneCard · ShotStrip · ShotCard · ShotEditorSheet
             TargetDurationSlider     ← continuous seconds, no provider grid
             NarrationFitMeter · RegenerateSceneDialog · StoryboardJsonDrawer
characters/  CharacterGrid · CharacterCard · CharacterEditorSheet
             AppearanceFieldset · VoicePicker · PortraitCandidates · LockBanner
stills/      ShotImageGrid · ImageCandidatePicker · ApproveStillButton
             PromptInspector · UploadDropzone · StaleBadge · BulkGenerateBar
motion/      MotionTable            ← one row per shot: mode, model, cost, status
             MotionModeToggle       ← Ken Burns | Generated | Uploaded
             ModelPicker            ← catalogue, grouped by tier, with $/clip
             ModelCapabilityChips   ← "≤3 refs · 5s/10s · no negative prompt"
             CostEstimateInline     ← live from motion:plan on every change
             CapabilityWarningList  ← what will be clamped, plainly worded
             MotionCard             ← clip player, provider badge, cost, re-roll
             ProviderCompareDrawer  ← same shot, two models, side by side
             MotionQueuePanel · PremiumUnlockDialog
narration/   NarrationLineRow · AudioPlayer · WaveformBar · VoiceAssignMenu
             OverflowFixMenu
render/      PreflightPanel · RenderProfileForm · TimelinePreview
             HybridTimelineLegend   ← shows which shots are clips vs Ken Burns
             RenderHistoryTable · VideoPlayer · DownloadButton
jobs/        JobBadge · JobProgressBar · JobLogSheet · RetryButton
common/      AsyncButton · ConfirmDialog · EmptyState · ErrorBoundary
             FreshnessLegend · SpendConfirmDialog
```

### 7.3 Key patterns

**`MotionTable` is the cost-control surface.** One row per shot: thumbnail,
screen time, motion priority from the storyboard, current mode, chosen model,
estimated or actual cost, status. A footer totals estimated spend for everything
currently selected. You decide what to animate by *looking at a table with
prices in it*, which is a far better instrument than a button on each card.

**`ModelPicker` + `CostEstimateInline`** call `motion:plan` (free) on every
change and render `resolved_duration_s`, capability chips, warnings, and price.
Premium-tier entries are visibly separated and disabled until `allow_premium`,
which is a project setting behind `PremiumUnlockDialog`.

**`SpendConfirmDialog`** wraps any action over a threshold (default $2), shows
itemised cost and estimated wall-clock, and posts `confirm_cost_cents`. Money
lives in one component, not sprinkled through a dozen handlers.

**`useProjectEvents(pid)`** — one `EventSource`; fans events to
`invalidateQueries`; refetches once on reconnect.

**`PromptInspector`** — both composed prompts, fragments colour-coded by origin
(style bible / character canon / shot fields), plus the provider dialect applied.
Build it in week 2.

### 7.4 Type sharing

`packages/schemas`: Pydantic → JSON Schema (`python -m app.schemas.export`) →
`json-schema-to-typescript`. The catalogue's capability types are exported the
same way, so `ModelCapabilityChips` renders from generated types rather than
hand-written unions. `make types`; CI fails on drift.

---

## 8. Background job architecture

### 8.1 Runtime

**arq**, three workers from one image:

| Worker | Queue | Concurrency | Timeout | Notes |
|---|---|---|---|---|
| `worker-ai` | `ai` | 8 | 5 min | LLM, stills, TTS |
| `worker-motion` | `motion` | 4 | 60s/tick | submit/poll/download, any provider; per-provider token buckets |
| `worker-render` | `render` | **1** | 30 min | FFmpeg + scratch disk |

### 8.2 Job contract

```python
async def handle(ctx, job_id: UUID) -> None:
    async with claim(job_id) as job:      # atomic 'queued'→'running'
        await progress(job, 0, "starting")
        job.result = await do_work(job)
        await progress(job, 100, "done")
```

### 8.3 Idempotency & caching

`idempotency_key = sha256(f"{kind}|{target_id}|{input_hash}")`, UNIQUE, with
`INSERT … ON CONFLICT DO NOTHING RETURNING`. Identical work is never paid twice.

### 8.4 The motion polling pattern

Every provider in the catalogue is asynchronous; the adapter normalises them to
one shape, so **one set of handlers serves all of them**:

```
motion.submit                    (motion queue, ~2s)
  ├─ re-plan from capabilities (never trust the client's plan)
  ├─ verify: still approved · budget · premium allowed · quota token
  ├─ insert motion_generations row: provider, model_key, model_id,
  │    requested_* , resolved_*, estimated_cost_cents, status='queued'
  ├─ port.submit(...) → provider_job_id, expires_at (if the provider expires)
  ├─ status='submitted'; job.status='awaiting_provider'
  └─ enqueue motion.poll with _defer_by = caps.typical_latency_s * 0.6

motion.poll                      (motion queue, ~1s per tick)
  ├─ port.poll(provider_job_id)
  ├─ pending → backoff 10s→20s→30s, publish elapsed-time progress,
  │            re-enqueue; HARD CAP by caps.max_wait_s → 'failed'
  ├─ error   → classify (refusal / quota / transient) → §8.5
  └─ done    → enqueue motion.download immediately

motion.download                  (motion queue)
  ├─ stream to blob storage      ← before any provider expiry window
  ├─ ffprobe: measured_duration_ms, fps, dimensions, has_audio
  ├─ insert asset(kind='clip'); link motion_generations.asset_id
  ├─ record actual_cost_cents if the provider reports it, else copy the
  │    estimate and set cost_source='estimated'
  ├─ record latency_ms = ready − submitted     ← feeds the catalogue's measured p50
  ├─ auto-select only if the shot has no clip yet; set motion_mode='generated'
  └─ status='ready'; publish invalidation
```

Splitting submit/poll/download means a crashed worker loses at most one tick and
never a paid generation: the provider job id is durable in Postgres before any
polling starts. **This is the single most important reliability property of the
motion integration.**

`reap_expiring_clips` (every 5 min) re-enqueues `motion.download` for rows with
`downloaded_at IS NULL` and `expires_at < now() + 6h`, and marks true expiries.

### 8.5 Error classification

```
transient  → 429/5xx/timeout/connection  → retry, expo backoff + jitter
quota      → provider concurrency/daily  → LONG defer, does NOT consume an attempt
refusal    → policy / person-generation  → NO retry, surface prompt editor
invalid    → schema validation failed 2× → NO retry, store raw response
budget     → project cap                 → NO retry, block enqueue
expired    → provider retention elapsed  → NO retry, mark and require re-run
```

Quota needs its own class: it is transient on a timescale of minutes-to-hours,
and burning the attempt budget on it turns a queue delay into a hard failure.

### 8.6 Fan-out

`motion:generate_all` creates a parent job over an explicit shot list plus N
children; a child's terminal transition calls `maybe_finish_parent()` (one
`count(*) FILTER (…)` over siblings). The parent carries
`payload.total_estimated_cost_cents` so the UI can show "$3.10 of $4.80 spent"
as it runs.

### 8.7 Progress transport

Redis pub/sub → SSE → EventSource. Motion progress is elapsed time against the
catalogue's measured p50 latency, not a fake percentage — most providers report
no partial progress. Say "~90s typical for this model" rather than stalling a
bar at 90%.

### 8.8 Scheduled maintenance

`reap_stuck_jobs` (60s) · `reap_expiring_clips` (5 min) · weekly orphan-blob
sweep · `refresh_measured_latency` (hourly, recomputes catalogue p50s from
`motion_generations`).

---

## 9. AI provider abstraction

### 9.1 Ports

Four capabilities. The video port is asynchronous by nature — submit / poll /
fetch — and that shape is deliberately not hidden behind a synchronous façade,
because the job layer needs it.

```python
class TextPort(Protocol):
    async def generate_structured(self, *, schema: type[BaseModelT], system: str,
                                  user: str, max_tokens: int = 16000,
                                  effort: str = "high") -> StructuredResult[BaseModelT]: ...

class ImagePort(Protocol):
    async def generate(self, *, positive: str, negative: str, size: str,
                       seed: int | None, n: int = 1) -> list[ImageResult]: ...

class SpeechPort(Protocol):
    async def synthesize(self, *, text: str, voice_id: str, speed: float = 1.0,
                         style: str | None = None) -> SpeechResult: ...
    async def list_voices(self) -> list[Voice]: ...

class VideoPort(Protocol):
    """One interface for Veo, Kling, Hailuo, Luma, Runway-compatible, or an
    aggregator. Implementations are thin; everything the app reasons about
    lives in the catalogue (§9.2), not in these methods."""
    def serves(self, model_key: str) -> bool: ...
    async def submit(self, req: VideoRequest) -> Submission: ...
    async def poll(self, sub: Submission) -> OperationState: ...
    async def fetch(self, state: OperationState) -> VideoResult: ...

@dataclass(frozen=True)
class VideoRequest:
    model_key: str
    model_id: str                       # provider-native id from the catalogue
    first_frame: bytes                  # the APPROVED still — always image-to-video
    prompt: str
    negative_prompt: str | None         # None when the model has no such input
    reference_images: list[bytes]       # already truncated to caps.max_reference_images
    duration_s: float                   # already resolved against capabilities
    resolution: str                     # already resolved
    aspect_ratio: str
    seed: int | None

@dataclass
class Submission:
    provider_job_id: str
    endpoint: str
    expires_at: datetime | None         # providers that delete media (Veo: +2 days)

@dataclass
class OperationState:
    done: bool
    error: ProviderError | None
    video_uri: str | None
    reported_cost_cents: int | None     # populated only where the provider says
    raw: dict
```

### 9.2 The model catalogue — provider facts as data

This is the core of the revision. Every provider constraint that used to leak
into the domain schema now lives in one typed, version-controlled file.

```python
# ai/catalog.py
class ModelTier(StrEnum):
    ECONOMY = "economy"; STANDARD = "standard"; PREMIUM = "premium"

class AudioBehavior(StrEnum):
    NONE = "none"; OPTIONAL = "optional"; ALWAYS_ON = "always_on"

@dataclass(frozen=True)
class DurationSupport:
    kind: Literal["discrete", "range"]
    values: tuple[float, ...] = ()          # discrete
    min_s: float = 0.0                      # range
    max_s: float = 0.0
    step_s: float = 1.0

    def resolve(self, target_s: float) -> float:
        """Smallest legal duration >= target; else the longest available."""
        if self.kind == "discrete":
            legal = sorted(self.values)
            return next((v for v in legal if v >= target_s - 1e-6), legal[-1])
        clamped = min(max(target_s, self.min_s), self.max_s)
        steps = math.ceil((clamped - self.min_s) / self.step_s - 1e-6)
        return min(self.min_s + steps * self.step_s, self.max_s)

@dataclass(frozen=True)
class Pricing:
    kind: Literal["per_second", "per_clip"]
    usd: Mapping[str, float]                # keyed by resolution
    def cents(self, duration_s: float, resolution: str) -> int:
        unit = self.usd.get(resolution) or next(iter(self.usd.values()))
        return round(unit * (duration_s if self.kind == "per_second" else 1) * 100)

@dataclass(frozen=True)
class VideoModelCaps:
    model_key: str                # our stable key, e.g. "kling-2.5-turbo-i2v"
    provider: str                 # "veo" | "kling" | "hailuo" | "luma" | "runway" | "fal"
    model_id: str                 # provider-native id actually sent on the wire
    display_name: str
    tier: ModelTier
    adapter: str                  # which VideoPort implementation serves it
    image_to_video: bool
    durations: DurationSupport
    resolutions: tuple[str, ...]
    aspect_ratios: tuple[str, ...]
    max_reference_images: int
    supports_negative_prompt: bool
    supports_seed: bool
    audio: AudioBehavior
    pricing: Pricing
    typical_latency_s: int        # seed value; overwritten by measured p50
    max_wait_s: int
    retention_hours: int | None   # None = provider keeps it indefinitely
    notes: str = ""
```

**Seed catalogue.** These entries are indicative *starting points* — published
prices vary between direct APIs and aggregators, and several sources disagree.
**M0.5 replaces every number here with a measured one** (§13). Treat the file as
a hypothesis, not a fact.

> **Implemented.** The real catalogue now lives at `tools/bakeoff/catalog.py`
> and is the source of truth. It carries pricing provenance (`source`,
> `verified_at`, `confidence`) and a per-entry `status` recording how well each
> provider's request shape is understood. The sketch below is illustrative
> only — read the module. It graduates to `apps/api/app/ai/catalog.py` at M6.

Sketch:

```python
CATALOG: dict[str, VideoModelCaps] = index_by_key([
    VideoModelCaps(
        model_key="wan-2.5-i2v", provider="fal", model_id="fal-ai/wan-2.5/i2v",
        display_name="Wan 2.5", tier=ModelTier.ECONOMY, adapter="fal",
        image_to_video=True,
        durations=DurationSupport("discrete", values=(5.0, 10.0)),
        resolutions=("480p", "720p"), aspect_ratios=("16:9", "9:16"),
        max_reference_images=0, supports_negative_prompt=True, supports_seed=True,
        audio=AudioBehavior.NONE,
        pricing=Pricing("per_second", {"480p": 0.05, "720p": 0.05}),
        typical_latency_s=90, max_wait_s=900, retention_hours=None,
        notes="Cheapest usable option; no reference images.",
    ),
    VideoModelCaps(
        model_key="kling-2.5-turbo-i2v", provider="fal", model_id="fal-ai/kling-video/v2.5-turbo/image-to-video",
        display_name="Kling 2.5 Turbo", tier=ModelTier.ECONOMY, adapter="fal",
        image_to_video=True,
        durations=DurationSupport("discrete", values=(5.0, 10.0)),
        resolutions=("720p", "1080p"), aspect_ratios=("16:9", "9:16"),
        max_reference_images=0, supports_negative_prompt=True, supports_seed=True,
        audio=AudioBehavior.NONE,
        pricing=Pricing("per_second", {"720p": 0.07, "1080p": 0.07}),
        typical_latency_s=120, max_wait_s=1200, retention_hours=None,
        notes="Strong motion quality per dollar; the expected MVP workhorse.",
    ),
    VideoModelCaps(
        model_key="hailuo-2.3-standard-i2v", provider="fal", model_id="fal-ai/minimax/hailuo-02/standard/image-to-video",
        display_name="Hailuo 2.3 Standard", tier=ModelTier.ECONOMY, adapter="fal",
        image_to_video=True,
        durations=DurationSupport("discrete", values=(6.0, 10.0)),
        resolutions=("768p",), aspect_ratios=("16:9", "9:16"),
        max_reference_images=0, supports_negative_prompt=False, supports_seed=False,
        audio=AudioBehavior.NONE,
        pricing=Pricing("per_clip", {"768p": 0.28}),
        typical_latency_s=150, max_wait_s=1200, retention_hours=None,
        notes="FLAT per-clip price — cheapest way to buy a 10s shot.",
    ),
    VideoModelCaps(
        model_key="luma-ray-i2v", provider="luma", model_id="ray-2",
        display_name="Luma Ray 2", tier=ModelTier.STANDARD, adapter="luma",
        image_to_video=True,
        durations=DurationSupport("discrete", values=(5.0, 9.0)),
        resolutions=("720p", "1080p"), aspect_ratios=("16:9", "9:16"),
        max_reference_images=0, supports_negative_prompt=False, supports_seed=False,
        audio=AudioBehavior.NONE,
        pricing=Pricing("per_second", {"720p": 0.10, "1080p": 0.14}),
        typical_latency_s=120, max_wait_s=1200, retention_hours=None,
    ),
    VideoModelCaps(
        model_key="runway-gen4-turbo-i2v", provider="runway", model_id="gen4_turbo",
        display_name="Runway Gen-4 Turbo", tier=ModelTier.STANDARD, adapter="runway",
        image_to_video=True,
        durations=DurationSupport("discrete", values=(5.0, 10.0)),
        resolutions=("720p", "1080p"), aspect_ratios=("16:9", "9:16"),
        max_reference_images=0, supports_negative_prompt=False, supports_seed=True,
        audio=AudioBehavior.NONE,
        pricing=Pricing("per_second", {"720p": 0.12, "1080p": 0.15}),
        typical_latency_s=100, max_wait_s=900, retention_hours=None,
    ),
    VideoModelCaps(
        model_key="veo-3.1-fast-i2v", provider="veo", model_id="veo-3.1-fast-generate-preview",
        display_name="Veo 3.1 Fast", tier=ModelTier.STANDARD, adapter="veo",
        image_to_video=True,
        durations=DurationSupport("discrete", values=(4.0, 6.0, 8.0)),
        resolutions=("720p", "1080p"), aspect_ratios=("16:9", "9:16"),
        max_reference_images=3, supports_negative_prompt=False, supports_seed=True,
        audio=AudioBehavior.ALWAYS_ON,
        pricing=Pricing("per_second", {"720p": 0.10, "1080p": 0.12}),
        typical_latency_s=150, max_wait_s=1500, retention_hours=48,
        notes="Reference images (≤3) — best character consistency in the catalogue. "
              "Audio cannot be disabled; media deleted after 48h.",
    ),
    VideoModelCaps(
        model_key="veo-3.1-standard-i2v", provider="veo", model_id="veo-3.1-generate-preview",
        display_name="Veo 3.1", tier=ModelTier.PREMIUM, adapter="veo",
        image_to_video=True,
        durations=DurationSupport("discrete", values=(4.0, 6.0, 8.0)),
        resolutions=("720p", "1080p"), aspect_ratios=("16:9", "9:16"),
        max_reference_images=3, supports_negative_prompt=False, supports_seed=True,
        audio=AudioBehavior.ALWAYS_ON,
        pricing=Pricing("per_second", {"720p": 0.40, "1080p": 0.40}),
        typical_latency_s=180, max_wait_s=1800, retention_hours=48,
        notes="PREMIUM. Reserve for the two or three shots that carry the film.",
    ),
])
```

Adding a model is a new entry in this list. No migration, no new endpoint, no UI
change — `/video-models` and `ModelPicker` render whatever is present.

### 9.3 Planning: intent resolved against capabilities

The one place the app reconciles what you asked for with what a model can do.
Pure, free, unit-testable, and called by both the UI (`motion:plan`) and the
submit handler (which re-plans rather than trusting the client).

```python
@dataclass
class GenerationPlan:
    model_key: str; model_id: str; adapter: str; provider: str
    prompt: MotionPrompt
    reference_images: list[bytes]; reference_checksums: list[str]
    requested_duration_s: float; resolved_duration_s: float
    requested_resolution: str;    resolved_resolution: str
    aspect_ratio: str; seed: int | None
    estimated_cost_cents: int
    warnings: list[Warning]        # clamped, dropped, substituted
    blocking: list[Blocking]       # cannot proceed

def plan_motion(shot, scene, project, model_key: str | None) -> GenerationPlan:
    caps = CATALOG[model_key or project.default_model_key or cheapest_capable(shot, project)]
    warnings, blocking = [], []

    if not shot.selected_image_id:
        blocking.append(Blocking("no_approved_still",
            "Approve a still for this shot before generating motion."))
    if caps.tier is ModelTier.PREMIUM and not project.allow_premium:
        blocking.append(Blocking("premium_not_enabled",
            f"{caps.display_name} is a premium model. Enable premium spend first."))
    if project.aspect_ratio not in caps.aspect_ratios:
        blocking.append(Blocking("aspect_unsupported",
            f"{caps.display_name} does not support {project.aspect_ratio}."))

    resolved_d = caps.durations.resolve(shot.target_duration_s)
    if abs(resolved_d - shot.target_duration_s) > 0.05:
        warnings.append(Warning("duration_adjusted",
            f"{caps.display_name} produces {resolved_d:g}s clips; your {shot.target_duration_s:g}s "
            f"shot will be {'padded with a held frame' if resolved_d < shot.target_duration_s else 'trimmed'}."))

    resolved_r = best_resolution(caps.resolutions, project.image_size)
    refs = locked_reference_images(shot)[:caps.max_reference_images]
    if len(locked_reference_images(shot)) > caps.max_reference_images:
        warnings.append(Warning("references_truncated",
            f"{caps.display_name} accepts {caps.max_reference_images} reference image(s); "
            f"using {[c.name for c in shot.locked_subjects][:caps.max_reference_images]}. "
            f"Character consistency may drift for the others."))
    if caps.max_reference_images == 0 and shot.locked_subjects:
        warnings.append(Warning("no_reference_support",
            f"{caps.display_name} has no reference-image input. Consistency relies "
            f"entirely on the approved still as the first frame."))
    if caps.audio is AudioBehavior.ALWAYS_ON:
        warnings.append(Warning("audio_discarded",
            f"{caps.display_name} always generates audio; it will be discarded (D12)."))

    prompt = compose_motion_prompt(shot, scene, project, caps)
    return GenerationPlan(..., estimated_cost_cents=caps.pricing.cents(resolved_d, resolved_r),
                          warnings=warnings, blocking=blocking)

def cheapest_capable(shot, project) -> str:
    """D5: economy-first default."""
    candidates = [c for c in CATALOG.values()
                  if c.image_to_video
                  and project.aspect_ratio in c.aspect_ratios
                  and (c.tier is not ModelTier.PREMIUM or project.allow_premium)]
    return min(candidates,
               key=lambda c: c.pricing.cents(c.durations.resolve(shot.target_duration_s),
                                             best_resolution(c.resolutions, project.image_size))
              ).model_key
```

Every warning above is a sentence a human can act on, shown in the picker before
any money moves. **Capability mismatches warn; only impossibilities block.** A
model with no reference-image support is a legitimate, cheap choice — it just
means you lean harder on the approved still, and the UI should say exactly that
rather than hiding the model or failing at submit time.

### 9.4 Adapters

Three implementations cover the whole catalogue:

| Adapter | Serves | Why |
|---|---|---|
| `FalVideoAdapter` | Wan, Kling, Hailuo, and most economy entries | **One integration, one credential, one polling shape, many models.** For a solo developer this is the single largest work saving available, and it is what makes "interchangeable providers" affordable to build. |
| `VeoVideoAdapter` | Veo 3.1 Fast / Standard | Direct Gemini API — reference images and the 48h retention behaviour are worth handling first-party |
| `RunwayVideoAdapter` | Runway-compatible task APIs (Runway, Luma) | Both follow the same submit→task-id→poll→asset-url shape; one adapter with a small dialect table serves them |

```python
class FalVideoAdapter(VideoPort):
    def serves(self, model_key): return CATALOG[model_key].adapter == "fal"

    async def submit(self, req: VideoRequest) -> Submission:
        payload = {
            "image_url": await self._upload(req.first_frame),
            "prompt": req.prompt,
            "duration": str(int(req.duration_s)),
            "resolution": req.resolution,
            "aspect_ratio": req.aspect_ratio,
        }
        if req.negative_prompt: payload["negative_prompt"] = req.negative_prompt
        if req.seed is not None: payload["seed"] = req.seed
        r = await self._http.post(f"/{req.model_id}", json=payload)   # queued submit
        return Submission(provider_job_id=r.json()["request_id"],
                          endpoint=req.model_id, expires_at=None)

    async def poll(self, sub) -> OperationState: ...
    async def fetch(self, state) -> VideoResult: ...
```

Adapters stay ~80 lines because they contain **no policy** — no duration
snapping, no reference truncation, no pricing, no tier rules. All of that
happened in `plan_motion` against the catalogue.

### 9.5 Text, image, speech adapters

**Text: Claude via the `anthropic` SDK with structured outputs.**

```python
resp = await self._client.messages.parse(
    model="claude-opus-5", max_tokens=16000,
    system=system, messages=[{"role": "user", "content": user}],
    output_format=schema,                    # Pydantic model as the contract
    thinking={"type": "adaptive"},
    output_config={"effort": effort},
)
if resp.stop_reason == "refusal":
    raise ProviderError(code="provider_refused", retryable=False)
value = resp.parsed_output
```

- `.parsed_output` is already validated; the repair loop handles only the
  cross-field validators (§4.2).
- Use `thinking={"type":"adaptive"}`; **do not** pass `budget_tokens` (rejected
  on current models). Tune with `output_config.effort` — `high` for storyboards,
  `low` for `narration:shorten`.
- **Prompt caching:** style bible + character canon + story text are a stable
  prefix across per-scene regenerations; `cache_control` breakpoint after them.
- Cost anchor: `claude-opus-5` $5/$25 per 1M in/out; `claude-sonnet-5` $3/$15;
  `claude-haiku-4-5` $1/$5. A storyboard run is ~15K in / ~8K out — cents.

**Image:** the highest-leverage provider choice in the system now, because the
still is both a shipped deliverable *and* the first frame. Judge on 1920×1080
quality and character fidelity. ~$0.04–0.08 per still; ~$2 per full pass.

**Speech:** per-character voices; duration from the response or `ffprobe`.

### 9.6 Cross-cutting concerns as decorators

```python
video = CostTracked(QuotaLimited(Retried(Traced(CompositeVideoPort([
    FalVideoAdapter(...), VeoVideoAdapter(...), RunwayVideoAdapter(...),
])))))
```

`CompositeVideoPort` routes by `caps.adapter`; each wrapper implements the same
Protocol. `QuotaLimited` holds a **per-provider** Redis token bucket.
`CostTracked` enforces the budget **before** submit and writes `ai_calls`.

**Cost model.** A 14-shot, ~6s-per-shot film, animating a subset:

| Strategy | Motion spend |
|---|---|
| Ken Burns only (the default product) | **$0** |
| 5 shots on Kling 2.5 Turbo @ ~$0.07/s | **~$3.50** |
| All 14 on Kling 2.5 Turbo | ~$9.80 |
| All 14 on Hailuo (flat ~$0.28/clip) | ~$3.92 |
| 12 economy + 2 premium Veo Standard @ 8s | ~$14 |
| All 14 on Veo Standard @ 8s | ~$44.80 |

The realistic finished gift is **$10–40 all-in**, versus $80–250 in v2.0 — and
the floor is now genuinely near zero, because the free preview is a complete
film. Default budget $60, `allow_premium=false`, default model = cheapest
capable.

### 9.7 Registry & fakes

```
AI_TEXT_PROVIDER=anthropic
AI_IMAGE_PROVIDER=<chosen>
AI_SPEECH_PROVIDER=<chosen>
VIDEO_ADAPTERS=fal,veo,runway        # which credentials exist in this env
DEFAULT_MODEL_KEY=kling-2.5-turbo-i2v
MOTION_MAX_CONCURRENT_FAL=4
MOTION_MAX_CONCURRENT_VEO=2
```

Every port has a fake. `FakeVideo` serves **every catalogue key**, honours the
declared capabilities (snapping duration exactly as the real one would), sleeps
`typical_latency_s / 10`, and returns an FFmpeg-generated clip built from the
submitted first frame with a slow zoom and a burned-in
`FAKE · {model_key} · {duration}s` label. The whole pipeline — both render
profiles, the planning path, the polling path — must run end to end with all
four fakes and zero spend.

---

## 10. Video rendering pipeline

### 10.1 One renderer, three source kinds

The Timeline abstracts *what fills a shot's screen time*, so a single renderer
covers the free preview, the hybrid final, and everything between:

| Source kind | Comes from | Used in |
|---|---|---|
| `still` | approved still + Ken Burns move | preview (always); final (any shot left on `kenburns`) |
| `clip` | generated motion asset | final, for shots with `motion_mode='generated'` |
| `clip` (manual) | uploaded video | final, for `motion_mode='manual'` |

| | `preview` | `final` |
|---|---|---|
| Sources | all `still` | mixed per shot |
| Resolution / fps | 1280×720 @ 24 | 1920×1080 @ 24 |
| x264 | crf 26, veryfast | crf 18, medium |
| Subtitles | burned (to proof them) | optional |
| Cost of sources | stills only (~$2) | + whatever motion you bought |
| Wall clock | < 90s | < 5 min |

**The hybrid final render is not a compromise, it is the product.** Motion where
it earns its cost, held frames with gentle movement elsewhere — which is how
plenty of real documentary and storybook film is cut.

### 10.2 Timeline JSON (the render contract)

Computed by `render_service`, frozen onto `renders.timeline`, handed to a
renderer that reads nothing else — no DB, no network, no catalogue.

```jsonc
{
  "schema_version": "3.0",
  "profile": "final",
  "width": 1920, "height": 1080, "fps": 24,
  "audio": { "sample_rate": 48000, "music_key": "beds/warm_piano.mp3", "music_db": -22 },
  "title_card": { "text": "The Lighthouse Keeper", "duration_ms": 2500 },
  "clips": [
    {
      "shot_id": "…", "scene_index": 0, "shot_index": 0,
      "source": { "kind": "clip", "path": "…/mg_9f2.mp4",
                  "native_duration_ms": 5000, "has_audio": false,
                  "provider": "fal", "model_key": "kling-2.5-turbo-i2v" },
      "kenburns": null,
      "start_ms": 2500, "duration_ms": 5600, "tail_freeze_ms": 600,
      "transition_out": { "type": "cut" },
      "audio": [ { "line_id": "…", "path": "…/nar_1.mp3",
                   "offset_ms": 300, "duration_ms": 4400,
                   "text": "It was the last winter the light would burn." } ],
      "clip_hash": "sha256:…"
    },
    {
      "shot_id": "…", "scene_index": 0, "shot_index": 1,
      "source": { "kind": "still", "path": "…/img_7c1.png" },
      "kenburns": { "move": "push_in", "start_scale": 1.00, "end_scale": 1.12 },
      "start_ms": 8100, "duration_ms": 6000, "tail_freeze_ms": 0,
      "transition_out": { "type": "cut" },
      "audio": [ … ], "clip_hash": "sha256:…"
    }
  ],
  "end_card": { "text": "Happy Birthday", "duration_ms": 3000 },
  "total_duration_ms": 96500
}
```

`provider` and `model_key` ride along on clip sources purely so a render is
self-describing when you come back to it in a week.

### 10.3 Duration algorithm

```
per shot:
  narration_ms = Σ(line.duration_ms on the shot)
  required_ms  = narration_ms + 900                (300 lead-in + 600 tail)
  kind == still →  duration_ms = max(target_duration_s*1000, required_ms)
                   tail_freeze_ms = 0              (Ken Burns stretches for free)
  kind == clip  →  clip_ms = ffprobe(path)         ← measured, never assumed
                   duration_ms    = max(clip_ms, required_ms)
                   tail_freeze_ms = max(0, required_ms - clip_ms)
start_ms accumulates over the ordered clips, after the title card.
```

Audio is placed at **absolute** offsets, so one wrong duration cannot cascade.
Video is padded, never retimed. Preflight blocks `tail_freeze_ms > 1500` —
and for an animated shot, one of the offered fixes is always "drop back to Ken
Burns", which is free and instantly satisfiable.

### 10.4 FFmpeg stages

**Stage A — normalise each source to a uniform intermediate**, branching on
`source.kind`. Uniformity is what makes Stage D a stream copy.

*A1 — clip source.* Providers differ in fps, resolution, padding, and whether
audio is present, so normalise unconditionally:

```bash
ffmpeg -y -i mg_9f2.mp4 \
  -filter_complex "\
     scale=1920:1080:force_original_aspect_ratio=decrease,\
     pad=1920:1080:(ow-iw)/2:(oh-ih)/2,\
     fps=24,tpad=stop_mode=clone:stop_duration=0.6,\
     format=yuv420p,setsar=1[v]" \
  -map "[v]" -an \
  -c:v libx264 -preset medium -crf 18 clips/000.mp4
```

`tpad=stop_mode=clone` is the freeze-frame tail (`stop_duration` =
`tail_freeze_ms/1000`; omit when zero). `-an` implements D12 — provider-generated
audio is dropped here, uniformly, whatever the provider.

*A2 — still source (Ken Burns).* Render at 2× and downscale to avoid
`zoompan`'s integer-pixel jitter:

```bash
ffmpeg -y -loop 1 -i img_7c1.png -t 6.0 \
  -filter_complex "\
     scale=3840:-2:flags=lanczos,\
     zoompan=z='min(1.0+0.12*on/(24*6.0),1.12)':\
             x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':\
             d=144:s=3840x2160:fps=24,\
     scale=1920:1080:flags=lanczos,format=yuv420p,setsar=1" \
  -c:v libx264 -preset medium -crf 18 -r 24 clips/001.mp4
```

`camera_move` maps to the `z`/`x`/`y` expressions, so a Ken Burns shot rehearses
the same camera intent it would get if you later animated it.

**Stage B — per-clip cache.** `clip_hash = sha256(source checksum + kind +
kenburns params + profile + duration + tail_freeze + RENDERER_VERSION)`. Cached
intermediates are hard-linked into the scratch dir, so re-rendering after
animating two shots takes seconds.

**Stage C — audio.** Narration lines at absolute offsets, mixed and
loudness-normalised; music bed looped at −22dB with fades:

```bash
ffmpeg -y -i nar_1.mp3 -i nar_2.mp3 … \
  -filter_complex "[0]adelay=2800|2800[a0];[1]adelay=8400|8400[a1];…;\
                   [a0][a1]…amix=inputs=N:normalize=0:duration=longest[vo];\
                   [vo]loudnorm=I=-16:TP=-1.5:LRA=11[out]" \
  -map "[out]" -ar 48000 -ac 2 narration.wav
```

**Stage D — concat.** `ffmpeg -f concat -safe 0 -i clips.txt -c copy video.mp4`

**Stage E — mix, mux, subtitle.**

```bash
ffmpeg -y -i video.mp4 -i narration.wav -i music.wav \
  -filter_complex "[1][2]amix=inputs=2:weights=1 0.18:normalize=0[a]" \
  -map 0:v -map "[a]" \
  -vf "subtitles=captions.ass:force_style='FontName=Inter,Fontsize=42'" \
  -c:v libx264 -crf 18 -preset medium -pix_fmt yuv420p \
  -c:a aac -b:a 192k -movflags +faststart final.mp4
```

Drop `-vf` and stream-copy the video when subtitles are off. Emit `poster.jpg`
at the 30% mark and a `.vtt` for the in-app player.

### 10.5 Preflight

| Class | Check |
|---|---|
| **blocking** | shot with no approved still (both profiles need one) |
| **blocking** | `motion_mode='generated'` with no ready clip |
| **blocking** | narration line with no audio |
| **blocking** | `tail_freeze_ms > 1500` on any shot |
| **blocking** | clip asset missing from blob storage (expired before download) |
| advisory | stale stills or clips |
| advisory | unlocked characters |
| advisory | **animated shots span more than two different models** |
| advisory | **a single animated shot isolated among 12 Ken Burns shots** |
| advisory | total duration outside the target band; no music bed |

Those two new advisories matter more than they look. A film where three shots
came from three different providers reads as *inconsistent* rather than varied,
and a single moving shot in a still film draws the eye to the wrong place.
Preflight should name the shots and suggest either committing more or reverting
that one to Ken Burns.

### 10.6 Operational notes

- Scratch dir per render at `/var/tmp/render/{render_id}`; kept on failure with
  a 24h reaper.
- Stream FFmpeg `stderr`, parse `time=`, publish real progress.
- Persist the full FFmpeg log to blob storage on failure.
- Pin the FFmpeg version in the image.
- Verify with `ffprobe` that output duration matches
  `timeline.total_duration_ms` within 100ms; fail loudly otherwise.

---

## 11. Folder structure

```
hbday-zee/
├─ docker-compose.yml       # postgres, redis, api, worker-ai, worker-motion, worker-render
├─ Makefile                 # dev, test, types, migrate, render-demo, bakeoff
├─ docs/
│  ├─ ARCHITECTURE.md
│  └─ adr/                  # 001-bakeoff-results.md lands here
│
├─ tools/
│  └─ bakeoff/              # M0.5 — standalone, no app or DB dependency
│     ├─ README.md
│     ├─ bakeoff.yaml       # which models, which prompts, how many repeats
│     ├─ run.py             # CLI entrypoint
│     ├─ catalog.py         # ← graduates to apps/api/app/ai/catalog.py at M6
│     ├─ adapters/          # ← graduate to apps/api/app/ai/adapters/ at M6
│     │   ├─ base.py · fal.py · veo.py · runway.py
│     ├─ prompts.py         # the standardized prompt set
│     ├─ report.py          # markdown + HTML contact sheet
│     └─ inputs/            # three source images live here
│
├─ apps/
│  ├─ api/
│  │  ├─ pyproject.toml · alembic/versions/
│  │  └─ app/
│  │     ├─ main.py · config.py
│  │     ├─ db/       session.py · models/ · repositories/
│  │     ├─ routers/  auth · projects · story · storyboard · characters
│  │     │            scenes · shots · stills · video_models · motion
│  │     │            narration · renders · jobs · events
│  │     ├─ services/ ← THE EIGHT DOMAINS
│  │     │   story_service · storyboard_service · character_service
│  │     │   scene_service · still_service
│  │     │   motion_service.py     # plan, submit, select, clear, tier rules
│  │     │   narration_service · render_service
│  │     │   freshness.py · budget.py
│  │     ├─ schemas/  ai/ · api/ · export.py
│  │     ├─ ai/
│  │     │   ├─ ports.py
│  │     │   ├─ catalog.py         # ← THE capability model (from tools/bakeoff)
│  │     │   ├─ planning.py        # plan_motion(), cheapest_capable()
│  │     │   ├─ registry.py · composite.py
│  │     │   ├─ adapters/  anthropic_text · <image> · <speech>
│  │     │   │             fal_video · veo_video · runway_video · fakes
│  │     │   ├─ middleware/ retry · quota · cost · trace
│  │     │   └─ prompts/   system/*.md · compose.py · versions.py
│  │     ├─ jobs/
│  │     │   ├─ queue.py · progress.py
│  │     │   └─ handlers/  story · storyboard · character · still
│  │     │                 motion_submit · motion_poll · motion_download
│  │     │                 narration · render · batch · maintenance
│  │     ├─ render/         ← standalone, DB-free, CLI-runnable
│  │     │   ├─ timeline.py · normalize.py · kenburns.py · audio.py
│  │     │   ├─ subtitles.py · ffmpeg.py · pipeline.py · cli.py
│  │     └─ storage/  base · local · s3
│  │  └─ tests/ unit/ · integration/ · fixtures/
│  │
│  └─ web/  app/ · components/ · lib/{api,hooks,types}/
│
├─ packages/schemas/
└─ assets/music/
```

**Two rules to protect.** `app/render/` imports nothing from `db/`, `services/`,
or `ai/` — it takes a Timeline and file paths. And **no file outside
`app/ai/adapters/` may name a provider.** Everything else routes through
`catalog.py` and `planning.py`. If a `if provider == "veo"` appears in a service
or a router, the abstraction has leaked and should be pushed back into a
capability field.

---

## 12. MVP vs. future

### 12.1 In the MVP

Story intake · analysis · storyboard generation with provider-neutral durations
and narration budgets · character lock with reference portraits · scene/shot
editing · still generation with candidates and an **approval checkpoint** ·
per-line TTS with fit checking · **free Ken Burns preview render as the default
deliverable** · **model catalogue with capability model** · per-shot provider and
model selection · free `motion:plan` cost estimates with plain-language
capability warnings · economy-first defaults with premium behind an explicit
opt-in · motion submit/poll/download across ≥2 adapters (fal + one direct) ·
full generation records (provider, model, provider job id, requested/resolved
duration and resolution, estimated and actual cost, status) · per-shot re-roll
and provider comparison · **hybrid final render** · job system with progress,
retry, cost tracking, hard budget · share link · fakes for all four ports.

### 12.2 Deliberately deferred

| Feature | Why it waits |
|---|---|
| Automated quality scoring across providers | You cannot cheaply score "does this look right"; the bake-off is a human sitting down for an hour |
| Clip extension / first-last-frame interpolation | Provider-specific, another generation graph |
| Text-to-video | Loses the approval checkpoint; D4 |
| Webhooks instead of polling | Needs a public endpoint; polling is one handler |
| 4K, crossfades, music ducking, generated score | Cost or filter-graph complexity for marginal gain |
| Word-level karaoke subtitles | Provider-dependent; line-level cues are fine |
| In-browser timeline editor | Weeks of work; TimelinePreview covers the real need |
| LoRA / character fine-tuning | Approved first frames + reference images get most of the way |
| Per-provider prompt auto-tuning | Real, but only after you know which providers you use |
| Multi-user, roles, real auth · 9:16 variants · RAG over the story | Out of scope by design |

### 12.3 Things to *not* build that will tempt you

- A generic workflow/DAG engine. Eight fixed stages, eight handlers.
- Provider-specific branches outside `adapters/`. Add a capability field instead.
- A database table for the model catalogue. It is typed code under review.
- An "animate everything" button. There is deliberately no such verb (§6).
- Automatic re-rolling on a heuristic. Each guess costs real money.

---

## 13. Development milestones

**M0 — Skeleton (2 days).** Compose stack; FastAPI health; Next shell; Alembic
initial; auth cookie; project CRUD end to end. Deploy to the real VPS now.

### M0.5 — Provider bake-off (2–3 days, ~$25). Before any application code.

A standalone harness under `tools/bakeoff/`. It has no database, no FastAPI, no
frontend, and does not import the app. It exists to replace every guessed number
in §9.2 with a measured one, and to tell you which two or three models are worth
integrating at all.

**Inputs.** Three source images in `inputs/`, chosen to stress different things
and **rendered in your actual intended art style** (generate them with your
chosen image provider first — testing on stock photos tells you nothing about
how your film will look):

1. `01-character-closeup.png` — one locked character, face prominent. Tests
   identity drift, the failure everyone notices.
2. `02-two-characters-medium.png` — two characters interacting. Tests multi-
   subject coherence and hands.
3. `03-establishing-wide.png` — landscape or interior, no people. Tests
   atmosphere, parallax, and whether "static camera" is honoured.

**Standardized prompts.** The same matrix for every model, composed by the same
`compose_motion_prompt` the app will use, so the harness tests *your* prompting
and not ad-hoc strings:

```python
# tools/bakeoff/prompts.py
CASES = [
    Case("static-subtle",  camera_move="static",   subject_motion="Small ambient movement only: hair and fabric shift slightly."),
    Case("push-in",        camera_move="push_in",  subject_motion="The subject breathes and blinks."),
    Case("pan",            camera_move="pan_right",subject_motion="Light moves gently across the scene."),
    Case("action",         camera_move="handheld", subject_motion="The character turns their head and looks off-screen."),
]
```

**Configuration.**

```yaml
# bakeoff.yaml
repeats: 2                     # same input+prompt twice → consistency signal
durations: [5, 6, 8]           # harness resolves each per capabilities
resolution: 1080p
aspect_ratio: "16:9"
budget_usd: 30                 # hard stop; the harness refuses to exceed it
models:
  - wan-2.5-i2v
  - kling-2.5-turbo-i2v
  - hailuo-2.3-standard-i2v
  - luma-ray-i2v
  - runway-gen4-turbo-i2v
  - veo-3.1-fast-i2v
  - veo-3.1-standard-i2v       # premium: 1 image × 1 case only
```

**What it does.**

```
run.py
  1. load catalog + config; expand the matrix (image × case × model × repeat)
  2. price the whole matrix up front; print the total; REFUSE if > budget_usd;
     require an explicit --yes to spend
  3. submit with bounded per-provider concurrency; poll with backoff
  4. for each generation record:
       provider · model_key · model_id · provider_job_id
       requested vs resolved duration · requested vs resolved resolution
       capability warnings raised at plan time
       submit_at · first_done_at · latency_ms
       estimated_cost_cents · actual_cost_cents (when reported)
       status · error_code
       output path · sha256 · ffprobe (duration, fps, dimensions, has_audio)
  5. download every clip to out/{model_key}/{image}-{case}-{n}.mp4
  6. write out/results.jsonl (one record per generation) + out/summary.json
  7. render the report
```

**Outputs.**

- `out/results.jsonl` — the raw record per generation, exactly the field set
  that `motion_generations` will later persist. This is not a coincidence: the
  harness is where that schema gets validated against reality.
- `out/report.md` — per-model table: median / p90 latency, estimated vs actual
  cost, cost per *finished second*, duration adherence (did an 8s request yield
  8.0s?), audio presence, failure and refusal counts.
- `out/contact-sheet.html` — a single self-contained page: rows are input image
  × case, columns are models, each cell an inline `<video>` with model, duration
  and price under it. **This is the actual deliverable.** Latency and cost
  decide the shortlist; your eyes decide the winner, and they need everything
  side by side to do it.
- `out/scores.csv` — a stub you fill in by hand: identity drift, motion quality,
  artifacts, style adherence, 1–5 each. Twenty minutes of honest scoring here is
  worth more than any automated metric you could build in a week.

**Exit criteria.** `docs/adr/001-bakeoff-results.md` records: the chosen default
economy model, the chosen premium model, measured `typical_latency_s` and
`max_wait_s` per model, corrected pricing, observed reference-image behaviour,
and any capability in §9.2 that turned out to be wrong. Update `catalog.py` from
the measurements. **Then delete every model you will not use** — a catalogue of
three well-understood models beats seven guesses.

**M1 — Renderer first, with fakes (4 days).** `app/render/` + CLI. Hand-write a
Timeline; produce a preview MP4 from stills, then a hybrid MP4 mixing stills and
the clips M0.5 produced. No database, no AI, no frontend. Exit: `make
render-demo` produces both, and you would be happy to send either.

**M2 — Schemas + AI text pipeline (4 days).** Pydantic contracts including
referential-integrity and narration-fit validators. Anthropic adapter with
structured outputs, repair loop, cost accounting, `FakeText` with golden
fixtures. Feed three real stories; assert valid storyboards. Tune system prompts
here — output quality is decided in this milestone.

**M3 — Persistence + jobs (4 days).** Full schema and migrations; arq; claim /
progress / idempotency; SSE; `JobDrawer`. Story page, Analyze, Generate
Storyboard, materialisation.

**M4 — Characters + stills (4 days).** Character editor, lock semantics, prompt
composition, `PromptInspector`, image adapter, candidate picker, **approval
checkpoint**, upload override, freshness badges, budget cap.

**M5 — Narration + free preview (4 days).** Speech adapter, voice assignment,
per-line review, duration capture, fit check, `OverflowFixMenu`, subtitles, and
the preview render wired end to end. Exit: **a complete watchable narrated film
for ~$3 of spend.** This is the milestone where the gift exists.

**M6 — Motion (5 days).** Lift `catalog.py` and the adapters out of
`tools/bakeoff/`. `plan_motion`, `/video-models`, `motion:plan`, submit / poll /
download handlers, per-provider quota buckets, `MotionTable`, `ModelPicker`,
cost gate with `confirm_cost_cents`, premium opt-in, re-roll and provider
compare, expiry reaper. Exit: three shots animated on two different providers.

**M7 — Hybrid final render (3 days).** Normalise stage for clips, mixed
timeline, final preflight with the consistency advisories, per-clip cache,
render history, watch page, download, share link. Exit: **the finished film.**

**M8 — Quality pass (all remaining time).** Prompt tuning against real clips,
pacing, motion vocabulary, title/end cards, error ergonomics, and iterating on
the story itself. Budget a third of total calendar here.

**Calendar guidance.** ~30 working days to M7, but **the gift exists at M5,
around day 20.** If the deadline tightens, cut in this order: provider compare →
premium tier → second adapter (ship fal only) → subtitles → music → per-scene
regeneration → share links → **motion entirely (ship the preview film)**. Do not
cut: the bake-off, the fakes, the approval checkpoint, the fit check, preflight,
manual upload overrides, the per-clip render cache, or the cost gate.

---

## 14. Architectural risks

**R0 — Two hazards the browser tests found that no unit test could.**
Both were invisible to the Python suite because that suite runs in a
configuration the deployed system never uses.

*The event bus never crossed processes.* Handlers run in the arq worker; SSE
subscribers live in the API. A per-process bus published into a process nobody
was listening to, so live updates never arrived outside tests -- and the inline
queue used by most tests runs handlers inside the API, where a local bus happens
to work. *Mitigation:* Redis pub/sub whenever the queue is `arq` (D13), plus
`tests/test_events_bus.py`, which publishes from a genuinely separate
interpreter and asserts the API process receives it.

*The test suite deleted real work.* `DELETE FROM projects` between tests ran
against whatever `DATABASE_URL` pointed at, which during normal development is
the development database -- so `make test` destroyed real stories, stills and
renders. *Mitigation:* `tests/conftest.py` redirects to a `_test` database,
creates and migrates it on demand, and asserts the name ends in `_test` so a
misconfiguration halts the run rather than quietly clearing live data.

**R1 — Provider quality variance and visual inconsistency.** Different models
render motion, colour, and faces differently; a film cut from three providers
reads as inconsistent rather than varied. Mixing animated and Ken Burns shots
can also draw the eye to the wrong place. *Mitigation:* pick **one** workhorse
model at M0.5 and use it for nearly everything; reserve a premium model for two
or three shots; preflight advisories name mixed-model and isolated-motion cases;
keep Ken Burns moves subtle so the two source kinds sit together.

**R2 — Character drift, worse without reference images.** Most economy models
accept no reference images, so consistency rests entirely on the approved first
frame. *Mitigation:* the first frame is the strongest lever and applies
everywhere; short clips; per-shot re-roll; stylised art direction; and if
identity drift proves unacceptable at M0.5, that is precisely the argument for
spending premium on close-ups and leaving wides on Ken Burns.

**R3 — The catalogue lies.** Capabilities and prices are copied from docs that
change, and aggregator pricing differs from first-party. A wrong
`max_reference_images` or duration set produces submit-time failures.
*Mitigation:* M0.5 measures everything; `plan_motion` warns rather than assumes;
adapters surface provider validation errors verbatim; `refresh_measured_latency`
keeps latency honest from real generations; treat any provider-side 400 as a
catalogue bug and fix the data, not the call site.

**R4 — Cost.** Much lower than v2.0 but not zero, and a fan-out can still
surprise you. *Mitigation:* free preview as the default product; economy-first
`cheapest_capable`; premium behind `allow_premium`; `motion:plan` is free and
always shown; `confirm_cost_cents` handshake; explicit `only: [shot_ids]` on
fan-out; hard budget enforced before submit; `MotionTable` shows the total.

**R5 — Aggregator dependency.** Routing most models through one aggregator is a
single point of failure and a pricing middleman. *Mitigation:* the catalogue's
`adapter` field means moving a model to a direct adapter is a one-line data
change plus one adapter; keep at least one direct integration (Veo) live from
M6 so the second path is proven, not theoretical.

**R6 — Media retention windows.** Some providers delete generated media (Veo:
48h). An undownloaded clip is money gone. *Mitigation:* download is a step of
the generation job; `expires_at` stored; reaper re-enqueues at T−6h;
`clip_expired` is a distinct error code.

**R7 — Latency and quota opacity.** Per-provider concurrency limits are mostly
undocumented and discovered empirically. *Mitigation:* per-provider token
buckets sized from M0.5; quota errors are their own non-attempt-consuming class;
progress shown as elapsed time against measured p50.

**R8 — Narration and clip length disagree.** *Mitigation:* word budgets enforced
at generation time against authorial duration; live fit meter; measured-audio
fit check; freeze-frame tolerance of 1.5s; preflight blocking beyond that; and
"revert to Ken Burns" as an always-available free fix.

**R9 — Content-policy refusals.** Both image and video providers can refuse.
*Mitigation:* distinct non-retryable class surfaced with the exact prompt and a
one-click override; most providers do not bill refused generations.

**R10 — The renderer is the long pole.** *Mitigation:* M1, non-negotiable, and
it must handle both source kinds from the start.

**R11 — Storyboard materialisation is destructive.** Re-applying over rows
carrying approved stills, recorded audio, and paid clips destroys money.
*Mitigation:* `:apply` refuses with 409 when assets exist unless `force=true`;
the force path reports the dollar value of what would be orphaned; per-scene
regeneration is the normal path after first apply.

**R12 — Scope creep into a video editor.** *Mitigation:* §12.3, and anything off
the M0–M7 list goes into `docs/adr/` as a note until the film exists.

---

## 15. Simplifications applied

| Tempting design | Simplified to | Saved |
|---|---|---|
| One foundational video provider | A catalogue of interchangeable models behind `VideoPort` | Vendor lock-in in a market that reprices monthly |
| Provider constants in the domain schema | A capability model resolved at plan time | Migrations and schema churn per provider |
| A `video_models` database table | Typed catalogue in version-controlled code | A migration every time a model ships |
| Direct integrations with five vendors | One aggregator adapter + one direct + one shared task-API adapter | Weeks of auth, polling, and error-shape work |
| Animating every shot | Per-shot opt-in over a free Ken Burns baseline | Most of the money, and often a better cut |
| Provider-specific prompt builders | One composer + a small capability-driven dialect | Five diverging prompt code paths |
| Capability mismatches as errors | Warnings that name the consequence; blocking only on impossibility | A brittle UI that hides usable cheap models |
| Estimating cost inside the submit path | A free, pure `motion:plan` endpoint | Special-casing "preview the price" everywhere |
| Text-to-video | Image-to-video from an approved still | Blind spend and lost composition control |
| Retiming clips to fit audio | Freeze-frame tail padding | Visibly wrong slow-motion |
| A worker blocked on a provider poll | submit / poll / download as three deferred jobs | Blocked workers and clips lost on restart |
| Deleting the Ken Burns renderer | Keeping it as the default preview *and* final fallback | The whole free tier of the product |
| Generic DAG/workflow engine | 8 named job kinds + parent fan-out | A permanent debugging tax |
| Celery + chords + beat + flower | arq, three queues, four periodic tasks | Setup and conceptual overhead |
| WebSockets with a state protocol | SSE carrying invalidations only | A class of desync bugs |
| One JSONB storyboard as working state | Immutable blob + normalised rows | Painful partial updates and per-item status |
| Explicit `is_stale` flags | Derived `input_hash` comparison, chained still→clip | Constant flag-maintenance bugs |
| Fractional ordering | Integer `sort_order`, gaps of 1000 | Real complexity for a ≤20-item list |
| Vector DB / RAG over the story | Full story in context (~11K tokens) | An entire dependency |
| Real auth | Passphrase → signed cookie, single tenant | Days, for zero user-facing value |
| Hand-written TS types | Pydantic → JSON Schema → generated `.d.ts` | Silent drift front-to-back |

---

## 16. Open questions to settle before M2

1. **Run M0.5 first.** Every cost, latency, and capability number in §9.2 is a
   hypothesis. The bake-off is the cheapest week of the project.
2. **Which one workhorse model?** Decided by the contact sheet, weighted toward
   identity stability on `01-character-closeup.png`, not by price alone.
3. **Is premium worth a slot at all?** If reference-image conditioning visibly
   wins at M0.5, budget two or three premium shots; if not, drop the tier and
   simplify the UI.
4. **Art style** — pick one and freeze it. It must survive both a Ken Burns hold
   and generated motion; heavy fine texture and intricate line work drift more
   visibly than flat, graphic styles.
5. **Target length** — 10–14 scenes, one shot each, ~6s. The preview path makes
   length nearly free, so let the story decide, then choose what to animate.
6. **Image provider for stills** — now the highest-leverage provider choice in
   the system: the still is a shipped deliverable *and* the first frame.
7. **Aspect ratio** — decide before any still is generated; changing it
   invalidates every still and every clip.
