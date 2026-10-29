import { defineConfig } from '@playwright/test'

// Runs against servers that are already up: `npm run dev` here and the API on
// :8000 (see README.md, "End-to-end tests"). The specs create and delete their
// own projects.
export default defineConfig({
  testDir: 'e2e',
  timeout: 120_000,
  expect: { timeout: 30_000 },
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:5173',
    channel: process.env.E2E_BROWSER_CHANNEL ?? 'chrome',
    trace: 'retain-on-failure',
  },
})
