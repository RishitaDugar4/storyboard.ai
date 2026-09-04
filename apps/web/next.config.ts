import type { NextConfig } from "next";

// 127.0.0.1, not "localhost": on macOS localhost resolves to ::1 first, and a
// dev API bound to IPv4 only would make every proxied request hang.
// Compose overrides this with API_ORIGIN=http://api:8000.
const API = process.env.API_ORIGIN ?? "http://127.0.0.1:8000";

const config: NextConfig = {
  reactStrictMode: true,
  // Standalone output ships only the files the server actually needs, which
  // keeps the production image small. It is opt-in because it also disables
  // `next start` -- the Dockerfile sets NEXT_OUTPUT=standalone and runs
  // server.js directly; local runs keep the normal server.
  ...(process.env.NEXT_OUTPUT === "standalone"
    ? { output: "standalone" as const }
    : {}),
  // The browser talks only to the Next origin, so the session cookie stays
  // first-party and no CORS credentials dance is needed in the browser.
  async rewrites() {
    // Mirrors the Caddy routing in deploy/Caddyfile, so a path that works in
    // production works here too.
    return [
      { source: "/api/:path*", destination: `${API}/api/:path*` },
      { source: "/healthz", destination: `${API}/healthz` },
      { source: "/readyz", destination: `${API}/readyz` },
    ];
  },
};

export default config;
