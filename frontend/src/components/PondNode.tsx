'use client';

import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { useLiveStore, appState, formatAge, stateColor, nodeFill, STATE_COLORS, THEME_BRAND, THEME_DEMAND } from '@/lib/store';
import { openApp } from '@/lib/api';
import { STRIP_LINE_H, STRIP_PAD, demandStripHeight } from '@/lib/layout';
import { DemandIndicators } from './DemandIndicators';
import { DbtIcon } from './DbtIcon';

// A small grid/table glyph — the affordance to open this Pond's data viewer (shown when it has tables).
function TableIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="1.5" y="2.5" width="13" height="11" rx="1.5" />
      <line x1="1.5" y1="6.5" x2="14.5" y2="6.5" />
      <line x1="6" y1="6.5" x2="6" y2="13.5" />
    </svg>
  );
}

// An arrow out of a box: the affordance to open an App in a new tab (an App has no tables to view).
export function OpenIcon({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"
         strokeLinecap="round" strokeLinejoin="round">
      <path d="M9.5 2.5h4v4" />
      <line x1="13.5" y1="2.5" x2="7.5" y2="8.5" />
      <path d="M12 9.5v3a1 1 0 0 1-1 1H3.5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h3" />
    </svg>
  );
}

const APP_STATE_COLOR: Record<string, string> = {
  running: STATE_COLORS.running, starting: STATE_COLORS.running,
  idle: '#71717a', failed: STATE_COLORS.failed, killed: STATE_COLORS.killed,
};

export const PondNode = memo(function PondNode({ data }: NodeProps) {
  const pondId = data.pondId as string;
  // TB (mobile) layout: flow enters at the top and leaves at the bottom.
  const vertical = data.vertical as boolean | undefined;
  // The Pond's recurring Refreshes and Taps, drawn as a strip joined to the bottom of its box (the layout
  // has already made the box tall and wide enough for them).
  const demandLines = (data.demandLines as string[] | undefined) ?? [];
  const pond = useLiveStore((s) => s.ponds[pondId]);
  const view = useLiveStore((s) => s.pondViews[pondId]);
  const info = useLiveStore((s) => s.pondInfo[pondId]);
  const selectedPondId = useLiveStore((s) => s.selectedPondId);
  const selectPond = useLiveStore((s) => s.selectPond);
  const selectorActive = useLiveStore((s) => s.selectorMode);
  const selecting = useLiveStore((s) => s.selectorMode && s.selectorPhase === 'select');
  const inScope = useLiveStore((s) => s.selectorScope.includes(pondId));
  const toggleSelect = useLiveStore((s) => s.toggleSelect);
  const rebuildPending = useLiveStore((s) => s.pondInfo[pondId]?.rebuildPending ?? false);
  const collapsed = useLiveStore((s) => !!s.collapsedPonds[pondId]);
  const toggleCollapse = useLiveStore((s) => s.toggleCollapse);
  // Only a Pond that owns Ripples can collapse — a Draw has nothing to hide, so it shows no caret.
  const hasRipples = useLiveStore((s) => Object.values(s.ripples).some((r) => r.pondId === pondId));
  const hasTables = useLiveStore((s) => s.pondInfo[pondId]?.hasTables ?? false);
  const hasObjects = useLiveStore((s) => s.pondInfo[pondId]?.hasObjects ?? false);
  const openDataViewer = useLiveStore((s) => s.openDataViewer);
  const now = useLiveStore((s) => s.now);

  if (!pond || !view) return null;

  // An App's rim follows its instance: brand cyan while one is serving (or starting), as a Run would.
  const app = pond.isApp ? appState(info) : null;
  const borderColor = app ? (view.status === 'blocked' ? stateColor(view) : APP_STATE_COLOR[app]) : stateColor(view);
  const radius = pond.isApp ? 16 : 10;
  const isSelected = selectedPondId === pondId;
  // A Spout's pond name is "{source}#{spout}" — show just the spout part on the node.
  const displayName = pond.isSpout ? pond.name.split('#').slice(1).join('#') || pond.name : pond.name;
  // Draws and Spouts both cross the Catchment boundary → dashed (ingress vs egress).
  const boundary = pond.isDraw || pond.isSpout;
  // In selector mode, a selected Pond keeps a bright cyan ring through BOTH phases — so during
  // "choose actions" the operator can still see exactly which Ponds the actions will hit. Clicking
  // toggles the scope only in the select phase; in the ops phase a canvas click is inert.
  const highlighted = selectorActive && inScope;

  return (
    <div
      onClick={(e) => {
        e.stopPropagation();
        if (selecting) toggleSelect(pondId);
        else if (!selectorActive) selectPond(pondId);
      }}
      style={{
        width: '100%',
        height: '100%',
        // Draws (ingress) and Spouts (egress) cross the Catchment boundary → dashed, vs a solid Pond.
        border: `2px ${boundary ? 'dashed' : 'solid'} ${borderColor}`,
        boxShadow: highlighted
          ? `0 0 0 3px ${THEME_BRAND}`
          : rebuildPending
            ? `0 0 0 2px #ee9333aa`  // pending refresh — an amber hint
            : isSelected ? `0 0 0 3px ${borderColor}40` : undefined,
        borderRadius: radius,
        background: nodeFill(borderColor),
        cursor: 'pointer',
        position: 'relative',
        boxSizing: 'border-box',
        userSelect: 'none',
      }}
    >
      <Handle id="in" type="target" position={vertical ? Position.Top : Position.Left} style={{ background: '#52525b' }} />

      {pond.isApp && info?.app && (
        <span
          role="button"
          title="Open App"
          onClick={(e) => {
            e.stopPropagation();
            openApp(info.app!.path);
          }}
          style={{
            position: 'absolute', top: 7, right: 9, zIndex: 1, lineHeight: 0, padding: 3, borderRadius: 5,
            cursor: 'pointer', color: borderColor, background: '#0f0f14cc', opacity: 0.9,
          }}
        >
          <OpenIcon />
        </span>
      )}

      {!pond.isApp && (hasTables || hasObjects) && (
        <span
          role="button"
          title="View data"
          onClick={(e) => {
            e.stopPropagation();
            openDataViewer(pondId);
          }}
          style={{
            position: 'absolute', top: 7, right: 9, zIndex: 1, lineHeight: 0, padding: 3, borderRadius: 5,
            cursor: 'pointer', color: borderColor, background: '#0f0f14cc', opacity: 0.9,
          }}
        >
          <TableIcon />
        </span>
      )}

      <div
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          right: 0,
          height: 64,
          padding: '6px 12px',
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'center',
          gap: 5,
          // No divider when there's no ripple area beneath to separate — a Draw/Spout, or a collapsed Pond.
          borderBottom: boundary || collapsed || pond.isApp ? 'none' : `1px solid ${borderColor}30`,
        }}
      >
        <span style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
          {hasRipples && (
            <span
              role="button"
              title={collapsed ? 'Expand ripples' : 'Collapse ripples'}
              onClick={(e) => {
                e.stopPropagation();
                toggleCollapse(pondId);
              }}
              style={{ fontSize: 10, color: '#a1a1aa', cursor: 'pointer', flexShrink: 0, userSelect: 'none' }}
            >
              {collapsed ? '▸' : '▾'}
            </span>
          )}
          {(pond.isDraw || pond.isSpout) && (
            <span style={{ fontSize: 10, fontWeight: 700, color: borderColor, letterSpacing: '0.06em', flexShrink: 0 }}>
              {pond.isDraw ? '[DRAW]' : '[SPOUT]'}
            </span>
          )}
          {pond.dbt && <DbtIcon size={15} />}
          <span style={{ fontSize: 13, fontWeight: 700, color: '#e4e4e7', letterSpacing: '0.04em', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {displayName}
          </span>
          {info && <span style={{ fontSize: 10, color: '#52525b', whiteSpace: 'nowrap' }}>v{info.version}</span>}
        </span>
        <span style={{ fontSize: 11, color: '#71717a', display: 'flex', alignItems: 'center', gap: 6 }}>
          <DemandIndicators demandF={view.demandF} now={now} />
          {app ? (
            // An App's Runs are passes, so run counts mean nothing: its instance state and data freshness instead.
            <>
              <span style={{ color: APP_STATE_COLOR[app] === '#71717a' ? '#a1a1aa' : APP_STATE_COLOR[app] }}>{app}</span>
              <span>✓{formatAge(view.endF, now)}</span>
            </>
          ) : (
            <>
              <span style={{ color: '#a1a1aa' }}>↑{formatAge(view.startF, now)} ({view.runsStarted})</span>
              <span>✓{formatAge(view.endF, now)} ({view.runsCompleted})</span>
            </>
          )}
        </span>
      </div>

      {demandLines.length > 0 && (
        <div
          title={demandLines.join('\n')}
          style={{
            position: 'absolute',
            left: 0,
            right: 0,
            bottom: 0,
            height: demandStripHeight(demandLines.length),
            boxSizing: 'border-box',
            padding: `${STRIP_PAD}px 12px`,
            borderTop: `1px dashed ${THEME_DEMAND}80`,
            borderRadius: `0 0 ${radius - 2}px ${radius - 2}px`,
            background: `${THEME_DEMAND}12`,
          }}
        >
          {demandLines.map((line) => (
            <div
              key={line}
              style={{
                height: STRIP_LINE_H, lineHeight: `${STRIP_LINE_H}px`, fontSize: 11, fontWeight: 600, color: THEME_DEMAND,
                whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
              }}
            >
              {line}
            </div>
          ))}
        </div>
      )}

      <Handle id="out" type="source" position={vertical ? Position.Bottom : Position.Right} style={{ background: '#52525b' }} />
    </div>
  );
});
