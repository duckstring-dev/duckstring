'use client';

import { useState } from 'react';
import { THEME_BRAND, THEME_SUCCESS } from '@/lib/store';
import { pill } from './ZSetFigure';
import { BORDER, FAINT, MONO, MUTED, PANEL, TEXT } from './theme';

// A join after one run, as an area: the order lines (A′ = A + ΔA) across, the catalog (B′ = B + ΔB) down.
// Stepping through adds the engine's terms one at a time.

const STEPS = [
  {
    eq: 'Δ(A ⋈ B) = ?',
    text: "Last run's output, A ⋈ B, is already stored. None of it is computed again.",
  },
  {
    eq: 'Δ(A ⋈ B) = ΔA ⋈ B′',
    text: 'Join the new order lines, ΔA, to the whole catalog as it is now, B′.',
  },
  {
    eq: 'Δ(A ⋈ B) = ΔA ⋈ B′ + A′ ⋈ ΔB',
    text: 'Join all the order lines as they are now, A′, to the catalog changes, ΔB. The corner, ΔA ⋈ ΔB, is now counted twice.',
  },
  {
    eq: 'Δ(A ⋈ B) = ΔA ⋈ B′ + A′ ⋈ ΔB − ΔA ⋈ ΔB',
    text: "Subtract the corner once. What's left is exactly the change to the output, worked out from the tables as they are now.",
  },
];

// Geometry (viewBox units). Changes are drawn far larger than they would be.
const X0 = 64, Y0 = 46, AW = 190, DAW = 52, BH = 150, DBH = 46;
const W = X0 + AW + DAW + 8, H = Y0 + BH + DBH + 10;

export function JoinGrid() {
  const [step, setStep] = useState(0);
  const s = STEPS[step];
  const xA = X0 + AW, yB = Y0 + BH;
  return (
    <div style={{ background: PANEL, border: `1px solid ${BORDER}`, borderRadius: 8, padding: 14 }}>
      <div data-testid="join-eq" style={{ fontFamily: MONO, fontSize: 13, color: TEXT, minHeight: 20, marginBottom: 10, overflowWrap: 'anywhere' }}>
        {s.eq}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', maxWidth: 440, display: 'block', margin: '0 auto' }} role="img"
        aria-label={`The join as a grid, step ${step + 1} of ${STEPS.length}: ${s.eq}`}>
        <defs>
          <pattern id="twice" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="6" height="6" fill={THEME_SUCCESS} fillOpacity={0.35} />
            <line x1="0" y1="0" x2="0" y2="6" stroke={THEME_BRAND} strokeWidth="3" />
          </pattern>
        </defs>
        {/* axes */}
        <Brace x1={X0} x2={xA} y={Y0 - 8} label="A" sub="order lines" />
        <Brace x1={xA} x2={xA + DAW} y={Y0 - 8} label="ΔA" sub="new" />
        <VBrace y1={Y0} y2={yB} x={X0 - 8} label="B" sub="catalog" />
        <VBrace y1={yB} y2={yB + DBH} x={X0 - 8} label="ΔB" sub="changed" />

        {/* last run's output */}
        <rect x={X0} y={Y0} width={AW} height={BH} fill="#3f3f46" />
        <text x={X0 + AW / 2} y={Y0 + BH / 2 - 4} textAnchor="middle" fill={TEXT} fontFamily={MONO} fontSize={13}>A ⋈ B</text>
        <text x={X0 + AW / 2} y={Y0 + BH / 2 + 14} textAnchor="middle" fill={MUTED} fontFamily={MONO} fontSize={10}>stored from last run</text>

        {/* the outline of what a run adds */}
        <rect x={xA} y={Y0} width={DAW} height={BH + DBH} fill="none" stroke={BORDER} strokeDasharray="3 3" />
        <rect x={X0} y={yB} width={AW + DAW} height={DBH} fill="none" stroke={BORDER} strokeDasharray="3 3" />

        {step >= 1 && (
          <g>
            <rect x={xA} y={Y0} width={DAW} height={BH + DBH} fill={THEME_SUCCESS} fillOpacity={0.35} stroke={THEME_SUCCESS} />
            <text transform={`translate(${xA + DAW / 2 + 4}, ${Y0 + BH / 2}) rotate(-90)`} textAnchor="middle" fill={TEXT} fontFamily={MONO} fontSize={11}>
              ΔA ⋈ B′
            </text>
          </g>
        )}
        {step >= 2 && (
          <g>
            <rect x={X0} y={yB} width={AW + DAW} height={DBH} fill={THEME_BRAND} fillOpacity={0.35} stroke={THEME_BRAND} />
            <text x={X0 + AW / 2} y={yB + DBH / 2 + 4} textAnchor="middle" fill={TEXT} fontFamily={MONO} fontSize={11}>A′ ⋈ ΔB</text>
          </g>
        )}
        {step >= 2 && (
          <g>
            <rect x={xA} y={yB} width={DAW} height={DBH} fill={step === 2 ? 'url(#twice)' : '#2f6f63'} stroke={step === 2 ? '#ffffff' : 'none'} strokeWidth={1.5} />
            <text x={xA + DAW / 2} y={yB + DBH / 2 + 4} textAnchor="middle" fill="#ffffff" fontFamily={MONO} fontSize={12} fontWeight={700}>
              {step === 2 ? '×2' : '×1'}
            </text>
          </g>
        )}
      </svg>
      <p style={{ margin: '10px 0 12px', color: MUTED, fontSize: 14, minHeight: 42 }}>{s.text}</p>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <button onClick={() => setStep((n) => Math.max(0, n - 1))} disabled={step === 0} style={{ ...pill(false), opacity: step === 0 ? 0.4 : 1 }}>
          Back
        </button>
        <button data-testid="join-next" onClick={() => setStep((n) => Math.min(STEPS.length - 1, n + 1))} disabled={step === STEPS.length - 1}
          style={{ ...pill(true), opacity: step === STEPS.length - 1 ? 0.4 : 1 }}>
          Next
        </button>
        <span style={{ marginLeft: 'auto', fontFamily: MONO, fontSize: 11, color: FAINT }}>
          {step + 1} / {STEPS.length}
        </span>
      </div>
    </div>
  );
}

function Brace({ x1, x2, y, label, sub }: { x1: number; x2: number; y: number; label: string; sub: string }) {
  return (
    <g>
      <path d={`M${x1 + 1},${y} v-5 H${x2 - 1} v5`} fill="none" stroke={FAINT} />
      <text x={(x1 + x2) / 2} y={y - 22} textAnchor="middle" fill={TEXT} fontFamily={MONO} fontSize={12}>{label}</text>
      <text x={(x1 + x2) / 2} y={y - 10} textAnchor="middle" fill={FAINT} fontFamily={MONO} fontSize={9}>{sub}</text>
    </g>
  );
}

function VBrace({ y1, y2, x, label, sub }: { y1: number; y2: number; x: number; label: string; sub: string }) {
  return (
    <g>
      <path d={`M${x},${y1 + 1} h-5 V${y2 - 1} h5`} fill="none" stroke={FAINT} />
      <text x={x - 9} y={(y1 + y2) / 2 - 2} textAnchor="end" fill={TEXT} fontFamily={MONO} fontSize={12}>{label}</text>
      <text x={x - 9} y={(y1 + y2) / 2 + 10} textAnchor="end" fill={FAINT} fontFamily={MONO} fontSize={9}>{sub}</text>
    </g>
  );
}
