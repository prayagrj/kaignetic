import json
import uuid
from collections import defaultdict

import config
from llm.client import LLMClient
from llm.prompts import ATOMIZE_WITH_CONTEXT_SYSTEM, ATOMIZE_WITH_CONTEXT_USER
from models.schemas import AtomicDecisionUnit, AtomicStepUnit, DecisionBranch, Job
from pipeline.utils.chunker import trim_previous_context


def run(job: Job) -> None:
    llm = LLMClient(job)

    for process in job.processes:
        target_chunks = process.chunks
        if not target_chunks:
            continue

        all_units: list[AtomicStepUnit | AtomicDecisionUnit] = []

        # Group by top-level heading for coherent LLM calls
        groups: dict[str, list] = {}
        for chunk in target_chunks:
            key = chunk.headings[0] if chunk.headings else "root"
            groups.setdefault(key, []).append(chunk)

        for section_context, group_chunks in groups.items():
            llm_previous: list[dict] = []
            batch_size = config.L4_BATCH_SIZE

            for batch_start in range(0, len(group_chunks), batch_size):
                batch = group_chunks[batch_start: batch_start + batch_size]

                target_blocks = [
                    {
                        "block_id": chunk.chunk_id,
                        "section_headings": chunk.headings,
                        "text": chunk.contextualized,
                    }
                    for chunk in batch
                ]
                target_blocks_text = json.dumps(target_blocks, ensure_ascii=False, indent=2)

                window = config.L4_WINDOW_BLOCKS
                context_snippets = [
                    {"block_id": c.chunk_id, "snippet": c.contextualized[:150]}
                    for c in group_chunks[max(0, batch_start - window): batch_start]
                ] + [
                    {"block_id": c.chunk_id, "snippet": c.contextualized[:150]}
                    for c in group_chunks[batch_start + len(batch): batch_start + len(batch) + window]
                ]
                context_blocks_text = json.dumps(context_snippets, ensure_ascii=False) if context_snippets else "[]"

                result = llm.call(
                    layer=4,
                    template_name="ATOMIZE_WITH_CONTEXT",
                    system_prompt=ATOMIZE_WITH_CONTEXT_SYSTEM,
                    user_prompt=ATOMIZE_WITH_CONTEXT_USER.format(
                        section_context=section_context,
                        preamble_context="None",
                        context_blocks_text=context_blocks_text,
                        target_blocks_text=target_blocks_text,
                    )
                )

                batch_id_map = {c.chunk_id: c for c in batch}

                if result and isinstance(result, list):
                    llm_previous = trim_previous_context(llm_previous + result, keep=2)

                    batch_units: list[AtomicStepUnit | AtomicDecisionUnit] = []

                    for item in result:
                        cid = item.get("block_id")
                        chunk = batch_id_map.get(cid)
                        if not chunk:
                            continue

                        for u_dict in item.get("atomic_units", []):
                            action = (u_dict.get("action") or "").strip()
                            actor = (u_dict.get("actor") or "Unknown").strip()
                            local_id = (u_dict.get("unit_id") or "").strip() or str(uuid.uuid4())[:8]

                            if not action:
                                chunk.needs_review = True
                                chunk.review_reasons.append("Empty action in atomic unit")
                                continue

                            step_type = (u_dict.get("step_type") or "STEP").strip().upper()

                            if step_type == "DECISION":
                                raw_branches = u_dict.get("branches") or []
                                branches = []
                                for b in raw_branches:
                                    if not isinstance(b, dict) or not b.get("label"):
                                        continue
                                    branches.append(DecisionBranch(
                                        label=b["label"],
                                        output_variables=[v for v in (b.get("output_variables") or []) if isinstance(v, str)],
                                        next_step_id=b.get("next_step_id") or None,
                                    ))

                                unit = AtomicDecisionUnit(
                                    unit_id=local_id,
                                    chunk_id=cid,
                                    action=action,
                                    actor=actor,
                                    prev_step_ids=[s for s in (u_dict.get("prev_step_ids") or []) if isinstance(s, str)],
                                    branches=branches,
                                )
                            else:
                                unit = AtomicStepUnit(
                                    unit_id=local_id,
                                    chunk_id=cid,
                                    action=action,
                                    actor=actor,
                                    prev_step_ids=[s for s in (u_dict.get("prev_step_ids") or []) if isinstance(s, str)],
                                    next_step_ids=[s for s in (u_dict.get("next_step_ids") or []) if isinstance(s, str)],
                                    is_terminal=bool(u_dict.get("is_terminal", False)),
                                )

                            chunk.atomic_units.append(unit)
                            batch_units.append(unit)

                    # ── Remap local LLM ids → globally unique UUIDs ──────────
                    id_map: dict[str, str] = {
                        u.unit_id: str(uuid.uuid4())[:8]
                        for u in batch_units
                    }
                    for u in batch_units:
                        u.unit_id = id_map[u.unit_id]
                        u.prev_step_ids = [id_map.get(s, s) for s in u.prev_step_ids]
                        if isinstance(u, AtomicStepUnit):
                            u.next_step_ids = [id_map.get(s, s) for s in u.next_step_ids]
                        elif isinstance(u, AtomicDecisionUnit):
                            for b in u.branches:
                                if b.next_step_id:
                                    b.next_step_id = id_map.get(b.next_step_id, b.next_step_id)

                    all_units.extend(batch_units)
                else:
                    # LLM failed — one fallback STEP unit per chunk
                    for chunk in batch:
                        unit = AtomicStepUnit(
                            unit_id=str(uuid.uuid4())[:8],
                            chunk_id=chunk.chunk_id,
                            action=chunk.contextualized[:100],
                            actor="Unknown",
                        )
                        chunk.atomic_units.append(unit)
                        all_units.append(unit)

        process.atomic_units = all_units

        # ── Build actor → heading_sections map for L3 canonicalization ────────
        # Maps each raw actor name (as LLM wrote it) to the set of section headings
        # under which it appeared. L3 uses this for context-aware alias resolution.
        actor_heading_map: dict[str, list[str]] = defaultdict(list)
        chunk_map = {c.chunk_id: c for c in process.chunks}
        for unit in all_units:
            actor = unit.actor
            if not actor or actor == "Unknown":
                continue
            chunk = chunk_map.get(unit.chunk_id)
            headings = chunk.headings if chunk else []
            # Store the most-specific heading (last in breadcrumb)
            heading = headings[-1] if headings else ""
            if heading and heading not in actor_heading_map[actor]:
                actor_heading_map[actor].append(heading)

        process.actor_heading_map = dict(actor_heading_map)


def validate_gate(job: Job) -> None:
    for process in job.processes:
        if not process.atomic_units:
            raise LayerError("L4_NO_UNITS", f"No atomic units produced for {process.name}.")


class LayerError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
