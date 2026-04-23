import { Graph as DagreGraph, layout as dagreLayout } from '@dagrejs/dagre';
import type { Node, Edge } from '@xyflow/react';
import type { BPMNNode, BPMNEdge, ProcessGraph, LaneLayout } from './types';

// ── Sizing constants ───────────────────────────────────────────────────────────
const CHAR_W = 7.5;      // px per character (monospace approximation)
const CHAR_H = 16;       // px per line
const H_PAD = 24;        // horizontal padding inside a node box
const V_PAD = 16;        // vertical padding inside a node box
const MAX_TEXT_W = 200;  // wrap at this width
const MIN_TASK_W = 140;
const MIN_TASK_H = 50;
const GATEWAY_SIZE = 44;
const EVENT_SIZE = 36;

const LANE_HEADER_W = 120;   // left strip width for lane label
const LANE_PAD_TOP = 40;     // space above first node in lane
const LANE_PAD_BOTTOM = 40;
const LANE_PAD_LEFT = 40;
const LANE_PAD_RIGHT = 40;
const POOL_HEADER_H = 40;    // top strip height for pool/process title

// ── Text measurement ───────────────────────────────────────────────────────────

function measureText(text: string): { width: number; height: number } {
  if (!text) return { width: MIN_TASK_W, height: MIN_TASK_H };

  const words = text.split(' ');
  const lineWidth = MAX_TEXT_W - H_PAD * 2;
  let currentLine = '';
  let lines = 1;

  for (const word of words) {
    const testLine = currentLine ? `${currentLine} ${word}` : word;
    if (testLine.length * CHAR_W > lineWidth && currentLine) {
      lines++;
      currentLine = word;
    } else {
      currentLine = testLine;
    }
  }

  const textW = Math.min(text.length * CHAR_W, lineWidth);
  const width = Math.max(MIN_TASK_W, textW + H_PAD * 2);
  const height = Math.max(MIN_TASK_H, lines * CHAR_H + V_PAD * 2);
  return { width, height };
}

function nodeSize(node: BPMNNode): { width: number; height: number } {
  switch (node.bpmn_type) {
    case 'START_EVENT':
    case 'END_EVENT':
    case 'BOUNDARY_EVENT':
      return { width: EVENT_SIZE, height: EVENT_SIZE };
    case 'GATEWAY':
      return { width: GATEWAY_SIZE, height: GATEWAY_SIZE };
    case 'TASK':
    case 'SUBPROCESS':
    default:
      return measureText(node.label);
  }
}

// ── Effective actor resolution ────────────────────────────────────────────────
// Nodes without an actor (start/end events, converging gateways) inherit from
// the nearest neighbour in the graph — same logic as the Python L8 resolver.

function resolveEffectiveActors(
  nodes: BPMNNode[],
  edges: BPMNEdge[],
  laneOrder: string[],
): Map<string, string> {
  const laneSet = new Set(laneOrder);
  const effective = new Map<string, string>();
  const succs = new Map<string, string[]>();
  const preds = new Map<string, string[]>();

  for (const n of nodes) {
    succs.set(n.node_id, []);
    preds.set(n.node_id, []);
  }
  for (const e of edges) {
    succs.get(e.source)?.push(e.target);
    preds.get(e.target)?.push(e.source);
  }

  for (const n of nodes) {
    if (n.actor && laneSet.has(n.actor)) effective.set(n.node_id, n.actor);
  }

  let changed = true;
  while (changed) {
    changed = false;
    for (const n of nodes) {
      if (effective.has(n.node_id)) continue;
      const neighbours = [
        ...(succs.get(n.node_id) ?? []),
        ...(preds.get(n.node_id) ?? []),
      ];
      for (const nb of neighbours) {
        const a = effective.get(nb);
        if (a && laneSet.has(a)) {
          effective.set(n.node_id, a);
          changed = true;
          break;
        }
      }
    }
  }

  if (laneOrder.length > 0) {
    for (const n of nodes) {
      if (!effective.has(n.node_id)) effective.set(n.node_id, laneOrder[0]);
    }
  }
  return effective;
}

// ── Dagre layout per swimlane ─────────────────────────────────────────────────
// Strategy: run one dagre graph *per lane*, all sharing the same horizontal
// column ranks so lanes align vertically. Then stack lanes top-to-bottom.
// Cross-lane edges are added as React Flow edges spanning the lane containers.

export interface LayoutResult {
  nodes: Node[];
  edges: Edge[];
  laneLayouts: LaneLayout[];
  totalWidth: number;
  totalHeight: number;
}

export function computeLayout(proc: ProcessGraph): LayoutResult {
  const laneOrder = Object.keys(proc.actor_to_lane);
  const hasSwimlanes = laneOrder.length > 0;

  const nodeMap = new Map(proc.nodes.map((n) => [n.node_id, n]));
  const sizes = new Map(proc.nodes.map((n) => [n.node_id, nodeSize(n)]));

  if (!hasSwimlanes) {
    return layoutFlat(proc, nodeMap, sizes);
  }

  const effectiveActor = resolveEffectiveActors(proc.nodes, proc.edges, laneOrder);

  // Group nodes by lane
  const laneNodes = new Map<string, BPMNNode[]>();
  for (const actor of laneOrder) laneNodes.set(actor, []);
  for (const n of proc.nodes) {
    const actor = effectiveActor.get(n.node_id) ?? laneOrder[0];
    laneNodes.get(actor)?.push(n);
  }

  // Run one dagre graph PER lane to get internal positions
  const laneGraphs = new Map<string, DagreGraph>();
  const laneInternalLayout = new Map<string, Map<string, { x: number; y: number }>>();

  // We also run a GLOBAL dagre to get consistent X column ranks across lanes
  const globalG = new DagreGraph({ directed: true });
  globalG.setGraph({
    rankdir: 'LR',
    ranksep: 80,
    nodesep: 30,
    edgesep: 20,
    marginx: LANE_PAD_LEFT,
    marginy: LANE_PAD_TOP,
  });
  globalG.setDefaultEdgeLabel(() => ({}));

  for (const n of proc.nodes) {
    const s = sizes.get(n.node_id)!;
    globalG.setNode(n.node_id, { width: s.width, height: s.height, label: n.node_id });
  }
  for (const e of proc.edges) {
    if (nodeMap.has(e.source) && nodeMap.has(e.target)) {
      globalG.setEdge(e.source, e.target);
    }
  }

  dagreLayout(globalG);

  // Extract x positions from global layout (these define the columns)
  const globalX = new Map<string, number>();
  for (const n of proc.nodes) {
    const gn = globalG.node(n.node_id);
    if (gn) globalX.set(n.node_id, gn.x);
  }

  // Per-lane dagre to get Y positions within each lane
  for (const actor of laneOrder) {
    const lNodes = laneNodes.get(actor) ?? [];
    if (lNodes.length === 0) {
      laneGraphs.set(actor, new DagreGraph());
      laneInternalLayout.set(actor, new Map());
      continue;
    }

    const g = new DagreGraph({ directed: true });
    g.setGraph({
      rankdir: 'LR',
      ranksep: 80,
      nodesep: 30,
      edgesep: 20,
      marginx: LANE_PAD_LEFT,
      marginy: LANE_PAD_TOP,
    });
    g.setDefaultEdgeLabel(() => ({}));

    const laneNodeIds = new Set(lNodes.map((n) => n.node_id));

    for (const n of lNodes) {
      const s = sizes.get(n.node_id)!;
      g.setNode(n.node_id, { width: s.width, height: s.height, label: n.node_id });
    }
    // Only intra-lane edges for the per-lane layout
    for (const e of proc.edges) {
      if (laneNodeIds.has(e.source) && laneNodeIds.has(e.target)) {
        g.setEdge(e.source, e.target);
      }
    }

    dagreLayout(g);
    laneGraphs.set(actor, g);

    const posMap = new Map<string, { x: number; y: number }>();
    for (const n of lNodes) {
      const gn = g.node(n.node_id);
      if (gn) posMap.set(n.node_id, { x: gn.x, y: gn.y });
    }
    laneInternalLayout.set(actor, posMap);
  }

  // Compute lane heights from their internal layouts
  const laneHeights = new Map<string, number>();
  for (const actor of laneOrder) {
    const posMap = laneInternalLayout.get(actor)!;
    const lNodes = laneNodes.get(actor) ?? [];
    let maxY = 0;
    for (const n of lNodes) {
      const pos = posMap.get(n.node_id);
      const s = sizes.get(n.node_id)!;
      if (pos) maxY = Math.max(maxY, pos.y + s.height / 2);
    }
    laneHeights.set(actor, Math.max(100, maxY + LANE_PAD_BOTTOM));
  }

  // Compute total width from global layout
  let maxGlobalX = 0;
  for (const n of proc.nodes) {
    const s = sizes.get(n.node_id)!;
    const x = globalX.get(n.node_id) ?? 0;
    maxGlobalX = Math.max(maxGlobalX, x + s.width / 2);
  }
  const contentWidth = maxGlobalX + LANE_PAD_RIGHT;
  const totalWidth = LANE_HEADER_W + contentWidth;

  // Stack lanes and compute Y offsets
  const laneY = new Map<string, number>();
  let cumY = POOL_HEADER_H;
  for (const actor of laneOrder) {
    laneY.set(actor, cumY);
    cumY += laneHeights.get(actor)!;
  }
  const totalHeight = cumY;

  // Build laneLayouts (for rendering lane bands)
  const laneLayouts: LaneLayout[] = laneOrder.map((actor) => ({
    actor,
    slug: proc.actor_to_lane[actor],
    y: laneY.get(actor)!,
    height: laneHeights.get(actor)!,
    width: totalWidth,
  }));

  // Build React Flow nodes
  // Each node's absolute position = LANE_HEADER_W + globalX (column), laneY[actor] + internal y
  const rfNodes: Node[] = proc.nodes.map((n) => {
    const s = sizes.get(n.node_id)!;
    const actor = effectiveActor.get(n.node_id) ?? laneOrder[0];
    const internalPos = laneInternalLayout.get(actor)?.get(n.node_id);
    const gx = globalX.get(n.node_id) ?? 0;

    // Use global X for column alignment, internal Y for row within lane
    const x = LANE_HEADER_W + gx - s.width / 2;
    const y = (laneY.get(actor) ?? 0) + (internalPos ? internalPos.y - s.height / 2 : LANE_PAD_TOP);

    return {
      id: n.node_id,
      type: rfNodeType(n),
      position: { x, y },
      data: {
        label: n.label,
        bpmn_type: n.bpmn_type,
        gateway_type: n.gateway_type,
        gateway_direction: n.gateway_direction,
        needs_review: n.needs_review,
        review_reasons: n.review_reasons,
        actor,
      },
      width: s.width,
      height: s.height,
      style: { width: s.width, height: s.height },
    };
  });

  // Build React Flow edges
  const rfEdges: Edge[] = proc.edges.map((e) => ({
    id: e.edge_id,
    source: e.source,
    target: e.target,
    label: edgeLabel(e),
    type: 'smoothstep',
    style: { strokeWidth: 1.5 },
    markerEnd: { type: 'arrowclosed' } as any,
    data: { is_default: e.is_default },
  }));

  return { nodes: rfNodes, edges: rfEdges, laneLayouts, totalWidth, totalHeight };
}

// ── Flat layout (no swimlanes) ─────────────────────────────────────────────────

function layoutFlat(
  proc: ProcessGraph,
  nodeMap: Map<string, BPMNNode>,
  sizes: Map<string, { width: number; height: number }>,
): LayoutResult {
  const g = new DagreGraph({ directed: true });
  g.setGraph({ rankdir: 'LR', ranksep: 80, nodesep: 30, marginx: 40, marginy: 40 });
  g.setDefaultEdgeLabel(() => ({}));

  for (const n of proc.nodes) {
    const s = sizes.get(n.node_id)!;
    g.setNode(n.node_id, { width: s.width, height: s.height, label: n.node_id });
  }
  for (const e of proc.edges) {
    if (nodeMap.has(e.source) && nodeMap.has(e.target)) {
      g.setEdge(e.source, e.target);
    }
  }
  dagreLayout(g);

  let maxX = 0, maxY = 0;
  const rfNodes: Node[] = proc.nodes.map((n) => {
    const s = sizes.get(n.node_id)!;
    const gn = g.node(n.node_id);
    const x = (gn?.x ?? 0) - s.width / 2;
    const y = (gn?.y ?? 0) - s.height / 2;
    maxX = Math.max(maxX, x + s.width);
    maxY = Math.max(maxY, y + s.height);
    return {
      id: n.node_id,
      type: rfNodeType(n),
      position: { x, y },
      data: {
        label: n.label,
        bpmn_type: n.bpmn_type,
        gateway_type: n.gateway_type,
        gateway_direction: n.gateway_direction,
        needs_review: n.needs_review,
        review_reasons: n.review_reasons,
        actor: n.actor,
      },
      width: s.width,
      height: s.height,
      style: { width: s.width, height: s.height },
    };
  });

  const rfEdges: Edge[] = proc.edges.map((e) => ({
    id: e.edge_id,
    source: e.source,
    target: e.target,
    label: edgeLabel(e),
    type: 'smoothstep',
    style: { strokeWidth: 1.5 },
    markerEnd: { type: 'arrowclosed' } as any,
    data: { is_default: e.is_default },
  }));

  return { nodes: rfNodes, edges: rfEdges, laneLayouts: [], totalWidth: maxX + 40, totalHeight: maxY + 40 };
}

// ── Helpers ────────────────────────────────────────────────────────────────────

function rfNodeType(n: BPMNNode): string {
  switch (n.bpmn_type) {
    case 'START_EVENT': return 'startEvent';
    case 'END_EVENT':   return 'endEvent';
    case 'GATEWAY':     return 'gateway';
    case 'TASK':
    case 'SUBPROCESS':  return 'task';
    default:            return 'task';
  }
}

function edgeLabel(e: BPMNEdge): string {
  if (e.is_default) return 'default';
  if (e.label) return e.label;
  if (e.condition_variable) {
    const val = e.condition_value ?? 'true';
    return `${e.condition_variable} = ${val}`;
  }
  return '';
}
