import {useCallback, useEffect, useRef, useState, type ReactNode} from 'react';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';

import styles from './styles.module.css';

// The Playground's story (/story): Duckstring in two minutes, played in the page. Nothing loads until Play, so the
// landing page pays for the Playground only when someone asks for it; the frame then starts at once.
//
// The story can fill the screen: it asks by postMessage (its button, or a tap on a touch screen) and this page does
// it, with real fullscreen where the browser allows it, else by filling the window (iPhone Safari only takes videos
// fullscreen). On a phone, Play opens it that way.
export default function Story(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  const {playgroundUrl, playgroundDevPort} = siteConfig.customFields as {
    playgroundUrl: string;
    playgroundDevPort: number | null;
  };
  const section = useRef<HTMLElement>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const [src, setSrc] = useState<string | null>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [filled, setFilled] = useState(false);
  const expanded = fullscreen || filled;

  const expand = useCallback((on: boolean) => {
    const el = section.current;
    if (!el) return;
    if (on) {
      if (typeof el.requestFullscreen === 'function') el.requestFullscreen().catch(() => setFilled(true));
      else setFilled(true);
    } else {
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
      setFilled(false);
    }
  }, []);

  const tell = useCallback(() => {
    const win = frame.current?.contentWindow;
    if (win && src) win.postMessage({type: 'ds-story-expanded', expanded}, new URL(src).origin);
  }, [expanded, src]);

  useEffect(tell, [tell]);

  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      if (e.source !== frame.current?.contentWindow) return;
      if (e.data?.type === 'ds-story-hello') tell();
      if (e.data?.type === 'ds-story-expand') expand(!!e.data.expand);
    };
    const onFullscreen = () => setFullscreen(document.fullscreenElement === section.current);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setFilled(false);
    };
    window.addEventListener('message', onMessage);
    document.addEventListener('fullscreenchange', onFullscreen);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('message', onMessage);
      document.removeEventListener('fullscreenchange', onFullscreen);
      window.removeEventListener('keydown', onKey);
    };
  }, [expand, tell]);

  // Filling the window: the page behind it doesn't scroll.
  useEffect(() => {
    if (!filled) return;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = overflow;
    };
  }, [filled]);

  const play = () => {
    // NODE_ENV is fixed when the bundle is compiled, so a dev server always takes the Playground dev server.
    const base =
      process.env.NODE_ENV === 'development' && playgroundDevPort
        ? `${window.location.protocol}//${window.location.hostname}:${playgroundDevPort}`
        : playgroundUrl;
    setSrc(`${base}/story?autoplay=1`);
    if (window.matchMedia('(max-width: 768px) and (pointer: coarse)').matches) expand(true);
  };

  return (
    <section
      ref={section}
      className={`${styles.story} ${filled ? styles.storyFilled : ''}`}
      aria-label="Duckstring in two minutes">
      {src ? (
        <iframe ref={frame} className={styles.storyFrame} src={src} title="Duckstring in two minutes" />
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
