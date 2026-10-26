# JIMOZI Document-Driven AI Receptionist

FastAPI-based Hindi/Hinglish receptionist with document ingestion, audience-aware retrieval, optional OpenAI embeddings/answer generation, and optional local Supertonic TTS.

## Quick start

1. Use Python 3.11 (recommended) or a compatible Python version.
2. Create and activate a virtual environment.
3. Install dependencies: `pip install -r requirements.txt`.
4. Copy `.env.example` to `.env` and set a unique `ADMIN_USERNAME` and strong `ADMIN_PASSWORD`. Add an OpenAI key only if you want hosted embeddings and grounded answer generation.
5. Run locally: `uvicorn app.main:app --host 127.0.0.1 --port 8000`.
6. Open `/admin`. The browser will prompt for the configured HTTP Basic credentials. Upload PDF, DOCX, TXT, Markdown, or CSV files and select `consumer`, `chef`, or `both`.
7. Open `/` to test the receptionist.

**Never deploy with the sample admin password.** Admin/document endpoints fail closed if credentials are missing. Basic authentication must only be used over HTTPS on a network that is not localhost.

## Behavior

- Uploaded documents are chunked and indexed in SQLite. Uploading a newer file with the same filename replaces the prior indexed version for that product.
- With an OpenAI key, the app can create embeddings and generate concise answers from retrieved excerpts. Without a key, lexical retrieval remains available and the app returns an extracted passage.
- Each document is tagged `consumer`, `chef`, or `both`; the audience filter is applied during retrieval.
- Responses include source metadata and retrieval scores for diagnostics. Scores are heuristic and are not calibrated probabilities.
- The API logs operational metadata by default, not raw transcripts. `LOG_TRANSCRIPTS=true` enables limited query metadata logging; avoid enabling it unless needed and approved.
- `/api/health` is a minimal liveness endpoint. It does not expose product inventory or configuration details.

## Security and operations

See [SECURITY.md](SECURITY.md) for deployment requirements and limitations. The built-in rate limiter is process-local; use a shared proxy/Redis limiter for multiple workers. Session history is also in-memory unless Redis support is added to `app/sessions.py`. Back up the SQLite database and uploaded source files together.

## Tests

Run `PYTHONPATH=. pytest -q`. CI runs tests and Python compilation checks. See `evaluation/README.md` for the benchmark needed to validate answer quality before production rollout.

## Important limitations

This is a hardened MVP, not a claim of production perfection. Scanned/image-only PDFs need OCR. Retrieval thresholds require a representative labeled benchmark. Streaming ASR, streaming TTS playback, shared distributed sessions, centralized rate limiting, and production load testing are not implemented by this codebase and should be added if required by deployment scale.
