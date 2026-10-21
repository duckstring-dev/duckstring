import type { Metadata } from 'next';
import { Story } from '@/components/story/Story';

const description = 'Duckstring in two minutes: pull orchestration, upgrades and incremental processing, played in your browser.';

// Embedded on the landing page; the Playground itself is the page to find in search.
export const metadata: Metadata = {
  title: 'Story | Duckstring Playground',
  description,
  robots: { index: false },
};

export default function Page() {
  return <Story />;
}
