'use client';

import { useEffect, useRef, useState } from 'react';
import { MARK_COLOURS, type CellMark } from './theme';

// A table drawn as one square per row, coloured by what the run did to it. Drawn on a canvas: the pipeline's
// tables run to a few thousand rows.
export function CellGrid({ marks, label, cell = 4, gap = 1 }: { marks: CellMark[]; label: string; cell?: number; gap?: number }) {
  const wrap = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const pitch = cell + gap;
  const cols = Math.max(1, Math.floor((width + gap) / pitch));
  const rows = Math.max(1, Math.ceil(marks.length / cols));
  const height = rows * pitch - gap;

  useEffect(() => {
    const c = canvas.current;
    if (!c || width === 0) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = Math.round(width * dpr);
    c.height = Math.round(height * dpr);
    const ctx = c.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    marks.forEach((m, i) => {
      ctx.fillStyle = MARK_COLOURS[m];
      ctx.fillRect((i % cols) * pitch, Math.floor(i / cols) * pitch, cell, cell);
    });
  }, [marks, width, height, cols, pitch, cell]);

  return (
    <div ref={wrap} style={{ width: '100%' }}>
      <canvas ref={canvas} role="img" aria-label={label} style={{ display: 'block', width, height }} />
    </div>
  );
}
