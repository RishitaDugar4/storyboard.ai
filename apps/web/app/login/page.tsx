"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError, api } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(email, passphrase);
      router.replace("/projects");
      router.refresh();
    } catch (err) {
      // Distinguish the three real cases. Reporting a rejected credential as
      // "the API is down" sends you debugging the wrong thing entirely.
      if (err instanceof ApiError) {
        setError(
          err.code === "unauthorized"
            ? "That email and passphrase do not match an account."
            : err.code === "validation_failed"
              ? "Enter both an email address and a passphrase."
              : `${err.problem.title}: ${err.problem.detail}`,
        );
      } else {
        setError("Could not reach the API. Is `make dev` running?");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <h1>hbday-zee</h1>
      <p className="sub">This is a private project. Sign in to continue.</p>
      <form onSubmit={submit}>
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="you@local"
          autoComplete="username"
          autoFocus
          style={{ width: "100%", marginBottom: 10 }}
        />
        <div className="row">
          <input
            type="password"
            value={passphrase}
            onChange={(e) => setPassphrase(e.target.value)}
            placeholder="Passphrase"
            autoComplete="current-password"
          />
          <button type="submit" disabled={busy || !email || !passphrase}>
            {busy ? "Checking…" : "Enter"}
          </button>
        </div>
      </form>
      {error && <p className="err">{error}</p>}
    </main>
  );
}
