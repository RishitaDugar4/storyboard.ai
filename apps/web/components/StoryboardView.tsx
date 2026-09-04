"use client";

import { useState } from "react";
import type { StoryboardDocument } from "@/lib/api";

const WORDS_PER_SECOND = 2.5;
const PAD_S = 0.9;

/** Mirrors app/ai/pacing.py. If these ever disagree the UI lies about fit. */
const wordBudget = (s: number) => Math.max(0, Math.floor((s - PAD_S) * WORDS_PER_SECOND));

export function StoryboardView({ doc }: { doc: StoryboardDocument }) {
  const [tab, setTab] = useState<"scenes" | "cast" | "style">("scenes");

  const runtime = doc.scenes.reduce(
    (t, s) => t + s.shots.reduce((u, sh) => u + sh.target_duration_s, 0), 0);
  const shots = doc.scenes.reduce((t, s) => t + s.shots.length, 0);
  const high = doc.scenes.reduce(
    (t, s) => t + s.shots.filter((sh) => sh.motion_priority === "high").length, 0);

  return (
    <div className="board">
      <p className="logline">{doc.logline}</p>
      <p className="muted small">
        {doc.scenes.length} scenes · {shots} shots · {Math.round(runtime)}s ·{" "}
        <strong>{high}</strong> worth animating
      </p>

      <div className="row tabs">
        {(["scenes", "cast", "style"] as const).map((t) => (
          <button key={t} className={`chip ${tab === t ? "primary" : ""}`}
                  onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>

      {tab === "scenes" && doc.scenes.map((sc) => (
        <div key={sc.local_index} className="scene">
          <div className="row between">
            <strong>{sc.local_index + 1}. {sc.title}</strong>
            <span className="muted small">{sc.time_of_day} · {sc.mood}</span>
          </div>
          {sc.shots.map((sh) => {
            const used = sc.narration
              .filter((n) => n.text)
              .reduce((t, n) => t + n.text.split(/\s+/).length, 0);
            const budget = wordBudget(sh.target_duration_s);
            return (
              <div key={sh.local_index} className="shot">
                <div className="row small muted">
                  <span className="chip">{sh.shot_type.replace(/_/g, " ")}</span>
                  <span className="chip">{sh.camera_move.replace(/_/g, " ")}</span>
                  <span className="chip">{sh.target_duration_s}s</span>
                  {sh.motion_priority === "high" && (
                    <span className="chip hot">motion</span>
                  )}
                  <span className={used > budget ? "over" : ""}>
                    {used}/{budget} words
                  </span>
                </div>
                <p>{sh.action}</p>
              </div>
            );
          })}
          {sc.narration.map((n) => (
            <p key={n.local_index} className="narration">“{n.text}”</p>
          ))}
        </div>
      ))}

      {tab === "cast" && doc.characters.map((c) => (
        <div key={c.slug} className="scene">
          <strong>{c.name}</strong>{" "}
          <span className="muted small">{c.slug} · {c.role}</span>
          <p className="muted small">
            {c.age_impression}, {c.build}. {c.hair} hair, {c.eyes} eyes,{" "}
            {c.skin} skin. Wears {c.default_wardrobe}.
            {c.distinguishing_features.length > 0 &&
              ` (${c.distinguishing_features.join("; ")})`}
          </p>
        </div>
      ))}

      {tab === "style" && (
        <div className="scene">
          <p>{doc.style_bible.art_style}</p>
          <div className="row" style={{ margin: "10px 0" }}>
            {doc.style_bible.palette.map((c) => (
              <span key={c} className="chip">{c}</span>
            ))}
          </div>
          <p className="muted small">{doc.style_bible.lighting}</p>
          <p className="muted small">{doc.style_bible.camera_language}</p>
          <p className="muted small">{doc.style_bible.motion_language}</p>
        </div>
      )}
    </div>
  );
}
