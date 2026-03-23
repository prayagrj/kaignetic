"""L9 — DAG Resolver: connectivity, cycle detection, lane assignment via NetworkX."""

import networkx as nx

from models.schemas import BPMNEdge, BPMNNode, BPMNNodeType, GatewayType, Job

# Node types that are structurally exempt from sequence-flow reachability.
# BOUNDARY_EVENT nodes attach to a task via exception semantics, not via
# sequence flows, so BFS-from-START will never reach them by design.
_REACHABILITY_EXEMPT = {BPMNNodeType.BOUNDARY_EVENT}


def run(job: Job) -> None:
    for process in job.processes:
        G = _build_graph(process)

        node_map = {n.node_id: n for n in process.bpmn_nodes}

        # Step 1: Unreachable nodes (BFS from START) — distinct from gateway shape warnings
        # BOUNDARY_EVENTs are exempt: they have no incoming sequence flows by BPMN spec.
        start_nodes = [n.node_id for n in process.bpmn_nodes if n.bpmn_type == BPMNNodeType.START_EVENT]
        if start_nodes:
            reachable = set(nx.bfs_tree(G, start_nodes[0]).nodes())
            for nid in G.nodes():
                if nid not in reachable:
                    node = node_map.get(nid)
                    if node and node.bpmn_type not in _REACHABILITY_EXEMPT:
                        node.unreachable_from_start = True
                        node.needs_review = True
                        node.review_reasons.append("Unreachable from START (L9 BFS)")

        # Step 2: Cycle detection — label back-edges; flag review (do not hide structure)
        try:
            cycles = list(nx.simple_cycles(G))
            for cycle in cycles:
                if len(cycle) == 1:
                    n0 = node_map.get(cycle[0])
                    if n0:
                        n0.bpmn_type = BPMNNodeType.SUBPROCESS
                        n0.needs_review = True
                        n0.review_reasons.append("Self-loop cycle collapsed to subprocess (L9)")
                else:
                    for e in process.bpmn_edges:
                        if e.source_node_id == cycle[-1] and e.target_node_id == cycle[0]:
                            if " [loop-back]" not in (e.label or ""):
                                e.label = (e.label or "") + " [loop-back]"
                            src = node_map.get(e.source_node_id)
                            if src:
                                src.needs_review = True
                                src.review_reasons.append("Cycle back-edge (L9)")
        except Exception:
            pass

        # Step 3: Gateway shape — flag under-branched gateways instead of demoting to TASK
        for node in process.bpmn_nodes:
            if node.bpmn_type != BPMNNodeType.GATEWAY:
                continue
            out_edges = [e for e in process.bpmn_edges if e.source_node_id == node.node_id]
            if len(out_edges) <= 1:
                node.needs_review = True
                node.review_reasons.append(
                    "Gateway has at most one outgoing sequence flow (L9)"
                )

        # Step 4: Lane assignment by actor
        actor_to_lane: dict[str, str] = {}
        for node in process.bpmn_nodes:
            if node.actor and node.actor not in actor_to_lane:
                actor_to_lane[node.actor] = _slug(node.actor)

        process.__dict__["_actor_to_lane"] = actor_to_lane

        # Step 5: Edge dedup
        seen = set()
        unique_edges = []
        for e in process.bpmn_edges:
            key = (e.source_node_id, e.target_node_id, e.label)
            if key not in seen:
                seen.add(key)
                unique_edges.append(e)
        process.bpmn_edges = unique_edges


def validate_gate(job: Job) -> None:
    for process in job.processes:
        node_ids = {n.node_id for n in process.bpmn_nodes}
        starts = [n for n in process.bpmn_nodes if n.bpmn_type == BPMNNodeType.START_EVENT]
        ends = [n for n in process.bpmn_nodes if n.bpmn_type == BPMNNodeType.END_EVENT]

        if len(starts) != 1:
            raise LayerError("L9_MULTIPLE_START_EVENTS", f"Expected 1 START_EVENT in {process.name}, found {len(starts)}.")
        if not ends:
            raise LayerError("L9_NO_END_EVENT", f"No END_EVENT found in {process.name}.")

        for e in process.bpmn_edges:
            if e.source_node_id not in node_ids or e.target_node_id not in node_ids:
                raise LayerError("L9_DANGLING_EDGE", f"Edge {e.edge_id} in {process.name} references missing node.")

        # Exclude structurally-exempt nodes (e.g. BOUNDARY_EVENTs) from the
        # rate denominator — they are never reachable via sequence flows.
        countable_nodes = [
            n for n in process.bpmn_nodes
            if n.bpmn_type not in _REACHABILITY_EXEMPT
        ]
        unreachable = [n for n in countable_nodes if getattr(n, "unreachable_from_start", False)]
        if countable_nodes and (len(unreachable) / len(countable_nodes)) >= 0.15:
            raise LayerError(
                "L9_HIGH_UNREACHABLE_RATE",
                f"Unreachable-from-START rate {len(unreachable)}/{len(countable_nodes)} >= 15% in {process.name}.",
            )


def _build_graph(process) -> nx.DiGraph:
    G = nx.DiGraph()
    for n in process.bpmn_nodes:
        G.add_node(n.node_id)
    for e in process.bpmn_edges:
        G.add_edge(e.source_node_id, e.target_node_id)
    return G


def _slug(name: str) -> str:
    return name.lower().replace(" ", "_").replace("-", "_")


class LayerError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
