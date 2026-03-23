from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class BlockType(str, Enum):
    STEP = "STEP"
    DECISION = "DECISION"
    EXCEPTION = "EXCEPTION"
    ACTOR = "ACTOR"
    CONDITION = "CONDITION"
    NOTE = "NOTE"
    HEADER = "HEADER"
    UNKNOWN = "UNKNOWN"


class BPMNNodeType(str, Enum):
    START_EVENT = "START_EVENT"
    END_EVENT = "END_EVENT"
    TASK = "TASK"
    GATEWAY = "GATEWAY"
    BOUNDARY_EVENT = "BOUNDARY_EVENT"
    SUBPROCESS = "SUBPROCESS"


class GatewayType(str, Enum):
    XOR = "XOR"
    AND = "AND"
    OR = "OR"


class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass
class CrossRef:
    ref_text: str
    resolved_block_id: Optional[str] = None
    resolution_method: str = "unresolved"  # structural_anchor | llm | unresolved


@dataclass
class PronounResolution:
    original_pronoun: str
    resolved_to: str
    confidence: float
    method: str = "llm"

@dataclass
class Block:
    block_id: str
    job_id: str
    parent_id: Optional[str] = None
    children_ids: list = field(default_factory=list)

    raw_text: str = ""
    heading_path: list = field(default_factory=list)
    page_number: Optional[int] = None
    list_depth: int = 0
    list_index: Optional[str] = None

    block_type: Optional[BlockType] = None
    block_type_confidence: Optional[float] = None
    block_type_method: Optional[str] = None  # structural_skip | llm

    resolved_actor: Optional[str] = None
    condition_scope: Optional[str] = None
    cross_refs: list = field(default_factory=list)
    pronoun_resolution: Optional[PronounResolution] = None
    enrichment_version: int = 0

    atomic_units: list = field(default_factory=list)
    needs_review: bool = False
    review_reasons: list = field(default_factory=list)


@dataclass
class DocumentNode:
    heading: str
    heading_path: list
    level: int
    blocks: list = field(default_factory=list)
    children: list = field(default_factory=list)



@dataclass
class AtomicUnit:
    unit_id: str
    block_id: str
    sequence_in_block: int
    action: str
    actor: str
    condition: Optional[str] = None
    output: Optional[str] = None
    is_terminal: bool = False
    is_start: bool = False
    # Variable propagation (populated by L6, consumed by L6b + L8)
    inputs: list = field(default_factory=list)   # variable names this unit consumes
    outputs: list = field(default_factory=list)  # variable names this unit produces


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
    condition_variable: Optional[str] = None   # variable name tested at this branch (L8)
    condition_value: Optional[str] = None      # expected value for the condition label


@dataclass
class Actor:
    canonical_name: str
    aliases: list = field(default_factory=list)
    source_section: str = ""
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
    block_id: str
    heading_path: list = field(default_factory=list)


@dataclass
class GlossaryEntry:
    term: str
    definition: str
    block_id: str
    definition_method: str = "llm"


@dataclass
class DataVar:
    """Tracks a single named variable flowing through the process."""
    name: str
    var_type: str = "unknown"  # bool | data | id | count | unknown
    producer_unit_id: Optional[str] = None
    consumers: list = field(default_factory=list)  # unit_ids that input this var


@dataclass
class ContextIndex:
    job_id: str
    section_anchors: list = field(default_factory=list)
    glossary: list = field(default_factory=list)
    exception_blocks: list = field(default_factory=list)
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
    block_id: Optional[str] = None


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
    blocks: list = field(default_factory=list)
    atomic_units: list = field(default_factory=list)
    data_vars: list = field(default_factory=list)
    bpmn_nodes: list = field(default_factory=list)
    bpmn_edges: list = field(default_factory=list)
    preamble: list = field(default_factory=list)  # non-executable sections attached as context


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

    blocks: list = field(default_factory=list)
    document_tree: list = field(default_factory=list)
    context_index: Optional[ContextIndex] = None
    processes: list = field(default_factory=list)
    extraction: dict = field(default_factory=dict)
