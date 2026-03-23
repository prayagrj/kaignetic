"""L10 — BPMN Translator: layout + lxml XML serialization + BPMN 2.0 output.

Writes one `.bpmn` file per ProcessModel (per SOP slice from L3b).
"""
import json
import os
import re

import networkx as nx
from lxml import etree

import config
from models.schemas import BPMNNodeType, GatewayType, Job


BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS = "http://www.omg.org/spec/DD/20100524/DC"
DI_NS = "http://www.omg.org/spec/DD/20100524/DI"

NODE_W = {"TASK": 120, "GATEWAY": 50, "START_EVENT": 36, "END_EVENT": 36, "BOUNDARY_EVENT": 36, "SUBPROCESS": 120}
NODE_H = {"TASK": 60, "GATEWAY": 50, "START_EVENT": 36, "END_EVENT": 36, "BOUNDARY_EVENT": 36, "SUBPROCESS": 60}
H_GAP = 60
V_GAP = 80


def _safe_bpmn_filename_part(text: str, max_len: int = 48) -> str:
    """ASCII slug for optional filename hint; falls back to empty if nothing left."""
    s = re.sub(r"[^\w\s-]", "", text, flags=re.ASCII)
    s = re.sub(r"[-\s]+", "_", s.strip()).strip("_").lower()
    return s[:max_len] if s else ""


def run(job: Job) -> None:
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    report_path = os.path.join(config.OUTPUT_DIR, f"{job.job_id}_report.json")

    sop_outputs: list[dict] = []
    output_paths: list[str] = []

    for proc in job.processes:
        if not proc.bpmn_nodes:
            continue

        actor_to_lane = proc.__dict__.get("_actor_to_lane", {})
        _compute_layout(proc.bpmn_nodes, proc.bpmn_edges, actor_to_lane)

        xml_bytes = _serialize_xml(
            job.job_id,
            proc.process_id,
            proc.bpmn_nodes,
            proc.bpmn_edges,
            actor_to_lane,
        )
        _validate_xml(xml_bytes)

        name_part = _safe_bpmn_filename_part(proc.name)
        base = f"{job.job_id}_{proc.process_id}"
        if name_part:
            base = f"{base}_{name_part}"
        bpmn_path = os.path.join(config.OUTPUT_DIR, f"{base}.bpmn")

        with open(bpmn_path, "wb") as f:
            f.write(xml_bytes)

        output_paths.append(bpmn_path)
        sop_outputs.append(
            {
                "process_id": proc.process_id,
                "name": proc.name,
                "output_file": bpmn_path,
                "graph_metadata": _graph_metadata(proc.bpmn_nodes, proc.bpmn_edges),
            }
        )

    report = _build_report(job, sop_outputs)
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    job.__dict__["output_files"] = output_paths
    job.__dict__["output_file"] = output_paths[0] if output_paths else ""
    job.__dict__["report_file"] = report_path
    job.__dict__["sop_outputs"] = sop_outputs


def validate_gate(job: Job) -> None:
    paths = job.__dict__.get("output_files") or []
    if not paths:
        raise LayerError("L10_EMPTY_OUTPUT", "No BPMN files were written (no process graphs to export).")
    for p in paths:
        if not p or not os.path.exists(p) or os.path.getsize(p) == 0:
            raise LayerError("L10_EMPTY_OUTPUT", f"BPMN output missing or empty: {p}")

    for proc in job.processes:
        missing_layout = [n for n in proc.bpmn_nodes if n.x is None]
        if missing_layout:
            raise LayerError(
                "L10_MISSING_LAYOUT",
                f"{len(missing_layout)} nodes without coordinates in {proc.name!r}.",
            )


def _rank_list_for_layout(G: nx.DiGraph, nodes) -> list[str]:
    """Topological order when DAG; otherwise BFS from START then remaining nodes (handles cycles)."""
    try:
        return list(nx.topological_sort(G))
    except nx.NetworkXUnfeasible:
        starts = [n.node_id for n in nodes if n.bpmn_type == BPMNNodeType.START_EVENT]
        ordered: list[str] = []
        seen: set[str] = set()
        for s in starts:
            if s not in G:
                continue
            for nid in nx.bfs_tree(G, s).nodes():
                if nid not in seen:
                    seen.add(nid)
                    ordered.append(nid)
        for nid in G.nodes():
            if nid not in seen:
                ordered.append(nid)
        return ordered


def _compute_layout(nodes, edges, actor_to_lane: dict) -> None:
    node_map = {n.node_id: n for n in nodes}

    G = nx.DiGraph()
    for n in nodes:
        G.add_node(n.node_id)
    for e in edges:
        if e.source_node_id in node_map and e.target_node_id in node_map:
            G.add_edge(e.source_node_id, e.target_node_id)

    rank_list = _rank_list_for_layout(G, nodes)

    # Rank columns: longest-path depth (relaxation; works for small graphs with cycles)
    depth_map: dict[str, int] = {nid: 0 for nid in G.nodes()}
    for _ in range(max(1, len(G) + 1)):
        stable = True
        for nid in rank_list:
            preds = list(G.predecessors(nid))
            new_d = 0 if not preds else max(depth_map.get(p, 0) for p in preds) + 1
            if depth_map.get(nid) != new_d:
                depth_map[nid] = new_d
                stable = False
        if stable:
            break

    rank_groups: dict[int, list] = {}
    for nid, depth in depth_map.items():
        rank_groups.setdefault(depth, []).append(nid)

    lane_order = list(actor_to_lane.keys())

    for depth, group in rank_groups.items():
        group.sort(key=lambda nid: (
            lane_order.index(node_map[nid].actor) if node_map[nid].actor in lane_order else 999
        ))
        for pos, nid in enumerate(group):
            node = node_map[nid]
            ntype = node.bpmn_type.value if node.bpmn_type else "TASK"
            w = NODE_W.get(ntype, 120)
            h = NODE_H.get(ntype, 60)
            node.width = w
            node.height = h
            node.x = depth * (120 + H_GAP) + 50
            node.y = pos * (60 + V_GAP) + 50

    # Any nodes not in graph
    for node in nodes:
        if node.x is None:
            node.x, node.y, node.width, node.height = 50, 50, 120, 60


def _serialize_xml(
    job_id: str,
    process_local_id: str,
    nodes,
    edges,
    actor_to_lane: dict,
) -> bytes:
    nsmap = {
        None: BPMN_NS,
        "bpmndi": BPMNDI_NS,
        "dc": DC_NS,
        "di": DI_NS,
    }

    proc_xml_id = f"process_{job_id}_{process_local_id}"
    definitions = etree.Element(f"{{{BPMN_NS}}}definitions", nsmap=nsmap)
    definitions.set("id", f"definitions_{job_id}_{process_local_id}")
    definitions.set("targetNamespace", "http://bpmn.io/schema/bpmn")

    process = etree.SubElement(definitions, f"{{{BPMN_NS}}}process")
    process.set("id", proc_xml_id)
    process.set("isExecutable", "false")

    node_map = {n.node_id: n for n in nodes}

    if actor_to_lane:
        lane_set = etree.SubElement(process, f"{{{BPMN_NS}}}laneSet")
        lane_set.set("id", "laneSet_1")
        for actor, slug in actor_to_lane.items():
            lane_el = etree.SubElement(lane_set, f"{{{BPMN_NS}}}lane")
            lane_el.set("id", f"lane_{slug}")
            lane_el.set("name", actor)
            for n in nodes:
                if n.actor == actor:
                    fn = etree.SubElement(lane_el, f"{{{BPMN_NS}}}flowNodeRef")
                    fn.text = _element_id(n)

    for node in nodes:
        el = etree.SubElement(process, _bpmn_element(node))
        el.set("id", _element_id(node))
        el.set("name", node.label or "")
        if node.bpmn_type == BPMNNodeType.GATEWAY and node.gateway_type:
            pass  # gateway_type is in the element tag, not an attribute in BPMN 2.0
        if node.bpmn_type == BPMNNodeType.BOUNDARY_EVENT:
            etree.SubElement(el, f"{{{BPMN_NS}}}errorEventDefinition")

    for edge in edges:
        seq = etree.SubElement(process, f"{{{BPMN_NS}}}sequenceFlow")
        seq.set("id", f"flow_{edge.edge_id}")
        seq.set("sourceRef", _element_id(node_map[edge.source_node_id]) if edge.source_node_id in node_map else edge.source_node_id)
        seq.set("targetRef", _element_id(node_map[edge.target_node_id]) if edge.target_node_id in node_map else edge.target_node_id)
        if edge.label:
            seq.set("name", edge.label)
        if edge.is_default:
            seq.set("isDefault", "true")

    # Diagram (DI)
    diagram = etree.SubElement(definitions, f"{{{BPMNDI_NS}}}BPMNDiagram")
    diagram.set("id", f"diagram_{process_local_id}")
    plane = etree.SubElement(diagram, f"{{{BPMNDI_NS}}}BPMNPlane")
    plane.set("id", f"plane_{process_local_id}")
    plane.set("bpmnElement", proc_xml_id)

    for node in nodes:
        shape = etree.SubElement(plane, f"{{{BPMNDI_NS}}}BPMNShape")
        shape.set("id", f"shape_{node.node_id}")
        shape.set("bpmnElement", _element_id(node))
        bounds = etree.SubElement(shape, f"{{{DC_NS}}}Bounds")
        bounds.set("x", str(node.x or 0))
        bounds.set("y", str(node.y or 0))
        bounds.set("width", str(node.width or 120))
        bounds.set("height", str(node.height or 60))

    for edge in edges:
        waypoint_el = etree.SubElement(plane, f"{{{BPMNDI_NS}}}BPMNEdge")
        waypoint_el.set("id", f"edge_{edge.edge_id}")
        waypoint_el.set("bpmnElement", f"flow_{edge.edge_id}")
        src_node = node_map.get(edge.source_node_id)
        tgt_node = node_map.get(edge.target_node_id)
        if src_node and tgt_node:
            for nx_, ny_ in [
                (src_node.x + src_node.width, src_node.y + src_node.height / 2),
                (tgt_node.x, tgt_node.y + tgt_node.height / 2),
            ]:
                wp = etree.SubElement(waypoint_el, f"{{{DI_NS}}}waypoint")
                wp.set("x", str(nx_))
                wp.set("y", str(ny_))

    return etree.tostring(definitions, pretty_print=True, xml_declaration=True, encoding="UTF-8")


def _validate_xml(xml_bytes: bytes) -> None:
    try:
        etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as e:
        raise LayerError("L10_INVALID_XML", str(e))


def _bpmn_element(node) -> str:
    mapping = {
        BPMNNodeType.START_EVENT: f"{{{BPMN_NS}}}startEvent",
        BPMNNodeType.END_EVENT: f"{{{BPMN_NS}}}endEvent",
        BPMNNodeType.TASK: f"{{{BPMN_NS}}}userTask",
        BPMNNodeType.GATEWAY: _gateway_tag(node),
        BPMNNodeType.BOUNDARY_EVENT: f"{{{BPMN_NS}}}boundaryEvent",
        BPMNNodeType.SUBPROCESS: f"{{{BPMN_NS}}}subProcess",
    }
    return mapping.get(node.bpmn_type, f"{{{BPMN_NS}}}task")


def _gateway_tag(node) -> str:
    gt_map = {
        GatewayType.XOR: f"{{{BPMN_NS}}}exclusiveGateway",
        GatewayType.AND: f"{{{BPMN_NS}}}parallelGateway",
        GatewayType.OR: f"{{{BPMN_NS}}}inclusiveGateway",
    }
    return gt_map.get(node.gateway_type, f"{{{BPMN_NS}}}exclusiveGateway")


def _element_id(node) -> str:
    prefix_map = {
        BPMNNodeType.START_EVENT: "start",
        BPMNNodeType.END_EVENT: "end",
        BPMNNodeType.TASK: "task",
        BPMNNodeType.GATEWAY: "gateway",
        BPMNNodeType.BOUNDARY_EVENT: "boundary",
        BPMNNodeType.SUBPROCESS: "subprocess",
    }
    prefix = prefix_map.get(node.bpmn_type, "node")
    return f"{prefix}_{node.node_id}"


def _graph_metadata(nodes, edges) -> dict:
    node_types: dict[str, int] = {}
    for n in nodes:
        k = n.bpmn_type.value if n.bpmn_type else "UNKNOWN"
        node_types[k] = node_types.get(k, 0) + 1

    actors = {n.actor for n in nodes if n.actor}
    gw_types: dict[str, int] = {}
    for n in nodes:
        if n.bpmn_type == BPMNNodeType.GATEWAY and n.gateway_type:
            k = n.gateway_type.value
            gw_types[k] = gw_types.get(k, 0) + 1

    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "lane_count": len(actors),
        "node_types": node_types,
        "gateway_types": gw_types,
    }


def _build_report(job: Job, sop_outputs: list[dict]) -> dict:
    total_nodes = sum(o["graph_metadata"]["node_count"] for o in sop_outputs)
    total_edges = sum(o["graph_metadata"]["edge_count"] for o in sop_outputs)

    review_flags = [
        {"block_id": f.block_id, "layer": f.layer, "reason": f.reason}
        for f in job.review_flags
    ]

    llm_log = [
        {"layer": r.layer, "template": r.prompt_template,
         "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
         "latency_ms": r.latency_ms, "cached": r.cached}
        for r in job.llm_call_log
    ]

    first_path = sop_outputs[0]["output_file"] if sop_outputs else ""

    return {
        "job_id": job.job_id,
        "sop_class": job.sop_class,
        "status": job.status.value,
        "graph_metadata": {
            "sop_count": len(sop_outputs),
            "node_count_total": total_nodes,
            "edge_count_total": total_edges,
        },
        "sop_outputs": sop_outputs,
        "review_flags": review_flags,
        "llm_call_log": llm_log,
        "output_files": [o["output_file"] for o in sop_outputs],
        "output_file": first_path,
    }


class LayerError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
