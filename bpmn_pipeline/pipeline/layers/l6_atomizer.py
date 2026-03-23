"""L6 — Block Atomizer: decompose blocks into AtomicUnit objects with variable extraction."""
import json
import uuid

import config
from llm.client import LLMClient
from llm.prompts import ATOMIZE_WITH_CONTEXT_SYSTEM, ATOMIZE_WITH_CONTEXT_USER
from models.schemas import AtomicUnit, BlockType, Job
from pipeline.utils.chunker import build_sliding_window, trim_previous_context


TARGET_TYPES = {BlockType.STEP, BlockType.DECISION, BlockType.EXCEPTION}

TERMINAL_PHRASES = [
    "process complete", "close the case", "file is archived",
    "no further action", "end of process", "process ends", "case closed",
]


def run(job: Job) -> None:
    llm = LLMClient(job)
    valid_actors = [a.canonical_name for a in job.context_index.actor_registry.actors]
    actors_json = json.dumps(valid_actors, separators=(',', ':'))

    for process in job.processes:
        target_blocks = [b for b in process.blocks if b.block_type in TARGET_TYPES and b.enrichment_version == 1]
        if not target_blocks:
            continue

        all_units: list[AtomicUnit] = []
        first_block_id = target_blocks[0].block_id

        # Group by section — context window will come from within the section
        groups: dict[str, list] = {}
        for b in target_blocks:
            key = " > ".join(b.heading_path) if b.heading_path else "root"
            groups.setdefault(key, []).append(b)

        for section_context, group_blocks in groups.items():
            n = len(group_blocks)

            # Variable registry for this section — carries names produced so far
            # so the LLM reuses them consistently across chunks
            known_vars: dict[str, str] = {}  # name → type

            llm_previous: list[dict] = []

            # Process in batches of L6_BATCH_SIZE, each with surrounding context window
            batch_size = config.L6_BATCH_SIZE
            window = config.L6_WINDOW_BLOCKS

            for batch_start in range(0, n, batch_size):
                batch_end = min(batch_start + batch_size, n)
                atomize_indices = list(range(batch_start, batch_end))

                # Build payload: context items before + atomize batch + context items after
                # Context items are from outside the batch but within the section
                context_before_idx = max(0, batch_start - window)
                context_after_idx = min(n, batch_end + window)

                context_texts = []

                # Before-context (read-only)
                for i in range(context_before_idx, batch_start):
                    context_texts.append(group_blocks[i].raw_text)

                # Atomize targets
                target_blocks_list = []
                for i in atomize_indices:
                    b = group_blocks[i]
                    target_str = f"BLOCK ID: {b.block_id}\nActor: {b.resolved_actor or 'Unknown'}"
                    if b.condition_scope:
                        target_str += f"\nCondition: {b.condition_scope}"
                    target_str += f"\nText:\n{b.raw_text}"
                    target_blocks_list.append(target_str)

                # After-context (read-only)
                for i in range(batch_end, context_after_idx):
                    context_texts.append(group_blocks[i].raw_text)

                context_blocks_text = "\n\n".join(context_texts) if context_texts else "None"
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
                    ),
                )

                # Build a fast lookup for this batch
                batch_id_map = {b.block_id: b for b in [group_blocks[i] for i in atomize_indices]}

                if result and isinstance(result, list):
                    llm_previous = trim_previous_context(llm_previous + result, keep=2)

                    for item in result:
                        bid = item.get("block_id")
                        block = batch_id_map.get(bid)
                        if not block:
                            continue

                        for u_dict in item.get("atomic_units", []):
                            action = (u_dict.get("action") or "").strip()
                            actor = (u_dict.get("actor") or block.resolved_actor or "Unknown").strip()

                            if not action:
                                block.needs_review = True
                                block.review_reasons.append("Empty action in atomic unit")
                                continue

                            unit_inputs = u_dict.get("inputs") or []
                            unit_outputs = u_dict.get("outputs") or []

                            # Update rolling variable registry (for next batch's prompt)
                            for vname in unit_outputs:
                                if isinstance(vname, str) and vname.startswith("V_"):
                                    known_vars[vname] = "unknown"

                            unit = AtomicUnit(
                                unit_id=str(uuid.uuid4())[:8],
                                block_id=bid,
                                sequence_in_block=u_dict.get("sequence_in_block", 0),
                                action=action,
                                actor=actor,
                                condition=u_dict.get("condition") or block.condition_scope,
                                output=u_dict.get("output"),
                                is_terminal=u_dict.get("is_terminal", False) or _is_terminal(block.raw_text),
                                is_start=(bid == first_block_id and u_dict.get("sequence_in_block", 0) == 0),
                                inputs=[v for v in unit_inputs if isinstance(v, str)],
                                outputs=[v for v in unit_outputs if isinstance(v, str)],
                            )
                            block.atomic_units.append(unit)
                            all_units.append(unit)
                else:
                    # LLM failed — fallback: one unit per block, no variables
                    for i in atomize_indices:
                        block = group_blocks[i]
                        unit = AtomicUnit(
                            unit_id=str(uuid.uuid4())[:8],
                            block_id=block.block_id,
                            sequence_in_block=0,
                            action=block.raw_text[:100],
                            actor=block.resolved_actor or "Unknown",
                            is_terminal=_is_terminal(block.raw_text),
                            is_start=(block.block_id == first_block_id),
                        )
                        block.atomic_units.append(unit)
                        all_units.append(unit)

        # Ensure at least one start and one terminal (per ProcessModel, after all sections)
        if all_units and not any(u.is_start for u in all_units):
            all_units[0].is_start = True
        if all_units and not any(u.is_terminal for u in all_units):
            all_units[-1].is_terminal = True

        process.atomic_units = all_units


def validate_gate(job: Job) -> None:
    for process in job.processes:
        target = [b for b in process.blocks if b.block_type in TARGET_TYPES]
        empty = [b for b in target if len(b.atomic_units) == 0]
        if empty:
            raise LayerError("L6_EMPTY_ATOMIZATION", f"{len(empty)} blocks in {process.name} have no atomic units.")
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
