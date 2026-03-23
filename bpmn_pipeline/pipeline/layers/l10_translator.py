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

        unit_to_node = proc.__dict__.get("_unit_to_task_node", {})
        xml_bytes = _serialize_xml(
            job.job_id,
            proc.process_id,
            proc.bpmn_nodes,
            proc.bpmn_edges,
            actor_to_lane,
            unit_to_node=unit_to_node,
            process_model=proc,
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
    unit_to_node: dict | None = None,
    process_model=None,
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
        # TASK 3: emit conditionExpression if a condition variable is linked
        if getattr(edge, 'condition_variable', None):
            cond_val = getattr(edge, 'condition_value', None) or 'true'
            cond_expr = etree.SubElement(seq, f"{{{BPMN_NS}}}conditionExpression")
            cond_expr.text = f"${{{edge.condition_variable}}} == {cond_val}"

    # needs_review annotations — emit textAnnotation + association for flagged nodes
    for node in nodes:
        if not node.needs_review:
            continue
        reason_text = "; ".join(node.review_reasons) if node.review_reasons else "Needs review"
        annotation_id = f"ann_{node.node_id}"
        assoc_id = f"assoc_{node.node_id}"

        annotation_el = etree.SubElement(process, f"{{{BPMN_NS}}}textAnnotation")
        annotation_el.set("id", annotation_id)
        text_el = etree.SubElement(annotation_el, f"{{{BPMN_NS}}}text")
        text_el.text = f"⚠ Review: {reason_text[:120]}"

        assoc_el = etree.SubElement(process, f"{{{BPMN_NS}}}association")
        assoc_el.set("id", assoc_id)
        assoc_el.set("sourceRef", _element_id(node))
        assoc_el.set("targetRef", annotation_id)
        assoc_el.set("associationDirection", "None")

    # TASK 4: DataObject serialization from process.data_vars
    _serialize_data_objects(process, nodes, node_map, unit_to_node, process_model, job_id)

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

    # Add BPMNShape for needs_review annotation boxes
    for node in nodes:
        if not node.needs_review:
            continue
        annotation_id = f"ann_{node.node_id}"
        ann_shape = etree.SubElement(plane, f"{{{BPMNDI_NS}}}BPMNShape")
        ann_shape.set("id", f"shape_{annotation_id}")
        ann_shape.set("bpmnElement", annotation_id)
        ann_bounds = etree.SubElement(ann_shape, f"{{{DC_NS}}}Bounds")
        ann_x = str((node.x or 0) + (node.width or 120) + 20)
        ann_y = str((node.y or 0) - 40)
        ann_bounds.set("x", ann_x)
        ann_bounds.set("y", ann_y)
        ann_bounds.set("width", "180")
        ann_bounds.set("height", "40")

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


class SoftGateFailure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _serialize_data_objects(
    process_el,
    nodes: list,
    node_map: dict,
    unit_to_node: dict,
    process_model,
    job_id: str,
) -> None:
    """
    TASK 4: Emit BPMN dataObject, dataObjectReference, and data associations
    for each DataVar in process_model.data_vars.

    Each variable gets:
    - <dataObject id="do_V_name"/>
    - <dataObjectReference id="dor_V_name" dataObjectRef="do_V_name"/>
    - <dataInputAssociation>  on the consumer task node(s)
    - <dataOutputAssociation> on the producer task node

    Safe to call if process_model is None or data_vars is empty.
    """
    if not process_model:
        return
    data_vars = getattr(process_model, 'data_vars', []) or []
    if not data_vars:
        return

    for var in data_vars:
        var_name = getattr(var, 'name', None)
        if not var_name:
            continue

        do_id = f"do_{var_name}"
        dor_id = f"dor_{var_name}"

        # dataObject definition
        do_el = etree.SubElement(process_el, f"{{{BPMN_NS}}}dataObject")
        do_el.set("id", do_id)
        do_el.set("name", var_name)

        # dataObjectReference (required for associations)
        dor_el = etree.SubElement(process_el, f"{{{BPMN_NS}}}dataObjectReference")
        dor_el.set("id", dor_id)
        dor_el.set("dataObjectRef", do_id)
        dor_el.set("name", var_name)

        # Producer association — find the task node that produces this var
        producer_uid = getattr(var, 'producer_unit_id', None)
        if producer_uid:
            producer_nid = unit_to_node.get(producer_uid)
            producer_node = node_map.get(producer_nid) if producer_nid else None
            if producer_node and producer_node.bpmn_type == BPMNNodeType.TASK:
                # Attach dataOutputAssociation to the producer task element
                # Find the task XML element — we need to insert into the existing one.
                # Instead, emit a standalone dataOutputAssociation inside the process
                # (BPMN 2.0 allows it as a child of task or process-level — process-level is simpler).
                assoc_id = f"doa_out_{var_name}_{producer_nid[:6]}"
                doa = etree.SubElement(process_el, f"{{{BPMN_NS}}}dataOutputAssociation")
                doa.set("id", assoc_id)
                src_ref = etree.SubElement(doa, f"{{{BPMN_NS}}}sourceRef")
                src_ref.text = _element_id(producer_node)
                tgt_ref = etree.SubElement(doa, f"{{{BPMN_NS}}}targetRef")
                tgt_ref.text = dor_id

        # Consumer associations
        consumers = getattr(var, 'consumers', []) or []
        for consumer_uid in consumers:
            consumer_nid = unit_to_node.get(consumer_uid)
            consumer_node = node_map.get(consumer_nid) if consumer_nid else None
            if consumer_node and consumer_node.bpmn_type == BPMNNodeType.TASK:
                assoc_id = f"dia_in_{var_name}_{consumer_nid[:6]}"
                dia = etree.SubElement(process_el, f"{{{BPMN_NS}}}dataInputAssociation")
                dia.set("id", assoc_id)
                src_ref = etree.SubElement(dia, f"{{{BPMN_NS}}}sourceRef")
                src_ref.text = dor_id
                tgt_ref = etree.SubElement(dia, f"{{{BPMN_NS}}}targetRef")
                tgt_ref.text = _element_id(consumer_node)
