"""L10 — BPMN Translator: lane-aware layout + BPMN 2.0 XML serialization.

Improvements over the previous version:
- Proper collaboration / participant / laneSet structure (BPMN 2.0 compliant).
  Tools like Camunda Modeler and bpmn.io require a <collaboration> wrapping the
  process when swim lanes are present.
- BPMNShape entries for the participant (pool) and each lane in the DI section.
- BPMNPlane now references the collaboration id (not the process id) when lanes exist.
- Lane-aware layout: each actor gets its own horizontal band; nodes are placed
  inside that band at the correct x/y.
- Unactored nodes (start/end events, converging gateways inserted by L8) are
  resolved to a neighbouring actor lane instead of floating outside all lanes.
- conditionExpression is suppressed on default (is_default=True) flows.
- DataObject: only definitions are emitted (process-level data associations were
  structurally invalid BPMN — they belong inside task elements).
- process element gets a name attribute.
"""
import json
import os
import re
from collections import defaultdict

import networkx as nx
from lxml import etree

import config
from models.schemas import BPMNNodeType, GatewayType, Job

BPMN_NS   = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS     = "http://www.omg.org/spec/DD/20100524/DC"
DI_NS     = "http://www.omg.org/spec/DD/20100524/DI"

NODE_W = {"TASK": 120, "GATEWAY": 50, "START_EVENT": 36, "END_EVENT": 36,
          "BOUNDARY_EVENT": 36, "SUBPROCESS": 120}
NODE_H = {"TASK": 60,  "GATEWAY": 50, "START_EVENT": 36, "END_EVENT": 36,
          "BOUNDARY_EVENT": 36, "SUBPROCESS": 60}
MAX_NODE_W = 120  # widest task — drives column stride
MAX_NODE_H = 60   # tallest non-gateway — drives row stride

COLUMN_STRIDE = MAX_NODE_W + 60   # 180 px per depth column
ROW_STRIDE    = MAX_NODE_H + 30   # 90 px per node row within a lane column
POOL_HEADER_W = 30                # rotated pool-title strip width
LANE_LABEL_W  = 100               # lane-name strip width inside the pool
LANE_PAD_X    = 40                # left padding for first node inside a lane
LANE_PAD_Y    = 25                # top/bottom padding within a lane
MIN_LANE_H    = 120               # minimum lane height
POOL_MARGIN   = 40                # right-side margin after the last column


def _safe_bpmn_filename_part(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^\w\s-]", "", text, flags=re.ASCII)
    s = re.sub(r"[-\s]+", "_", s.strip()).strip("_").lower()
    return s[:max_len] if s else ""


# ── Entry point ───────────────────────────────────────────────────────────────

def run(job: Job) -> None:
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    report_path = os.path.join(config.OUTPUT_DIR, f"{job.job_id}_report.json")

    sop_outputs: list[dict] = []
    output_paths: list[str] = []

    for proc in job.processes:
        if not proc.bpmn_nodes:
            continue

        actor_to_lane: dict[str, str] = proc.__dict__.get("_actor_to_lane", {})
        unit_to_node: dict[str, str] = proc.__dict__.get("_unit_to_task_node", {})

        layout_info = _compute_layout(proc.bpmn_nodes, proc.bpmn_edges, actor_to_lane)

        xml_bytes = _serialize_xml(
            job_id=job.job_id,
            process_local_id=proc.process_id,
            nodes=proc.bpmn_nodes,
            edges=proc.bpmn_edges,
            actor_to_lane=actor_to_lane,
            unit_to_node=unit_to_node,
            process_model=proc,
            layout_info=layout_info,
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
        sop_outputs.append({
            "process_id": proc.process_id,
            "name": proc.name,
            "output_file": bpmn_path,
            "graph_metadata": _graph_metadata(proc.bpmn_nodes, proc.bpmn_edges),
        })

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
        raise LayerError("L10_EMPTY_OUTPUT", "No BPMN files were written.")
    for p in paths:
        if not p or not os.path.exists(p) or os.path.getsize(p) == 0:
            raise LayerError("L10_EMPTY_OUTPUT", f"BPMN output missing or empty: {p}")
    for proc in job.processes:
        missing = [n for n in proc.bpmn_nodes if n.x is None]
        if missing:
            raise LayerError(
                "L10_MISSING_LAYOUT",
                f"{len(missing)} nodes without coordinates in {proc.name!r}.",
            )


# ── Layout ────────────────────────────────────────────────────────────────────

def _rank_list_for_layout(G: nx.DiGraph, nodes) -> list[str]:
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


def _compute_layout(nodes, edges, actor_to_lane: dict) -> dict:
    """
    Assign x/y/width/height to every node using a lane-aware layout.

    Returns a layout_info dict consumed by _serialize_xml for DI shape emission:
      - lane_bounds: {actor: {"y", "height", "slug"}}
      - total_width, total_height
      - effective_actor: {node_id: actor} (includes resolved unactored nodes)
    """
    node_map = {n.node_id: n for n in nodes}

    G = nx.DiGraph()
    for n in nodes:
        G.add_node(n.node_id)
    for e in edges:
        if e.source_node_id in node_map and e.target_node_id in node_map:
            G.add_edge(e.source_node_id, e.target_node_id)

    # Longest-path depth per node (determines column / x-position)
    rank_list = _rank_list_for_layout(G, nodes)
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

    lane_order = list(actor_to_lane.keys())

    # Resolve an actor for every node, including unactored infrastructure nodes
    effective_actor = _resolve_effective_actors(nodes, G, lane_order)

    if not lane_order:
        # No swim lanes — flat layout (depth × 180, single column of rows)
        for i, n in enumerate(nodes):
            ntype = n.bpmn_type.value if n.bpmn_type else "TASK"
            n.width  = NODE_W.get(ntype, 120)
            n.height = NODE_H.get(ntype, 60)
            n.x = 50 + depth_map.get(n.node_id, 0) * COLUMN_STRIDE
            n.y = 50 + i * ROW_STRIDE  # rough stacking; no lane context
        return {"lane_bounds": {}, "total_width": 800, "total_height": 400,
                "effective_actor": effective_actor}

    # Build (actor, depth) → [node_ids] for row counting within each lane column
    lane_depth_groups: dict[tuple, list] = defaultdict(list)
    for n in nodes:
        actor = effective_actor.get(n.node_id)
        if actor:
            lane_depth_groups[(actor, depth_map.get(n.node_id, 0))].append(n.node_id)

    max_depth = max(depth_map.values()) if depth_map else 0

    # Lane heights: enough rows for the busiest column in that lane
    lane_heights: dict[str, float] = {}
    for actor in lane_order:
        max_rows = max(
            (len(lane_depth_groups.get((actor, d), [])) for d in range(max_depth + 1)),
            default=0,
        )
        lane_heights[actor] = max(MIN_LANE_H, max_rows * ROW_STRIDE + 2 * LANE_PAD_Y)

    # Cumulative Y offset for each lane
    lane_y: dict[str, float] = {}
    cumulative = 0.0
    for actor in lane_order:
        lane_y[actor] = cumulative
        cumulative += lane_heights[actor]
    total_height = cumulative

    total_width = (
        POOL_HEADER_W + LANE_LABEL_W + LANE_PAD_X
        + (max_depth + 1) * COLUMN_STRIDE + POOL_MARGIN
    )

    # Assign node positions
    depth_lane_row: dict[tuple, int] = defaultdict(int)  # (actor, depth) → next row index
    for n in nodes:
        ntype   = n.bpmn_type.value if n.bpmn_type else "TASK"
        n.width  = NODE_W.get(ntype, 120)
        n.height = NODE_H.get(ntype, 60)

        actor = effective_actor.get(n.node_id)
        depth = depth_map.get(n.node_id, 0)

        if actor and actor in lane_y:
            row_idx = depth_lane_row[(actor, depth)]
            depth_lane_row[(actor, depth)] += 1
            # Centre node horizontally within the column (wider tasks vs narrow gateways)
            node_x = (POOL_HEADER_W + LANE_LABEL_W + LANE_PAD_X
                      + depth * COLUMN_STRIDE + (MAX_NODE_W - n.width) // 2)
            # Centre node vertically within its row slot
            node_y = (lane_y[actor] + LANE_PAD_Y
                      + row_idx * ROW_STRIDE + (MAX_NODE_H - n.height) // 2)
        else:
            node_x = 50 + depth * COLUMN_STRIDE
            node_y = 50

        n.x = node_x
        n.y = node_y

    # Build lane_bounds for DI emission
    lane_bounds = {
        actor: {"y": lane_y[actor], "height": lane_heights[actor], "slug": actor_to_lane[actor]}
        for actor in lane_order
    }

    return {
        "lane_bounds": lane_bounds,
        "total_width": total_width,
        "total_height": total_height,
        "effective_actor": effective_actor,
    }


def _resolve_effective_actors(nodes, G: nx.DiGraph, lane_order: list) -> dict:
    """
    Return {node_id: actor} for every node.

    Nodes with a genuine actor use it directly. Unactored nodes (start/end events,
    converging gateways added by L8) are assigned to a neighbouring lane via BFS
    over the graph. Last resort: first lane.
    """
    lane_set = set(lane_order)
    effective: dict[str, str] = {}

    for n in nodes:
        if n.actor and n.actor in lane_set:
            effective[n.node_id] = n.actor

    # Iterative neighbourhood resolution (handles chains of unactored nodes)
    changed = True
    while changed:
        changed = False
        for n in nodes:
            if n.node_id in effective:
                continue
            for neighbour in list(G.successors(n.node_id)) + list(G.predecessors(n.node_id)):
                nb_actor = effective.get(neighbour)
                if nb_actor and nb_actor in lane_set:
                    effective[n.node_id] = nb_actor
                    changed = True
                    break

    # Last resort
    if lane_order:
        for n in nodes:
            if n.node_id not in effective:
                effective[n.node_id] = lane_order[0]

    return effective


# ── XML serialisation ─────────────────────────────────────────────────────────

def _serialize_xml(
    job_id: str,
    process_local_id: str,
    nodes,
    edges,
    actor_to_lane: dict,
    unit_to_node: dict | None = None,
    process_model=None,
    layout_info: dict | None = None,
) -> bytes:
    has_lanes = bool(actor_to_lane)
    proc_xml_id    = f"process_{job_id}_{process_local_id}"
    collab_xml_id  = f"collab_{job_id}_{process_local_id}"
    part_xml_id    = f"participant_{job_id}_{process_local_id}"
    process_name   = getattr(process_model, "name", "") or ""

    nsmap = {None: BPMN_NS, "bpmndi": BPMNDI_NS, "dc": DC_NS, "di": DI_NS}
    definitions = etree.Element(f"{{{BPMN_NS}}}definitions", nsmap=nsmap)
    definitions.set("id", f"definitions_{job_id}_{process_local_id}")
    definitions.set("targetNamespace", "http://bpmn.io/schema/bpmn")

    # ── Collaboration + participant (required for swim lanes) ──────────────────
    if has_lanes:
        collab_el = etree.SubElement(definitions, f"{{{BPMN_NS}}}collaboration")
        collab_el.set("id", collab_xml_id)
        part_el = etree.SubElement(collab_el, f"{{{BPMN_NS}}}participant")
        part_el.set("id", part_xml_id)
        part_el.set("name", process_name)
        part_el.set("processRef", proc_xml_id)

    # ── Process element ───────────────────────────────────────────────────────
    process_el = etree.SubElement(definitions, f"{{{BPMN_NS}}}process")
    process_el.set("id", proc_xml_id)
    process_el.set("name", process_name)
    process_el.set("isExecutable", "false")

    node_map = {n.node_id: n for n in nodes}

    # ── LaneSet ───────────────────────────────────────────────────────────────
    if has_lanes:
        lane_set_el = etree.SubElement(process_el, f"{{{BPMN_NS}}}laneSet")
        lane_set_el.set("id", "laneSet_1")
        for actor, slug in actor_to_lane.items():
            lane_el = etree.SubElement(lane_set_el, f"{{{BPMN_NS}}}lane")
            lane_el.set("id", f"lane_{slug}")
            lane_el.set("name", actor)
            # Only reference nodes that truly own this actor (not effective-resolved ones)
            for n in nodes:
                if n.actor == actor:
                    fn = etree.SubElement(lane_el, f"{{{BPMN_NS}}}flowNodeRef")
                    fn.text = _element_id(n)

    # ── Flow elements (tasks, gateways, events) ────────────────────────────────
    for node in nodes:
        el = etree.SubElement(process_el, _bpmn_element(node))
        el.set("id", _element_id(node))
        el.set("name", node.label or "")
        if node.bpmn_type == BPMNNodeType.BOUNDARY_EVENT:
            etree.SubElement(el, f"{{{BPMN_NS}}}errorEventDefinition")

    # ── Sequence flows ────────────────────────────────────────────────────────
    for edge in edges:
        seq = etree.SubElement(process_el, f"{{{BPMN_NS}}}sequenceFlow")
        seq.set("id", f"flow_{edge.edge_id}")
        seq.set(
            "sourceRef",
            _element_id(node_map[edge.source_node_id])
            if edge.source_node_id in node_map else edge.source_node_id,
        )
        seq.set(
            "targetRef",
            _element_id(node_map[edge.target_node_id])
            if edge.target_node_id in node_map else edge.target_node_id,
        )
        if edge.label:
            seq.set("name", edge.label)
        if edge.is_default:
            seq.set("isDefault", "true")
        # conditionExpression must NOT appear on default flows
        if not edge.is_default and getattr(edge, "condition_variable", None):
            cond_val = getattr(edge, "condition_value", None) or "true"
            cond_expr = etree.SubElement(seq, f"{{{BPMN_NS}}}conditionExpression")
            cond_expr.text = f"${{{edge.condition_variable}}} == {cond_val}"

    # ── Review annotations ────────────────────────────────────────────────────
    for node in nodes:
        if not node.needs_review:
            continue
        reason = "; ".join(node.review_reasons) if node.review_reasons else "Needs review"
        ann_id   = f"ann_{node.node_id}"
        assoc_id = f"assoc_{node.node_id}"
        ann_el = etree.SubElement(process_el, f"{{{BPMN_NS}}}textAnnotation")
        ann_el.set("id", ann_id)
        text_el = etree.SubElement(ann_el, f"{{{BPMN_NS}}}text")
        text_el.text = f"\u26a0 Review: {reason[:120]}"
        assoc_el = etree.SubElement(process_el, f"{{{BPMN_NS}}}association")
        assoc_el.set("id", assoc_id)
        assoc_el.set("sourceRef", _element_id(node))
        assoc_el.set("targetRef", ann_id)
        assoc_el.set("associationDirection", "None")

    # ── BPMNDiagram ───────────────────────────────────────────────────────────
    diagram = etree.SubElement(definitions, f"{{{BPMNDI_NS}}}BPMNDiagram")
    diagram.set("id", f"diagram_{process_local_id}")
    plane = etree.SubElement(diagram, f"{{{BPMNDI_NS}}}BPMNPlane")
    plane.set("id", f"plane_{process_local_id}")
    # Plane references the collaboration when lanes exist, otherwise the process
    plane.set("bpmnElement", collab_xml_id if has_lanes else proc_xml_id)

    li = layout_info or {}
    total_w = li.get("total_width", 800)
    total_h = li.get("total_height", 400)
    lane_bounds = li.get("lane_bounds", {})

    if has_lanes:
        # Participant (pool) shape — covers the whole diagram
        ps = etree.SubElement(plane, f"{{{BPMNDI_NS}}}BPMNShape")
        ps.set("id", f"shape_{part_xml_id}")
        ps.set("bpmnElement", part_xml_id)
        ps.set("isHorizontal", "true")
        _bounds(ps, 0, 0, int(total_w), int(total_h))

        # Lane shapes
        for actor, slug in actor_to_lane.items():
            info = lane_bounds.get(actor, {})
            ls = etree.SubElement(plane, f"{{{BPMNDI_NS}}}BPMNShape")
            ls.set("id", f"shape_lane_{slug}")
            ls.set("bpmnElement", f"lane_{slug}")
            ls.set("isHorizontal", "true")
            _bounds(
                ls,
                POOL_HEADER_W,
                int(info.get("y", 0)),
                int(total_w - POOL_HEADER_W),
                int(info.get("height", MIN_LANE_H)),
            )

    # Node shapes
    for node in nodes:
        shape = etree.SubElement(plane, f"{{{BPMNDI_NS}}}BPMNShape")
        shape.set("id", f"shape_{node.node_id}")
        shape.set("bpmnElement", _element_id(node))
        if node.bpmn_type == BPMNNodeType.GATEWAY:
            shape.set("isMarkerVisible", "true")
        _bounds(shape, int(node.x or 0), int(node.y or 0),
                int(node.width or 120), int(node.height or 60))

    # Annotation shapes
    for node in nodes:
        if not node.needs_review:
            continue
        ann_id = f"ann_{node.node_id}"
        ann_s = etree.SubElement(plane, f"{{{BPMNDI_NS}}}BPMNShape")
        ann_s.set("id", f"shape_{ann_id}")
        ann_s.set("bpmnElement", ann_id)
        _bounds(ann_s,
                int((node.x or 0) + (node.width or 120) + 20),
                int((node.y or 0) - 40),
                180, 40)

    # Edge shapes
    for edge in edges:
        edge_shape = etree.SubElement(plane, f"{{{BPMNDI_NS}}}BPMNEdge")
        edge_shape.set("id", f"edge_{edge.edge_id}")
        edge_shape.set("bpmnElement", f"flow_{edge.edge_id}")
        src = node_map.get(edge.source_node_id)
        tgt = node_map.get(edge.target_node_id)
        if src and tgt:
            sx = (src.x or 0) + (src.width or 120)
            sy = (src.y or 0) + (src.height or 60) / 2
            tx = tgt.x or 0
            ty = (tgt.y or 0) + (tgt.height or 60) / 2
            # Add a mid-waypoint when the edge crosses lanes (different Y bands)
            waypoints = [(sx, sy), (tx, ty)]
            if abs(sy - ty) > ROW_STRIDE:
                mid_x = (sx + tx) / 2
                waypoints = [(sx, sy), (mid_x, sy), (mid_x, ty), (tx, ty)]
            for wx, wy in waypoints:
                wp = etree.SubElement(edge_shape, f"{{{DI_NS}}}waypoint")
                wp.set("x", str(int(wx)))
                wp.set("y", str(int(wy)))

    return etree.tostring(definitions, pretty_print=True, xml_declaration=True, encoding="UTF-8")


def _bounds(parent_el, x: int, y: int, width: int, height: int) -> None:
    """Append a <dc:Bounds> child to a BPMNShape element."""
    b = etree.SubElement(parent_el, f"{{{DC_NS}}}Bounds")
    b.set("x", str(x))
    b.set("y", str(y))
    b.set("width", str(width))
    b.set("height", str(height))


def _emit_data_objects(process_el, process_model) -> list[tuple]:
    """
    Emit <dataObject> + <dataObjectReference> elements for each DataVar.
    Returns a list of (dor_id, approx_x, approx_y) so the caller can add DI shapes.

    Note: dataInputAssociation / dataOutputAssociation are NOT emitted here
    because in BPMN 2.0 they must be children of the activity element, not the
    process. The caller would need the live task XML element to do this correctly.
    """
    if not process_model:
        return []
    data_vars = getattr(process_model, "data_vars", []) or []
    if not data_vars:
        return []

    placed: list[tuple] = []
    for i, var in enumerate(data_vars):
        var_name = getattr(var, "name", None)
        if not var_name:
            continue
        do_id  = f"do_{_slug(var_name)}"
        dor_id = f"dor_{_slug(var_name)}"

        do_el = etree.SubElement(process_el, f"{{{BPMN_NS}}}dataObject")
        do_el.set("id", do_id)
        do_el.set("name", var_name)

        dor_el = etree.SubElement(process_el, f"{{{BPMN_NS}}}dataObjectReference")
        dor_el.set("id", dor_id)
        dor_el.set("dataObjectRef", do_id)
        dor_el.set("name", var_name)

        # Place data objects below the diagram in a compact row
        do_x = POOL_HEADER_W + LANE_LABEL_W + i * 60
        do_y = -80  # negative Y — tools typically render these below the pool
        placed.append((dor_id, do_x, do_y))

    return placed


# ── XML helpers ───────────────────────────────────────────────────────────────

def _validate_xml(xml_bytes: bytes) -> None:
    try:
        etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as e:
        raise LayerError("L10_INVALID_XML", str(e))


def _bpmn_element(node) -> str:
    mapping = {
        BPMNNodeType.START_EVENT:    f"{{{BPMN_NS}}}startEvent",
        BPMNNodeType.END_EVENT:      f"{{{BPMN_NS}}}endEvent",
        BPMNNodeType.TASK:           f"{{{BPMN_NS}}}userTask",
        BPMNNodeType.GATEWAY:        _gateway_tag(node),
        BPMNNodeType.BOUNDARY_EVENT: f"{{{BPMN_NS}}}boundaryEvent",
        BPMNNodeType.SUBPROCESS:     f"{{{BPMN_NS}}}subProcess",
    }
    return mapping.get(node.bpmn_type, f"{{{BPMN_NS}}}task")


def _gateway_tag(node) -> str:
    return {
        GatewayType.EXCLUSIVE:   f"{{{BPMN_NS}}}exclusiveGateway",
        GatewayType.PARALLEL:    f"{{{BPMN_NS}}}parallelGateway",
        GatewayType.EVENT_BASED: f"{{{BPMN_NS}}}eventBasedGateway",
    }.get(node.gateway_type, f"{{{BPMN_NS}}}exclusiveGateway")


def _element_id(node) -> str:
    prefix = {
        BPMNNodeType.START_EVENT:    "start",
        BPMNNodeType.END_EVENT:      "end",
        BPMNNodeType.TASK:           "task",
        BPMNNodeType.GATEWAY:        "gateway",
        BPMNNodeType.BOUNDARY_EVENT: "boundary",
        BPMNNodeType.SUBPROCESS:     "subprocess",
    }.get(node.bpmn_type, "node")
    return f"{prefix}_{node.node_id}"


def _slug(text: str) -> str:
    return re.sub(r"[^\w]", "_", text.lower())[:40]


# ── Reporting ─────────────────────────────────────────────────────────────────

def _graph_metadata(nodes, edges) -> dict:
    node_types: dict[str, int] = {}
    for n in nodes:
        k = n.bpmn_type.value if n.bpmn_type else "UNKNOWN"
        node_types[k] = node_types.get(k, 0) + 1

    actors  = {n.actor for n in nodes if n.actor}
    gw_types: dict[str, int] = {}
    for n in nodes:
        if n.bpmn_type == BPMNNodeType.GATEWAY and n.gateway_type:
            k = n.gateway_type.value
            gw_types[k] = gw_types.get(k, 0) + 1

    return {
        "node_count":    len(nodes),
        "edge_count":    len(edges),
        "lane_count":    len(actors),
        "node_types":    node_types,
        "gateway_types": gw_types,
    }


def _build_report(job: Job, sop_outputs: list[dict]) -> dict:
    total_nodes = sum(o["graph_metadata"]["node_count"] for o in sop_outputs)
    total_edges = sum(o["graph_metadata"]["edge_count"] for o in sop_outputs)

    review_flags = [
        {"chunk_id": f.chunk_id, "layer": f.layer, "reason": f.reason}
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
        "job_id":      job.job_id,
        "sop_class":   job.sop_class,
        "status":      job.status.value,
        "graph_metadata": {
            "sop_count":         len(sop_outputs),
            "node_count_total":  total_nodes,
            "edge_count_total":  total_edges,
        },
        "sop_outputs":   sop_outputs,
        "review_flags":  review_flags,
        "llm_call_log":  llm_log,
        "output_files":  [o["output_file"] for o in sop_outputs],
        "output_file":   first_path,
    }


# ── Exceptions ────────────────────────────────────────────────────────────────

class LayerError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class SoftGateFailure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
