"""L5 — Edge Detector

Pipeline:
  1. Explicit edges  — from unit.next_step_ids / branch.next_step_id (L2 intra-batch links)
  2. Sequential fill — document-order edges within the same top-level section only
  3. Gateway wiring  — replace spine edges out of EXCLUSIVE gateways with labelled branches
  4. Converging gateways — insert XOR merge nodes before any task with ≥2 incoming edges
  5. Prune trivial gateways — remove 1-in/1-out gateways
  6. LLM reconnect  — ask LLM to wire any still-unreachable nodes within the process

Process splitting is handled by the next layer (L6 process splitter).
"""
import uuid
from collections import defaultdict

import networkx as nx

from llm.client import LLMClient
from llm.prompts import (
    RECONNECT_ISOLATED_NODES_SYSTEM, RECONNECT_ISOLATED_NODES_USER,
    CROSS_SECTION_EDGES_SYSTEM, CROSS_SECTION_EDGES_USER,
)
from models.schemas import (
    AtomicDecisionUnit, BPMNEdge, BPMNNode, BPMNNodeType,
    GatewayType, Job, ProcessModel,
)
import json


# ── Public entry point ────────────────────────────────────────────────────────

def run(job: Job) -> None:
    llm = LLMClient(job)
    result: list[ProcessModel] = []
    for process in job.processes:
        result.extend(_process_to_bpmn(process, job, llm))
    job.processes = result


def validate_gate(job: Job) -> None:
    for process in job.processes:
        starts = [n for n in process.bpmn_nodes if n.bpmn_type == BPMNNodeType.START_EVENT]
        if not starts:
            continue
        if not any(e.source_node_id == starts[0].node_id for e in process.bpmn_edges):
            raise LayerError("L6_DISCONNECTED_START",
                             f"START_EVENT in {process.name} has no outgoing edges.")
        all_ids = {n.node_id for n in process.bpmn_nodes}
        sources = {e.source_node_id for e in process.bpmn_edges}
        dead = [n for n in process.bpmn_nodes
                if n.node_id not in sources and n.bpmn_type != BPMNNodeType.END_EVENT]
        if all_ids and len(dead) / len(all_ids) >= 0.1:
            raise SoftGateFailure("L6_DEAD_END_NODES",
                                  f"Dead-end nodes >= 10% in {process.name}")


# ── Core pipeline ─────────────────────────────────────────────────────────────

def _process_to_bpmn(process: ProcessModel, job: Job, llm: LLMClient) -> list[ProcessModel]:
    """Run all edge-building and splitting steps for one incoming ProcessModel.
    Returns 1..N ProcessModels (split when disconnected components are found)."""

    unit_to_node: dict[str, str] = process.__dict__.get("_unit_to_task_node", {})
    nodes = process.bpmn_nodes
    node_map = {n.node_id: n for n in nodes}

    # Document-order list of task/gateway node IDs (no START/END — we add fresh ones per split)
    ordered_tasks: list[str] = [
        unit_to_node[u.unit_id]
        for u in process.atomic_units
        if u.unit_id in unit_to_node
    ]

    # node_id → top-level section heading (empty string for unmapped nodes)
    node_section = _build_node_section_map(ordered_tasks, unit_to_node, process)

    edges = _build_explicit_edges(process, unit_to_node, job.job_id)
    edges = _fill_sequential_gaps(edges, ordered_tasks, node_section, node_map, job.job_id, llm)
    edges = _wire_gateways(edges, process, unit_to_node, node_map, ordered_tasks, job.job_id)
    nodes, edges = _insert_converging_gateways(nodes, edges, job.job_id)
    nodes, edges = _prune_trivial_gateways(nodes, edges)

    # Persist resolved edges back onto the process so the next layer (L6 splitter)
    # can see the full edge set without re-running edge building.
    process.bpmn_nodes = nodes
    process.bpmn_edges = edges

    # LLM reconnect: wire still-isolated nodes within this single process.
    process.bpmn_edges = _llm_reconnect(process.bpmn_nodes, process.bpmn_edges, llm, job.job_id)

    return [process]


# ── Step 1: explicit edges ────────────────────────────────────────────────────

def _build_explicit_edges(
    process: ProcessModel,
    unit_to_node: dict[str, str],
    job_id: str,
) -> list[BPMNEdge]:
    edges: list[BPMNEdge] = []
    for unit in process.atomic_units:
        src = unit_to_node.get(unit.unit_id)
        if not src:
            continue
        if isinstance(unit, AtomicDecisionUnit):
            for branch in unit.branches:
                tgt = unit_to_node.get(branch.next_step_id) if branch.next_step_id else None
                if tgt and tgt != src:
                    e = _make_edge(job_id, src, tgt)
                    e.label = branch.label
                    edges.append(e)
        else:
            for tgt_uid in (unit.next_step_ids or []):
                tgt = unit_to_node.get(tgt_uid)
                if tgt and tgt != src:
                    edges.append(_make_edge(job_id, src, tgt))
    return edges


# ── Step 2: sequential gap fill ──────────────────────────────────────────────

def _fill_sequential_gaps(
    edges: list[BPMNEdge],
    ordered_tasks: list[str],
    node_section: dict[str, str],
    node_map: dict[str, BPMNNode],
    job_id: str,
    llm: LLMClient,
) -> list[BPMNEdge]:
    """Add document-order edges between consecutive task nodes that have no explicit link yet.

    Same-section pairs are added directly. Cross-section pairs are batched and sent to the
    LLM to decide whether a sequence flow should span the section boundary.
    """
    existing = {(e.source_node_id, e.target_node_id) for e in edges}
    result = list(edges)
    cross_section_pairs: list[tuple[int, str, str]] = []  # (index, src, tgt)

    for i in range(len(ordered_tasks) - 1):
        src, tgt = ordered_tasks[i], ordered_tasks[i + 1]
        if (src, tgt) in existing:
            continue
        src_node = node_map.get(src)
        if src_node and src_node.bpmn_type == BPMNNodeType.END_EVENT:
            continue
        src_sec = node_section.get(src, "")
        tgt_sec = node_section.get(tgt, "")
        if src_sec and src_sec == tgt_sec:
            result.append(_make_edge(job_id, src, tgt))
            existing.add((src, tgt))
        else:
            cross_section_pairs.append((i, src, tgt))

    if not cross_section_pairs:
        return result

    pairs_payload = [
        {
            "pair_index": idx,
            "src_node_id": src,
            "src_label": node_map[src].label if src in node_map else "",
            "src_actor": node_map[src].actor if src in node_map else "",
            "src_section": node_section.get(src, ""),
            "tgt_node_id": tgt,
            "tgt_label": node_map[tgt].label if tgt in node_map else "",
            "tgt_actor": node_map[tgt].actor if tgt in node_map else "",
            "tgt_section": node_section.get(tgt, ""),
        }
        for idx, (_, src, tgt) in enumerate(cross_section_pairs)
    ]

    llm_result = llm.call(
        layer=5,
        template_name="CROSS_SECTION_EDGES",
        system_prompt=CROSS_SECTION_EDGES_SYSTEM,
        user_prompt=CROSS_SECTION_EDGES_USER.format(
            pairs_json=json.dumps(pairs_payload, indent=2)
        ),
    )

    approved: set[int] = set()
    if llm_result and isinstance(llm_result, list):
        for item in llm_result:
            if isinstance(item, dict) and item.get("add_edge") and float(item.get("confidence", 0)) >= 0.55:
                approved.add(int(item["pair_index"]))

    for idx, (_, src, tgt) in enumerate(cross_section_pairs):
        if idx in approved and (src, tgt) not in existing:
            result.append(_make_edge(job_id, src, tgt))
            existing.add((src, tgt))

    return result


# ── Step 3: gateway wiring ────────────────────────────────────────────────────

def _wire_gateways(
    edges: list[BPMNEdge],
    process: ProcessModel,
    unit_to_node: dict[str, str],
    node_map: dict[str, BPMNNode],
    ordered_tasks: list[str],
    job_id: str,
) -> list[BPMNEdge]:
    """For each EXCLUSIVE gateway, replace its outgoing edges with labelled branch edges."""
    pos = {nid: i for i, nid in enumerate(ordered_tasks)}

    for unit in process.atomic_units:
        if not isinstance(unit, AtomicDecisionUnit) or not unit.branches:
            continue
        gw_nid = unit_to_node.get(unit.unit_id)
        if not gw_nid:
            continue

        node_map[gw_nid].bpmn_type = BPMNNodeType.GATEWAY
        node_map[gw_nid].gateway_type = GatewayType.EXCLUSIVE
        node_map[gw_nid].gateway_direction = "DIVERGING"

        gw_pos = pos.get(gw_nid)
        next_task = ordered_tasks[gw_pos + 1] if gw_pos is not None and gw_pos + 1 < len(ordered_tasks) else None

        # Remove all existing outgoing edges from this gateway (spine + explicit)
        edges = [e for e in edges if e.source_node_id != gw_nid]

        branch_targets: list[str] = []
        has_next = False

        for branch in unit.branches:
            tgt = unit_to_node.get(branch.next_step_id) if branch.next_step_id else next_task
            if not tgt:
                continue
            e = _make_edge(job_id, gw_nid, tgt)
            e.label = branch.label
            edges.append(e)
            branch_targets.append(tgt)
            if tgt == next_task:
                has_next = True

        if not has_next and next_task:
            e = _make_edge(job_id, gw_nid, next_task)
            e.label = "otherwise (implicit)"
            e.is_default = True
            edges.append(e)
            branch_targets.append(next_task)

        # Store branch metadata on the node for L8
        node_map[gw_nid].branches = [
            {"label": b.label, "output_variables": b.output_variables,
             "target_unit_id": b.next_step_id, "is_default": False}
            for b in unit.branches
        ]

    return edges


# ── Step 4: insert converging gateways ───────────────────────────────────────

def _insert_converging_gateways(
    nodes: list[BPMNNode],
    edges: list[BPMNEdge],
    job_id: str,
) -> tuple[list[BPMNNode], list[BPMNEdge]]:
    """Insert an XOR converging gateway before any non-gateway node that has ≥2 incoming edges."""
    incoming: dict[str, list[BPMNEdge]] = defaultdict(list)
    for e in edges:
        incoming[e.target_node_id].append(e)

    node_map = {n.node_id: n for n in nodes}
    new_nodes = list(nodes)
    new_edges = list(edges)

    for tgt_id, inc in list(incoming.items()):
        tgt = node_map.get(tgt_id)
        if not tgt or tgt.bpmn_type in {BPMNNodeType.GATEWAY, BPMNNodeType.END_EVENT}:
            continue
        if len(inc) < 2:
            continue
        # Already a valid pattern: single diverging gateway fanning in
        src_ids = {e.source_node_id for e in inc}
        if len(src_ids) == 1:
            src = node_map.get(next(iter(src_ids)))
            if src and src.bpmn_type == BPMNNodeType.GATEWAY:
                continue

        cg_id = f"cg_{uuid.uuid4().hex[:6]}"
        cg = BPMNNode(node_id=cg_id, job_id=job_id, bpmn_type=BPMNNodeType.GATEWAY,
                      gateway_type=GatewayType.EXCLUSIVE, gateway_direction="CONVERGING", label="")
        new_nodes.append(cg)
        node_map[cg_id] = cg
        for e in inc:
            e.target_node_id = cg_id
        new_edges.append(_make_edge(job_id, cg_id, tgt_id))

    return new_nodes, new_edges


# ── Step 5: prune trivial gateways ───────────────────────────────────────────

def _prune_trivial_gateways(
    nodes: list[BPMNNode],
    edges: list[BPMNEdge],
) -> tuple[list[BPMNNode], list[BPMNEdge]]:
    """Remove gateways with exactly 1-in / 1-out and replace with a direct edge."""
    removed: set[str] = set()
    result = list(edges)
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if node.node_id in removed or node.bpmn_type != BPMNNodeType.GATEWAY:
                continue
            out = [e for e in result if e.source_node_id == node.node_id]
            inc = [e for e in result if e.target_node_id == node.node_id]
            if len(out) == 1 and len(inc) == 1:
                bypass = _make_edge(node.job_id, inc[0].source_node_id, out[0].target_node_id)
                bypass.label = inc[0].label or out[0].label
                result = [e for e in result if e not in (inc[0], out[0])]
                result.append(bypass)
                removed.add(node.node_id)
                node.needs_review = True
                node.review_reasons.append("Pruned: 1-in/1-out gateway (L6)")
                changed = True
                break
    return [n for n in nodes if n.node_id not in removed], result


# ── Step 6: LLM reconnect ─────────────────────────────────────────────────────

def _llm_reconnect(
    nodes: list[BPMNNode],
    edges: list[BPMNEdge],
    llm: LLMClient,
    job_id: str,
) -> list[BPMNEdge]:
    """Ask the LLM to wire any task/gateway nodes not reachable from START."""
    starts = [n for n in nodes if n.bpmn_type == BPMNNodeType.START_EVENT]
    if not starts:
        return edges

    G = nx.DiGraph()
    for n in nodes:
        G.add_node(n.node_id)
    for e in edges:
        G.add_edge(e.source_node_id, e.target_node_id)

    try:
        reachable = set(nx.bfs_tree(G, starts[0].node_id).nodes())
    except Exception:
        return edges

    isolated = [
        n for n in nodes
        if n.node_id not in reachable
        and n.bpmn_type not in {BPMNNodeType.START_EVENT, BPMNNodeType.END_EVENT,
                                 BPMNNodeType.BOUNDARY_EVENT}
    ]
    if not isolated:
        return edges

    def _info(n: BPMNNode) -> dict:
        return {"node_id": n.node_id, "label": n.label, "actor": n.actor,
                "bpmn_type": n.bpmn_type.value if n.bpmn_type else None}

    result = llm.call(
        layer=6,
        template_name="RECONNECT_ISOLATED_NODES",
        system_prompt=RECONNECT_ISOLATED_NODES_SYSTEM,
        user_prompt=RECONNECT_ISOLATED_NODES_USER.format(
            isolated_nodes_json=json.dumps([_info(n) for n in isolated], indent=2),
            reachable_nodes_json=json.dumps(
                [_info(n) for n in nodes
                 if n.node_id in reachable and n.bpmn_type != BPMNNodeType.END_EVENT],
                indent=2,
            ),
        ),
    )

    new_edges = list(edges)
    if not result or not isinstance(result, list):
        return new_edges

    for item in result:
        if not isinstance(item, dict):
            continue
        if float(item.get("confidence", 0)) < 0.3:
            continue
        nid = item.get("node_id")
        connect_from = item.get("connect_from")
        connect_to = item.get("connect_to")
        if connect_from and connect_from not in reachable:
            continue
        if connect_to and connect_to not in reachable:
            continue
        if connect_from and nid:
            if not any(e.source_node_id == connect_from and e.target_node_id == nid for e in new_edges):
                new_edges.append(_make_edge(job_id, connect_from, nid))
        if connect_to and nid:
            if not any(e.source_node_id == nid and e.target_node_id == connect_to for e in new_edges):
                new_edges.append(_make_edge(job_id, nid, connect_to))

    return new_edges


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_node_section_map(
    ordered_tasks: list[str],
    unit_to_node: dict[str, str],
    process: ProcessModel,
) -> dict[str, str]:
    """Return {node_id: top_level_heading} for every task node."""
    node_to_unit = {nid: uid for uid, nid in unit_to_node.items()}
    unit_map = {u.unit_id: u for u in process.atomic_units}
    chunk_map = {c.chunk_id: c for c in process.chunks}

    result: dict[str, str] = {}
    for nid in ordered_tasks:
        uid = node_to_unit.get(nid)
        unit = unit_map.get(uid) if uid else None
        chunk = chunk_map.get(unit.chunk_id) if unit else None
        result[nid] = chunk.headings[0] if chunk and chunk.headings else ""
    return result


def _make_edge(job_id: str, src: str, tgt: str) -> BPMNEdge:
    return BPMNEdge(
        edge_id=uuid.uuid4().hex[:8],
        job_id=job_id,
        source_node_id=src,
        target_node_id=tgt,
    )


# ── Exceptions ────────────────────────────────────────────────────────────────

class LayerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class SoftGateFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
