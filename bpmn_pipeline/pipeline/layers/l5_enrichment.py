"""L5 — Block Enrichment: structural traversal + targeted chunked LLM for ambiguous cases."""
import json
import re

from llm.client import LLMClient
from llm.prompts import ENRICH_CHUNK_SYSTEM, ENRICH_CHUNK_USER
from models.schemas import Block, BlockType, CrossRef, Job
from pipeline.utils.chunker import chunk_items, trim_previous_context

TARGET_TYPES = {BlockType.STEP, BlockType.DECISION, BlockType.EXCEPTION}
PRONOUNS = {"they", "them", "their", "it", "he", "she"}


def has_ambiguous_pronoun(block: Block) -> bool:
    words = re.findall(r'\b\w+\b', block.raw_text.lower())
    return any(p in words for p in PRONOUNS)


def run(job: Job) -> None:
    llm = LLMClient(job)
    target_blocks = [b for b in job.blocks if b.block_type in TARGET_TYPES]
    if not target_blocks:
        return

    id_map = {b.block_id: b for b in job.blocks}

    # ── Pass 1: Structural Traversal ─────────────────────────────────────────
    actor_stack: list[str] = []
    actor_depths: list[int] = []
    current_condition: str | None = None
    condition_owner_depth: int | None = None
    current_inline_actor: str | None = None

    llm_queue: list[Block] = []
    actor_snapshot: dict[str, list[str]] = {}  # block_id → actor candidates at that point

    for b in job.blocks:
        depth = len(b.heading_path)

        # Pop actors that went out of scope
        while actor_depths and actor_depths[-1] >= depth:
            actor_depths.pop()
            actor_stack.pop()

        if current_condition and isinstance(condition_owner_depth, int) and depth <= condition_owner_depth:
            current_condition = None
            condition_owner_depth = None

        if b.block_type == BlockType.HEADER:
            current_inline_actor = None
            current_condition = None
            heading_text = b.heading_path[-1] if b.heading_path else b.raw_text
            matched = job.context_index.actor_registry.find_canonical(heading_text)
            if matched:
                actor_stack.append(matched)
                actor_depths.append(depth)

        elif b.block_type == BlockType.ACTOR:
            matched = job.context_index.actor_registry.find_canonical(b.raw_text)
            if matched:
                current_inline_actor = matched

        elif b.block_type == BlockType.CONDITION:
            current_condition = b.raw_text
            condition_owner_depth = depth

        if b.block_type in TARGET_TYPES:
            # Resolve actor structurally
            if current_inline_actor:
                b.resolved_actor = current_inline_actor
            elif actor_stack:
                b.resolved_actor = actor_stack[-1]

            if current_condition:
                b.condition_scope = current_condition

            # Resolve cross-refs against section anchors
            b.cross_refs = []
            ref_pattern = r'(Section|Step|Clause|Appendix|Refer to|see)\s+([\d.]+|[A-Z]+-[A-Z]+-\d+)'
            for match in re.finditer(ref_pattern, b.raw_text, re.IGNORECASE):
                ref_text = match.group(0)
                anchor = next(
                    (a for a in job.context_index.section_anchors
                     if a.anchor_text.lower() == ref_text.lower()),
                    None,
                )
                method = "structural_anchor" if anchor else "unresolved"
                b.cross_refs.append(CrossRef(
                    ref_text=ref_text,
                    resolved_block_id=anchor.block_id if anchor else None,
                    resolution_method=method,
                ))

            # Decide if LLM pass is needed
            tasks: list[str] = []
            if not b.resolved_actor:
                tasks.append("resolve_actor")

            ambiguous_actor = (len(actor_stack) >= 2) or (current_inline_actor and len(actor_stack) >= 1)
            if has_ambiguous_pronoun(b) and ambiguous_actor:
                tasks.append("resolve_pronoun")

            if any(r.resolution_method == "unresolved" for r in b.cross_refs):
                tasks.append("resolve_cross_ref")

            if tasks:
                snapshot: list[str] = []
                if actor_stack:
                    snapshot.extend(actor_stack)
                if current_inline_actor:
                    snapshot.append(current_inline_actor)
                actor_snapshot[b.block_id] = snapshot
                b._enrichment_tasks = tasks
                llm_queue.append(b)

    # ── Pass 2: Chunked LLM enrichment ───────────────────────────────────────
    if llm_queue:
        # Compact actor registry — only canonical names and first alias
        actor_reg_dict = {
            a.canonical_name: a.aliases[:2] for a in job.context_index.actor_registry.actors
        }
        actor_reg_str = json.dumps(actor_reg_dict, separators=(',', ':'))

        anchors_dict = {a.anchor_text: a.block_id for a in job.context_index.section_anchors}

        # Group by section for coherent context
        groups: dict[str, list[Block]] = {}
        for b in llm_queue:
            key = " > ".join(b.heading_path) if b.heading_path else "root"
            groups.setdefault(key, []).append(b)

        for heading_key, group_blocks in groups.items():

            def _serialize(b: Block) -> str:
                tasks = getattr(b, "_enrichment_tasks", [])
                d: dict = {"id": b.block_id, "text": b.raw_text[:200], "tasks": tasks}
                ctxs = actor_snapshot.get(b.block_id, [])
                if ctxs and ("resolve_actor" in tasks or "resolve_pronoun" in tasks):
                    d["ctx"] = ctxs[:3]  # cap to 3 candidates
                if "resolve_pronoun" in tasks:
                    d["prev"] = _preceding_text(b, job.blocks)[:80]
                if "resolve_cross_ref" in tasks:
                    d["unresolved"] = [r.ref_text for r in b.cross_refs if r.resolution_method == "unresolved"]
                return json.dumps(d, separators=(',', ':'))

            chunks = chunk_items(
                group_blocks,
                serialize_fn=_serialize,
                max_tokens=1400,  # leave ~400 for system, registry, anchors
                overlap=1,
            )

            llm_results: list[dict] = []

            for chunk in chunks:
                has_cross_ref = any("resolve_cross_ref" in getattr(b, "_enrichment_tasks", []) for b in chunk)
                anchors_str = json.dumps(anchors_dict, separators=(',', ':')) if has_cross_ref else "{}"

                # Trim actor registry to actors relevant to this chunk
                chunk_actors = set()
                for b in chunk:
                    chunk_actors.update(actor_snapshot.get(b.block_id, []))
                chunk_actor_reg = {k: v for k, v in actor_reg_dict.items() if k in chunk_actors}
                chunk_actor_str = json.dumps(chunk_actor_reg or actor_reg_dict, separators=(',', ':'))

                chunk_dicts = [json.loads(_serialize(b)) for b in chunk]

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
                        bid = item.get("id")
                        if not bid or bid not in id_map:
                            continue
                        b = id_map[bid]

                        resolved = item.get("actor") or item.get("pronoun_actor")
                        if resolved and not b.resolved_actor:
                            b.resolved_actor = resolved

                        resolutions = item.get("refs") or []
                        res_map = {r.get("ref"): r.get("id") for r in resolutions if r.get("id")}
                        for cr in b.cross_refs:
                            if cr.resolution_method == "unresolved" and cr.ref_text in res_map:
                                cr.resolved_block_id = res_map[cr.ref_text]
                                cr.resolution_method = "llm"

    # ── Pass 3: Final stamp ───────────────────────────────────────────────────
    for b in target_blocks:
        b.enrichment_version = 1
        if not b.resolved_actor:
            b.needs_review = True
            b.review_reasons.append("Actor could not be resolved by structural traversal or LLM")


def validate_gate(job: Job) -> None:
    target = [b for b in job.blocks if b.block_type in TARGET_TYPES]
    incomplete = [b for b in target if b.enrichment_version != 1]
    if incomplete:
        raise LayerError("L5_INCOMPLETE_ENRICHMENT", f"{len(incomplete)} blocks not enriched.")

    unresolved = [b for b in target if not b.resolved_actor]
    if target and (len(unresolved) / len(target)) >= 0.15:
        raise SoftGateFailure("L5_HIGH_UNRESOLVED_ACTOR_RATE", "Unresolved actor rate >= 15%")

    unresolved_refs = [cr for b in target for cr in b.cross_refs if cr.resolution_method == "unresolved"]
    if unresolved_refs:
        raise SoftGateFailure("L5_UNRESOLVED_CROSS_REFS", f"{len(unresolved_refs)} cross-references unresolved.")


def _preceding_text(block: Block, all_blocks: list) -> str:
    for i, b in enumerate(all_blocks):
        if b.block_id == block.block_id and i > 0:
            return all_blocks[i - 1].raw_text
    return ""


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
