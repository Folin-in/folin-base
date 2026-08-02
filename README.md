# folin-base

Folin backend: FastAPI + Supabase (Postgres, Storage) + Gemini document analysis.

## Run

```bash
python -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill every value - all env vars are required, no fallbacks
./venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
```

Schema is created automatically at startup. Self-checks: `python sanity.py`, `python gamification.py`, `python ai_agent.py`.

The mobile app lives in [Folin-in/folin-handy](https://github.com/Folin-in/folin-handy).
