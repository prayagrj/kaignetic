# BPMN Pipeline

A document-to-BPMN converter that turns unstructured SOP documents (PDF, DOCX) into standards-compliant BPMN 2.0 process diagrams — one diagram per logical process discovered in the document.

---

## The Problem

SOPs live in PDFs and Word docs. Process teams spend hours manually translating them into BPMN diagrams to onboard tools, audit compliance, or hand off to automation. The text is rich but messy: mixed narrative prose, numbered steps, decision conditions, actor references, and cross-references — all nested inside an opaque document structure that no off-the-shelf parser understands.

The pipeline solves this end-to-end: feed in a document, get back a set of valid, layouted `.bpmn` files you can open in any BPMN editor.

---

## How It Works

The pipeline runs as an ordered sequence of ten layers (`L1` → `L10`). Each layer has a single responsibility; it reads from a shared `Job` object and writes back into it. Every layer also runs a **gate check** after execution — if the output is structurally invalid the job halts immediately with a clear error code.

```
PDF/DOCX  →  L1  →  L2  →  L3  →  L3b  →  L4  →  L5  →  L6  →  L6b  →  L7  →  L8  →  L9  →  L10  →  .bpmn
```

---

### L1 — Document Extraction

Converts the source file to clean Markdown using [Docling](https://github.com/DS4SD/docling). `.doc` files are pre-converted to `.docx` via LibreOffice. Excess blank lines are collapsed.

| Input | Output |
|---|---|
| `employee_onboarding.pdf` | Markdown string + raw Docling document dict stored in `job.extraction` |

> Gate: fails if the extracted Markdown is shorter than 100 characters.

---

### L2 — Segmentation

Parses the Markdown line-by-line into a flat list of typed `Block` objects. Headings become `HEADER` blocks; list items carry depth and index; paragraphs are plain content blocks. A parent–child `id` graph is built. The document domain (HR, IT, Finance, etc.) is detected from keyword frequency and stored as `job.sop_class`.

| Input | Output |
|---|---|
| Raw Markdown | `job.blocks = [Block(id="a1f3", raw_text="Submit the leave form", heading_path=["Leave Process", "Employee Steps"], list_depth=1), ...]` |

> Gate: fails if the block tree is empty or contains no content blocks.

---

### L3 — Block Classifier

Labels every non-header block with a semantic type: `STEP`, `DECISION`, `EXCEPTION`, `ACTOR`, `CONDITION`, or `NOTE`.

Two passes run in order:
1. **Heuristic (~70% of blocks)** — heading-path regex and inline patterns classify obvious blocks instantly (e.g. a block under a "Roles" heading → `ACTOR`; text starting with "Note:" → `NOTE`; inline "if / else / whether" → tentative `DECISION`).
2. **LLM (remaining ~30%)** — ambiguous blocks are grouped by section and sent to the LLM in token-bounded batches with their heuristic hint when available.

| Input | Output |
|---|---|
| `{block_id: "a1f3", text: "Submit the leave form"}` | `block.block_type = STEP, confidence = 0.97, method = "structural"` |
| `{block_id: "b2c1", text: "If the employee has been with the company for less than 6 months..."}` | `block.block_type = DECISION, confidence = 0.91, method = "llm"` |

> Gate: fails if no `STEP` blocks found; soft-fails if LLM unknown rate ≥ 20%.

---

### L3b — Process Splitter

A single SOP document often contains several independent sub-processes (e.g. "Onboarding", "Exit Clearance", "Probation Review"). This layer discovers them so each gets its own BPMN diagram.

**Phase 1 (heuristic):** Sections with zero `STEP`/`DECISION`/`EXCEPTION` blocks (purpose statements, glossaries, etc.) are discarded as non-executable preamble.

**Phase 2 (LLM):** The surviving sections are described as a compact outline (heading key + block-type counts) and the LLM is asked to semantically group adjacent sections into coherent process names.

| Input (outline sent to LLM) | Output |
|---|---|
| `[{"heading_key": "3. Joining Formalities", "block_counts": {"STEP": 8, "DECISION": 2}}, {"heading_key": "4. IT Access Setup", "block_counts": {"STEP": 5}}]` | `[{process_name: "Employee Joining & IT Onboarding", heading_keys: ["3. Joining Formalities", "4. IT Access Setup"]}]` |

Each group becomes a `ProcessModel`. All downstream layers (`L5`–`L10`) operate per process, not on the full document.

> Fallback: if the LLM call fails, each surviving section becomes its own process.

---

### L4 — Context Indexer

Builds a `ContextIndex` shared across all subsequent layers:

- **Section anchors** — maps every heading text (and numbered aliases) to the block that defines it, enabling cross-reference resolution later.
- **Actor registry** — candidate role names are collected from `ACTOR` blocks, deduplicated and canonicalised by the LLM (e.g. "HR Exec", "HR Executive", "HR" → `"HR Executive"`).
- **Glossary** — if a "Definitions" section exists, the LLM extracts term–definition pairs.

| Input | Output |
|---|---|
| `["HR Executive", "HR Exec", "hr", "Manager", "Reporting Manager"]` | `ActorRegistry: [Actor(canonical="HR Executive", aliases=["HR Exec", "hr"]), Actor(canonical="Reporting Manager", aliases=["Manager"])]` |

> Gate: fails if no actors can be identified.

---

### L5 — Block Enrichment

Walks every `STEP`, `DECISION`, and `EXCEPTION` block in document order and attaches three pieces of metadata that later layers need to build correct edges.

1. **Actor resolution** — a heading-scoped stack propagates the active actor; blocks under a "Manager" section inherit "Reporting Manager" as their resolved actor. A second LLM pass handles blocks where the actor is still ambiguous (pronoun references, multiple overlapping scopes).
2. **Condition scope** — the governing `CONDITION` block in scope is attached (e.g. "If employee is on probation").
3. **Cross-reference resolution** — inline phrases like "refer to Section 4.2" are matched against section anchors; unresolved ones are sent to the LLM.

| Input block | After enrichment |
|---|---|
| `raw_text: "They must submit the ID proof"` | `resolved_actor: "HR Executive", pronoun_resolution: {original: "They", resolved_to: "HR Executive"}` |

> Gate: soft-fails if > 15% of action blocks have no resolved actor.

---

### L6 — Block Atomizer

Decomposes each enriched block into one or more `AtomicUnit` objects — the smallest independently schedulable actions. A single block like *"Review and approve the request, then notify the employee"* becomes two units.

The LLM receives each batch of blocks with surrounding context and a rolling **variable registry** (`V_approval_granted`, `V_employee_id`, etc.) that carries across batches. Every unit declares the variables it **inputs** (consumes) and **outputs** (produces), enabling data-flow tracking in L6b and L8.

| Block text | Atomic units produced |
|---|---|
| *"Verify the document. If valid, stamp and return; otherwise reject."* | `[{action: "Verify document", outputs: ["V_doc_valid"]}, {action: "Stamp and return", condition: "V_doc_valid = true"}, {action: "Reject document", condition: "V_doc_valid = false", is_terminal: false}]` |

> Gate: fails if any action block yields zero atomic units, or if no start/terminal unit is identified per process.

---

### L6b — Variable Linker

Pure Python, no LLM. Walks all `AtomicUnit` objects in document order and builds a `DataVar` graph: for each named variable, records which unit **produces** it and which units **consume** it. This deterministic registry is later consumed by L8 to generate data-flow edges that reflect real information dependencies — not just document order.

| Variable | Producer unit | Consumer units |
|---|---|---|
| `V_leave_approved` | `u_3a1` (HR approval step) | `u_4b2` (notify employee), `u_4c1` (update payroll) |

> Soft gate: warns if > 30% of variables have no known producer (may indicate LLM extraction gaps).

---

### L7 — Node Detector

Maps every `AtomicUnit` to a `BPMNNode` with the correct type:

- `DECISION` blocks → `GATEWAY` nodes
- Everything else → `TASK` nodes
- One `START_EVENT` and one `END_EVENT` per process (bookends)
- Each `EXCEPTION` block → a `BOUNDARY_EVENT` node (attached to its parent task in L8)

Labels are truncated to 80 characters to keep diagrams readable.

| AtomicUnit | BPMNNode |
|---|---|
| `{action: "Submit leave form", actor: "Employee"}` | `{bpmn_type: TASK, label: "Submit leave form", actor: "Employee"}` |
| `{action: "Is leave balance sufficient?"}` | `{bpmn_type: GATEWAY, gateway_type: null (set in L8)}` |

> Gate: exactly one `START_EVENT` and one `END_EVENT` required; all nodes must have a label.

---

### L8 — Edge Detector

Builds the directed edges that connect BPMN nodes. Four passes run in order, each layering more information:

1. **Sequential base edges** — a flat spine in document order (Start → Task₁ → Task₂ → … → End).
2. **Data-flow edges** — uses the `DataVar` graph from L6b to add edges where a variable producer feeds a consumer that isn't its sequential neighbour.
3. **Gateway branch inference (LLM)** — for each gateway node, the LLM receives the gateway block text, surrounding atomic units as context, and the known variables at that point. It returns the gateway type (`XOR`/`AND`/`OR`) and the branch conditions with their target units. The flat sequential spine is then surgically trimmed so cross-branch edges don't contaminate independent branches.
4. **Cross-reference overrides** — resolved cross-refs from L5 (e.g. "refer to Step 3.2") create explicit jump edges.
5. **Converging gateways (Phase 5a)** — after diverging flows are mapped, the layer identifies join points where multiple edges target the same node. It automatically inserts XOR converging gateways to maintain BPMN structural validity.
6. **Exception boundary edges** — each `BOUNDARY_EVENT` gets an edge to `END_EVENT`.

| Gateway block | LLM output |
|---|---|
| `"Is the leave balance sufficient? If yes, proceed to approval. If no, notify employee of rejection."` | `{gateway_type: "XOR", branches: [{condition_label: "Yes", target_unit_id: "u_4a1", condition_var: "V_bal_ok", condition_value: "true"}, {condition_label: "No", target_unit_id: "u_5b2", is_default: true, condition_var: "V_bal_ok", condition_value: "false"}]}` |

> Gate: start event must have at least one outgoing edge; soft-fails if dead-end nodes ≥ 10%.

---

### L9 — DAG Resolver

Graph-level validation and cleanup using NetworkX:

1. **Reachability** — BFS from `START_EVENT`; any node not reached is flagged `unreachable_from_start` and marked for review. `BOUNDARY_EVENT` nodes are exempt from this check as they are attached via exception semantics, not sequence flows.
2. **Cycle detection** — back-edges are labelled `[loop-back]` and flagged for review; self-loops become `SUBPROCESS` nodes.
3. **Gateway shape check** — a gateway with ≤ 1 outgoing edge is flagged (it should split the flow).
4. **Lane assignment** — actors are mapped to swim-lane slugs for the L10 XML serialiser.
5. **Edge deduplication** — identical `(source, target, label)` triples are collapsed.

> Gate: fails if unreachable (non-exempt) node rate ≥ 15%; fails on dangling edge references.

---

### L10 — BPMN Translator

Serialises each `ProcessModel` to a standards-compliant BPMN 2.0 XML file using `lxml`.

- **Layout** — node positions are computed via longest-path depth ranking (topological sort, or BFS fallback for cyclic graphs). Nodes in the same depth column are sorted by actor so swim-lane rows are coherent.
- **XML structure** — produces `bpmn:definitions` with `bpmn:process`, swim-lane `bpmn:laneSet`, all elements, and `bpmn:sequenceFlow`s.
- **Condition Expressions** — for edges originating from gateways, the translator emits `bpmn:conditionExpression` elements containing the logic (e.g., `${V_status} == 'approved'`) established in L8.
- **Data Flow (DataObjects)** — serializes the process variable graph into BPMN `dataObject` and `dataObjectReference` elements, with `dataInputAssociation` and `dataOutputAssociation` linking them to the relevant tasks.
- **Output files** — one `.bpmn` file per process, named `{job_id}_{process_id}_{process_name}.bpmn`, plus a `_report.json` summarising node/edge counts, LLM call statistics, and any review flags.

| Output file | Contents |
|---|---|
| `f9d33b37_onboarding.bpmn` | Valid BPMN 2.0 XML with DataObjects, condition expressions, and layout waypoints. |
| `f9d33b37_report.json` | `{sop_count: 2, node_count_total: 43, edge_count_total: 51, review_flags: [...], llm_call_log: [...]}` |

> Gate: fails if any output file is missing or empty; fails if any node is missing layout coordinates.

---

## Capabilities

- **Multi-format input** — PDF, DOCX, and legacy `.doc` files (auto-converted via LibreOffice).
- **Multi-process documents** — a single document produces multiple independent BPMN diagrams, one per logical SOP.
- **Hybrid classification** — heuristic rules handle ~70% of blocks without an LLM call; the LLM is invoked only for genuinely ambiguous content.
- **Data-flow visualization** — variable producer/consumer relationships (DataVars) are visualized as BPMN DataObjects with explicit associations to the tasks that use or create them.
- **Automatic join resolution** — inserts XOR converging gateways at flow merge points to ensure diagrams follow standard BPMN structural rules.
- **Actor swim lanes** — resolved actors are mapped to BPMN swim lanes in the output XML.
- **LLM result caching** — repeated identical prompts (same template + input) are served from a local cache, making re-runs fast and cost-free.
- **Graceful degradation** — every LLM call has a structural fallback so the pipeline produces output even when an LLM step fails.
- **Review flags** — nodes and blocks that couldn't be confidently resolved are marked `needs_review` with a reason, surfaced in the report JSON.
- **Gate-guarded layers** — each layer validates its own output before passing control to the next; ambiguous soft failures are captured as warnings rather than hard stops where possible.
- **Standards-compliant output** — BPMN 2.0 XML with namespace-correct `bpmndi` diagram interchange and `conditionExpression` logic.
