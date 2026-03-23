"""
Smoke Test — End-to-End Pipeline Structural Validator
======================================================
Runs the full pipeline on real SOP files (passed via CLI or discovered in jobs/)
and asserts bare-minimum structural correctness on every .bpmn output.

Usage:
    # Run on all .pdf/.docx files in jobs/
    python -m pytest tests/smoke_test.py -v

    # Run on a specific file (set SMOKE_TEST_FILE env var)
    SMOKE_TEST_FILE=/path/to/sop.pdf python -m pytest tests/smoke_test.py -v

bpmnlint integration (optional — install with `npm install -g bpmnlint`):
    If bpmnlint is on PATH, it will be called automatically on each .bpmn file.
    Failures are reported as warnings, not hard errors.
"""
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from lxml import etree

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from models.schemas import Job, JobStatus
from pipeline.orchestrator import run_pipeline

# ── BPMN namespace ────────────────────────────────────────────────────────────
BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _discover_sop_files() -> list[str]:
    """
    Find real SOP files to test against.
    Priority order:
      1. SMOKE_TEST_FILE env var (comma-separated list of paths)
      2. Any .pdf or .docx files in jobs/ relative to the repo root
    """
    env_paths = os.environ.get("SMOKE_TEST_FILE", "")
    if env_paths:
        return [p.strip() for p in env_paths.split(",") if p.strip()]

    repo_root = Path(__file__).parent.parent
    jobs_dir = repo_root / "jobs"
    found = []
    if jobs_dir.exists():
        found.extend(str(p) for p in jobs_dir.glob("*.pdf"))
        found.extend(str(p) for p in jobs_dir.glob("*.docx"))
    return found


def _run_pipeline_on_file(filepath: str) -> tuple[Job, list[str]]:
    """Run the full pipeline on a single SOP file. Returns (job, bpmn_output_paths)."""
    job_id = f"smoke_{str(uuid.uuid4())[:8]}"
    job = Job(
        job_id=job_id,
        source_file_path=filepath,
        created_at=datetime.utcnow().isoformat(),
    )
    job = run_pipeline(job)
    output_files = job.__dict__.get("output_files") or []
    if not output_files and job.__dict__.get("output_file"):
        output_files = [job.__dict__["output_file"]]
    return job, output_files


def _assert_bpmn_sane(bpmn_path: str) -> None:
    """
    Parse a .bpmn file and assert bare-minimum structural correctness.
    Raises AssertionError with a descriptive message if any check fails.
    """
    assert os.path.exists(bpmn_path), f"BPMN file not found: {bpmn_path}"
    assert os.path.getsize(bpmn_path) > 0, f"BPMN file is empty: {bpmn_path}"

    tree = etree.parse(bpmn_path)
    root = tree.getroot()

    # 1. At least one startEvent
    starts = root.findall(f".//{{{BPMN_NS}}}startEvent")
    assert len(starts) >= 1, \
        f"[{bpmn_path}] No startEvent found — pipeline must emit at least one."

    # 2. At least one endEvent
    ends = root.findall(f".//{{{BPMN_NS}}}endEvent")
    assert len(ends) >= 1, \
        f"[{bpmn_path}] No endEvent found — pipeline must emit at least one."

    # 3. Every task/gateway/endEvent has at least one incoming sequence flow
    #    (startEvent and boundaryEvent are exempt by BPMN spec)
    flows = root.findall(f".//{{{BPMN_NS}}}sequenceFlow")
    targets = {f.get("targetRef") for f in flows}

    checkable_tags = [
        f"{{{BPMN_NS}}}task",
        f"{{{BPMN_NS}}}userTask",
        f"{{{BPMN_NS}}}serviceTask",
        f"{{{BPMN_NS}}}exclusiveGateway",
        f"{{{BPMN_NS}}}parallelGateway",
        f"{{{BPMN_NS}}}inclusiveGateway",
        f"{{{BPMN_NS}}}endEvent",
        f"{{{BPMN_NS}}}subProcess",
    ]
    all_checkable = []
    for tag in checkable_tags:
        all_checkable.extend(root.findall(f".//{tag}"))

    for node in all_checkable:
        node_id = node.get("id", "")
        name = node.get("name", "")
        assert node_id in targets, (
            f"[{bpmn_path}] Node '{name}' (id={node_id}) "
            f"has no incoming sequence flow — unreachable node."
        )

    # 4. Every exclusiveGateway has >= 2 outgoing sequence flows
    gateway_tags = [
        f"{{{BPMN_NS}}}exclusiveGateway",
        f"{{{BPMN_NS}}}parallelGateway",
        f"{{{BPMN_NS}}}inclusiveGateway",
    ]
    sources: dict[str, int] = {}
    for f in flows:
        src = f.get("sourceRef", "")
        sources[src] = sources.get(src, 0) + 1

    for tag in gateway_tags:
        for gw in root.findall(f".//{tag}"):
            gw_id = gw.get("id", "")
            gw_name = gw.get("name", "")
            out_count = sources.get(gw_id, 0)
            assert out_count >= 2, (
                f"[{bpmn_path}] Gateway '{gw_name}' (id={gw_id}) "
                f"has only {out_count} outgoing edge(s) — must have >= 2."
            )

    # 5. Sequence flows reference valid node IDs (no dangling refs)
    all_ids = {
        node.get("id")
        for node in root.iter()
        if node.get("id")
    }
    for flow in flows:
        src_ref = flow.get("sourceRef", "")
        tgt_ref = flow.get("targetRef", "")
        assert src_ref in all_ids, \
            f"[{bpmn_path}] sequenceFlow sourceRef='{src_ref}' not found in document."
        assert tgt_ref in all_ids, \
            f"[{bpmn_path}] sequenceFlow targetRef='{tgt_ref}' not found in document."


def _run_bpmnlint(bpmn_path: str) -> None:
    """
    Run bpmnlint if available. Failures are printed as warnings, not hard errors.
    Install: npm install -g bpmnlint
    """
    result = subprocess.run(
        ["which", "bpmnlint"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  [bpmnlint] not on PATH — skipping spec validation for {bpmn_path}")
        return

    lint_result = subprocess.run(
        ["bpmnlint", bpmn_path],
        capture_output=True, text=True,
    )
    if lint_result.returncode != 0:
        print(f"  [bpmnlint] WARNINGS for {bpmn_path}:\n{lint_result.stdout}\n{lint_result.stderr}")
    else:
        print(f"  [bpmnlint] OK: {bpmn_path}")


# ── Test ───────────────────────────────────────────────────────────────────────

def get_sop_fixtures():
    """Pytest parametrize factory — returns list of SOP file paths."""
    files = _discover_sop_files()
    if not files:
        return [pytest.param(None, marks=pytest.mark.skip(
            reason="No SOP files found in jobs/ and SMOKE_TEST_FILE not set. "
                   "Add a .pdf/.docx to jobs/ or set SMOKE_TEST_FILE=/path/to/sop.pdf"
        ))]
    return files


@pytest.mark.parametrize("sop_path", get_sop_fixtures())
def test_smoke_pipeline(sop_path):
    """
    Full end-to-end smoke test:
    1. Runs the pipeline on a real SOP file
    2. Asserts the pipeline completed (COMPLETE or NEEDS_REVIEW — not FAILED)
    3. Asserts every .bpmn output is structurally sane
    4. Optionally validates with bpmnlint
    """
    print(f"\n[smoke_test] Running pipeline on: {sop_path}")
    job, bpmn_paths = _run_pipeline_on_file(sop_path)

    assert job.status in (JobStatus.COMPLETE, JobStatus.NEEDS_REVIEW), (
        f"Pipeline FAILED for {sop_path}.\n"
        f"Error: [{getattr(job.error, 'error_code', 'N/A')}] "
        f"{getattr(job.error, 'message', 'No message')}\n"
        f"Traceback:\n{getattr(job.error, 'traceback', '')}"
    )

    assert len(bpmn_paths) > 0, (
        f"Pipeline completed but produced no .bpmn files for {sop_path}."
    )

    print(f"[smoke_test] Produced {len(bpmn_paths)} .bpmn file(s)")

    for bpmn_path in bpmn_paths:
        print(f"[smoke_test] Asserting structural sanity: {bpmn_path}")
        _assert_bpmn_sane(bpmn_path)
        _run_bpmnlint(bpmn_path)

    if job.review_flags:
        print(f"[smoke_test] Review flags ({len(job.review_flags)}):")
        for flag in job.review_flags:
            print(f"  L{flag.layer}: {flag.reason}")

    print(f"[smoke_test] ✓ PASSED — {len(bpmn_paths)} BPMN file(s) structurally valid")
