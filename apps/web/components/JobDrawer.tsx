"use client";

import { type Job, jobsApi } from "@/lib/api";

const LABEL: Record<string, string> = {
  "story.analyze": "Reading the story",
  "storyboard.generate": "Directing the storyboard",
};

function money(cents: unknown) {
  return typeof cents === "number" ? ` · ${cents.toFixed(1)}¢` : "";
}

export function JobDrawer({ jobs, connected, onChange }: {
  jobs: Job[];
  connected: boolean;
  onChange: () => void;
}) {
  if (jobs.length === 0) return null;
  const recent = jobs.slice(0, 6);

  return (
    <aside className="jobs">
      <header>
        <span className={connected ? "dot live" : "dot"} />
        {connected ? "live" : "reconnecting…"}
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
