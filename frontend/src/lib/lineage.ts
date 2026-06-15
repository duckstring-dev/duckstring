import dagre from '@dagrejs/dagre';
import type { Node, Edge } from '@xyflow/react';
import type { ViewPayload } from './types';

// Lays out the upstream-lineage overlay: each upstream Catchment as a labelled container (group)
// node, its Ponds inside (read-only boxes), positioned to the upstream side of the local graph and
// ordered by duct depth, with cross-container duct edges into the local Draw nodes. The local
// Catchment is rendered by the normal (status-driven) layout; this only adds the remote side.

const RP_W = 184;
const RP_H = 52;
const PAD_TOP = 32; // container header band
const PAD_SIDE = 16;
const PAD_BOT = 16;
const STACK_GAP = 44; // between stacked containers
const AWAY = 140; // distance of the remote column/row from the local graph origin

interface Built {
  id: string;
  name: string | null;
  reachable: boolean;
  depth: number;
  w: number;
  h: number;
  pondPos: Record<string, { x: number; y: number }>;
  ponds: ViewPayload['catchments'][number]['ponds'];
  edges: [string, string][];
}

function depths(view: ViewPayload, selfId: string | null): Record<string, number> {
  const depth: Record<string, number> = {};
  if (selfId) depth[selfId] = 0;
  let changed = true;
  while (changed) {
    changed = false;
    for (const e of view.duct_edges) {
      const tc = e.to.catchment;
      const fc = e.from.catchment;
      if (tc && fc && depth[tc] !== undefined) {
        const d = depth[tc] + 1;
        if (depth[fc] === undefined || d > depth[fc]) {
          depth[fc] = d;
          changed = true;
        }
      }
    }
  }
  return depth;
}

export function computeLineage(
  view: ViewPayload | null,
  selfId: string | null,
  direction: 'LR' | 'TB'
): { nodes: Node[]; edges: Edge[] } {
  if (!view) return { nodes: [], edges: [] };
  const remote = view.catchments.filter((c) => c.id && c.id !== selfId);
  if (remote.length === 0) return { nodes: [], edges: [] };

  const depth = depths(view, selfId);
  const rendered = new Set(remote.map((c) => c.id as string));

  // Pass 1: lay out each Catchment's Ponds and size its container.
  const built: Built[] = remote.map((c) => {
    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: direction, ranksep: 48, nodesep: 28, marginx: 0, marginy: 0 });
    g.setDefaultEdgeLabel(() => ({}));
    const ids = new Set(c.ponds.map((p) => p.id));
    for (const p of c.ponds) g.setNode(p.id, { width: RP_W, height: RP_H });
    for (const [src, snk] of c.edges) if (ids.has(src) && ids.has(snk)) g.setEdge(src, snk);
    dagre.layout(g);
    const pondPos: Record<string, { x: number; y: number }> = {};
    let maxX = 0;
    let maxY = 0;
    for (const p of c.ponds) {
      const n = g.node(p.id);
      const x = (n?.x ?? RP_W / 2) - RP_W / 2;
      const y = (n?.y ?? RP_H / 2) - RP_H / 2;
      pondPos[p.id] = { x: x + PAD_SIDE, y: y + PAD_TOP };
      maxX = Math.max(maxX, x + RP_W);
      maxY = Math.max(maxY, y + RP_H);
    }
    return {
      id: c.id as string,
      name: c.name,
      reachable: c.reachable,
      depth: depth[c.id as string] ?? 1,
      w: (maxX || RP_W) + PAD_SIDE * 2,
      h: (maxY || RP_H) + PAD_TOP + PAD_BOT,
      pondPos,
      ponds: c.ponds,
      edges: c.edges,
    };
  });

  // Pass 2: place containers — ordered by depth (shallowest nearest the local graph), stacked along
  // the cross axis. LR puts them in a column to the left; TB in a row above.
  const vertical = direction === 'TB';
  built.sort((a, b) => a.depth - b.depth || a.id.localeCompare(b.id));
  const maxW = Math.max(...built.map((b) => b.w));
  const maxH = Math.max(...built.map((b) => b.h));
  let cursor = 0;

  const nodes: Node[] = [];
  const edges: Edge[] = [];

  for (const b of built) {
    const pos = vertical
      ? { x: cursor, y: -(AWAY + maxH) }
      : { x: -(AWAY + maxW), y: cursor };
    cursor += (vertical ? b.w : b.h) + STACK_GAP;

    nodes.push({
      id: `cat:${b.id}`,
      type: 'catchmentGroup',
      position: pos,
      data: { name: b.name, reachable: b.reachable },
      style: { width: b.w, height: b.h },
      draggable: false,
      selectable: false,
    });
    for (const p of b.ponds) {
      nodes.push({
        id: `${b.id}::${p.id}`,
        type: 'remotePond',
        parentId: `cat:${b.id}`,
        extent: 'parent',
        position: b.pondPos[p.id],
        data: { name: p.name, catchmentId: b.id, pondId: p.id, isDraw: p.is_draw, vertical },
        draggable: false,
        style: { width: RP_W, height: RP_H },
      });
    }
    for (const [src, snk] of b.edges) {
      edges.push({
        id: `le:${b.id}:${src}->${snk}`,
        source: `${b.id}::${src}`,
        target: `${b.id}::${snk}`,
        style: { stroke: '#3f3f46' },
      });
    }
  }

  // Cross-container duct edges: upstream source Pond → consumer's Draw node.
  for (const e of view.duct_edges) {
    const fc = e.from.catchment;
    if (!fc || !rendered.has(fc)) continue; // unknown / unrendered upstream
    const source = `${fc}::${e.from.pond}`;
    const target = e.to.catchment === selfId ? e.to.pond : `${e.to.catchment}::${e.to.pond}`;
    edges.push({
      id: `de:${fc}:${e.from.pond}->${e.to.catchment}:${e.to.pond}`,
      source,
      target,
      animated: true,
      style: { stroke: '#52525b', strokeDasharray: '4 3' },
    });
  }

  return { nodes, edges };
}
