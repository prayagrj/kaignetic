"""L6b — Variable Linker: pure Python data-flow graph from AtomicUnit inputs/outputs.

No LLM. Walks all AtomicUnits in document order, registers each output variable
with its producer, and maps consumer unit_ids. The resulting job.data_vars list
is consumed by L8 for deterministic edge resolution.
"""
from models.schemas import DataVar, Job


# Variable type inference from name conventions
_BOOL_SUFFIXES = {"approved", "rejected", "valid", "invalid", "complete", "failed",
                  "verified", "checked", "confirmed", "granted", "denied"}


def _infer_type(var_name: str) -> str:
    """Infer DataVar type from naming conventions."""
    lower = var_name.lower().replace("v_", "")
    for suffix in _BOOL_SUFFIXES:
        if lower.endswith(suffix) or lower.startswith(suffix):
            return "bool"
    if any(kw in lower for kw in ("id", "number", "code", "ref")):
        return "id"
    if any(kw in lower for kw in ("count", "total", "amount", "qty")):
        return "count"
    if any(kw in lower for kw in ("data", "form", "record", "document", "report", "file")):
        return "data"
    return "unknown"


def run(job: Job) -> None:
    for process in job.processes:
        registry: dict[str, DataVar] = {}  # var_name → DataVar

        for unit in process.atomic_units:
            # Register outputs (this unit is the producer)
            for var_name in unit.outputs:
                if not var_name or not isinstance(var_name, str):
                    continue
                if var_name not in registry:
                    registry[var_name] = DataVar(
                        name=var_name,
                        var_type=_infer_type(var_name),
                        producer_unit_id=unit.unit_id,
                    )
                else:
                    # Variable re-produced (e.g., loop updates) — keep first producer
                    if registry[var_name].producer_unit_id is None:
                        registry[var_name].producer_unit_id = unit.unit_id

            # Register inputs (this unit is a consumer)
            for var_name in unit.inputs:
                if not var_name or not isinstance(var_name, str):
                    continue
                if var_name not in registry:
                    # Consumer seen before producer — create a placeholder
                    registry[var_name] = DataVar(
                        name=var_name,
                        var_type=_infer_type(var_name),
                        producer_unit_id=None,
                    )
                registry[var_name].consumers.append(unit.unit_id)

        # Flag units whose inputs have no known producer (potential review item)
        unit_map = {u.unit_id: u for u in process.atomic_units}
        for var in registry.values():
            if var.producer_unit_id is None:
                for consumer_uid in var.consumers:
                    consumer = unit_map.get(consumer_uid)
                    if consumer:
                        block_map = {b.block_id: b for b in process.blocks}
                        block = block_map.get(consumer.block_id)
                        if block:
                            block.needs_review = True
                            block.review_reasons.append(
                                f"Input variable '{var.name}' has no known producer in the flow."
                            )

        process.data_vars = list(registry.values())


def validate_gate(job: Job) -> None:
    for process in job.processes:
        # Soft: warn if more than 30% of variables have no producer
        total = len(process.data_vars)
        if total == 0:
            continue  # No variables extracted — L6 may not have found any (acceptable)
        orphaned = [v for v in process.data_vars if v.producer_unit_id is None]
        if (len(orphaned) / total) >= 0.3:
            raise SoftGateFailure(
                "L6B_HIGH_ORPHAN_VAR_RATE",
                f"{len(orphaned)}/{total} variables in {process.name} have no producer unit."
            )


class SoftGateFailure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
