import type { NextConfig } from "next";

// The playground is a fully client-side, in-memory simulation — it has no backend and makes no
// /api calls, so there are no rewrites or static-export branch. Vercel builds this Next app natively.
const nextConfig: NextConfig = {
  // Dev-only: let phones on the LAN load the dev server (Next blocks cross-origin requests to
  // dev resources by default). The wildcard covers DHCP handing the Mac a different address.
  allowedDevOrigins: ['192.168.0.4', '192.168.*.*'],
};

export default nextConfig;
