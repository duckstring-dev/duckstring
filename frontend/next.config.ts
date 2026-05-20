import type { NextConfig } from "next";

const FASTAPI_URL = process.env.FASTAPI_URL ?? "http://localhost:8000";
const isStaticExport = process.env.NEXT_STATIC_EXPORT === "true";

// Static export (used when building for FastAPI to serve) is incompatible with rewrites.
const nextConfig: NextConfig = isStaticExport
  ? { output: "export" }
  : {
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
