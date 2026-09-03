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
  login: (passphrase: string) =>
    request<void>("/api/v1/auth/session", {
      method: "POST", body: JSON.stringify({ passphrase }),
    }),
  logout: () => request<void>("/api/v1/auth/session", { method: "DELETE" }),
  me: () => request<{ id: string; email: string; display_name: string }>("/api/v1/me"),
  listProjects: () =>
    request<{ items: Project[]; total: number }>("/api/v1/projects"),
  createProject: (title: string) =>
    request<Project>("/api/v1/projects", {
      method: "POST", body: JSON.stringify({ title }),
    }),
};
