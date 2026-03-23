"""Groq LLM client: disk cache + Langfuse tracing.

Every pipeline layer that calls an LLM (L3 classify, L4 actors/glossary, L5 enrich,
L6 atomize, L8 gateways) uses :meth:`LLMClient.call` only. There are no parallel
uncached code paths — same behavior and caching as :mod:`pipeline.layers.l3_classifier`.

Cache: SHA-256 files under ``config.CACHE_DIR``, keyed by ``LLM_CACHE_VERSION``,
``GROQ_MODEL``, and the exact system + user prompt strings. Set ``LLM_CACHE_ENABLED=0``
to skip read/write (forces live API calls). Change ``LLM_CACHE_VERSION`` or
``GROQ_MODEL`` to invalidate entries after prompt changes.
"""
import hashlib
import json
import os
import time
from typing import Any

from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, SystemMessage
from langfuse import Langfuse

import config
from pipeline.utils.chunker import estimate_tokens_for_messages


def _cache_payload_key(system_prompt: str, user_prompt: str) -> str:
    """Stable hash for cache filename; includes model + version so prompts stay isolated."""
    material = f"{config.LLM_CACHE_VERSION}\n{config.GROQ_MODEL}\n{system_prompt}\n{user_prompt}"
    return hashlib.sha256(material.encode()).hexdigest()


def _read_cache(cache_file: str) -> dict | None:
    if not config.LLM_CACHE_ENABLED:
        return None
    if not os.path.exists(cache_file):
        return None
    try:
        with open(cache_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(cache_file: str, payload: dict) -> None:
    if not config.LLM_CACHE_ENABLED:
        return
    try:
        with open(cache_file, "w") as f:
            json.dump(payload, f)
    except OSError:
        pass


class LLMClient:
    def __init__(self, job: Any):
        self.job = job
        self.model = ChatGroq(
            api_key=config.GROQ_API_KEY,
            model=config.GROQ_MODEL,
            temperature=0,
        )
        # Initialise Langfuse — reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY,
        # and LANGFUSE_HOST from the environment (loaded by config via dotenv).
        self.langfuse = Langfuse(
            public_key=config.LANGFUSE_PUBLIC_KEY,
            secret_key=config.LANGFUSE_SECRET_KEY,
            host=config.LANGFUSE_HOST,
        )
        # One trace per job so all generations are grouped together.
        self._trace = self.langfuse.trace(
            name=f"bpmn-pipeline-{getattr(job, 'job_id', 'unknown')}",
            metadata={"job_id": str(getattr(job, 'job_id', 'unknown'))},
        )

    def call(self, layer: int, template_name: str, system_prompt: str, user_prompt: str) -> Any:
        # Warn when estimated tokens exceed budget
        est_tokens = estimate_tokens_for_messages(system_prompt, user_prompt)
        if est_tokens > config.LLM_MAX_INPUT_TOKENS:
            print(
                f"[LLM][L{layer}] ⚠ {template_name}: estimated {est_tokens} tokens "
                f"(budget {config.LLM_MAX_INPUT_TOKENS}). Consider tightening the chunk."
            )

        cache_key = _cache_payload_key(system_prompt, user_prompt)
        cache_file = os.path.join(config.CACHE_DIR, f"{cache_key}.json")

        cached = _read_cache(cache_file)
        if cached and "result" in cached:
            in_tok = cached.get("input_tokens", 0)
            out_tok = cached.get("output_tokens", 0)
            self._log(layer, template_name, in_tok, out_tok, 0, cached=True)
            # Record cache hit in Langfuse as a zero-latency generation
            self._trace.generation(
                name=f"L{layer}/{template_name}",
                model=config.GROQ_MODEL,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                output=cached["result"],
                usage={"input": in_tok, "output": out_tok, "unit": "TOKENS"},
                metadata={"layer": layer, "cache_hit": True},
            )
            return cached["result"]

        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        parsed = None
        last_err = None

        # Two attempts with exponential back-off (handles Groq rate-limit 429s)
        for attempt in range(2):
            generation = self._trace.generation(
                name=f"L{layer}/{template_name}",
                model=config.GROQ_MODEL,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                metadata={"layer": layer, "attempt": attempt + 1, "cache_hit": False},
            )
            try:
                t0 = time.time()
                response = self.model.invoke(messages)
                latency = (time.time() - t0) * 1000
                content = response.content.strip()

                # Strip markdown fences if present
                if content.startswith("```"):
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]

                parsed = json.loads(content)
                usage = getattr(response, "usage_metadata", {}) or {}
                in_tok = usage.get("input_tokens", 0)
                out_tok = usage.get("output_tokens", 0)

                # End the Langfuse generation with output + token usage
                generation.end(
                    output=parsed,
                    usage={"input": in_tok, "output": out_tok, "unit": "TOKENS"},
                    metadata={"latency_ms": round(latency, 1)},
                )

                _write_cache(
                    cache_file,
                    {"result": parsed, "input_tokens": in_tok, "output_tokens": out_tok},
                )

                self._log(layer, template_name, in_tok, out_tok, latency, cached=False)
                return parsed

            except Exception as e:
                last_err = e
                generation.end(
                    level="ERROR",
                    status_message=str(e),
                )
                if attempt == 0:
                    sleep_secs = 2 ** attempt  # 1s, then 2s
                    print(f"[LLM][L{layer}] {template_name} attempt {attempt+1} failed: {e}. Retrying in {sleep_secs}s…")
                    time.sleep(sleep_secs)

        print(f"[LLM][L{layer}] {template_name} failed after retries: {last_err}")
        return None

    def flush(self):
        """Flush all pending Langfuse events — call at the end of a pipeline run."""
        self.langfuse.flush()

    def _log(self, layer, template_name, in_tok, out_tok, latency, cached):
        from models.schemas import LLMCallRecord
        self.job.llm_call_log.append(LLMCallRecord(
            layer=layer,
            prompt_template=template_name,
            input_tokens=in_tok,
            output_tokens=out_tok,
            latency_ms=round(latency, 1),
            cached=cached,
        ))
