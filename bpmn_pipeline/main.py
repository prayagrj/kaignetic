"""CLI entry point: python main.py <path_to_sop_file>"""
import sys
import uuid
from datetime import datetime

from models.schemas import Job, JobStatus
from pipeline.orchestrator import run_pipeline


def main():
    if len(sys.argv) < 2:
        print("Usage: python main.py <path_to_pdf_or_docx>")
        sys.exit(1)

    source_file = sys.argv[1]
    job_id = str(uuid.uuid4())[:12]

    job = Job(
        job_id=job_id,
        source_file_path=source_file,
        created_at=datetime.utcnow().isoformat(),
    )

    print(f"\n{'='*50}")
    print(f"  PDF → BPMN Pipeline")
    print(f"  Job ID : {job_id}")
    print(f"  File   : {source_file}")
    print(f"{'='*50}\n")

    job = run_pipeline(job)

    print(f"\n{'='*50}")
    print(f"  Status : {job.status.value}")

    if job.status in (JobStatus.COMPLETE, JobStatus.NEEDS_REVIEW):
        outs = job.__dict__.get("output_files") or []
        if outs:
            print(f"  BPMN   : {len(outs)} file(s)")
            for p in outs:
                print(f"           {p}")
        else:
            print(f"  BPMN   : {job.__dict__.get('output_file', 'N/A')}")
        print(f"  Report : {job.__dict__.get('report_file', 'N/A')}")
        sop_meta = job.__dict__.get("sop_outputs") or []
        if sop_meta:
            total_n = sum(m["graph_metadata"]["node_count"] for m in sop_meta)
            total_e = sum(m["graph_metadata"]["edge_count"] for m in sop_meta)
            print(f"  Nodes  : {total_n} (across {len(sop_meta)} SOP(s))")
            print(f"  Edges  : {total_e}")
        else:
            print(f"  Nodes  : 0")
            print(f"  Edges  : 0")

        print(f"\n  LLM Calls ({len(job.llm_call_log)} total):")
        for r in job.llm_call_log:
            cached_tag = "[cached]" if r.cached else ""
            print(f"    L{r.layer} {r.prompt_template}: {r.input_tokens}in/{r.output_tokens}out {r.latency_ms:.0f}ms {cached_tag}")

        if job.review_flags:
            print(f"\n  Review Flags ({len(job.review_flags)}):")
            for f in job.review_flags:
                print(f"    L{f.layer}: {f.reason}")

    if job.status == JobStatus.FAILED and job.error:
        print(f"\n  Error  : [{job.error.error_code}] {job.error.message}")
        if job.error.traceback:
            print(f"\n--- Traceback ---\n{job.error.traceback}")

    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
