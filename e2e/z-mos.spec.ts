import { test, expect } from '@playwright/test';

test('多维 MOS：创建、指定参考、完整评分、刷新锁定与结果导出', async ({ page }) => {
  // 可在全新服务单独运行；本用例使用独立合成任务。
  await page.goto('/');
  const status = await (await page.request.get('/api/status')).json();
  if (status.needs_setup) await page.getByLabel('初始化密钥').fill('browser-test-setup-key');
  await page.getByLabel('账号', { exact: true }).fill('浏览器测试');
  await page.getByLabel('密码', { exact: true }).fill('test-password-only');
  await page
    .getByRole('button', { name: status.needs_setup ? '创建并进入' : '进入工作台', exact: true })
    .click();
  await page.getByRole('button', { name: '新建评测', exact: true }).click();
  await page.locator('input[name="title"]').fill('多维 MOS 浏览器验收');
  await page.locator('select[name="mode"]').selectOption('blind');
  await page.locator('select[name="evaluation"]').selectOption('mos');
  const created = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/tasks',
  );
  await page.getByRole('button', { name: '创建任务', exact: true }).click();
  const creationResponse = await created;
  expect(creationResponse.status()).toBe(200);
  const task = await creationResponse.json();
  // 从运行时合成演示取 WAV，真实上传到新建的 MOS 任务。
  const demo = await (await page.request.post('/api/demo')).json();
  const demoTask = await (await page.request.get(`/api/tasks/${demo.id}`)).json();
  const demoSample = await (
    await page.request.get(`/api/samples/${demoTask.samples[0].id}`)
  ).json();
  const wav = await (await page.request.get(`/api/audio/${demoSample.tracks[0].id}`)).body();
  const sample = await (
    await page.request.post(`/api/tasks/${task.id}/samples`, { data: { name: 'MOS 合成片段' } })
  ).json();
  for (const name of ['音色参考素材', 'MOS 候选甲', 'MOS 候选乙']) {
    const result = await page.request.post(`/api/samples/${sample.id}/tracks`, {
      multipart: {
        name,
        version: '合成测试',
        file: { name: 'generated.wav', mimeType: 'audio/wav', buffer: wav },
      },
    });
    expect(result.ok()).toBeTruthy();
  }
  await page.reload();
  await page.locator('.task-card').filter({ hasText: '多维 MOS 浏览器验收' }).click();
  await page.getByLabel('指定参考音色').selectOption({ label: '音色参考素材' });
  await expect(page.getByLabel('指定参考音色').locator('option:checked')).toHaveText('参考音色');
  await page.getByRole('button', { name: '发布评测', exact: true }).click();
  await page.getByRole('checkbox', { name: '我也参与评测（提交偏好并计入进度）' }).check();
  await page.locator('input[name="alignment"]').check();
  await page.getByRole('button', { name: '发布并锁定任务' }).click();
  await page.getByRole('button', { name: '多维评分', exact: true }).click();
  await expect(page.locator('.mos-card')).toHaveCount(2);
  await expect(page.getByRole('button', { name: '提交全部评分' })).toBeDisabled();
  const groups = page.getByRole('radiogroup');
  await expect(groups).toHaveCount(8);
  await expect(page.locator('.mos-options input:checked')).toHaveCount(0);
  for (let i = 0; i < 8; i++) await groups.nth(i).locator('input[value="4"]').check();
  await expect(page.getByText('已填 8/8 项', { exact: false })).toBeVisible();
  await page.locator('.notes-panel').evaluate((el) => {
    el.scrollTop = 0;
  });
  await page.screenshot({ path: test.info().outputPath('mos-preview.png'), fullPage: true });
  // 未提交草稿在刷新后恢复。
  await page.reload();
  await page.locator('.task-card').filter({ hasText: '多维 MOS 浏览器验收' }).click();
  await page.getByRole('button', { name: '多维评分', exact: true }).click();
  await expect(page.locator('.mos-options input:checked')).toHaveCount(8);
  await page.getByRole('button', { name: '提交全部评分' }).click();
  await expect(
    page.locator('.mos-panel').getByRole('button', { name: '已提交', exact: true }),
  ).toBeDisabled();
  await page.getByRole('button', { name: '关闭并揭晓', exact: true }).click();
  await page.getByRole('button', { name: '关闭收集并统一揭晓' }).click();
  await page.getByRole('button', { name: '查看结果', exact: true }).click();
  await expect(page.getByRole('heading', { name: '逐片段多维 MOS' })).toBeVisible();
  await expect(page.locator('.mos-result')).toHaveCount(2);
  await expect(page.locator('.mos-result').first()).toContainText('4.00');
  await expect(page.locator('.report')).not.toContainText('无分歧');
});
