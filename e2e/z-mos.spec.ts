import { test, expect } from '@playwright/test';

for (const withReference of [true, false]) {
  test(`多维 MOS：${withReference ? '有参考' : '无参考'}完整评分与结果`, async ({ page }) => {
    const title = `多维 MOS 浏览器验收 ${withReference ? '有参考' : '无参考'}`;
    const candidates = withReference ? 2 : 3;
    const fields = candidates * 4;
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
    await page.locator('input[name="title"]').fill(title);
    await page.locator('select[name="mode"]').selectOption('blind');
    await page.locator('select[name="evaluation"]').selectOption('mos');
    await page.getByLabel('指标名称 1', { exact: true }).fill('降噪表现');
    await page.getByLabel('指标 1 的 1 分说明', { exact: true }).fill('噪声明显干扰');
    await page.getByLabel('指标 1 的 5 分说明', { exact: true }).fill('噪声几乎不可察觉');
    await page.getByLabel('指标名称 2', { exact: true }).fill('音色');
    await page.getByLabel('指标名称 3', { exact: true }).fill('综合表现');
    await page.getByLabel('指标说明 3', { exact: true }).fill('整体听感是否满意？');
    await page.getByLabel('指标名称 4', { exact: true }).fill('语音可懂度');
    await page.getByRole('button', { name: '增加评测指标', exact: true }).click();
    await page.getByLabel('指标名称 5', { exact: true }).fill('临时指标');
    await page.getByRole('button', { name: '上移指标 5', exact: true }).click();
    await expect(page.getByLabel('指标名称 4', { exact: true })).toHaveValue('临时指标');
    await page.getByRole('button', { name: '删除指标 4', exact: true }).click();
    await expect(page.getByLabel('指标名称 4', { exact: true })).toHaveValue('语音可懂度');
    await page
      .locator('.mos-config-card')
      .first()
      .screenshot({ path: test.info().outputPath('mos-config-preview.png') });

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
    await page.locator('.task-card').filter({ hasText: title }).click();
    await page.getByLabel('指定参考音色').selectOption({ label: '音色参考素材' });
    await expect(page.getByLabel('指定参考音色').locator('option:checked')).toHaveText('参考音色');
    if (!withReference) {
      await page.getByLabel('指定参考音色').selectOption('');
      await expect(page.getByLabel('指定参考音色')).toHaveValue('');
    }
    await page.getByRole('button', { name: '多维评分', exact: true }).click();
    await expect(page.locator('.mos-slider input[type=range]').first()).toBeDisabled();
    await page.getByRole('button', { name: '发布评测', exact: true }).click();
    await page.getByRole('checkbox', { name: '我也参与评测（提交偏好并计入进度）' }).check();
    await page.locator('input[name="alignment"]').check();
    await page.getByRole('button', { name: '发布并锁定任务' }).click();
    await page.getByRole('button', { name: '多维评分', exact: true }).click();
    await expect(page.locator('.mos-card')).toHaveCount(candidates);
    await expect(page.getByRole('button', { name: '提交全部评分' })).toBeDisabled();
    const groups = page.locator('.mos-slider input[type=range]');
    await expect(groups).toHaveCount(fields);
    await expect(page.locator('.mos-panel')).toContainText('噪声明显干扰');
    await expect(page.locator('.mos-panel')).toContainText('综合表现');
    await expect(page.getByText('当前分数：未评分', { exact: true })).toHaveCount(fields);
    for (let i = 0; i < fields; i++) {
      await groups.nth(i).focus();
      await groups.nth(i).press('End');
      for (let j = 0; j < 7; j++) await groups.nth(i).press('ArrowLeft');
    }
    const numeric = page.locator('.mos-slider input[type=number]').first();
    await expect(numeric).toHaveValue('4.3');
    await numeric.fill('3.3');
    await expect(groups.first()).toHaveValue('3.3');
    await numeric.fill('3.15');
    await expect(numeric).toHaveAttribute('aria-invalid', 'true');
    await expect(page.getByRole('button', { name: '提交全部评分' })).toBeDisabled();
    await numeric.fill('6');
    await expect(page.getByRole('button', { name: '提交全部评分' })).toBeDisabled();
    await numeric.fill('');
    await expect(page.getByRole('button', { name: '提交全部评分' })).toBeDisabled();
    await numeric.fill('4.3');
    await expect(page.getByText(`已填 ${fields}/${fields} 项`, { exact: false })).toBeVisible();
    await page.locator('.notes-panel').evaluate((el) => {
      el.scrollTop = 0;
    });
    await page.screenshot({ path: test.info().outputPath('mos-preview.png'), fullPage: true });
    // 未提交草稿在刷新后恢复。
    await page.reload();
    await page.locator('.task-card').filter({ hasText: title }).click();
    await page.getByRole('button', { name: '多维评分', exact: true }).click();
    for (let i = 0; i < fields; i++)
      await expect(page.locator('.mos-slider input[type=range]').nth(i)).toHaveValue('4.3');
    await page.getByRole('button', { name: '提交全部评分' }).click();
    await expect(
      page.locator('.mos-panel').getByRole('button', { name: '已提交', exact: true }),
    ).toBeDisabled();
    await page.getByRole('button', { name: '关闭并揭晓', exact: true }).click();
    await page.getByRole('button', { name: '关闭收集并统一揭晓' }).click();
    await page.getByRole('button', { name: '查看结果', exact: true }).click();
    await expect(page.getByRole('heading', { name: '逐片段多维 MOS' })).toBeVisible();
    await expect(page.locator('.review-sample .mos-result')).toHaveCount(candidates);
    await expect(page.locator('.review-sample .mos-result').first()).toContainText('4.30');
    await page.getByText('指标与档位说明（发布时量表）', { exact: true }).click();
    await expect(
      page.locator('.mos-scale-snapshot').getByText('噪声明显干扰', { exact: true }),
    ).toBeVisible();
    await expect(page.locator('.report')).not.toContainText('无分歧');
  });
}
