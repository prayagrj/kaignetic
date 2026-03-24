"""L3b — Process Splitter: LLM-guided semantic process identification.
"""
import json
import uuid

from llm.client import LLMClient
from llm.prompts import SPLIT_PROCESSES_SYSTEM, SPLIT_PROCESSES_USER
from models.schemas import BlockType, Job, ProcessModel, StructuredChunk

_ACTION_TYPES = {BlockType.STEP, BlockType.DECISION, BlockType.EXCEPTION}
_CONTEXT_TYPES = {BlockType.CONDITION, BlockType.NOTE, BlockType.ACTOR}


# ── helpers ───────────────────────────────────────────────────────────────────

def _heading_key(chunk: StructuredChunk) -> str:
    """Bucket key from top two heading levels."""
    if not chunk.headings:
        return "__root__"
    if len(chunk.headings) >= 2:
        return f"{chunk.headings[0]} > {chunk.headings[1]}"
    return chunk.headings[0]


def _new_id() -> str:
    return str(uuid.uuid4())[:8]


def _build_section_map(chunks: list) -> dict:
    """Group chunks by heading key, preserving document order."""
    section_map: dict[str, list[StructuredChunk]] = {}
    for chunk in chunks:
        key = _heading_key(chunk)
        section_map.setdefault(key, []).append(chunk)
    return section_map


def _classify_section_role(chunks: list) -> str:
    has_executable = any(c.chunk_type in _ACTION_TYPES for c in chunks)
    has_context = any(c.chunk_type in _CONTEXT_TYPES for c in chunks)
    if has_executable:
        return "process"
    if has_context:
        return "preamble"
    return "discard"


def _build_outline(section_map: dict) -> list:
    outline = []
    for key, chunks in section_map.items():
        counts: dict[str, int] = {}
        for c in chunks:
            if c.chunk_type:
                t = c.chunk_type.value if hasattr(c.chunk_type, "value") else str(c.chunk_type)
                counts[t] = counts.get(t, 0) + 1
        outline.append({"heading_key": key, "block_counts": counts})
    return outline


def _doc_title(chunks: list) -> str:
    for c in chunks:
        if c.chunk_type == BlockType.HEADER and c.contextualized:
            return str(c.contextualized)[:80]
    return "Unknown SOP"


def _phase2_llm_grouping(candidates: dict, job: Job):
    if not candidates:
        return None

    outline = _build_outline(candidates)
    title = _doc_title(job.chunks)

    llm = LLMClient(job)
    result = llm.call(
        layer="3b",
        template_name="SPLIT_PROCESSES",
        system_prompt=SPLIT_PROCESSES_SYSTEM,
        user_prompt=SPLIT_PROCESSES_USER.format(
            sop_class=job.sop_class,
            doc_title=title,
            outline_json=json.dumps(outline, indent=2),
        ),
    )

    if not result or not isinstance(result, list):
        return None

    known_keys = set(candidates.keys())
    validated = []
    for item in result:
        if not isinstance(item, dict):
            continue
        name = item.get("process_name", "").strip()
        keys = [k for k in (item.get("heading_keys") or []) if k in known_keys]
        if name and keys:
            validated.append({"process_name": name, "heading_keys": keys})

    return validated if validated else None


def run(job: Job) -> None:
    if not job.chunks:
        return

    section_map = _build_section_map(job.chunks)

    candidates: dict[str, list[StructuredChunk]] = {}
    pending_preamble: list[StructuredChunk] = []

    for key, chunks in section_map.items():
        role = _classify_section_role(chunks)
        if role == "process":
            candidates[key] = chunks
        elif role == "preamble":
            pending_preamble.extend(chunks)

    if not candidates:
        job.processes.append(ProcessModel(
            process_id=_new_id(), name="Main Process", chunks=job.chunks,
        ))
        return

    groupings = _phase2_llm_grouping(candidates, job)
    created: list[ProcessModel] = []

    if groupings:
        assigned: set[str] = set()
        for group in groupings:
            merged: list[StructuredChunk] = []
            for key in group["heading_keys"]:
                merged.extend(candidates.get(key, []))
                assigned.add(key)
            if merged:
                created.append(ProcessModel(
                    process_id=_new_id(), name=group["process_name"], chunks=merged,
                ))
        for key, chunks in candidates.items():
            if key not in assigned:
                created.append(ProcessModel(
                    process_id=_new_id(), name=key, chunks=chunks,
                ))
    else:
        print("[L3b] LLM grouping unavailable — using heuristic fallback")
        for key, chunks in candidates.items():
            created.append(ProcessModel(process_id=_new_id(), name=key, chunks=chunks))

    for proc in created:
        proc.preamble.extend(pending_preamble)
        pending_preamble = []
        job.processes.append(proc)


def validate_gate(job: Job) -> None:
    if not job.processes:
        raise LayerError("L3B_NO_PROCESSES", "Failed to split chunks into processes.")


class LayerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
