"""
Smoke Test — End-to-End Pipeline Validator
==========================================
Runs the full pipeline on real SOP files (passed via CLI or discovered in jobs/)
and asserts bare-minimum structural correctness on the resulting graph.

Usage:
    # Run on all .pdf/.docx files in jobs/
    python -m pytest tests/smoke_test.py -v

    # Run on a specific file
    SMOKE_TEST_FILE=/path/to/sop.pdf python -m pytest tests/smoke_test.py -v
"""
import os
import sys
import uuid
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from models.schemas import Job, JobStatus
from pipeline.orchestrator import run_pipeline


def _discover_sop_files() -> list[str]:
    env_paths = os.environ.get("SMOKE_TEST_FILE", "")
    if env_paths:
        return [p.strip() for p in env_paths.split(",") if p.strip()]

    from pathlib import Path
    jobs_dir = Path(__file__).parent.parent / "jobs"
    found = []
    if jobs_dir.exists():
        found.extend(str(p) for p in jobs_dir.glob("*.pdf"))
        found.extend(str(p) for p in jobs_dir.glob("*.docx"))
    return found


def _run_pipeline_on_file(filepath: str) -> Job:
    job = Job(
        job_id=f"smoke_{str(uuid.uuid4())[:8]}",
        source_file_path=filepath,
        created_at=datetime.utcnow().isoformat(),
    )
    return run_pipeline(job)


def _assert_graph_sane(job: Job) -> None:
    assert job.processes, "Pipeline produced no processes."

    for proc in job.processes:
        nodes = proc.bpmn_nodes
        edges = proc.bpmn_edges
        node_ids = {n.node_id for n in nodes}

        starts = [n for n in nodes if n.bpmn_type.value == "START_EVENT"]
        ends   = [n for n in nodes if n.bpmn_type.value == "END_EVENT"]
        assert len(starts) >= 1, f"[{proc.name}] No START_EVENT node."
        assert len(ends)   >= 1, f"[{proc.name}] No END_EVENT node."

        targets = {e.target_node_id for e in edges}
        for n in nodes:
            if n.bpmn_type.value in ("TASK", "GATEWAY", "END_EVENT", "SUBPROCESS"):
                assert n.node_id in targets, (
                    f"[{proc.name}] Node '{n.label}' ({n.node_id}) has no incoming edge."
                )

        sources: dict[str, int] = {}
        for e in edges:
            sources[e.source_node_id] = sources.get(e.source_node_id, 0) + 1
        for n in nodes:
            if n.bpmn_type.value == "GATEWAY":
                out = sources.get(n.node_id, 0)
                assert out >= 2, (
                    f"[{proc.name}] Gateway '{n.label}' ({n.node_id}) has only {out} outgoing edge(s)."
                )

        for e in edges:
            assert e.source_node_id in node_ids, \
                f"[{proc.name}] Edge {e.edge_id} sourceRef '{e.source_node_id}' not in nodes."
            assert e.target_node_id in node_ids, \
                f"[{proc.name}] Edge {e.edge_id} targetRef '{e.target_node_id}' not in nodes."


def get_sop_fixtures():
    files = _discover_sop_files()
    if not files:
        return [pytest.param(None, marks=pytest.mark.skip(
            reason="No SOP files found in jobs/ and SMOKE_TEST_FILE not set."
        ))]
    return files


@pytest.mark.parametrize("sop_path", get_sop_fixtures())
def test_smoke_pipeline(sop_path):
    print(f"\n[smoke_test] Running pipeline on: {sop_path}")
    job = _run_pipeline_on_file(sop_path)

    assert job.status in (JobStatus.COMPLETE, JobStatus.NEEDS_REVIEW), (
        f"Pipeline FAILED for {sop_path}.\n"
        f"Error: [{getattr(job.error, 'error_code', 'N/A')}] "
        f"{getattr(job.error, 'message', 'No message')}\n"
        f"Traceback:\n{getattr(job.error, 'traceback', '')}"
    )

    _assert_graph_sane(job)

    total_nodes = sum(len(p.bpmn_nodes) for p in job.processes)
    total_edges = sum(len(p.bpmn_edges) for p in job.processes)
    print(f"[smoke_test] {len(job.processes)} process(es), {total_nodes} nodes, {total_edges} edges")

    if job.review_flags:
        print(f"[smoke_test] Review flags ({len(job.review_flags)}):")
        for flag in job.review_flags:
            print(f"  L{flag.layer}: {flag.reason}")

    print(f"[smoke_test] ✓ PASSED")
