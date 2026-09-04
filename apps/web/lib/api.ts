/** Thin typed fetch wrapper. Every call is same-origin and carries the cookie. */

export type ProjectStage =
  | "draft" | "analyzed" | "storyboarded" | "characters_locked"
  | "stills" | "narration" | "previewed" | "motion" | "rendered";

export interface Project {
  id: string;
  title: string;
  stage: ProjectStage;
  aspect_ratio: string;
  image_size: string;
  style_preset: string;
  allow_premium: boolean;
  narrator_voice_id: string | null;
  budget_cents: number;
  spent_cents: number;
  share_token: string | null;
  created_at: string;
  updated_at: string;
}

/** RFC 9457 problem+json, as emitted by the API. */
export interface ProblemDetail {
  title: string;
  status: number;
  detail: string;
  code: string;
}

export class ApiError extends Error {
  constructor(readonly problem: ProblemDetail) {
    super(problem.detail || problem.title);
  }
  get code() {
    return this.problem.code;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...init,
    credentials: "include",
    headers: { "content-type": "application/json", ...(init.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    let problem: ProblemDetail = {
      title: "Request failed", status: res.status,
      detail: res.statusText, code: `http_${res.status}`,
    };
    try { problem = { ...problem, ...(await res.json()) }; } catch { /* non-JSON */ }
    throw new ApiError(problem);
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

export const api = {
  login: (email: string, passphrase: string) =>
    request<void>("/api/v1/auth/session", {
      method: "POST", body: JSON.stringify({ email, passphrase }),
    }),
  logout: () => request<void>("/api/v1/auth/session", { method: "DELETE" }),
  me: () => request<{ id: string; email: string; display_name: string }>("/api/v1/me"),
  listProjects: () =>
    request<{ items: Project[]; total: number }>("/api/v1/projects"),
  getProject: (id: string) => request<Project>(`/api/v1/projects/${id}`),
  createProject: (title: string) =>
    request<Project>("/api/v1/projects", {
      method: "POST", body: JSON.stringify({ title }),
    }),
  updateProject: (id: string, changes: Partial<Pick<Project,
      "title" | "style_preset" | "budget_cents" | "allow_premium">> &
      { narrator_voice_id?: string }) =>
    request<Project>(`/api/v1/projects/${id}`, {
      method: "PATCH", body: JSON.stringify(changes),
    }),
  deleteProject: (id: string) =>
    request<void>(`/api/v1/projects/${id}`, { method: "DELETE" }),
};

// ---------------------------------------------------------------------------
// Story, storyboard, and jobs
// ---------------------------------------------------------------------------

export type JobStatus =
  | "queued" | "running" | "awaiting_provider"
  | "succeeded" | "failed" | "cancelled" | "skipped";

export const ACTIVE_JOB_STATUSES: JobStatus[] = [
  "queued", "running", "awaiting_provider",
];

export interface Job {
  id: string;
  kind: string;
  status: JobStatus;
  progress: number;
  message: string;
  attempt: number;
  target_type: string | null;
  target_id: string | null;
  error_code: string | null;
  error_detail: string | null;
  result: Record<string, unknown> | null;
  queued_at: string | null;
  finished_at: string | null;
}

export interface JobAccepted {
  job_id: string;
  kind: string;
  status: JobStatus;
  /** False means identical work was already queued — this is that job. */
  created: boolean;
}

export interface Story {
  id: string;
  version: number;
  raw_text: string;
  word_count: number;
  created_at: string;
}

export interface StoryboardSummary {
  id: string;
  version: number;
  model: string;
  repaired: boolean;
  applied_at: string | null;
  scenes: number;
  created_at: string;
}

export interface StoryboardScene {
  local_index: number;
  title: string;
  summary: string;
  mood: string;
  time_of_day: string;
  shots: {
    local_index: number;
    shot_type: string;
    camera_move: string;
    action: string;
    subject_motion: string;
    motion_priority: "low" | "medium" | "high";
    target_duration_s: number;
    subject_slugs: string[];
  }[];
  narration: { local_index: number; speaker: string; text: string }[];
}

export interface StoryboardDocument {
  title: string;
  logline: string;
  style_bible: {
    art_style: string; palette: string[]; lighting: string;
    camera_language: string; motion_language: string;
  };
  characters: {
    slug: string; name: string; role: string; age_impression: string;
    hair: string; eyes: string; skin: string; build: string;
    default_wardrobe: string; distinguishing_features: string[];
  }[];
  locations: { slug: string; name: string; description: string }[];
  scenes: StoryboardScene[];
}

const p = (id: string) => `/api/v1/projects/${id}`;

export const storyApi = {
  get: (pid: string) => request<Story>(`${p(pid)}/story`),
  put: (pid: string, raw_text: string) =>
    request<Story>(`${p(pid)}/story`, {
      method: "PUT", body: JSON.stringify({ raw_text }),
    }),
  analyze: (pid: string) =>
    request<JobAccepted>(`${p(pid)}/story:analyze`, { method: "POST" }),
  analysis: (pid: string) =>
    request<{ id: string; document: Record<string, unknown> }>(`${p(pid)}/analysis`),
};

export const storyboardApi = {
  generate: (pid: string, target_length_s: number, notes = "") =>
    request<JobAccepted>(`${p(pid)}/storyboard:generate`, {
      method: "POST", body: JSON.stringify({ target_length_s, notes }),
    }),
  list: (pid: string) =>
    request<{ items: StoryboardSummary[]; total: number }>(`${p(pid)}/storyboards`),
  get: (pid: string, sbid: string) =>
    request<{ id: string; version: number; document: StoryboardDocument }>(
      `${p(pid)}/storyboards/${sbid}`),
  apply: (pid: string, sbid: string, force = false) =>
    request<{ applied: boolean; scenes: number; shots: number;
              narration_lines: number; characters: number }>(
      `${p(pid)}/storyboards/${sbid}:apply`,
      { method: "POST", body: JSON.stringify({ force }) }),
};

export const jobsApi = {
  get: (id: string) => request<Job>(`/api/v1/jobs/${id}`),
  list: (pid: string, status?: "active") =>
    request<{ items: Job[]; total: number }>(
      `${p(pid)}/jobs${status ? `?status=${status}` : ""}`),
  retry: (id: string) =>
    request<{ requeued: boolean }>(`/api/v1/jobs/${id}:retry`, { method: "POST" }),
  cancel: (id: string) =>
    request<{ cancelled: boolean }>(`/api/v1/jobs/${id}:cancel`, { method: "POST" }),
};

// ---------------------------------------------------------------------------
// Characters and stills
// ---------------------------------------------------------------------------

export interface MusicState {
  attached: boolean;
  url: string | null;
  filename: string | null;
  bytes?: number | null;
}

export interface Character {
  id: string;
  slug: string;
  name: string;
  role: string;
  appearance: Record<string, unknown>;
  /** The frozen canon embedded verbatim in every prompt this character is in. */
  appearance_prompt: string;
  voice: Record<string, unknown>;
  seed: number;
  locked: boolean;
  locked_at: string | null;
  reference_asset_id: string | null;
  /** Narration lines whose speaker is this character speak in this voice. */
  voice_name: string | null;
  spoken_lines: number;
  unlock_impact?: {
    shots: number;
    stills: number;
    estimated_recost_cents: number;
  };
}

export interface StillAsset {
  id: string;
  url: string;
  width: number | null;
  height: number | null;
  source: "generated" | "manual" | "derived";
  provider: string | null;
  model: string | null;
  cost_cents: number;
  selected: boolean;
  created_at: string;
}

export interface ShotRow {
  id: string;
  scene_title: string;
  scene_index: number;
  shot_type: string;
  camera_move: string;
  action: string;
  subject_slugs: string[];
  motion_priority: "low" | "medium" | "high";
  target_duration_s: number;
  motion_mode: "kenburns" | "generated" | "manual";
  still: StillAsset | null;
  /** Derived server-side: false means the prompt changed since this was made. */
  still_fresh: boolean;
}

export interface PromptInspection {
  positive: string;
  negative: string;
  fragments: { origin: string; text: string }[];
  size: string;
  seed: number;
  model: string;
  input_hash: string;
  characters: string[];
  estimated_cost_cents: number;
  current_hash: string | null;
  would_reuse_cache: boolean;
}

export interface ShotEdit {
  action?: string;
  composition_note?: string;
  camera_move?: string;
  subject_motion?: string;
  target_duration_s?: number;
  motion_priority?: "low" | "medium" | "high";
  subject_slugs?: string[];
}

export const charactersApi = {
  list: (pid: string) =>
    request<{ items: Character[]; total: number }>(`${p(pid)}/characters`),
  patch: (cid: string, changes: Partial<Pick<Character, "name" | "role">> &
                                { appearance?: Record<string, unknown>;
                                  voice?: { voice_name: string } }) =>
    request<Character>(`/api/v1/characters/${cid}`, {
      method: "PATCH", body: JSON.stringify(changes),
    }),
  lock: (cid: string) =>
    request<Character>(`/api/v1/characters/${cid}:lock`, { method: "POST" }),
  unlock: (cid: string) =>
    request<Character & { invalidated: { shots: number; stills: number;
                                         estimated_recost_cents: number } }>(
      `/api/v1/characters/${cid}:unlock`, { method: "POST" }),
};

export const stillsApi = {
  patchShot: (shotId: string, changes: ShotEdit) =>
    request<ShotRow & { still_fresh: boolean }>(`/api/v1/shots/${shotId}`, {
      method: "PATCH", body: JSON.stringify(changes),
    }),
  shots: (pid: string) =>
    request<{ items: ShotRow[]; total: number }>(`${p(pid)}/shots`),
  prompt: (shotId: string) =>
    request<PromptInspection>(`/api/v1/shots/${shotId}/prompt`),
  candidates: (shotId: string) =>
    request<{ items: StillAsset[] }>(`/api/v1/shots/${shotId}/images`),
  generate: (shotId: string, n = 2) =>
    request<JobAccepted>(`/api/v1/shots/${shotId}/image:generate`, {
      method: "POST", body: JSON.stringify({ n }),
    }),
  select: (shotId: string, assetId: string) =>
    request<{ selected: boolean; asset: StillAsset }>(
      `/api/v1/shots/${shotId}/image:select`,
      { method: "POST", body: JSON.stringify({ asset_id: assetId }) }),
  upload: async (shotId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`/api/v1/shots/${shotId}/image:upload`, {
      method: "POST", body: form, credentials: "include",
    });
    if (!res.ok) {
      let problem: ProblemDetail = {
        title: "Upload failed", status: res.status,
        detail: res.statusText, code: `http_${res.status}`,
      };
      try { problem = { ...problem, ...(await res.json()) }; } catch { /* */ }
      throw new ApiError(problem);
    }
    return (await res.json()) as { uploaded: boolean; asset: StillAsset };
  },
};

// ---------------------------------------------------------------------------
// Narration and renders
// ---------------------------------------------------------------------------

export type FitStatus = "fits" | "tight" | "overflow" | "unknown";

export interface NarrationLine {
  id: string;
  text: string;
  speaker: string;
  delivery: string;
  /** Measured from the rendered audio, not estimated. */
  duration_ms: number | null;
  audio_url: string | null;
  fresh: boolean;
}

export interface NarrationShot {
  shot_id: string;
  scene_title: string;
  scene_index: number;
  target_duration_s: number;
  fit: {
    status: FitStatus;
    message: string;
    words: number;
    word_budget: number;
    slack_ms: number;
    tail_freeze_ms: number;
    blocks_render: boolean;
  };
  lines: NarrationLine[];
}

export interface RenderRow {
  id: string;
  profile: "preview" | "final";
  status: string;
  duration_ms: number | null;
  error: string | null;
  created_at: string;
  video_url: string | null;
  poster_url: string | null;
  clips: number;
}

export interface Preflight {
  ok: boolean;
  blocking: { code: string; message: string; shot_id: string | null }[];
  advisory: { code: string; message: string; shot_id: string | null }[];
  duration_ms: number | null;
  clips: number;
}

export const narrationApi = {
  list: (pid: string) =>
    request<{ items: NarrationShot[]; total: number }>(`${p(pid)}/narration`),
  voices: () => request<{ items: string[]; model: string }>("/api/v1/voices"),
  patch: (lineId: string, changes: { text?: string; delivery?: string }) =>
    request<{ id: string; text: string }>(`/api/v1/narration-lines/${lineId}`, {
      method: "PATCH", body: JSON.stringify(changes),
    }),
  generate: (lineId: string) =>
    request<JobAccepted>(`/api/v1/narration-lines/${lineId}/audio:generate`,
      { method: "POST" }),
  generateAll: (pid: string) =>
    request<{ queued: number; lines: number }>(
      `${p(pid)}/narration:generate_all`, { method: "POST" }),
};

export const musicApi = {
  get: (pid: string) => request<MusicState>(`${p(pid)}/music`),
  remove: (pid: string) =>
    request<MusicState>(`${p(pid)}/music`, { method: "DELETE" }),
  upload: async (pid: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${p(pid)}/music`, {
      method: "POST", body: form, credentials: "include",
    });
    if (!res.ok) {
      let problem: ProblemDetail = {
        title: "Upload failed", status: res.status,
        detail: res.statusText, code: `http_${res.status}`,
      };
      try { problem = { ...problem, ...(await res.json()) }; } catch { /* */ }
      throw new ApiError(problem);
    }
    return (await res.json()) as { attached: boolean; filename: string };
  },
};

export const rendersApi = {
  preflight: (pid: string, profile: "preview" | "final" = "preview") =>
    request<Preflight>(`${p(pid)}/preflight`, {
      method: "POST", body: JSON.stringify({ profile }),
    }),
  create: (pid: string, profile: "preview" | "final" = "preview") =>
    request<JobAccepted>(`${p(pid)}/renders`, {
      method: "POST", body: JSON.stringify({ profile }),
    }),
  list: (pid: string) =>
    request<{ items: RenderRow[]; total: number }>(`${p(pid)}/renders`),
};
