"""L2 — Segmentation: DoclingDocument → StructuredChunks.
"""
from models.schemas import Job
from pipeline.utils.chunk_builder import build_structured_chunks, reconstruct_doc


SOP_CLASSES = {
    "HR_PROCESS": ["employee", "onboarding", "offboarding", "leave", "payroll", "appraisal", "termination"],
    "IT_PROCESS": ["access", "provisioning", "ticket", "incident", "server", "deployment", "credentials"],
    "FINANCE_PROCESS": ["invoice", "expense", "approval", "budget", "reimbursement", "purchase order"],
    "COMPLIANCE_PROCESS": ["audit", "regulatory", "risk", "policy", "violation", "breach"],
    "OPERATIONS_PROCESS": ["production", "quality", "inventory", "dispatch", "warehouse", "shipment"],
}


def run(job: Job) -> None:
    docling_dict = job.extraction.get("docling_document")
    if not docling_dict:
        raise LayerError("L2_NO_DOCLING_DOC", "docling_document missing from extraction output.")

    doc = reconstruct_doc(docling_dict)
    chunks = build_structured_chunks(doc, job.job_id)

    job.chunks = chunks
    job.sop_class = _detect_sop_class(job.extraction.get("markdown", ""))


def validate_gate(job: Job) -> None:
    if not job.chunks:
        raise LayerError("L2_EMPTY_CHUNKS", "No chunks produced from document.")

    content_chunks = [
        c for c in job.chunks
        if c.elements
    ]
    if not content_chunks:
        raise LayerError("L2_NO_CONTENT", "All chunks are empty.")


def _detect_sop_class(text: str) -> str:
    text_lower = text.lower()
    scores = {cls: sum(text_lower.count(kw) for kw in kws) for cls, kws in SOP_CLASSES.items()}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "GENERIC_PROCESS"


class LayerError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
