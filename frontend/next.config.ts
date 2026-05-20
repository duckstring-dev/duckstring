import type { NextConfig } from "next";

const FASTAPI_URL = process.env.FASTAPI_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
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
