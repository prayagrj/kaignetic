export type BPMNNodeType = 'START_EVENT' | 'END_EVENT' | 'TASK' | 'GATEWAY' | 'BOUNDARY_EVENT' | 'SUBPROCESS';
export type GatewayType = 'EXCLUSIVE' | 'PARALLEL' | 'EVENT_BASED';
export type GatewayDirection = 'DIVERGING' | 'CONVERGING';

export interface BPMNNode {
  node_id: string;
  bpmn_type: BPMNNodeType;
  label: string;
  actor: string | null;
  gateway_type: GatewayType | null;
  gateway_direction: GatewayDirection | null;
  needs_review: boolean;
  review_reasons: string[];
}

export interface BPMNEdge {
  edge_id: string;
  source: string;
  target: string;
  label: string | null;
  is_default: boolean;
  condition_variable: string | null;
  condition_value: string | null;
}

export interface ProcessGraph {
  process_id: string;
  name: string;
  actor_to_lane: Record<string, string>;
  nodes: BPMNNode[];
  edges: BPMNEdge[];
}

export interface JobGraph {
  job_id: string;
  source_file?: string;
  processes: ProcessGraph[];
}

// Swimlane layout info computed after dagre layout
export interface LaneLayout {
  actor: string;
  slug: string;
  y: number;
  height: number;
  width: number;
}
