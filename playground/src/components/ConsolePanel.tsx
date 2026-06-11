'use client';

import { useState, useRef, useEffect } from 'react';
import { usePlaygroundStore, THEME_BRAND, THEME_PULL, THEME_PUSH, THEME_SUCCESS, THEME_DANGER, THEME_BLOCKED, THEME_WAKE } from '@/lib/store';

// Colour per event kind, for quick scanning. Triggers and demand follow the pull/push palette;
// the control verbs reuse their Sidebar button colours.
const KIND_COLOR: Record<string, string> = {
  tap: THEME_PULL,
  pulse: THEME_PUSH,
  force: THEME_SUCCESS,
  wake: THEME_WAKE,
  sleep: THEME_BLOCKED,
  kill: THEME_DANGER,
  'pond-pull': THEME_PULL,
  'ripple-pull': THEME_PULL,
  'pond-push': THEME_PUSH,
  'pond-start': '#a1a1aa',
  'ripple-start': '#a1a1aa',
  'ripple-done': '#71717a',
  'pond-done': '#e4e4e7',
};

function fmtClock(t: number): string {
  const d = new Date(t);
  const ss = d.getSeconds().toString().padStart(2, '0');
  const ms = d.getMilliseconds().toString().padStart(3, '0');
  return `${d.getMinutes().toString().padStart(2, '0')}:${ss}.${ms}`;
}

export function ConsolePanel() {
  const logs = usePlaygroundStore((s) => s.logs);
  const clearLogs = usePlaygroundStore((s) => s.clearLogs);
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);

  // Keep pinned to the bottom unless the user has scrolled up.
  useEffect(() => {
    const el = bodyRef.current;
    if (open && el && atBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [logs, open]);

  const onScroll = () => {
    const el = bodyRef.current;
    if (!el) return;
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  };

  const copyAll = async () => {
    const text = logs.map((l) => `${fmtClock(l.t)}  [${l.kind}] ${l.msg}`).join('\n');
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard unavailable */
    }
  };

  return (
    <div
      style={{
        borderTop: '1px solid #27272a',
        background: '#0c0c10',
        fontFamily: 'ui-monospace, SFMono-Regular, monospace',
        flexShrink: 0,
      }}
    >
      {/* header bar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '4px 10px',
          cursor: 'pointer',
          userSelect: 'none',
        }}
        onClick={() => setOpen((v) => !v)}
      >
        <span style={{ fontSize: 11, fontWeight: 700, color: '#a1a1aa', letterSpacing: '0.08em' }}>
          {open ? '▾' : '▸'} CONSOLE
          <span style={{ color: '#52525b', fontWeight: 400, marginLeft: 8 }}>{logs.length} events</span>
        </span>
        {open && (
          <span style={{ display: 'inline-flex', gap: 6 }}>
            <button
              onClick={(e) => { e.stopPropagation(); clearLogs(); }}
              style={btn('#71717a')}
            >
              Clear
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); copyAll(); }}
              style={btn(copied ? THEME_SUCCESS : THEME_BRAND)}
            >
              {copied ? 'Copied' : 'Copy'}
            </button>
          </span>
        )}
      </div>

      {open && (
        <div
          ref={bodyRef}
          onScroll={onScroll}
          style={{ height: 200, overflowY: 'auto', padding: '4px 10px 8px', fontSize: 11, lineHeight: 1.5 }}
        >
          {logs.length === 0 ? (
            <div style={{ color: '#52525b' }}>No events yet — trigger a Tap, Wave, Pulse…</div>
          ) : (
            logs.map((l, i) => (
              <div key={i} style={{ display: 'flex', gap: 8, whiteSpace: 'pre' }}>
                <span style={{ color: '#3f3f46' }}>{fmtClock(l.t)}</span>
                <span style={{ color: KIND_COLOR[l.kind] ?? '#71717a', minWidth: 96, display: 'inline-block' }}>
                  [{l.kind}]
                </span>
                <span style={{ color: '#d4d4d8' }}>{l.msg}</span>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function btn(color: string): React.CSSProperties {
  return {
    background: 'transparent',
    border: `1px solid ${color}`,
    color,
    borderRadius: 4,
    padding: '2px 8px',
    fontSize: 10,
    fontWeight: 600,
    cursor: 'pointer',
    letterSpacing: '0.04em',
  };
}
