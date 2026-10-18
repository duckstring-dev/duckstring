'use client';

import { THEME_BRAND, THEME_SUCCESS } from '@/lib/store';
import { FAINT, MONO, MUTED, TEXT } from './theme';

// A join after one run, as an area: the order lines across (A′ = A + ΔA), the catalog down (B′ = B + ΔB).
// Step 0 is the whole join, 1 cuts it by what changed into four quadrants, 2 skips the stored A ⋈ B, and 3 is
// how the engine evaluates the rest: two strips over the current tables, minus their shared corner.

export const JOIN_EQS = [
  'A′ ⋈ B′',
  'A ⋈ B  +  ΔA ⋈ B  +  A ⋈ ΔB  +  ΔA ⋈ ΔB',
  'Δ(A ⋈ B) = ΔA ⋈ B + A ⋈ ΔB + ΔA ⋈ ΔB',
  'Δ(A ⋈ B) = ΔA ⋈ B′ + A′ ⋈ ΔB − ΔA ⋈ ΔB',
];

// Geometry (viewBox units). Changes are drawn far larger than they would be.
const X0 = 64, Y0 = 46, AW = 190, DAW = 52, BH = 150, DBH = 46;
const W = X0 + AW + DAW + 8, H = Y0 + BH + DBH + 10;
const T = 'all 450ms ease';

export function JoinGrid({ step }: { step: number }) {
  const eq = JOIN_EQS[step];
  const xA = X0 + AW, yB = Y0 + BH;
  const cut = step >= 1;
  const skip = step >= 2;
  const strips = step >= 3;
  // Each quadrant's fill: the whole join in brand cyan, then the three change quadrants green once A ⋈ B is
  // skipped. With the strips, the corner is the overlap.
  const whole = { fill: THEME_BRAND, fillOpacity: 0.28 };
  const change = { fill: THEME_SUCCESS, fillOpacity: 0.32 };
  const stored = { fill: '#3f3f46', fillOpacity: 1 };
  const quad = (q: 'AB' | 'dAB' | 'AdB' | 'dAdB') => (q === 'AB' ? (skip ? stored : whole) : skip ? change : whole);
  return (
    <div>
      <div data-testid="join-eq" style={{ fontFamily: MONO, fontSize: 12, color: TEXT, minHeight: 18, marginBottom: 8, overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>
        {eq}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', maxWidth: 380, maxHeight: '34dvh', display: 'block', margin: '0 auto' }} role="img"
        aria-label={`The join as a grid: ${eq}`}>
        <defs>
          <pattern id="twice" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="6" height="6" fill={THEME_SUCCESS} fillOpacity={0.45} />
            <line x1="0" y1="0" x2="0" y2="6" stroke="#ffffff" strokeOpacity={0.6} strokeWidth="2" />
          </pattern>
        </defs>
        {/* axes: one span each until the cut, then previous state and change */}
        {cut ? (
          <>
            <Brace x1={X0} x2={xA} y={Y0 - 8} label="A" sub="order lines" />
            <Brace x1={xA} x2={xA + DAW} y={Y0 - 8} label="ΔA" sub="changes" />
            <VBrace y1={Y0} y2={yB} x={X0 - 8} label="B" sub="catalog" />
            <VBrace y1={yB} y2={yB + DBH} x={X0 - 8} label="ΔB" sub="changes" />
          </>
        ) : (
          <>
            <Brace x1={X0} x2={xA + DAW} y={Y0 - 8} label="A′" sub="order lines" />
            <VBrace y1={Y0} y2={yB + DBH} x={X0 - 8} label="B′" sub="catalog" />
          </>
        )}

        <rect x={X0} y={Y0} width={AW} height={BH} shapeRendering="crispEdges" style={{ transition: T }} {...quad('AB')} />
        <rect x={xA} y={Y0} width={DAW} height={BH} shapeRendering="crispEdges" style={{ transition: T }} {...quad('dAB')} />
        <rect x={X0} y={yB} width={AW} height={DBH} shapeRendering="crispEdges" style={{ transition: T }} {...quad('AdB')} />
        <rect x={xA} y={yB} width={DAW} height={DBH} shapeRendering="crispEdges" style={{ transition: T }} {...(strips ? { fill: 'url(#twice)', fillOpacity: 1 } : quad('dAdB'))} />

        {/* the cut lines */}
        <line x1={xA} y1={Y0} x2={xA} y2={yB + DBH} stroke="#ffffff" strokeWidth={1.5} strokeDasharray="4 3" style={{ transition: T, opacity: cut ? 0.8 : 0 }} />
        <line x1={X0} y1={yB} x2={xA + DAW} y2={yB} stroke="#ffffff" strokeWidth={1.5} strokeDasharray="4 3" style={{ transition: T, opacity: cut ? 0.8 : 0 }} />

        {/* the strips the engine evaluates */}
        <rect x={xA} y={Y0} width={DAW} height={BH + DBH} fill="none" stroke={THEME_BRAND} strokeWidth={2} style={{ transition: T, opacity: strips ? 1 : 0 }} />
        <rect x={X0} y={yB} width={AW + DAW} height={DBH} fill="none" stroke={THEME_BRAND} strokeWidth={2} style={{ transition: T, opacity: strips ? 1 : 0 }} />

        {/* labels */}
        {!cut && (
          <>
            <text x={X0 + (AW + DAW) / 2} y={Y0 + (BH + DBH) / 2 - 4} textAnchor="middle" fill={TEXT} fontFamily={MONO} fontSize={13}>A′ ⋈ B′</text>
            <text x={X0 + (AW + DAW) / 2} y={Y0 + (BH + DBH) / 2 + 14} textAnchor="middle" fill={MUTED} fontFamily={MONO} fontSize={10}>every pair, recomputed</text>
          </>
        )}
        {cut && (
          <>
            <text x={X0 + AW / 2} y={Y0 + BH / 2 - 4} textAnchor="middle" fill={TEXT} fontFamily={MONO} fontSize={13}>A ⋈ B</text>
            <text x={X0 + AW / 2} y={Y0 + BH / 2 + 14} textAnchor="middle" fill={MUTED} fontFamily={MONO} fontSize={10}>
              {skip ? 'last run’s output: skipped' : 'the previous states'}
            </text>
            <text transform={`translate(${xA + DAW / 2 + 4}, ${Y0 + BH / 2}) rotate(-90)`} textAnchor="middle" fill={TEXT} fontFamily={MONO} fontSize={11}>
              {strips ? 'ΔA ⋈ B′' : 'ΔA ⋈ B'}
            </text>
            <text x={X0 + AW / 2} y={yB + DBH / 2 + 4} textAnchor="middle" fill={TEXT} fontFamily={MONO} fontSize={11}>
              {strips ? 'A′ ⋈ ΔB' : 'A ⋈ ΔB'}
            </text>
            <text x={xA + DAW / 2} y={yB + DBH / 2 + 4} textAnchor="middle" fill="#ffffff" fontFamily={MONO} fontSize={strips ? 12 : 9} fontWeight={strips ? 700 : 400}>
              {strips ? '×2 −1' : 'ΔA⋈ΔB'}
            </text>
          </>
        )}
      </svg>
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
