import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Playground | Duckstring",
  description: "There is no DAG.",
  icons: {
    icon: '/favicon.svg',
  }
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
