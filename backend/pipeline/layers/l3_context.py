"""L3 — Context: canonicalize actors found during atomization.

Runs after L4.
Takes the actor_heading_map from ProcessModel (populated by L4),
sends to LLM for canonicalization and alias resolution,
then remaps unit.actor to the canonical name on all atomic units.
"""
import json

from llm.client import LLMClient
from llm.prompts import CANONICALIZE_ACTORS_SYSTEM, CANONICALIZE_ACTORS_USER
from models.schemas import Actor, ActorRegistry, ContextIndex, Job


def run(job: Job) -> None:
    llm = LLMClient(job)

    # 1. Gather combined actor_heading_map from all processes
    combined_actor_map = {}
    for process in job.processes:
        if not hasattr(process, 'actor_heading_map'):
            continue
        for actor, headings in process.actor_heading_map.items():
            if actor not in combined_actor_map:
                combined_actor_map[actor] = set()
            combined_actor_map[actor].update(headings)

    # Convert sets to lists for JSON serialization
    actor_map_json_ready = {
        actor: list(headings)
        for actor, headings in combined_actor_map.items()
    }

    # 2. Call LLM to canonicalize
    actor_registry = _build_actor_registry(job, llm, actor_map_json_ready)

    job.context_index = ContextIndex(
        job_id=job.job_id,
        actor_registry=actor_registry,
    )

    # 3. Remap unit.actor to canonical names
    _remap_actors(job)


def validate_gate(job: Job) -> None:
    if not job.context_index or not job.context_index.actor_registry:
        raise LayerError("L3_NO_ACTORS", "Actor registry is empty.")
    if not job.context_index.actor_registry.actors:
        raise LayerError("L3_NO_ACTORS", "No actors found in document.")


def _build_actor_registry(job: Job, llm: LLMClient, actor_map: dict) -> ActorRegistry:
    if not actor_map:
        return ActorRegistry(job_id=job.job_id, actors=[Actor(canonical_name="Unknown")])

    result = llm.call(
        layer=3,
        template_name="CANONICALIZE_ACTORS",
        system_prompt=CANONICALIZE_ACTORS_SYSTEM,
        user_prompt=CANONICALIZE_ACTORS_USER.format(
            actor_heading_map_json=json.dumps(actor_map, indent=2),
        ),
    )

    actors = []
    if result and isinstance(result, list):
        for item in result:
            name = (item.get("canonical_name") or "").strip()
            if name:
                actors.append(Actor(
                    canonical_name=name,
                    aliases=item.get("aliases", []),
                    source_method="llm_canonicalization",
                ))

    if not actors:
        # Fallback if LLM fails: just make every extracted actor its own canonical name
        actors = [Actor(canonical_name=k) for k in actor_map.keys()]

    return ActorRegistry(job_id=job.job_id, actors=actors)


def _remap_actors(job: Job) -> None:
    """Remap unit.actor to canonical names."""
    registry = job.context_index.actor_registry
    if not registry:
        return

    for process in job.processes:
        # Remap any found actors
        for unit in process.atomic_units:
            if unit.actor and unit.actor != "Unknown":
                canonical = registry.find_canonical(unit.actor)
                if canonical:
                    unit.actor = canonical

        # Fallback: if actor is missing, try to inherit from a previous step
        unit_map = {u.unit_id: u for u in process.atomic_units}
        
        # We need to resolve topologically or iteratively until steady state
        changed = True
        while changed:
            changed = False
            for unit in process.atomic_units:
                if not unit.actor or unit.actor == "Unknown":
                    # Look at prev_step_ids
                    for prev_id in unit.prev_step_ids:
                        prev_u = unit_map.get(prev_id)
                        if prev_u and prev_u.actor and prev_u.actor != "Unknown":
                            unit.actor = prev_u.actor
                            changed = True
                            break


class LayerError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class SoftGateFailure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
