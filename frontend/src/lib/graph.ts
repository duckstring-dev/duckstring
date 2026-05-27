import type { PondId, RippleId, Pond, Ripple } from './types';

export function getRoots(pondId: PondId, ripples: Record<RippleId, Ripple>): Ripple[] {
  const inPond = Object.values(ripples).filter((r) => r.pondId === pondId);
  return inPond.filter((r) => !r.parents.some((pid) => ripples[pid]?.pondId === pondId));
}

export function getLeaves(pondId: PondId, ripples: Record<RippleId, Ripple>): Ripple[] {
  const inPond = Object.values(ripples).filter((r) => r.pondId === pondId);
  const childIds = new Set(
    inPond.flatMap((r) => r.parents.filter((pid) => ripples[pid]?.pondId === pondId))
  );
  return inPond.filter((r) => !childIds.has(r.id));
}

// Returns true if adding edge from→to would create a cycle in the Pond graph.
// "from" is the new source pond, "to" is the new sink pond.
// A cycle exists if "from" is already reachable from "to" via existing sources.
export function hasCyclePond(
  ponds: Record<PondId, Pond>,
  fromId: PondId,
  toId: PondId
): boolean {
  // Adding edge: toId.sources will include fromId.
  // Cycle if fromId is reachable from toId via existing sources (before we add the edge).
  // i.e., can we reach fromId by following sources starting from toId?
  return isReachablePond(ponds, toId, fromId, new Set());
}

function isReachablePond(
  ponds: Record<PondId, Pond>,
  startId: PondId,
  targetId: PondId,
  visited: Set<PondId>
): boolean {
  if (startId === targetId) return true;
  if (visited.has(startId)) return false;
  visited.add(startId);
  const pond = ponds[startId];
  if (!pond) return false;
  return pond.sources.some((sid) => isReachablePond(ponds, sid, targetId, visited));
}

// Returns true if adding parentId as parent of childId would create a cycle in the Ripple graph.
export function hasCycleRipple(
  ripples: Record<RippleId, Ripple>,
  parentId: RippleId,
  childId: RippleId
): boolean {
  // Adding edge: childId.parents will include parentId.
  // Cycle if childId is reachable from parentId via existing parents.
  return isReachableRipple(ripples, parentId, childId, new Set());
}

function isReachableRipple(
  ripples: Record<RippleId, Ripple>,
  startId: RippleId,
  targetId: RippleId,
  visited: Set<RippleId>
): boolean {
  if (startId === targetId) return true;
  if (visited.has(startId)) return false;
  visited.add(startId);
  const ripple = ripples[startId];
  if (!ripple) return false;
  return ripple.parents.some((pid) => isReachableRipple(ripples, pid, targetId, visited));
}

// Returns true if sinkPondId already has fromPondId as a source (direct or indirect)
// Used to check if a Pond is "downstream" of another.
export function isPondDownstreamOf(
  ponds: Record<PondId, Pond>,
  sinkPondId: PondId,
  sourcePondId: PondId
): boolean {
  return isReachablePond(ponds, sinkPondId, sourcePondId, new Set());
}
