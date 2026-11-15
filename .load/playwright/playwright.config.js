import { defineConfig } from '@playwright/test'

// Web UI load blueprint for LifeManager (server-rendered FastAPI on :8000).
// Run: start the backend first (.\start.ps1), then from load/playwright:
//   npm install; npx playwright install chromium
//   $env:BASE_URL='http://127.0.0.1:8000'; $env:API_URL='http://127.0.0.1:8000'
//   npm run test:load
//
// workers = browser VUs (parallel contexts). Increase for heavier UI soak.
// retries=0 on purpose: flakes under load are signal, not noise.
export default defineConfig({
  testDir: './tests',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  workers: Number(process.env.PW_WORKERS ?? 4),
  retries: 0,
  reporter: [['list'], ['html', { outputFolder: '../load-report', open: 'never' }], ['json', { outputFile: '../load-report/results.json' }]],
  use: {
    baseURL: process.env.BASE_URL ?? 'http://127.0.0.1:8000',
    trace: 'retain-on-failure',
    video: 'off',
  },
})
