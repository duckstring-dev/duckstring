import type { Metadata, Viewport } from "next";
import { Analytics } from '@vercel/analytics/next';
import { SpeedInsights } from '@vercel/speed-insights/next';
import "./globals.css";

const description = "Duckstring's pull orchestration, simulated in your browser, with a guided tour.";

// metadataBase makes the Open Graph image URL (app/opengraph-image.png) absolute for link previews.
export const metadata: Metadata = {
  metadataBase: new URL('https://playground.duckstring.com'),
  title: "Playground | Duckstring",
  description,
  openGraph: { title: "Duckstring Playground", description, url: '/', siteName: 'Duckstring' },
  twitter: { card: 'summary_large_image', title: "Duckstring Playground", description },
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
      <body className="min-h-full flex flex-col">
        {children}
        <Analytics />
        <SpeedInsights />
      </body>
    </html>
  );
}
