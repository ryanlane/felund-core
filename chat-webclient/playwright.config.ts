import { defineConfig } from '@playwright/test'

const RELAY_BASE = process.env.FELUND_RELAY_BASE || 'http://127.0.0.1:8765'

export default defineConfig({
  testDir: './tests',
  timeout: 60_000,
  expect: {
    timeout: 15_000,
  },
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:5173/app/',
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
    launchOptions: {
      args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'],
    },
  },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5173',
    url: 'http://127.0.0.1:5173/app/',
    reuseExistingServer: !process.env.CI,
    env: {
      ...process.env,
      VITE_FELUND_API_BASE: RELAY_BASE,
    },
  },
  globalSetup: './tests/global-setup',
  globalTeardown: './tests/global-teardown',
  projects: [
    {
      name: 'chromium',
      use: { browserName: 'chromium' },
    },
  ],
})
