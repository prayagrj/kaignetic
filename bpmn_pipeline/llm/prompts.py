"""LLM prompt templates for all pipeline layers."""

# ── L3 — Block Classifier ────────────────────────────────────────────────────

CLASSIFY_BLOCKS_SYSTEM = """\
You are a process document parser. You are given a batch of blocks from a business SOP.
One block is marked as the TARGET (\"is_target\": true). The others are surrounding CONTEXT.

Classify ONLY the TARGET block. Use context to resolve ambiguity.

Block types:
- STEP: An action to be performed by a person or system.
- DECISION: A branching point — one of multiple paths chosen based on a condition.
  Strong signals: "if", "check if", "depending on", "based on", "unless", "otherwise".
- EXCEPTION: An error case, escalation, or unusual handling path.
- ACTOR: Defines a role, person, or system responsible for part of the process.
- CONDITION: A precondition, scope statement, or requirement for following steps.
- NOTE: Informational text not part of the process flow.
- UNKNOWN: Cannot be confidently classified.

Respond with JSON only.\
"""

CLASSIFY_BLOCKS_USER = """\
SOP class: {sop_class}
Section: {heading_path}

Target Block:
{target_block_text}

Context Blocks:
{context_blocks_text}

Return a JSON object:
{{"block_type": "STEP|DECISION|EXCEPTION|ACTOR|CONDITION|NOTE|UNKNOWN", "confidence": 0.0, "reasoning": "one sentence"}}\
"""


# ── L4 — Context Indexing ────────────────────────────────────────────────────

DEDUPLICATE_ACTORS_SYSTEM = """\
You are given a list of candidate strings from a business process document. 
Your task is to EXTRACT the actual actor names (e.g., "HR Manager", "System", "Employee"), ignoring any descriptive text, responsibilities, or non-actor noise.
Group identical or similar actors into a canonical name with short aliases.
Do NOT include descriptions or sentences in aliases. Discard generic table headers like "Role".
Respond with JSON only.\
"""

DEDUPLICATE_ACTORS_USER = """\
Actor candidates:
{actor_candidates_json}

Return a JSON array:
[{{"canonical_name": "string", "aliases": ["string"]}}]\
"""

EXTRACT_GLOSSARY_SYSTEM = """\
You extract term definitions from a glossary/definitions section of a business document.
Return all term-definition pairs regardless of formatting. Respond with JSON only.\
"""

EXTRACT_GLOSSARY_USER = """\
Section text:
{section_text}

Return a JSON array:
[{{"term": "string", "definition": "string"}}]\
"""


# ── L5 — Enrichment ──────────────────────────────────────────────────────────

ENRICH_CHUNK_SYSTEM = """\
You are enriching a chunk of blocks from a business SOP. Each block has a list of tasks.

Tasks:
- resolve_actor: Provide the canonical actor name using context.
- resolve_pronoun: Identify the canonical actor referred to by a pronoun.
- resolve_cross_ref: Resolve references to the correct block_id from anchors.

Respond with JSON only.\
"""

ENRICH_CHUNK_USER = """\
Actor registry (canonical → aliases):
{actor_registry_json}

Section anchors (text → block_id):
{section_anchors_json}

Previous chunk results (last 2, for continuity):
{previous_context}

Blocks to enrich:
{blocks_json}

Return a JSON array — one object per block:
[
  {{
    "id": "string",
    "actor": "string or null",
    "pronoun_actor": "string or null",
    "refs": [{{ "ref": "string", "id": "string or null" }}]
  }}
]\
"""


# ── L6 — Atomizer ────────────────────────────────────────────────────────────

ATOMIZE_WITH_CONTEXT_SYSTEM = """\
You decompose enriched SOP blocks into atomic process units.
Each unit: ONE action, ONE actor, optional condition, optional output.

Rules:
- Split on "and", "then", "after that" only when they describe distinct, separate actions.
- Multiple actors doing different things → one unit per actor.
- A decision block → the decision itself is one unit; each branch is NOT included here.
- Use ONLY canonical actor names from the provided list.
- is_terminal: true if this unit ends a process path (final approval, archival, closure).

Variable extraction (IMPORTANT):
- For each atomic unit, identify what named data variables it CONSUMES (inputs) and PRODUCES (outputs).
- Variable names must be snake_case starting with "V_", e.g. V_request_id, V_approved (bool), V_form_data.
- Reuse variable names from "Known variables" when the same data is referenced.
- Variable types: bool (true/false decision result), data (a document/form/record), id (identifier), count, unknown.

Context blocks are provided for reference only to help understand the situation. Do NOT produce atomic_units for them.
All target blocks provided MUST be atomized.

Respond with JSON only.\
"""

ATOMIZE_WITH_CONTEXT_USER = """\
Valid actors:
{actors_json}

Section: {section_context}

Known variables produced so far (reuse names for the same data):
{known_vars_json}

Context Blocks Text:
{context_blocks_text}

Target Blocks to Atomize:
{target_blocks_text}

Return a JSON array — one entry per Target Block ID:
[
  {{
    "block_id": "string",
    "atomic_units": [
      {{
        "sequence_in_block": 0,
        "action": "string",
        "actor": "string",
        "condition": "string or null",
        "output": "string or null",
        "is_terminal": false,
        "inputs": ["V_var_name"],
        "outputs": ["V_var_name"]
      }}
    ]
  }}
]\
"""


# ── L8 — Edge Detector / Gateway Inference ───────────────────────────────────

INFER_SINGLE_GATEWAY_SYSTEM = """\
You analyse a decision/gateway block from a business process. The block may map to one or more atomic units (listed in atomic_units); treat them as one decision context.

Your task:
1. Determine the gateway type: XOR (exactly one branch), AND (all branches parallel), OR (one or more).
2. Identify the branches — what condition (and variable value if available) leads to which next step.
3. For each branch, provide the condition_label and the unit_id of the first step on that branch (unit_id must match one of the listed units or a following unit from context).

Respond with JSON only.\
"""

INFER_SINGLE_GATEWAY_USER = """\
Gateway block (includes all atomic units for this block):
{gateway_block_json}

Preceding units (in order, most recent last):
{preceding_units_json}

Following units (in order):
{following_units_json}

Known variables at this point:
{known_vars_json}

Return:
{{
  "gateway_type": "XOR|AND|OR",
  "branches": [
    {{
      "condition_label": "string",
      "condition_var": "V_var_name or null",
      "condition_value": "true|false|string or null",
      "target_unit_id": "string or null",
      "is_default": false
    }}
  ]
}}\
"""


# ── Legacy prompts (kept as fallback, no longer used by default) ─────────────

CLASSIFY_BLOCKS_BATCH_SYSTEM = """\
You are a process document parser. Classify EVERY block in the input list.

Block types:
- STEP: An action to be performed.
- DECISION: Branching on a condition ("if", "depending on", "unless", "otherwise", etc.).
- EXCEPTION: Error, escalation, or alternate failure path.
- ACTOR: Role or party definition.
- CONDITION: Scope, prerequisite, or applicability.
- NOTE: Informational only, not part of the flow.
- UNKNOWN: Cannot classify confidently.

Use the section heading path for context. If heuristic_hint is present, treat it as a weak signal (verify from text).
You MUST return exactly one JSON object per input block_id (same block_ids, no omissions). Respond with JSON only.\
"""

CLASSIFY_BLOCKS_BATCH_USER = """\
Document SOP class: {sop_class}
Section heading path: {section_heading}

Blocks to classify (each item has block_id, text, optional heuristic_hint):
{blocks_json}

Return a JSON array — one entry per block, same block_ids as input:
[{{"block_id": "string", "block_type": "STEP|DECISION|EXCEPTION|ACTOR|CONDITION|NOTE|UNKNOWN", "confidence": 0.0, "reasoning": "one sentence"}}]\
"""

CLASSIFY_COMBINED_SECTION_SYSTEM = """\
You are a process document parser. Classify the section of a business SOP document.
All text belongs to a single logical section. Determine the primary classification for this section.
Respond with JSON only.\
"""

CLASSIFY_COMBINED_SECTION_USER = """\
Document SOP class: {sop_class}
Section Heading Path: {heading_path}

Combined Section Text:
{combined_text}

Return: {{"block_type": "STEP|DECISION|EXCEPTION|ACTOR|CONDITION|NOTE|UNKNOWN", "confidence": 0.0, "reasoning": "one sentence"}}\
"""

CLASSIFY_SINGLE_BLOCK_SYSTEM = CLASSIFY_BLOCKS_SYSTEM
CLASSIFY_SINGLE_BLOCK_USER = CLASSIFY_BLOCKS_USER

ATOMIZE_BLOCKS_BATCH_SYSTEM = ATOMIZE_WITH_CONTEXT_SYSTEM
ATOMIZE_BLOCKS_BATCH_USER = """\
Valid Actors: {actors_json}
Section Context: {section_context}
Previous context: {previous_context}
Blocks to atomize: {blocks_json}

Each block in input has: id, text, actor, cond.
Return: [{{"block_id": "string", "atomic_units": [{{"sequence_in_block": 0, "action": "string", "actor": "string", "condition": "string or null", "output": "string or null", "is_terminal": false}}]}}]\
"""

INFER_GATEWAY_AND_EDGES_SYSTEM = INFER_SINGLE_GATEWAY_SYSTEM
INFER_GATEWAY_AND_EDGES_USER = """\
Section Context: {section_context}
Previous context: {previous_context}
Decision blocks: {decision_blocks_json}
Cross-reference blocks: {cross_ref_blocks_json}
Block id→label map: {block_id_label_map_json}

Return: {{"gateways": [{{"block_id": "string", "gateway_type": "XOR|AND|OR", "branches": [{{"branch_text": "string", "condition_label": "string", "is_default": false}}]}}], "edge_overrides": [{{"source_block_id": "string", "target_block_id": "string", "edge_label": "string or null", "override_reason": "loop_back|forward_jump|explicit_reference"}}]}}\
"""


# ── L3b — Process Splitter ───────────────────────────────────────────────────

SPLIT_PROCESSES_SYSTEM = """\
You are analysing the structure of a business SOP document to identify its distinct executable processes.

An "executable process" is a section that contains a workflow people actually perform
(it has steps, decisions, actors performing actions).

A "structural/preamble section" is one that only contains:
- Purpose, scope, or applicability statements
- Definitions or glossary entries
- Role lists that do not contain workflow steps
- Container headings with sub-sections but no direct content of their own

Rules:
1. Only include sections that have at least one STEP or DECISION block.
2. You may merge adjacent sections into one process when they clearly form a single workflow
   (e.g., a sub-process and its parent container that introduces it).
3. Do NOT create a process entry for pure preamble sections.
4. Use a concise, human-readable process name for each group.
5. Respond with JSON only.\
"""

SPLIT_PROCESSES_USER = """\
SOP class: {sop_class}
Document title: {doc_title}

Section outline — each entry shows the heading key and its block-type counts:
{outline_json}

Identify all executable processes and how to group the heading keys.

Return a JSON array — one entry per process:
[
  {{
    "process_name": "string",
    "heading_keys": ["exact heading_key string", ...]
  }}
]\
"""
