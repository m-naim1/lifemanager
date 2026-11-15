# LifeManager — Stress Load Blueprint (Go fasthttp + Playwright)

Two complementary layers. Run them separately for baselines, together for worst-case soak.

```
                    ┌──────────────────────────────┐
                    │        LifeManager           │
                    │  FastAPI :8000 + SQLite      │
                    │  Jinja2 + htmx + Alpine UI   │
                    └──────────────┬───────────────┘
              ┌────────────────────┼────────────────────┐
              │ API hammer         │                    │ Browser VUs
              │ Go + fasthttp      │                    │ Playwright
              │ load/cmd/stress    │                    │ load/playwright
              │ 50–500 goroutines  │                    │ 4–16 contexts
              │ stateless, pooled  │                    │ real Chromium,
              │ req/res reuse      │                    │ JS+render+API
              └────────────────────┴────────────────────┘
```

## 1. Layer A — API hammer (Go + fasthttp)

**Why fasthttp, not net/http:** `AcquireRequest/ReleaseResponse` pooling =
near-zero alloc per request; `MaxConnsPerHost` tuned to VU count; keeps the
*load generator* from becoming the bottleneck before uvicorn/SQLite does.

**Location:** `load/cmd/stress/main.go` · `load/go.mod`

**Workload mix** (mirrors `backend/app/web.py` + `backend/smoke_test.py`):

| Scenario | Weight | Endpoint | Why |
|---|---|---|---|
| today | 30% | `GET /api/home/today` | Heaviest — `TodayService` fan-out |
| list-tasks | 15% | `GET /api/tasks` | Core read |
| list-projects/routines | 10+10% | `GET /api/projects`, `/api/routines` | Core reads |
| heatmap | 10% | `GET /api/stats/heatmap?type=tasks` | Compute-heavy |
| calendar/health | 5+5% | `.../calendar?...`, `/api/health` | Periodic + probe |
| write-task | 10% | `POST /api/tasks → complete → DELETE` | Write contention, self-cleaning |
| write-project | 5% | `POST /api/projects → subtask → DELETE` | Write contention, self-cleaning |

**Phases (recommended):**

1. `warmup` — `-c 10 -n 1000` — JIT/DB-page-cache warm, baseline p50/p95.
2. `ramp` — `-c 100 -duration 60s -ramp 10s` — find knee where p95 cliffs.
3. `sustain` — `-c 200 -duration 5m` — SQLite lock errors (`500`/`ERR:timeout`) surface here.
4. `spike` — burst `-c 500 -n 20000` right after sustain — recovery check.
5. `soak` (nightly) — `-c 100 -duration 30m -out soak.json`.

**Auth:** `/api/*` (except health/login/register) requires a JWT. The hammer
logs in with `-email` / `-password` (env `LM_EMAIL` / `LM_PASSWORD`) and sends
`Authorization: Bearer <token>` on every request. The account is registered on
first run, so the defaults (`load@example.com` / `load-password`) work against
a fresh database; on an existing deployment pass an account you own. Use
`-token` (or `LM_TOKEN`) to skip login entirely.

**Run:**
```powershell
cd load
go mod tidy
go run ./cmd/stress -base-url http://127.0.0.1:8000 -c 50 -n 10000
go run ./cmd/stress -c 100 -duration 60s -ramp 10s -out results.json
# existing account / explicit token
go run ./cmd/stress -email you@example.com -password '...' -c 100 -n 10000
go run ./cmd/stress -token $env:LM_TOKEN -c 100 -n 10000
```

**Read the output:** per-endpoint `mean/p50/p95/p99/max` table. Expected
bottleneck order: `today > heatmap/calendar > write-* (SQLite `aiosqlite`
serializes writes) > plain list reads`. If `write-*` p95 explodes while reads
stay flat → DB lock, not CPU. If everything degrades together → uvicorn
single-worker saturated → consider `--workers N` or Postgres.

## 2. Layer B — Browser VUs (Playwright)

**Why a second layer:** API hammer never executes browser JavaScript, htmx,
or rendering. Playwright measures what users feel: navigation, LCP, console errors.

**Location:** `load/playwright/` (`playwright.config.js`, `tests/browser-load.spec.js`, `utils/metrics.js`, `utils/auth.js`)

**Auth:** the UI is server-rendered and cookie-authenticated. Before each
test the spec calls `/api/auth/login` (registering the load account on first
run), installs the returned JWT as the HttpOnly `lm_token` cookie, and uses
`Authorization: Bearer <token>` for direct API checks. Override the account
with `LM_EMAIL` / `LM_PASSWORD`; use an existing account on a deployment where
signup is disabled.

**What it does:**
- Visits every page route in `backend/app/web.py`: `/`, `/projects`,
  `/projects/:id`, `/tasks`, `/routines`, `/goals`, `/goals/:id`, `/calendar`.
  Project and goal detail fixtures are created for the run and deleted
  afterwards.
- Asserts the authenticated header rendered (no silent `/login` redirect) and
  that the browser emitted no console/page errors.
- Repeats `ROUTE_REPEATS` (default 5) per worker → sustained pressure.
- Records per navigation: `domcontentloaded`, `load`, LCP (PerformanceObserver),
  slowest `/api/*` response, console/page errors.
- Budgets (tune per machine): page `load` < 8000 ms, slowest API < 2000 ms,
  `GET /api/home/today` p95 < 2000 ms over 20 samples.

**Run:**
```powershell
.\start.ps1                                   # API + web UI on :8000
cd load\playwright; npm install; npx playwright install chromium
$env:BASE_URL='http://127.0.0.1:8000'; $env:API_URL='http://127.0.0.1:8000'
# optional: use an existing account (defaults register load@example.com on first run)
$env:LM_EMAIL='you@example.com'; $env:LM_PASSWORD='...'
npm run test:load                             # PW_WORKERS=8 npm run test:load for heavier soak
```

## 3. Combined protocol

```powershell
# terminal 1 — API hammer (from load/)
go run ./cmd/stress -c 100 -duration 120s -ramp 10s -out api.json
# terminal 2 — browser VUs (from load/playwright/)
$env:PW_WORKERS=8; $env:ROUTE_REPEATS=10; npm run test:load
# terminal 3 — watch backend errors / DB size
Get-Content backend\lifemanager.db -Wait  # placeholder: monitor uvicorn logs + .db growth
```

Pass criteria (initial): API `p95(today) < 1000 ms` at 100 VUs, zero `5xx`,
Playwright zero console errors, LCP p95 < 5 s. Tighten after first baseline.

## 4. Files

```
load/
  go.mod                    # module lifemanager/load, fasthttp dep
  cmd/stress/main.go        # hammer: pool, weights, quantiles, JSON out
  playwright/package.json
  playwright/playwright.config.js
  playwright/tests/browser-load.spec.js
  playwright/utils/metrics.js
  playwright/utils/auth.js  # login/register + bearer/cookie helpers
  run.ps1                   # one-shot orchestrator (below)
  README.md                 # this file
```

## 5. Next steps (not in blueprint scope)

- Persist baselines (`results.json` → trend chart); fail CI on p95 regression >20%.
- Multi-user mix (per-VU accounts); per-user SQLite seeding for realistic fan-out.
- Replace ad-hoc quantiles with HdrHistogram for >100k samples.
- Chaos: kill uvicorn mid-soak, assert Playwright surfaces error UI not blank page.
