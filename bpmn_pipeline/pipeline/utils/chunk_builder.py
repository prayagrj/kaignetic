"""
chunk_builder — DoclingDocument → list[StructuredChunk]

Walks DoclingDocument.iterate_items() with a heading-level stack.
Each time a new SectionHeaderItem is encountered the current accumulation
is flushed as a StructuredChunk; content items are appended to the
current chunk as ChunkElement objects.

The result is a flat list of chunks where each chunk carries:
  - headings   : the full section breadcrumb ["h1", "h2", ...]
  - elements   : typed items found in that section
  - contextualized : headings breadcrumb + all text (primary LLM input)
  - page_numbers   : deduplicated pages spanned by the chunk

No fine-grained tree is maintained — the heading breadcrumb on every
chunk provides the structural context downstream layers need.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from models.schemas import BlockType, ChunkElement, ElementType, StructuredChunk

if TYPE_CHECKING:
    from docling_core.types.doc import DoclingDocument


_FURNITURE_LABELS = {"page_header", "page_footer"}


def build_structured_chunks(
    doc: "DoclingDocument",
    job_id: str,
) -> list[StructuredChunk]:
    """
    Convert a DoclingDocument into a flat list of StructuredChunks.

    One chunk per section (a section starts at each SectionHeaderItem).
    Content before the first heading goes into an implicit intro chunk.
    Sections that produce no elements are silently dropped.
    """
    try:
        from docling_core.types.doc.document import (
            SectionHeaderItem,
            TextItem,
            TableItem,
            PictureItem,
            ListItem,
            CodeItem,
        )
    except ImportError as e:
        raise ImportError(
            "docling_core is required for chunk building. "
            "Install with: pip install docling"
        ) from e

    chunks: list[StructuredChunk] = []

    # section_stack holds (level, headings_list_so_far)
    # index 0 is an implicit root so pre-heading content has an empty breadcrumb
    section_stack: list[tuple[int, list[str]]] = [(0, [])]

    current_elements: list[ChunkElement] = []
    current_headings: list[str] = []

    def _flush() -> None:
        """Emit the current accumulation as a StructuredChunk if non-empty."""
        if not current_elements:
            return
        text_parts = [e.text for e in current_elements if e.text]
        page_numbers = sorted({e.page_no for e in current_elements if e.page_no is not None})
        breadcrumb = " > ".join(current_headings) if current_headings else ""
        body = " ".join(text_parts)
        contextualized = f"Section: {breadcrumb}\n\n{body}" if breadcrumb else body

        chunks.append(StructuredChunk(
            chunk_id=_new_id(),
            job_id=job_id,
            headings=list(current_headings),
            contextualized=contextualized.strip(),
            elements=list(current_elements),
            page_numbers=page_numbers,
        ))

    def _page(item) -> int | None:
        try:
            return item.prov[0].page_no if item.prov else None
        except Exception:
            return None

    for item, _level in doc.iterate_items():

        # ── Section heading → flush + push new heading context ────────────
        if isinstance(item, SectionHeaderItem):
            _flush()
            current_elements = []

            heading_level = getattr(item, "level", 1)
            heading_text = (item.text or "").strip()

            # Pop the stack back so the new heading is at the right depth
            while len(section_stack) > 1 and section_stack[-1][0] >= heading_level:
                section_stack.pop()

            parent_headings = section_stack[-1][1]
            new_headings = parent_headings + [heading_text]
            section_stack.append((heading_level, new_headings))
            current_headings = new_headings

        # ── Tables ────────────────────────────────────────────────────────
        elif isinstance(item, TableItem):
            meta: dict = {}
            try:
                meta["num_rows"] = item.data.num_rows
                meta["num_cols"] = item.data.num_cols
            except Exception:
                pass
            try:
                if hasattr(item, "export_to_dataframe"):
                    meta["dataframe"] = item.export_to_dataframe()
            except Exception:
                pass
            # Use markdown export for text representation
            text = None
            try:
                text = item.export_to_markdown()
            except Exception:
                pass
            current_elements.append(ChunkElement(
                element_id=item.self_ref,
                element_type=ElementType.TABLE,
                text=text,
                page_no=_page(item),
                metadata=meta,
            ))

        # ── Figures ───────────────────────────────────────────────────────
        elif isinstance(item, PictureItem):
            current_elements.append(ChunkElement(
                element_id=item.self_ref,
                element_type=ElementType.FIGURE,
                text=None,
                page_no=_page(item),
            ))

        # ── List items ────────────────────────────────────────────────────
        elif isinstance(item, ListItem):
            meta = {}
            if hasattr(item, "enumerated"):
                meta["enumerated"] = item.enumerated
            if hasattr(item, "marker"):
                meta["marker"] = item.marker
            current_elements.append(ChunkElement(
                element_id=item.self_ref,
                element_type=ElementType.LIST_ITEM,
                text=(item.text or "").strip() or None,
                page_no=_page(item),
                metadata=meta,
            ))

        # ── Code ──────────────────────────────────────────────────────────
        elif hasattr(item, "__class__") and item.__class__.__name__ == "CodeItem":
            current_elements.append(ChunkElement(
                element_id=item.self_ref,
                element_type=ElementType.CODE,
                text=(item.text or "").strip() or None,
                page_no=_page(item),
            ))

        # ── All other text items ───────────────────────────────────────────
        elif isinstance(item, TextItem):
            label_str = str(getattr(item, "label", "")).lower()
            if label_str in _FURNITURE_LABELS:
                continue
            text = (item.text or "").strip()
            if not text:
                continue
            current_elements.append(ChunkElement(
                element_id=item.self_ref,
                element_type=ElementType.PARAGRAPH,
                text=text,
                page_no=_page(item),
                metadata={"label": label_str} if label_str else {},
            ))

    # Flush the last section
    _flush()
    return chunks


def reconstruct_doc(docling_dict: dict) -> "DoclingDocument":
    """Reconstruct a live DoclingDocument from its exported dict (stored in job.extraction)."""
    from docling_core.types.doc import DoclingDocument
    return DoclingDocument.model_validate(docling_dict)


def _new_id() -> str:
    return str(uuid.uuid4())[:8]
