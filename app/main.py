from __future__ import annotations
import asyncio
import json
import logging
import re
import time
import uuid
import secrets
import os
from collections import defaultdict, deque
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Any, Literal
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .config import ProductStore, ROOT, settings
from .documents import knowledge_base, _hindi_ratio
from .services import TTSService
from .sessions import SessionStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("voice_receptionist")
# Print every question and answer in the console. Set CONSOLE_PRINT_CHAT=false in .env to turn this off
# (conversations can contain personal data, so disable it in production).
PRINT_CHAT = os.getenv("CONSOLE_PRINT_CHAT", "true").lower() == "true"
@asynccontextmanager
async def lifespan(app: FastAPI):
    warmup_task = None
    if settings.supertonic_enabled:
        # Warm local TTS in the background without blocking server startup.
        warmup_task = asyncio.create_task(tts.warmup())
        app.state.tts_warmup_task = warmup_task
    try:
        yield
    finally:
        if warmup_task and not warmup_task.done():
            warmup_task.cancel()
        await sessions.close()


app = FastAPI(title="Document-Driven JIMOZI Voice Receptionist", version="3.1.0", lifespan=lifespan)

# CSP: CSS/JS are served as external files from /static, so inline code stays blocked.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data: blob:; "
       "media-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")

# Lightweight per-process request throttling. For multi-worker deployments use a shared
# gateway/Redis limiter; this local guard still protects development and single-worker use.
class RequestGuardMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.hits = defaultdict(deque)
        self.requests_seen = 0

    async def dispatch(self, request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id", str(uuid.uuid4()))[:100]
        client = request.client.host if request.client else "unknown"
        # Only trust forwarded addresses when explicitly configured behind a trusted proxy.
        if settings.trust_proxy_headers and request.headers.get("x-forwarded-for"):
            client = request.headers["x-forwarded-for"].split(",", 1)[0].strip()[:64]
        admin_path = request.url.path == "/admin" or "/documents" in request.url.path or request.url.path.startswith("/api/products")
        limit = settings.admin_rate_limit_per_minute if admin_path else settings.rate_limit_per_minute
        now = time.monotonic()
        self.requests_seen += 1
        if self.requests_seen % 256 == 0:
            stale = [k for k, values in self.hits.items() if not values or now - values[-1] >= 60]
            for stale_key in stale:
                self.hits.pop(stale_key, None)
            # Bound memory if a flood of distinct source addresses is observed.
            while len(self.hits) > 10000:
                self.hits.pop(next(iter(self.hits)), None)
        key = (client, "admin" if admin_path else "public")
        bucket = self.hits[key]
        while bucket and now - bucket[0] >= 60:
            bucket.popleft()
        if len(bucket) >= max(1, limit):
            return JSONResponse(status_code=429, content={"detail": "Too many requests. Please try again shortly.", "request_id": request.state.request_id}, headers={"Retry-After": "60"})
        bucket.append(now)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
        if request.url.path in {"/", "/admin"}:
            response.headers["Content-Security-Policy"] = CSP
        return response

app.add_middleware(RequestGuardMiddleware)
security = HTTPBasic(auto_error=False)

async def require_admin(credentials: HTTPBasicCredentials | None = Depends(security)):
    username = settings.admin_username
    password = settings.admin_password
    if not username or not password:
        raise HTTPException(status_code=503, detail="Admin access is disabled. Configure ADMIN_USERNAME and ADMIN_PASSWORD.")
    valid = bool(credentials and secrets.compare_digest(credentials.username, username) and secrets.compare_digest(credentials.password, password))
    if not valid:
        from fastapi import status
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin authentication required", headers={"WWW-Authenticate": "Basic"})
    return credentials.username
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
store = ProductStore(settings.products_dir)
tts = TTSService()
sessions = SessionStore()

class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    product_id: str | None = Field(default=None, max_length=80)
    session_id: str | None = Field(default=None, max_length=128)
    include_audio: bool = False
    mode: Literal["consumer", "chef"] = "consumer"

class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    voice: str | None = None
    lang: str | None = None

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(ROOT / "static" / "index.html")

@app.get("/admin", response_class=HTMLResponse)
async def admin(_admin: str = Depends(require_admin)):
    return FileResponse(ROOT / "static" / "admin.html")

@app.get("/api/health")
async def health():
    # Liveness endpoint deliberately excludes secrets, product inventory, and config details.
    return {"status": "ok", "version": app.version, "timestamp": datetime.now(timezone.utc).isoformat()}

@app.get("/api/products")
async def products(_admin: str = Depends(require_admin)):
    result = []
    for pid in store.list_products():
        try:
            snap = store.get(pid)
            result.append({"id": pid, "name": snap["config"].get("business", {}).get("name", pid), "version": snap["version"]})
        except Exception as exc:
            log.warning("Could not load product %s: %s", pid, exc)
    return {"products": result}

@app.get("/api/products/{product_id}/faqs")
async def list_faqs(product_id: str, _admin: str = Depends(require_admin)):
    # Backward-compatible endpoint: the document-driven architecture does not require FAQ JSON.
    try:
        snap = store.get(product_id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"product_id": product_id, "version": snap["version"], "faqs": [], "message": "Document-based knowledge is active; upload documents instead of editing FAQs."}

@app.get("/api/products/{product_id}/documents")
async def list_documents(product_id: str, _admin: str = Depends(require_admin)):
    try:
        return {"product_id": product_id, "documents": knowledge_base.list_documents(product_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/products/{product_id}/documents")
async def upload_document(product_id: str, file: UploadFile = File(...), audience: str = Form("both"), _admin: str = Depends(require_admin)):
    # Read in bounded chunks; do not trust Content-Length alone.
    from .documents import MAX_UPLOAD_BYTES
    parts = []
    total = 0
    while True:
        part = await file.read(min(1024 * 1024, MAX_UPLOAD_BYTES + 1 - total))
        if not part:
            break
        total += len(part)
        if total > MAX_UPLOAD_BYTES:
            await file.close()
            raise HTTPException(status_code=413, detail="File is larger than the 25 MB limit")
        parts.append(part)
    data = b"".join(parts)
    try:
        result = await knowledge_base.ingest(product_id, file.filename or "upload.txt", data, audience)
        log.info("DOCUMENT_INGESTED %s", json.dumps({"product_id": product_id, **result}, ensure_ascii=False))
        return {"ok": True, "product_id": product_id, "document": result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        log.exception("Document upload failed")
        raise HTTPException(status_code=500, detail="Document processing failed. Check server logs for details.") from exc

@app.delete("/api/products/{product_id}/documents/{document_id}")
async def delete_document(product_id: str, document_id: str, _admin: str = Depends(require_admin)):
    try:
        deleted = knowledge_base.delete_document(product_id, document_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found")
    log.info("DOCUMENT_DELETED %s", json.dumps({"product_id": product_id, "document_id": document_id}))
    return {"deleted": True, "document_id": document_id}

@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    started = time.perf_counter()
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")
    if len(text) > settings.max_input_chars:
        raise HTTPException(status_code=413, detail=f"Input exceeds {settings.max_input_chars} characters")
    product_id = req.product_id or settings.product_id
    try:
        snap = store.get(product_id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    session_id = req.session_id or str(uuid.uuid4())
    mode = req.mode
    # Privacy by default: log operational metadata, not customer transcripts.
    if settings.log_transcripts:
        log.info("CHAT_QUERY %s", json.dumps({"product_id": product_id, "mode": mode,
                 "session_id_hash": __import__("hashlib").sha256(session_id.encode()).hexdigest()[:12],
                 "query_chars": len(text)}, ensure_ascii=False))
    business = snap["config"].get("business", {})
    policies = dict(snap["config"].get("policies", {}))
    if mode == "chef":
        policies["fallback"] = str((policies.get("mode_fallbacks") or {}).get(
            "chef", "माफ़ कीजिए, Chef से जुड़ी इस जानकारी की पुष्टि अभी उपलब्ध नहीं है। कृपया अपनी टीम से पुष्टि करें।"
        ))
    # Knowledge is extracted from uploaded documents and filtered by the selected audience.
    history_key = f"{mode}:{session_id}"
    history = await sessions.get(history_key)
    answer_started = time.perf_counter()
    result = await knowledge_base.answer(product_id, text, mode, business, history, policies=policies)
    answer_ms = round((time.perf_counter() - answer_started) * 1000, 1)
    answer = result["answer"]
    await sessions.append(history_key, text, answer)
    if PRINT_CHAT:
        src = ", ".join(f"{x.get('filename') or x.get('source')}" + (f" p{x['page']}" if x.get("page") else "") for x in result.get("sources", [])[:3]) or "-"
        print(f"\n{'=' * 70}\n[{mode.upper()}] QUESTION : {text}\n[{mode.upper()}] ANSWER   : {answer}\n"
              f"  intent={result.get('intent')}  understood_as={result.get('rewritten_query')}\n  mode={result.get('answer_mode')}  score={result.get('match_score')}  time={answer_ms}ms  sources={src}\n{'=' * 70}", flush=True)
    response: dict[str, Any] = {
        "session_id": session_id, "product_id": product_id, "mode": mode,
        "product_name": business.get("name", product_id), "transcript": text,
        **result, "knowledge_version": snap["version"],
        "latency_ms": {"answer_generation": answer_ms, "total_before_tts": round((time.perf_counter() - started) * 1000, 1)}
    }
    # The voice must match the script of the answer actually produced (English or Hindi).
    response["answer_language"] = "hi" if _hindi_ratio(answer) >= 0.3 else "en"
    if req.include_audio:
        audio_started = time.perf_counter()
        audio = await tts.synthesize(answer, lang=response["answer_language"])
        response["audio"] = audio
        response["latency_ms"]["tts"] = round((time.perf_counter() - audio_started) * 1000, 1)
    response["latency_ms"]["total_server"] = round((time.perf_counter() - started) * 1000, 1)
    # Keep logs compact and readable; intentionally exclude optional audio data.
    log.info("CHAT_RESPONSE %s", json.dumps({
        "request_id": getattr(request.state, "request_id", None),
        "product_id": product_id,
        "mode": mode,
        "session_id_hash": __import__("hashlib").sha256(session_id.encode()).hexdigest()[:12],
        "query_chars": len(text),
        "answer_chars": len(answer),
        "source_count": len(result.get("sources", [])),
        "answer_mode": result.get("answer_mode"),
        "intent": result.get("intent"),
        "latency_ms": response["latency_ms"],
    }, ensure_ascii=False, default=str))
    return response

@app.post("/api/session/{session_id}/clear")
async def clear_session(session_id: str):
    # Clear both isolated mode histories and legacy sessions.
    await sessions.clear(session_id)
    await sessions.clear(f"consumer:{session_id}")
    await sessions.clear(f"chef:{session_id}")
    return {"cleared": True, "session_id": session_id}

@app.post("/api/speak")
async def speak(req: SpeakRequest):
    if len(req.text.strip()) > 1000:
        raise HTTPException(status_code=413, detail="Text exceeds the 1000 character limit")
    if req.voice and not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", req.voice):
        raise HTTPException(status_code=400, detail="Invalid voice identifier")
    if req.lang and req.lang not in {"hi", "en", "es", "fr", "pt", "ko"}:
        raise HTTPException(status_code=400, detail="Unsupported language identifier")
    result = await tts.synthesize(req.text.strip(), req.voice, req.lang)
    if result.get("error"):
        raise HTTPException(status_code=503, detail=f"TTS failed: {result['error']}")
    if result.get("disabled"):
        raise HTTPException(status_code=503, detail="Supertonic is disabled in configuration")
    return result

@app.get("/api/config")
async def public_config():
    try:
        snap = store.get(settings.product_id)
        business = snap["config"].get("business", {})
    except Exception:
        business = {"name": settings.product_id}
    return {"default_product_id": settings.product_id, "default_product_name": business.get("name", settings.product_id),
            "browser_speech_language": settings.browser_speech_language,
            "browser_tts_fast_path": settings.browser_tts_fast_path,
            "supertonic_enabled": settings.supertonic_enabled,
            "supertonic_voice": settings.supertonic_voice,
            "supertonic_language": settings.supertonic_language,
            "greeting": business.get("greeting", "नमस्ते! मैं आपकी ग्राहक सहायता रिसेप्शनिस्ट हूँ। बताइए, मैं आपकी क्या मदद कर सकती हूँ?"),
            "llm_enabled": settings.enable_llm_answers and bool(settings.openai_api_key),
            "full_context": settings.enable_full_context and settings.enable_llm_answers and bool(settings.openai_api_key),
            "supported_modes": ["consumer", "chef"],
            "strict_faq_only": False,
            "knowledge_mode": "uploaded_documents",
            "supported_document_types": ["pdf", "docx", "txt", "md", "csv"]}