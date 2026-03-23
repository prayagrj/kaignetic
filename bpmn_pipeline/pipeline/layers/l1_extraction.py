"""L1 — Document Extraction using Docling."""
import subprocess
from pathlib import Path

from models.schemas import Job, JobStatus


GATE_ERRORS = {
    "L1_EMPTY_OUTPUT": "Markdown output is empty or too short.",
    "L1_CONVERSION_FAILED": "Docling conversion failed.",
}


def run(job: Job) -> None:

    src = job.source_file_path
    ext = Path(src).suffix.lower()

    if ext == ".doc":
        src = _convert_doc_to_docx(src)

    try:
        from docling.document_converter import DocumentConverter
        converter = DocumentConverter()
        result = converter.convert(src)
        markdown = result.document.export_to_markdown()
        docling_doc = result.document.export_to_dict()
    except Exception as e:
        raise LayerError("L1_CONVERSION_FAILED", str(e))

    # Minimal post-processing
    markdown = _collapse_blank_lines(markdown)

    job.extraction = {
        "markdown": markdown,
        "docling_document": docling_doc,
    }


def validate_gate(job: Job) -> None:
    markdown = job.extraction.get("markdown", "")
    if len(markdown.strip()) < 100:
        raise LayerError("L1_EMPTY_OUTPUT", GATE_ERRORS["L1_EMPTY_OUTPUT"])


def _convert_doc_to_docx(src: str) -> str:
    out_dir = str(Path(src).parent)
    subprocess.run(
        ["soffice", "--headless", "--convert-to", "docx", src, "--outdir", out_dir],
        check=True,
    )
    return src.replace(".doc", ".docx")


def _collapse_blank_lines(text: str) -> str:
    import re
    return re.sub(r'\n{3,}', '\n\n', text)


class LayerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
