import os
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# ── Langfuse telemetry ──────────────────────────────────────────────────────
LANGFUSE_PUBLIC_KEY = os.environ["LANGFUSE_PUBLIC_KEY"]
LANGFUSE_SECRET_KEY = os.environ["LANGFUSE_SECRET_KEY"]
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"))

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "outputs")
JOBS_DIR = os.path.join(os.path.dirname(__file__), "jobs")
CACHE_DIR = os.path.join(os.path.dirname(__file__), ".llm_cache")

# Disk cache for LLMClient.call() (L3, L4, L5, L6, L8). Set LLM_CACHE_ENABLED=0 to disable.
_llm_cache_env = os.getenv("LLM_CACHE_ENABLED", "1").strip().lower()
LLM_CACHE_ENABLED = _llm_cache_env not in ("0", "false", "no")
# Bump (or change GROQ_MODEL) to invalidate old cache files after prompt/schema changes.
LLM_CACHE_VERSION = os.getenv("LLM_CACHE_VERSION", "1")

# ── LLM token budgets ───────────────────────────────────────────────────────
# Hard input-token cap per call (leaves ~200 tokens headroom for output).
LLM_MAX_INPUT_TOKENS = 1800

# Number of items carried over as context between consecutive chunks.
LLM_OVERLAP_ITEMS = 1

# ── Layer tuning knobs ──────────────────────────────────────────────────────
# L3 — how many ambiguous blocks to batch per LLM classification call
L3_BATCH_SIZE = 5

# L6 — context window size (blocks before/after the target) for atomization
L6_WINDOW_BLOCKS = 2

# L6 — how many blocks to atomize per LLM call
L6_BATCH_SIZE = 4

# L8 — how many preceding/following atomic-unit labels to show per gateway
L8_GATEWAY_WINDOW = 5

# Legacy — kept for backward compat
MAX_ATOMIZE_BATCH = 50

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(JOBS_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
