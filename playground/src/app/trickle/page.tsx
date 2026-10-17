import type { Metadata } from 'next';
import { TricklePage } from '@/components/trickle/TricklePage';

const description = 'How a Duckstring Trickle updates a table from its changes: Z-sets, join deltas and a pipeline run by run.';

export const metadata: Metadata = {
  title: 'Trickle | Duckstring Playground',
  description,
  openGraph: { title: 'Trickle | Duckstring Playground', description, url: '/trickle', siteName: 'Duckstring' },
  twitter: { card: 'summary_large_image', title: 'Trickle | Duckstring Playground', description },
};

export default function Page() {
  return <TricklePage />;
}
