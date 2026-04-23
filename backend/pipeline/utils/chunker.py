"""Shared token-aware chunking utilities for all pipeline layers."""
from typing import Any, Callable, TypeVar

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Fast token count approximation: ~4 chars per token (no external deps)."""
    return max(1, len(text) // 4)


def estimate_tokens_for_messages(system: str, user: str) -> int:
    """Estimate total tokens for a system + user prompt pair."""
    # Add ~20 tokens for message formatting overhead
    return estimate_tokens(system) + estimate_tokens(user) + 20


# ---------------------------------------------------------------------------
# Generic chunker
# ---------------------------------------------------------------------------

def chunk_items(
    items: list[T],
    serialize_fn: Callable[[T], str],
    max_tokens: int,
    overlap: int = 1,
) -> list[list[T]]:
    """
    Split ``items`` into token-bounded chunks.

    Args:
        items: The items to chunk (blocks, atomic units, etc.).
        serialize_fn: Convert an item to its string representation for
                      token estimation purposes.
        max_tokens: Maximum tokens allowed per chunk (estimated).
        overlap: Number of items from the *end* of the previous chunk to
                 prepend to the next chunk as read-only context.  These
                 overlap items are included in the new chunk but already
                 processed — callers must skip them when writing results.

    Returns:
        List of chunks, each chunk being a list of items.
    """
    if not items:
        return []

    chunks: list[list[T]] = []
    current: list[T] = []
    current_tokens = 0

    for item in items:
        item_tokens = estimate_tokens(serialize_fn(item))

        # If a single item exceeds the budget on its own, still include it
        # alone so we never silently skip content.
        if current and current_tokens + item_tokens > max_tokens:
            chunks.append(current)
            # Carry overlap items forward for context continuity
            tail = current[-overlap:] if overlap > 0 else []
            current = list(tail)
            current_tokens = sum(estimate_tokens(serialize_fn(x)) for x in current)

        current.append(item)
        current_tokens += item_tokens

    if current:
        chunks.append(current)

    return chunks


# ---------------------------------------------------------------------------
# Sliding window
# ---------------------------------------------------------------------------

def build_sliding_window(
    items: list[T],
    center_idx: int,
    before: int = 3,
    after: int = 3,
) -> tuple[list[T], list[T], list[T]]:
    """
    Return (before_window, [center_item], after_window) slices.

    All three lists are sub-lists of ``items``; the center item is not
    repeated in the before/after lists.

    Args:
        items: The flat ordered list.
        center_idx: Index of the item to centre the window on.
        before: Max items to include before centre.
        after: Max items to include after centre.

    Returns:
        Tuple of (items_before, [target_item], items_after).
    """
    start = max(0, center_idx - before)
    end = min(len(items), center_idx + after + 1)
    return (
        items[start:center_idx],
        [items[center_idx]],
        items[center_idx + 1 : end],
    )


# ---------------------------------------------------------------------------
# Variable registry helper
# ---------------------------------------------------------------------------

def trim_previous_context(results: list[dict], keep: int = 2) -> list[dict]:
    """
    Keep only the last ``keep`` items from a list of LLM result objects
    to use as ``previous_context``.  This prevents the context from
    growing unboundedly across chunks.
    """
    return results[-keep:] if len(results) > keep else results
