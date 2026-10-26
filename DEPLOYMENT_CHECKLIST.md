# Deployment checklist

- [ ] Python 3.11 environment created and dependencies installed
- [ ] `.env` created locally from `.env.example` and excluded from version control
- [ ] Previously exposed API credentials revoked and replaced
- [ ] Unique long admin password configured; sample placeholder removed
- [ ] HTTPS configured before public access
- [ ] Reverse proxy body-size and request-timeout limits configured
- [ ] `TRUST_PROXY_HEADERS=true` only if a trusted proxy sanitizes forwarded headers
- [ ] Redis configured for multi-worker sessions, if applicable
- [ ] Shared gateway rate limiting configured for multi-worker deployments
- [ ] Backups and restore test cover both SQLite DB and uploaded source documents
- [ ] Labeled Hindi/Hinglish evaluation set reviewed and benchmarked
- [ ] Load tests run against expected concurrency and latency targets
- [ ] Error reporting and monitoring configured without logging raw personal data
- [ ] Rollback procedure tested
