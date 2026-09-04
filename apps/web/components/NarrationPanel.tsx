"use client";

import { useCallback, useEffect, useState } from "react";
import { useRef } from "react";
import {
  ApiError, type MusicState, type NarrationShot, type Preflight,
  type RenderRow, musicApi, narrationApi, rendersApi,
} from "@/lib/api";

const FIT_TONE: Record<string, string> = {
  fits: "ok", tight: "warn", overflow: "bad", unknown: "muted",
};

export function NarrationPanel({ projectId, revision, onJob }: {
  projectId: string;
  revision: number;
  onJob: () => void;
}) {
  const [shots, setShots] = useState<NarrationShot[] | null>(null);
  const [renders, setRenders] = useState<RenderRow[]>([]);
  const [pre, setPre] = useState<Preflight | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [watchingId, setWatchingId] = useState<string | null>(null);
  const [music, setMusic] = useState<MusicState | null>(null);
  const musicInput = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const [n, r, m] = await Promise.all([
        narrationApi.list(projectId),
        rendersApi.list(projectId).catch(() => ({ items: [], total: 0 })),
        musicApi.get(projectId).catch(() => null),
      ]);
      setShots(n.items);
      setRenders(r.items);
      setMusic(m);
    } catch { setError("Could not load narration."); }
  }, [projectId]);

  useEffect(() => { void load(); }, [load, revision]);

  async function run(key: string, fn: () => Promise<unknown>) {
    setBusy(key); setError(null);
    try { await fn(); await load(); onJob(); }
    catch (e) { setError(e instanceof ApiError ? e.problem.detail : "Failed."); }
    finally { setBusy(null); }
  }

  if (!shots) return <p className="sub">Loading…</p>;
  if (shots.length === 0) {
    return <div className="empty">Apply a storyboard first.</div>;
  }

  const recorded = shots.flatMap((s) => s.lines).filter((l) => l.duration_ms).length;
  const total = shots.flatMap((s) => s.lines).length;
  const blocked = shots.filter((s) => s.fit.blocks_render).length;
  const latest = renders[0];
  const playable = renders.filter((r) => r.video_url);
  const watching = playable.find((r) => r.id === watchingId) ?? playable[0];

  return (
    <>
      <div className="row between">
        <span className="muted small">
          {recorded}/{total} lines recorded
          {blocked > 0 && <> · <span className="over">{blocked} too long to render</span></>}
        </span>
        <div className="row">
          <button
            disabled={busy !== null}
            onClick={() => run("all", () => narrationApi.generateAll(projectId))}
          >
            {busy === "all" ? "Queued…" : "Record all"}
          </button>
          <button
            disabled={busy !== null}
            onClick={() => run("pre", async () => setPre(
              await rendersApi.preflight(projectId)))}
            className="chip"
          >
            check
          </button>
          <button
            disabled={busy !== null || recorded === 0}
            onClick={() => run("render", () => rendersApi.create(projectId))}
          >
            {busy === "render" ? "Rendering…" : "Render preview"}
          </button>
        </div>
      </div>

      {error && <p className="err">{error}</p>}

      <div className="row between music" data-testid="music">
        <span className="muted small">
          {music?.attached
            ? `music: ${music.filename ?? "attached"} — mixed 24dB under the narration`
            : "no music bed — the film will play with narration only"}
        </span>
        <div className="row">
          {music?.url && (
            // eslint-disable-next-line jsx-a11y/media-has-caption
            <audio src={music.url} controls style={{ height: 30 }} />
          )}
          <input
            ref={musicInput} type="file" accept="audio/*" hidden
            onChange={async (e) => {
              const f = e.target.files?.[0];
              e.target.value = "";
              if (f) await run("music", () => musicApi.upload(projectId, f));
            }}
          />
          <button className="chip" disabled={busy !== null}
                  onClick={() => musicInput.current?.click()}>
            {music?.attached ? "replace music" : "add music"}
          </button>
          {music?.attached && (
            <button className="chip" disabled={busy !== null}
                    onClick={() => run("music", () => musicApi.remove(projectId))}>
              remove
            </button>
          )}
        </div>
      </div>

      {pre && (
        <div className={`board ${pre.ok ? "" : "bad-border"}`} style={{ marginTop: 12 }}>
          <strong>{pre.ok ? "Ready to render" : "Not ready"}</strong>
          <p className="muted small">
            {pre.clips} shots · {((pre.duration_ms ?? 0) / 1000).toFixed(0)}s
          </p>
          {[...pre.blocking, ...pre.advisory].map((p, i) => (
            <p key={i} className={`small ${pre.blocking.includes(p) ? "over" : "muted"}`}>
              {p.message}
            </p>
          ))}
        </div>
      )}

      {watching?.video_url && (
        <div className="board" style={{ marginTop: 14 }} data-testid="player">
          <div className="row between">
            <strong>{watching === latest ? "Latest render" : "Earlier render"}</strong>
            <span className="muted small">
              {((watching.duration_ms ?? 0) / 1000).toFixed(1)}s ·{" "}
              {watching.clips} shots ·{" "}
              {new Date(watching.created_at).toLocaleString()}
            </span>
          </div>
          {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
          <video key={watching.id} src={watching.video_url}
                 poster={watching.poster_url ?? undefined} controls
                 style={{ width: "100%", borderRadius: 10, marginTop: 8 }} />
          <a href={watching.video_url}
             download={`${watching.profile}-${watching.id.slice(0, 8)}.mp4`}
             className="chip" style={{ display: "inline-block", marginTop: 10 }}>
            save to your computer
          </a>
        </div>
      )}
      {latest && latest.status === "failed" && (
        <p className="err">Render failed: {latest.error?.slice(0, 200)}</p>
      )}

      {renders.length > 1 && (
        <div style={{ marginTop: 12 }}>
          <p className="muted small">
            {renders.length} renders — every version is kept, so you can compare
            a change against what it replaced.
          </p>
          <div className="row" style={{ gap: 8 }} data-testid="render-history">
            {renders.map((r) => (
              <button
                key={r.id}
                className={`chip ${r.id === watchingId ? "primary" : ""}`}
                disabled={!r.video_url}
                onClick={() => setWatchingId(r.id)}
                title={new Date(r.created_at).toLocaleString()}
              >
                {r.profile} · {((r.duration_ms ?? 0) / 1000).toFixed(0)}s
                {r.status !== "succeeded" && ` · ${r.status}`}
              </button>
            ))}
          </div>
        </div>
      )}

      <div style={{ marginTop: 18 }}>
        {shots.map((s) => (
          <div key={s.shot_id} className="scene">
            <div className="row between">
              <strong>{s.scene_index + 1}. {s.scene_title}</strong>
              <span className={`chip ${FIT_TONE[s.fit.status]}`}
                    title={s.fit.message}>
                {s.fit.status} · {s.fit.words}/{s.fit.word_budget}w
              </span>
            </div>
            {s.fit.status !== "fits" && (
              <p className={`small ${s.fit.blocks_render ? "over" : "muted"}`}>
                {s.fit.message}
              </p>
            )}

            {s.lines.map((l) => (
              <div key={l.id} className="line">
                <textarea
                  rows={2}
                  value={draft[l.id] ?? l.text}
                  onChange={(e) => setDraft((d) => ({ ...d, [l.id]: e.target.value }))}
                />
                <div className="row between">
                  <span className="muted small">
                    {l.speaker} · {l.delivery}
                    {l.duration_ms
                      ? ` · ${(l.duration_ms / 1000).toFixed(1)}s`
                      : " · not recorded"}
                    {l.duration_ms && !l.fresh && (
                      <span className="over"> · stale</span>
                    )}
                  </span>
                  <div className="row">
                    {l.audio_url && (
                      // eslint-disable-next-line jsx-a11y/media-has-caption
                      <audio src={l.audio_url} controls style={{ height: 30 }} />
                    )}
                    {draft[l.id] !== undefined && draft[l.id] !== l.text && (
                      <button className="chip primary"
                              onClick={() => run(`save-${l.id}`, async () => {
                                await narrationApi.patch(l.id, { text: draft[l.id] });
                                setDraft((d) => { const n = { ...d }; delete n[l.id]; return n; });
                              })}>
                        save
                      </button>
                    )}
                    <button className="chip" disabled={busy !== null}
                            onClick={() => run(`rec-${l.id}`,
                              () => narrationApi.generate(l.id))}>
                      {l.duration_ms ? "re-record" : "record"}
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>
    </>
  );
}
