# Multi-Pass SOP Extraction — Implementation Spec

## Context

This document is a Claude Code implementation guide. The existing BPMN pipeline lives under `backend/`. All new files go into that directory tree. Read `CLAUDE.md` and `backend/models/schemas.py` before writing any code.

---

## Overview of Changes

Four additions to the existing 8-layer pipeline:

| File | Type | Insert before |
|---|---|---|
| `backend/pipeline/layers/l0_survey.py` | New layer | L1 |
| `backend/models/schemas.py` | Extend existing | — |
| `backend/pipeline/layers/l2_atomizer.py` | Modify existing | — |
| `backend/pipeline/layers/l2b_gap_backfill.py` | New layer | L3 |
| `backend/pipeline/layers/l2c_consensus_qc.py` | New layer | L3 |
| `backend/pipeline/orchestrator.py` | Modify existing | — |
| `backend/llm/prompts.py` | Extend existing | — |

---

## Step 1 — Extend `backend/models/schemas.py`

Add these dataclasses. Do not remove or modify any existing dataclasses.

```python
from typing import Literal, Optional
from dataclasses import dataclass, field

@dataclass
class ActorDefinition:
    """A known actor extracted during the document survey pass."""
    canonical_name: str
    aliases: list[str]          # e.g. ["RM", "regional mgr", "the manager"]
    role_description: str       # one sentence describing the role

@dataclass
class SectionSummary:
    """High-level summary of a document section."""
    heading_path: list[str]     # matches StructuredChunk breadcrumb format
    summary: str                # one sentence
    section_type: Literal["PROCESS", "REFERENCE", "APPENDIX", "GLOSSARY", "PREAMBLE", "UNKNOWN"]

@dataclass
class DocumentSchema:
    """
    Global document intelligence produced by l0_survey.
    Stored on Job and read by all downstream layers.
    """
    document_type: str                          # e.g. "Quality Management SOP", "IT Runbook"
    scope: str                                  # one-sentence scope description
    actors: list[ActorDefinition]
    systems: list[str]                          # tool/system names referenced (e.g. "SAP", "Jira")
    global_conditions: list[str]               # invariants that apply across all steps
    key_terms: dict[str, str]                  # term -> definition (from glossary/preamble)
    section_map: list[SectionSummary]
    info_location_index: dict[str, str]        # "approval authority" -> "Section 3.1, para 2"

@dataclass
class GapResolution:
    """
    A single resolved gap in an atomic unit, produced by l2b_gap_backfill.
    Units are patched in-place; this record provides an audit trail.
    """
    unit_id: str
    gap_type: Literal[
        "unresolved_actor",
        "implicit_reference",
        "missing_precondition",
        "unresolved_term"
    ]
    original_value: str
    resolved_value: str
    source_section: str         # heading path string where resolution was found
    confidence: float           # 0.0–1.0

@dataclass
class ConsensusFlag:
    """
    A disagreement between two prompt configurations on the same unit.
    Produced by l2c_consensus_qc. Surfaced in report.json.
    """
    unit_id: str
    field: str                  # "actor", "is_terminal", "action"
    config_a_value: str
    config_b_value: str
```

Also add these fields to the existing `Job` dataclass (add with `field(default_factory=...)` so existing code doesn't break):

```python
document_schema: Optional[DocumentSchema] = None
gap_resolutions: list[GapResolution] = field(default_factory=list)
consensus_flags: list[ConsensusFlag] = field(default_factory=list)
```

---

## Step 2 — Add prompts to `backend/llm/prompts.py`

Add these four prompt templates. Do not modify existing prompts.

### `SURVEY_DOCUMENT`

```python
SURVEY_DOCUMENT = """
You are analysing a Standard Operating Procedure (SOP) document before detailed extraction begins.

Your goal is to build a global understanding of the document so that later extraction steps can use it as context.

Respond with a single JSON object. No preamble. No markdown fences.

JSON schema:
{
  "document_type": "<string: e.g. 'Quality Management SOP'>",
  "scope": "<string: one sentence>",
  "actors": [
    {
      "canonical_name": "<string>",
      "aliases": ["<string>", ...],
      "role_description": "<string: one sentence>"
    }
  ],
  "systems": ["<string>", ...],
  "global_conditions": ["<string>", ...],
  "key_terms": { "<term>": "<definition>", ... },
  "section_map": [
    {
      "heading_path": ["<string>", ...],
      "summary": "<string: one sentence>",
      "section_type": "PROCESS|REFERENCE|APPENDIX|GLOSSARY|PREAMBLE|UNKNOWN"
    }
  ],
  "info_location_index": {
    "<question a reader might ask>": "<heading path or section reference where the answer lives>"
  }
}

Rules:
- actors: include every role that performs or authorises any action. Include their shorthand aliases.
- global_conditions: include only invariants that apply across ALL steps (e.g. "all changes require dual approval"). Do not include step-specific conditions.
- key_terms: extract defined terms from glossary or preamble sections only.
- info_location_index: think like a new employee reading this SOP. What would they need to look up? List 5–15 such questions and where the answers are.
- If the document has no glossary or preamble, return empty dicts/lists for those fields.

Document (compressed):
{compressed_document}
"""
```

### `DOCUMENT_CONTEXT_HEADER`

```python
DOCUMENT_CONTEXT_HEADER = """
=== DOCUMENT CONTEXT (read before processing the chunk below) ===

Document type: {document_type}
Scope: {scope}

Known actors in this document:
{actors_block}

Global conditions that apply to ALL steps:
{global_conditions_block}

Key terms:
{key_terms_block}

Current section type: {section_type}
=== END DOCUMENT CONTEXT ===

"""
```

### `RESOLVE_GAP`

```python
RESOLVE_GAP = """
You are resolving a specific gap in an extracted process step.

Gap type: {gap_type}
Step action: {action}
Current value that needs resolving: {current_value}

Relevant document excerpt (from {source_section}):
{source_excerpt}

Known actors: {actors_list}
Known systems: {systems_list}

Task: Provide the resolved value for the gap.

Respond with JSON only. No preamble.
{
  "resolved_value": "<string>",
  "confidence": <float 0.0-1.0>,
  "reasoning": "<one sentence>"
}

If you cannot resolve with confidence >= 0.4, set resolved_value to the original value and confidence to 0.0.
"""
```

### `CONSENSUS_CHECK`

```python
CONSENSUS_CHECK = """
You are extracting structured data from a process step with explicit attention to actor roles.

Document type: {document_type}
Known actors (canonical names and aliases):
{actors_block}

Process step text:
{step_text}

Extract the following fields. Be conservative — if uncertain, say so.

Respond with JSON only. No preamble.
{
  "actor": "<canonical actor name from the known actors list, or 'UNKNOWN'>",
  "action": "<concise verb-phrase describing the action>",
  "is_terminal": <true|false>,
  "confidence_actor": <float 0.0-1.0>,
  "confidence_terminal": <float 0.0-1.0>
}
"""
```

---

## Step 3 — Create `backend/pipeline/layers/l0_survey.py`

```python
"""
L0 Survey Layer — single-pass global document understanding.

Reads the full document text (compressed to headings + first sentences),
produces a DocumentSchema, and stores it on job.document_schema.

Runs before L1 extraction. Does not modify chunks.
"""

import json
from backend.models.schemas import Job, DocumentSchema, ActorDefinition, SectionSummary
from backend.llm.client import LLMClient
from backend.llm.prompts import SURVEY_DOCUMENT
from backend.config import LLM_MAX_INPUT_TOKENS


class L0Survey:

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def run(self, job: Job) -> Job:
        compressed = self._compress_document(job)
        prompt = SURVEY_DOCUMENT.format(compressed_document=compressed)

        raw = self.llm.call(prompt, layer="l0_survey")
        data = json.loads(raw) if isinstance(raw, str) else raw

        job.document_schema = self._parse_schema(data)
        return job

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _compress_document(self, job: Job) -> str:
        """
        Build a compressed representation: heading breadcrumb + first sentence
        of each chunk. Stays well under LLM_MAX_INPUT_TOKENS for most SOPs.
        """
        lines = []
        for chunk in job.structured_chunks:
            heading = " > ".join(chunk.heading_breadcrumb) if chunk.heading_breadcrumb else "(no heading)"
            # Take the first non-empty text element as the first sentence
            first_text = ""
            for el in chunk.elements:
                text = getattr(el, "text", "").strip()
                if text:
                    # Truncate to first sentence (naive split)
                    first_text = text.split(".")[0][:200]
                    break
            lines.append(f"[{heading}] {first_text}")

        # Hard cap: if still too long, keep first N lines
        full = "\n".join(lines)
        # Rough token estimate: 4 chars per token
        max_chars = LLM_MAX_INPUT_TOKENS * 4
        if len(full) > max_chars:
            full = full[:max_chars] + "\n... (truncated)"
        return full

    def _parse_schema(self, data: dict) -> DocumentSchema:
        actors = [
            ActorDefinition(
                canonical_name=a.get("canonical_name", ""),
                aliases=a.get("aliases", []),
                role_description=a.get("role_description", ""),
            )
            for a in data.get("actors", [])
        ]

        section_map = [
            SectionSummary(
                heading_path=s.get("heading_path", []),
                summary=s.get("summary", ""),
                section_type=s.get("section_type", "UNKNOWN"),
            )
            for s in data.get("section_map", [])
        ]

        return DocumentSchema(
            document_type=data.get("document_type", "SOP"),
            scope=data.get("scope", ""),
            actors=actors,
            systems=data.get("systems", []),
            global_conditions=data.get("global_conditions", []),
            key_terms=data.get("key_terms", {}),
            section_map=section_map,
            info_location_index=data.get("info_location_index", {}),
        )
```

---

## Step 4 — Modify `backend/pipeline/layers/l2_atomizer.py`

Find the method that builds the prompt for each batch (likely calls a prompt template with chunk content). Add a call to `_build_context_header()` and prepend it to the prompt string.

Add this private method to the `L2Atomizer` class:

```python
def _build_context_header(self, job: Job, chunk) -> str:
    """
    Build the document context block to prepend to each atomizer batch prompt.
    Selects relevant actors based on chunk heading, injects global conditions and key terms.
    """
    from backend.llm.prompts import DOCUMENT_CONTEXT_HEADER

    schema = job.document_schema
    if schema is None:
        return ""  # graceful degradation if l0 didn't run

    # Match actors relevant to this chunk's heading path
    chunk_headings_lower = {h.lower() for h in (chunk.heading_breadcrumb or [])}
    # Include all actors — the list is short enough for the context window
    actors_block = "\n".join(
        f"- {a.canonical_name} (aliases: {', '.join(a.aliases) or 'none'}): {a.role_description}"
        for a in schema.actors
    ) or "None identified."

    global_conditions_block = "\n".join(
        f"- {c}" for c in schema.global_conditions
    ) or "None."

    # Only inject key terms whose keys appear in chunk text
    chunk_text = " ".join(
        getattr(el, "text", "") for el in chunk.elements
    ).lower()
    relevant_terms = {
        k: v for k, v in schema.key_terms.items()
        if k.lower() in chunk_text
    }
    key_terms_block = "\n".join(
        f"- {k}: {v}" for k, v in relevant_terms.items()
    ) or "None."

    # Determine section type from section_map
    section_type = "UNKNOWN"
    for s in schema.section_map:
        if s.heading_path == chunk.heading_breadcrumb:
            section_type = s.section_type
            break

    return DOCUMENT_CONTEXT_HEADER.format(
        document_type=schema.document_type,
        scope=schema.scope,
        actors_block=actors_block,
        global_conditions_block=global_conditions_block,
        key_terms_block=key_terms_block,
        section_type=section_type,
    )
```

Then in the existing batch prompt construction (wherever `prompt = ...` is assembled for each batch), prepend the header:

```python
context_header = self._build_context_header(job, current_chunk)
prompt = context_header + existing_prompt
```

---

## Step 5 — Create `backend/pipeline/layers/l2b_gap_backfill.py`

```python
"""
L2b Gap Backfill Layer.

Runs after l2_atomizer. Scans all atomic units for under-specified fields
and runs targeted LLM resolution calls per gap.

Gap signals (rules-first, no LLM for detection):
  - unresolved_actor:    unit.actor not in DocumentSchema actor names/aliases
  - implicit_reference:  action contains unresolved pronoun-like references
  - unresolved_term:     action contains a DocumentSchema key_term that isn't in the unit

Resolution:
  - Look up the relevant section via info_location_index
  - Extract that chunk's text
  - Run a focused RESOLVE_GAP LLM call
  - Accept if confidence >= GAP_CONFIDENCE_THRESHOLD, else flag for review
"""

import json
import re
from dataclasses import replace
from backend.models.schemas import (
    Job, GapResolution,
    AtomicStepUnit, AtomicDecisionUnit,
)
from backend.llm.client import LLMClient
from backend.llm.prompts import RESOLVE_GAP
from backend.config import GAP_CONFIDENCE_THRESHOLD  # add this to config.py: GAP_CONFIDENCE_THRESHOLD = 0.4

# Patterns that signal an implicit / unresolved reference in an action string
IMPLICIT_REFERENCE_PATTERNS = [
    r"\bthe form\b",
    r"\bthe system\b",
    r"\bthe request\b",
    r"\bthe document\b",
    r"\bthe manager\b",
    r"\bthe approver\b",
    r"\bthe above\b",
    r"\bsaid \w+",
    r"\bthe aforementioned\b",
]
_IMPLICIT_RE = re.compile("|".join(IMPLICIT_REFERENCE_PATTERNS), re.IGNORECASE)


class L2bGapBackfill:

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def run(self, job: Job) -> Job:
        if job.document_schema is None:
            # l0_survey didn't run; skip gracefully
            return job

        schema = job.document_schema
        known_actor_names: set[str] = set()
        for a in schema.actors:
            known_actor_names.add(a.canonical_name.lower())
            for alias in a.aliases:
                known_actor_names.add(alias.lower())

        all_units = self._collect_units(job)

        for process_model in job.process_models:
            for unit in self._collect_units_from_model(process_model):
                gaps = self._detect_gaps(unit, known_actor_names, schema)
                for gap_type, current_value in gaps:
                    resolution = self._resolve_gap(
                        unit, gap_type, current_value, schema, job
                    )
                    if resolution:
                        job.gap_resolutions.append(resolution)
                        if resolution.confidence >= GAP_CONFIDENCE_THRESHOLD:
                            self._patch_unit(unit, gap_type, resolution.resolved_value)
                        else:
                            # Mark for human review via job status (don't override COMPLETE→FAILED)
                            if job.status.name == "COMPLETE":
                                job.status = job.status.__class__["NEEDS_REVIEW"]

        return job

    # ------------------------------------------------------------------

    def _collect_units(self, job: Job) -> list:
        units = []
        for pm in job.process_models:
            units.extend(self._collect_units_from_model(pm))
        return units

    def _collect_units_from_model(self, process_model) -> list:
        units = []
        units.extend(process_model.atomic_step_units or [])
        units.extend(process_model.atomic_decision_units or [])
        return units

    def _detect_gaps(
        self, unit, known_actor_names: set[str], schema
    ) -> list[tuple[str, str]]:
        """
        Returns list of (gap_type, current_value) tuples.
        Pure rule-based — no LLM calls here.
        """
        gaps = []
        action = getattr(unit, "action", "") or ""
        actor = getattr(unit, "actor", "") or ""

        # Gap 1: unresolved actor
        if actor.lower() not in known_actor_names and actor.strip():
            gaps.append(("unresolved_actor", actor))

        # Gap 2: implicit reference in action
        if _IMPLICIT_RE.search(action):
            gaps.append(("implicit_reference", _IMPLICIT_RE.search(action).group(0)))

        # Gap 3: unresolved term used in action but not yet resolved
        for term in schema.key_terms:
            if term.lower() in action.lower():
                # Only flag if the term itself appears as a reference, not inline defined
                gaps.append(("unresolved_term", term))
                break  # one term gap per unit is enough

        return gaps

    def _resolve_gap(
        self, unit, gap_type: str, current_value: str, schema, job: Job
    ) -> GapResolution | None:
        """
        Find the relevant excerpt via info_location_index, then call RESOLVE_GAP prompt.
        """
        action = getattr(unit, "action", "") or ""

        # Find best matching key in info_location_index
        source_section = self._find_relevant_section(action, gap_type, schema)
        source_excerpt = self._extract_section_text(source_section, job)

        if not source_excerpt:
            return None

        actors_list = ", ".join(a.canonical_name for a in schema.actors)
        systems_list = ", ".join(schema.systems)

        prompt = RESOLVE_GAP.format(
            gap_type=gap_type,
            action=action,
            current_value=current_value,
            source_section=source_section,
            source_excerpt=source_excerpt[:600],  # hard cap to stay within token limit
            actors_list=actors_list,
            systems_list=systems_list,
        )

        raw = self.llm.call(prompt, layer="l2b_gap_backfill")
        data = json.loads(raw) if isinstance(raw, str) else raw

        return GapResolution(
            unit_id=getattr(unit, "id", ""),
            gap_type=gap_type,
            original_value=current_value,
            resolved_value=data.get("resolved_value", current_value),
            source_section=source_section,
            confidence=float(data.get("confidence", 0.0)),
        )

    def _find_relevant_section(self, action: str, gap_type: str, schema) -> str:
        """
        Fuzzy match action text against info_location_index keys.
        Falls back to first REFERENCE section if no match.
        """
        action_lower = action.lower()
        best_key = ""
        best_overlap = 0

        for question, location in schema.info_location_index.items():
            # Count word overlap between action and question
            q_words = set(question.lower().split())
            a_words = set(action_lower.split())
            overlap = len(q_words & a_words)
            if overlap > best_overlap:
                best_overlap = overlap
                best_key = location

        # For actor gaps specifically, look for actor-definition sections
        if gap_type == "unresolved_actor":
            for s in schema.section_map:
                if s.section_type in ("PREAMBLE", "REFERENCE"):
                    return " > ".join(s.heading_path)

        return best_key or "document preamble"

    def _extract_section_text(self, section_ref: str, job: Job) -> str:
        """
        Pull raw text from the StructuredChunk matching the section reference.
        """
        if not section_ref:
            return ""

        section_parts = [p.strip().lower() for p in section_ref.replace(" > ", ">").split(">")]

        for chunk in job.structured_chunks:
            breadcrumb_lower = [h.lower() for h in (chunk.heading_breadcrumb or [])]
            # Partial match: section_ref parts appear in breadcrumb
            if any(part in " ".join(breadcrumb_lower) for part in section_parts if part):
                return " ".join(
                    getattr(el, "text", "") for el in chunk.elements
                )[:800]

        return ""

    def _patch_unit(self, unit, gap_type: str, resolved_value: str) -> None:
        """Mutate the unit in-place with the resolved value."""
        if gap_type == "unresolved_actor":
            unit.actor = resolved_value
        elif gap_type in ("implicit_reference", "unresolved_term"):
            # Append clarification to the action rather than replacing it
            unit.action = f"{unit.action} [{resolved_value}]"
```

---

## Step 6 — Create `backend/pipeline/layers/l2c_consensus_qc.py`

```python
"""
L2c Consensus QC Layer.

Runs after l2b_gap_backfill. Re-processes only ambiguous units
(those in gap_resolutions with low confidence, or where actor is still UNKNOWN)
using two different prompt configurations. Flags disagreements as ConsensusFlag.

Only affects job.consensus_flags and job.status. Does not mutate units.
"""

import json
from backend.models.schemas import Job, ConsensusFlag
from backend.llm.client import LLMClient
from backend.llm.prompts import CONSENSUS_CHECK

CONSENSUS_CONFIDENCE_GATE = 0.65  # units below this from gap backfill get QC'd
FIELD_AGREEMENT_REQUIRED = ["actor", "is_terminal"]  # fields that must agree


class L2cConsensusQC:

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def run(self, job: Job) -> Job:
        if job.document_schema is None:
            return job

        schema = job.document_schema

        # Identify unit IDs that need QC
        low_confidence_ids = {
            r.unit_id
            for r in job.gap_resolutions
            if r.confidence < CONSENSUS_CONFIDENCE_GATE
        }

        actors_block = "\n".join(
            f"- {a.canonical_name} (aliases: {', '.join(a.aliases) or 'none'})"
            for a in schema.actors
        )

        for process_model in job.process_models:
            all_units = list(process_model.atomic_step_units or []) + \
                        list(process_model.atomic_decision_units or [])

            for unit in all_units:
                unit_id = getattr(unit, "id", "")
                actor = getattr(unit, "actor", "") or ""

                should_qc = (
                    unit_id in low_confidence_ids
                    or actor.upper() in ("UNKNOWN", "", "UNRESOLVED")
                )

                if not should_qc:
                    continue

                action = getattr(unit, "action", "") or ""
                step_text = f"Actor: {actor}\nAction: {action}"

                result_a = self._run_config(
                    step_text, actors_block, schema.document_type, "default"
                )
                result_b = self._run_config(
                    step_text, actors_block, schema.document_type, "role_explicit"
                )

                if result_a is None or result_b is None:
                    continue

                for field in FIELD_AGREEMENT_REQUIRED:
                    val_a = str(result_a.get(field, "")).strip().lower()
                    val_b = str(result_b.get(field, "")).strip().lower()
                    if val_a != val_b:
                        job.consensus_flags.append(ConsensusFlag(
                            unit_id=unit_id,
                            field=field,
                            config_a_value=val_a,
                            config_b_value=val_b,
                        ))

        if job.consensus_flags:
            if job.status.name == "COMPLETE":
                job.status = job.status.__class__["NEEDS_REVIEW"]

        return job

    # ------------------------------------------------------------------

    def _run_config(
        self,
        step_text: str,
        actors_block: str,
        document_type: str,
        config: str,
    ) -> dict | None:
        """
        Run CONSENSUS_CHECK prompt.
        config='role_explicit' adds extra instruction to be conservative on actor assignment.
        """
        extra = ""
        if config == "role_explicit":
            extra = "\nIMPORTANT: Only assign an actor if their name or a recognised alias appears explicitly in the step text. If ambiguous, use 'UNKNOWN'."

        prompt = CONSENSUS_CHECK.format(
            document_type=document_type,
            actors_block=actors_block,
            step_text=step_text,
        ) + extra

        try:
            raw = self.llm.call(prompt, layer="l2c_consensus_qc")
            return json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return None
```

---

## Step 7 — Add config constants to `backend/config.py`

Add these lines (do not modify existing constants):

```python
# Multi-pass extraction settings
GAP_CONFIDENCE_THRESHOLD = 0.4      # minimum confidence to accept a gap resolution
CONSENSUS_CONFIDENCE_GATE = 0.65    # units below this confidence are sent to consensus QC
L0_SURVEY_ENABLED = True            # set False to skip Pass 0 (e.g. for very short docs)
L2B_BACKFILL_ENABLED = True
L2C_CONSENSUS_ENABLED = True
```

---

## Step 8 — Modify `backend/pipeline/orchestrator.py`

Import the new layers:

```python
from backend.pipeline.layers.l0_survey import L0Survey
from backend.pipeline.layers.l2b_gap_backfill import L2bGapBackfill
from backend.pipeline.layers.l2c_consensus_qc import L2cConsensusQC
from backend.config import L0_SURVEY_ENABLED, L2B_BACKFILL_ENABLED, L2C_CONSENSUS_ENABLED
```

Insert into the layer execution sequence. The new order is:

```
L0Survey          ← new (before L1)
L1Extraction      ← unchanged
L2Atomizer        ← modified (context header injection)
L2bGapBackfill    ← new (after L2, before L3)
L2cConsensusQC    ← new (after L2b, before L3)
L3Context         ← unchanged (but now seeded from DocumentSchema)
L4NodeDetector    ← unchanged
L5EdgeDetector    ← unchanged
L6ProcessSplitter ← unchanged
L7DagResolver     ← unchanged
L8Translator      ← unchanged
```

Wrap new layers with their config flags:

```python
if L0_SURVEY_ENABLED:
    job = L0Survey(llm).run(job)

# ... L1 runs here ...

# ... L2 runs here (modified) ...

if L2B_BACKFILL_ENABLED:
    job = L2bGapBackfill(llm).run(job)

if L2C_CONSENSUS_ENABLED:
    job = L2cConsensusQC(llm).run(job)

# ... L3–L8 run here (unchanged) ...
```

---

## Step 9 — Seed `L3Context` from `DocumentSchema`

In `backend/pipeline/layers/l3_context.py`, find where `ActorRegistry` is initialised. Before the LLM canonicalization call, pre-populate it with known actors from `DocumentSchema`:

```python
# At the start of l3_context.run():
if job.document_schema:
    for actor_def in job.document_schema.actors:
        # Pre-register known canonical names so L3 canonicalization
        # starts from ground truth rather than discovering from scratch.
        job.context_index.actor_registry.register(
            canonical=actor_def.canonical_name,
            aliases=actor_def.aliases,
        )
```

Adjust to match the actual `ActorRegistry` API in your codebase.

---

## Step 10 — Surface new fields in `report.json`

In whatever code writes `outputs/{job_id}_report.json`, add:

```python
"gap_resolutions": [
    {
        "unit_id": r.unit_id,
        "gap_type": r.gap_type,
        "original": r.original_value,
        "resolved": r.resolved_value,
        "source": r.source_section,
        "confidence": r.confidence,
    }
    for r in job.gap_resolutions
],
"consensus_flags": [
    {
        "unit_id": f.unit_id,
        "field": f.field,
        "config_a": f.config_a_value,
        "config_b": f.config_b_value,
    }
    for f in job.consensus_flags
],
"document_schema_summary": {
    "document_type": job.document_schema.document_type if job.document_schema else None,
    "actors_found": len(job.document_schema.actors) if job.document_schema else 0,
    "global_conditions": job.document_schema.global_conditions if job.document_schema else [],
},
```

---

## Implementation Notes for Claude Code

1. **Read the existing files first** before writing any code. Especially `schemas.py`, `l2_atomizer.py`, `l3_context.py`, and `orchestrator.py`. Match existing code style exactly.

2. **`LLMClient.call()` signature** — check the actual signature in `llm/client.py` before using it. The `layer=` kwarg may not exist; add it or use the existing parameter name.

3. **`job.structured_chunks`** — verify the exact field name on `Job` for the list of `StructuredChunk` objects. Also verify `chunk.heading_breadcrumb` and `chunk.elements` field names.

4. **`job.process_models`** — in the orchestrator, `ProcessModel` objects may not exist until after L2. In `l2b` and `l2c`, if `job.process_models` is empty (single model not yet split), access units directly from `job` instead — check what field L2 writes to.

5. **`job.status` enum** — check the actual `JobStatus` enum values. Use the correct enum member name for `NEEDS_REVIEW`.

6. **`GAP_CONFIDENCE_THRESHOLD` import** — `l2b_gap_backfill.py` imports it from `config`. Add it to `config.py` first, or define it as a module constant in `l2b` if that's cleaner.

7. **Cache invalidation** — after implementing, bump `LLM_CACHE_VERSION` in `config.py` to invalidate cached responses from previous runs.

8. **Run smoke tests** after each new layer is wired in: `pytest tests/smoke_test.py`. The suite already checks for `COMPLETE`/`NEEDS_REVIEW` status, startEvent/endEvent presence, and gateway wiring — all should still pass.

---

## Testing the New Layers in Isolation

Before wiring into the orchestrator, test each layer standalone:

```python
# Quick sanity check for l0_survey
from backend.models.schemas import Job
from backend.llm.client import LLMClient
from backend.pipeline.layers.l0_survey import L0Survey

# Assuming job has been populated through L1 already
llm = LLMClient(...)
job = ...  # load from a debug output JSON
job = L0Survey(llm).run(job)
print(job.document_schema)
```

For `l2b`, print `job.gap_resolutions` after running and inspect whether detected gaps match what a human would expect from the SOP.
