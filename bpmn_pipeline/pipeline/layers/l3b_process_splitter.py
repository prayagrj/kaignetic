"""L3b — Process Splitter (v2): LLM-guided semantic process identification.

Two-phase approach:
  Phase 1 — Heuristic  : Build heading-keyed section buckets. Discard any bucket
                         that has zero STEP or DECISION blocks (preamble/containers).
                         This alone eliminates empty-shell processes like "1. Purpose"
                         or "5. Pre-Joining Phase" that are section containers.

  Phase 2 — LLM        : Present the filtered outline (heading key + block-type
                         distribution) to the LLM and ask it to semantically group
                         adjacent candidates into coherent processes and give each
                         a canonical name.

  Fallback             : If the LLM call fails or returns an invalid/empty result,
                         fall back to the Phase 1 heuristic output — one ProcessModel
                         per surviving candidate section. This is already far better
                         than the original behaviour.

Generic SOP design notes
------------------------
- "Actions" = STEP | DECISION | EXCEPTION blocks.  A section with *only*
  CONDITION / ACTOR / NOTE / HEADER blocks is non-executable.
- The LLM input is the heading outline (not raw text) → small, cheap call.
- Merging is opt-in: the LLM decides which candidates truly belong together.
"""
import json
import uuid

from llm.client import LLMClient
from llm.prompts import SPLIT_PROCESSES_SYSTEM, SPLIT_PROCESSES_USER
from models.schemas import Block, BlockType, Job, ProcessModel

# Block types that make a section "executable"
_ACTION_TYPES = {BlockType.STEP, BlockType.DECISION, BlockType.EXCEPTION}


# ── helpers ───────────────────────────────────────────────────────────────────

def _heading_key(block: Block) -> str:
    """Stable bucket key from a block's heading path (up to depth 2)."""
    hp = block.heading_path
    if not hp:
        return "__root__"
    if len(hp) >= 2:
        return f"{hp[0]} > {hp[1]}"
    return hp[0]


def _new_id() -> str:
    return str(uuid.uuid4())[:8]


def _build_section_map(blocks: list[Block]) -> dict[str, list[Block]]:
    """Group blocks by heading key, preserving document order."""
    section_map: dict[str, list[Block]] = {}
    for block in blocks:
        key = _heading_key(block)
        section_map.setdefault(key, []).append(block)
    return section_map


def _action_count(blocks: list[Block]) -> int:
    """Count STEP / DECISION / EXCEPTION blocks in a group."""
    return sum(1 for b in blocks if b.block_type in _ACTION_TYPES)


def _build_outline(section_map: dict[str, list[Block]]) -> list[dict]:
    """Compact outline for each section: heading key + per-type block counts."""
    outline = []
    for key, blocks in section_map.items():
        counts: dict[str, int] = {}
        for b in blocks:
            if b.block_type:
                t = b.block_type.value if hasattr(b.block_type, "value") else str(b.block_type)
                counts[t] = counts.get(t, 0) + 1
        outline.append({"heading_key": key, "block_counts": counts})
    return outline


def _doc_title(blocks: list[Block]) -> str:
    """Return the text of the first HEADER block as document title."""
    for b in blocks:
        if b.block_type == BlockType.HEADER and b.raw_text:
            return b.raw_text
    return "Unknown SOP"


# ── Section role classification ───────────────────────────────────────────────

# Block types that carry contextual (non-executable) information
_CONTEXT_TYPES = {BlockType.CONDITION, BlockType.NOTE, BlockType.ACTOR}


def _classify_section_role(blocks: list[Block]) -> str:
    """
    Determine whether a section should be treated as:
      - "process"  : has at least one STEP/DECISION/EXCEPTION → goes into a process
      - "preamble" : has only context blocks (CONDITION/NOTE/ACTOR) → attached to next process
      - "discard"  : empty or pure HEADER/UNKNOWN → dropped
    """
    has_executable = any(b.block_type in _ACTION_TYPES for b in blocks)
    has_context = any(b.block_type in _CONTEXT_TYPES for b in blocks)

    if has_executable:
        return "process"
    if has_context:
        return "preamble"
    return "discard"


# ── Phase 1: heuristic pre-screening ─────────────────────────────────────────

def _phase1_candidates(section_map: dict[str, list[Block]]) -> dict[str, list[Block]]:
    """Return only sections with at least one action-type block."""
    return {k: v for k, v in section_map.items() if _action_count(v) > 0}


# ── Phase 2: LLM semantic grouping ───────────────────────────────────────────

def _phase2_llm_grouping(
    candidates: dict[str, list[Block]],
    job: Job,
) -> list[dict] | None:
    """
    Ask the LLM to semantically group the candidate sections into processes.

    Returns a list of dicts:
        [{"process_name": str, "heading_keys": [str, ...]}, ...]

    Returns None on failure (caller should fall back to Phase 1).
    """
    if not candidates:
        return None

    outline = _build_outline(candidates)
    title = _doc_title(job.blocks)

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

    # Validate: each item must have process_name and heading_keys
    validated = []
    known_keys = set(candidates.keys())
    for item in result:
        if not isinstance(item, dict):
            continue
        name = item.get("process_name", "").strip()
        keys = [k for k in (item.get("heading_keys") or []) if k in known_keys]
        if name and keys:
            validated.append({"process_name": name, "heading_keys": keys})

    return validated if validated else None


# ── ProcessModel builder ──────────────────────────────────────────────────────

def _make_process(name: str, blocks: list[Block]) -> ProcessModel:
    return ProcessModel(
        process_id=_new_id(),
        name=name,
        blocks=blocks,
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def run(job: Job) -> None:
    if not job.blocks:
        return

    # Build full section map (preserves document order)
    section_map = _build_section_map(job.blocks)

    # Classify each section into roles (process / preamble / discard)
    # Collect preamble sections to carry forward until we find a process section
    candidates: dict[str, list[Block]] = {}   # process-role sections only
    pending_preamble: list[Block] = []         # preamble blocks accumulated so far

    for key, blocks in section_map.items():
        role = _classify_section_role(blocks)
        if role == "process":
            candidates[key] = blocks
        elif role == "preamble":
            pending_preamble.extend(blocks)
        # "discard" → ignored

    if not candidates:
        # Entire document is preamble — fall back to a single process with all blocks
        job.processes.append(_make_process("Main Process", job.blocks))
        return

    # Phase 2 — LLM semantic grouping
    groupings = _phase2_llm_grouping(candidates, job)

    # Helper: attach accumulated preamble to a process model
    def _attach_preamble(proc: ProcessModel, accumulated: list[Block]) -> None:
        proc.preamble.extend(accumulated)

    # Build processes and distribute preamble sections to them
    created_processes: list[ProcessModel] = []

    if groupings:
        # Build ProcessModels from LLM groupings
        assigned_keys: set[str] = set()
        for group in groupings:
            merged_blocks: list[Block] = []
            for key in group["heading_keys"]:
                merged_blocks.extend(candidates.get(key, []))
                assigned_keys.add(key)
            if merged_blocks:
                proc = _make_process(group["process_name"], merged_blocks)
                created_processes.append(proc)

        # Safety: any candidate key not assigned by LLM gets its own process
        for key, blocks in candidates.items():
            if key not in assigned_keys:
                proc = _make_process(key, blocks)
                created_processes.append(proc)

    else:
        # Fallback — one process per surviving candidate (Phase 1 result)
        print("[L3b] LLM grouping unavailable — using heuristic fallback")
        for key, blocks in candidates.items():
            proc = _make_process(key, blocks)
            created_processes.append(proc)

    # Attach accumulated preamble to the first process; any preamble after a
    # process boundary gets attached to the next one.
    for proc in created_processes:
        _attach_preamble(proc, pending_preamble)
        pending_preamble = []  # consumed — reset for next process
        job.processes.append(proc)


# ── Gate ──────────────────────────────────────────────────────────────────────

def validate_gate(job: Job) -> None:
    if not job.processes:
        raise LayerError("L3B_NO_PROCESSES", "Failed to split blocks into processes.")


class LayerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
