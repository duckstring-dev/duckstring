import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Catchment | Duckstring",
  description: "There is no DAG.",
  icons: {
    icon: '/favicon.svg',
  }
};

// maximumScale 1 stops iOS Safari auto-zooming when a small-font input gets focus;
// pinch-zooming the page stays available (iOS ignores the cap for user gestures).
export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  maximumScale: 1,
  themeColor: '#0f0f14',
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="h-full">
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
