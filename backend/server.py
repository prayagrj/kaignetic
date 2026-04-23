"""Lightweight API server that exposes pipeline outputs for the React frontend."""

import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, str(Path(__file__).parent))

import config
from models.schemas import Job, JobStatus
from pipeline.orchestrator import run_pipeline

app = FastAPI(title="BPMN Pipeline API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _slug(text: str) -> str:
    return re.sub(r"[^\w]", "_", text.lower())[:40]


def _extract_processes(job_id: str) -> list[dict]:
    """Load processes from the L7 layer snapshot for a completed job."""
    layer_dir = Path(config.OUTPUT_DIR) / "layer-wise-output" / job_id
    snapshot_path = layer_dir / "L7_l7_dag_resolver_output.json"
    if not snapshot_path.exists():
        raise HTTPException(status_code=404, detail=f"No graph snapshot found for job {job_id!r}")

    with open(snapshot_path) as f:
        raw = json.load(f)

    processes = []
    for proc in raw.get("processes", []):
        actor_to_lane = proc.get("_actor_to_lane") or proc.get("actor_to_lane") or {}

        for n in proc.get("bpmn_nodes", []):
            if n.get("actor") and n["actor"] not in actor_to_lane:
                actor_to_lane[n["actor"]] = _slug(n["actor"])

        nodes = [
            {
                "node_id": n["node_id"],
                "bpmn_type": n.get("bpmn_type", "TASK"),
                "label": n.get("label") or "",
                "actor": n.get("actor"),
                "gateway_type": n.get("gateway_type"),
                "gateway_direction": n.get("gateway_direction"),
                "needs_review": n.get("needs_review", False),
                "review_reasons": n.get("review_reasons", []),
            }
            for n in proc.get("bpmn_nodes", [])
        ]

        edges = [
            {
                "edge_id": e["edge_id"],
                "source": e["source_node_id"],
                "target": e["target_node_id"],
                "label": e.get("label"),
                "is_default": e.get("is_default", False),
                "condition_variable": e.get("condition_variable"),
                "condition_value": e.get("condition_value"),
            }
            for e in proc.get("bpmn_edges", [])
        ]

        processes.append({
            "process_id": proc["process_id"],
            "name": proc["name"],
            "actor_to_lane": actor_to_lane,
            "nodes": nodes,
            "edges": edges,
        })

    return processes


def _write_report(job: Job, source_filename: str, processes: list[dict]) -> None:
    """Write a report.json for the completed job (used by GET /api/jobs)."""
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    report = {
        "job_id": job.job_id,
        "status": job.status.value,
        "source_file": source_filename,
        "process_count": len(processes),
        "node_count_total": sum(len(p["nodes"]) for p in processes),
        "edge_count_total": sum(len(p["edges"]) for p in processes),
        "review_flags": [
            {"chunk_id": f.chunk_id, "layer": f.layer, "reason": f.reason}
            for f in job.review_flags
        ],
        "llm_call_log": [
            {
                "layer": r.layer,
                "template": r.prompt_template,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "latency_ms": r.latency_ms,
                "cached": r.cached,
            }
            for r in job.llm_call_log
        ],
    }
    report_path = Path(config.OUTPUT_DIR) / f"{job.job_id}_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload_and_process(file: UploadFile = File(...)):
    """
    Accept a PDF or DOCX upload, run the full pipeline synchronously,
    and return the graph data (nodes + edges) ready for the frontend.
    """
    suffix = Path(file.filename or "upload").suffix.lower()
    if suffix not in (".pdf", ".docx", ".doc"):
        raise HTTPException(status_code=400, detail="Only PDF and DOCX files are supported.")

    job_id = str(uuid.uuid4())[:12]
    dest_path = Path(config.JOBS_DIR) / f"{job_id}{suffix}"

    with open(dest_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    job = Job(
        job_id=job_id,
        source_file_path=str(dest_path),
        created_at=datetime.utcnow().isoformat(),
    )

    job = run_pipeline(job)

    if job.status == JobStatus.FAILED:
        err = job.error
        detail = f"{err.error_code}: {err.message}" if err else "Pipeline failed"
        raise HTTPException(status_code=500, detail=detail)

    processes = _extract_processes(job_id)
    _write_report(job, file.filename or "", processes)

    return {
        "job_id": job_id,
        "status": job.status.value,
        "source_file": file.filename,
        "processes": processes,
    }


@app.get("/api/jobs")
def list_jobs():
    """List all job IDs that have reports in the output directory."""
    output_dir = Path(config.OUTPUT_DIR)
    reports = sorted(output_dir.glob("*_report.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"job_id": p.stem.replace("_report", "")} for p in reports]


@app.get("/api/jobs/{job_id}/graph")
def get_graph(job_id: str):
    """Return graph data for a previously processed job."""
    report_path = Path(config.OUTPUT_DIR) / f"{job_id}_report.json"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")

    processes = _extract_processes(job_id)
    return {"job_id": job_id, "processes": processes}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
