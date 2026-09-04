"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError, type Project, api, narrationApi } from "@/lib/api";
import { useEffect } from "react";

export function ProjectSettings({ project, onChange }: {
  project: Project;
  onChange: () => void;
}) {
  const router = useRouter();
  const [title, setTitle] = useState(project.title);
  const [budget, setBudget] = useState(project.budget_cents);
  const [voice, setVoice] = useState(project.narrator_voice_id ?? "");
  const [voices, setVoices] = useState<string[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    narrationApi.voices().then((v) => setVoices(v.items)).catch(() => {});
  }, []);

  async function run(key: string, fn: () => Promise<unknown>) {
    setBusy(key); setError(null);
    try { await fn(); onChange(); }
    catch (e) { setError(e instanceof ApiError ? e.problem.detail : "Failed."); }
    finally { setBusy(null); }
  }

  const dirty = title !== project.title ||
    budget !== project.budget_cents ||
    voice !== (project.narrator_voice_id ?? "");

  return (
    <div className="scene">
      {error && <p className="err">{error}</p>}
      <label className="field">
        <span className="muted small">title</span>
        <input value={title} onChange={(e) => setTitle(e.target.value)} />
      </label>

      <div className="row" style={{ gap: 14, marginTop: 10 }}>
        <label className="field">
          <span className="muted small">narrator voice</span>
          <select value={voice} onChange={(e) => setVoice(e.target.value)}>
            <option value="">default</option>
            {voices.map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
        </label>
        <label className="field">
          <span className="muted small">
            budget (¢) — {project.spent_cents}¢ spent
          </span>
          <input type="number" min={0} step={100} value={budget}
                 onChange={(e) => setBudget(Number(e.target.value))} />
        </label>
      </div>

      <div className="row between" style={{ marginTop: 14 }}>
        <button
          disabled={!dirty || busy !== null}
          onClick={() => run("save", () => api.updateProject(project.id, {
            title, budget_cents: budget,
            narrator_voice_id: voice || undefined,
          }))}
        >
          {busy === "save" ? "Saving…" : "Save settings"}
        </button>
        <button
          className="chip bad"
          disabled={busy !== null}
          onClick={() => run("delete", async () => {
            if (!confirm(
              `Delete "${project.title}"?\n\nThis removes the story, storyboard, ` +
              `every still, all narration and every render. It cannot be undone.`
            )) return;
            await api.deleteProject(project.id);
            router.replace("/projects");
          })}
        >
          delete project
        </button>
      </div>
    </div>
  );
}
