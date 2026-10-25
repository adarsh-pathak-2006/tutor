# Deployment

**Live now (demo/staging, D24, D29):** the Django platform runs on Render from `main` using the blueprint in
[`render.yaml`](../../render.yaml) and [`build.sh`](../../build.sh), with Neon Postgres (Singapore).
Hosted URL: `https://tutor-platform-ovlg.onrender.com` (free tier: the first request after idle takes up to a minute).
After each deploy, run the smoke test against it:
`python qa/smoke_test.py --hosted --base https://tutor-platform-ovlg.onrender.com` (from `backend/`). Hosted mode is read-only: it creates no accounts and needs no tester tools (health, HTTPS and security headers, public catalogue, anonymous access refused, tester tools off). The full sign-up → consent → quiz journey runs only locally (`python qa/smoke_test.py`).
The AI service, worker and web app below are still planned.

## Target

| Component | Platform | Notes |
|---|---|---|
| Django platform | Render web service (root directory `backend/`) | `gunicorn config.wsgi` · settings `config.settings.prod` |
| AI service | Render web service (root directory `ai/`) | Reachable only with the service token |
| Worker | Render background worker | Indexing, messages, scheduled deletions |
| Web app | Render (or static hosting) from `web/` | |
| Database | Neon PostgreSQL + pgvector | Direct (non-pooled) connection string; project `tutor`, branch `production`, database `tutor`, role `tutor_owner`, pgvector 0.8.0 |

Render's free Postgres is **not** used: free instances expire after 30 days.

## Build and release (Django)

`build.sh` runs, from `backend/`:

```bash
pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate                  # before traffic switches; a failed build keeps the old version live
python manage.py createcachetable         # the cache holds rate limits and login lockouts
python manage.py seed_reference && python manage.py seed_consent   # reference data + consent text
python manage.py ensure_superuser         # first admin from DJANGO_SUPERUSER_* (free tier has no shell); never changes existing accounts
[ "$TUTOR_DEMO_DATA" = "true" ] && python manage.py seed_demo_catalogue   # demo environments only
```

Start command (render.yaml): `gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 120`

Health check path: `/api/v1/health` (200 when the database answers, 503 otherwise). Render deploys only after GitHub CI
passes (`autoDeployTrigger: checksPass`) and switches traffic only when the new version's health check succeeds.

## Environment variables (production)

| Variable | Example / note |
|---|---|
| `DJANGO_SETTINGS_MODULE` | `config.settings.prod` |
| `DJANGO_SECRET_KEY` | long random string (never reuse dev's) |
| `DATABASE_URL` | Neon **direct** (non-pooled) connection string: hostname without `-pooler`, `sslmode=require`. Direct because migrations run on every deploy and Django uses session-level Postgres features the transaction pooler breaks; with 2 gunicorn workers we need ~2 connections |
| `DJANGO_ALLOWED_HOSTS` | `api.tutor.org.in` (demo: `tutor-platform-ovlg.onrender.com`; a wrong value makes every request `400`) |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://tutor.org.in,https://app.tutor.org.in` |
| `DJANGO_COOKIE_DOMAIN` | `.tutor.org.in` |
| `FRONTEND_URL` | `https://tutor.org.in` (used in approval links) |
| `DJANGO_HSTS_SECONDS` | start at `3600`, raise to `31536000` once HTTPS is proven |
| `TUTOR_TRUSTED_PROXIES` | `1` on Render (the default in prod settings). Wrong value = rate limits keyed on the proxy's IP |
| `DJANGO_SUPERUSER_EMAIL`, `DJANGO_SUPERUSER_PASSWORD` | First admin, created once by `build.sh`. Strong password; change it after first login |
| `TUTOR_DEMO_DATA` | `true` on the demo/staging service only; unset in production |
| `DJANGO_HSTS_PRELOAD` | leave unset for the API host (preload applies to the apex domain) |

## Release checklist

- [ ] CI green on the commit being released (includes `check --deploy`, `pip-audit`, coverage ≥ 90%)
- [ ] Migrations reviewed (no destructive change without a data plan)
- [ ] Deployed to staging and smoke-tested (sign-up → approval → lesson → quiz)
- [ ] Production deploy; health check green; smoke test repeated
- [ ] Rollback plan: redeploy the previous commit; migrations must be backward compatible for one release

## AI service on the hosted demo (D41)

Before any change to the AI service's hosting, rehearse it locally: `ai/rehearse_hosted.sh` runs Render's build
and start commands with the hosted settings and checks health, hidden docs, the service token, lesson indexing from
Django and one real student tutor turn; it ends with `REHEARSAL: PASS` or `REHEARSAL: FAIL`.

One-time set-up, in this order. Passwords and tokens go only into the Neon SQL editor and Render; never into git,
chat or files.

1. **Neon role.** In the Neon SQL editor, connected as the owner role, run (replace `<password>` with a fresh random
   value from `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`):

   ```sql
   CREATE EXTENSION IF NOT EXISTS vector;
   CREATE ROLE tutor_ai WITH LOGIN PASSWORD '<password>';
   GRANT CONNECT ON DATABASE tutor TO tutor_ai;
   CREATE SCHEMA IF NOT EXISTS ai AUTHORIZATION tutor_ai;
   ```

   The role can create and use tables only in schema `ai`; Django's tables in `public` stay out of its reach (M7).
   Build its **direct** (non-pooled) connection string: the owner's string with `tutor_ai:<password>` as user and
   password.
2. **Render service `tutor-ai`.** It is defined in `render.yaml` (the Blueprint), so merging it to `main` makes
   Render create the service (Blueprint → sync). Then open `tutor-ai` → Environment and set the two secret values:
   `AI_DATABASE_URL` (step 1) and `TUTOR_AI_SERVICE_TOKEN` (a new random value, ≥ 32 characters), and deploy. Until
   they are set its first deploy fails, which is expected. `https://<service>.onrender.com/health` then answers
   `{"ok": true, "db": true, "provider": "mock", "provider_ok": true}`.
3. **Django.** On `tutor-platform` add `TUTOR_AI_URL` (the https URL from step 2) and the same
   `TUTOR_AI_SERVICE_TOKEN`. The redeploy runs `sync_ai_index`, which indexes every published lesson: the deploy log
   shows `Reconciled: … published, 0 indexed, … queued` and `Sent: … done`.
4. **Check.** As an enrolled demo student, start a tutor chat and send a message (TC-AIT-01). The first message after
   the AI service has slept can take up to a minute.

Rotating the token: set `TUTOR_AI_SERVICE_TOKEN_PREVIOUS` on `tutor-ai` to the old value and a new
`TUTOR_AI_SERVICE_TOKEN` on both services; remove `…_PREVIOUS` after both have redeployed.
