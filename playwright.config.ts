import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './e2e',
  workers: 1,
  timeout: 45000,
  use: {
    baseURL: 'http://127.0.0.1:8877',
    viewport: { width: 1366, height: 768 },
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'uv run --locked python scripts/browser_server.py',
    url: 'http://127.0.0.1:8877/api/status',
    reuseExistingServer: false,
  },
});
