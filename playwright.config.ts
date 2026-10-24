import { defineConfig } from '@playwright/test';

const externalBaseUrl = process.env.PLAYWRIGHT_BASE_URL;

export default defineConfig({
  testDir: './tests/generated',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [
    ['list'],
    ['json', { outputFile: 'reports/evidence/playwright-results.json' }],
  ],
  use: {
    baseURL: externalBaseUrl || 'http://127.0.0.1:8090',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  webServer: externalBaseUrl
    ? undefined
    : {
        command: 'python -m uvicorn app.main:app --host 127.0.0.1 --port 8090',
        url: 'http://127.0.0.1:8090/health',
        reuseExistingServer: true,
        timeout: 30_000,
      },
});
