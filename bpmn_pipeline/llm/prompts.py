"""LLM prompt templates for all pipeline layers."""

# ── L3 — Element Classifier ───────────────────────────────────────────────────

CLASSIFY_ELEMENTS_SYSTEM = """\
You are a process document parser. Classify every element in the input list.

Keep classification coarse — do NOT try to identify sub-types of steps (no decisions, conditions,
exceptions). Fine-grained step classification is handled downstream by the atomizer.

Element types:
- STEP: Any action, task, or activity to be performed by a person or system. Includes conditional
  actions, approval steps, exception handling, and decision points. When in doubt, prefer STEP if
  the text describes something someone must DO.
- ACTOR: Defines a role, person, department, or system involved in the process
  (e.g. "HR Manager", "IT Team", "System"). Must name a specific party.
- NOTE: Informational text that is not part of the flow — explanations, tips, warnings, examples.
- HEADER: A section title, heading, or label that organises content but contains no action.
- META: Scope, purpose, applicability, definitions, glossary entries, prerequisites — contextual
  framing that is not itself a step.
- UNKNOWN: Cannot be confidently classified even with the section heading as context.

Use the section heading path to resolve ambiguity.
You MUST return exactly one JSON object per element_id (no omissions). Respond with JSON only.\
"""

CLASSIFY_ELEMENTS_USER = """\
SOP class: {sop_class}
Section heading: {section_heading}

Elements to classify:
{elements_json}

Return a JSON array — one entry per element, same element_ids as input:
[{{"element_id": "string", "block_type": "STEP|ACTOR|NOTE|HEADER|META|UNKNOWN", "confidence": 0.0}}]\
"""


# ── L3 — Block Classifier (legacy) ───────────────────────────────────────────

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
- Write `action` as a complete, clear imperative sentence. Never truncate or add "…". Be concise but complete.
- Use ONLY canonical actor names from the provided list.
- is_terminal: true if this unit ends a process path (final approval, archival, closure).
- If a block contains no actionable steps (purely informational/scope), return an empty atomic_units list.

Step type (REQUIRED for every unit):
- SIMPLE: a direct, unconditional action (e.g., "Send the completed form to HR").
- CONDITIONAL: an action that only occurs under a specific condition — the condition field MUST be filled
  (e.g., action="Return documents to applicant", condition="If documents are incomplete").
- DECISION: a branching point where the process splits into multiple distinct paths based on an outcome.
  The action describes what is being decided (e.g., "Determine whether the application is approved or rejected").
  The condition field captures the decision question or criteria.
  IMPORTANT: Mark as DECISION only when the text clearly describes a fork — two or more different paths
  are taken depending on the outcome.

Variable extraction (IMPORTANT):
- For each atomic unit, identify what named data variables it CONSUMES (inputs) and PRODUCES (outputs).
- Variable names must be snake_case starting with "V_", e.g. V_request_id, V_approved (bool), V_form_data.
- Reuse variable names from "Known variables" when the same data is referenced.
- Variable types: bool (true/false decision result), data (a document/form/record), id (identifier), count, unknown.

Context blocks are provided for reference only. Do NOT produce atomic_units for them.

Respond with JSON only.\
"""

ATOMIZE_WITH_CONTEXT_USER = """\
Valid actors:
{actors_json}

Section: {section_context}

Process context (pre-conditions, scope — use this to resolve ambiguity):
{preamble_context}

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
        "step_type": "SIMPLE|CONDITIONAL|DECISION",
        "action": "string (complete sentence, no truncation)",
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
1. Determine the gateway type — choose exactly one:
   - EXCLUSIVE: only ONE branch is taken based on a condition.
     Signals: "if … then … otherwise", "depending on", "based on the result",
              "if approved / if rejected", "unless", "in case of".
   - PARALLEL: ALL branches run at the same time (split into concurrent work).
     Signals: "simultaneously", "at the same time", "in parallel", "concurrently",
              "both X and Y are done", "trigger all of the following".
   - EVENT_BASED: the next step depends on which external event arrives first.
     Signals: "wait for", "whichever comes first", "upon receiving",
              "if the timer expires before", "escalate if no response by".

2. Write a short human-readable gateway_label that names what is being decided or split
   (e.g. "Approval decision", "Parallel document preparation", "Response or timeout").

3. Identify ALL branches — a valid gateway has at least 2 branches. Tips:
   - Following units with step_type="CONDITIONAL" are likely branch entry points — use their unit_id.
   - If only one path is explicit, add a "Default / otherwise" branch as the second.
   For each branch provide:
   - label: concise text on the arrow (e.g. "Approved", "Rejected", "Documents incomplete", "Default / otherwise")
   - condition_var / condition_value: variable and its value driving this branch (if known)
   - target_unit_id: unit_id from following_units — never invent IDs
   - is_default: true for the catch-all / otherwise branch

Respond with JSON only.\
"""

INFER_SINGLE_GATEWAY_USER = """\
Gateway block (includes all atomic units for this block):
{gateway_block_json}

Preceding units (in order, most recent last):
{preceding_units_json}

Following units (in order) — units with step_type="CONDITIONAL" are branch entry points:
{following_units_json}

Known variables at this point:
{known_vars_json}

Return:
{{
  "gateway_type": "EXCLUSIVE|PARALLEL|EVENT_BASED",
  "gateway_label": "string",
  "branches": [
    {{
      "label": "string",
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


# ── L8 — Isolated Subgraph Reconnection ──────────────────────────────────────

RECONNECT_ISOLATED_NODES_SYSTEM = """\
You reconnect isolated subgraphs in a BPMN process flow diagram.

You are given:
- isolated_nodes: task/gateway nodes that are NOT reachable from the START event.
- reachable_nodes: nodes already wired into the main flow (reachable from START).

For each isolated node decide:
  connect_from — the node_id of a reachable node that should have a sequence flow
                 INTO the isolated node (its logical predecessor).
  connect_to   — the node_id of a reachable node that the isolated node should flow
                 INTO (its logical successor). May be null if the isolated node is
                 a terminal step or if its successor is also isolated.

Reasoning guidelines:
- doc_position is the node's order in the source document (lower = earlier).
- A reachable node just before the isolated node in doc_position is a strong
  predecessor candidate; one just after is a strong successor candidate.
- Actor alignment matters: prefer nodes with the same actor for direct connections.
- Node type matters: a GATEWAY usually precedes conditional branches.
- If you cannot determine a connection with reasonable confidence, set the field
  to null and lower your confidence score.

CRITICAL constraints:
- connect_from MUST be a node_id from reachable_nodes, or null.
- connect_to   MUST be a node_id from reachable_nodes, or null.
- Never invent node_ids. Only use the exact strings from the provided lists.
- Respond with JSON only.\
"""

RECONNECT_ISOLATED_NODES_USER = """\
Isolated nodes (not reachable from START):
{isolated_nodes_json}

Reachable candidate nodes (already in the main flow):
{reachable_nodes_json}

Return a JSON array — one entry per isolated node:
[
  {{
    "node_id": "the isolated node_id (exact string from input)",
    "connect_from": "reachable node_id or null",
    "connect_to":   "reachable node_id or null",
    "confidence":   0.0
  }}
]\
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
