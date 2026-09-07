import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import ts from 'typescript';

test('首次使用、合成音播放、片段标注、发布判断和重开', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto('/');
  await page.getByLabel('初始化密钥').fill('browser-test-setup-key');
  await page.getByLabel('账号', { exact: true }).fill('浏览器测试');
  await page.getByLabel('密码', { exact: true }).fill('test-password-only');
  await page.getByRole('button', { name: '创建并进入' }).click();
  await page.getByRole('button', { name: '创建合成音演示' }).click();
  await expect(page.getByRole('button', { name: '播放', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '播放', exact: true }).click();
  await expect
    .poll(async () => Number(await page.getByRole('slider', { name: '播放位置' }).inputValue()))
    .toBeGreaterThan(0.15);
  const before = Number(await page.getByRole('slider', { name: '播放位置' }).inputValue());
  await page.keyboard.press('2');
  await expect(page.locator('.current-label')).toHaveText('试听 B');
  await expect
    .poll(async () => Number(await page.getByRole('slider', { name: '播放位置' }).inputValue()))
    .toBeGreaterThan(before);
  await page.getByRole('button', { name: '暂停', exact: true }).click();
  await page.getByLabel('起点', { exact: true }).fill('3.2');
  await page.getByLabel('终点', { exact: true }).fill('3.8');
  await page.locator('#comment').fill('测试标注：选区中有短暂衰减');
  await page.getByRole('button', { name: '保存标注', exact: true }).click();
  await expect(page.getByText('测试标注：选区中有短暂衰减', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '频谱', exact: true }).click();
  await expect(page.getByText('共用色阶 −100～0 dB')).toBeVisible();
  const footer = await page.locator('.transport').boundingBox();
  expect(footer!.y + footer!.height).toBeLessThanOrEqual(768);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await page.getByRole('button', { name: '发布评测', exact: true }).click();
  await page.getByRole('checkbox').check();
  await page.getByRole('button', { name: '发布并锁定任务' }).click();
  await page.getByRole('button', { name: '偏好判断', exact: true }).click();
  await page.getByRole('button', { name: '无明显差异', exact: true }).click();
  await page.getByRole('button', { name: '提交判断', exact: true }).click();
  await expect(page.getByRole('button', { name: '已提交', exact: true })).toBeVisible();
  await page.reload();
  await page.locator('.task-card').first().click();
  await page.getByRole('button', { name: '偏好判断', exact: true }).click();
  await expect(page.getByRole('button', { name: '已提交', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '关闭并揭晓', exact: true }).click();
  await page.getByRole('button', { name: '关闭收集并统一揭晓' }).click();
  await page.getByRole('button', { name: '查看结果' }).click();
  await expect(page.getByText('1 人已提交', { exact: false })).toBeVisible();
  expect(errors).toEqual([]);
});

test('真实 Web Audio 渲染：原始增益、共同起点、重复轨道切换与非恒等变异', async ({ page }) => {
  const source = ts.transpileModule(fs.readFileSync('src/audio.ts', 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  }).outputText;
  await page.route('**/test-audio.js', (route) =>
    route.fulfill({ body: source, contentType: 'text/javascript' }),
  );
  await page.goto('/');
  const results = await page.evaluate(async () => {
    // 从生产播放器模块导入；期望值由独立输入序列和已知调度延迟计算。
    const { AudioEngine } = await import('/test-audio.js');
    async function render(mutate: boolean) {
      const sr = 16000,
        ctx = new OfflineAudioContext(1, sr, sr);
      const resume = ctx.resume.bind(ctx);
      ctx.resume = async () => {};
      const engine = new AudioEngine(ctx);
      engine.master.gain.value = 1;
      const input = Float32Array.from(
        { length: sr },
        (_, i) => 0.2 * Math.sin(i * 0.123) + 0.1 * Math.cos(i * 0.731),
      );
      const a = ctx.createBuffer(1, sr, sr),
        b = ctx.createBuffer(1, sr, sr);
      a.copyToChannel(input, 0);
      b.copyToChannel(
        mutate ? Float32Array.from(input, (_, i) => input[Math.max(0, i - 1)]) : input,
        0,
      );
      engine.buffers = [a, b];
      await engine.play(0);
      const switching = ctx.suspend(0.25).then(async () => {
        engine.select(1);
        await resume();
      });
      const rendering = ctx.startRendering();
      await switching;
      const output = (await rendering).getChannelData(0);
      let residual = 0;
      for (let i = 560; i < sr; i++)
        residual = Math.max(residual, Math.abs(output[i] - input[i - 560]));
      return residual;
    }
    return { same: await render(false), shifted: await render(true) };
  });
  expect(results.same).toBeLessThanOrEqual(1e-6);
  expect(results.shifted).toBeGreaterThan(0.01);
});

test('版本目录预览、原子导入与草稿候选管理', async ({ page }) => {
  const os = await import('node:os');
  const path = await import('node:path');
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'audio-batch-'));
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  try {
    // 合成 PCM WAV，不读取或提交真实录音。
    const wav = Buffer.alloc(44 + 32000);
    wav.write('RIFF', 0);
    wav.writeUInt32LE(wav.length - 8, 4);
    wav.write('WAVEfmt ', 8);
    wav.writeUInt32LE(16, 16);
    wav.writeUInt16LE(1, 20);
    wav.writeUInt16LE(1, 22);
    wav.writeUInt32LE(16000, 24);
    wav.writeUInt32LE(32000, 28);
    wav.writeUInt16LE(2, 32);
    wav.writeUInt16LE(16, 34);
    wav.write('data', 36);
    wav.writeUInt32LE(32000, 40);
    for (const v of ['v1', 'v2']) {
      fs.mkdirSync(path.join(folder, v, '室内'), { recursive: true });
      fs.writeFileSync(path.join(folder, v, '室内', '001.wav'), wav);
    }
    fs.writeFileSync(path.join(folder, 'v1', '缺失.wav'), wav);
    await page.goto('/');
    await page.getByLabel('账号', { exact: true }).fill('浏览器测试');
    await page.getByLabel('密码', { exact: true }).fill('test-password-only');
    await page.getByRole('button', { name: '进入工作台', exact: true }).click();
    await page.getByRole('button', { name: '新建评测', exact: true }).first().click();
    await page.getByLabel('任务名称').fill('批量导入验收');
    await page.getByRole('button', { name: '创建任务', exact: true }).click();
    await page.getByRole('button', { name: '批量导入', exact: true }).click();
    await page.getByLabel('版本 1 目录').setInputFiles(path.join(folder, 'v1'));
    await page.getByLabel('版本 2 目录').setInputFiles(path.join(folder, 'v2'));
    await expect(page.getByText('匹配预览 · 1 个可导入片段')).toBeVisible();
    await expect(page.getByText('版本 2 缺失')).toBeVisible();
    await page.getByRole('button', { name: '确认匹配并导入' }).click();
    await expect(page.getByText('已导入', { exact: true })).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.locator('.batch-import')).not.toBeVisible();
    await expect(page.locator('.track')).toHaveCount(2);
    await page.getByRole('button', { name: '管理候选 版本 1' }).click();
    await page.getByLabel('版本名称', { exact: true }).fill('自研 v1');
    await page.getByRole('button', { name: '保存修改', exact: true }).click();
    await expect(page.getByRole('button', { name: '管理候选 自研 v1' })).toBeVisible();
    await page.getByRole('button', { name: '管理候选 版本 2' }).click();
    page.once('dialog', (dialog) => dialog.accept());
    await page.getByRole('button', { name: '删除这个候选', exact: true }).click();
    await expect(page.locator('.track')).toHaveCount(1);
    expect(errors).toEqual([]);
  } finally {
    fs.rmSync(folder, { recursive: true, force: true });
  }
});

test('同事获授组织者权限后可上传、删除及恢复自己的任务', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('账号', { exact: true }).fill('浏览器测试');
  await page.getByLabel('密码', { exact: true }).fill('test-password-only');
  await page.getByRole('button', { name: '进入工作台', exact: true }).click();
  await page.getByRole('button', { name: '团队成员', exact: true }).click();
  await page.getByLabel('账号', { exact: true }).fill('同事上传者');
  await page.getByLabel('初始密码').fill('colleague-password');
  await page.getByRole('button', { name: '创建账号', exact: true }).click();
  await page.getByLabel('同事上传者的角色').selectOption('organizer');
  await expect(page.getByLabel('同事上传者的角色')).toHaveValue('organizer');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '退出登录' }).click();
  await page.getByLabel('账号', { exact: true }).fill('同事上传者');
  await page.getByLabel('密码', { exact: true }).fill('colleague-password');
  await page.getByRole('button', { name: '进入工作台', exact: true }).click();
  await expect(page.getByRole('button', { name: '新建评测', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '团队成员', exact: true })).toHaveCount(0);
  await expect(page.getByRole('link', { name: '下载完整备份（含音频）' })).toHaveCount(0);
  await page.getByRole('button', { name: '创建合成音演示' }).click();
  await expect(page.getByRole('button', { name: '批量导入', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '发布评测', exact: true }).click();
  await expect(page.getByText('浏览器测试', { exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '删除任务', exact: true }).click();
  await page.getByLabel('输入完整任务名称确认').fill('合成音试听 · 熟悉工作台');
  await page.getByRole('button', { name: '确认移入回收站' }).click();
  await expect(page.locator('.task-card')).toHaveCount(0);
  await page.getByRole('button', { name: '回收站', exact: true }).click();
  await expect(page.locator('.trash-row')).toHaveCount(1);
  await page.getByRole('button', { name: '恢复任务', exact: true }).click();
  await expect(page.getByText('回收站为空', { exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.locator('.task-card').click();
  await expect(page.locator('.track')).toHaveCount(3);
});

test('角色指南默认入口、章节搜索、完整下载与窄屏阅读', async ({ page }) => {
  await page.request.post('/api/login', {
    data: { name: '浏览器测试', password: 'test-password-only' },
  });
  await page.request.post('/api/users', {
    data: { name: '指南评测者', password: 'guide-password-only', role: 'reviewer' },
  });
  for (const [name, password, role, heading] of [
    ['浏览器测试', 'test-password-only', '管理员', '首次启动与局域网部署'],
    ['同事上传者', 'colleague-password', '组织者', '准备一组公平的比较'],
    ['指南评测者', 'guide-password-only', '评测者', '登录并找到分配的任务'],
  ]) {
    await page.request.post('/api/login', { data: { name, password } });
    await page.goto('/');
    await page.getByRole('button', { name: '使用指南', exact: true }).click();
    await expect(
      page
        .getByRole('group', { name: '选择指南角色' })
        .getByRole('button', { name: new RegExp(role + '.*当前角色') }),
    ).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();
    await page.keyboard.press('Escape');
  }
  await page.getByRole('button', { name: '使用指南', exact: true }).click();
  await page
    .getByRole('group', { name: '选择指南角色' })
    .getByRole('button', { name: '组织者', exact: true })
    .click();
  await page.getByLabel('搜索当前角色指南').fill('共享目录');
  await expect(
    page.getByRole('heading', { name: '单个上传与共享目录', exact: true }),
  ).toBeVisible();
  await page.getByLabel('搜索当前角色指南').fill('无此关键词xyz');
  await expect(page.getByRole('heading', { name: '没有匹配的章节' })).toBeVisible();
  await page.getByRole('button', { name: '清除搜索' }).click();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: '下载完整手册' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('听鉴分角色使用手册.md');
  const markdown = fs.readFileSync((await download.path())!, 'utf8');
  for (const text of ['## 管理员', '## 组织者', '## 评测者', '## 通用操作与常见问题', '0.3.1'])
    expect(markdown).toContain(text);
  const footer = await page.locator('.guide-footer').boundingBox();
  expect(footer!.y + footer!.height).toBeLessThanOrEqual(768);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await page
    .getByRole('navigation', { name: '指南章节' })
    .getByRole('button', { name: '同步试听与快捷键' })
    .click();
  await expect(page.getByRole('heading', { name: '同步试听与快捷键' })).toBeVisible();
});
