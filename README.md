# LifeManager

Personal project, task, routine and goal tracker.

## Stack

- **Backend:** Python + FastAPI
- **UI:** Jinja2 server-rendered pages
- **Interactions:** htmx + Alpine.js
- **Database:** SQLite locally (`backend/lifemanager.db`), Cloudflare D1 in production

No frontend framework and no separate build step.

## Run

```bash
cd backend
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. The first account becomes admin.

The database schema is created/upgraded automatically on startup.

## Deploy to Cloudflare

Dockerized and deployed via [Cloudflare Containers](https://developers.cloudflare.com/containers/)
with **Cloudflare D1** as the database. See **[DEPLOY.md](DEPLOY.md)**.

- `Dockerfile` — container image (FastAPI on port 8080)
- `wrangler.jsonc` + `worker/index.ts` — Worker and Durable Object that proxy requests to the container
- `backend/migrations/*.sql` — raw SQL migrations for D1 (applied on startup in D1 mode)
- `backend/app/d1.py` — D1 REST-API driver; enable with `LIFEMANAGER_DB_DRIVER=d1`

## Tests

```bash
cd backend
uv run python smoke_test.py
```

## Admin

Admins get an `/admin` dashboard with:

- account, project, task, routine and goal totals
- 14-day signup chart
- user search and role/status filters
- user detail pages with usage stats and recent content
- create user, edit email, reset password, promote/demote, enable/disable, delete
- audit log for admin actions

## Goals

- **Measured:** multiple metrics with units, targets and latest/average/best
  comparison
- **Percent:** manual 0–100% progress
- **Staged:** ordered checklists such as HTML → CSS → JavaScript → Tailwind → React
