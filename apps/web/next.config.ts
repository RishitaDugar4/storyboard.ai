import type { NextConfig } from "next";

// 127.0.0.1, not "localhost": on macOS localhost resolves to ::1 first, and a
// dev API bound to IPv4 only would make every proxied request hang.
// Compose overrides this with API_ORIGIN=http://api:8000.
const API = process.env.API_ORIGIN ?? "http://127.0.0.1:8000";

const config: NextConfig = {
  reactStrictMode: true,
  // Standalone output ships only the files the server actually needs, which
  // keeps the production image small and avoids installing dev dependencies
  // on the box.
  output: "standalone",
  // The browser talks only to the Next origin, so the session cookie stays
  // first-party and no CORS credentials dance is needed in the browser.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API}/api/:path*` }];
  },
};

export default config;
