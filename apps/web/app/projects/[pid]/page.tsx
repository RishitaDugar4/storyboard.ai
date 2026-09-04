"use client";

import { use, useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ApiError, type Character, type Project, type Story,
  type StoryboardDocument, type StoryboardSummary, api, charactersApi,
  storyApi, storyboardApi,
} from "@/lib/api";
import { useProjectEvents } from "@/lib/hooks/useProjectEvents";
import { JobDrawer } from "@/components/JobDrawer";
import { StoryboardView } from "@/components/StoryboardView";
import { CharacterPanel } from "@/components/CharacterPanel";
import { ShotGrid } from "@/components/ShotGrid";
import { NarrationPanel } from "@/components/NarrationPanel";
import { ProjectSettings } from "@/components/ProjectSettings";

const STAGES = ["draft", "analyzed", "storyboarded", "characters_locked",
                "stills", "narration", "previewed", "motion", "rendered"];

export default function ProjectPage({ params }: { params: Promise<{ pid: string }> }) {
  const { pid } = use(params);
  const router = useRouter();

  const [project, setProject] = useState<Project | null>(null);
  const [story, setStory] = useState<Story | null>(null);
  const [draft, setDraft] = useState("");
  const [boards, setBoards] = useState<StoryboardSummary[]>([]);
  const [doc, setDoc] = useState<StoryboardDocument | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [length, setLength] = useState(90);
  const [characters, setCharacters] = useState<Character[]>([]);
  const [tab, setTab] = useState<"story" | "cast" | "stills" | "film" | "settings">("story");

  const { jobs, activeJobs, connected, revision, refresh } = useProjectEvents(pid);

  const load = useCallback(async () => {
    try {
      const [p, list, cast] = await Promise.all([
        api.getProject(pid),
        storyboardApi.list(pid).catch(() => ({ items: [], total: 0 })),
        charactersApi.list(pid).catch(() => ({ items: [], total: 0 })),
      ]);
      setProject(p);
      setBoards(list.items);
      setCharacters(cast.items);
      try {
        const s = await storyApi.get(pid);
        setStory(s);
        setDraft((d) => (d ? d : s.raw_text));
      } catch { /* no story saved yet */ }
      if (list.items.length) {
        setDoc((await storyboardApi.get(pid, list.items[0].id)).document);
      }
    } catch (err) {
      if (err instanceof ApiError && err.code === "unauthorized") {
        router.replace("/login");
        return;
      }
      setError("Could not load this project.");
    }
  }, [pid, router]);

  useEffect(() => { void load(); }, [load, revision]);

  async function act(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.problem.detail : "Something failed.");
    } finally {
      setBusy(null);
    }
  }

  if (!project) {
    return <main><p className="sub">{error ?? "Loading…"}</p></main>;
  }

  const stageIndex = STAGES.indexOf(project.stage);
  const words = draft.trim() ? draft.trim().split(/\s+/).length : 0;
  const dirty = draft.trim() !== (story?.raw_text ?? "").trim();
  const working = activeJobs.length > 0;

  return (
    <main className="wide">
      <h1>{project.title}</h1>
      <p className="sub">{project.stage.replace(/_/g, " ")}</p>

      <ol className="stepper">
        {STAGES.slice(0, 5).map((s, i) => (
          <li key={s} className={i <= stageIndex ? "done" : ""}>
            {s.replace(/_/g, " ")}
          </li>
        ))}
      </ol>

      <div className="row tabs">
        {(["story", "cast", "stills", "film", "settings"] as const).map((t) => (
          <button key={t} className={`chip ${tab === t ? "primary" : ""}`}
                  onClick={() => setTab(t)}>
            {t}
            {t === "cast" && characters.length > 0 && ` (${characters.length})`}
          </button>
        ))}
        <span className="muted small">
          {project.spent_cents}¢ of {project.budget_cents}¢ spent
        </span>
      </div>

      {error && <p className="err">{error}</p>}

      <section hidden={tab !== "story"}>
        <h2>Story</h2>
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Paste the story here."
          rows={12}
        />
        <div className="row between">
          <span className="muted small">
            {words} words{story ? ` · saved v${story.version}` : " · unsaved"}
          </span>
          <div className="row">
            <button
              disabled={!dirty || !draft.trim() || busy !== null}
              onClick={() => act("save", async () => {
                setStory(await storyApi.put(pid, draft));
              })}
            >
              {busy === "save" ? "Saving…" : "Save"}
            </button>
            <button
              disabled={!story || dirty || working || busy !== null}
              onClick={() => act("analyze", () => storyApi.analyze(pid))}
              title={dirty ? "Save the story first" : undefined}
            >
              Analyse
            </button>
          </div>
        </div>
      </section>

      <section hidden={tab !== "story"}>
        <div className="row between">
          <h2>Storyboard</h2>
          <div className="row">
            <label className="muted small">
              length{" "}
              <input
                type="number" min={20} max={600} step={10} value={length}
                onChange={(e) => setLength(Number(e.target.value))}
                style={{ width: 74 }}
              />s
            </label>
            <button
              disabled={stageIndex < 1 || working || busy !== null}
              onClick={() => act("generate", () =>
                storyboardApi.generate(pid, length))}
              title={stageIndex < 1 ? "Analyse the story first" : undefined}
            >
              Generate
            </button>
          </div>
        </div>

        {boards.length === 0 ? (
          <div className="empty">
            No storyboard yet. Save a story, analyse it, then generate.
          </div>
        ) : (
          <>
            <div className="row muted small" style={{ marginBottom: 10 }}>
              {boards.map((b) => (
                <button
                  key={b.id} className="chip"
                  onClick={() => act("open", async () => {
                    setDoc((await storyboardApi.get(pid, b.id)).document);
                  })}
                >
                  v{b.version} · {b.scenes} scenes
                  {b.applied_at ? " · applied" : ""}
                </button>
              ))}
              <button
                className="chip primary"
                disabled={busy !== null}
                onClick={() => act("apply", async () => {
                  try {
                    await storyboardApi.apply(pid, boards[0].id);
                  } catch (err) {
                    // Applying over curated work is refused; the message names
                    // exactly what would be lost.
                    if (err instanceof ApiError &&
                        err.code === "apply_would_destroy_work" &&
                        confirm(`${err.problem.detail}\n\nOverwrite anyway?`)) {
                      await storyboardApi.apply(pid, boards[0].id, true);
                    } else { throw err; }
                  }
                })}
              >
                Apply v{boards[0].version} to the workspace
              </button>
            </div>
            {doc && <StoryboardView doc={doc} />}
          </>
        )}
      </section>

      <section hidden={tab !== "cast"}>
        <h2>Cast</h2>
        <p className="sub">
          Locking freezes a character&apos;s description into the exact words
          every prompt will use. Lock before generating stills.
        </p>
        <CharacterPanel characters={characters} onChange={load} />
      </section>

      <section hidden={tab !== "stills"}>
        <h2>Stills</h2>
        <p className="sub">
          The approved still is the shot&apos;s picture — and the first frame if
          you animate it later. Nothing here is throwaway.
        </p>
        <ShotGrid projectId={pid} revision={revision} onJob={refresh} />
      </section>

      <section hidden={tab !== "film"}>
        <h2>Narration &amp; preview</h2>
        <p className="sub">
          Recorded audio sets each shot&apos;s screen time — the picture is
          padded to fit the voice, never the other way round.
        </p>
        <NarrationPanel projectId={pid} revision={revision} onJob={refresh} />
      </section>

      <section hidden={tab !== "settings"}>
        <h2>Settings</h2>
        <ProjectSettings project={project} onChange={load} />
      </section>

      <JobDrawer jobs={jobs} connected={connected} onChange={refresh} />
    </main>
  );
}
