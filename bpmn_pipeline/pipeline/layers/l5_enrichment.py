import json
import re

from llm.client import LLMClient
from llm.prompts import ENRICH_CHUNK_SYSTEM, ENRICH_CHUNK_USER
from models.schemas import BlockType, CrossRef, Job, StructuredChunk
from pipeline.utils.chunker import chunk_items, trim_previous_context

TARGET_TYPES = {BlockType.STEP, BlockType.DECISION, BlockType.EXCEPTION}
_PRONOUNS = {"they", "them", "their", "it", "he", "she"}


def _has_ambiguous_pronoun(chunk: StructuredChunk) -> bool:
    words = re.findall(r'\b\w+\b', chunk.contextualized.lower())
    return any(p in words for p in _PRONOUNS)


def run(job: Job) -> None:
    llm = LLMClient(job)
    target_chunks = [c for c in job.chunks if c.chunk_type in TARGET_TYPES]
    if not target_chunks:
        return

    chunk_map = {c.chunk_id: c for c in job.chunks}

    # ── Pass 1: Structural Traversal ─────────────────────────────────────────
    actor_stack: list[str] = []
    actor_depths: list[int] = []
    current_condition: str | None = None
    condition_depth: int | None = None
    current_inline_actor: str | None = None

    llm_queue: list[StructuredChunk] = []
    actor_snapshot: dict[str, list[str]] = {}  # chunk_id → actor candidates at this point

    for chunk in job.chunks:
        depth = len(chunk.headings)

        # Pop actors that went out of scope (deeper heading exited)
        while actor_depths and actor_depths[-1] >= depth:
            actor_depths.pop()
            actor_stack.pop()

        # Condition scope resets when we move up the heading hierarchy
        if current_condition and isinstance(condition_depth, int) and depth <= condition_depth:
            current_condition = None
            condition_depth = None

        if chunk.chunk_type == BlockType.HEADER:
            current_inline_actor = None
            current_condition = None
            heading_text = chunk.headings[-1] if chunk.headings else chunk.contextualized[:40]
            matched = job.context_index.actor_registry.find_canonical(heading_text)
            if matched:
                actor_stack.append(matched)
                actor_depths.append(depth)

        elif chunk.chunk_type == BlockType.ACTOR:
            matched = job.context_index.actor_registry.find_canonical(chunk.contextualized[:100])
            if matched:
                current_inline_actor = matched

        elif chunk.chunk_type == BlockType.CONDITION:
            current_condition = chunk.contextualized
            condition_depth = depth

        if chunk.chunk_type in TARGET_TYPES:
            # Structural actor resolution
            if current_inline_actor:
                chunk.resolved_actor = current_inline_actor
            elif actor_stack:
                chunk.resolved_actor = actor_stack[-1]

            if current_condition:
                chunk.condition_scope = current_condition

            # Cross-reference extraction from contextualized text
            chunk.cross_refs = []
            ref_pattern = r'(Section|Step|Clause|Appendix|Refer to|see)\s+([\d.]+|[A-Z]+-[A-Z]+-\d+)'
            for match in re.finditer(ref_pattern, chunk.contextualized, re.IGNORECASE):
                ref_text = match.group(0)
                anchor = next(
                    (a for a in job.context_index.section_anchors
                     if a.anchor_text.lower() == ref_text.lower()),
                    None,
                )
                method = "structural_anchor" if anchor else "unresolved"
                chunk.cross_refs.append(CrossRef(
                    ref_text=ref_text,
                    resolved_chunk_id=anchor.chunk_id if anchor else None,
                    resolution_method=method,
                ))

            # Decide if LLM pass is needed
            tasks: list[str] = []
            if not chunk.resolved_actor:
                tasks.append("resolve_actor")

            ambiguous_actor = (len(actor_stack) >= 2) or (current_inline_actor and len(actor_stack) >= 1)
            if _has_ambiguous_pronoun(chunk) and ambiguous_actor:
                tasks.append("resolve_pronoun")

            if any(r.resolution_method == "unresolved" for r in chunk.cross_refs):
                tasks.append("resolve_cross_ref")

            if tasks:
                snapshot = list(actor_stack)
                if current_inline_actor:
                    snapshot.append(str(current_inline_actor))
                actor_snapshot[chunk.chunk_id] = snapshot
                chunk._enrichment_tasks = tasks
                llm_queue.append(chunk)

    # ── Pass 2: LLM enrichment ────────────────────────────────────────────────
    if llm_queue:
        actor_reg_dict = {
            a.canonical_name: a.aliases[:2]
            for a in job.context_index.actor_registry.actors
        }
        anchors_dict = {a.anchor_text: a.chunk_id for a in job.context_index.section_anchors}

        # Group by top-level heading for coherent batches
        groups: dict[str, list[StructuredChunk]] = {}
        for chunk in llm_queue:
            key = chunk.headings[0] if chunk.headings else "root"
            groups.setdefault(key, []).append(chunk)

        for heading_key, group_chunks in groups.items():

            def _serialize(c: StructuredChunk) -> str:
                tasks = getattr(c, "_enrichment_tasks", [])
                d: dict = {
                    "id": c.chunk_id,
                    # chunk.contextualized is the natural context — no reconstruction needed
                    "text": c.contextualized[:300],
                    "tasks": tasks,
                }
                ctxs = actor_snapshot.get(c.chunk_id, [])
                if ctxs and ("resolve_actor" in tasks or "resolve_pronoun" in tasks):
                    d["ctx"] = list(ctxs)[:3]
                if "resolve_cross_ref" in tasks:
                    d["unresolved"] = [r.ref_text for r in c.cross_refs if r.resolution_method == "unresolved"]
                return json.dumps(d, separators=(',', ':'))

            batches = chunk_items(group_chunks, serialize_fn=_serialize, max_tokens=1400, overlap=1)
            llm_results: list[dict] = []

            for batch in batches:
                has_cross_ref = any("resolve_cross_ref" in getattr(c, "_enrichment_tasks", []) for c in batch)
                anchors_str = json.dumps(anchors_dict, separators=(',', ':')) if has_cross_ref else "{}"

                chunk_actors: set = set()
                for c in batch:
                    chunk_actors.update(actor_snapshot.get(c.chunk_id, []))
                chunk_actor_reg = {k: v for k, v in actor_reg_dict.items() if k in chunk_actors}
                chunk_actor_str = json.dumps(chunk_actor_reg or actor_reg_dict, separators=(',', ':'))

                chunk_dicts = [json.loads(_serialize(c)) for c in batch]

                result = llm.call(
                    layer=5,
                    template_name="ENRICH_CHUNK",
                    system_prompt=ENRICH_CHUNK_SYSTEM,
                    user_prompt=ENRICH_CHUNK_USER.format(
                        actor_registry_json=chunk_actor_str,
                        section_anchors_json=anchors_str,
                        previous_context=json.dumps(trim_previous_context(llm_results, keep=2), separators=(',', ':')),
                        blocks_json=json.dumps(chunk_dicts, indent=2),
                    ),
                )

                if result and isinstance(result, list):
                    llm_results.extend(result)
                    for item in result:
                        cid = item.get("id")
                        if not cid or cid not in chunk_map:
                            continue
                        c = chunk_map[cid]

                        resolved = item.get("actor") or item.get("pronoun_actor")
                        if resolved and not c.resolved_actor:
                            c.resolved_actor = resolved

                        for cr in c.cross_refs:
                            if cr.resolution_method == "unresolved":
                                res_map = {r.get("ref"): r.get("id") for r in (item.get("refs") or []) if r.get("id")}
                                if cr.ref_text in res_map:
                                    cr.resolved_chunk_id = res_map[cr.ref_text]
                                    cr.resolution_method = "llm"

    # ── Pass 3: Flag unresolved ────────────────────────────────────────────────
    for chunk in target_chunks:
        if not chunk.resolved_actor:
            chunk.needs_review = True
            chunk.review_reasons.append("Actor could not be resolved by structural traversal or LLM")


def validate_gate(job: Job) -> None:
    target = [c for c in job.chunks if c.chunk_type in TARGET_TYPES]

    unresolved = [c for c in target if not c.resolved_actor]
    if target and (len(unresolved) / len(target)) >= 0.15:
        raise SoftGateFailure("L5_HIGH_UNRESOLVED_ACTOR_RATE", "Unresolved actor rate >= 15%")

    unresolved_refs = [cr for c in target for cr in c.cross_refs if cr.resolution_method == "unresolved"]
    if unresolved_refs:
        raise SoftGateFailure("L5_UNRESOLVED_CROSS_REFS", f"{len(unresolved_refs)} cross-references unresolved.")


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
