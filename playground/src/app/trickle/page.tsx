import type { Metadata } from 'next';
import { TrickleApp } from '@/components/trickle/TrickleApp';

const description = 'How a Duckstring Trickle updates a table from its changes, with a pipeline running in your browser and a guided tour.';

export const metadata: Metadata = {
  title: 'Trickle | Duckstring Playground',
  description,
  openGraph: { title: 'Trickle | Duckstring Playground', description, url: '/trickle', siteName: 'Duckstring' },
  twitter: { card: 'summary_large_image', title: 'Trickle | Duckstring Playground', description },
};

export default function Page() {
  return <TrickleApp />;
}
