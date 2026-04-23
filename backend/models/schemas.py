from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


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

    # ── Enrichment (formerly L4, now deprecated fields) ───────────────────

    # ── Atomization (set by L6) ───────────────────────────────────────────
    atomic_units: list = field(default_factory=list)   # list[AtomicStepUnit | AtomicDecisionUnit]

    # ── Review flags ──────────────────────────────────────────────────────
    needs_review: bool = False
    review_reasons: list = field(default_factory=list)


@dataclass
class DecisionBranch:
    """One outcome branch of a decision gateway."""
    label: str                                             # e.g. "Employee replied"
    output_variables: list = field(default_factory=list)  # descriptive labels for the edge (e.g. ["employee_replied"])
    next_step_id: Optional[str] = None                    # unit_id of first step on this branch (filled by L4, repaired by L6)


@dataclass
class AtomicStepUnit:
    """A sequential task — an actor performs a single action."""
    unit_id: str
    chunk_id: str           # parent StructuredChunk
    action: str             # close paraphrase of the source document text
    actor: str
    prev_step_ids: list = field(default_factory=list)   # list[str] — upstream unit_ids
    next_step_ids: list = field(default_factory=list)   # list[str] — downstream unit_ids
    is_terminal: bool = False                           # True if this step ends a process path (no successor)


@dataclass
class AtomicDecisionUnit:
    """A conditional gateway — evaluates state and routes flow to branches."""
    unit_id: str
    chunk_id: str           # parent StructuredChunk
    action: str             # the question/check ("Check if employee replied after 1 day")
    actor: str
    prev_step_ids: list = field(default_factory=list)   # list[str] — upstream unit_ids
    branches: list = field(default_factory=list)        # list[DecisionBranch]


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
    branches: list = field(default_factory=list)  # DIVERGING gateway only: [{label, condition, condition_var, condition_value, target_unit_id, is_default}]
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
class ContextIndex:
    job_id: str
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
    atomic_units: list = field(default_factory=list)  # list[AtomicStepUnit | AtomicDecisionUnit]
    bpmn_nodes: list = field(default_factory=list)
    bpmn_edges: list = field(default_factory=list)
    preamble: list = field(default_factory=list)       # list[StructuredChunk] — context-only chunks
    actor_heading_map: dict = field(default_factory=dict)


@dataclass
class Job:
    job_id: str
    source_file_path: str
    status: JobStatus = JobStatus.PENDING
    current_layer: Optional[int] = None
    error: Optional[JobError] = None
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
