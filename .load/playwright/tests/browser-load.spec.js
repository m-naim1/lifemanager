// Browser-VU soak across every LifeManager route.
// Mirrors backend/app/web.py: / /projects /projects/:id /tasks /routines /calendar
// Each worker loops ROUTE_REPEATS times to simulate sustained user pressure
// while the Go fasthttp hammer runs against the API in parallel.
//
// The UI is server-rendered and cookie-authenticated, so the spec logs in via
// /api/auth/* (registering the load account on first run) and installs the
// `lm_token` cookie before navigating. Without it every route would redirect
// to /login and the test would measure the login page instead.
//
// Run alongside API load for combined stress:
//   # terminal 1: go run ./cmd/stress -c 100 -duration 120s   (from load/)
//   # terminal 2: npm run test:load                            (from load/playwright)
import { test as base, expect } from '@playwright/test'
import { measureNavigation, expectBudgets } from '../utils/metrics.js'
import { TOKEN_COOKIE, ensureAuth, authHeaders } from '../utils/auth.js'

const REPEATS = Number(process.env.ROUTE_REPEATS ?? 5)
const BASE_URL = process.env.BASE_URL ?? 'http://127.0.0.1:8000'

// One login per test; exposed to the test body and to the cookie setup below.
const test = base.extend({
  apiToken: async ({ request }, use) => {
    await use(await ensureAuth(request))
  },
})

test.describe('web UI load soak', () => {
  test.beforeEach(async ({ context, apiToken }) => {
    await context.addCookies([{ name: TOKEN_COOKIE, value: apiToken, url: BASE_URL }])
  })

  test('all routes stay within budget under repetition', async ({ page, request, apiToken }) => {
    const headers = authHeaders(apiToken)

    // Create a project and goal so their detail routes are always covered,
    // then clean both up.
    const created = await request.post('/api/projects', {
      headers,
      data: { name: `load-browser-${Date.now()}` },
    })
    const projectId = created.ok() ? (await created.json()).id : null

    const createdGoal = await request.post('/api/goals', {
      headers,
      data: {
        title: `load-goal-${Date.now()}`,
        kind: 'measured',
        fields: [{ name: 'Score', unit: 'pts', target_value: 100 }],
      },
    })
    let goalId = null
    if (createdGoal.ok()) {
      const goal = await createdGoal.json()
      goalId = goal.id
      await request.post(`/api/goals/${goalId}/entries`, {
        headers,
        data: { values: [{ field_id: goal.fields[0].id, value: 80 }] },
      })
    }

    try {
      const routes = ['/', '/projects', '/tasks', '/routines', '/goals', '/calendar']
      if (projectId) routes.push(`/projects/${projectId}`)
      if (goalId) routes.push(`/goals/${goalId}`)

      const samples = []
      for (let i = 0; i < REPEATS; i++) {
        for (const path of routes) {
          const m = await measureNavigation(page, path)
          samples.push(m)
          expectBudgets(expect, m)
          // Auth works and the real view rendered - guards against a login
          // redirect or blank-page regression silently passing the budget.
          await expect(page.locator('form[action="/logout"]')).toBeVisible()
          await expect(page.locator('body')).not.toBeEmpty()
        }
      }

      // Aggregate p95 per route into the test log for quick bottleneck reads
      const byRoute = {}
      for (const s of samples) (byRoute[s.path] ??= []).push(s.load)
      for (const [path, arr] of Object.entries(byRoute)) {
        arr.sort((a, b) => a - b)
        const p95 = arr[Math.floor(0.95 * (arr.length - 1))]
        console.log(`route ${path}: n=${arr.length} p50=${arr[Math.floor(arr.length / 2)]}ms p95=${p95}ms`)
      }

      const consoleErrors = samples.flatMap((s) => s.consoleErrors.map((e) => `${s.path}: ${e}`))
      expect(consoleErrors, 'browser console/page errors').toEqual([])
    } finally {
      if (projectId) {
        await request.delete(`/api/projects/${projectId}`, { headers }).catch(() => {})
      }
      if (goalId) {
        await request.delete(`/api/goals/${goalId}`, { headers }).catch(() => {})
      }
    }
  })

  test('home/today API backing the heaviest view stays fast', async ({ request, apiToken }) => {
    // Direct API check from the browser context - catches backend saturation
    // that page-load timings alone would attribute to the web UI.
    const headers = authHeaders(apiToken)
    const lat = []
    for (let i = 0; i < 20; i++) {
      const t0 = Date.now()
      const res = await request.get('/api/home/today', { headers })
      lat.push(Date.now() - t0)
      expect(res.ok()).toBeTruthy()
    }
    lat.sort((a, b) => a - b)
    const p95 = lat[Math.floor(0.95 * (lat.length - 1))]
    console.log(`GET /api/home/today via browser context: p50=${lat[10]}ms p95=${p95}ms`)
    expect(p95).toBeLessThan(2000)
  })
})
