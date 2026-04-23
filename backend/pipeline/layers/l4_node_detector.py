"""L5 — Node Detector: AtomicUnit → BPMNNode (pure structural mapping).

One START_EVENT per ProcessModel. END_EVENTs come from is_terminal AtomicStepUnits;
a single fallback END_EVENT is added only when none are flagged by the atomizer.
DECISION atomic units → GATEWAY nodes; is_terminal steps → END_EVENT; others → TASK.
"""
import uuid

from models.schemas import AtomicDecisionUnit, AtomicStepUnit, BPMNNode, BPMNNodeType, Job, ProcessModel

MAX_LABEL_LEN = 120


def truncate_label(text: str, max_len: int = MAX_LABEL_LEN) -> str:
    if len(text) <= max_len:
        return text
    truncated = text[:max_len].rsplit(" ", 1)[0]
    return truncated + "\u2026"


def run(job: Job) -> None:
    for process in job.processes:
        _build_process_nodes(job, process)


def _build_process_nodes(job: Job, process: ProcessModel) -> None:
    unit_to_task_node: dict[str, str] = {}

    start_node = BPMNNode(
        node_id=_nid(),
        job_id=job.job_id,
        bpmn_type=BPMNNodeType.START_EVENT,
        label="Start",
    )
    nodes: list[BPMNNode] = [start_node]

    for unit in process.atomic_units:
        task_node = BPMNNode(
            node_id=_nid(),
            job_id=job.job_id,
            unit_id=unit.unit_id,
            label=truncate_label(unit.action),
            actor=unit.actor,
        )

        if isinstance(unit, AtomicDecisionUnit):
            task_node.bpmn_type = BPMNNodeType.GATEWAY
        elif isinstance(unit, AtomicStepUnit) and unit.is_terminal:
            task_node.bpmn_type = BPMNNodeType.END_EVENT
        else:
            task_node.bpmn_type = BPMNNodeType.TASK

        nodes.append(task_node)
        unit_to_task_node[unit.unit_id] = task_node.node_id

    has_terminal = any(n.bpmn_type == BPMNNodeType.END_EVENT for n in nodes)
    if not has_terminal:
        end_node = BPMNNode(
            node_id=_nid(),
            job_id=job.job_id,
            bpmn_type=BPMNNodeType.END_EVENT,
            label="End",
        )
        nodes.append(end_node)

    process.bpmn_nodes = nodes
    process.__dict__["_unit_to_task_node"] = unit_to_task_node


def validate_gate(job: Job) -> None:
    for process in job.processes:
        types = {n.bpmn_type for n in process.bpmn_nodes}
        if BPMNNodeType.START_EVENT not in types:
            raise LayerError("L5_NO_START_EVENT", f"No START_EVENT node in {process.name}.")
        if BPMNNodeType.END_EVENT not in types:
            raise LayerError("L5_NO_END_EVENT", f"No END_EVENT node in {process.name}.")

        starts = [n for n in process.bpmn_nodes if n.bpmn_type == BPMNNodeType.START_EVENT]
        ends = [n for n in process.bpmn_nodes if n.bpmn_type == BPMNNodeType.END_EVENT]
        if len(starts) != 1:
            raise LayerError("L5_MULTIPLE_START_EVENTS", f"Expected 1 START_EVENT in {process.name}, found {len(starts)}.")
        if len(ends) == 0:
            raise LayerError("L5_NO_END_EVENTS", f"Expected at least 1 END_EVENT in {process.name}, found none.")

        unlabeled = [n for n in process.bpmn_nodes if not n.label]
        if unlabeled:
            raise LayerError("L5_UNLABELED_NODES", f"{len(unlabeled)} nodes in {process.name} without labels.")


def _nid() -> str:
    return str(uuid.uuid4())[:8]


class LayerError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
