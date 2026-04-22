# BPMN Pipeline

A document-to-BPMN converter that turns unstructured SOP documents (PDF, DOCX) into standards-compliant BPMN 2.0 process diagrams — one diagram per logical process discovered in the document.

---

## How It Works

The pipeline runs as an ordered sequence of eight layers (`L1` → `L8`). Each layer has a single responsibility; it reads from a shared `Job` object and writes back into it.

---

## Core Data Models

### StructuredChunk

The primary processing unit. Represents one logical section of the document.

```
StructuredChunk
├── chunk_id
├── headings                    ["5. Pre-Joining", "5.1 Offer & Documentation"]
├── contextualized              "Section: 5. Pre-Joining > 5.1 Offer...\n\n<all text>"
├── elements: [ChunkElement]    items from Docling (paragraph, table, list item, figure, code)
├── chunk_type                  STEP | META | HEADER
└── atomic_units: [AtomicUnit]  decomposed actions (set by L2)
```

### ChunkElement

One typed item within a chunk. Maps directly to a Docling element.

```
ChunkElement
├── element_id          from Docling's self_ref
├── element_type        PARAGRAPH | TABLE | LIST_ITEM | FIGURE | CODE
├── text
├── page_no
└── block_type          STEP | META | HEADER (set by L1)
```

### AtomicStepUnit / AtomicDecisionUnit

The smallest independently schedulable action produced by L2.

```
AtomicStepUnit
├── unit_id
├── chunk_id            parent chunk
├── action              full imperative sentence
├── actor               raw actor name (canonicalized by L3)
├── prev_step_ids       upstream unit IDs
├── next_step_ids       downstream unit IDs
└── is_terminal         true if this ends the process

AtomicDecisionUnit
├── unit_id, chunk_id, action, actor, prev_step_ids
└── branches: [DecisionBranch]
      ├── label
      ├── output_variables: [str]
      └── next_step_id
```

### BPMNNode / BPMNEdge

```
BPMNNode
├── node_id, label, actor
├── bpmn_type           START_EVENT | END_EVENT | TASK | GATEWAY | SUBPROCESS | BOUNDARY_EVENT
├── gateway_type        EXCLUSIVE | PARALLEL | EVENT_BASED  (if GATEWAY)
├── branches            outgoing branch labels (if GATEWAY)
├── needs_review        bool
└── x, y, width, height (set by L8)

BPMNEdge
├── edge_id, source_node_id, target_node_id, label
├── is_default
├── condition_variable
└── condition_value
```

---

## Pipeline Layers

### L1 — Document Extraction (`l1_extraction.py`)

Converts the source file to a structured format using [Docling](https://github.com/DS4SD/docling). `.doc` files are pre-converted to `.docx` via LibreOffice.

The `chunk_builder` walks `doc.iterate_items()` maintaining a heading stack. When a `SectionHeaderItem` is encountered the current chunk is flushed and a new one starts. All Docling element types (paragraphs, tables, list items, figures, code blocks) are accumulated as `ChunkElement` objects. Each chunk gets a `contextualized` string bundling its heading path with all element text.

Element `chunk_type` is assigned heuristically:
- `META` — preamble sections (scope, glossary, definitions)
- `STEP` — actionable content
- `HEADER` — section titles with no content

| Input | Output |
|---|---|
| `employee_onboarding.pdf` | `job.chunks = [StructuredChunk(...), ...]`; `job.processes[0]` seeded with STEP chunks |

---

### L2 — Chunk Atomizer (`l2_atomizer.py`)

Decomposes each STEP chunk into one or more `AtomicStepUnit` or `AtomicDecisionUnit` objects via LLM.

Chunks are batched by top-level heading (up to `L4_BATCH_SIZE=4` per call) with `L4_WINDOW_BLOCKS=2` context chunks before and after. The LLM prompt (`ATOMIZE_WITH_CONTEXT`) receives preamble context, context blocks, and target blocks. Local LLM-assigned IDs are remapped to globally unique UUIDs after parsing.

As a side effect, L2 builds `process.actor_heading_map` (raw actor name → section headings), which L3 consumes.

| Chunk text | Atomic units produced |
|---|---|
| *"Verify the document. If valid, stamp and return; otherwise reject."* | `[AtomicDecisionUnit(action="Verify document", branches=[{label:"Valid", ...}, {label:"Invalid", ...}])]` |

---

### L3 — Context (`l3_context.py`)

Canonicalizes actor names across all processes.

Collects all actor mentions from `actor_heading_map`, calls the LLM (`CANONICALIZE_ACTORS`), and receives back a list of `{canonical_name, aliases[]}` groupings. Builds an `ActorRegistry` and remaps every `unit.actor` value to the canonical name. If a unit has no actor, it inherits from the nearest predecessor.

| Input | Output |
|---|---|
| `["HR Executive", "HR Exec", "hr", "Manager", "Reporting Manager"]` | `ActorRegistry: [Actor(canonical="HR Executive", aliases=["HR Exec", "hr"]), Actor(canonical="Reporting Manager", aliases=["Manager"])]` |

---

### L4 — Node Detector (`l4_node_detector.py`)

Maps every atomic unit to a `BPMNNode`. No LLM calls.

- `AtomicDecisionUnit` → `GATEWAY` node
- `AtomicStepUnit` with `is_terminal=True` → `END_EVENT` node
- All other `AtomicStepUnit` → `TASK` node
- One `START_EVENT` node is added per process
- If no terminal unit is found, a fallback `END_EVENT` is injected

Stores a `unit_id → node_id` mapping on the process for use by L5.

---

### L5 — Edge Detector (`l5_edge_detector.py`)

The most complex layer. Builds the complete directed edge set using a multi-phase approach.

**Phase 1 — Explicit edges:**
From `unit.next_step_ids` and `branch.next_step_id` declared by the atomizer.

**Phase 2 — Sequential spine:**
For consecutive tasks within the same document section, add a `SEQUENCE_FLOW` edge as a fallback where no explicit edge exists.

**Phase 3 — Gateway wiring:**
For each `AtomicDecisionUnit`, replace its outgoing edges with labelled branch edges and mark the node as `EXCLUSIVE` gateway with `gateway_direction = "DIVERGING"`.

**Phase 4 — Converging gateway insertion:**
Auto-insert XOR merge gateways before any non-gateway node with ≥ 2 incoming edges from different sources.

**Phase 5 — Trivial gateway pruning:**
Remove gateways with exactly 1 incoming and 1 outgoing edge; replace with a direct bypass edge.

**Phase 6 — Isolated node reconnection:**
BFS from `START_EVENT` identifies unreachable nodes. The LLM (`RECONNECT_ISOLATED_NODES`) suggests `connect_from`/`connect_to` for each; suggestions with confidence ≥ 0.3 are applied.

---

### L6 — Process Splitter (`l6_process_splitter.py`)

Takes the single `ProcessModel` produced by L1–L5 and splits it into N independent processes, each of which gets its own BPMN file.

Uses connected-component analysis on the graph followed by merging/splitting decisions:

1. **Tiny-fragment absorption** — components with < 3 task nodes are merged into the nearest neighbour.
2. **Pair scoring** — adjacent component pairs are scored: shared actors (weight 0.45), heading similarity (0.35), document adjacency (0.20).
3. **Decision thresholds:**
   - Score ≥ 0.55 → auto-merge (no LLM)
   - Score < 0.20 → auto-split (no LLM)
   - 0.20–0.55 → LLM `PROCESS_SPLIT` call; merge if LLM confidence ≥ 0.55
4. **Assembly** — each final component becomes a new `ProcessModel` with fresh START/END events.

---

### L7 — DAG Resolver (`l7_dag_resolver.py`)

Graph-level validation and cleanup using NetworkX.

1. **Reachability** — BFS from `START_EVENT`; unreachable nodes are flagged `unreachable_from_start`. `BOUNDARY_EVENT` nodes are exempt.
2. **Cycle detection** — back-edges are labelled `[loop-back]` and flagged for review; self-loops become `SUBPROCESS` nodes.
3. **Gateway shape check** — a diverging gateway with ≤ 1 outgoing edge is flagged. Converging gateways are exempt.
4. **Lane assignment** — actor names are mapped to swim-lane slugs (lowercase with underscores) stored for L8.
5. **Edge deduplication** — identical `(source, target, label)` triples are collapsed to one edge.

---

### L8 — BPMN Translator (`l8_translator.py`)

Serializes each `ProcessModel` to a standards-compliant BPMN 2.0 XML file using `lxml`.

**Layout computation:**
Nodes are depth-ranked by longest-path (topological sort; BFS fallback for cyclic graphs). Each actor gets a lane band; nodes at the same depth column are stacked vertically within their lane. Nodes without an actor (START_EVENT, END_EVENT, converging gateways) are y-positioned by averaging their neighbours.

**Collaboration + Swimlane structure:**
```xml
<collaboration id="collab_...">
  <participant id="participant_..." processRef="process_..." />
</collaboration>
<process id="process_...">
  <laneSet>
    <lane id="lane_hr_executive" name="HR Executive">
      <flowNodeRef>task_001</flowNodeRef>
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

**Review annotations:**
Nodes flagged `needs_review` get an embedded `<textAnnotation>` element with the review reason.

**Output files:**

| File | Contents |
|---|---|
| `{job_id}_{process_id}_{name}.bpmn` | BPMN 2.0 XML with swimlanes, gateway conditions, and layout |
| `{job_id}_report.json` | node/edge counts, gateway types, review flags, LLM call log |

---

## Capabilities

- **Multi-format input** — PDF, DOCX, and legacy `.doc` files (auto-converted via LibreOffice).
- **Multi-process output** — a single document produces multiple independent BPMN diagrams, one per logical process discovered by L6.
- **Automatic join resolution** — L5 inserts XOR converging gateways at flow merge points automatically.
- **Isolated node repair** — L5 detects nodes unreachable from the start and uses the LLM to reconnect them.
- **Actor swim lanes** — resolved actors are mapped to BPMN 2.0 `<lane>` elements inside a full `<collaboration>` structure.
- **Graceful degradation** — every LLM call has a structural fallback so the pipeline produces output even when an LLM step fails.
- **Review flags** — nodes that couldn't be confidently resolved are marked `needs_review` and embedded as annotations in the output BPMN.
- **Standards-compliant output** — BPMN 2.0 XML with namespace-correct `bpmndi` diagram interchange, and proper collaboration/participant/lane structure.
