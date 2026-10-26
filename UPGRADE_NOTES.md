# Upgrade notes — hardened JIMOZI receptionist

## Included in this release

- HTTP Basic authentication protects the admin page and product/document management APIs. Those routes fail closed if `ADMIN_USERNAME` or `ADMIN_PASSWORD` is not configured.
- Per-process rate limiting with bounded in-memory limiter state and `Retry-After` responses.
- Security response headers and request IDs.
- Uploads are read in bounded chunks, file names are normalized, and file type/size validation remains enforced.
- Transcript logging is off by default. Operational logs omit raw questions and answers; optional logging records only limited query metadata.
- Health endpoint no longer exposes product inventory or internal configuration.
- Request session/product IDs have length constraints; TTS voice/language parameters are validated.
- Retrieval rejection uses the configured `MIN_MATCH_SCORE` threshold instead of a hardcoded very permissive threshold.
- Optional Redis-backed shared session history when `REDIS_URL` is configured, with a bounded local fallback.
- Added security/deployment guidance, evaluation dataset guidance, and GitHub Actions CI.
- Removed the `.env` file from the release and added `.env.example` plus `.gitignore`.

## Required operator actions

1. **Immediately revoke and rotate any API key that was present in the earlier archive.** Removing it from this release does not invalidate the old key.
2. Copy `.env.example` to `.env`; set a strong unique admin password and a valid OpenAI key only if required.
3. Run `PYTHONPATH=. pytest -q` and `python -m compileall -q app tests`.
4. Use HTTPS for any non-local deployment. The built-in HTTP Basic authentication is not safe over plain HTTP on a public network.
5. For multiple workers, use Redis for sessions and a shared rate limiter at the reverse proxy/gateway. The included limiter is process-local.
6. Maintain a reviewed accuracy benchmark; unit tests are not proof of production answer accuracy.

## Still not a claim of 10/10 production readiness

The codebase still needs representative answer-quality measurements, real load/concurrency tests, deployment-specific security review, OCR/layout handling for scanned PDFs, streaming ASR/TTS, and operational validation. A source-code change cannot honestly guarantee hallucination-free output or production readiness without these tests and the target deployment environment.
