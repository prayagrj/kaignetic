"""LLM prompt templates for all pipeline layers."""
# ── L3 — Context (actor deduplication) ──────────────────────────────────────

CANONICALIZE_ACTORS_SYSTEM = """\
You are given a map of actor names found in a business process document, along with the document section headings under which each actor was mentioned.
Your job is to group actors that refer to the same real-world role/person, then pick the best canonical name and list all aliases.

Rules:
- Use the section headings as context to disambiguate: the same abbreviation in different sections may mean different actors.
- Canonical name should be the clearest, most complete form (e.g. prefer "HR Manager" over "HR" or "Manager").
- Aliases are all other strings that refer to the same actor.
- Do NOT merge actors that are genuinely different roles, even if they share words.
- Discard any entry that is clearly not an actor (e.g. table headers, generic words like "System" without context, numbers).
- Respond with JSON only.\
"""

CANONICALIZE_ACTORS_USER = """\
Actor mentions found during atomization (actor_name → list of section headings where it appeared):
{actor_heading_map_json}

Return a JSON array:
[{{ "canonical_name": "string", "aliases": ["string"] }}]\
"""


# ── L4 — Atomizer ────────────────────────────────────────────────────────────

ATOMIZE_WITH_CONTEXT_SYSTEM = """\
You decompose enriched SOP blocks into atomic process units.
Each unit has ONE action and ONE actor. Two types exist: STEP and DECISION.

Language rules (highest priority):
- `action` MUST use the exact words, phrasing, and terminology from the source document.
- Do NOT paraphrase, reword, or substitute synonyms. Copy key verbs, nouns, and phrases verbatim.
- Only restructure minimally (e.g. converting passive to active voice, inserting the actor as subject).
- Preserve domain-specific terms, proper nouns, abbreviations, and document-specific labels exactly as written.

Decomposition rules:
- Split on "and", "then", "after that" only when they describe distinct, separate actions.
- Multiple actors doing different things → one unit per actor.
- `action` must be a complete imperative sentence — never truncate.
- Use ONLY canonical actor names from the provided list.
- If a block contains no actionable steps (purely informational/scope), return an empty atomic_units list.

Type A — STEP: an actor performs a single action.
  Fields: prev_step_ids, next_step_ids, is_terminal.
  Use prev/next to express sequential order within the block and across blocks in the window.
  A step with len(next_step_ids) > 1 is a parallel split — no condition, just concurrent work.
  A step with len(prev_step_ids) > 1 is a join of parallel branches.
  Set is_terminal=true when the step explicitly ends a process path: the document says something like
  "process ends", "procedure is complete", "no further action required", "file is closed", or the step
  has no successor in the document and is clearly a concluding action (archive, notify completion, close case).
  IMPORTANT: A task that only runs IF A CONDITION IS MET is still a STEP.
             The condition belongs on the gateway EDGE, not in the step itself.

Type B — DECISION: a conditional fork that routes the process to different paths.
  Use when the text explicitly describes a fork (if/else, check result, approval/rejection, etc.).
  Fields: prev_step_ids, branches (≥2 DecisionBranch entries).
  Each branch: label (outcome name), output_variables (short snake_case names describing this
  outcome, e.g. ["employee_replied"]), next_step_id (unit_id of the first step on this branch).
  output_variables are purely descriptive edge labels — they are NOT data variables.
  Do NOT use DECISION for a single conditional task — that is a STEP.

Respond with JSON only.\
"""

ATOMIZE_WITH_CONTEXT_USER = """\
Process context (pre-conditions, scope — use to resolve ambiguity):
{preamble_context}

Context Blocks (nearby blocks for reference only — do NOT atomize these):
{context_blocks_text}

Target Blocks to Atomize (produce atomic_units for each block_id):
{target_blocks_text}

Return a JSON array — one entry per Target Block ID:
[
  {{
    "block_id": "string",
    "atomic_units": [
      {{
        "unit_id": "short unique id, e.g. u_001",
        "step_type": "STEP|DECISION",
        "action": "complete imperative sentence",
        "actor": "canonical actor name",

        "prev_step_ids": ["unit_id"],
        "next_step_ids": ["unit_id"],
        "is_terminal": false,

        "branches": [
          {{
            "label": "string",
            "output_variables": ["snake_case_outcome"],
            "next_step_id": "unit_id or null"
          }}
        ]
      }}
    ]
  }}
]

Rules:
- STEP units: populate prev_step_ids, next_step_ids, and is_terminal. Leave branches=[].
- DECISION units: populate prev_step_ids and branches (≥2). Leave next_step_ids=[] and is_terminal=false.
- unit_ids must be unique across ALL blocks in this response.
- prev/next_step_ids may reference unit_ids in other blocks within the same target set.
- If connection to a prior batch's unit is unknown, leave prev_step_ids=[].
- is_terminal=true only for steps that explicitly conclude a process path per the document language.\
"""


# ── L6 — Process Split Arbiter ───────────────────────────────────────────────

PROCESS_SPLIT_SYSTEM = """\
You are an expert business-process analyst. You are given two candidate sub-processes
extracted from the same SOP document. Each has a name, a list of actors, the document
headings it covers, and a sample of its tasks.

Decide whether these two sub-processes should be kept as SEPARATE BPMN files or
MERGED into a single BPMN file.

Merge when:
- Sub-process B is a direct continuation of sub-process A (B picks up where A ends).
- They share the same actors AND cover the same top-level document heading.
- One is clearly a sub-step or follow-up of the other with no conceptual boundary.

Keep separate when:
- They represent genuinely independent workflows that could run at different times
  or be owned by different teams.
- They cover different top-level process phases with distinct start/end conditions.
- Combining them would produce a confusing, overly long diagram.

Respond with JSON only.\
"""

PROCESS_SPLIT_USER = """\
Sub-process A:
{process_a_json}

Sub-process B:
{process_b_json}

Return exactly:
{{
  "decision": "merge" | "split",
  "confidence": 0.0,
  "reason": "one sentence"
}}\
"""


# ── L5 — Cross-Section Edge Judgment ─────────────────────────────────────────

CROSS_SECTION_EDGES_SYSTEM = """\
You are an expert business-process analyst reviewing a BPMN process flow.

You are given pairs of consecutive task/gateway nodes from an SOP document that belong to
DIFFERENT document sections. For each pair, decide whether the first node should flow directly
into the second node (i.e., a sequence flow edge should be added between them).

Add an edge when:
- The second node is a direct continuation of the first (e.g., "after step A is done, do step B").
- They are part of the same logical workflow even though the document heading changed.
- The section boundary is just a formatting/heading change, not a true process boundary.

Do NOT add an edge when:
- The two nodes belong to genuinely separate sub-processes or phases.
- The second node has its own explicit predecessors already (it starts a new independent flow).
- Adding the edge would create a logical contradiction or loop.

Respond with JSON only.\
"""

CROSS_SECTION_EDGES_USER = """\
Consecutive node pairs crossing section boundaries (evaluate each independently):
{pairs_json}

Return a JSON array — one entry per pair, in the same order:
[
  {{
    "pair_index": 0,
    "add_edge": true,
    "confidence": 0.0,
    "reason": "one sentence"
  }}
]\
"""


# ── L6 — Isolated Subgraph Reconnection ──────────────────────────────────────

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
