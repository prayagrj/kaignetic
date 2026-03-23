"""Orchestrator: runs all 10 layers sequentially with gate checking."""
import traceback
from datetime import datetime

from models.schemas import Job, JobError, JobStatus, ReviewFlag
from pipeline.layers import (
    l1_extraction, l2_segmentation, l3_classifier, l3b_process_splitter, l4_context,
    l5_enrichment, l6_atomizer, l6b_var_linker, l7_node_detector,
    l8_edge_detector, l9_dag_resolver, l10_translator,
)

LAYERS = [
    (1,  l1_extraction),
    (2,  l2_segmentation),
    (3,  l3_classifier),
    ("3b", l3b_process_splitter),
    (4,  l4_context),
    (5,  l5_enrichment),
    (6,  l6_atomizer),
    ("6b", l6b_var_linker),   # Pure Python — no LLM
    (7,  l7_node_detector),
    (8,  l8_edge_detector),
    (9,  l9_dag_resolver),
    (10, l10_translator),
]


def run_pipeline(job: Job) -> Job:
    job.status = JobStatus.RUNNING
    job.created_at = _now()

    for layer_num, layer_mod in LAYERS:
        job.current_layer = layer_num
        job.updated_at = _now()
        print(f"[Pipeline] Running L{layer_num}: {layer_mod.__name__.split('.')[-1]}")

        try:
            layer_mod.run(job)
        except Exception as e:
            code = getattr(e, "code", f"L{layer_num}_ERROR")
            job.status = JobStatus.FAILED
            job.error = JobError(
                layer=layer_num,
                error_code=code,
                message=str(e),
                traceback=traceback.format_exc(),
            )
            print(f"[Pipeline] L{layer_num} FAILED: {code} — {e}")
            return job

        try:
            layer_mod.validate_gate(job)
        except Exception as e:
            code = getattr(e, "code", f"L{layer_num}_GATE_FAILURE")
            # Soft gate failures → NEEDS_REVIEW, continue
            if isinstance(e, _soft_failure_types(layer_mod)):
                job.review_flags.append(ReviewFlag(layer=layer_num, reason=str(e)))
                job.status = JobStatus.NEEDS_REVIEW
                print(f"[Pipeline] L{layer_num} soft gate: {code} — continuing")
            else:
                job.status = JobStatus.FAILED
                job.error = JobError(
                    layer=layer_num,
                    error_code=code,
                    message=str(e),
                    traceback=traceback.format_exc(),
                )
                print(f"[Pipeline] L{layer_num} gate FAILED: {code} — {e}")
                return job

        job.layer_timestamps[f"L{layer_num}"] = _now()
        print(f"[Pipeline] L{layer_num} complete ✓")

    if job.status != JobStatus.NEEDS_REVIEW:
        job.status = JobStatus.COMPLETE
    job.updated_at = _now()
    return job


def _soft_failure_types(layer_mod):
    soft = getattr(layer_mod, "SoftGateFailure", None)
    return (soft,) if soft else ()


def _now() -> str:
    return datetime.utcnow().isoformat()
