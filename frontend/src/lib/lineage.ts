import dagre from '@dagrejs/dagre';
import type { Node, Edge } from '@xyflow/react';
import type { ViewPayload, ViewPond } from './types';

// Builds the upstream-lineage overlay: each upstream Catchment as a labelled container (group) node,
// its Ponds inside (read-only boxes), with cross-container duct edges into the consumer's Draw nodes.
// Placement is NOT done here — the boxes are laid out *together with* the local graph by a single
// dagre pass in computeLayout (each box is a node, the duct edges give it its rank), so a Pond that
// feeds an upstream box sits on the correct side of it. computeRemote produces the box internals +
// resolved edges; assembleRemote turns them into React Flow nodes once positions are known.

const RP_W = 184;
const RP_H = 52;
const PAD_TOP = 32; // container header band
const PAD_SIDE = 16;
const PAD_BOT = 16;

// A box-internal layout + sizing, position-free (the global pass places it).
export interface RemoteBox {
  id: string;
  name: string | null;
  reachable: boolean;
  w: number;
  h: number;
  pondPos: Record<string, { x: number; y: number }>; // box-relative top-left per Pond
  ponds: ViewPond[];
  edges: [string, string][]; // intra-Catchment edges, filtered to visible Ponds
}

// A duct edge resolved to node ids: `rf*` are React Flow node ids (a local Pond, or `cat::pond` in a
// box); `dagre*` collapse the box side to its container node `cat:{id}` so the global pass can rank
// boxes against local Ponds.
export interface RemoteDuctEdge {
  id: string;
  rfSource: string;
  rfTarget: string;
  dagreSource: string;
  dagreTarget: string;
}

export interface RemoteModel {
  boxes: RemoteBox[];
  ductEdges: RemoteDuctEdge[];
}

export function computeRemote(
  view: ViewPayload | null,
  selfId: string | null,
  direction: 'LR' | 'TB'
): RemoteModel {
  if (!view) return { boxes: [], ductEdges: [] };
  const remote = view.catchments.filter((c) => c.id && c.id !== selfId);
  if (remote.length === 0) return { boxes: [], ductEdges: [] };
  const rendered = new Set(remote.map((c) => c.id as string));

  // A Draw only shows if a Pond in its own Catchment sources from it (it appears as an edge source) —
  // an unconsumed Draw is noise. Computed per Catchment (incl. the local one) so the cross-Catchment
  // duct edges can drop the ones that target a hidden Draw.
  const shownDraws: Record<string, Set<string>> = {};
  for (const c of view.catchments) {
    if (!c.id) continue;
    const draws = new Set(c.ponds.filter((p) => p.is_draw).map((p) => p.id));
    const shown = new Set<string>();
    for (const [src] of c.edges) if (draws.has(src)) shown.add(src);
    shownDraws[c.id] = shown;
  }
  const visible = (c: ViewPayload['catchments'][number]) =>
    c.ponds.filter((p) => !p.is_draw || (c.id ? shownDraws[c.id]?.has(p.id) : false));

  // Box internals: a dagre layout of each Catchment's visible Ponds, sized to fit.
  const boxes: RemoteBox[] = remote.map((c) => {
    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: direction, ranksep: 48, nodesep: 28, marginx: 0, marginy: 0 });
    g.setDefaultEdgeLabel(() => ({}));
    const ponds = visible(c);
    const ids = new Set(ponds.map((p) => p.id));
    for (const p of ponds) g.setNode(p.id, { width: RP_W, height: RP_H });
    for (const [src, snk] of c.edges) if (ids.has(src) && ids.has(snk)) g.setEdge(src, snk);
    dagre.layout(g);
    const pondPos: Record<string, { x: number; y: number }> = {};
    let maxX = 0;
    let maxY = 0;
    for (const p of ponds) {
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
      w: (maxX || RP_W) + PAD_SIDE * 2,
      h: (maxY || RP_H) + PAD_TOP + PAD_BOT,
      pondPos,
      ponds,
      edges: c.edges.filter(([s, t]) => ids.has(s) && ids.has(t)),
    };
  });

  // Resolve each duct edge to node ids. Either endpoint may be the local Catchment (a mesh duct draws
  // *from* the local graph too). Skip an edge whose target Draw was hidden as unconsumed.
  const resolve = (c: string, p: string) =>
    c === selfId ? { rf: p, dagre: p } : { rf: `${c}::${p}`, dagre: `cat:${c}` };
  const ductEdges: RemoteDuctEdge[] = [];
  for (const e of view.duct_edges) {
    const fc = e.from.catchment;
    const tc = e.to.catchment;
    if (!fc || !tc) continue;
    const fromOk = fc === selfId || rendered.has(fc);
    const toOk = tc === selfId || rendered.has(tc);
    if (!fromOk || !toOk) continue;
    if (!shownDraws[tc]?.has(e.to.pond)) continue;
    const s = resolve(fc, e.from.pond);
    const t = resolve(tc, e.to.pond);
    ductEdges.push({
      id: `de:${fc}:${e.from.pond}->${tc}:${e.to.pond}`,
      rfSource: s.rf,
      rfTarget: t.rf,
      dagreSource: s.dagre,
      dagreTarget: t.dagre,
    });
  }

  return { boxes, ductEdges };
}

// Turn the box internals + duct edges into React Flow nodes/edges, given each box's top-left position
// (computed by the global dagre pass in computeLayout). Boxes without a position are dropped.
export function assembleRemote(
  boxes: RemoteBox[],
  ductEdges: RemoteDuctEdge[],
  boxPos: Record<string, { x: number; y: number }>,
  vertical: boolean
): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  for (const b of boxes) {
    const pos = boxPos[b.id];
    if (!pos) continue;
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

  for (const de of ductEdges) {
    edges.push({
      id: de.id,
      source: de.rfSource,
      target: de.rfTarget,
      animated: true,
      style: { stroke: '#52525b', strokeDasharray: '4 3' },
    });
  }

  return { nodes, edges };
}
