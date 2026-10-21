'use client';

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ReactFlowProvider, useReactFlow } from '@xyflow/react';
import { track } from '@vercel/analytics';
import { usePlaygroundStore, THEME_BRAND } from '@/lib/store';
import { holdMs, playStory, setUpStory, type SayOptions, type StoryCtx } from '@/lib/story';
import type { PondId } from '@/lib/types';
import { DagCanvas } from '../DagCanvas';
import { TraceChart } from '../TraceChart';
import { JoinGrid } from '../trickle/JoinGrid';

// The story page (/story): the canvas alone, played by the script in lib/story.ts. Lines show large and centred over
// the paused, dimmed pipeline, then shrink into the caption band at the foot (one element, laid out at its band size
// and scaled up while centred, so the move never reflows the text). The page fits any shape: a wide frame lays the
// graph out left to right with the run chart beside the caption, a tall one top to bottom with the chart above it.
//
// ?autoplay=1 starts at once (the landing page loads it on a click); otherwise a Play button waits. ?rate=N runs the
// whole story N times faster (the tests). It pauses itself while scrolled out of view or in a hidden tab.
// It can fill the screen (useExpand): a button, or on a touch screen the first tap while it plays.

const MONO = 'ui-monospace, SFMono-Regular, monospace';
const BG = '#0f0f14';

class Stopped extends Error {}

type Phase = 'ready' | 'playing' | 'ended';
type Caption = 'hidden' | 'centred' | 'docked';

function useViewport() {
  const [size, setSize] = useState({ w: 1080, h: 500 });
  useEffect(() => {
    const read = () => setSize({ w: window.innerWidth, h: window.innerHeight });
    read();
    window.addEventListener('resize', read);
    return () => window.removeEventListener('resize', read);
  }, []);
  return size;
}

// The frame's shape decides the layout: wide lays the graph out left to right with the chart beside the caption,
// tall top to bottom with the chart above it. The caption's size in the band (centred, it's scaled up); narrow frames
// dock it narrower, so it can grow.
function dims(w: number, h: number) {
  const wide = w / h >= 1.15;
  const fs = wide ? Math.max(12, Math.min(19, w / 57)) : Math.max(13, Math.min(17, w / 25));
  return { wide, fs, capMax: wide ? fs * 29.5 : w * 0.74, capLines: wide ? 2 : 3, maxScale: wide ? 1.5 : 1.35 };
}

function param(name: string): string | null {
  return typeof window === 'undefined' ? null : new URLSearchParams(window.location.search).get(name);
}

// Expanding the story to fill the screen. Embedded, the landing page owns it (real fullscreen where the browser
// allows it, else the frame fills the window: iPhone Safari only takes videos fullscreen), asked and told by
// postMessage. On its own, the page's own fullscreen. On a touch screen the first tap while it plays expands it.
function useExpand() {
  const [expanded, setExpanded] = useState(false);
  const [canExpand, setCanExpand] = useState(false);
  const [touch, setTouch] = useState(false);
  useEffect(() => {
    const embedded = window.parent !== window;
    const t = window.setTimeout(() => {
      setCanExpand(embedded || typeof document.documentElement.requestFullscreen === 'function');
      setTouch(window.matchMedia('(pointer: coarse)').matches);
    }, 0);
    const onMessage = (e: MessageEvent) => {
      if (e.source === window.parent && e.data?.type === 'ds-story-expanded') setExpanded(!!e.data.expanded);
    };
    const onFullscreen = () => {
      if (!embedded) setExpanded(!!document.fullscreenElement);
    };
    window.addEventListener('message', onMessage);
    document.addEventListener('fullscreenchange', onFullscreen);
    if (embedded) window.parent.postMessage({ type: 'ds-story-hello' }, '*');
    return () => {
      window.clearTimeout(t);
      window.removeEventListener('message', onMessage);
      document.removeEventListener('fullscreenchange', onFullscreen);
    };
  }, []);
  const toggle = useCallback((expand: boolean) => {
    if (window.parent !== window) window.parent.postMessage({ type: 'ds-story-expand', expand }, '*');
    else if (expand) document.documentElement.requestFullscreen?.().catch(() => {});
    else if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  }, []);
  return { expanded, canExpand, touch, toggle };
}

export function Story() {
  return (
    <ReactFlowProvider>
      <StoryInner />
    </ReactFlowProvider>
  );
}

function StoryInner() {
  const { w, h } = useViewport();
  const { wide, fs, capLines } = dims(w, h);
  const direction = wide ? 'LR' : 'TB';

  const [phase, setPhase] = useState<Phase>('ready');
  const [userPaused, setUserPaused] = useState(false);
  const [hidden, setHidden] = useState(false); // scrolled away or tab hidden
  const [chartPond, setChartPond] = useState<PondId | null>(null);
  const [grid, setGrid] = useState<{ step: number; eq?: string } | null>(null);
  const [gridOn, setGridOn] = useState(false);
  const [rings, setRings] = useState<string[]>([]);
  const [fast, setFast] = useState(false);
  const [dockH, setDockH] = useState(80);
  const [ending, setEnding] = useState(false);

  const rootRef = useRef<HTMLDivElement>(null);
  const capRef = useRef<HTMLDivElement>(null);
  const dimRef = useRef<HTMLDivElement>(null);
  const slotRef = useRef<HTMLDivElement>(null);
  const dockRef = useRef<HTMLDivElement>(null);

  // Story time: advances only while playing, unpaused and in view. Every wait in the script is on this clock.
  const rate = useRef(1);
  const clock = useRef({ t: 0, running: false });
  const holding = useRef(false); // a line is up and the pipeline paused for it
  const skip = useRef(false);
  const speedX = useRef(1);
  const run = useRef(0); // bumped to stop a run (restart, unmount)
  const caption = useRef<Caption>('hidden');

  const running = phase === 'playing' && !userPaused && !hidden;
  const expand = useExpand();
  const tapExpands = expand.touch && expand.canExpand && !expand.expanded;

  const syncSim = useCallback(() => {
    const st = usePlaygroundStore.getState();
    const paused = !clock.current.running || holding.current;
    if (st.paused !== paused) usePlaygroundStore.setState({ paused });
    const sp = speedX.current * rate.current;
    if (st.speed !== sp) st.setSpeed(sp);
  }, []);

  useEffect(() => {
    clock.current.running = running;
    syncSim();
  }, [running, syncSim]);

  useEffect(() => {
    const r = Number(param('rate'));
    if (r > 0) rate.current = r;
    let raf = 0;
    let last = performance.now();
    const step = (now: number) => {
      if (clock.current.running) clock.current.t += (now - last) * rate.current;
      last = now;
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, []);

  // The simulation's real-time driver (as the Playground's).
  useEffect(() => {
    let last = performance.now();
    const id = setInterval(() => {
      const real = performance.now();
      const dt = real - last;
      last = real;
      const { paused, speed, now, tick } = usePlaygroundStore.getState();
      if (!paused) tick(now + dt * speed);
    }, 100);
    return () => clearInterval(id);
  }, []);

  // Pause while out of view: the landing page embeds this further down the page, and a hidden tab drifts.
  useEffect(() => {
    const root = rootRef.current;
    let inView = true;
    const update = () => setHidden(!inView || document.visibilityState === 'hidden');
    const io = new IntersectionObserver(([e]) => {
      inView = e.intersectionRatio >= 0.3;
      update();
    }, { threshold: [0, 0.3, 0.6] });
    if (root) io.observe(root);
    document.addEventListener('visibilitychange', update);
    return () => {
      io.disconnect();
      document.removeEventListener('visibilitychange', update);
    };
  }, []);

  useEffect(() => {
    const el = dockRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setDockH(el.offsetHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Where the caption sits: in the band's caption slot, or (by transform) centred in the frame at a larger size. It
  // reads the frame's size live: the script's context outlives renders.
  const place = useCallback(
    (centred: boolean, animate: boolean) => {
      const cap = capRef.current;
      const slot = slotRef.current;
      if (!cap || !slot) return;
      const w = window.innerWidth;
      const h = window.innerHeight;
      const { capMax, maxScale } = dims(w, h);
      const r = slot.getBoundingClientRect();
      const capW = Math.min(capMax, r.width - 16);
      cap.style.width = `${capW}px`;
      const ch = cap.offsetHeight;
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      const scale = Math.min(maxScale, (w * 0.9) / capW);
      cap.style.transition = animate ? 'opacity 300ms ease, transform 650ms cubic-bezier(0.4, 0, 0.2, 1)' : 'opacity 300ms ease';
      cap.style.left = `${cx - capW / 2}px`;
      cap.style.top = `${cy - ch / 2}px`;
      cap.style.transform = centred ? `translate(${w / 2 - cx}px, ${h / 2 - cy}px) scale(${scale})` : 'none';
    },
    []
  );

  // Keep the caption where it belongs when the frame or the band changes size.
  useLayoutEffect(() => {
    if (caption.current !== 'hidden') place(caption.current === 'centred', false);
  }, [place, dockH, chartPond, w, h]);

  const setDim = (on: boolean) => {
    const d = dimRef.current;
    if (!d) return;
    d.style.opacity = on ? '1' : '0';
    d.style.pointerEvents = on ? 'auto' : 'none';
  };

  const makeCtx = useCallback(
    (token: number): StoryCtx => {
      const alive = () => {
        if (token !== run.current) throw new Stopped();
      };
      const frame = () => new Promise<void>((res, rej) => requestAnimationFrame(() => (token === run.current ? res() : rej(new Stopped()))));
      const until = async (cond: (s: ReturnType<typeof usePlaygroundStore.getState>) => boolean, minMs = 0) => {
        const start = clock.current.t;
        for (;;) {
          alive();
          if (clock.current.t - start >= minMs && cond(usePlaygroundStore.getState())) return;
          await frame();
        }
      };
      const wait = async (ms: number) => {
        const end = clock.current.t + ms;
        for (;;) {
          alive();
          if (clock.current.t >= end) return;
          await frame();
        }
      };
      const say = async (text: string, opts: SayOptions = {}) => {
        alive();
        const cap = capRef.current!;
        if (caption.current !== 'hidden') {
          cap.style.opacity = '0';
          await wait(300);
        }
        holding.current = !opts.run;
        syncSim();
        cap.textContent = text;
        setDim(true);
        caption.current = 'centred';
        place(true, false);
        cap.style.opacity = '1';
        const start = clock.current.t;
        skip.current = false;
        if (opts.during) await opts.during();
        place(true, false); // the band may have changed under it
        const end = start + holdMs(text);
        for (;;) {
          alive();
          if (skip.current || clock.current.t >= end) break;
          await frame();
        }
        if (opts.stay) return;
        caption.current = 'docked';
        place(false, true);
        setDim(false);
        holding.current = false;
        syncSim();
        await wait(450);
      };
      return {
        get: () => usePlaygroundStore.getState(),
        say,
        wait,
        until,
        ring: (ids) => setRings(ids),
        chart: async (pond) => {
          alive();
          setChartPond(pond);
          await wait(700); // the band grows or shrinks, and the graph re-frames behind the dim
        },
        grid: (step, eq) => {
          if (step === null) setGridOn(false);
          else {
            setGrid({ step, eq });
            setGridOn(true);
          }
        },
        speed: (x) => {
          speedX.current = x;
          setFast(x !== 1);
          syncSim();
        },
      };
    },
    [place, syncSim]
  );

  const reset = useCallback(() => {
    run.current += 1;
    holding.current = true;
    speedX.current = 1;
    caption.current = 'hidden';
    if (capRef.current) capRef.current.style.opacity = '0';
    setDim(false);
    setChartPond(null);
    setGridOn(false);
    setGrid(null);
    setRings([]);
    setFast(false);
    setEnding(false);
    setUpStory(usePlaygroundStore.getState);
    syncSim();
  }, [syncSim]);

  const start = useCallback(() => {
    reset();
    const token = run.current;
    setPhase('playing');
    setUserPaused(false);
    track('Story', { event: 'start' });
    playStory(makeCtx(token))
      .then(() => {
        if (token !== run.current) return;
        setPhase('ended');
        setEnding(true);
        track('Story', { event: 'end' });
      })
      .catch((e) => {
        if (!(e instanceof Stopped)) throw e;
      });
  }, [reset, makeCtx]);

  useEffect(() => {
    setUpStory(usePlaygroundStore.getState);
    syncSim();
    const t = param('autoplay') === '1' ? window.setTimeout(start, 0) : 0;
    return () => {
      window.clearTimeout(t);
      run.current += 1;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div
      ref={rootRef}
      data-testid="story"
      data-phase={phase}
      data-expanded={expand.expanded}
      onClick={() => {
        if (phase === 'playing' && tapExpands) expand.toggle(true);
      }}
      style={{ position: 'fixed', inset: 0, overflow: 'hidden', background: BG, fontFamily: MONO, color: '#e4e4e7' }}
    >
      <style>{'.ds-story-grid svg { max-height: none !important; max-width: none !important; }'}</style>
      {/* The canvas takes no input: the story drives it, and a wheel over it scrolls the page it's embedded in. */}
      <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
        <DagCanvas bare direction={direction} />
      </div>
      <StoryFramer dockH={dockH} w={w} h={h} direction={direction} />
      <Rings ids={rings} />

      <GridPanel grid={grid} on={gridOn} dockH={dockH} w={w} h={h} />

      {/* The caption band: kept clear of the graph. */}
      <div
        ref={dockRef}
        style={{
          position: 'absolute',
          left: 0,
          right: 0,
          bottom: 0,
          display: 'flex',
          flexDirection: wide ? 'row' : 'column-reverse',
          alignItems: wide ? 'flex-end' : 'center',
          gap: 12,
          padding: '8px 14px 10px',
          pointerEvents: 'none',
        }}
      >
        <div ref={slotRef} style={{ flex: wide ? 1 : undefined, alignSelf: 'stretch', minHeight: capLines * fs * 1.4 + 12 }} />
        {chartPond && <ChartPanel pond={chartPond} width={wide ? 250 : Math.min(280, w - 32)} />}
      </div>

      <div
        ref={dimRef}
        data-testid="story-dim"
        onClick={(e) => {
          if (tapExpands) return; // the root expands it; skipping waits for the next tap
          e.stopPropagation();
          skip.current = true;
        }}
        style={{ position: 'absolute', inset: 0, background: 'rgba(9,9,12,0.8)', opacity: 0, transition: 'opacity 550ms ease', pointerEvents: 'none' }}
      />
      <div
        ref={capRef}
        data-testid="story-caption"
        style={{
          position: 'fixed',
          left: 0,
          top: 0,
          color: '#f4f4f5',
          fontSize: fs,
          fontWeight: 700,
          lineHeight: 1.4,
          textAlign: 'center',
          opacity: 0,
          transformOrigin: '50% 50%',
          pointerEvents: 'none',
        }}
      />

      <div
        style={{
          position: 'absolute',
          left: 12,
          top: 10,
          padding: '4px 9px',
          borderRadius: 6,
          background: 'rgba(24,24,27,0.9)',
          border: '1px solid #3f3f46',
          color: '#a1a1aa',
          fontSize: 12,
          fontWeight: 700,
          opacity: fast && phase === 'playing' ? 1 : 0,
          transition: 'opacity 300ms ease',
          pointerEvents: 'none',
        }}
      >
        2× speed
      </div>

      {phase !== 'ready' && (
        <div style={{ position: 'absolute', right: 10, top: 8, display: 'flex', gap: 6 }}>
          {phase === 'playing' && (
            <>
              <IconButton label={userPaused ? 'Play' : 'Pause'} testId="story-pause" onClick={() => setUserPaused((p) => !p)}>
                {userPaused ? '▶' : '❚❚'}
              </IconButton>
              <IconButton
                label="Start again"
                testId="story-restart"
                onClick={() => {
                  track('Story', { event: 'restart' });
                  start();
                }}
              >
                ↺
              </IconButton>
            </>
          )}
          {expand.canExpand && (
            <IconButton
              label={expand.expanded ? 'Close full screen' : 'Full screen'}
              testId="story-expand"
              onClick={() => expand.toggle(!expand.expanded)}
            >
              {expand.expanded ? '✕' : '⤢'}
            </IconButton>
          )}
        </div>
      )}

      {phase === 'ready' && (
        <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(9,9,12,0.72)' }}>
          <button data-testid="story-play" onClick={start} style={bigButton(true)}>
            ▶ Play <span style={{ fontWeight: 400, opacity: 0.75 }}>(2 min)</span>
          </button>
        </div>
      )}

      {phase === 'ended' && (
        <div
          data-testid="story-end"
          style={{
            position: 'absolute',
            left: 0,
            right: 0,
            top: `calc(50% + ${Math.max(44, fs * 3)}px)`,
            display: 'flex',
            justifyContent: 'center',
            flexWrap: 'wrap',
            gap: 10,
            padding: '0 16px',
            opacity: ending ? 1 : 0,
            transition: 'opacity 500ms ease',
          }}
        >
          <button
            data-testid="story-replay"
            onClick={() => {
              track('Story', { event: 'replay' });
              start();
            }}
            style={bigButton(false)}
          >
            ↺ Watch again
          </button>
          <a href="/" target="_blank" rel="noopener noreferrer" onClick={() => track('Story', { event: 'playground' })} style={bigButton(true)}>
            Try the Playground ↗
          </a>
        </div>
      )}
    </div>
  );
}

function bigButton(primary: boolean): React.CSSProperties {
  return {
    fontFamily: MONO,
    fontSize: 15,
    fontWeight: 700,
    padding: '11px 18px',
    borderRadius: 8,
    cursor: 'pointer',
    textDecoration: 'none',
    background: primary ? THEME_BRAND : 'rgba(24,24,27,0.9)',
    color: primary ? BG : '#e4e4e7',
    border: primary ? `1px solid ${THEME_BRAND}` : '1px solid #3f3f46',
  };
}

function IconButton({ label, testId, onClick, children }: { label: string; testId: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      aria-label={label}
      title={label}
      data-testid={testId}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      style={{
        width: 32,
        height: 30,
        borderRadius: 6,
        cursor: 'pointer',
        background: 'rgba(24,24,27,0.85)',
        border: '1px solid #3f3f46',
        color: '#a1a1aa',
        fontFamily: MONO,
        fontSize: 12,
      }}
    >
      {children}
    </button>
  );
}

// Fits the graph into the space between the controls and the caption band, whenever the frame, the band or the
// graph changes.
function StoryFramer({ dockH, w, h, direction }: { dockH: number; w: number; h: number; direction: string }) {
  const { fitView } = useReactFlow();
  const shape = usePlaygroundStore(
    (s) =>
      Object.keys(s.ponds).join(',') +
      '|' +
      Object.keys(s.refreshes)
        .filter((k) => s.refreshes[k]?.length)
        .join(',')
  );
  useEffect(() => {
    const t = setTimeout(
      () => fitView({ padding: { top: '48px', left: '18px', right: '18px', bottom: `${dockH + 10}px` }, maxZoom: 1.25, duration: 450 }),
      220
    );
    return () => clearTimeout(t);
  }, [fitView, dockH, w, h, direction, shape]);
  return null;
}

// The tour's white ring around canvas nodes, tracking them as the canvas moves.
function Rings({ ids }: { ids: string[] }) {
  const layer = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = layer.current;
    if (!el) return;
    el.replaceChildren(
      ...ids.map(() => {
        const d = document.createElement('div');
        d.style.cssText =
          'position:fixed;border:3px solid #fff;box-sizing:border-box;border-radius:6px;box-shadow:0 0 16px 2px rgba(255,255,255,0.35);display:none';
        return d;
      })
    );
    if (!ids.length) return;
    let raf = 0;
    const tick = () => {
      ids.forEach((id, i) => {
        const node = document.querySelector(`.react-flow__node[data-id="${id}"]`);
        const d = el.children[i] as HTMLDivElement;
        const b = node?.getBoundingClientRect();
        if (!b || !b.width) {
          d.style.display = 'none';
          return;
        }
        const p = 7;
        Object.assign(d.style, { display: 'block', left: `${b.left - p}px`, top: `${b.top - p}px`, width: `${b.width + 2 * p}px`, height: `${b.height + 2 * p}px` });
      });
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [ids]);
  return <div ref={layer} data-testid="story-rings" style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }} />;
}

function ChartPanel({ pond, width }: { pond: PondId; width: number }) {
  const name = usePlaygroundStore((s) => s.ponds[pond]?.name ?? '');
  const ps = usePlaygroundStore((s) => s.pondStates[pond]);
  return (
    <div
      data-testid="story-chart"
      style={{
        width,
        flex: 'none',
        boxSizing: 'border-box',
        background: '#18181b',
        border: '1px solid #3f3f46',
        borderRadius: 10,
        padding: '9px 12px 6px',
        boxShadow: '0 10px 32px rgba(0,0,0,0.55)',
      }}
    >
      <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 4 }}>{name}</div>
      <TraceChart times={ps?.completionTimes ?? []} durations={ps?.durations ?? []} ages={ps?.ages ?? []} />
    </div>
  );
}

// The Trickle join grid (the Trickle tour's figure), centred in the space above the band and scaled to fill it.
function GridPanel({ grid, on, dockH, w, h }: { grid: { step: number; eq?: string } | null; on: boolean; dockH: number; w: number; h: number }) {
  const panel = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(1);
  useLayoutEffect(() => {
    const el = panel.current;
    if (!el) return;
    const availW = w - 32;
    const availH = h - dockH - 24;
    setScale(Math.min(1.45, availW / el.offsetWidth, availH / el.offsetHeight));
  }, [grid, w, h, dockH]);
  if (!grid) return null;
  return (
    <div
      style={{
        position: 'absolute',
        left: 0,
        right: 0,
        top: 0,
        bottom: dockH,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        pointerEvents: 'none',
        opacity: on ? 1 : 0,
        transition: 'opacity 400ms ease',
      }}
    >
      <div
        ref={panel}
        data-testid="story-grid"
        className="ds-story-grid"
        style={{
          width: 300,
          boxSizing: 'border-box',
          padding: '12px 14px 14px',
          background: '#18181b',
          border: '1px solid #3f3f46',
          borderRadius: 10,
          boxShadow: '0 10px 40px rgba(0,0,0,0.6)',
          transform: `scale(${scale})`,
        }}
      >
        <JoinGrid step={grid.step} eq={grid.eq} />
      </div>
    </div>
  );
}
