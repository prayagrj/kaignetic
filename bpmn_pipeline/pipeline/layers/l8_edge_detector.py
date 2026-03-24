"""L8 — Edge Detector: var-graph DAG first, sequential fallback, LLM as judge for gateways.
"""
import json
import uuid
from collections import defaultdict

import networkx as nx

import config
from llm.client import LLMClient
from llm.prompts import (
    INFER_SINGLE_GATEWAY_SYSTEM, INFER_SINGLE_GATEWAY_USER,
    RECONNECT_ISOLATED_NODES_SYSTEM, RECONNECT_ISOLATED_NODES_USER,
)
from models.schemas import BPMNEdge, BPMNNode, BPMNNodeType, BlockType, DataVar, GatewayType, Job


# ── Sub-stage: variable linker ────────────────────────────────────────────────
_BOOL_SUFFIXES = {"approved", "rejected", "valid", "invalid", "complete", "failed",
                  "verified", "checked", "confirmed", "granted", "denied"}


def _infer_var_type(name: str) -> str:
    lower = name.lower().replace("v_", "")
    for s in _BOOL_SUFFIXES:
        if lower.endswith(s) or lower.startswith(s):
            return "bool"
    if any(k in lower for k in ("id", "number", "code", "ref")):
        return "id"
    if any(k in lower for k in ("count", "total", "amount", "qty")):
        return "count"
    if any(k in lower for k in ("data", "form", "record", "document", "report", "file")):
        return "data"
    return "unknown"


def _build_data_vars(process) -> None:
    """Populate process.data_vars from AtomicUnit inputs/outputs (pure Python, no LLM)."""
    registry: dict[str, DataVar] = {}

    for unit in process.atomic_units:
        for var_name in unit.outputs:
            if not var_name or not isinstance(var_name, str):
                continue
            if var_name not in registry:
                registry[var_name] = DataVar(
                    name=var_name,
                    var_type=_infer_var_type(var_name),
                    producer_unit_id=unit.unit_id,
                )
            elif registry[var_name].producer_unit_id is None:
                registry[var_name].producer_unit_id = unit.unit_id

        for var_name in unit.inputs:
            if not var_name or not isinstance(var_name, str):
                continue
            if var_name not in registry:
                registry[var_name] = DataVar(
                    name=var_name,
                    var_type=_infer_var_type(var_name),
                    producer_unit_id=None,
                )
            registry[var_name].consumers.append(unit.unit_id)

    # Flag inputs with no known producer for review
    unit_map = {u.unit_id: u for u in process.atomic_units}
    chunk_map = {c.chunk_id: c for c in process.chunks}
    for var in registry.values():
        if var.producer_unit_id is None:
            for cuid in var.consumers:
                consumer = unit_map.get(cuid)
                if consumer:
                    chunk = chunk_map.get(consumer.chunk_id)
                    if chunk:
                        chunk.needs_review = True
                        chunk.review_reasons.append(
                            f"Input variable '{var.name}' has no known producer in the flow."
                        )

    process.data_vars = list(registry.values())


def run(job: Job) -> None:
    llm = LLMClient(job)

    for process in job.processes:
        # ── Sub-stage 0: build variable graph ────────────────────────────────
        _build_data_vars(process)

        nodes = process.bpmn_nodes
        node_map = {n.node_id: n for n in nodes}
        unit_to_node: dict[str, str] = process.__dict__.get("_unit_to_task_node", {})
        chunk_map = {c.chunk_id: c for c in process.chunks}

        # L7 guarantees exactly one START, one END, tasks in between.
        start_nodes = [n for n in nodes if n.bpmn_type == BPMNNodeType.START_EVENT]
        end_nodes = [n for n in nodes if n.bpmn_type == BPMNNodeType.END_EVENT]
        task_node_ids = [
            unit_to_node[u.unit_id]
            for u in process.atomic_units
            if u.unit_id in unit_to_node
        ]
        start_id = start_nodes[0].node_id if start_nodes else None
        end_id = end_nodes[0].node_id if end_nodes else None
        ordered_node_ids: list[str] = [nid for nid in ([start_id] + task_node_ids + [end_id]) if nid]

        # ── Phase 1: Primary DAG from var-graph (producer → consumer) ─────────
        var_edges: set[tuple[str, str]] = set()
        var_outgoing: dict[str, set[str]] = defaultdict(set)

        for var in process.data_vars:
            if not var.producer_unit_id or not var.consumers:
                continue
            producer_nid = unit_to_node.get(var.producer_unit_id)
            if not producer_nid:
                continue
            for consumer_uid in var.consumers:
                consumer_nid = unit_to_node.get(consumer_uid)
                if consumer_nid and consumer_nid != producer_nid:
                    var_edges.add((producer_nid, consumer_nid))
                    var_outgoing[producer_nid].add(consumer_nid)

        edges: list[BPMNEdge] = []
        for src_nid, tgt_nid in var_edges:
            e = _make_edge(job.job_id, src_nid, tgt_nid)
            e.edge_type = "DATA_FLOW"
            edges.append(e)

        # ── Phase 2: Sequential fallback for var-graph-isolated nodes ─────────
        # For each consecutive document-order pair, add a sequential edge only
        # when the source node has NO outgoing var-graph edges (true unknown).
        # This avoids the cross-branch "spine" that caused invalid diagrams.
        for i in range(len(ordered_node_ids) - 1):
            src, tgt = ordered_node_ids[i], ordered_node_ids[i + 1]
            if not src or not tgt:
                continue
            if src not in var_outgoing:
                # Node has no known data-flow successors → fall back to document order
                if not any(e.source_node_id == src and e.target_node_id == tgt for e in edges):
                    edges.append(_make_edge(job.job_id, src, tgt))

        # ── Phase 3: Gateway type + branch inference (LLM, one call per gateway) ──
        # Discovery: any atomic unit marked DECISION by the atomizer becomes a gateway.
        # This replaces the old chunk-type-based detection which required L3 to emit
        # BlockType.DECISION — causing false positives. The atomizer (L6) now identifies
        # decision points with finer granularity.
        decision_units = [u for u in process.atomic_units if getattr(u, 'step_type', None) == 'DECISION']

        # Build a flat ordered list of atomic units for window lookups
        all_units = process.atomic_units
        unit_index = {u.unit_id: i for i, u in enumerate(all_units)}

        # Var registry for per-gateway context
        var_by_producer = {}
        for var in process.data_vars:
            if var.producer_unit_id:
                var_by_producer.setdefault(var.producer_unit_id, []).append(var)

        for gw_unit in decision_units:
            gw_nid = unit_to_node.get(gw_unit.unit_id)
            if not gw_nid:
                continue

            gw_chunk = chunk_map.get(gw_unit.chunk_id)
            center_idx = unit_index.get(gw_unit.unit_id, 0)
            win = config.L8_GATEWAY_WINDOW

            # Preceding units context (full action text, no truncation)
            preceding = []
            for u in all_units[max(0, center_idx - win):center_idx]:
                preceding.append({
                    "unit_id": u.unit_id,
                    "action": u.action,
                    "step_type": getattr(u, 'step_type', 'SIMPLE'),
                    "actor": u.actor,
                    "outputs": u.outputs,
                })

            # Following units context (full action text + step_type so LLM can spot branches)
            following = []
            for u in all_units[center_idx + 1: center_idx + 1 + win]:
                following.append({
                    "unit_id": u.unit_id,
                    "action": u.action,
                    "step_type": getattr(u, 'step_type', 'SIMPLE'),
                    "actor": u.actor,
                    "inputs": u.inputs,
                    "condition": u.condition,
                })

            # Variables known at this point (from producer units up to center)
            known_vars = {}
            for u in all_units[:center_idx + 1]:
                for var in var_by_producer.get(u.unit_id, []):
                    known_vars[var.name] = var.var_type

            gateway_block_dict = {
                "block_id": gw_unit.chunk_id,
                "text": gw_chunk.contextualized if gw_chunk else gw_unit.action,
                "condition_scope": gw_chunk.condition_scope if gw_chunk else None,
                "decision_unit_id": gw_unit.unit_id,
                "decision_action": gw_unit.action,
                "decision_condition": gw_unit.condition,
                "atomic_units": [
                    {
                        "unit_id": gw_unit.unit_id,
                        "action": gw_unit.action,
                        "step_type": gw_unit.step_type,
                        "outputs": gw_unit.outputs,
                        "inputs": gw_unit.inputs,
                    }
                ],
            }

            result = llm.call(
                layer=8,
                template_name="INFER_SINGLE_GATEWAY",
                system_prompt=INFER_SINGLE_GATEWAY_SYSTEM,
                user_prompt=INFER_SINGLE_GATEWAY_USER.format(
                    gateway_block_json=json.dumps(gateway_block_dict, indent=2),
                    preceding_units_json=json.dumps(preceding, indent=2),
                    following_units_json=json.dumps(following, indent=2),
                    known_vars_json=json.dumps(known_vars, separators=(',', ':')),
                ),
            )

            if not result:
                continue

            # Apply gateway type
            gw_type_str = result.get("gateway_type", "EXCLUSIVE")
            try:
                gw_type = GatewayType(gw_type_str)
            except ValueError:
                gw_type = GatewayType.EXCLUSIVE
            node_map[gw_nid].bpmn_type = BPMNNodeType.GATEWAY
            node_map[gw_nid].gateway_type = gw_type
            # Apply gateway label when provided
            gw_label = result.get("gateway_label")
            if gw_label:
                node_map[gw_nid].label = gw_label

            # TASK 2: tag this gateway as DIVERGING
            node_map[gw_nid].gateway_direction = "DIVERGING"

            gw_pos = ordered_node_ids.index(gw_nid) if gw_nid in ordered_node_ids else None
            next_nid = ordered_node_ids[gw_pos + 1] if gw_pos is not None and gw_pos + 1 < len(ordered_node_ids) else None

            branches = result.get("branches", [])
            if branches:
                # Remove the single sequential out-edge from this gateway
                edges = [e for e in edges if e.source_node_id != gw_nid]
                
                has_edge_to_next = False
                branch_target_nids: list[str] = []

                for branch in branches:
                    target_uid = branch.get("target_unit_id")
                    target_nid = unit_to_node.get(target_uid) if target_uid else None

                    # Fallback: try to match by position in ordered_node_ids
                    if not target_nid:
                        target_nid = next_nid

                    if target_nid:
                        e = _make_edge(job.job_id, gw_nid, target_nid)
                        e.label = branch.get("label")
                        e.is_default = branch.get("is_default", False)
                        # TASK 3: annotate with condition variable from LLM
                        e.condition_variable = branch.get("condition_var")
                        e.condition_value = branch.get("condition_value")
                        edges.append(e)
                        branch_target_nids.append(target_nid)
                        if target_nid == next_nid:
                            has_edge_to_next = True
                
                # If the LLM didn't return any branch heading to the natural next step,
                # we must add it implicitly, otherwise the entire downstream graph becomes orphaned.
                if not has_edge_to_next and next_nid is not None:
                    implicit_tgt: str = str(next_nid)
                    e = _make_edge(job.job_id, gw_nid, implicit_tgt)
                    e.label = "otherwise (implicit)"
                    e.is_default = True
                    edges.append(e)
                    branch_target_nids.append(implicit_tgt)

                # ── Remove stale cross-branch sequential edges ────────────────
                # Phase 1 built a flat sequential spine in document order.
                # After branch surgery, some edges connect the END of one branch
                # to the START of the NEXT branch (e.g. na2 → nb1 in document
                # order). These are invalid — each branch is independent.
                # We identify and remove them now.
                #
                # Rule: for each consecutive pair of branch targets (t_i, t_{i+1})
                # in ordered_node_ids, delete the sequential edge that bridges
                # from any node between t_i (exclusive) and t_{i+1} (inclusive)
                # to t_{i+1}, because that edge came from the flat spine and no
                # longer reflects the real branching structure.
                branch_positions = []
                for t_nid in branch_target_nids:
                    if t_nid in ordered_node_ids:
                        branch_positions.append(ordered_node_ids.index(t_nid))
                    else:
                        branch_positions.append(-1)

                valid_positions = sorted(
                    (pos, t_nid)
                    for pos, t_nid in zip(branch_positions, branch_target_nids)
                    if pos >= 0
                )

                # For each adjacent pair of branch entry-points, remove the
                # Phase-1 edge that bridges the gap between branches.
                stale_targets: set[str] = set()
                for idx in range(1, len(valid_positions)):
                    stale_targets.add(valid_positions[idx][1])

                if stale_targets:
                    edges = [
                        e for e in edges
                        if not (
                            e.target_node_id in stale_targets
                            and e.source_node_id != gw_nid
                            # Only remove edges whose source comes BEFORE the
                            # target in ordered_node_ids (i.e. old sequential).
                            and e.source_node_id in ordered_node_ids
                            and e.target_node_id in ordered_node_ids
                            and ordered_node_ids.index(e.source_node_id)
                                < ordered_node_ids.index(e.target_node_id)
                        )
                    ]

        # ── Phase 4: Cross-reference overrides ────────────────────────────────────
        for chunk in process.chunks:
            for cr in chunk.cross_refs:
                if cr.resolution_method in ("structural_anchor", "llm") and cr.resolved_chunk_id:
                    src_unit = next((u for u in all_units if u.chunk_id == chunk.chunk_id), None)
                    tgt_unit = next((u for u in all_units if u.chunk_id == cr.resolved_chunk_id), None)
                    if src_unit and tgt_unit:
                        src_nid = unit_to_node.get(src_unit.unit_id)
                        tgt_nid = unit_to_node.get(tgt_unit.unit_id)
                        if src_nid and tgt_nid and src_nid != tgt_nid:
                            if not any(e.source_node_id == src_nid and e.target_node_id == tgt_nid for e in edges):
                                e = _make_edge(job.job_id, src_nid, tgt_nid)
                                e.label = cr.ref_text
                                edges.append(e)

        # ── Phase 5a: Insert converging gateways ───────────────────────────────────
        nodes, edges = _insert_converging_gateways(nodes, edges, job.job_id)
        process.bpmn_nodes = nodes

        # ── Phase 5: Exception boundary edges → END ───────────────────────────────
        exception_node_map = process.__dict__.get("_chunk_to_exception_node", {})
        end_nid = end_id
        for bid, exc_nid in exception_node_map.items():
            if end_nid:
                edges.append(_make_edge(job.job_id, exc_nid, end_nid))

        # ── Phase 6: Prune trivial gateways (1-in / 1-out) ───────────────────────
        nodes, edges = _prune_trivial_gateways(nodes, edges)
        process.bpmn_nodes = nodes

        # ── Phase 7: Reconnect isolated subgraphs via LLM ────────────────────
        # Must run AFTER all structural phases so the reachable set is stable.
        edges = _reconnect_isolated_nodes(nodes, edges, ordered_node_ids, llm, job.job_id)
        process.bpmn_edges = edges


def validate_gate(job: Job) -> None:
    for process in job.processes:
        starts = [n for n in process.bpmn_nodes if n.bpmn_type == BPMNNodeType.START_EVENT]
        if not starts:
            continue
        out_from_start = [e for e in process.bpmn_edges if e.source_node_id == starts[0].node_id]
        if not out_from_start:
            raise LayerError("L8_DISCONNECTED_START", f"START_EVENT in {process.name} has no outgoing edges.")

        all_node_ids = {n.node_id for n in process.bpmn_nodes}
        outgoing = {e.source_node_id for e in process.bpmn_edges}
        dead_ends = [
            n for n in process.bpmn_nodes
            if n.node_id not in outgoing and n.bpmn_type not in {BPMNNodeType.END_EVENT}
        ]
        if all_node_ids and (len(dead_ends) / len(all_node_ids)) >= 0.1:
            raise SoftGateFailure("L8_DEAD_END_NODES", f"Dead-end nodes >= 10% in {process.name}")


def _prune_trivial_gateways(
    nodes: list,
    edges: list,
) -> tuple[list, list]:
    """
    A gateway with exactly 1 incoming AND 1 outgoing edge is not branching.
    These are typically over-classified DECISION blocks from L3.
    Bypass the gateway: connect its predecessor directly to its successor.
    Flag the removed node for review so its source block can be re-evaluated.
    """
    removed: set[str] = set()
    new_edges = list(edges)

    # Iterate until stable (cascades are unlikely but possible)
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if node.node_id in removed:
                continue
            if node.bpmn_type != BPMNNodeType.GATEWAY:
                continue

            outgoing = [e for e in new_edges if e.source_node_id == node.node_id]
            incoming = [e for e in new_edges if e.target_node_id == node.node_id]

            if len(outgoing) == 1 and len(incoming) == 1:
                pred_edge = incoming[0]
                succ_edge = outgoing[0]
                bypass = _make_edge(node.job_id, pred_edge.source_node_id, succ_edge.target_node_id)
                bypass.label = pred_edge.label or succ_edge.label
                new_edges.append(bypass)
                new_edges = [e for e in new_edges if e not in (pred_edge, succ_edge)]
                removed.add(node.node_id)

                # Flag the node for review (don't set it on the orphaned schema object —
                # just mark it so L9 can see it if we accidentally keep it)
                node.needs_review = True
                node.review_reasons.append(
                    "Gateway had only 1 outgoing branch — pruned and bypassed (L8)"
                )
                changed = True
                break  # restart iteration since list changed

    pruned_nodes = [n for n in nodes if n.node_id not in removed]
    return pruned_nodes, new_edges


def _insert_converging_gateways(
    nodes: list,
    edges: list,
    job_id: str,
) -> tuple[list, list]:
    """
    After diverging gateways are resolved, find any node that has >= 2 incoming
    edges from different sources (a join point) and lacks an explicit CONVERGING
    gateway upstream. Insert an XOR converging gateway to make the graph valid BPMN.

    The inserted gateway is placed between the join node and all its predecessors.
    """
    # Build incoming-edge count map
    incoming: dict[str, list] = {}
    for e in edges:
        incoming.setdefault(e.target_node_id, []).append(e)

    node_map = {n.node_id: n for n in nodes}
    new_nodes = list(nodes)
    new_edges = list(edges)

    for tgt_nid, inc_edges in list(incoming.items()):
        tgt_node = node_map.get(tgt_nid)
        if not tgt_node:
            continue
        # Only act on nodes that aren't already gateways and have >= 2 incoming flows
        if tgt_node.bpmn_type == BPMNNodeType.GATEWAY:
            continue
        if tgt_node.bpmn_type == BPMNNodeType.END_EVENT:
            continue
        if len(inc_edges) < 2:
            continue

        # Check if all predecessors are from the same diverging gateway (already handled)
        src_nids = {e.source_node_id for e in inc_edges}
        all_from_same_gw = (len(src_nids) == 1 and
                            node_map.get(next(iter(src_nids)), None) is not None and
                            node_map[next(iter(src_nids))].bpmn_type == BPMNNodeType.GATEWAY)
        if all_from_same_gw:
            continue  # natural gateway → target pattern, no converging needed

        # Insert a converging gateway
        conv_id = f"cg_{str(uuid.uuid4())[:6]}"
        conv_node = BPMNNode(
            node_id=conv_id,
            job_id=job_id,
            bpmn_type=BPMNNodeType.GATEWAY,
            gateway_type=GatewayType.EXCLUSIVE,
            gateway_direction="CONVERGING",
            label="",
        )
        new_nodes.append(conv_node)
        node_map[conv_id] = conv_node

        # Redirect all incoming edges to point at the converging gateway
        for e in inc_edges:
            e.target_node_id = conv_id

        # Add a single edge from converging gateway → join target
        new_edges.append(_make_edge(job_id, conv_id, tgt_nid))

    return new_nodes, new_edges

def _reconnect_isolated_nodes(
    nodes: list,
    edges: list,
    ordered_node_ids: list[str],
    llm: "LLMClient",
    job_id: str,
) -> list:
    """Use LLM to reconnect nodes that are not reachable from START."""
    start_nodes = [n for n in nodes if n.bpmn_type == BPMNNodeType.START_EVENT]
    if not start_nodes:
        return edges

    G = nx.DiGraph()
    for n in nodes:
        G.add_node(n.node_id)
    for e in edges:
        G.add_edge(e.source_node_id, e.target_node_id)

    try:
        reachable: set[str] = set(nx.bfs_tree(G, start_nodes[0].node_id).nodes())
    except Exception:
        return edges

    exempt = {BPMNNodeType.BOUNDARY_EVENT}
    node_map = {n.node_id: n for n in nodes}
    pos_map = {nid: i for i, nid in enumerate(ordered_node_ids)}

    isolated = [
        n for n in nodes
        if n.node_id not in reachable
        and n.bpmn_type not in exempt
        and n.bpmn_type not in {BPMNNodeType.START_EVENT, BPMNNodeType.END_EVENT}
    ]
    if not isolated:
        return edges

    def _node_info(n) -> dict:
        return {
            "node_id": n.node_id,
            "label": n.label,
            "actor": n.actor,
            "bpmn_type": n.bpmn_type.value if n.bpmn_type else None,
            "doc_position": pos_map.get(n.node_id),
        }

    isolated_data = [_node_info(n) for n in isolated]
    reachable_data = [
        _node_info(n) for n in nodes
        if n.node_id in reachable and n.bpmn_type != BPMNNodeType.END_EVENT
    ]

    result = llm.call(
        layer=8,
        template_name="RECONNECT_ISOLATED_NODES",
        system_prompt=RECONNECT_ISOLATED_NODES_SYSTEM,
        user_prompt=RECONNECT_ISOLATED_NODES_USER.format(
            isolated_nodes_json=json.dumps(isolated_data, indent=2),
            reachable_nodes_json=json.dumps(reachable_data, indent=2),
        ),
    )

    new_edges = list(edges)
    if not result or not isinstance(result, list):
        return new_edges

    reachable_ids = set(reachable)
    for item in result:
        if not isinstance(item, dict):
            continue
        nid = item.get("node_id")
        connect_from = item.get("connect_from")
        connect_to = item.get("connect_to")
        if float(item.get("confidence", 0)) < 0.3:
            continue
        if connect_from and connect_from not in reachable_ids:
            continue
        if connect_to and connect_to not in reachable_ids:
            continue
        if connect_from and nid:
            if not any(e.source_node_id == connect_from and e.target_node_id == nid for e in new_edges):
                new_edges.append(_make_edge(job_id, str(connect_from), str(nid)))
        if connect_to and nid:
            if not any(e.source_node_id == nid and e.target_node_id == connect_to for e in new_edges):
                new_edges.append(_make_edge(job_id, str(nid), str(connect_to)))

    return new_edges


def _make_edge(job_id: str, src: str, tgt: str) -> BPMNEdge:
    return BPMNEdge(
        edge_id=str(uuid.uuid4())[:8],
        job_id=job_id,
        source_node_id=src,
        target_node_id=tgt,
    )


class LayerError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class SoftGateFailure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
