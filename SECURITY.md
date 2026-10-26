# Security and deployment checklist

## Before deployment

- Rotate any API key ever included in a shared ZIP or repository. The previously supplied archive contained a `.env` file; this release removes it.
- Copy `.env.example` to `.env`; set unique, long `ADMIN_USERNAME` and `ADMIN_PASSWORD` values. Admin/document-management routes fail closed when credentials are absent.
- Keep `.env`, local databases, logs, and uploaded customer documents out of version control.
- Use HTTPS behind a maintained reverse proxy. Set `TRUST_PROXY_HEADERS=true` only when the application is behind a trusted proxy that overwrites forwarded headers.
- Configure network-level request limits and a shared rate limiter for multi-worker or multi-instance deployments; the built-in limiter is per process.
- Restrict the admin account to trusted operators. Basic authentication must be used only over HTTPS outside localhost.
- Keep the OpenAI key server-side, rotate it periodically, and set usage limits at the provider.
- Back up `data/knowledge.sqlite3` and `data/documents/` together; test restoration regularly.
- Review retention/legal requirements before enabling transcript logging. `LOG_TRANSCRIPTS=false` is the default.

## Known boundaries

- Local in-memory sessions and rate limiting are single-process only. Configure Redis/shared infrastructure for horizontally scaled production.
- SQLite is appropriate for a small single-node deployment, not every high-write or multi-region workload.
- A retrieval score is a heuristic, not a calibrated probability or a guarantee of correctness. Maintain a labeled evaluation set and tune abstention thresholds against it.
- The API cannot guarantee hallucination-free responses. Validate high-impact facts with structured records and regression tests.
- Scanned PDFs need OCR; upload previews and human verification are recommended for tables and complex layouts.
