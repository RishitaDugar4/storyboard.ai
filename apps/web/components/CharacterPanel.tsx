"use client";

import { useState } from "react";
import { useEffect } from "react";
import { ApiError, type Character, charactersApi, narrationApi } from "@/lib/api";

export function CharacterPanel({ characters, onChange }: {
  characters: Character[];
  onChange: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [fields, setFields] = useState<Record<string, string>>({});
  const [voices, setVoices] = useState<string[]>([]);

  useEffect(() => {
    narrationApi.voices().then((v) => setVoices(v.items)).catch(() => {});
  }, []);

  async function run(id: string, fn: () => Promise<unknown>) {
    setBusy(id); setError(null);
    try { await fn(); onChange(); }
    catch (e) { setError(e instanceof ApiError ? e.problem.detail : "Failed."); }
    finally { setBusy(null); }
  }

  if (characters.length === 0) {
    return <div className="empty">Apply a storyboard to populate the cast.</div>;
  }

  return (
    <>
      {error && <p className="err">{error}</p>}
      {characters.map((c) => {
        const impact = c.unlock_impact;
        return (
          <div key={c.id} className="scene">
            <div className="row between">
              <div>
                <strong>{c.name}</strong>{" "}
                <span className="muted small">{c.slug} · {c.role}</span>
              </div>
              <div className="row">
                {c.locked && <span className="chip hot">locked</span>}
                {!c.locked && editing !== c.id && (
                  <button className="chip" onClick={() => {
                    setEditing(c.id);
                    setFields({ ...(c.appearance as Record<string, string>) });
                  }}>
                    edit
                  </button>
                )}
                <button
                  disabled={busy !== null}
                  onClick={() => run(c.id, async () => {
                    if (!c.locked) return charactersApi.lock(c.id);
                    // Unlocking is the most expensive edit in the app; say so
                    // with real numbers before doing it.
                    const cost = impact
                      ? `\n\n${impact.shots} shot(s), ${impact.stills} still(s) ` +
                        `would need regenerating — about ` +
                        `${(impact.estimated_recost_cents / 100).toFixed(2)} USD.`
                      : "";
                    if (impact?.shots &&
                        !confirm(`Unlock ${c.name}?${cost}`)) return;
                    return charactersApi.unlock(c.id);
                  })}
                >
                  {c.locked ? "Unlock" : "Lock"}
                </button>
              </div>
            </div>

            {editing === c.id && !c.locked ? (
              <div className="editcanon">
                {["age_impression", "build", "hair", "eyes", "skin",
                  "default_wardrobe"].map((f) => (
                  <label key={f} className="field">
                    <span className="muted small">{f.replace(/_/g, " ")}</span>
                    <input
                      value={String(fields[f] ?? c.appearance?.[f] ?? "")}
                      onChange={(e) =>
                        setFields((v) => ({ ...v, [f]: e.target.value }))}
                    />
                  </label>
                ))}
                <div className="row" style={{ marginTop: 8 }}>
                  <button
                    disabled={busy !== null}
                    onClick={() => run(c.id, async () => {
                      await charactersApi.patch(c.id, { appearance: fields });
                      setEditing(null); setFields({});
                    })}
                  >
                    Save appearance
                  </button>
                  <button className="chip"
                          onClick={() => { setEditing(null); setFields({}); }}>
                    cancel
                  </button>
                </div>
                <p className="muted small">
                  Saving re-renders the canon below and marks every still they
                  appear in as stale.
                </p>
              </div>
            ) : (
              <p className="canon">{c.appearance_prompt}</p>
            )}
            <div className="row" style={{ marginTop: 8, gap: 10 }}>
              <label className="field">
                <span className="muted small">voice</span>
                <select
                  value={c.voice_name ?? ""}
                  disabled={busy !== null}
                  onChange={(e) => run(c.id, () => charactersApi.patch(c.id, {
                    voice: { voice_name: e.target.value },
                  }))}
                >
                  <option value="">narrator&apos;s voice</option>
                  {voices.map((v) => <option key={v} value={v}>{v}</option>)}
                </select>
              </label>
              <span className="muted small">
                {c.spoken_lines > 0
                  ? `speaks ${c.spoken_lines} line${c.spoken_lines === 1 ? "" : "s"}` +
                    (c.voice_name ? " — changing this re-records them" : "")
                  : "speaks no lines yet"}
              </span>
            </div>

            <p className="muted small">
              {c.locked
                ? "Frozen — this exact text goes into every prompt they appear in."
                : "Not yet frozen. Lock before generating stills so it stops changing."}
              {impact && impact.shots > 0 &&
                ` Appears in ${impact.shots} shot${impact.shots === 1 ? "" : "s"}.`}
            </p>
          </div>
        );
      })}
    </>
  );
}
