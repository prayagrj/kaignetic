"""L3 — Element Classifier

Each ChunkElement gets its own block_type via LLM.
chunk_type on StructuredChunk is derived as majority vote so downstream layers still work.

Strategy:
  - If the whole chunk fits in the token budget → one LLM call.
  - If not → split elements into sub-chunks at element boundaries (never mid-element),
    each sub-chunk gets the section heading as context.
"""
import json
from collections import Counter

import config
from llm.client import LLMClient
from llm.prompts import CLASSIFY_ELEMENTS_SYSTEM, CLASSIFY_ELEMENTS_USER
from models.schemas import BlockType, Job, StructuredChunk

_CHARS_PER_TOKEN = 4
_PROMPT_OVERHEAD_TOKENS = 350  # system prompt + fixed prompt framing


def _tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


def _make_sub_chunks(heading: str, elements: list) -> list[list]:
    """Split elements into groups that fit within the token budget."""
    budget = config.LLM_MAX_INPUT_TOKENS - _PROMPT_OVERHEAD_TOKENS - _tokens(heading)
    budget = max(budget, 100)

    sub_chunks, current, used = [], [], 0
    for e in elements:
        cost = _tokens(e.text or "") + 15  # +15 for JSON key/id overhead per element
        if current and used + cost > budget:
            sub_chunks.append(current)
            current, used = [], 0
        current.append(e)
        used += cost

    if current:
        sub_chunks.append(current)

    return sub_chunks


def run(job: Job) -> None:
    llm = LLMClient(job)

    for chunk in job.chunks:
        text_elements = [e for e in chunk.elements if e.text and e.text.strip()]

        if not text_elements:
            chunk.chunk_type = BlockType.NOTE
            chunk.chunk_type_confidence = 1.0
            chunk.chunk_type_method = "fallback"
            continue

        heading = " > ".join(chunk.headings) if chunk.headings else "root"
        for sub in _make_sub_chunks(heading, text_elements):
            _classify_elements(llm, job, heading, sub)

        _derive_chunk_type(chunk)

    _ensure_all_typed(job.chunks)


def _classify_elements(llm: LLMClient, job: Job, heading: str, elements: list) -> None:
    payload = [{"element_id": e.element_id, "text": e.text or ""} for e in elements]

    result = llm.call(
        layer=3,
        template_name="CLASSIFY_ELEMENTS",
        system_prompt=CLASSIFY_ELEMENTS_SYSTEM,
        user_prompt=CLASSIFY_ELEMENTS_USER.format(
            sop_class=job.sop_class,
            section_heading=heading,
            elements_json=json.dumps(payload, indent=2),
        ),
    )

    rows = result if isinstance(result, list) else ([result] if isinstance(result, dict) else [])
    by_id = {row.get("element_id"): row for row in rows if row.get("element_id")}

    for e in elements:
        row = by_id.get(e.element_id)
        if row:
            try:
                e.block_type = BlockType(row.get("block_type", "UNKNOWN"))
            except ValueError:
                e.block_type = BlockType.UNKNOWN
            e.block_type_confidence = float(row.get("confidence", 0.0))
        else:
            e.block_type = BlockType.UNKNOWN
            e.block_type_confidence = 0.0


def _derive_chunk_type(chunk: StructuredChunk) -> None:
    """Majority vote across element block_types, ignoring NOTE/UNKNOWN."""
    typed = [
        e.block_type for e in chunk.elements
        if e.block_type and e.block_type not in (
            BlockType.NOTE, BlockType.META, BlockType.HEADER, BlockType.UNKNOWN
        )
    ]
    if typed:
        majority = Counter(typed).most_common(1)[0][0]
        chunk.chunk_type = majority
        chunk.chunk_type_confidence = typed.count(majority) / len(typed)
    else:
        all_typed = [e.block_type for e in chunk.elements if e.block_type]
        chunk.chunk_type = Counter(all_typed).most_common(1)[0][0] if all_typed else BlockType.UNKNOWN
        chunk.chunk_type_confidence = 0.0
    chunk.chunk_type_method = "llm"


def _ensure_all_typed(chunks: list) -> None:
    for chunk in chunks:
        if chunk.chunk_type is None:
            chunk.chunk_type = BlockType.UNKNOWN
            chunk.chunk_type_confidence = 0.0
            chunk.chunk_type_method = "fallback"
        for e in chunk.elements:
            if e.block_type is None:
                e.block_type = BlockType.UNKNOWN
                e.block_type_confidence = 0.0


def validate_gate(job: Job) -> None:
    all_elements = [e for chunk in job.chunks for e in chunk.elements if e.text and e.text.strip()]
    if not any(e.block_type == BlockType.STEP for e in all_elements):
        raise LayerError("L3_NO_STEPS_FOUND", "No STEP elements found.")

    unknown = [e for e in all_elements if e.block_type == BlockType.UNKNOWN]
    if all_elements and (len(unknown) / len(all_elements)) >= 0.2:
        for chunk in job.chunks:
            for e in chunk.elements:
                if e.block_type == BlockType.UNKNOWN:
                    chunk.needs_review = True
                    chunk.review_reasons.append(f"Element {e.element_id} could not be classified")
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
