// Shared timing helpers for frontend load tests.
// Every route test records: navigation (domcontentloaded), full load, and
// LCP via PerformanceObserver, plus the slowest /api/* response seen.

/** Navigate and return page timings in ms. */
export async function measureNavigation(page, path) {
  // Listeners must be attached *before* navigation to catch this page's
  // console errors and API responses.
  const consoleErrors = []
  const onConsole = (m) => { if (m.type() === 'error') consoleErrors.push(m.text()) }
  const onPageError = (e) => consoleErrors.push(String(e))
  page.on('console', onConsole)
  page.on('pageerror', onPageError)

  const apiSlow = { url: '', ms: 0 }
  const onResp = (resp) => {
    try {
      const url = resp.url()
      if (!url.includes('/api/')) return
      // Playwright exposes resource timing on Request, not Response.
      const t = resp.request().timing()
      const ms = t ? t.responseEnd : 0
      if (ms > apiSlow.ms) apiSlow = { url, ms }
    } catch { /* ignore */ }
  }
  page.on('response', onResp)

  const t0 = Date.now()
  await page.goto(path, { waitUntil: 'domcontentloaded', timeout: 30_000 })
  const domContentLoaded = Date.now() - t0

  await page.waitForLoadState('load', { timeout: 30_000 }).catch(() => {})
  const load = Date.now() - t0

  const lcp = await page.evaluate(() => new Promise((resolve) => {
    let done = false
    const finish = (v) => { if (!done) { done = true; resolve(v) } }
    try {
      const obs = new PerformanceObserver((list) => {
        const entries = list.getEntries()
        const last = entries[entries.length - 1]
        if (last) finish(Math.round(last.startTime))
      })
      obs.observe({ type: 'largest-contentful-paint', buffered: true })
      setTimeout(() => finish(-1), 5000) // no LCP (empty state) -> -1
    } catch { finish(-1) }
  }))

  page.off('response', onResp)
  page.off('console', onConsole)
  page.off('pageerror', onPageError)

  // Surface console errors in the assertion message; under load these reveal
  // real breakage (failed htmx swaps, Alpine exceptions, 401 asset loads).
  return { path, domContentLoaded, load, lcp, slowestApi: apiSlow, consoleErrors }
}

/** Budget assertions shared by all route specs. Tune per machine. */
export function expectBudgets(expect, m, { loadP95 = 8000, apiP95 = 2000 } = {}) {
  expect.soft(m.load, `${m.path}: page load`).toBeLessThan(loadP95)
  if (m.slowestApi.ms > 0) {
    expect.soft(m.slowestApi.ms, `${m.path}: slowest API ${m.slowestApi.url}`).toBeLessThan(apiP95)
  }
}
