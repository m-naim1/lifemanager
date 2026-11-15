# Deploying LifeManager to Cloudflare (Containers + D1)

LifeManager ships as a Docker container (the existing Python/FastAPI app) that
Cloudflare runs via [Cloudflare Containers](https://developers.cloudflare.com/containers/).
A small Worker fronts the container and a Durable Object owns its lifecycle.
All data lives in **Cloudflare D1**; the container talks to D1 through the
Cloudflare REST API (`backend/app/d1.py`).

```
Browser ─▶ Worker ─▶ Durable Object ─▶ Container (uvicorn :8080)
                                            │
                                            └─▶ D1 (REST API, https)
```

## Files added for the cloud deployment

| File | Purpose |
| --- | --- |
| `Dockerfile` | Container image (Python 3.12 + uv, app on port 8080, answers `/api/health`) |
| `.dockerignore` | Keeps the Docker build context small |
| `wrangler.jsonc` | Worker + container + D1 configuration |
| `worker/index.ts` | Worker + Durable Object that starts and proxies the container |
| `backend/migrations/*.sql` | Raw SQL migrations for D1 |
| `backend/app/d1.py` | D1 REST-API driver (drop-in for the aiosqlite `Database` class) |
| `backend/app/migrations.py` | Startup migration runner (tracks files in `_schema_migrations`) |

## Prerequisites

- Docker running locally (`docker info` succeeds) — `wrangler deploy` builds
  and pushes the image with Docker.
- Node.js (for wrangler).
- `npm install` in the repo root once, to get wrangler.

## One-time setup

1. **Log in** (this also picks your account ID):

   ```bash
   npx wrangler login
   ```

2. **Create the D1 database:**

   ```bash
   npx wrangler d1 create lifemanager
   ```

   Copy the printed `database_id` into `wrangler.jsonc` — both
   `d1_databases[0].database_id` and `vars.D1_DATABASE_ID` — and your account
   ID into `vars.CLOUDFLARE_ACCOUNT_ID` (visible in the Cloudflare dashboard
   or `npx wrangler whoami`).

3. **Create an API token with D1 Edit permission**
   (dash.cloudflare.com → My Profile → API Tokens → Create Token). Scope it to
   your account and the D1 "Edit" permission, then:

   ```bash
   npx wrangler secret put CLOUDFLARE_API_TOKEN
   npx wrangler secret put JWT_SECRET          # long random string
   # optional: bootstrap an admin on first deploy
   npx wrangler secret put ADMIN_EMAIL
   npx wrangler secret put ADMIN_PASSWORD
   ```

## Deploy

```bash
npx wrangler deploy
```

This uploads the Worker and builds/pushes the container image. On first
request the container boots and, in D1 mode, applies every pending file in
`backend/migrations/*.sql` before serving traffic (tracked in
`_schema_migrations`, so restarts and redeploys are safe and incremental).

Open `https://lifemanager.<your-subdomain>.workers.dev` — the first account
created becomes admin (or use the `ADMIN_EMAIL`/`ADMIN_PASSWORD` bootstrap).
Note: the first request may take ~30–60s while the container provisions.

## Local development

```bash
npx wrangler dev
```

This runs the Worker locally and starts the container locally via Docker.
For local dev, put placeholder secrets in a `.dev.vars` file at the repo root
(see `.dev.vars.example`); D1 runs locally against a wrangler-managed SQLite
file, so no API token calls hit production.

## Schema changes

1. Add a new file `backend/migrations/0004_<name>.sql` with plain SQL
   (single-line `--` comments, one statement per semicolon).
2. Redeploy. The container applies it on startup, or apply manually:

   ```bash
   npx wrangler d1 migrations apply lifemanager --remote
   ```

   Pick **one** mechanism: the startup runner and wrangler keep separate
   tracking tables, so running both would apply the same file twice. If you
   prefer managing migrations only with wrangler, set
   `LIFEMANAGER_AUTO_MIGRATE=false` in `worker/index.ts`'s container env.

## Running the container standalone (no Cloudflare)

```bash
docker build -t lifemanager .
docker run --rm -p 8080:8080 \
  -e LIFEMANAGER_JWT_SECRET=dev-secret lifemanager
```

Without `LIFEMANAGER_DB_DRIVER=d1` it uses a local SQLite file inside the
container (mount a volume if you want it to persist).

## Notes / gotchas

- **D1 is SQLite**, so the app's SQL works unchanged; foreign-key cascades
  are enforced by D1 by default.
- Each SQL statement is one HTTPS round trip to the D1 REST API — fine for a
  personal app; the D1 binding API (Worker-side) would be faster if the app
  were ever ported to run in the Worker itself.
- D1 has no cross-statement transactions via this driver; every statement
  auto-commits. Multi-step repository writes are small and low-risk, but a
  crash mid-write can leave partial data.
- Container restarts are stateless-safe: the app keeps nothing on disk in
  D1 mode, and Cloudflare may stop idle containers after the inactivity
  timeout (30 minutes here; the next request boots it again).
