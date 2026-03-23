"""L2 — Segmentation: Markdown → Block tree."""
import re
import uuid

from models.schemas import Block, BlockType, Job


SOP_CLASSES = {
    "HR_PROCESS": ["employee", "onboarding", "offboarding", "leave", "payroll", "appraisal", "termination"],
    "IT_PROCESS": ["access", "provisioning", "ticket", "incident", "server", "deployment", "credentials"],
    "FINANCE_PROCESS": ["invoice", "expense", "approval", "budget", "reimbursement", "purchase order"],
    "COMPLIANCE_PROCESS": ["audit", "regulatory", "risk", "policy", "violation", "breach"],
    "OPERATIONS_PROCESS": ["production", "quality", "inventory", "dispatch", "warehouse", "shipment"],
}

HEADING_RE = re.compile(r'^(#{1,6})\s+(.*)')
LIST_RE = re.compile(r'^(\s*)([-*]|\d+[.)])\s+(.*)')


def run(job: Job) -> None:

    markdown = job.extraction["markdown"]
    blocks = []
    heading_stack = []  # (level, block_id)
    current_heading_id = None
    current_heading_path = []

    def new_id():
        return str(uuid.uuid4())[:8]

    def make_block(raw_text, parent_id, heading_path, list_depth=0, list_index=None):
        b = Block(
            block_id=new_id(),
            job_id=job.job_id,
            parent_id=parent_id,
            raw_text=raw_text.strip(),
            heading_path=list(heading_path),
            list_depth=list_depth,
            list_index=list_index,
        )
        return b

    lines = markdown.splitlines()
    para_buffer = []

    def flush_para(parent_id, heading_path):
        nonlocal para_buffer
        text = "\n".join(para_buffer).strip()
        if text:
            b = make_block(text, parent_id, heading_path)
            blocks.append(b)
        para_buffer = []

    for line in lines:
        h_match = HEADING_RE.match(line)
        list_match = LIST_RE.match(line)

        if h_match:
            flush_para(current_heading_id, current_heading_path)
            level = len(h_match.group(1))
            heading_text = h_match.group(2).strip()

            # Pop stack to current level
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()

            parent_id = heading_stack[-1][1] if heading_stack else None
            path = [s[2] for s in heading_stack] + [heading_text]

            b = make_block(heading_text, parent_id, path[:-1])
            b.block_type = BlockType.HEADER
            b.block_type_method = "structural_skip"
            b.block_type_confidence = 1.0
            blocks.append(b)

            heading_stack.append((level, b.block_id, heading_text))
            current_heading_id = b.block_id
            current_heading_path = path

        elif list_match:
            flush_para(current_heading_id, current_heading_path)
            indent = list_match.group(1)
            marker = list_match.group(2)
            text = list_match.group(3).strip()
            depth = len(indent) // 2 + 1
            b = make_block(text, current_heading_id, current_heading_path, list_depth=depth, list_index=marker)
            blocks.append(b)

        elif line.strip() == "":
            flush_para(current_heading_id, current_heading_path)
        else:
            para_buffer.append(line)

    flush_para(current_heading_id, current_heading_path)

    # Build children_ids
    id_map = {b.block_id: b for b in blocks}
    for b in blocks:
        if b.parent_id and b.parent_id in id_map:
            id_map[b.parent_id].children_ids.append(b.block_id)

    job.blocks = blocks
    job.sop_class = _detect_sop_class(markdown)


def validate_gate(job: Job) -> None:
    if len(job.blocks) <= 1:
        raise LayerError("L2_EMPTY_TREE", "Block tree has no nodes.")
    content_blocks = [b for b in job.blocks if b.list_depth >= 1 or (b.block_type != BlockType.HEADER and b.raw_text)]
    if not content_blocks:
        raise LayerError("L2_NO_CONTENT", "No content blocks found.")


def _detect_sop_class(text: str) -> str:
    text_lower = text.lower()
    scores = {cls: sum(text_lower.count(kw) for kw in kws) for cls, kws in SOP_CLASSES.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "GENERIC_PROCESS"


class LayerError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
