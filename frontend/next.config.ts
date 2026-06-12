import type { NextConfig } from "next";

// Dev-only: where `npm run dev` proxies /api/* (a running Catchment). Default matches the
// Catchment's default port (7474); override with FASTAPI_URL for a Catchment on another port.
const FASTAPI_URL = process.env.FASTAPI_URL ?? "http://localhost:7474";
const isStaticExport = process.env.NEXT_STATIC_EXPORT === "true";

// Static export (used when building for FastAPI to serve) is incompatible with rewrites.
const nextConfig: NextConfig = isStaticExport
  ? { output: "export" }
  : {
      // Dev-only: let phones on the LAN load the dev server (Next blocks cross-origin
      // requests to dev resources by default).
      allowedDevOrigins: ["192.168.*.*"],
      async rewrites() {
        return [
          {
            source: "/api/:path*",
            destination: `${FASTAPI_URL}/api/:path*`,
          },
        ];
      },
    };

export default nextConfig;
