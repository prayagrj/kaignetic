# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

BPMN Pipeline converts unstructured SOP documents (PDF, DOCX, DOC) into BPMN 2.0 process diagrams. It produces swimlane-aware BPMN 2.0 XML with one file per discovered sub-process.

## Repository Layout

All source code lives under `bpmn_pipeline/`. Run commands from that directory or use paths relative to it.

```
kaignetic/
├── bpmn_pipeline/
│   ├── main.py              ← CLI entry point
│   ├── config.py            ← all configuration constants
│   ├── requirements.txt
│   ├── .env.example
│   ├── llm/
│   │   ├── client.py        ← LLMClient (Groq + Langfuse + disk cache)
│   │   └── prompts.py       ← all prompt templates
│   ├── models/
│   │   └── schemas.py       ← all dataclasses and enums
│   ├── pipeline/
│   │   ├── orchestrator.py  ← runs layers in sequence
│   │   └── layers/
│   │       ├── l1_extraction.py
│   │       ├── l2_atomizer.py
│   │       ├── l3_context.py
│   │       ├── l4_node_detector.py
│   │       ├── l5_edge_detector.py
│   │       ├── l6_process_splitter.py
│   │       ├── l7_dag_resolver.py
│   │       └── l8_translator.py
│   ├── pipeline/utils/
│   │   ├── chunk_builder.py
│   │   ├── chunker.py
│   │   ├── debug_utils.py
│   │   └── decision_patterns.py
│   ├── tests/
│   │   └── smoke_test.py
│   ├── jobs/                ← place input .pdf/.docx files here
│   └── outputs/             ← final .bpmn and report.json files
```

## Commands

```bash
# Run the pipeline on a document (from bpmn_pipeline/)
python main.py <path_to_pdf_or_docx>

# Run smoke/integration tests
pytest tests/smoke_test.py
```

**Environment setup:** Copy `.env.example` to `.env` and set `GROQ_API_KEY` and `GROQ_MODEL` (default `llama-3.3-70b-versatile`). Optionally set Langfuse keys for tracing.

## Architecture

The pipeline is a **sequential 8-layer orchestrator** (`pipeline/orchestrator.py`). Layers run in order; each reads from and writes back to a shared `Job` object. On any layer exception, `job.status` is set to `FAILED` and the pipeline halts.

### Layer Execution Order

| Exec Order | File | Responsibility |
|---|---|---|
| 1 | `l1_extraction.py` | PDF/DOCX → `StructuredChunk[]` via Docling; heuristic STEP/META/HEADER classification |
| 2 | `l2_atomizer.py` | LLM decomposes chunks into `AtomicStepUnit` / `AtomicDecisionUnit`; builds `actor_heading_map` |
| 3 | `l3_context.py` | LLM canonicalizes actor aliases; populates `ActorRegistry` in `ContextIndex`; remaps all unit actors |
| 4 | `l4_node_detector.py` | Maps atomic units to `BPMNNode` types (START, TASK, GATEWAY, END) — no LLM |
| 5 | `l5_edge_detector.py` | Builds `BPMNEdge` set: explicit links → sequential spine → gateway wiring → converging gateways → LLM reconnect |
| 6 | `l6_process_splitter.py` | Splits single graph into multiple `ProcessModel` objects via heuristic scoring + optional LLM |
| 7 | `l7_dag_resolver.py` | NetworkX validation: reachability, cycles, lane assignment, edge deduplication |
| 8 | `l8_translator.py` | Serializes to BPMN 2.0 XML with swimlane layout; writes `.bpmn` + `report.json` |

### Core Data Models (`models/schemas.py`)

- **`Job`** — top-level container passed through all layers
- **`StructuredChunk`** — a document section (heading breadcrumb + `list[ChunkElement]`); primary input to the atomizer
- **`AtomicStepUnit`** — a single sequential action with `actor`, `action`, `prev_step_ids`, `next_step_ids`, `is_terminal`
- **`AtomicDecisionUnit`** — a conditional fork with `actor`, `action`, `prev_step_ids`, and a list of `DecisionBranch` objects
- **`DecisionBranch`** — one outcome of a decision: `label`, `output_variables`, `next_step_id`
- **`BPMNNode`** / **`BPMNEdge`** — graph primitives; populated by L4/L5, validated by L7, serialized by L8
- **`ProcessModel`** — one per sub-process; holds chunks, atomic units, nodes, edges, preamble, and `actor_heading_map`
- **`ActorRegistry`** / **`ContextIndex`** — canonical actor registry built by L3

### Edge Detection Logic (L5)

L5 runs in phases:
1. **Explicit links** — edges from `unit.next_step_ids` / `branch.next_step_id`
2. **Sequential spine** — document-order `SEQUENCE_FLOW` edges between consecutive tasks in the same section
3. **Gateway wiring** — for each `AtomicDecisionUnit`, replace outgoing edges with labelled branches; mark gateway as `EXCLUSIVE`
4. **Converging gateway insertion** — auto-inserts XOR merge gateways before any non-gateway node with 2+ incoming edges
5. **Trivial gateway pruning** — removes 1-in/1-out gateways and replaces with direct bypass edges
6. **Isolated node reconnection** — LLM call (`RECONNECT_ISOLATED_NODES`) to reconnect nodes unreachable from START via BFS; suggestions accepted at confidence ≥ 0.3

### Process Splitting Logic (L6)

L6 takes a single `ProcessModel` (full graph from L5) and splits it into N separate processes:
1. **Tiny-fragment absorption** — merge components with <3 task nodes into nearest neighbour
2. **Pair scoring** — adjacent components scored on shared actors (0.45), heading similarity (0.35), document adjacency (0.20)
3. **Thresholds** — score ≥ 0.55 → auto-merge; score < 0.20 → auto-split; 0.20–0.55 → LLM `PROCESS_SPLIT` call (merge if confidence ≥ 0.55)
4. **Assembly** — each final component becomes a new `ProcessModel` with fresh START/END events

### LLM Client (`llm/client.py`)

All layers share a single `LLMClient.call()` method backed by LangChain's `ChatGroq`. Responses are cached to disk keyed by `sha256(LLM_CACHE_VERSION + GROQ_MODEL + prompt)`. Increment `LLM_CACHE_VERSION` in `config.py` to invalidate the cache. The client retries on rate limits (2 attempts, exponential backoff) and uses `json_repair` for malformed responses. All calls are traced as Langfuse generations on a per-job trace. Call `llm.flush()` at pipeline end to flush Langfuse events.

### Key Configuration (`config.py`)

- `LLM_CACHE_ENABLED` — toggle disk cache (default: true)
- `LLM_CACHE_VERSION` — bump to invalidate cached LLM responses (currently `3`)
- `LLM_MAX_INPUT_TOKENS=1800` — hard cap; warns if exceeded
- `L4_BATCH_SIZE=4` — chunks per atomizer batch
- `L4_WINDOW_BLOCKS=2` — context blocks before/after target batch
- `LLM_OVERLAP_ITEMS=1` — context carry-over between batches
- `OUTPUT_DIR` — where BPMN files and reports are written

### Outputs

- `outputs/{job_id}_{process_id}_{name}.bpmn` — BPMN 2.0 XML with swimlanes, gateway conditions, computed layout
- `outputs/{job_id}_report.json` — node/edge counts, gateway types, review flags, LLM call log

### Debug Output

`debug_utils.save_layer_state()` serializes `job` state to JSON after each layer:
`bpmn_pipeline/outputs/layer-wise-output/{job_id}/L{n}_{name}_{stage}.json`

## Testing Notes

`smoke_test.py` runs the full pipeline end-to-end. It asserts: pipeline status is COMPLETE or NEEDS_REVIEW; at least one `.bpmn` file produced; each BPMN has ≥1 startEvent and ≥1 endEvent; every task/gateway/endEvent has ≥1 incoming flow; every gateway has ≥2 outgoing flows. Set `SMOKE_TEST_FILE` env var to point at a specific input file, or drop files into `jobs/`.

## Known Quirks

- **Gate validation is disabled**: `validate_gate()` exists on all layer classes but is commented out in the orchestrator.
- **`decision_patterns.py`** (`DECISION_INLINE` regex) is defined but unused — decision detection is fully delegated to the LLM atomizer.
