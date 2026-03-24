import json
import uuid

import config
from llm.client import LLMClient
from llm.prompts import ATOMIZE_WITH_CONTEXT_SYSTEM, ATOMIZE_WITH_CONTEXT_USER
from models.schemas import AtomicUnit, BlockType, Job
from pipeline.utils.chunker import trim_previous_context

TARGET_TYPES = {BlockType.STEP}

TERMINAL_PHRASES = [
    "process complete", "close the case", "file is archived",
    "no further action", "end of process", "process ends", "case closed",
]


def run(job: Job) -> None:
    llm = LLMClient(job)
    valid_actors = [a.canonical_name for a in job.context_index.actor_registry.actors]
    actors_json = json.dumps(valid_actors, separators=(',', ':'))

    for process in job.processes:
        target_chunks = [c for c in process.chunks if c.chunk_type in TARGET_TYPES]
        if not target_chunks:
            continue

        all_units: list[AtomicUnit] = []
        first_chunk_id = target_chunks[0].chunk_id

        # Preamble context for this process (scope, pre-conditions, etc.)
        preamble_context = ""
        if getattr(process, 'preamble', None):
            preamble_texts = [c.contextualized.strip() for c in process.preamble if c.contextualized.strip()]
            if preamble_texts:
                preamble_context = "\n".join(preamble_texts[:10])

        # Group by top-level heading for coherent LLM calls
        groups: dict[str, list] = {}
        for chunk in target_chunks:
            key = chunk.headings[0] if chunk.headings else "root"
            groups.setdefault(key, []).append(chunk)

        for section_context, group_chunks in groups.items():
            known_vars: dict[str, str] = {}
            llm_previous: list[dict] = []

            batch_size = config.L6_BATCH_SIZE

            for batch_start in range(0, len(group_chunks), batch_size):
                batch = group_chunks[batch_start: batch_start + batch_size]

                # Build atomize targets — each chunk is self-contained via contextualized
                target_blocks_list = []
                for chunk in batch:
                    entry = f"CHUNK ID: {chunk.chunk_id}\nActor: {chunk.resolved_actor or 'Unknown'}"
                    if chunk.condition_scope:
                        entry += f"\nCondition: {chunk.condition_scope}"
                    # chunk.contextualized already has heading breadcrumb + full text
                    entry += f"\nText:\n{chunk.contextualized}"
                    target_blocks_list.append(entry)

                # Adjacent chunks in the same section provide read-only context
                # (only needed as a brief hint — the chunk itself has full context)
                window = config.L6_WINDOW_BLOCKS
                before_texts = [
                    c.contextualized[:150]
                    for c in group_chunks[max(0, batch_start - window): batch_start]
                ]
                after_texts = [
                    c.contextualized[:150]
                    for c in group_chunks[batch_start + len(batch): batch_start + len(batch) + window]
                ]
                context_blocks_text = "\n\n".join(before_texts + after_texts) or "None"
                target_blocks_text = "\n\n".join(["---\n" + t + "\n---" for t in target_blocks_list])

                result = llm.call(
                    layer=6,
                    template_name="ATOMIZE_WITH_CONTEXT",
                    system_prompt=ATOMIZE_WITH_CONTEXT_SYSTEM,
                    user_prompt=ATOMIZE_WITH_CONTEXT_USER.format(
                        actors_json=actors_json,
                        section_context=section_context,
                        known_vars_json=json.dumps(known_vars, separators=(',', ':')),
                        context_blocks_text=context_blocks_text,
                        target_blocks_text=target_blocks_text,
                        preamble_context=preamble_context or "None",
                    ),
                )

                batch_id_map = {c.chunk_id: c for c in batch}

                if result and isinstance(result, list):
                    llm_previous = trim_previous_context(llm_previous + result, keep=2)

                    for item in result:
                        # LLM returns block_id key (prompt template unchanged) — map to chunk_id
                        cid = item.get("block_id")
                        chunk = batch_id_map.get(cid)
                        if not chunk:
                            continue

                        for u_dict in item.get("atomic_units", []):
                            action = (u_dict.get("action") or "").strip()
                            actor = (u_dict.get("actor") or chunk.resolved_actor or "Unknown").strip()

                            if not action:
                                chunk.needs_review = True
                                chunk.review_reasons.append("Empty action in atomic unit")
                                continue

                            unit_inputs = u_dict.get("inputs") or []
                            unit_outputs = u_dict.get("outputs") or []

                            for vname in unit_outputs:
                                if isinstance(vname, str) and vname.startswith("V_"):
                                    known_vars[vname] = "unknown"

                            raw_step_type = (u_dict.get("step_type") or "SIMPLE").strip().upper()
                            step_type = raw_step_type if raw_step_type in ("SIMPLE", "CONDITIONAL", "DECISION") else "SIMPLE"

                            unit = AtomicUnit(
                                unit_id=str(uuid.uuid4())[:8],
                                chunk_id=cid,
                                sequence_in_chunk=u_dict.get("sequence_in_block", 0),
                                action=action,
                                actor=actor,
                                step_type=step_type,
                                condition=u_dict.get("condition") or chunk.condition_scope,
                                output=u_dict.get("output"),
                                is_terminal=u_dict.get("is_terminal", False) or _is_terminal(chunk.contextualized),
                                is_start=(cid == first_chunk_id and u_dict.get("sequence_in_block", 0) == 0),
                                inputs=[v for v in unit_inputs if isinstance(v, str)],
                                outputs=[v for v in unit_outputs if isinstance(v, str)],
                            )
                            chunk.atomic_units.append(unit)
                            all_units.append(unit)
                else:
                    # LLM failed — one fallback unit per chunk
                    for chunk in batch:
                        unit = AtomicUnit(
                            unit_id=str(uuid.uuid4())[:8],
                            chunk_id=chunk.chunk_id,
                            sequence_in_chunk=0,
                            action=chunk.contextualized[:100],
                            actor=chunk.resolved_actor or "Unknown",
                            is_terminal=_is_terminal(chunk.contextualized),
                            is_start=(chunk.chunk_id == first_chunk_id),
                        )
                        chunk.atomic_units.append(unit)
                        all_units.append(unit)

        # Ensure at least one start and one terminal per process
        if all_units and not any(u.is_start for u in all_units):
            all_units[0].is_start = True
        if all_units and not any(u.is_terminal for u in all_units):
            all_units[-1].is_terminal = True

        process.atomic_units = all_units


def validate_gate(job: Job) -> None:
    for process in job.processes:
        target = [c for c in process.chunks if c.chunk_type in TARGET_TYPES]
        empty = [c for c in target if len(c.atomic_units) == 0]
        if empty:
            raise LayerError("L6_EMPTY_ATOMIZATION", f"{len(empty)} chunks in {process.name} have no atomic units.")
        if process.atomic_units and not any(u.is_start for u in process.atomic_units):
            raise LayerError("L6_NO_START_UNIT", f"No start unit found in {process.name}.")
        if process.atomic_units and not any(u.is_terminal for u in process.atomic_units):
            raise LayerError("L6_NO_END_UNIT", f"No terminal unit found in {process.name}.")


def _is_terminal(text: str) -> bool:
    text_lower = text.lower()
    return any(phrase in text_lower for phrase in TERMINAL_PHRASES)


class LayerError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
