"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError, api } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [passphrase, setPassphrase] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(passphrase);
      router.replace("/projects");
      router.refresh();
    } catch (err) {
      setError(
        err instanceof ApiError && err.code === "unauthorized"
          ? "That passphrase is not correct."
          : "Could not reach the API. Is it running on :8000?",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <h1>hbday-zee</h1>
      <p className="sub">This is a private project. Enter the passphrase.</p>
      <form onSubmit={submit} className="row">
        <input
          type="password"
          value={passphrase}
          onChange={(e) => setPassphrase(e.target.value)}
          placeholder="Passphrase"
          autoFocus
          autoComplete="current-password"
        />
        <button type="submit" disabled={busy || !passphrase}>
          {busy ? "Checking…" : "Enter"}
        </button>
      </form>
      {error && <p className="err">{error}</p>}
    </main>
  );
}
