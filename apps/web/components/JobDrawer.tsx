"use client";

import { useEffect, useState } from "react";

import { type Job, jobsApi } from "@/lib/api";

const LABEL: Record<string, string> = {
  "story.analyze": "Reading the story",
  "storyboard.generate": "Directing the storyboard",
};

const STORAGE_KEY = "hbz.jobs.minimized";

function money(cents: unknown) {
  return typeof cents === "number" ? ` · ${cents.toFixed(1)}¢` : "";
}

function countActive(jobs: Job[]) {
  return jobs.filter((j) => j.status === "running" || j.status === "queued").length;
}

function summarize(jobs: Job[]) {
  const active = countActive(jobs);
  const failed = jobs.filter((j) => j.status === "failed").length;
  if (active > 0) return `${active} running`;
  if (failed > 0) return `${failed} failed`;
  return `${jobs.length} recent`;
}

export function JobDrawer({ jobs, connected, onChange }: {
  jobs: Job[];
  connected: boolean;
  onChange: () => void;
}) {
  const [minimized, setMinimized] = useState(false);

  // Read after mount, not in the initial state: the server renders this too,
  // and a value only the browser has would make the first paint disagree with
  // the markup React hydrates against.
  useEffect(() => {
    try {
      setMinimized(window.localStorage.getItem(STORAGE_KEY) === "1");
    } catch {
      // Private browsing and blocked site data both throw here. The drawer
      // opening is a fine thing to fall back to.
    }
  }, []);

  function toggle() {
    const next = !minimized;
    setMinimized(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next ? "1" : "0");
    } catch {
      // Not persisting the preference is survivable; refusing to collapse
      // because we could not write it down is not.
    }
  }

  if (jobs.length === 0) return null;
  const recent = jobs.slice(0, 6);
  const failed = jobs.some((j) => j.status === "failed");
  // Published on the element rather than inferred from the rendered rows,
  // because collapsing removes those rows -- and anything watching the queue
  // through the DOM would read an empty drawer as a quiet one.
  const active = countActive(jobs);

  if (minimized) {
    return (
      <aside className="jobs minimized" data-active={active}>
        <button
          type="button"
          className={failed ? "jobs-pill has-failure" : "jobs-pill"}
          onClick={toggle}
          aria-expanded={false}
          title="Show jobs"
        >
          <span className={connected ? "dot live" : "dot"} />
          {summarize(jobs)}
        </button>
      </aside>
    );
  }

  return (
    <aside className="jobs" data-active={active}>
      <header>
        <span className={connected ? "dot live" : "dot"} />
        {connected ? "live" : "reconnecting…"}
        <button
          type="button"
          className="jobs-toggle"
          onClick={toggle}
          aria-expanded
          aria-label="Minimize jobs"
          title="Minimize"
        >
          –
        </button>
      </header>
      {recent.map((j) => (
        <div key={j.id} className={`job ${j.status}`}>
          <div className="job-row">
            <span>{LABEL[j.kind] ?? j.kind}</span>
            <span className="muted">{j.status}</span>
          </div>

          {(j.status === "running" || j.status === "queued") && (
            <div className="bar"><i style={{ width: `${j.progress}%` }} /></div>
          )}

          <div className="muted small">
            {j.message}
            {j.result ? money((j.result as Record<string, unknown>).cost_cents) : ""}
          </div>

          {j.status === "failed" && (
            <div className="job-error">
              <code>{j.error_code}</code>
              <p>{j.error_detail?.slice(0, 240)}</p>
              <button onClick={async () => { await jobsApi.retry(j.id); onChange(); }}>
                Retry
              </button>
            </div>
          )}
        </div>
      ))}
    </aside>
  );
}
