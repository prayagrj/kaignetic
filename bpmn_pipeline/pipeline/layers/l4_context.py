import json
import re

from llm.client import LLMClient
from llm.prompts import DEDUPLICATE_ACTORS_SYSTEM, DEDUPLICATE_ACTORS_USER, EXTRACT_GLOSSARY_SYSTEM, EXTRACT_GLOSSARY_USER
from models.schemas import Actor, ActorRegistry, BlockType, ContextIndex, GlossaryEntry, Job, SectionAnchor

_ROLE_PATTERN = re.compile(
    r'\b(manager|officer|coordinator|director|analyst|supervisor|lead|head|specialist|administrator|agent|staff|team)\b',
    re.I,
)


def run(job: Job) -> None:
    llm = LLMClient(job)

    section_anchors = _build_section_anchors(job.chunks)
    exception_chunks = [c.chunk_id for c in job.chunks if c.chunk_type == BlockType.EXCEPTION]
    actor_registry = _build_actor_registry(job, llm)
    glossary = _extract_glossary(job, llm)

    job.context_index = ContextIndex(
        job_id=job.job_id,
        section_anchors=section_anchors,
        glossary=glossary,
        exception_chunks=exception_chunks,
        actor_registry=actor_registry,
    )


def validate_gate(job: Job) -> None:
    if not job.context_index or not job.context_index.actor_registry:
        raise LayerError("L4_NO_ACTORS", "Actor registry is empty.")
    if not job.context_index.actor_registry.actors:
        raise LayerError("L4_NO_ACTORS", "No actors found in document.")


def _build_section_anchors(chunks: list) -> list:
    """
    One anchor per chunk, keyed by the deepest heading text.
    Also add a number-stripped alias (e.g. "Pre-Joining Phase" for "5. Pre-Joining Phase")
    only when that alias doesn't already exist as another anchor.
    """
    anchors = []
    seen: set[str] = set()  # lowercase anchor_text already emitted

    for chunk in chunks:
        if not chunk.headings:
            continue
        heading_text = chunk.headings[-1]  # deepest = most specific
        key = heading_text.lower()
        if key not in seen:
            anchors.append(SectionAnchor(
                anchor_text=heading_text,
                chunk_id=chunk.chunk_id,
                heading_path=list(chunk.headings),
            ))
            seen.add(key)

        # Short alias without leading numbering (e.g. "5.1 Offer Docs" → "Offer Docs")
        short = re.sub(r'^\d+[\.\d]*\s*', '', heading_text).strip()
        short_key = short.lower()
        if short and short_key != key and short_key not in seen:
            anchors.append(SectionAnchor(
                anchor_text=short,
                chunk_id=chunk.chunk_id,
                heading_path=list(chunk.headings),
            ))
            seen.add(short_key)

    return anchors


def _build_actor_registry(job, llm: LLMClient) -> ActorRegistry:
    actor_candidates = []

    # Explicit ACTOR chunks — highest confidence; use full contextualized text
    for chunk in job.chunks:
        if chunk.chunk_type == BlockType.ACTOR and chunk.contextualized:
            actor_candidates.append(chunk.contextualized[:300])

    # Inline actors via spaCy NER — scan STEP/DECISION chunk contextualized text
    inline_actors = _extract_inline_actors(job.chunks)
    for actor in inline_actors:
        if actor not in actor_candidates:
            actor_candidates.append(actor)

    if not actor_candidates:
        # Fallback: short contextualized chunks that look like role names
        for chunk in job.chunks:
            words = chunk.contextualized.split()
            if len(words) <= 4 and chunk.chunk_type in [BlockType.STEP, BlockType.NOTE]:
                actor_candidates.append(chunk.contextualized.strip())

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


def _extract_inline_actors(chunks: list) -> list:
    """spaCy NER over STEP/DECISION chunk texts in one batch pass."""
    target_types = {BlockType.STEP, BlockType.DECISION, BlockType.EXCEPTION}
    target_chunks = [c for c in chunks if c.chunk_type in target_types]
    if not target_chunks:
        return []

    try:
        import spacy
        nlp = spacy.load("en_core_web_sm", disable=["parser", "lemmatizer"])
    except Exception:
        return []

    # Use contextualized text (contains full section context)
    texts = [c.contextualized[:400] for c in target_chunks]
    inline_actors: set = set()

    for doc in nlp.pipe(texts, batch_size=64):
        for ent in doc.ents:
            if ent.label_ not in ("PERSON", "ORG"):
                continue
            text = ent.text.strip()
            words = text.split()
            if len(words) <= 5 and (_ROLE_PATTERN.search(text) or len(words) <= 3):
                inline_actors.add(text)

    return sorted(inline_actors)


def _extract_glossary(job, llm: LLMClient) -> list:
    """Find glossary chunk by heading, send its contextualized text to LLM."""
    glossary_re = re.compile(r'glossary|definitions|terms', re.I)
    glossary_chunk = None

    for chunk in job.chunks:
        if any(glossary_re.search(h) for h in chunk.headings):
            glossary_chunk = chunk
            break

    if not glossary_chunk or not glossary_chunk.contextualized:
        return []

    result = llm.call(
        layer=4,
        template_name="EXTRACT_GLOSSARY",
        system_prompt=EXTRACT_GLOSSARY_SYSTEM,
        user_prompt=EXTRACT_GLOSSARY_USER.format(section_text=glossary_chunk.contextualized),
    )

    entries = []
    if result:
        for item in result:
            entries.append(GlossaryEntry(
                term=item.get("term", ""),
                definition=item.get("definition", ""),
                chunk_id=glossary_chunk.chunk_id,
            ))
    return entries


class LayerError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
