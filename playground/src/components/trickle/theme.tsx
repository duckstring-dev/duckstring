import { THEME_AMBER, THEME_BRAND, THEME_DANGER, THEME_SUCCESS } from '@/lib/store';
import type { Mark } from '@/lib/trickle/pipeline';

// `dim`: outside what the figure is pointing at.
export type CellMark = Mark | 'dim';

export const MARK_COLOURS: Record<CellMark, string> = {
  base: '#55555e',
  dim: '#2c2b30',
  read: THEME_BRAND,
  added: THEME_SUCCESS,
  updated: THEME_AMBER,
  removed: THEME_DANGER,
};

export const MARK_LABELS: Record<Exclude<CellMark, 'dim'>, string> = {
  base: 'not touched',
  read: 'took part',
  added: 'added',
  updated: 'changed',
  removed: 'removed',
};

export const PANEL = '#18171a';
export const BORDER = '#34333a';
export const TEXT = '#e4e4e7';
export const MUTED = '#a1a1aa';
export const FAINT = '#71717a';
export const MONO = 'ui-monospace, SFMono-Regular, Menlo, monospace';
export const SANS = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';

export function Legend({ marks }: { marks: Exclude<CellMark, 'dim'>[] }) {
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 14px', fontFamily: MONO, fontSize: 11, color: MUTED }}>
      {marks.map((m) => (
        <span key={m} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          <span style={{ width: 9, height: 9, background: MARK_COLOURS[m], display: 'inline-block' }} />
          {MARK_LABELS[m]}
        </span>
      ))}
    </div>
  );
}
