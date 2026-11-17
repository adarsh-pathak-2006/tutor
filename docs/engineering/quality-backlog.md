# Quality backlog

TUTOR is a production product. This page lists every known gap between the current code and industry-standard quality,
found by reviewing the code against the standards in [standards.md](standards.md). Items are closed in order of
priority; nothing here is "optional polish".

Priority: **P1** fix before the next feature milestone · **P2** fix before staging · **P3** fix before public launch.

| # | Area | Gap today | Industry-standard target | Priority |
|---|---|---|---|---|
| Q6 | Testing | Hand-built fixtures repeated across tests | `factory_boy` factories per app; tests split into `tests/` packages by feature | P2 |
| Q7 | Type safety | No static type checking | `mypy` + `django-stubs` in CI, strict on `services.py` and new code | P2 |
| Q9 | Background work | Messages go to an in-process list; no queue, retries or scheduling | Background task queue (Django tasks framework or Celery/RQ) with retries; real SMS/email provider behind an interface | P2 |
| Q10 | Privacy (legal) | 30-day deletion of unapproved accounts and withdrawn-consent data isn't implemented | Scheduled, idempotent deletion jobs with audit entries and tests | P2 |
| Q11 | Observability | Request IDs, global error handler and rotating local logs done; logs are plain text, no error tracking or metrics | JSON log format for the hosted service; error tracking (e.g. Sentry); latency and error metrics | P2 |
| Q14 | Developer workflow | Checks run only via `dev.sh` / CI | `pre-commit` hooks (ruff, formatting, large-file and secret checks) | P3 |
| Q15 | Data integrity | Soft-deleted users can still appear in some queries | Default managers exclude `deleted_at`; explicit `all_objects` for admin/audit | P2 |
| Q16 | Code hygiene | API modules call a private service helper (`_current_consent_text`) | Public service functions only across module boundaries | P3 |
| Q18 | Scale | Rate limits and lockouts use Django's database cache (a read and a write per limited request) | Redis (Render Key Value) as the cache once traffic justifies it; same settings, no code change | P3 |
| Q22 | Tests | `apps.tutor.tests.FailureTests.test_lesson_not_indexed_and_timeouts_have_their_own_messages` failed once in a full backend run (got a plain response instead of a stream) and passed on its own and on the next full run; likely timing on WSL `/mnt/c` | Find the timing dependency (relay deadline or streamed-response path) and make the test deterministic; if it repeats in CI, raise to P1 | P2 |
| Q21 | Performance | Each streamed tutor reply holds one gunicorn sync worker until it finishes (up to the turn timeout), so a few simultaneous chats can starve other requests | Before real students: run Django under ASGI (uvicorn workers) or gevent workers for the streaming endpoint, and load-test tutor turns with Locust | P1 |
| Q20 | Dependencies | `ai/` tests: Starlette 1.7 warns that its TestClient on `httpx` is deprecated in favour of `httpx2` | When FastAPI/Starlette document the replacement, switch the test client and pin it; until then the warning is harmless | P3 |
| Q19 | Security | Throttle state is read-modify-write, so bursts of truly simultaneous requests can slightly exceed a limit | Atomic counters (Redis `INCR` with expiry) when moving to Redis (Q18) | P3 |
| Q17 | Load | Locust scenarios exist (`qa/locustfile.py`) but have never been run on production-like infrastructure | Run against a staging copy of production, record p95 per endpoint against NFR-PERF-1 before launch | P3 |

Closed so far: Q1 (DB pagination), Q2 (N+1 + query-budget tests), Q3 (rate limits + login lockout), Q4 (role-based
publishing and role management), Q5 (coverage gate), Q8 (pip-audit, ruff `S`, Dependabot), Q13 (`check --deploy` in CI), Q12 (OpenAPI contract snapshot checked in CI)
— see decisions D19–D22.

When an item is done, delete its row and record the change in the decision log if it changed a design decision.
