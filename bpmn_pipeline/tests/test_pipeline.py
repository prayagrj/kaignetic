"""Basic tests for pure-Python layers (no LLM calls needed)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from models.schemas import Job, BlockType, BPMNNodeType


# ── L2 Segmentation ──────────────────────────────────────────────────────────

def make_job():
    return Job(job_id="test_job", source_file_path="test.pdf")


def test_l2_heading_creates_header_block():
    from pipeline.layers import l2_segmentation
    job = make_job()
    job.extraction = {"markdown": "# Section One\n\nDo this step.", "docling_document": {}}
    l2_segmentation.run(job)
    headers = [b for b in job.blocks if b.block_type == BlockType.HEADER]
    assert len(headers) >= 1
    assert headers[0].raw_text == "Section One"


def test_l2_list_item_creates_child_block():
    from pipeline.layers import l2_segmentation
    job = make_job()
    job.extraction = {"markdown": "# Process\n\n- Step one\n- Step two", "docling_document": {}}
    l2_segmentation.run(job)
    list_blocks = [b for b in job.blocks if b.list_depth >= 1]
    assert len(list_blocks) == 2


def test_l2_sop_class_detection():
    from pipeline.layers import l2_segmentation
    job = make_job()
    job.extraction = {"markdown": "# Policy\n\nEmployee onboarding and payroll process.", "docling_document": {}}
    l2_segmentation.run(job)
    assert job.sop_class == "HR_PROCESS"


def test_l2_gate_fails_on_empty_tree():
    from pipeline.layers import l2_segmentation
    job = make_job()
    job.blocks = []
    with pytest.raises(l2_segmentation.LayerError) as exc:
        l2_segmentation.validate_gate(job)
    assert exc.value.code == "L2_EMPTY_TREE"


# ── L7 Node Detector ─────────────────────────────────────────────────────────

def test_l7_creates_start_and_end_events():
    from pipeline.layers import l7_node_detector
    from models.schemas import AtomicUnit, Block, BlockType, ProcessModel

    job = make_job()
    block = Block(block_id="b1", job_id=job.job_id, raw_text="Do something", block_type=BlockType.STEP)
    unit = AtomicUnit(
        unit_id="u1", block_id="b1", sequence_in_block=0,
        action="Do something", actor="User", is_start=True, is_terminal=True,
    )
    block.atomic_units = [unit]
    proc = ProcessModel(process_id="p1", name="Test", blocks=[block], atomic_units=[unit])
    job.processes = [proc]

    l7_node_detector.run(job)

    nodes = proc.bpmn_nodes
    starts = [n for n in nodes if n.bpmn_type == BPMNNodeType.START_EVENT]
    ends = [n for n in nodes if n.bpmn_type == BPMNNodeType.END_EVENT]
    tasks = [n for n in nodes if n.bpmn_type == BPMNNodeType.TASK]
    assert len(starts) == 1
    assert len(ends) == 1
    assert len(tasks) == 1
    assert nodes[0].bpmn_type == BPMNNodeType.START_EVENT
    assert nodes[1].bpmn_type == BPMNNodeType.TASK
    assert nodes[2].bpmn_type == BPMNNodeType.END_EVENT


def test_l7_multi_unit_single_start_end():
    """Multiple atomic units still yield one START and one END (linear spine)."""
    from pipeline.layers import l7_node_detector
    from models.schemas import AtomicUnit, Block, BlockType, ProcessModel

    job = make_job()
    b1 = Block(block_id="b1", job_id=job.job_id, raw_text="First", block_type=BlockType.STEP)
    b2 = Block(block_id="b2", job_id=job.job_id, raw_text="Last", block_type=BlockType.STEP)
    u1 = AtomicUnit(unit_id="u1", block_id="b1", sequence_in_block=0, action="First", actor="A", is_start=True, is_terminal=False)
    u2 = AtomicUnit(unit_id="u2", block_id="b2", sequence_in_block=0, action="Last", actor="A", is_start=False, is_terminal=True)
    b1.atomic_units = [u1]
    b2.atomic_units = [u2]
    proc = ProcessModel(
        process_id="p1", name="Test",
        blocks=[b1, b2], atomic_units=[u1, u2],
    )
    job.processes = [proc]

    l7_node_detector.run(job)

    starts = [n for n in proc.bpmn_nodes if n.bpmn_type == BPMNNodeType.START_EVENT]
    ends = [n for n in proc.bpmn_nodes if n.bpmn_type == BPMNNodeType.END_EVENT]
    assert len(starts) == 1
    assert len(ends) == 1
    assert len([n for n in proc.bpmn_nodes if n.bpmn_type == BPMNNodeType.TASK]) == 2


def test_l8_sequential_spine_start_to_end():
    from unittest.mock import MagicMock, patch

    from pipeline.layers import l7_node_detector, l8_edge_detector
    from models.schemas import AtomicUnit, Block, BlockType, ProcessModel

    job = make_job()
    b1 = Block(block_id="b1", job_id=job.job_id, raw_text="First", block_type=BlockType.STEP)
    u1 = AtomicUnit(unit_id="u1", block_id="b1", sequence_in_block=0, action="First", actor="A", is_start=True, is_terminal=True)
    b1.atomic_units = [u1]
    proc = ProcessModel(process_id="p1", name="Test", blocks=[b1], atomic_units=[u1])
    job.processes = [proc]

    l7_node_detector.run(job)
    proc.data_vars = []

    mock_llm = MagicMock()
    mock_llm.call.return_value = None

    with patch("pipeline.layers.l8_edge_detector.LLMClient", return_value=mock_llm):
        l8_edge_detector.run(job)

    assert len(proc.bpmn_edges) >= 2
    start_id = next(n.node_id for n in proc.bpmn_nodes if n.bpmn_type == BPMNNodeType.START_EVENT)
    end_id = next(n.node_id for n in proc.bpmn_nodes if n.bpmn_type == BPMNNodeType.END_EVENT)
    task_id = next(n.node_id for n in proc.bpmn_nodes if n.bpmn_type == BPMNNodeType.TASK)
    pairs = {(e.source_node_id, e.target_node_id) for e in proc.bpmn_edges}
    assert (start_id, task_id) in pairs
    assert (task_id, end_id) in pairs


# ── L9 DAG Resolver ───────────────────────────────────────────────────────────

def test_l9_detects_dangling_edge():
    from pipeline.layers import l9_dag_resolver
    from models.schemas import BPMNNode, BPMNEdge, BPMNNodeType, ProcessModel

    job = make_job()
    proc = ProcessModel(process_id="p1", name="Test")
    proc.bpmn_nodes = [
        BPMNNode(node_id="n1", job_id=job.job_id, bpmn_type=BPMNNodeType.START_EVENT, label="Start"),
        BPMNNode(node_id="n2", job_id=job.job_id, bpmn_type=BPMNNodeType.END_EVENT, label="End"),
    ]
    proc.bpmn_edges = [
        BPMNEdge(edge_id="e1", job_id=job.job_id, source_node_id="n1", target_node_id="n2"),
        BPMNEdge(edge_id="e2", job_id=job.job_id, source_node_id="n2", target_node_id="GHOST"),
    ]
    job.processes = [proc]

    l9_dag_resolver.run(job)

    with pytest.raises(l9_dag_resolver.LayerError) as exc:
        l9_dag_resolver.validate_gate(job)
    assert exc.value.code == "L9_DANGLING_EDGE"


def test_l9_valid_graph_passes():
    from pipeline.layers import l9_dag_resolver
    from models.schemas import BPMNNode, BPMNEdge, BPMNNodeType, ProcessModel

    job = make_job()
    proc = ProcessModel(process_id="p1", name="Test")
    proc.bpmn_nodes = [
        BPMNNode(node_id="n1", job_id=job.job_id, bpmn_type=BPMNNodeType.START_EVENT, label="Start"),
        BPMNNode(node_id="n2", job_id=job.job_id, bpmn_type=BPMNNodeType.TASK, label="Task A", actor="User"),
        BPMNNode(node_id="n3", job_id=job.job_id, bpmn_type=BPMNNodeType.END_EVENT, label="End"),
    ]
    proc.bpmn_edges = [
        BPMNEdge(edge_id="e1", job_id=job.job_id, source_node_id="n1", target_node_id="n2"),
        BPMNEdge(edge_id="e2", job_id=job.job_id, source_node_id="n2", target_node_id="n3"),
    ]
    job.processes = [proc]

    l9_dag_resolver.run(job)
    l9_dag_resolver.validate_gate(job)  # Should not raise


def test_l3b_splits_distinct_actionable_sections():
    """Two H2 sections, each with STEP blocks → two separate processes."""
    from unittest.mock import MagicMock, patch
    from pipeline.layers import l3b_process_splitter
    from models.schemas import Block, BlockType

    job = make_job()
    job.sop_class = "HR_PROCESS"
    job.blocks = [
        Block(block_id="a", job_id=job.job_id, raw_text="Send offer letter",
              heading_path=["Policy", "Onboarding"], block_type=BlockType.STEP),
        Block(block_id="b", job_id=job.job_id, raw_text="Revoke access",
              heading_path=["Policy", "Offboarding"], block_type=BlockType.STEP),
    ]

    mock_llm = MagicMock()
    mock_llm.call.return_value = [
        {"process_name": "Onboarding", "heading_keys": ["Policy > Onboarding"]},
        {"process_name": "Offboarding", "heading_keys": ["Policy > Offboarding"]},
    ]

    with patch("pipeline.layers.l3b_process_splitter.LLMClient", return_value=mock_llm):
        l3b_process_splitter.run(job)

    assert len(job.processes) == 2
    names = {p.name for p in job.processes}
    assert "Onboarding" in names
    assert "Offboarding" in names


def test_l3b_ignores_preamble_sections():
    """Sections with only NOTE/CONDITION blocks must not produce a process."""
    from unittest.mock import MagicMock, patch
    from pipeline.layers import l3b_process_splitter
    from models.schemas import Block, BlockType

    job = make_job()
    job.sop_class = "HR_PROCESS"
    job.blocks = [
        # Preamble — CONDITION only
        Block(block_id="p1", job_id=job.job_id, raw_text="This SOP applies to all full-time employees.",
              heading_path=["1. Scope"], block_type=BlockType.CONDITION),
        # Preamble — NOTE only
        Block(block_id="p2", job_id=job.job_id, raw_text="Note: Contact HR for exceptions.",
              heading_path=["2. Purpose"], block_type=BlockType.NOTE),
        # Actual process — STEP block
        Block(block_id="s1", job_id=job.job_id, raw_text="Send offer letter",
              heading_path=["3. Onboarding"], block_type=BlockType.STEP),
    ]

    mock_llm = MagicMock()
    mock_llm.call.return_value = [
        {"process_name": "Onboarding", "heading_keys": ["3. Onboarding"]},
    ]

    with patch("pipeline.layers.l3b_process_splitter.LLMClient", return_value=mock_llm):
        l3b_process_splitter.run(job)

    assert len(job.processes) == 1
    assert job.processes[0].name == "Onboarding"
    # Preamble blocks should not appear in any process
    all_block_ids = {b.block_id for p in job.processes for b in p.blocks}
    assert "p1" not in all_block_ids
    assert "p2" not in all_block_ids


def test_l3b_fallback_on_llm_failure():
    """When LLM returns None, Phase 1 heuristic produces one process per actionable section."""
    from unittest.mock import MagicMock, patch
    from pipeline.layers import l3b_process_splitter
    from models.schemas import Block, BlockType

    job = make_job()
    job.sop_class = "HR_PROCESS"
    job.blocks = [
        Block(block_id="a", job_id=job.job_id, raw_text="Send offer letter",
              heading_path=["Onboarding"], block_type=BlockType.STEP),
        Block(block_id="b", job_id=job.job_id, raw_text="Revoke access",
              heading_path=["Offboarding"], block_type=BlockType.STEP),
        # Preamble — should still be filtered by Phase 1
        Block(block_id="c", job_id=job.job_id, raw_text="This SOP applies to all.",
              heading_path=["Scope"], block_type=BlockType.CONDITION),
    ]

    mock_llm = MagicMock()
    mock_llm.call.return_value = None  # LLM failure

    with patch("pipeline.layers.l3b_process_splitter.LLMClient", return_value=mock_llm):
        l3b_process_splitter.run(job)

    # Phase 1 fallback: two actionable sections, preamble excluded
    assert len(job.processes) == 2
    all_block_ids = {b.block_id for p in job.processes for b in p.blocks}
    assert "a" in all_block_ids
    assert "b" in all_block_ids
    assert "c" not in all_block_ids




def test_l10_layout_assigns_coordinates_on_cyclic_graph():
    from pipeline.layers import l10_translator
    from models.schemas import BPMNNode, BPMNEdge, BPMNNodeType

    nodes = [
        BPMNNode(node_id="n1", job_id="j", bpmn_type=BPMNNodeType.START_EVENT, label="Start"),
        BPMNNode(node_id="n2", job_id="j", bpmn_type=BPMNNodeType.TASK, label="T", actor="U"),
        BPMNNode(node_id="n3", job_id="j", bpmn_type=BPMNNodeType.END_EVENT, label="End"),
    ]
    edges = [
        BPMNEdge(edge_id="e1", job_id="j", source_node_id="n1", target_node_id="n2"),
        BPMNEdge(edge_id="e2", job_id="j", source_node_id="n2", target_node_id="n3"),
        BPMNEdge(edge_id="e3", job_id="j", source_node_id="n3", target_node_id="n2"),
    ]
    l10_translator._compute_layout(nodes, edges, {})
    assert all(n.x is not None and n.y is not None for n in nodes)


def test_l9_boundary_events_excluded_from_unreachable_rate():
    """BOUNDARY_EVENT nodes must not count toward the unreachable rate.
    Pre-fix: 6 boundary events on 9 total nodes = 67% unreachable -> FAIL.
    Post-fix: 0/3 countable nodes unreachable -> PASS.
    """
    from pipeline.layers import l9_dag_resolver
    from models.schemas import BPMNNode, BPMNEdge, BPMNNodeType, ProcessModel

    job = make_job()
    proc = ProcessModel(process_id="p1", name="8.3 BGV Failure Test")
    proc.bpmn_nodes = [
        BPMNNode(node_id="n1", job_id=job.job_id, bpmn_type=BPMNNodeType.START_EVENT, label="Start"),
        BPMNNode(node_id="n2", job_id=job.job_id, bpmn_type=BPMNNodeType.TASK, label="Task A", actor="HR"),
        BPMNNode(node_id="n3", job_id=job.job_id, bpmn_type=BPMNNodeType.END_EVENT, label="End"),
        BPMNNode(node_id="b1", job_id=job.job_id, bpmn_type=BPMNNodeType.BOUNDARY_EVENT, label="Exc 1"),
        BPMNNode(node_id="b2", job_id=job.job_id, bpmn_type=BPMNNodeType.BOUNDARY_EVENT, label="Exc 2"),
        BPMNNode(node_id="b3", job_id=job.job_id, bpmn_type=BPMNNodeType.BOUNDARY_EVENT, label="Exc 3"),
        BPMNNode(node_id="b4", job_id=job.job_id, bpmn_type=BPMNNodeType.BOUNDARY_EVENT, label="Exc 4"),
        BPMNNode(node_id="b5", job_id=job.job_id, bpmn_type=BPMNNodeType.BOUNDARY_EVENT, label="Exc 5"),
        BPMNNode(node_id="b6", job_id=job.job_id, bpmn_type=BPMNNodeType.BOUNDARY_EVENT, label="Exc 6"),
    ]
    proc.bpmn_edges = [
        BPMNEdge(edge_id="e1", job_id=job.job_id, source_node_id="n1", target_node_id="n2"),
        BPMNEdge(edge_id="e2", job_id=job.job_id, source_node_id="n2", target_node_id="n3"),
        *[
            BPMNEdge(edge_id=f"eb{i}", job_id=job.job_id, source_node_id=f"b{i}", target_node_id="n3")
            for i in range(1, 7)
        ],
    ]
    job.processes = [proc]

    l9_dag_resolver.run(job)
    l9_dag_resolver.validate_gate(job)  # Must not raise L9_HIGH_UNREACHABLE_RATE
