"""L3 — Block Classifier: heuristic-first, then LLM for genuinely ambiguous blocks."""
import json
import re

from llm.client import LLMClient
from llm.prompts import CLASSIFY_BLOCKS_BATCH_SYSTEM, CLASSIFY_BLOCKS_BATCH_USER
from models.schemas import Block, BlockType, Job
from pipeline.utils.chunker import chunk_items
from pipeline.utils.tree_builder import build_document_tree
import config

from pipeline.utils.decision_patterns import DECISION_INLINE


# ── Heading keywords that strongly imply a block type ────────────────────────
_ACTOR_HEADINGS = re.compile(
    r'\b(roles?|actors?|responsible|performed by|owned by|stakeholders?)\b', re.I
)
_CONDITION_HEADINGS = re.compile(
    r'\b(scope|purpose|applicability|prerequisites?|preconditions?|assumptions?)\b', re.I
)
_EXCEPTION_HEADINGS = re.compile(
    r'\b(exception|escalation|error|failure|fallback|alternative)\b', re.I
)

# Inline text patterns that strongly hint at NOTE
_NOTE_PATTERNS = re.compile(r'^(note[:\s]|tip[:\s]|warning[:\s]|important[:\s])', re.I)


def _heuristic_classify(block: Block) -> tuple[BlockType, float] | None:
    """
    Return (BlockType, confidence) if a rule matches, else None (needs LLM).
    Handles approximately 70% of blocks without LLM.
    """
    text = block.raw_text.strip()
    heading_path_str = " > ".join(block.heading_path).lower()

    if not text:
        return BlockType.NOTE, 1.0

    # Heading-path signals — very high confidence
    if _ACTOR_HEADINGS.search(heading_path_str):
        return BlockType.ACTOR, 0.9
    if _CONDITION_HEADINGS.search(heading_path_str):
        return BlockType.CONDITION, 0.9
    if _EXCEPTION_HEADINGS.search(heading_path_str):
        return BlockType.EXCEPTION, 0.88

    # Inline note prefix
    if _NOTE_PATTERNS.match(text):
        return BlockType.NOTE, 0.95

    # Inline decision pattern — mark as DECISION but lower confidence (LLM confirms)
    if DECISION_INLINE.search(text):
        return BlockType.DECISION, 0.75  # still LLM-worthy; returned as hint only

    return None  # ambiguous → needs LLM


def run(job: Job) -> None:
    job.document_tree = build_document_tree(job.blocks)
    llm = LLMClient(job)

    ambiguous_blocks: list[Block] = []

    # Pass 1: Classify structurally obvious blocks
    all_content_blocks = [
        b for b in job.blocks
        if b.block_type != BlockType.HEADER and b.raw_text.strip()
    ]

    for b in all_content_blocks:
        result = _heuristic_classify(b)
        if result is not None:
            btype, conf = result
            # DECISION heuristic still gets LLM confirmation (lower threshold)
            if btype == BlockType.DECISION and conf < 0.85:
                b._heuristic_hint = btype  # store hint for LLM prompt
                ambiguous_blocks.append(b)
            else:
                b.block_type = btype
                b.block_type_confidence = conf
                b.block_type_method = "structural"
        else:
            ambiguous_blocks.append(b)

    if not ambiguous_blocks:
        return

    # Pass 2: LLM for ambiguous blocks, batched in section-scoped windows
    # Group by section so cross-block context is coherent
    section_groups: dict[str, list[Block]] = {}

    for b in all_content_blocks:
        key = " > ".join(b.heading_path) if b.heading_path else "root"
        section_groups.setdefault(key, [])
        section_groups[key].append(b)

    # Pass 2: one LLM call per token batch — true batch classification
    for heading_key, group in section_groups.items():
        ambiguous_in_group = [b for b in group if b in ambiguous_blocks or id(b) in {id(x) for x in ambiguous_blocks}]
        if not ambiguous_in_group:
            continue

        def _serialize(b: Block) -> str:
            obj = {"block_id": b.block_id, "text": b.raw_text[:500]}
            hint = getattr(b, "_heuristic_hint", None)
            if hint is not None:
                obj["heuristic_hint"] = f"regex suggests {getattr(hint, 'value', hint)}"
            return json.dumps(obj, separators=(",", ":"))

        batches = chunk_items(
            ambiguous_in_group,
            serialize_fn=_serialize,
            max_tokens=config.LLM_MAX_INPUT_TOKENS - 450,
            overlap=config.LLM_OVERLAP_ITEMS,
        )

        for batch in batches:
            payload = []
            for b in batch:
                item = {"block_id": b.block_id, "text": b.raw_text[:500]}
                hint = getattr(b, "_heuristic_hint", None)
                if hint is not None:
                    item["heuristic_hint"] = f"regex suggests {getattr(hint, 'value', hint)}"
                payload.append(item)

            result = llm.call(
                layer=3,
                template_name="CLASSIFY_BLOCKS_BATCH",
                system_prompt=CLASSIFY_BLOCKS_BATCH_SYSTEM,
                user_prompt=CLASSIFY_BLOCKS_BATCH_USER.format(
                    sop_class=job.sop_class,
                    section_heading=heading_key,
                    blocks_json=json.dumps(payload, indent=2),
                ),
            )

            by_id: dict[str, dict] = {}
            rows = result
            if isinstance(result, dict) and "block_id" in result:
                rows = [result]
            if rows and isinstance(rows, list):
                for item in rows:
                    bid = item.get("block_id")
                    if bid:
                        by_id[bid] = item

            for b in batch:
                item = by_id.get(b.block_id)
                if item:
                    try:
                        btype = BlockType(item.get("block_type", "UNKNOWN"))
                    except ValueError:
                        btype = BlockType.UNKNOWN
                    b.block_type = btype
                    b.block_type_confidence = float(item.get("confidence", 0.0))
                    b.block_type_method = "llm"
                else:
                    b.block_type = BlockType.UNKNOWN
                    b.block_type_confidence = 0.0
                    b.block_type_method = "llm_failed"

    # Ensure every block has a type
    for b in job.blocks:
        if b.block_type is None:
            b.block_type = BlockType.UNKNOWN
            b.block_type_confidence = 0.0
            b.block_type_method = "fallback"


def validate_gate(job: Job) -> None:
    steps = [b for b in job.blocks if b.block_type == BlockType.STEP]
    if not steps:
        raise LayerError("L3_NO_STEPS_FOUND", "No STEP blocks found.")

    llm_classified = [b for b in job.blocks if b.block_type_method == "llm"]
    unknown = [b for b in llm_classified if b.block_type == BlockType.UNKNOWN]
    if llm_classified and (len(unknown) / len(llm_classified)) >= 0.2:
        for b in unknown:
            b.needs_review = True
            b.review_reasons.append("Block type could not be classified")
        raise SoftGateFailure("L3_HIGH_UNKNOWN_RATE", "UNKNOWN rate >= 20%")


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
