"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError, type ShotRow, type StillAsset, stillsApi,
} from "@/lib/api";
import { PromptInspector } from "./PromptInspector";

export function ShotGrid({ projectId, revision, onJob }: {
  projectId: string;
  revision: number;
  onJob: () => void;
}) {
  const [shots, setShots] = useState<ShotRow[] | null>(null);
  const [candidates, setCandidates] = useState<Record<string, StillAsset[]>>({});
  const [inspecting, setInspecting] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const uploadFor = useRef<string | null>(null);

  const load = useCallback(async () => {
    try { setShots((await stillsApi.shots(projectId)).items); }
    catch { setError("Could not load shots."); }
  }, [projectId]);

  useEffect(() => { void load(); }, [load, revision]);

  async function run(key: string, fn: () => Promise<unknown>) {
    setBusy(key); setError(null);
    try { await fn(); await load(); onJob(); }
    catch (e) { setError(e instanceof ApiError ? e.problem.detail : "Failed."); }
    finally { setBusy(null); }
  }

  async function showCandidates(shotId: string) {
    const items = (await stillsApi.candidates(shotId)).items;
    setCandidates((c) => ({ ...c, [shotId]: items }));
  }

  if (!shots) return <p className="sub">Loading shots…</p>;
  if (shots.length === 0) {
    return <div className="empty">Apply a storyboard to see its shots here.</div>;
  }

  const withStill = shots.filter((s) => s.still).length;
  const stale = shots.filter((s) => s.still && !s.still_fresh).length;

  return (
    <>
      <p className="muted small">
        {withStill}/{shots.length} shots have an approved still
        {stale > 0 && <> · <span className="over">{stale} stale</span></>}
      </p>
      {error && <p className="err">{error}</p>}

      <input
        ref={fileInput} type="file" accept="image/*" hidden
        onChange={async (e) => {
          const file = e.target.files?.[0];
          const shotId = uploadFor.current;
          e.target.value = "";
          if (file && shotId) {
            await run(`up-${shotId}`, () => stillsApi.upload(shotId, file));
          }
        }}
      />

      <div className="shots">
        {shots.map((s) => (
          <div key={s.id} className="shotcard">
            <div className="thumb">
              {s.still ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={s.still.url} alt={s.action} />
              ) : (
                <span className="muted small">no still yet</span>
              )}
              {s.still && !s.still_fresh && <span className="badge">stale</span>}
              {s.still?.source === "manual" && <span className="badge ok">yours</span>}
            </div>

            <div className="shotbody">
              <div className="row small muted">
                <span className="chip">{s.scene_index + 1}. {s.shot_type.replace(/_/g, " ")}</span>
                <span className="chip">{s.target_duration_s}s</span>
                {s.motion_priority === "high" && <span className="chip hot">motion</span>}
              </div>
              <p className="action">{s.action}</p>

              <div className="row">
                <button
                  disabled={busy !== null}
                  onClick={() => run(`gen-${s.id}`, async () => {
                    await stillsApi.generate(s.id, 2);
                  })}
                >
                  {busy === `gen-${s.id}` ? "Queued…"
                    : s.still ? "Regenerate" : "Generate"}
                </button>
                <button className="chip" onClick={() => showCandidates(s.id)}>
                  candidates
                </button>
                <button className="chip"
                        onClick={() => setInspecting(inspecting === s.id ? null : s.id)}>
                  prompt
                </button>
                <button className="chip" onClick={() => {
                  uploadFor.current = s.id;
                  fileInput.current?.click();
                }}>
                  upload
                </button>
              </div>

              {candidates[s.id] && (
                <div className="row candidates">
                  {candidates[s.id].map((a) => (
                    <button
                      key={a.id}
                      className={`cand ${a.selected ? "on" : ""}`}
                      title={a.selected ? "approved" : "approve this one"}
                      onClick={() => run(`sel-${a.id}`,
                        () => stillsApi.select(s.id, a.id))}
                    >
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img src={a.url} alt="candidate" />
                    </button>
                  ))}
                  {candidates[s.id].length === 0 && (
                    <span className="muted small">no candidates yet</span>
                  )}
                </div>
              )}

              {inspecting === s.id && (
                <PromptInspector shotId={s.id} onClose={() => setInspecting(null)} />
              )}
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
