"""L7 — Node Detector: AtomicUnit → BPMNNode (pure structural mapping).

One START_EVENT and one END_EVENT per ProcessModel; all tasks/gateways lie between them.
"""
MAX_LABEL_LEN = 45


def truncate_label(text: str, max_len: int = MAX_LABEL_LEN) -> str:
    """Truncate at word boundary with ellipsis if text exceeds max_len."""
    if len(text) <= max_len:
        return text
    truncated = text[:max_len].rsplit(" ", 1)[0]
    return truncated + "\u2026"
import uuid

from models.schemas import BPMNNode, BPMNNodeType, BlockType, Job, ProcessModel


def run(job: Job) -> None:
    for process in job.processes:
        _build_process_nodes(job, process)


def _build_process_nodes(job: Job, process: ProcessModel) -> None:
    block_map = {b.block_id: b for b in process.blocks}
    unit_to_task_node: dict[str, str] = {}
    block_to_exception_node: dict[str, str] = {}

    start_node = BPMNNode(
        node_id=_nid(),
        job_id=job.job_id,
        bpmn_type=BPMNNodeType.START_EVENT,
        label="Start",
    )
    nodes: list[BPMNNode] = [start_node]

    for unit in process.atomic_units:
        block = block_map.get(unit.block_id)
        if not block:
            continue

        task_node = BPMNNode(
            node_id=_nid(),
            job_id=job.job_id,
            unit_id=unit.unit_id,
            label=truncate_label(unit.action),
            actor=unit.actor,
        )

        if block.block_type == BlockType.DECISION:
            task_node.bpmn_type = BPMNNodeType.GATEWAY
        else:
            task_node.bpmn_type = BPMNNodeType.TASK

        nodes.append(task_node)
        unit_to_task_node[unit.unit_id] = task_node.node_id

    end_node = BPMNNode(
        node_id=_nid(),
        job_id=job.job_id,
        bpmn_type=BPMNNodeType.END_EVENT,
        label="End",
    )
    nodes.append(end_node)

    for block in process.blocks:
        if block.block_type != BlockType.EXCEPTION:
            continue
        label = " ".join(block.raw_text.split()[:8])
        node = BPMNNode(
            node_id=_nid(),
            job_id=job.job_id,
            bpmn_type=BPMNNodeType.BOUNDARY_EVENT,
            label=label,
        )
        nodes.append(node)
        block_to_exception_node[block.block_id] = node.node_id

    process.bpmn_nodes = nodes
    process.__dict__["_unit_to_task_node"] = unit_to_task_node
    process.__dict__["_block_to_exception_node"] = block_to_exception_node


def validate_gate(job: Job) -> None:
    for process in job.processes:
        types = {n.bpmn_type for n in process.bpmn_nodes}
        if BPMNNodeType.START_EVENT not in types:
            raise LayerError("L7_NO_START_EVENT", f"No START_EVENT node in {process.name}.")
        if BPMNNodeType.END_EVENT not in types:
            raise LayerError("L7_NO_END_EVENT", f"No END_EVENT node in {process.name}.")

        starts = [n for n in process.bpmn_nodes if n.bpmn_type == BPMNNodeType.START_EVENT]
        ends = [n for n in process.bpmn_nodes if n.bpmn_type == BPMNNodeType.END_EVENT]
        if len(starts) != 1:
            raise LayerError(
                "L7_MULTIPLE_START_EVENTS",
                f"Expected exactly 1 START_EVENT in {process.name}, found {len(starts)}.",
            )
        if len(ends) != 1:
            raise LayerError(
                "L7_MULTIPLE_END_EVENTS",
                f"Expected exactly 1 END_EVENT in {process.name}, found {len(ends)}.",
            )

        unlabeled = [n for n in process.bpmn_nodes if not n.label]
        if unlabeled:
            raise LayerError("L7_UNLABELED_NODES", f"{len(unlabeled)} nodes in {process.name} without labels.")


def _nid() -> str:
    return str(uuid.uuid4())[:8]


class LayerError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
