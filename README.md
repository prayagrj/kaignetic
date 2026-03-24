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
PDF/DOCX  →  L1  →  L2  →  L3  →  L3b  →  L4  →  L5  →  L6  →  L7  →  L8  →  L9  →  L10  →  .bpmn
```

---

## Core Data Models

Understanding the data models is essential to following the pipeline. The central unit of work changed from a flat `Block` to a richer `StructuredChunk` in the current architecture.

### StructuredChunk

The primary processing unit. A chunk represents one **logical section** of the document — it carries the full heading breadcrumb and all content elements belonging to that section.

```
StructuredChunk
├── chunk_id                    unique ID
├── headings                    ["5. Pre-Joining", "5.1 Offer & Documentation"]
├── contextualized              "Section: 5. Pre-Joining > 5.1 Offer...\n\n<all text>"
├── elements: [ChunkElement]    atoms from Docling (paragraph, table, list item, figure)
├── chunk_type                  STEP | DECISION | EXCEPTION | ACTOR | CONDITION | NOTE | META
├── resolved_actor              canonical actor (set by L5)
├── condition_scope             governing condition in scope (set by L5)
├── cross_refs: [CrossRef]      resolved/unresolved references to other chunks
└── atomic_units: [AtomicUnit]  decomposed actions (set by L6)
```

The `contextualized` field bundles the heading path with the full text — every LLM call downstream receives this field rather than raw text, so no LLM call ever sees a chunk without its section context.

### ChunkElement

The atom within a chunk. Each Docling element (paragraph, list item, table, figure, code block) becomes one `ChunkElement`. L3 classifies at this level, not at the chunk level.

```
ChunkElement
├── element_id          from Docling's self_ref
├── element_type        PARAGRAPH | TABLE | LIST_ITEM | FIGURE | CODE
├── text
├── page_no
├── block_type          STEP | ACTOR | NOTE | HEADER | META | UNKNOWN  (set by L3)
└── block_type_confidence
```

### AtomicUnit

The smallest independently schedulable action. L6 decomposes each `StructuredChunk` into one or more of these.

```
AtomicUnit
├── unit_id
├── chunk_id            parent chunk
├── sequence_in_chunk   ordering within chunk
├── action              full imperative sentence
├── actor               canonical actor name
├── step_type           "SIMPLE" | "CONDITIONAL" | "DECISION"
├── condition           governing condition (if CONDITIONAL)
├── output              optional output description
├── is_start / is_terminal
├── inputs: [str]       variable names consumed  (e.g. ["V_request_id"])
└── outputs: [str]      variable names produced  (e.g. ["V_approved"])
```

### DataVar

A named process variable that flows between units. Built inside L8 from the `inputs`/`outputs` declared by every `AtomicUnit`.

```
DataVar
├── name                "V_approved" (V_-prefixed snake_case)
├── var_type            "bool" | "data" | "id" | "count" | "unknown"
├── producer_unit_id    unit that writes this variable
└── consumers: [str]    unit IDs that read this variable
```

### BPMNNode / BPMNEdge

```
BPMNNode
├── node_id, label, actor
├── bpmn_type           START_EVENT | END_EVENT | TASK | GATEWAY | BOUNDARY_EVENT | SUBPROCESS
├── gateway_type        EXCLUSIVE | PARALLEL | EVENT_BASED  (if GATEWAY)
├── gateway_direction   "DIVERGING" | "CONVERGING"          (if GATEWAY)
└── x, y, width, height                                     (set by L10)

BPMNEdge
├── edge_id, source_node_id, target_node_id, label
├── edge_type           "SEQUENCE_FLOW" | "DATA_FLOW"
├── is_default
├── condition_variable  e.g. "V_approved"
└── condition_value     e.g. "true"
```

---

## Pipeline Layers

### L1 — Document Extraction

Converts the source file to clean Markdown using [Docling](https://github.com/DS4SD/docling), which also returns a structured `DoclingDocument` object (JSON). `.doc` files are pre-converted to `.docx` via LibreOffice. Excess blank lines are collapsed.

| Input | Output |
|---|---|
| `employee_onboarding.pdf` | `job.extraction = {markdown: "...", docling_document: {...}}` |

> Gate: fails if extracted Markdown is shorter than 100 characters.

---

### L2 — Segmentation

Converts the `DoclingDocument` into a flat list of `StructuredChunk` objects. This is the first major departure from the old block-based design.

**How it works:**

The `chunk_builder` walks `doc.iterate_items()` maintaining a `section_stack`. When a `SectionHeaderItem` is encountered the current chunk is flushed and a new one starts under the new heading. All Docling element types (paragraphs, tables, list items, figures, code blocks) are accumulated as `ChunkElement` objects into the current chunk. After flushing, each chunk gets a `contextualized` string:

```
"Section: 5. Pre-Joining > 5.1 Offer & Documentation\n\n<all element text joined>"
```

The document domain (HR, IT, Finance, etc.) is detected from keyword frequency and stored as `job.sop_class`.

**Old vs. new:** Previously, this layer produced a flat list of typed `Block` objects — one per line/paragraph — with a parent-child ID graph. Chunks are coarser: one per section, carrying full heading context. This makes every downstream LLM prompt self-contained.

| Input | Output |
|---|---|
| `DoclingDocument` | `job.chunks = [StructuredChunk(chunk_id="a1f3", headings=["5. Pre-Joining", "5.1 Offer Docs"], elements=[...]), ...]` |

> Gate: fails if the chunk list is empty or contains no content chunks.

---

### L3 — Element Classifier

Labels every `ChunkElement` inside each chunk, then derives a chunk-level type via **majority vote**.

**Two passes in order:**

1. **LLM call per chunk** — sends element_id, element text, section heading, and SOP class in a batch. Returns `block_type` (STEP | ACTOR | NOTE | HEADER | META | UNKNOWN) and a confidence score for each element. If a chunk exceeds the token budget it is split at element boundaries before calling.

2. **Majority vote** — ignores NOTE / META / HEADER / UNKNOWN elements. The most frequent remaining type wins as `chunk.chunk_type`. Ties favour STEP over DECISION over EXCEPTION.

This is intentionally coarse — fine-grained action/condition decomposition happens in L6. The goal here is to identify which chunks are executable (STEP/DECISION/EXCEPTION) vs. informational (ACTOR/META/NOTE).

| Input | Output |
|---|---|
| `ChunkElement(text="Submit the leave form")` | `element.block_type = STEP, confidence = 0.97` |
| `ChunkElement(text="If the employee has been with the company for < 6 months…")` | `element.block_type = STEP, confidence = 0.89` (coarse pass; DECISION inferred later in L6) |

> Gate: fails if no chunks receive a STEP classification; soft-fails if UNKNOWN rate ≥ 20%.

---

### L3b — Process Splitter

A single SOP document often contains several independent sub-processes. This layer discovers them so each gets its own BPMN diagram.

**Phase 1 (structural):** Groups chunks by top-two-level heading key. Sections whose chunks are exclusively NOTE/META/ACTOR/CONDITION are classified as `preamble` (non-executable). The rest are `process candidates`.

**Phase 2 (LLM):** The surviving sections are described as a compact outline (heading key + element block-type counts) and sent to the LLM, which semantically groups adjacent sections into coherent process names.

| Input (outline sent to LLM) | Output |
|---|---|
| `[{"heading_key": "3. Joining Formalities", "block_counts": {"STEP": 8, "DECISION": 2}}, {"heading_key": "4. IT Access Setup", "block_counts": {"STEP": 5}}]` | `[{process_name: "Employee Joining & IT Onboarding", heading_keys: ["3. Joining Formalities", "4. IT Access Setup"]}]` |

Each group becomes a `ProcessModel` with its own chunks, nodes, edges, and output file. Preamble chunks are attached to all processes as shared context. All downstream layers (L5–L10) operate per process, not on the full document.

> Fallback: if the LLM call fails, each surviving section becomes its own process.

---

### L4 — Context Indexer

Builds a `ContextIndex` shared across all subsequent layers.

- **Section anchors** — one anchor per chunk (deepest heading text) plus de-numbered aliases (e.g. "5.1 Offer Docs" → alias "Offer Docs"). Used by L5 to resolve inline cross-references.
- **Actor registry** — candidate role names are collected from ACTOR chunks and inline NER on STEP/DECISION chunks; deduplicated and canonicalised by the LLM (e.g. "HR Exec", "HR Executive", "HR" → `"HR Executive"`).
- **Glossary** — if a definitions section exists, the LLM extracts term–definition pairs.
- **Exception chunk IDs** — list of chunks classified EXCEPTION, consumed by L7 to create boundary event nodes.

| Input | Output |
|---|---|
| `["HR Executive", "HR Exec", "hr", "Manager", "Reporting Manager"]` | `ActorRegistry: [Actor(canonical="HR Executive", aliases=["HR Exec", "hr"]), Actor(canonical="Reporting Manager", aliases=["Manager"])]` |

> Gate: fails if no actors can be identified.

---

### L5 — Chunk Enrichment

Walks every STEP, DECISION, and EXCEPTION chunk in document order and attaches three pieces of metadata.

**Pass 1 (structural traversal):**
- Maintains an `actor_stack` — when a HEADER chunk's text matches a canonical actor name, that actor is pushed onto the stack. All subsequent chunks in that section inherit it.
- `condition_scope` — the governing CONDITION chunk in scope is attached (e.g. "If employee is on probation").
- Cross-references — inline phrases like "refer to Section 4.2" are matched against section anchors via regex. Unresolved ones are flagged.

**Pass 2 (LLM enrichment):**
- Chunks where the actor is still ambiguous (pronoun references, overlapping scopes) are batched and sent to the LLM with the actor registry and heading context.
- Unresolved cross-references are sent for anchor lookup.

| Input chunk | After enrichment |
|---|---|
| `raw_text: "They must submit the ID proof"` | `resolved_actor: "HR Executive", pronoun_resolution: {original: "They", resolved_to: "HR Executive"}` |

> Gate: soft-fails if > 15% of target chunks have no resolved actor.

---

### L6 — Chunk Atomizer

Decomposes each executable `StructuredChunk` into one or more `AtomicUnit` objects. This layer does the heavy lifting that the old block atomizer did, but now also extracts **process variables** (inputs/outputs) for each unit.

**Process:**

1. For each process, collect chunks where `chunk_type == STEP`.
2. Group by top-level heading for coherent LLM calls; batch to respect token budget.
3. For each batch, the LLM receives:
   - Valid actor names from the registry
   - Preamble context (scope, pre-conditions)
   - A rolling **variable registry** (`known_vars`) — variables produced so far, carried across batches
   - Context chunks (adjacent, read-only for coherence)
   - Target chunks (to atomize)

4. The LLM returns one entry per target chunk:

```json
{
  "block_id": "chunk_id",
  "atomic_units": [
    {
      "sequence_in_block": 0,
      "step_type": "SIMPLE | CONDITIONAL | DECISION",
      "action": "full imperative sentence",
      "actor": "canonical actor",
      "condition": "optional governing condition",
      "output": "optional output description",
      "is_terminal": false,
      "inputs": ["V_request_id"],
      "outputs": ["V_approved"]
    }
  ]
}
```

The `inputs`/`outputs` arrays are the key addition over the old atomizer. They enable L8 to build a data-flow graph rather than relying purely on document order. The rolling `known_vars` ensures variable names stay consistent across batches.

| Chunk text | Atomic units produced |
|---|---|
| *"Verify the document. If valid, stamp and return; otherwise reject."* | `[{action: "Verify document", step_type: "DECISION", outputs: ["V_doc_valid"]}, {action: "Stamp and return", step_type: "CONDITIONAL", condition: "V_doc_valid is true", inputs: ["V_doc_valid"]}, {action: "Reject document", step_type: "CONDITIONAL", condition: "V_doc_valid is false", inputs: ["V_doc_valid"], is_terminal: true}]` |

> Gate: fails if any target chunk yields zero atomic units, or if no start/terminal unit is identified per process.

---

### L7 — Node Detector

Maps every `AtomicUnit` to a `BPMNNode` with the correct type.

- `unit.step_type == "DECISION"` → `GATEWAY` node (gateway type and direction are set later in L8)
- Everything else → `TASK` node
- One `START_EVENT` and one `END_EVENT` per process (bookends)
- Each EXCEPTION chunk from L4's exception registry → `BOUNDARY_EVENT` node

Labels are truncated to 80 characters. Note that DECISION detection now comes from the atomizer's `step_type` field (set in L6), not from the chunk's top-level classification (set in L3) — this is more accurate because L6 sees the full action sentence in context.

| AtomicUnit | BPMNNode |
|---|---|
| `{action: "Submit leave form", actor: "Employee", step_type: "SIMPLE"}` | `{bpmn_type: TASK, label: "Submit leave form", actor: "Employee"}` |
| `{action: "Is leave balance sufficient?", step_type: "DECISION"}` | `{bpmn_type: GATEWAY, gateway_type: null}` (type set in L8) |

> Gate: exactly one `START_EVENT` and one `END_EVENT` required; all nodes must have a label.

---

### L8 — Edge Detector

The most complex layer. Builds the complete directed edge set for each process using a multi-phase approach that combines variable-flow data dependencies, LLM-based gateway inference, and structural repair passes.

**Sub-stage 0 — Variable Linker (internal):**

Before building any edges, L8 runs the variable linker internally (previously a separate L6b layer). For each `AtomicUnit`'s declared `inputs` and `outputs`:
- Creates `DataVar` records, inferring type from name patterns (e.g. `V_is_*` → bool, `V_*_id` → id)
- Registers producer_unit_id and consumer lists
- Flags inputs with no known producer for review
- Stores results in `process.data_vars`

**Phase 1 — Variable-flow DAG:**

For every `DataVar` that has both a producer and at least one consumer:
- Create a `DATA_FLOW` edge from the producer node to each consumer node
- These edges represent actual information dependencies, not document proximity

**Phase 2 — Sequential fallback:**

For each consecutive (node_i, node_{i+1}) pair in document order:
- If node_i has **no outgoing data-flow edges**, add a `SEQUENCE_FLOW` edge to node_{i+1}
- This avoids adding cross-branch spine edges for nodes that are already wired by variables

The combination of phases 1+2 gives a hybrid DAG: data-driven where information is explicit, document-order where it isn't.

**Phase 3 — Gateway branch inference (LLM):**

For each node identified as a GATEWAY (from L7):
1. Tag node as `gateway_direction = "DIVERGING"`
2. Call LLM (`INFER_SINGLE_GATEWAY`) with:
   - The gateway's block text and heading context
   - Preceding units (up to N for context)
   - Following units (branch candidates)
   - `known_vars` dict (variable name → type)
3. LLM returns:

```json
{
  "gateway_type": "EXCLUSIVE | PARALLEL | EVENT_BASED",
  "gateway_label": "display text",
  "branches": [
    {
      "label": "Yes — sufficient balance",
      "condition_var": "V_bal_ok",
      "condition_value": "true",
      "target_unit_id": "u_4a1",
      "is_default": false
    },
    {
      "label": "No",
      "condition_var": "V_bal_ok",
      "condition_value": "false",
      "target_unit_id": "u_5b2",
      "is_default": true
    }
  ]
}
```

4. Branch edges are created from the gateway to each target unit.
5. If no branch reaches the natural next document node, an implicit `"otherwise"` default branch is added.
6. Stale cross-branch sequential edges (flat document-order spine connections between branch targets) are surgically removed.

| Gateway block | LLM output |
|---|---|
| `"Is the leave balance sufficient? If yes, proceed to approval. If no, notify employee."` | `{gateway_type: "XOR", branches: [{label: "Yes", target: "u_4a1", condition_var: "V_bal_ok", condition_value: "true"}, {label: "No", target: "u_5b2", is_default: true}]}` |

**Phase 4 — Cross-reference overrides:**

Resolved cross-references from L5 (e.g. "refer to Step 3.2") create explicit jump edges labelled with the reference text.

**Phase 5a — Converging gateway insertion:**

After diverging flows are mapped, L8 scans for join points — nodes with ≥ 2 incoming edges from different upstream sources that aren't already handled by a single diverging gateway. At each such node, an XOR converging gateway is automatically inserted between the predecessors and the join node, and tagged with `gateway_direction = "CONVERGING"`. This step ensures the graph is structurally valid BPMN without requiring manual intervention.

**Phase 5b — Exception boundary edges:**

Each `BOUNDARY_EVENT` node gets an outgoing edge to `END_EVENT`.

**Phase 6 — Prune trivial gateways:**

Any gateway with exactly one incoming and one outgoing edge is not a true branch point. It is bypassed: the predecessor connects directly to the successor. The node is flagged `needs_review`.

**Phase 7 — Reconnect isolated nodes:**

A BFS from `START_EVENT` identifies nodes not reachable from the start (excluding `BOUNDARY_EVENT` nodes, which are attached via exception semantics). For each isolated node the LLM is asked to suggest a `connect_from` and `connect_to` based on surrounding context. Suggestions with confidence ≥ 0.3 are applied.

> Gate: start event must have at least one outgoing edge; soft-fails if dead-end nodes ≥ 10%.

---

### L9 — DAG Resolver

Graph-level validation and cleanup using NetworkX.

1. **Reachability** — BFS from `START_EVENT`; unreachable non-exempt nodes are flagged `unreachable_from_start`. `BOUNDARY_EVENT` nodes are exempt — they are wired via exception semantics, not sequence flow.
2. **Cycle detection** — `networkx.simple_cycles()` identifies back-edges, which are labelled `[loop-back]` and flagged for review. Self-loops become `SUBPROCESS` nodes.
3. **Gateway shape check** — a diverging gateway with ≤ 1 outgoing edge is flagged (it should split the flow). Converging gateways are exempt from this check.
4. **Lane assignment** — canonical actor names are mapped to swim-lane slugs (lowercase with underscores) and stored for L10 consumption.
5. **Edge deduplication** — identical `(source, target, label)` triples are collapsed to one edge.

> Gate: fails if unreachable (non-exempt) node rate ≥ 15%; fails on dangling edge references.

---

### L10 — BPMN Translator

Serialises each `ProcessModel` to a standards-compliant BPMN 2.0 XML file using `lxml`. This layer changed significantly from the previous version — it now outputs full BPMN 2.0 Collaboration structures with swimlanes, condition expressions, and data objects.

**Layout computation:**

Nodes are assigned x-positions via iterative longest-path depth ranking (topological sort, with BFS fallback for cyclic graphs). Y-positions are lane-aware:
- Actors are sorted; each gets a lane band with height proportional to its node count
- Within a lane band, nodes at the same depth column are stacked vertically
- Nodes without an actor (START_EVENT, END_EVENT, converging gateways) are positioned by averaging their neighbours' y-coordinates

**Collaboration + Swimlane structure:**

When actors exist, the output uses the full BPMN 2.0 collaboration structure:

```xml
<collaboration id="collab_...">
  <participant id="participant_..." processRef="process_..." />
</collaboration>

<process id="process_...">
  <laneSet>
    <lane id="lane_hr_executive" name="HR Executive">
      <flowNodeRef>task_001</flowNodeRef>
      ...
    </lane>
    <lane id="lane_employee" name="Employee">
      ...
    </lane>
  </laneSet>
  ...
</process>
```

**Element serialization:**

| BPMN type | XML element |
|---|---|
| START_EVENT | `<startEvent>` |
| TASK | `<userTask>` |
| GATEWAY (EXCLUSIVE) | `<exclusiveGateway>` |
| GATEWAY (PARALLEL) | `<parallelGateway>` |
| GATEWAY (EVENT_BASED) | `<eventBasedGateway>` |
| BOUNDARY_EVENT | `<boundaryEvent>` |
| SUBPROCESS | `<subProcess>` |
| END_EVENT | `<endEvent>` |

**Condition expressions:**

Sequence flows from EXCLUSIVE gateways carry condition expressions for all non-default branches:

```xml
<sequenceFlow id="sf_001" sourceRef="gw_001" targetRef="task_002" name="Yes">
  <conditionExpression>${V_approved} == true</conditionExpression>
</sequenceFlow>
```

Default branches use `isDefault="true"` and carry no `conditionExpression` (per BPMN 2.0 spec).

**Data objects:**

Process variables from `process.data_vars` are emitted as `<dataObject>` and `<dataObjectReference>` elements inside the process. These are rendered in the diagram as data objects linked to their producer and consumer tasks.

**Diagram interchange (DI):**

Every element gets a `BPMNShape` with computed bounds. Every edge gets a `BPMNEdge` with two-point waypoints (or quad-points when an edge crosses lane boundaries). The `BPMNPlane` references the collaboration (if lanes exist) or the process directly (no-lane fallback).

**Output files:**

| File | Contents |
|---|---|
| `{job_id}_{process_id}_{name}.bpmn` | BPMN 2.0 XML with swimlanes, condition expressions, data objects, and layout waypoints |
| `{job_id}_report.json` | `{sop_count, node_count_total, edge_count_total, gateway_types, review_flags, llm_call_log}` |

> Gate: fails if any output file is missing or empty; fails if any node is missing layout coordinates.

---

## Capabilities

- **Multi-format input** — PDF, DOCX, and legacy `.doc` files (auto-converted via LibreOffice).
- **Multi-process documents** — a single document produces multiple independent BPMN diagrams, one per logical SOP discovered by L3b.
- **Chunk-based processing** — document sections are processed as self-contained `StructuredChunk` objects carrying full heading context, eliminating the need for fine-grained block trees and making every LLM call self-sufficient.
- **Variable-flow DAG** — L6 extracts named process variables (`inputs`/`outputs`) from each atomic action; L8 uses these to build data-dependency edges rather than relying on document order alone.
- **Per-gateway LLM inference** — one targeted LLM call per decision point determines the gateway type (EXCLUSIVE / PARALLEL / EVENT_BASED) and branch conditions, keeping prompts small and precise.
- **Automatic join resolution** — L8 inserts XOR converging gateways at flow merge points to maintain BPMN structural validity without manual annotation.
- **Isolated node repair** — L8 detects nodes unreachable from the start and uses the LLM to reconnect them based on surrounding context.
- **Actor swim lanes** — resolved actors are mapped to BPMN 2.0 `<lane>` elements inside a full `<collaboration>` structure.
- **Condition expressions** — gateway branch conditions are serialised as `<conditionExpression>` elements (e.g. `${V_approved} == true`) in the output XML.
- **Data objects** — process variables (DataVars) are serialised as BPMN `<dataObject>` elements and rendered in the diagram.
- **LLM result caching** — identical prompts (same template + input hash) are served from a local disk cache, making re-runs fast and cost-free.
- **Graceful degradation** — every LLM call has a structural fallback so the pipeline produces output even when an LLM step fails.
- **Review flags** — nodes and chunks that couldn't be confidently resolved are marked `needs_review` with a reason, surfaced in the report JSON.
- **Gate-guarded layers** — each layer validates its own output before passing control to the next; soft failures are captured as warnings rather than hard stops where possible.
- **Standards-compliant output** — BPMN 2.0 XML with namespace-correct `bpmndi` diagram interchange, `conditionExpression` logic, and proper collaboration/participant/lane structure.
