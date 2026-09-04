"use client";

import { useState } from "react";
import { ApiError, type Character, charactersApi } from "@/lib/api";

export function CharacterPanel({ characters, onChange }: {
  characters: Character[];
  onChange: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

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

            <p className="canon">{c.appearance_prompt}</p>
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
