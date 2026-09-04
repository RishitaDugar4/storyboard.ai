"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ApiError, type Project, api } from "@/lib/api";

export default function ProjectsPage() {
  const router = useRouter();
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setProjects((await api.listProjects()).items);
    } catch (err) {
      if (err instanceof ApiError && err.code === "unauthorized") {
        router.replace("/login");
        return;
      }
      setError("Could not load projects. Is the API running on :8000?");
    }
  }, [router]);

  useEffect(() => {
    void load();
  }, [load]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createProject(title.trim());
      setTitle("");
      await load();
    } catch {
      setError("Could not create the project.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <h1>Projects</h1>
      <p className="sub">Each project is one story on its way to a film.</p>

      <form onSubmit={create} className="row">
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="New project title"
        />
        <button type="submit" disabled={busy || !title.trim()}>
          {busy ? "Creating…" : "Create"}
        </button>
      </form>

      {error && <p className="err">{error}</p>}

      {projects === null ? (
        <p className="sub" style={{ marginTop: 24 }}>Loading…</p>
      ) : projects.length === 0 ? (
        <div className="empty" style={{ marginTop: 24 }}>
          No projects yet. Create one above.
        </div>
      ) : (
        <ul className="projects">
          {projects.map((p) => (
            <li key={p.id} onClick={() => router.push(`/projects/${p.id}`)}
                style={{ cursor: "pointer" }}>
              <span>{p.title}</span>
              <span className="stage">{p.stage.replace(/_/g, " ")}</span>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
