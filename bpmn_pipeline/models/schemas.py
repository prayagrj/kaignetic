from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class BlockType(str, Enum):
    """Semantic classification for a chunk — what role it plays in the SOP."""
    STEP = "STEP"
    DECISION = "DECISION"
    EXCEPTION = "EXCEPTION"
    ACTOR = "ACTOR"
    CONDITION = "CONDITION"
    NOTE = "NOTE"
    META = "META"        # scope, purpose, definitions, informational text (from simplified L3)
    HEADER = "HEADER"
    UNKNOWN = "UNKNOWN"


class ElementType(str, Enum):
    """Low-level docling element type within a chunk."""
    PARAGRAPH = "paragraph"
    TABLE = "table"
    LIST_ITEM = "list_item"
    FIGURE = "figure"
    CODE = "code"


class BPMNNodeType(str, Enum):
    START_EVENT = "START_EVENT"
    END_EVENT = "END_EVENT"
    TASK = "TASK"
    GATEWAY = "GATEWAY"
    BOUNDARY_EVENT = "BOUNDARY_EVENT"
    SUBPROCESS = "SUBPROCESS"


class GatewayType(str, Enum):
    EXCLUSIVE = "EXCLUSIVE"      # exactly one branch is taken
    PARALLEL = "PARALLEL"        # all branches run simultaneously
    EVENT_BASED = "EVENT_BASED"  # next branch determined by which event arrives first


class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass
class CrossRef:
    ref_text: str
    resolved_chunk_id: Optional[str] = None
    resolution_method: str = "unresolved"  # structural_anchor | llm | unresolved


@dataclass
class ChunkElement:
    """
    One typed item inside a StructuredChunk — the atom of Docling's output.
    metadata holds element-specific extras (table dims, list marker, etc.).
    """
    element_id: str          # Docling self_ref or generated id
    element_type: ElementType
    text: Optional[str] = None
    page_no: Optional[int] = None
    metadata: dict = field(default_factory=dict)

    # ── L3: Classification (element-level) ────────────────────────────────
    block_type: Optional[BlockType] = None
    block_type_confidence: float = 0.0


@dataclass
class StructuredChunk:
    """
    Primary processing unit — one logical section of the document.

    Built from DoclingDocument.iterate_items() grouped by heading boundaries.
    Everything downstream (classification, enrichment, atomization) operates
    at this level. Fine-grained tree navigation is not needed — the chunk
    already carries its full heading breadcrumb + all content.
    """
    chunk_id: str
    job_id: str

    # ── Structure from Docling ─────────────────────────────────────────────
    headings: list = field(default_factory=list)      # ["5. Pre-Joining", "5.1 Offer Docs"]
    contextualized: str = ""                           # headings breadcrumb + all text — primary LLM input
    elements: list = field(default_factory=list)       # list[ChunkElement]
    page_numbers: list = field(default_factory=list)   # deduplicated page numbers spanned

    # ── L3: Classification (derived from majority element block_type) ────────
    chunk_type: Optional[BlockType] = None
    chunk_type_confidence: float = 0.0
    chunk_type_method: str = ""                        # llm | fallback

    # ── L5: Enrichment ────────────────────────────────────────────────────
    resolved_actor: Optional[str] = None
    condition_scope: Optional[str] = None
    cross_refs: list = field(default_factory=list)     # list[CrossRef]

    # ── L6: Atomization ───────────────────────────────────────────────────
    atomic_units: list = field(default_factory=list)   # list[AtomicUnit]

    # ── Review flags ──────────────────────────────────────────────────────
    needs_review: bool = False
    review_reasons: list = field(default_factory=list)


@dataclass
class AtomicUnit:
    unit_id: str
    chunk_id: str           # parent StructuredChunk
    sequence_in_chunk: int
    action: str
    actor: str
    step_type: str = "SIMPLE"  # SIMPLE | CONDITIONAL | DECISION (set by L6 atomizer)
    condition: Optional[str] = None
    output: Optional[str] = None
    is_terminal: bool = False
    is_start: bool = False
    inputs: list = field(default_factory=list)    # variable names consumed
    outputs: list = field(default_factory=list)   # variable names produced


@dataclass
class BPMNNode:
    node_id: str
    job_id: str
    unit_id: Optional[str] = None
    bpmn_type: Optional[BPMNNodeType] = None
    label: str = ""
    actor: Optional[str] = None
    gateway_type: Optional[GatewayType] = None
    gateway_direction: Optional[str] = None  # "DIVERGING" | "CONVERGING" (set by L8)
    x: Optional[float] = None
    y: Optional[float] = None
    width: Optional[float] = None
    height: Optional[float] = None
    needs_review: bool = False
    review_reasons: list = field(default_factory=list)
    unreachable_from_start: bool = False


@dataclass
class BPMNEdge:
    edge_id: str
    job_id: str
    source_node_id: str
    target_node_id: str
    label: Optional[str] = None
    is_default: bool = False
    edge_type: str = "SEQUENCE_FLOW"
    condition_variable: Optional[str] = None
    condition_value: Optional[str] = None


@dataclass
class Actor:
    canonical_name: str
    aliases: list = field(default_factory=list)
    source_method: str = "structural_extraction"


@dataclass
class ActorRegistry:
    job_id: str
    actors: list = field(default_factory=list)

    def canonical_names(self) -> list:
        return [a.canonical_name for a in self.actors]

    def find_canonical(self, name: str) -> Optional[str]:
        name_lower = name.lower()
        for actor in self.actors:
            if actor.canonical_name.lower() == name_lower:
                return actor.canonical_name
            if any(a.lower() == name_lower for a in actor.aliases):
                return actor.canonical_name
        return None


@dataclass
class SectionAnchor:
    anchor_text: str
    chunk_id: str
    heading_path: list = field(default_factory=list)


@dataclass
class GlossaryEntry:
    term: str
    definition: str
    chunk_id: str
    definition_method: str = "llm"


@dataclass
class DataVar:
    """Named variable flowing through the process (produced by one unit, consumed by others)."""
    name: str
    var_type: str = "unknown"  # bool | data | id | count | unknown
    producer_unit_id: Optional[str] = None
    consumers: list = field(default_factory=list)


@dataclass
class ContextIndex:
    job_id: str
    section_anchors: list = field(default_factory=list)   # list[SectionAnchor]
    glossary: list = field(default_factory=list)           # list[GlossaryEntry]
    exception_chunks: list = field(default_factory=list)   # chunk_ids of EXCEPTION chunks
    actor_registry: Optional[ActorRegistry] = None


@dataclass
class JobError:
    layer: int
    error_code: str
    message: str
    traceback: Optional[str] = None


@dataclass
class ReviewFlag:
    layer: int
    reason: str
    chunk_id: Optional[str] = None


@dataclass
class LLMCallRecord:
    layer: int
    prompt_template: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0
    cached: bool = False


@dataclass
class ProcessModel:
    process_id: str
    name: str
    chunks: list = field(default_factory=list)        # list[StructuredChunk] — executable chunks
    atomic_units: list = field(default_factory=list)
    data_vars: list = field(default_factory=list)
    bpmn_nodes: list = field(default_factory=list)
    bpmn_edges: list = field(default_factory=list)
    preamble: list = field(default_factory=list)       # list[StructuredChunk] — context-only chunks


@dataclass
class Job:
    job_id: str
    source_file_path: str
    status: JobStatus = JobStatus.PENDING
    current_layer: Optional[int] = None
    error: Optional[JobError] = None
    sop_class: str = "GENERIC_PROCESS"
    created_at: str = ""
    updated_at: str = ""
    layer_timestamps: dict = field(default_factory=dict)
    review_flags: list = field(default_factory=list)
    llm_call_log: list = field(default_factory=list)

    # Primary data — StructuredChunks replace both blocks and document_tree
    chunks: list = field(default_factory=list)         # list[StructuredChunk]
    context_index: Optional[ContextIndex] = None
    processes: list = field(default_factory=list)
    extraction: dict = field(default_factory=dict)     # markdown + docling_document dict
