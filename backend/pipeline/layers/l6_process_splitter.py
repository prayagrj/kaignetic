"""L6 — Process Splitter

Decides which weakly-connected components of the BPMN graph should become
independent BPMN files vs. which should be re-joined as a continuation of
the same workflow.

Decision pipeline per candidate split:
  1. Trivial pass   — single component → nothing to do.
  2. Size guard     — components with fewer than MIN_COMPONENT_NODES nodes
                      are always merged back into the nearest component.
  3. Heuristic score — each component pair is scored on:
                        • shared actors                (high weight)
                        • heading similarity           (medium weight)
                        • cross-boundary edge density  (low weight — often 0)
                      If the score exceeds MERGE_HEURISTIC_THRESHOLD the pair
                      is merged without an LLM call.
  4. LLM arbiter    — borderline pairs (score in the ambiguous band) are
                      passed to the LLM for a final merge/split decision.
                      Confident split pairs (very low score) are kept split.

After the component membership is settled, each component is assembled into
a ProcessModel with fresh START / END events.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

import networkx as nx

from llm.client import LLMClient
from llm.prompts import PROCESS_SPLIT_SYSTEM, PROCESS_SPLIT_USER
from models.schemas import (
    BPMNEdge, BPMNNode, BPMNNodeType, GatewayType, Job, ProcessModel,
)

# ── Tuning knobs ──────────────────────────────────────────────────────────────
# A component must have at least this many content nodes to be kept separate.
MIN_COMPONENT_NODES = 3

# Pair score at or above this → auto-merge (no LLM call needed).
MERGE_HEURISTIC_THRESHOLD = 0.55

# Pair score below this → confident split (no LLM call needed).
SPLIT_HEURISTIC_THRESHOLD = 0.20

# LLM confidence floor — below this the LLM answer is ignored.
LLM_CONFIDENCE_FLOOR = 0.55


# ── Public entry point ────────────────────────────────────────────────────────

def run(job: Job) -> None:
    llm = LLMClient(job)
    result: list[ProcessModel] = []
    for process in job.processes:
        result.extend(_split_process(process, job, llm))
    job.processes = result


# ── Core split logic ─────────────────────────────────────────────────────────

def _split_process(process: ProcessModel, job: Job, llm: LLMClient) -> list[ProcessModel]:
    """Analyse one ProcessModel and return 1..N ProcessModels."""
    # Content nodes only (no bare START/END injected by earlier layers)
    content_nodes = [
        n for n in process.bpmn_nodes
        if n.bpmn_type in {BPMNNodeType.TASK, BPMNNodeType.GATEWAY}
        or (n.bpmn_type == BPMNNodeType.END_EVENT and n.unit_id)
    ]
    content_ids = {n.node_id for n in content_nodes}

    if not content_nodes:
        return [process]

    # Build undirected graph of content nodes only
    G = nx.Graph()
    G.add_nodes_from(content_ids)
    for e in process.bpmn_edges:
        if e.source_node_id in content_ids and e.target_node_id in content_ids:
            G.add_edge(e.source_node_id, e.target_node_id)

    raw_components = list(nx.connected_components(G))

    if len(raw_components) <= 1:
        return [_assemble(process, content_nodes, process.bpmn_edges, job)]

    node_map = {n.node_id: n for n in content_nodes}

    # ── Phase 1: absorb tiny fragments ───────────────────────────────────────
    components = _absorb_tiny_fragments(raw_components, node_map, process)

    if len(components) == 1:
        all_ids = components[0]
        return [_assemble(process, [node_map[i] for i in all_ids if i in node_map],
                          process.bpmn_edges, job)]

    # ── Phase 2: build component metadata for scoring ─────────────────────────
    comp_meta = [_component_meta(comp_ids, node_map, process) for comp_ids in components]

    # ── Phase 3: merge passes ────────────────────────────────────────────────
    # Iteratively evaluate adjacent component pairs (document order).
    # We keep going until no more merges happen in a pass.
    changed = True
    while changed and len(comp_meta) > 1:
        changed = False
        new_meta: list[_CompMeta] = []
        i = 0
        while i < len(comp_meta):
            if i + 1 < len(comp_meta):
                score = _pair_score(comp_meta[i], comp_meta[i + 1], process)
                decision = _decide(score, comp_meta[i], comp_meta[i + 1], llm, process, job)
                if decision == "merge":
                    merged = _merge_meta(comp_meta[i], comp_meta[i + 1], node_map, process)
                    new_meta.append(merged)
                    i += 2
                    changed = True
                    continue
            new_meta.append(comp_meta[i])
            i += 1
        comp_meta = new_meta

    # ── Phase 4: assemble final ProcessModels ────────────────────────────────
    result: list[ProcessModel] = []
    for cm in comp_meta:
        cm_nodes = [node_map[nid] for nid in cm.node_ids if nid in node_map]
        result.append(_assemble(process, cm_nodes, process.bpmn_edges, job,
                                name=cm.name, proc_id=cm.proc_id))

    print(f"[L6-Split] '{process.name}' → {len(result)} process(es)")
    return result


# ── Component metadata ────────────────────────────────────────────────────────

@dataclass
class _CompMeta:
    proc_id: str
    name: str
    node_ids: set[str]
    actors: set[str]
    top_headings: list[str]   # document section headings present in this component
    doc_position: float       # mean document-order index of its nodes


def _component_meta(
    comp_ids: set[str],
    node_map: dict[str, BPMNNode],
    process: ProcessModel,
) -> _CompMeta:
    unit_to_node = process.__dict__.get("_unit_to_task_node", {})
    node_to_unit = {nid: uid for uid, nid in unit_to_node.items()}
    unit_map = {u.unit_id: u for u in process.atomic_units}
    chunk_map = {c.chunk_id: c for c in process.chunks}

    actors: set[str] = set()
    headings: list[str] = []
    positions: list[int] = []

    ordered_ids = [n.node_id for n in process.bpmn_nodes]

    for nid in comp_ids:
        node = node_map.get(nid)
        if not node:
            continue
        if node.actor:
            actors.add(node.actor)
        uid = node_to_unit.get(nid)
        unit = unit_map.get(uid) if uid else None
        chunk = chunk_map.get(unit.chunk_id) if unit else None
        if chunk and chunk.headings:
            headings.extend(chunk.headings)
        if nid in ordered_ids:
            positions.append(ordered_ids.index(nid))

    # deduplicate headings preserving order
    seen: set[str] = set()
    unique_headings: list[str] = []
    for h in headings:
        if h not in seen:
            seen.add(h)
            unique_headings.append(h)

    # Name: most common top-level heading
    top_counts: dict[str, int] = defaultdict(int)
    for h in headings:
        top = h.split(" > ")[0] if " > " in h else h
        top_counts[top] += 1
    name = max(top_counts, key=top_counts.get) if top_counts else "Sub-process"

    # Task count for name disambiguation
    task_count = sum(1 for nid in comp_ids
                     if node_map.get(nid) and node_map[nid].bpmn_type == BPMNNodeType.TASK)
    label_node = next(
        (node_map[nid] for nid in comp_ids
         if nid in node_map and node_map[nid].bpmn_type == BPMNNodeType.TASK
         and node_map[nid].label),
        None,
    )
    if label_node:
        name = label_node.label[:50]

    return _CompMeta(
        proc_id=str(uuid.uuid4())[:8],
        name=name,
        node_ids=set(comp_ids),
        actors=actors,
        top_headings=unique_headings,
        doc_position=sum(positions) / len(positions) if positions else 0,
    )


def _merge_meta(a: _CompMeta, b: _CompMeta, node_map: dict[str, BPMNNode],
                process: ProcessModel) -> _CompMeta:
    merged_ids = a.node_ids | b.node_ids
    merged_actors = a.actors | b.actors

    seen: set[str] = set()
    merged_headings: list[str] = []
    for h in a.top_headings + b.top_headings:
        if h not in seen:
            seen.add(h)
            merged_headings.append(h)

    # Name from whichever component appears first in document order
    name = a.name if a.doc_position <= b.doc_position else b.name
    pos = (a.doc_position + b.doc_position) / 2

    return _CompMeta(
        proc_id=a.proc_id,
        name=name,
        node_ids=merged_ids,
        actors=merged_actors,
        top_headings=merged_headings,
        doc_position=pos,
    )


# ── Pair scoring ──────────────────────────────────────────────────────────────

def _pair_score(a: _CompMeta, b: _CompMeta, process: ProcessModel) -> float:
    """Return a score in [0, 1] indicating how likely a and b should be merged.
    Higher = more likely same workflow."""
    score = 0.0

    # Shared actors — strongest signal
    if a.actors and b.actors:
        overlap = len(a.actors & b.actors) / max(len(a.actors | b.actors), 1)
        score += 0.45 * overlap

    # Heading similarity — do they share a top-level section heading?
    a_tops = {h.split(" > ")[0] for h in a.top_headings}
    b_tops = {h.split(" > ")[0] for h in b.top_headings}
    if a_tops and b_tops:
        overlap = len(a_tops & b_tops) / max(len(a_tops | b_tops), 1)
        score += 0.35 * overlap

    # Document adjacency — components that appear consecutively are more likely
    # to be continuations than components that have many nodes between them.
    all_node_positions = [n.node_id for n in process.bpmn_nodes]
    total = max(len(all_node_positions), 1)
    position_gap = abs(a.doc_position - b.doc_position) / total
    adjacency = max(0.0, 1.0 - position_gap * 2)
    score += 0.20 * adjacency

    return min(score, 1.0)


# ── Decision logic ────────────────────────────────────────────────────────────

def _decide(
    score: float,
    a: _CompMeta,
    b: _CompMeta,
    llm: LLMClient,
    process: ProcessModel,
    job: Job,
) -> str:
    """Return 'merge' or 'split'."""
    if score >= MERGE_HEURISTIC_THRESHOLD:
        return "merge"
    if score < SPLIT_HEURISTIC_THRESHOLD:
        return "split"
    # Ambiguous band — ask LLM
    return _llm_decide(a, b, llm, process, job)


def _llm_decide(
    a: _CompMeta,
    b: _CompMeta,
    llm: LLMClient,
    process: ProcessModel,
    job: Job,
) -> str:
    node_map = {n.node_id: n for n in process.bpmn_nodes}

    def _node_summaries(ids: set[str]) -> list[dict]:
        summaries = []
        for nid in ids:
            n = node_map.get(nid)
            if n and n.bpmn_type == BPMNNodeType.TASK:
                summaries.append({"label": n.label, "actor": n.actor or ""})
        return summaries[:12]  # cap to keep prompt small

    payload_a = {
        "name": a.name,
        "actors": sorted(a.actors),
        "headings": a.top_headings[:5],
        "sample_tasks": _node_summaries(a.node_ids),
    }
    payload_b = {
        "name": b.name,
        "actors": sorted(b.actors),
        "headings": b.top_headings[:5],
        "sample_tasks": _node_summaries(b.node_ids),
    }

    result = llm.call(
        layer=6,
        template_name="PROCESS_SPLIT",
        system_prompt=PROCESS_SPLIT_SYSTEM,
        user_prompt=PROCESS_SPLIT_USER.format(
            process_a_json=json.dumps(payload_a, indent=2),
            process_b_json=json.dumps(payload_b, indent=2),
        ),
    )

    if not isinstance(result, dict):
        return "split"

    decision = result.get("decision", "split")
    confidence = float(result.get("confidence", 0.0))
    if confidence < LLM_CONFIDENCE_FLOOR:
        return "split"

    return "merge" if decision == "merge" else "split"


# ── Tiny-fragment absorption ──────────────────────────────────────────────────

def _absorb_tiny_fragments(
    components: list[set[str]],
    node_map: dict[str, BPMNNode],
    process: ProcessModel,
) -> list[set[str]]:
    """Merge any component smaller than MIN_COMPONENT_NODES into its nearest
    (by document order) larger neighbour."""
    ordered_ids = [n.node_id for n in process.bpmn_nodes]

    def _mean_pos(comp: set[str]) -> float:
        positions = [ordered_ids.index(nid) for nid in comp if nid in ordered_ids]
        return sum(positions) / len(positions) if positions else 0.0

    def _task_count(comp: set[str]) -> int:
        return sum(1 for nid in comp
                   if nid in node_map and node_map[nid].bpmn_type == BPMNNodeType.TASK)

    changed = True
    result = list(components)
    while changed:
        changed = False
        small = [c for c in result if _task_count(c) < MIN_COMPONENT_NODES]
        if not small:
            break
        frag = small[0]
        result.remove(frag)
        if not result:
            result.append(frag)
            break
        frag_pos = _mean_pos(frag)
        nearest = min(result, key=lambda c: abs(_mean_pos(c) - frag_pos))
        nearest |= frag
        changed = True

    return result


# ── ProcessModel assembly ─────────────────────────────────────────────────────

def _assemble(
    source: ProcessModel,
    content_nodes: list[BPMNNode],
    all_edges: list[BPMNEdge],
    job: Job,
    name: Optional[str] = None,
    proc_id: Optional[str] = None,
) -> ProcessModel:
    """Build a ProcessModel from a subset of content nodes, wiring START/END."""
    comp_ids = {n.node_id for n in content_nodes}
    proc_id = proc_id or source.process_id
    name = name or source.name

    # Fresh START event
    start = BPMNNode(
        node_id=f"start_{proc_id}",
        job_id=job.job_id,
        bpmn_type=BPMNNodeType.START_EVENT,
        label="Start",
    )

    # Filter edges internal to this component
    comp_edges = [
        e for e in all_edges
        if e.source_node_id in comp_ids and e.target_node_id in comp_ids
    ]

    # Document-ordered content nodes
    ordered_all = [n.node_id for n in source.bpmn_nodes]
    ordered_comp = [nid for nid in ordered_all if nid in comp_ids]

    # Terminal END_EVENT nodes (those with unit_id — explicit "process ends" steps)
    terminal_ids = {n.node_id for n in content_nodes if n.bpmn_type == BPMNNodeType.END_EVENT}
    ordered_non_terminal = [nid for nid in ordered_comp if nid not in terminal_ids]

    extra_nodes: list[BPMNNode] = []

    # Wire START → first content node
    if ordered_comp:
        comp_edges.append(_make_edge(job.job_id, start.node_id, ordered_comp[0]))

    if terminal_ids:
        # Ensure every terminal has an incoming edge
        has_incoming = {e.target_node_id for e in comp_edges}
        for tid in terminal_ids:
            if tid not in has_incoming:
                idx = ordered_comp.index(tid)
                predecessors = [nid for nid in ordered_comp[:idx] if nid not in terminal_ids]
                if predecessors:
                    comp_edges.append(_make_edge(job.job_id, predecessors[-1], tid))
    else:
        # Inject a fallback END_EVENT after the last non-terminal node
        end = BPMNNode(
            node_id=f"end_{proc_id}",
            job_id=job.job_id,
            bpmn_type=BPMNNodeType.END_EVENT,
            label="End",
        )
        extra_nodes.append(end)
        if ordered_non_terminal:
            comp_edges.append(_make_edge(job.job_id, ordered_non_terminal[-1], end.node_id))

    # Carry over atomic units and chunks
    unit_to_node: dict[str, str] = source.__dict__.get("_unit_to_task_node", {})
    comp_unit_ids = {uid for uid, nid in unit_to_node.items() if nid in comp_ids}
    comp_atomic = [u for u in source.atomic_units if u.unit_id in comp_unit_ids]
    comp_chunk_ids = {u.chunk_id for u in comp_atomic}
    comp_chunks = [c for c in source.chunks if c.chunk_id in comp_chunk_ids]

    sp = ProcessModel(
        process_id=proc_id,
        name=name,
        chunks=comp_chunks,
        atomic_units=comp_atomic,
        bpmn_nodes=[start] + content_nodes + extra_nodes,
        bpmn_edges=comp_edges,
        preamble=source.preamble,
        actor_heading_map=source.actor_heading_map,
    )
    sp.__dict__["_unit_to_task_node"] = {
        uid: nid for uid, nid in unit_to_node.items() if nid in comp_ids
    }
    sp.__dict__["_actor_to_lane"] = source.__dict__.get("_actor_to_lane", {})
    return sp


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_edge(job_id: str, src: str, tgt: str) -> BPMNEdge:
    return BPMNEdge(
        edge_id=uuid.uuid4().hex[:8],
        job_id=job_id,
        source_node_id=src,
        target_node_id=tgt,
    )
