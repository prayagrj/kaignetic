"""Shared detection of decision / branching language (L3 heuristics, L8 implicit gateway)."""
import re

DECISION_INLINE = re.compile(
    r"\b(if |check if |depending on |based on |unless |otherwise |in case |"
    r"verify whether |determine if |assess whether )\b",
    re.I,
)
