import { defineConfig } from '@playwright/test';
// 端口可用 E2E_PORT 覆盖（默认 8877），避免同一主机上的多套验证互相占用。
const port = Number(process.env.E2E_PORT || 8877);
export default defineConfig({
  testDir: './e2e',
  workers: 1,
  timeout: 45000,
  // Windows runner 上个别 UI 刷新断言存在已证实的时序抖动（删除片段后
  // 列表刷新、模态关闭），自动重试一次；失败仍会以 flaky 标注暴露。
  retries: 2,
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    viewport: { width: 1366, height: 768 },
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'uv run --locked python scripts/browser_server.py',
    url: `http://127.0.0.1:${port}/api/status`,
    reuseExistingServer: false,
    env: { E2E_PORT: String(port) },
  },
});
