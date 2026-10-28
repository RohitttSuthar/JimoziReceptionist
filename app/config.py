from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

def _load_dotenv(path: Path) -> None:
    # Small .env loader to keep the project dependency-light.
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

_load_dotenv(ROOT / ".env")

class Settings:
    product_id = os.getenv("PRODUCT_ID", "jimozi")
    products_dir = Path(os.getenv("PRODUCTS_DIR", str(ROOT / "products")))
    if not products_dir.is_absolute():
        products_dir = ROOT / products_dir
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    openai_api_key = os.getenv("OPENAI_API_KEY", "")
    admin_username = os.getenv("ADMIN_USERNAME", "")
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    trust_proxy_headers = os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true"
    rate_limit_per_minute = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
    admin_rate_limit_per_minute = int(os.getenv("ADMIN_RATE_LIMIT_PER_MINUTE", "20"))
    log_transcripts = os.getenv("LOG_TRANSCRIPTS", "false").lower() == "true"
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None  # any OpenAI-compatible provider (Groq, Ollama, ...)
    openai_embedding_model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small").strip()
    enable_grounded_llm_rewrite = os.getenv("ENABLE_GROUNDED_LLM_REWRITE", "false").lower() == "true"  # legacy setting
    enable_llm_answers = os.getenv("ENABLE_LLM_ANSWERS", "true").lower() == "true"
    enable_query_understanding = os.getenv("ENABLE_QUERY_UNDERSTANDING", "true").lower() == "true"  # LLM works out the user's intent
    openai_understanding_model = os.getenv("OPENAI_UNDERSTANDING_MODEL", "").strip() or openai_model
    enable_llm_generation = os.getenv("ENABLE_LLM_GENERATION", "true").lower() == "true"  # GPT writes the final customer-facing answer by default
    openai_timeout_seconds = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "5.0"))
    openai_max_tokens = int(os.getenv("OPENAI_MAX_TOKENS", "180"))
    llm_max_faqs_context = int(os.getenv("LLM_MAX_FAQS_CONTEXT", "60"))
    llm_min_faqs_context = int(os.getenv("LLM_MIN_FAQS_CONTEXT", "12"))
    llm_min_confidence = float(os.getenv("LLM_MIN_CONFIDENCE", "0.65"))
    llm_min_token_overlap = float(os.getenv("LLM_MIN_TOKEN_OVERLAP", "0.18"))
    semantic_search_enabled = os.getenv("SEMANTIC_SEARCH_ENABLED", "false").lower() == "true"
    semantic_model = os.getenv("SEMANTIC_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    semantic_top_k = int(os.getenv("SEMANTIC_TOP_K", "24"))
    semantic_min_score = float(os.getenv("SEMANTIC_MIN_SCORE", "0.30"))
    max_answer_chars = int(os.getenv("MAX_ANSWER_CHARS", "650"))

    # --- Full-context mode: GPT reads ALL uploaded documents on every question, works out the intent,
    # and answers in English or Hindi. Falls back to the older retrieval pipeline if anything fails.
    enable_full_context = os.getenv("ENABLE_FULL_CONTEXT", "true").lower() == "true"
    openai_full_context_model = os.getenv("OPENAI_FULL_CONTEXT_MODEL", "").strip() or openai_model
    full_context_max_tokens = int(os.getenv("FULL_CONTEXT_MAX_TOKENS", "90000"))   # estimated corpus budget; above it -> retrieval pipeline
    full_context_timeout_seconds = float(os.getenv("FULL_CONTEXT_TIMEOUT_SECONDS", "15"))
    full_context_max_output_tokens = int(os.getenv("FULL_CONTEXT_MAX_OUTPUT_TOKENS", "450"))  # Hindi needs far more tokens than English
    redis_url = os.getenv("REDIS_URL", "")
    session_ttl_seconds = int(os.getenv("SESSION_TTL_SECONDS", "7200"))
    max_local_sessions = int(os.getenv("MAX_LOCAL_SESSIONS", "1000"))
    supertonic_enabled = os.getenv("SUPERTONIC_ENABLED", "true").lower() == "true"
    supertonic_voice = os.getenv("SUPERTONIC_VOICE", "F1")
    supertonic_language = os.getenv("SUPERTONIC_LANGUAGE", "hi")
    supertonic_steps = int(os.getenv("SUPERTONIC_STEPS", "5"))
    supertonic_speed = float(os.getenv("SUPERTONIC_SPEED", "1.15"))
    browser_speech_language = os.getenv("BROWSER_SPEECH_LANGUAGE", "hi-IN")
    browser_tts_fast_path = os.getenv("BROWSER_TTS_FAST_PATH", "true").lower() == "true"
    min_match_score = float(os.getenv("MIN_MATCH_SCORE", "0.16"))
    max_history_turns = int(os.getenv("MAX_HISTORY_TURNS", "6"))
    max_input_chars = int(os.getenv("MAX_INPUT_CHARS", "1000"))
    tts_cache_size = int(os.getenv("TTS_CACHE_SIZE", "128"))

settings = Settings()

class ProductStore:
    """Reloads product JSON on demand; keeps last valid snapshot on invalid edits."""
    def __init__(self, products_dir: Path):
        self.products_dir = products_dir
        self._cache: dict[str, dict[str, Any]] = {}
        self._mtimes: dict[str, int] = {}
        self.last_reload_error: dict[str, str] = {}

    def _paths(self, product_id: str) -> Path:
        # Product IDs are deliberately restrictive to prevent path traversal.
        if not product_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in product_id):
            raise ValueError("Invalid product_id")
        return self.products_dir / product_id / "config.json"

    def get(self, product_id: str) -> dict[str, Any]:
        config_path = self._paths(product_id)
        if not config_path.exists():
            raise FileNotFoundError(f"Product '{product_id}' needs config.json in {config_path.parent}")
        stamp = config_path.stat().st_mtime_ns
        if product_id in self._cache and self._mtimes.get(product_id) == stamp:
            return self._cache[product_id]
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                raise ValueError("Product config must contain a JSON object")
            snapshot = {"config": config, "faqs": [], "product_id": product_id, "version": str(stamp)}
            self._cache[product_id] = snapshot
            self._mtimes[product_id] = stamp
            self.last_reload_error.pop(product_id, None)
            return snapshot
        except Exception as exc:
            self.last_reload_error[product_id] = str(exc)
            if product_id in self._cache:
                return self._cache[product_id]
            raise

    def list_products(self) -> list[str]:
        if not self.products_dir.exists():
            return []
        return sorted(p.name for p in self.products_dir.iterdir() if p.is_dir() and (p / "config.json").exists())