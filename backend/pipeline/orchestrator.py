"""Orchestrator: runs all 7 layers sequentially."""
import traceback
from datetime import datetime

from pipeline.layers import (
    l1_extraction, l2_atomizer, l3_context, l4_node_detector,
    l5_edge_detector, l6_process_splitter, l7_dag_resolver,
)
from models.schemas import Job, JobError, JobStatus

from pipeline.utils.debug_utils import save_layer_state

LAYERS = [
    (1, l1_extraction),
    (2, l2_atomizer),          # atomize first — LLM freely names actors per unit
    (3, l3_context),           # then canonicalize actors found across all units
    (4, l4_node_detector),
    (5, l5_edge_detector),     # build edges, wire gateways, LLM reconnect
    (6, l6_process_splitter),  # decide which components are separate processes (heuristic + LLM)
    (7, l7_dag_resolver),      # reachability, cycles, lane assignment, edge dedup
]


def run_pipeline(job: Job) -> Job:
    job.status = JobStatus.RUNNING
    job.created_at = _now()

    for layer_num, layer_mod in LAYERS:
        job.current_layer = layer_num
        job.updated_at = _now()
        print(f"[Pipeline] Running L{layer_num}: {layer_mod.__name__.split('.')[-1]}")
        layer_name = layer_mod.__name__.split('.')[-1]

        # Save input state for debugging
        save_layer_state(job, str(layer_num), layer_name, "input")

        try:
            layer_mod.run(job)
            # Save output state for debugging
            save_layer_state(job, str(layer_num), layer_name, "output")
        except Exception as e:
            code = getattr(e, "code", f"L{layer_num}_ERROR")
            job.status = JobStatus.FAILED
            job.error = JobError(
                layer=layer_num,
                error_code=code,
                message=str(e),
                traceback=traceback.format_exc(),
            )
            # Save failure state for debugging
            save_layer_state(job, str(layer_num), layer_name, "failure")
            print(f"[Pipeline] L{layer_num} FAILED: {code} — {e}")
            return job

        # try:
        #     layer_mod.validate_gate(job)
        # except Exception as e:
        #     code = getattr(e, "code", f"L{layer_num}_GATE_FAILURE")
        #     # Soft gate failures → NEEDS_REVIEW, continue
        #     if isinstance(e, _soft_failure_types(layer_mod)):
        #         job.review_flags.append(ReviewFlag(layer=layer_num, reason=str(e)))
        #         job.status = JobStatus.NEEDS_REVIEW
        #         print(f"[Pipeline] L{layer_num} soft gate: {code} — continuing")
        #     else:
        #         job.status = JobStatus.FAILED
        #         job.error = JobError(
        #             layer=layer_num,
        #             error_code=code,
        #             message=str(e),
        #             traceback=traceback.format_exc(),
        #         )
        #         print(f"[Pipeline] L{layer_num} gate FAILED: {code} — {e}")
        #         return job

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
