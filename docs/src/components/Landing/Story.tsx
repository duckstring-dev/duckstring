import {useState, type ReactNode} from 'react';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';

import styles from './styles.module.css';

// The Playground's story (/story): Duckstring in two minutes, played in the page. Nothing loads until Play, so the
// landing page pays for the Playground only when someone asks for it; the frame then starts at once.
export default function Story(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  const {playgroundUrl, playgroundDevPort} = siteConfig.customFields as {
    playgroundUrl: string;
    playgroundDevPort: number | null;
  };
  const [src, setSrc] = useState<string | null>(null);
  const play = () => {
    const base = playgroundDevPort
      ? `${window.location.protocol}//${window.location.hostname}:${playgroundDevPort}`
      : playgroundUrl;
    setSrc(`${base}/story?autoplay=1`);
  };
  return (
    <section className={styles.story} aria-label="Duckstring in two minutes">
      {src ? (
        <iframe className={styles.storyFrame} src={src} title="Duckstring in two minutes" />
      ) : (
        <button type="button" className={styles.storyPoster} onClick={play}>
          <span className={styles.storyQuestion}>
            What if pipelines ran on demand from the end, instead of on a schedule from the start?
          </span>
          <span className={styles.storyPlay}>
            ▶ Play <span className={styles.storyLength}>(2 min)</span>
          </span>
        </button>
      )}
    </section>
  );
}
