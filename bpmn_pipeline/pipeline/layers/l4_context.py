"""L4 — Context Indexing: section anchors, actor registry, glossary."""
import json
import re

from llm.client import LLMClient
from llm.prompts import DEDUPLICATE_ACTORS_SYSTEM, DEDUPLICATE_ACTORS_USER, EXTRACT_GLOSSARY_SYSTEM, EXTRACT_GLOSSARY_USER
from models.schemas import Actor, ActorRegistry, ContextIndex, GlossaryEntry, Job, SectionAnchor, BlockType


# ── spaCy inline actor extraction ───────────────────────────────────────────────

# Actor-role patterns that look like job titles (help filter NER noise)
_ROLE_PATTERN = re.compile(
    r'\b(manager|officer|coordinator|director|analyst|supervisor|lead|head|specialist|administrator|officer|agent|staff|team)\b',
    re.I,
)


def _extract_inline_actors(blocks) -> list[str]:
    """
    Use spaCy NER to find PERSON and ORG entities in STEP/DECISION blocks.
    Filters to entities that look like job titles (via _ROLE_PATTERN) or
    are short enough to be a role name (<=5 words).

    Falls back to empty list if spaCy is unavailable.
    """
    target_types = {BlockType.STEP, BlockType.DECISION, BlockType.EXCEPTION}
    target_blocks = [b for b in blocks if b.block_type in target_types]
    if not target_blocks:
        return []

    try:
        import spacy  # noqa: PLC0415
        nlp = spacy.load("en_core_web_sm", disable=["parser", "lemmatizer"])
    except Exception:
        return []  # spaCy unavailable — degrade gracefully

    texts = [b.raw_text[:300] for b in target_blocks]
    inline_actors: set[str] = set()

    for doc in nlp.pipe(texts, batch_size=64):
        for ent in doc.ents:
            if ent.label_ not in ("PERSON", "ORG"):
                continue
            text = ent.text.strip()
            words = text.split()
            # Keep only short spans (potential role names) or those matching role keywords
            if len(words) <= 5 and (_ROLE_PATTERN.search(text) or len(words) <= 3):
                inline_actors.add(text)

    return sorted(inline_actors)


def run(job: Job) -> None:

    blocks = job.blocks
    id_map = {b.block_id: b for b in blocks}
    llm = LLMClient(job)

    section_anchors = _build_section_anchors(blocks)
    exception_blocks = [b.block_id for b in blocks if b.block_type == BlockType.EXCEPTION]
    actor_registry = _build_actor_registry(job, llm)
    glossary = _extract_glossary(job, llm)

    job.context_index = ContextIndex(
        job_id=job.job_id,
        section_anchors=section_anchors,
        glossary=glossary,
        exception_blocks=exception_blocks,
        actor_registry=actor_registry,
    )


def validate_gate(job: Job) -> None:
    if not job.context_index or not job.context_index.actor_registry:
        raise LayerError("L4_NO_ACTORS", "Actor registry is empty.")
    if not job.context_index.actor_registry.actors:
        raise LayerError("L4_NO_ACTORS", "No actors found in document.")


def _build_section_anchors(blocks) -> list:
    anchors = []
    for b in blocks:
        if b.block_type == BlockType.HEADER:
            text = b.raw_text.strip()
            anchors.append(SectionAnchor(anchor_text=text, block_id=b.block_id, heading_path=b.heading_path))
            # Short aliases: remove leading numbering
            short = re.sub(r'^\d+[\.\d]*\s*', '', text).strip()
            if short and short != text:
                anchors.append(SectionAnchor(anchor_text=short, block_id=b.block_id, heading_path=b.heading_path))
        elif b.block_type == BlockType.STEP and b.list_depth == 1 and b.list_index:
            anchors.append(SectionAnchor(
                anchor_text=f"Step {b.list_index}",
                block_id=b.block_id,
                heading_path=b.heading_path,
            ))
    return anchors


def _build_actor_registry(job, llm: LLMClient) -> ActorRegistry:
    actor_candidates = []
    role_headers = re.compile(r'role|actor|responsible|performed by', re.I)

    # Explicit ACTOR blocks — highest confidence
    for b in job.blocks:
        if b.block_type == BlockType.ACTOR:
            actor_candidates.append(b.raw_text.strip())

    # Inline actors mined via spaCy NER from STEP/DECISION blocks (TASK 5)
    inline_actors = _extract_inline_actors(job.blocks)
    for actor in inline_actors:
        if actor not in actor_candidates:
            actor_candidates.append(actor)

    if not actor_candidates:
        # Fallback: short text blocks that might be role names
        for b in job.blocks:
            words = b.raw_text.split()
            if len(words) <= 4 and b.block_type in [BlockType.STEP, BlockType.NOTE]:
                actor_candidates.append(b.raw_text.strip())

    if not actor_candidates:
        return ActorRegistry(job_id=job.job_id, actors=[Actor(canonical_name="Process Owner")])

    result = llm.call(
        layer=4,
        template_name="DEDUPLICATE_ACTORS",
        system_prompt=DEDUPLICATE_ACTORS_SYSTEM,
        user_prompt=DEDUPLICATE_ACTORS_USER.format(
            actor_candidates_json=json.dumps(actor_candidates, indent=2)
        ),
    )

    actors = []
    if result:
        for item in result:
            actors.append(Actor(
                canonical_name=item.get("canonical_name", "Unknown"),
                aliases=item.get("aliases", []),
                source_method="llm_scan",
            ))

    if not actors:
        actors = [Actor(canonical_name=c) for c in set(actor_candidates)]

    return ActorRegistry(job_id=job.job_id, actors=actors)


def _extract_glossary(job, llm: LLMClient) -> list:
    glossary_re = re.compile(r'glossary|definitions|terms', re.I)
    section_text = None
    section_block_id = None

    for b in job.blocks:
        if b.block_type == BlockType.HEADER and glossary_re.search(b.raw_text):
            section_block_id = b.block_id
            child_texts = []
            for cb in job.blocks:
                if cb.parent_id == b.block_id:
                    child_texts.append(cb.raw_text)
            section_text = "\n".join(child_texts)
            break

    if not section_text:
        return []

    result = llm.call(
        layer=4,
        template_name="EXTRACT_GLOSSARY",
        system_prompt=EXTRACT_GLOSSARY_SYSTEM,
        user_prompt=EXTRACT_GLOSSARY_USER.format(section_text=section_text),
    )

    entries = []
    if result:
        for item in result:
            entries.append(GlossaryEntry(
                term=item.get("term", ""),
                definition=item.get("definition", ""),
                block_id=section_block_id or "",
            ))
    return entries


class LayerError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
