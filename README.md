# folin-base

Folin backend: FastAPI + Supabase (Postgres, Storage) + Gemini document analysis.

## Run

```bash
python -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill required values; optional settings and defaults are documented
./venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
```

Schema is applied idempotently from `db/schema/*.sql` at startup.

The API also runs a durable retest-reminder worker. Authenticated native clients register
Expo push tokens at `/api/user/push-token`; Pro users receive privacy-safe reminders at
09:00 or later in their device timezone, at the Daily/Weekly cadence selected in Settings.
Expo delivery tickets and receipts are persisted so retries are deduplicated across API
workers. Set `EXPO_ACCESS_TOKEN` only when enhanced push security is enabled in EAS.

## Verify

```bash
./venv/bin/python -m pytest -q
```

The default suite is local and deterministic: AI calls are stubbed and no separate HTTP
server is contacted. Historical deployment smoke tests are explicitly opt-in because they
use seeded data and can consume quota:

```bash
./venv/bin/python -m pytest -q --run-live
```

## Admin dashboard

Add the administrator's email to `ADMIN_EMAILS`, restart the API, then mint a short-lived
access token with `./venv/bin/python scripts/admin_token.py <email>`. Open `/api/admin` and
paste that token into the dashboard. Metrics are read-only and the API route is admin-gated.

The mobile app lives in [Folin-in/folin-handy](https://github.com/Folin-in/folin-handy).
