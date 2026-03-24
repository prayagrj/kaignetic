Let me do thorough research before giving you the implementation plan.Good. I now have a complete and accurate picture of the Docling APIs. Let me build the full implementation plan.Now I have everything I need. Let me build the full implementation plan with diagrams and complete code.

The architecture has three clean layers: the document tree (structural), the block model (your domain enrichments), and the chunking layer (embedding-ready output). Let me show the full picture first, then go module by module.Now the data flow — how a PDF becomes a chunk:Now the full implementation, module by module.

---

## Phase 1 — Dependencies and project structure

```
pip install "docling[chunking]" transformers
```

File layout:

```
document_pipeline/
├── models/
│   ├── content_node.py      # Phase 2 — the tree node
│   └── enriched_chunk.py    # Phase 5 — the final output schema
├── tree/
│   ├── builder.py           # Phase 3 — DoclingDocument → ContentNode tree
│   └── enricher.py          # Phase 4 — attaches your Block metadata
├── chunking/
│   └── pipeline.py          # Phase 5 — HybridChunker integration
└── pipeline.py              # Phase 6 — top-level orchestrator
```

---

## Phase 2 — `ContentNode` (the tree node model)

This replaces your `DocumentNode`. One type, no path strings stored, no redundant fields.

```python
# document_pipeline/models/content_node.py
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generator


class NodeType(str, Enum):
    DOCUMENT  = "document"   # synthetic root
    SECTION   = "section"    # SectionHeaderItem — has a heading level
    PARAGRAPH = "paragraph"  # TextItem (paragraph, caption, footnote, etc.)
    TABLE     = "table"      # TableItem
    LIST      = "list"       # list group container
    LIST_ITEM = "list_item"  # individual ListItem
    FIGURE    = "figure"     # PictureItem
    CODE      = "code"       # CodeItem


@dataclass
class ContentNode:
    # ── Identity ──────────────────────────────────────────────────────────
    node_id: str                 # docling self_ref, e.g. "#/texts/12"
    node_type: NodeType

    # ── Content ───────────────────────────────────────────────────────────
    text: str | None = None      # raw text; None for structural containers
    level: int = 0               # heading depth (1=h1, 2=h2…); 0 for non-headings
    page_no: int | None = None   # from prov[0].page_no when available

    # ── Your enrichment data (populated in Phase 4) ───────────────────────
    # Keep it open so your enrichment pipeline can add any field freely.
    metadata: dict[str, Any] = field(default_factory=dict)

    # ── Tree structure ─────────────────────────────────────────────────────
    # repr=False prevents infinite recursion when printing; compare=False
    # means equality is on content, not pointer identity.
    parent: ContentNode | None = field(
        default=None, repr=False, compare=False
    )
    children: list[ContentNode] = field(
        default_factory=list, repr=False
    )

    # ── Tree operations ────────────────────────────────────────────────────
    def add_child(self, child: ContentNode) -> None:
        child.parent = self
        self.children.append(child)

    def remove_child(self, child: ContentNode) -> None:
        self.children.remove(child)
        child.parent = None

    # ── Computed properties (never stored — always derived) ────────────────
    @property
    def heading_path(self) -> list[str]:
        """Full breadcrumb from root to this node's nearest section ancestors."""
        path: list[str] = []
        node = self.parent
        while node is not None:
            if node.node_type == NodeType.SECTION and node.text:
                path.append(node.text)
            node = node.parent
        return list(reversed(path))

    @property
    def depth(self) -> int:
        """Distance from root (root = 0)."""
        d, node = 0, self.parent
        while node is not None:
            d += 1
            node = node.parent
        return d

    @property
    def is_leaf(self) -> bool:
        return len(self.children) == 0

    # ── Traversal ──────────────────────────────────────────────────────────
    def iter_depth_first(self) -> Generator[ContentNode, None, None]:
        yield self
        for child in self.children:
            yield from child.iter_depth_first()

    def iter_leaves(self) -> Generator[ContentNode, None, None]:
        for node in self.iter_depth_first():
            if node.is_leaf:
                yield node

    def find_by_id(self, node_id: str) -> ContentNode | None:
        for node in self.iter_depth_first():
            if node.node_id == node_id:
                return node
        return None

    def __repr__(self) -> str:
        text_preview = f" {self.text[:40]!r}" if self.text else ""
        return (
            f"ContentNode({self.node_type.value}, "
            f"level={self.level},{text_preview} "
            f"children={len(self.children)})"
        )
```

Key decisions:

- `heading_path` is a `@property` — computed, never stored. The tree IS the truth.
- `metadata` is a plain `dict` — your enrichment pipeline can add anything without changing the schema.
- `parent` is back-linked so traversal in any direction is O(depth), not O(n).

---

## Phase 3 — `build_content_tree()` (Docling → your tree)

This is the core replacement for your current `build_document_tree`. It walks Docling's `iterate_items()` and constructs `ContentNode` instances using the heading level stack pattern.

```python
# document_pipeline/tree/builder.py
from __future__ import annotations
from docling.document_converter import DocumentConverter
from docling_core.types.doc import DoclingDocument
from docling_core.types.doc.document import (
    SectionHeaderItem,
    TextItem,
    TableItem,
    PictureItem,
    ListItem,
    CodeItem,
)
from docling_core.types.doc.labels import DocItemLabel

from document_pipeline.models.content_node import ContentNode, NodeType


# Map Docling's DocItemLabel values to your NodeType
_LABEL_TO_NODE_TYPE: dict[DocItemLabel, NodeType] = {
    DocItemLabel.PARAGRAPH:      NodeType.PARAGRAPH,
    DocItemLabel.TEXT:           NodeType.PARAGRAPH,
    DocItemLabel.CAPTION:        NodeType.PARAGRAPH,
    DocItemLabel.FOOTNOTE:       NodeType.PARAGRAPH,
    DocItemLabel.TABLE:          NodeType.TABLE,
    DocItemLabel.PICTURE:        NodeType.FIGURE,
    DocItemLabel.LIST_ITEM:      NodeType.LIST_ITEM,
    DocItemLabel.CODE:           NodeType.CODE,
}


def convert_document(source: str) -> DoclingDocument:
    """Convert a file path or URL to a DoclingDocument."""
    converter = DocumentConverter()
    return converter.convert(source=source).document


def build_content_tree(doc: DoclingDocument) -> ContentNode:
    """
    Walk DoclingDocument.iterate_items() and build a ContentNode tree.

    The heading level stack ensures sections nest correctly regardless
    of whether the document skips heading levels (e.g. h1 → h3).
    """
    root = ContentNode(node_id="root", node_type=NodeType.DOCUMENT)

    # section_stack[i] holds the currently-open section at heading depth i.
    # section_stack[0] is always root — content with no heading parent
    # attaches here.
    section_stack: list[ContentNode] = [root]

    for item, _level in doc.iterate_items():

        # ── Section headings ───────────────────────────────────────────────
        if isinstance(item, SectionHeaderItem):
            heading_level = item.level  # 1, 2, 3 … from Docling

            # Pop the stack back to the correct parent level.
            # If heading_level=2 and stack is [root, h1, h2], pop to [root, h1].
            while len(section_stack) > heading_level:
                section_stack.pop()

            node = ContentNode(
                node_id=item.self_ref,
                node_type=NodeType.SECTION,
                text=item.text,
                level=heading_level,
                page_no=item.prov[0].page_no if item.prov else None,
            )
            section_stack[-1].add_child(node)
            section_stack.append(node)

        # ── Tables ─────────────────────────────────────────────────────────
        elif isinstance(item, TableItem):
            node = ContentNode(
                node_id=item.self_ref,
                node_type=NodeType.TABLE,
                page_no=item.prov[0].page_no if item.prov else None,
                metadata={
                    "num_rows": item.data.num_rows,
                    "num_cols": item.data.num_cols,
                    # Preserve the raw DataFrame for downstream use
                    "dataframe": item.export_to_dataframe()
                    if hasattr(item, "export_to_dataframe")
                    else None,
                },
            )
            section_stack[-1].add_child(node)

        # ── Pictures / figures ─────────────────────────────────────────────
        elif isinstance(item, PictureItem):
            node = ContentNode(
                node_id=item.self_ref,
                node_type=NodeType.FIGURE,
                page_no=item.prov[0].page_no if item.prov else None,
                metadata={"annotations": [
                    a.model_dump() for a in (item.annotations or [])
                ]},
            )
            section_stack[-1].add_child(node)

        # ── List items ─────────────────────────────────────────────────────
        elif isinstance(item, ListItem):
            node = ContentNode(
                node_id=item.self_ref,
                node_type=NodeType.LIST_ITEM,
                text=item.text,
                page_no=item.prov[0].page_no if item.prov else None,
                metadata={"enumerated": item.enumerated, "marker": item.marker},
            )
            section_stack[-1].add_child(node)

        # ── All other text items ───────────────────────────────────────────
        elif isinstance(item, TextItem):
            # Skip furniture (page headers/footers) — they're structural noise
            if str(item.label) in ("page_header", "page_footer"):
                continue

            node = ContentNode(
                node_id=item.self_ref,
                node_type=_LABEL_TO_NODE_TYPE.get(item.label, NodeType.PARAGRAPH),
                text=item.text,
                page_no=item.prov[0].page_no if item.prov else None,
                metadata={"label": str(item.label)},
            )
            section_stack[-1].add_child(node)

    return root
```

The heading level stack is the critical piece. When Docling gives you a `SectionHeaderItem` with `level=2`, you pop the stack back to depth 1 before pushing the new h2 node. This correctly handles skipped levels, consecutive same-level headings, and deeply nested documents without any path-string bookkeeping.

---

## Phase 4 — `enrich_tree()` (attach your Block metadata)

Your existing enrichment pipeline produces `Block` objects with `block_type`, `resolved_actor`, etc. This phase joins them to the tree by `block_id` / `self_ref`.

```python
# document_pipeline/tree/enricher.py
from __future__ import annotations
from document_pipeline.models.content_node import ContentNode
from models.schemas import Block  # your existing Block pydantic model


def enrich_tree(
    root: ContentNode,
    blocks: list[Block],
) -> ContentNode:
    """
    Attach enrichment data from your Block pipeline to matching ContentNodes.

    The join key is block.block_id  ↔  node.node_id (both are Docling self_refs,
    e.g. "#/texts/12").  Blocks with no matching node are silently skipped —
    they may be furniture items that were filtered during tree building.
    """
    # Build a lookup so the join is O(1) per block
    node_index: dict[str, ContentNode] = {
        node.node_id: node
        for node in root.iter_depth_first()
    }

    for block in blocks:
        node = node_index.get(block.block_id)
        if node is None:
            continue  # furniture or filtered item — expected, not an error

        # Merge the enrichment data into the node's metadata dict.
        # All your existing enrichment fields land here as plain values —
        # no schema change to ContentNode is needed.
        node.metadata.update({
            "block_type":            block.block_type,
            "block_type_confidence": block.block_type_confidence,
            "block_type_method":     block.block_type_method,
            "resolved_actor":        block.resolved_actor,
            "condition_scope":       block.condition_scope,
            "cross_refs":            block.cross_refs,
            "atomic_units":          block.atomic_units,
            "needs_review":          block.needs_review,
            "review_reasons":        block.review_reasons,
        })

    return root
```

This is a pure join — the tree structure never changes, only the `metadata` dicts get populated. If you later add a new field to `Block`, you just add it here; `ContentNode` doesn't need to change.

---

## Phase 5 — Chunking pipeline with `HybridChunker` and `contextualize()`

This is where the chunking happens. The output is `EnrichedChunk` — a `DocChunk` extended with your block metadata indexed by `self_ref`.

```python
# document_pipeline/models/enriched_chunk.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EnrichedChunk:
    """
    A DocChunk extended with your domain enrichment metadata.

    chunk_text        — the raw chunk text (chunk.text from Docling)
    contextualized    — chunk_text prepended with heading breadcrumb
                        (output of chunker.contextualize(chunk))
    headings          — list of section heading strings above this chunk
                        (from chunk.meta.headings, already provided by Docling)
    page_numbers      — de-duplicated page numbers spanned by this chunk
    doc_item_refs     — Docling self_refs for every item in this chunk
    block_enrichments — dict keyed by self_ref → your Block metadata dict
                        (only populated for items that were in your Block list)
    token_count       — token count of the contextualized text
    """
    chunk_text:        str
    contextualized:    str
    headings:          list[str]
    page_numbers:      list[int]
    doc_item_refs:     list[str]
    block_enrichments: dict[str, dict[str, Any]] = field(default_factory=dict)
    token_count:       int = 0
```

```python
# document_pipeline/chunking/pipeline.py
from __future__ import annotations
from typing import Iterator

from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
from docling_core.transforms.chunker.hierarchical_chunker import DocChunk
from docling_core.types.doc import DoclingDocument
from transformers import AutoTokenizer

from document_pipeline.models.content_node import ContentNode
from document_pipeline.models.enriched_chunk import EnrichedChunk


def build_chunker(
    embed_model_id: str = "sentence-transformers/all-MiniLM-L6-v2",
    max_tokens: int = 512,
    merge_peers: bool = True,
) -> HybridChunker:
    """
    Create a HybridChunker aligned to your embedding model's tokenizer.

    Always align the chunker tokenizer to your embedding model — if they
    differ, you may silently truncate during embedding.
    """
    tokenizer = HuggingFaceTokenizer(
        tokenizer=AutoTokenizer.from_pretrained(embed_model_id),
        max_tokens=max_tokens,
    )
    return HybridChunker(tokenizer=tokenizer, merge_peers=merge_peers)


def _extract_block_enrichments(
    chunk: DocChunk,
    enriched_root: ContentNode,
) -> dict:
    """
    For each doc_item in this chunk, look up the matching ContentNode
    and pull out its block enrichment metadata.
    """
    enrichments = {}
    node_index = {
        node.node_id: node
        for node in enriched_root.iter_depth_first()
    }
    for doc_item in chunk.meta.doc_items:
        node = node_index.get(doc_item.self_ref)
        if node and node.metadata:
            enrichments[doc_item.self_ref] = {
                k: v
                for k, v in node.metadata.items()
                # Only include your enrichment fields, not Docling's own fields
                if k in {
                    "block_type", "block_type_confidence", "block_type_method",
                    "resolved_actor", "condition_scope", "cross_refs",
                    "atomic_units", "needs_review", "review_reasons",
                }
            }
    return enrichments


def chunk_document(
    doc: DoclingDocument,
    enriched_root: ContentNode,
    chunker: HybridChunker,
) -> Iterator[EnrichedChunk]:
    """
    Run HybridChunker on the DoclingDocument and yield EnrichedChunks.

    The DoclingDocument is the native input to the chunker (not your tree).
    Your tree is only used to pull enrichment metadata into the output.
    """
    for chunk in chunker.chunk(dl_doc=doc):
        doc_chunk = DocChunk.model_validate(chunk)

        # contextualize() prepends the heading breadcrumb — this is what
        # you send to your embedding model
        contextualized = chunker.contextualize(chunk=doc_chunk)

        # Page numbers from provenance
        page_numbers = sorted({
            prov.page_no
            for item in doc_chunk.meta.doc_items
            for prov in item.prov
        })

        # Heading breadcrumb list (Docling already computes this)
        # chunk.meta.headings is a list of DocItemLabel+text pairs
        headings = [h.text for h in (doc_chunk.meta.headings or [])]

        # Pull your enrichment data for items in this chunk
        block_enrichments = _extract_block_enrichments(doc_chunk, enriched_root)

        # Token count on the contextualized text (what you'll actually embed)
        token_count = chunker.tokenizer.count_tokens(text=contextualized)

        yield EnrichedChunk(
            chunk_text=doc_chunk.text,
            contextualized=contextualized,
            headings=headings,
            page_numbers=page_numbers,
            doc_item_refs=[i.self_ref for i in doc_chunk.meta.doc_items],
            block_enrichments=block_enrichments,
            token_count=token_count,
        )
```

---

## Phase 6 — Top-level orchestrator

```python
# document_pipeline/pipeline.py
from __future__ import annotations
from typing import Iterator

from document_pipeline.tree.builder import convert_document, build_content_tree
from document_pipeline.tree.enricher import enrich_tree
from document_pipeline.chunking.pipeline import build_chunker, chunk_document
from document_pipeline.models.content_node import ContentNode
from document_pipeline.models.enriched_chunk import EnrichedChunk
from models.schemas import Block  # your existing Block type


def process_document(
    source: str,                      # file path or URL
    blocks: list[Block] | None = None, # your pre-computed enrichment blocks
    embed_model_id: str = "sentence-transformers/all-MiniLM-L6-v2",
    max_tokens: int = 512,
) -> tuple[ContentNode, Iterator[EnrichedChunk]]:
    """
    Full pipeline: source file → enriched ContentNode tree + chunk stream.

    Returns both the tree (for inspection, search, rendering) and the
    chunk iterator (for embedding).  The tree is built once and shared.
    """
    # 1. Docling conversion
    doc = convert_document(source)

    # 2. Build the structural tree from Docling's native representation
    root = build_content_tree(doc)

    # 3. Attach your block enrichments if provided
    if blocks:
        enrich_tree(root, blocks)

    # 4. Build chunker and run
    chunker = build_chunker(embed_model_id=embed_model_id, max_tokens=max_tokens)
    chunks = chunk_document(doc=doc, enriched_root=root, chunker=chunker)

    return root, chunks
```

Usage:

```python
from document_pipeline.pipeline import process_document

root, chunk_iter = process_document(
    source="my_sop.pdf",
    blocks=my_existing_blocks,   # list[Block] from your enrichment pipeline
    embed_model_id="sentence-transformers/all-MiniLM-L6-v2",
    max_tokens=512,
)

# Inspect the tree
for node in root.iter_depth_first():
    indent = "  " * node.depth
    print(f"{indent}{node}")

# Consume chunks for embedding
for chunk in chunk_iter:
    # chunk.contextualized is what you embed
    # chunk.headings gives you ["5. Pre-Joining Phase", "5.1 Offer and Documentation"]
    # chunk.block_enrichments gives you block_type/actor/etc. per item
    embed(chunk.contextualized)
    store(chunk)
```

---

## What each phase replaces from your current code

| Current code | Replaced by |
|---|---|
| `DocumentNode` with `heading_path: list[str]` stored | `ContentNode` with `heading_path` as a `@property` |
| `build_document_tree()` keying on path strings | `build_content_tree()` walking `iterate_items()` with a level stack |
| `iter_nodes_with_blocks()` filtering generator | `root.iter_depth_first()` + `root.iter_leaves()` — no filtering needed |
| `blocks` list on `DocumentNode` (conflates structure + content) | Children of `ContentNode` (structure) + `metadata` dict (content) |
| No chunking | `HybridChunker` + `contextualize()` giving token-bounded, breadcrumb-prepended chunks |
| Metadata fragments appearing as siblings | Filtered by `isinstance` check in `build_content_tree()` — furniture skipped at the source |

The single most important change: Docling's `iterate_items()` already gives you items in reading order with heading levels. You don't need to reconstruct the tree from path strings — you walk the stream and maintain a level stack. Everything else follows from that.