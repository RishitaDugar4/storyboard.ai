"use client";

import { useEffect, useState } from "react";
import { type PromptInspection, stillsApi } from "@/lib/api";

/** Colour by where the fragment came from, so a wrong image points at its
 *  cause: the style bible, a character's canon, or the shot's own action. */
const TONE: Record<string, string> = {
  style: "f-style", shot: "f-shot", action: "f-action",
  location: "f-location", light: "f-light", lighting: "f-light",
  palette: "f-palette", texture: "f-style", composition: "f-shot",
  override: "f-override",
};

const toneOf = (origin: string) =>
  origin.startsWith("character:") ? "f-character" : TONE[origin] ?? "";

export function PromptInspector({ shotId, onClose }: {
  shotId: string;
  onClose: () => void;
}) {
  const [data, setData] = useState<PromptInspection | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    stillsApi.prompt(shotId).then(setData).catch(() => setErr("Could not load."));
  }, [shotId]);

  return (
    <div className="inspector">
      <div className="row between">
        <strong>Composed prompt</strong>
        <button className="chip" onClick={onClose}>close</button>
      </div>

      {err && <p className="err">{err}</p>}
      {!data ? <p className="muted small">Loading…</p> : (
        <>
          <p className="prompt">
            {data.fragments.map((f, i) => (
              <span key={i} className={toneOf(f.origin)} title={f.origin}>
                {f.text}{" "}
              </span>
            ))}
          </p>

          <div className="row legend">
            {[...new Set(data.fragments.map((f) => f.origin))].map((o) => (
              <span key={o} className={`chip ${toneOf(o)}`}>{o}</span>
            ))}
          </div>

          <p className="muted small">
            <strong>excluded:</strong> {data.negative}
          </p>
          <p className="muted small">
            {data.model} · {data.size} · seed {data.seed} ·{" "}
            {data.estimated_cost_cents.toFixed(1)}¢ per image
            {data.would_reuse_cache && " · an identical still already exists (free)"}
          </p>
        </>
      )}
    </div>
  );
}
