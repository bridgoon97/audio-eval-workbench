import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import ts from 'typescript';

// 与 playwright.config 同源：E2E_PORT 覆盖端口（默认 8877）。
// 手工 browser.newContext()/request 不继承 use.baseURL，必须用同一常量构造绝对地址。
const port = Number(process.env.E2E_PORT || 8877);
const lan = `http://127.0.0.1:${port}`;
const packageVersion = JSON.parse(fs.readFileSync('package.json', 'utf-8')).version as string;

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
  await page.getByRole('button', { name: '播放', exact: true }).click();
  await expect
    .poll(async () => Number(await page.getByRole('slider', { name: '播放位置' }).inputValue()))
    .toBeGreaterThan(3.25);
  await expect(page.getByRole('button', { name: '播放', exact: true })).toBeVisible({
    timeout: 3000,
  });
  expect(Number(await page.getByRole('slider', { name: '播放位置' }).inputValue())).toBeCloseTo(
    3.8,
    1,
  );
  await page.getByRole('button', { name: '播放', exact: true }).click();
  await expect
    .poll(async () => Number(await page.getByRole('slider', { name: '播放位置' }).inputValue()))
    .toBeLessThan(3.5);
  await page.getByRole('button', { name: '暂停', exact: true }).click();
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
  await page.getByRole('checkbox', { name: '我也参与评测（提交偏好并计入进度）' }).check();
  await page.locator('input[name="alignment"]').check();
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

test('隐藏版本显示共享内容提示而不暴露候选波形', async ({ page }) => {
  await page.request.post('/api/login', {
    data: { name: '浏览器测试', password: 'test-password-only' },
  });
  const me = await (await page.request.get('/api/me')).json();
  const demo = await (await page.request.post('/api/demo')).json();
  await page.request.patch(`/api/tasks/${demo.id}`, {
    data: { title: '隐藏内容时间轴测试', kind: '算法版本', mode: 'blind' },
  });
  await page.request.post(`/api/tasks/${demo.id}/publish`, {
    data: { users: [me.id], alignment_confirmed: true },
  });
  await page.goto('/');
  await page.locator('.task-card').filter({ hasText: '隐藏内容时间轴测试' }).click();
  await expect(page.getByLabel('共享内容提示，拖动选择音频片段')).toHaveCount(3);
  await expect(page.getByRole('button', { name: '波形', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '频谱', exact: true })).toBeDisabled();
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
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.locator('.overlay')).toHaveCount(0);
  await page.getByRole('button', { name: '删除任务', exact: true }).click();
  await page.getByLabel('输入完整任务名称确认').fill('合成音试听 · 熟悉工作台');
  await page.getByRole('button', { name: '确认移入回收站' }).click();
  await expect(page.locator('.task-card')).toHaveCount(0);
  await page.getByRole('button', { name: '回收站', exact: true }).click();
  await expect(page.locator('.trash-row')).toHaveCount(1);
  // 组织者只能软删除/恢复：无永久清除入口，服务端同样拒绝。
  await expect(page.getByRole('button', { name: '永久清除' })).toHaveCount(0);
  const trashedTasks = await (await page.request.get('/api/tasks?deleted=true')).json();
  const denied = await page.request.post(`/api/tasks/${trashedTasks[0].id}/purge/prepare`);
  expect(denied.status()).toBe(403);
  await page.getByRole('button', { name: '恢复任务', exact: true }).click();
  await expect(page.getByText('回收站为空', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.locator('.overlay')).toHaveCount(0);
  await page.locator('.task-card').click();
  await expect(page.locator('.track')).toHaveCount(3);
});

test('角色指南默认入口、章节搜索、完整下载与窄屏阅读', async ({ page }) => {
  // 登录次数受服务端限流（20 次/5 分钟）约束：管理员首项登录后创建指南
  // 评测者账号，避免重复登录占用整套件的限流额度。
  for (const [name, password, role, heading] of [
    ['浏览器测试', 'test-password-only', '管理员', '首次启动与局域网部署'],
    ['同事上传者', 'colleague-password', '组织者', '准备一组公平的比较'],
    ['指南评测者', 'guide-password-only', '评测者', '登录并找到分配的任务'],
  ]) {
    await page.request.post('/api/login', { data: { name, password } });
    if (name === '浏览器测试') {
      await page.request.post('/api/users', {
        data: { name: '指南评测者', password: 'guide-password-only', role: 'reviewer' },
      });
    }
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
  for (const text of [
    '## 管理员',
    '## 组织者',
    '## 评测者',
    '## 通用操作与常见问题',
    packageVersion,
  ])
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

test('自动密码、HTTP 复制回退与现有账号重置', async ({ page, browser }) => {
  await page.request.post('/api/login', {
    data: { name: '浏览器测试', password: 'test-password-only' },
  });
  await page.goto('/');
  await page.getByRole('button', { name: '团队成员', exact: true }).click();
  const first = await page.getByLabel('初始密码').inputValue();
  expect(first).toMatch(/^[A-Za-z0-9_-]{24}$/);
  await page.getByRole('button', { name: '生成随机密码', exact: true }).click();
  const generated = await page.getByLabel('初始密码').inputValue();
  expect(generated).not.toBe(first);
  await page.getByLabel('同事登录地址').fill('http://192.0.2.10:8765');
  await page.getByLabel('账号', { exact: true }).fill('随机密码同事');
  await page.getByRole('button', { name: '创建账号', exact: true }).click();
  await expect(page.getByLabel('本次登录凭证内容')).toHaveValue(
    `听鉴登录地址：http://192.0.2.10:8765\n账号：随机密码同事\n密码：${generated}`,
  );
  // 模拟 HTTP LAN 的剪贴板限制，必须保留可手动复制的文本。
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
    document.execCommand = () => false;
  });
  await page.getByRole('button', { name: '复制登录凭证' }).click();
  await expect(page.getByText('已选中文本，请按 Ctrl+C（Mac 为 ⌘C）复制。')).toBeVisible();
  const colleague = await browser.newContext();
  try {
    expect(
      (
        await colleague.request.post(`${lan}/api/login`, {
          data: { name: '随机密码同事', password: generated },
        })
      ).status(),
    ).toBe(200);
    await page.getByRole('button', { name: '重置 随机密码同事 的密码' }).click();
    const replacement = await page.getByLabel('将生效的新密码').inputValue();
    await page.getByLabel('输入账号名称确认').fill('随机密码同事');
    await page.getByRole('button', { name: '确认重置密码', exact: true }).click();
    await expect(page.getByLabel('本次登录凭证内容')).toHaveValue(
      `听鉴登录地址：http://192.0.2.10:8765\n账号：随机密码同事\n密码：${replacement}`,
    );
    expect((await colleague.request.get(`${lan}/api/me`)).status()).toBe(401);
    expect(
      (
        await colleague.request.post(`${lan}/api/login`, {
          data: { name: '随机密码同事', password: generated },
        })
      ).status(),
    ).toBe(401);
    expect(
      (
        await colleague.request.post(`${lan}/api/login`, {
          data: { name: '随机密码同事', password: replacement },
        })
      ).status(),
    ).toBe(200);
    await page.keyboard.press('Escape');
    await page.getByRole('button', { name: '团队成员', exact: true }).click();
    await expect(page.getByLabel('本次登录凭证内容')).toHaveCount(0);
    const local = await page.evaluate(() => JSON.stringify(localStorage));
    expect(local).not.toContain(generated);
    expect(local).not.toContain(replacement);
  } finally {
    await colleague.close();
  }
});

test('同事不刷新网页即可收到角色和任务变化，保留未提交标注', async ({ page, browser }) => {
  await page.request.post('/api/login', {
    data: { name: '浏览器测试', password: 'test-password-only' },
  });
  await page.request.post('/api/users', {
    data: { name: '实时同步同事', password: 'sync-test-password', role: 'reviewer' },
  });
  const members = await (await page.request.get('/api/users')).json();
  const member = members.find((u: { name: string }) => u.name === '实时同步同事');
  const context = await browser.newContext();
  const colleague = await context.newPage();
  try {
    await colleague.goto(lan);
    await colleague.getByLabel('账号', { exact: true }).fill('实时同步同事');
    await colleague.getByLabel('密码', { exact: true }).fill('sync-test-password');
    await colleague.getByRole('button', { name: '进入工作台', exact: true }).click();
    await expect(colleague.getByRole('button', { name: '新建评测', exact: true })).toHaveCount(0);
    await page.request.patch(`/api/users/${member.id}/role`, { data: { role: 'organizer' } });
    await expect(colleague.getByRole('button', { name: '新建评测', exact: true })).toBeVisible({
      timeout: 12000,
    });
    const task = await (await page.request.post('/api/demo')).json();
    await page.request.post(`/api/tasks/${task.id}/publish`, {
      data: { users: [member.id], alignment_confirmed: true },
    });
    await expect(colleague.locator('.task-card')).toHaveCount(1, { timeout: 12000 });
    await colleague.locator('.task-card').click();
    await colleague.locator('#comment').fill('尚未提交的听感不能被自动刷新清空');
    await colleague.getByLabel('起点', { exact: true }).fill('1.2');
    await colleague.getByLabel('终点', { exact: true }).fill('1.8');
    await page.request.post(`/api/tasks/${task.id}/close`);
    await expect(colleague.getByRole('button', { name: '查看结果', exact: true })).toBeVisible({
      timeout: 12000,
    });
    await expect(colleague.locator('#comment')).toHaveValue('尚未提交的听感不能被自动刷新清空');
    await expect(colleague.getByLabel('起点', { exact: true })).toHaveValue('1.2');
    await colleague.getByRole('button', { name: `服务信息 · ${packageVersion}` }).click();
    await expect(colleague.getByText('数据编号', { exact: true })).toBeVisible();
    await expect(colleague.getByText('数据目录（仅管理员可见）')).toHaveCount(0);
    await colleague.keyboard.press('Escape');
    await page.request.delete(`/api/tasks/${task.id}`, {
      data: { title: '合成音试听 · 熟悉工作台' },
    });
    await expect(colleague.locator('.task-card')).toHaveCount(0, { timeout: 12000 });
    await expect(colleague.getByText('当前任务已移除或访问权限已变更，列表已同步。')).toBeVisible({
      timeout: 12000,
    });
  } finally {
    await context.close();
  }
});

test('服务与网页版本不一致时给出明确刷新提示', async ({ page }) => {
  await page.request.post('/api/login', {
    data: { name: '浏览器测试', password: 'test-password-only' },
  });
  await page.route('**/api/status', (route) =>
    route.fulfill({ json: { needs_setup: false, version: '0.3.0' } }),
  );
  await page.goto('/');
  await expect(page.getByRole('alert')).toContainText(
    `页面版本 ${packageVersion} 与服务版本 0.3.0 不一致`,
  );
  await expect(page.getByRole('button', { name: '重新加载页面' })).toBeVisible();
});

test('草稿信息可修正、发布后增补成员、评论按楼层回复', async ({ page, browser }) => {
  await page.request.post('/api/login', {
    data: { name: '浏览器测试', password: 'test-password-only' },
  });
  for (const name of ['评论甲', '评论乙', '待移除成员']) {
    await page.request.post('/api/users', {
      data: { name, password: 'comment-test-password', role: 'reviewer' },
    });
  }
  const users = await (await page.request.get('/api/users')).json();
  const id = (name: string) => users.find((user: { name: string }) => user.name === name).id;
  const created = await (
    await page.request.post('/api/tasks', {
      data: { title: '写错的任务名', kind: '算法版本', mode: 'development' },
    })
  ).json();
  await page.request.post(`/api/tasks/${created.id}/samples`, {
    data: { name: '写错的片段名', scene: '', provenance: 'PRIVATE local verification' },
  });
  await page.goto('/');
  await page.locator('.task-card').filter({ hasText: '写错的任务名' }).click();
  await page.getByRole('button', { name: '编辑任务', exact: true }).click();
  await page.getByLabel('任务名称', { exact: true }).fill('修正后的任务名');
  await page.getByLabel('比较场景').selectOption({ label: 'VPU 支路' });
  await page.getByRole('button', { name: '保存任务信息' }).click();
  await expect(page.getByRole('heading', { name: '修正后的任务名' })).toBeVisible();
  await page.getByRole('button', { name: '编辑片段', exact: true }).click();
  await page.getByLabel('片段名称').fill('修正后的片段名');
  await page.getByLabel('场景', { exact: true }).fill('会议室');
  await page.getByRole('button', { name: '保存片段信息' }).click();
  await expect(page.getByRole('heading', { name: '修正后的片段名' })).toBeVisible();

  // 使用已有合成任务验证发布后的成员调整和评论楼层。
  const demo = await (await page.request.post('/api/demo')).json();
  await page.request.patch(`/api/tasks/${demo.id}`, {
    data: { title: '评论线程与增补成员测试', kind: '算法版本', mode: 'development' },
  });
  await page.request.post(`/api/tasks/${demo.id}/publish`, {
    data: { users: [id('评论甲'), id('待移除成员')], alignment_confirmed: true },
  });
  await page.getByText('全部任务', { exact: true }).click();
  await expect(
    page.locator('.task-card').filter({ hasText: '评论线程与增补成员测试' }),
  ).toBeVisible();
  await page.locator('.task-card').filter({ hasText: '评论线程与增补成员测试' }).click();
  await page.getByRole('button', { name: '受邀评测者', exact: true }).click();
  await page.getByRole('checkbox', { name: '评论乙', exact: true }).check();
  await page.getByRole('checkbox', { name: '待移除成员', exact: true }).uncheck();
  await page.getByRole('button', { name: '保存受邀评测者' }).click();

  const first = await browser.newContext();
  const second = await browser.newContext();
  const firstPage = await first.newPage();
  const secondPage = await second.newPage();
  try {
    for (const [participant, name] of [
      [firstPage, '评论甲'],
      [secondPage, '评论乙'],
    ] as const) {
      await participant.goto(lan);
      await participant.getByLabel('账号', { exact: true }).fill(name);
      await participant.getByLabel('密码', { exact: true }).fill('comment-test-password');
      await participant.getByRole('button', { name: '进入工作台' }).click();
      await participant.locator('.task-card').filter({ hasText: '评论线程与增补成员测试' }).click();
    }
    await firstPage.locator('#comment').fill('主评论：这里有音色变化');
    await firstPage.getByRole('button', { name: '保存标注' }).click();
    await secondPage.getByRole('button', { name: '刷新', exact: true }).click();
    await secondPage
      .locator('.comment-thread')
      .getByRole('button', { name: '回复', exact: true })
      .click();
    await secondPage.locator('#comment').fill('一级回复：我也听到了');
    await secondPage.getByRole('button', { name: '保存标注' }).click();
    await firstPage.getByRole('button', { name: '刷新', exact: true }).click();
    const thread = firstPage.locator('.comment-thread');
    await expect(thread).toHaveCount(1);
    await expect(thread).toContainText('回复 @评论甲');
    await thread.locator('.comment-reply').getByRole('button', { name: '回复' }).click();
    await expect(firstPage.getByText('回复 评论乙')).toBeVisible();
    await firstPage.locator('#comment').fill('二级回复：收到');
    await firstPage.getByRole('button', { name: '保存标注' }).click();
    await expect(firstPage.locator('.comment-thread')).toHaveCount(1);
    await expect(firstPage.locator('.comment-replies')).toContainText('回复 @评论乙');
  } finally {
    await first.close();
    await second.close();
  }
});

test('关闭任务后复盘：参与进度、分歧定位、标签口径与导出', async ({ page, browser }) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.request.post('/api/login', {
    data: { name: '浏览器测试', password: 'test-password-only' },
  });
  for (const name of ['复盘甲', '复盘乙']) {
    await page.request.post('/api/users', {
      data: { name, password: 'review-flow-password', role: 'reviewer' },
    });
  }
  const users = await (await page.request.get('/api/users')).json();
  const id = (name: string) => users.find((user: { name: string }) => user.name === name).id;

  // 合成 1 秒 16 kHz PCM WAV，不使用任何真实录音。
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

  const created = await (
    await page.request.post('/api/tasks', {
      data: { title: '复盘验收任务', kind: '算法版本', mode: 'development' },
    })
  ).json();
  const samples: { name: string; id: string; tracks: string[] }[] = [];
  for (const name of ['有分歧片段', '一致片段']) {
    const sample = await (
      await page.request.post(`/api/tasks/${created.id}/samples`, {
        data: { name, scene: '复盘场景', provenance: 'PUBLIC reproducible' },
      })
    ).json();
    const tracks: string[] = [];
    for (const suffix of ['A', 'B']) {
      const track = await (
        await page.request.post(`/api/samples/${sample.id}/tracks`, {
          multipart: {
            name: `候选${suffix}`,
            version: 'v1',
            file: { name: 'clip.wav', mimeType: 'audio/wav', buffer: wav },
          },
        })
      ).json();
      tracks.push(track.id);
    }
    samples.push({ name, id: sample.id, tracks });
  }
  await page.request.post(`/api/tasks/${created.id}/publish`, {
    data: { users: [id('复盘甲'), id('复盘乙')], alignment_confirmed: true },
  });

  // 未受邀负责人首次评分被拒绝（403），且不产生评分行。
  const rejected = await page.request.post(`/api/samples/${samples[0].id}/rating`, {
    data: { choice: samples[0].tracks[0] },
  });
  expect(rejected.status()).toBe(403);

  // 负责人显式加入受邀名单后即可评分并计入进度与分母。
  const me = await (await page.request.get('/api/me')).json();
  await page.request.patch(`/api/tasks/${created.id}/members`, {
    data: { users: [id('复盘甲'), id('复盘乙'), me.id] },
  });

  // 管理员（已受邀，页面会话）独立提交：片段一留标签评论并投候选A，片段二投候选A。
  // 复盘甲全程不提交，用于验证未开始名单。
  await page.request.post(`/api/samples/${samples[0].id}/comments`, {
    data: { start: 0, end: 100, body: '字尾残噪明显', tag: '残噪' },
  });
  await page.request.post(`/api/samples/${samples[0].id}/rating`, {
    data: { choice: samples[0].tracks[0] },
  });
  await page.request.post(`/api/samples/${samples[1].id}/rating`, {
    data: { choice: samples[1].tracks[0] },
  });

  // 复盘乙通过 API 提交：片段一投候选B（制造分歧），片段二投候选A（保持一致），
  // 并用“听感”标签回复评论——回复不应计入标签汇总。
  const second = await browser.newContext();
  try {
    const api = second.request;
    const base = lan;
    expect(
      (
        await api.post(`${base}/api/login`, {
          data: { name: '复盘乙', password: 'review-flow-password' },
        })
      ).status(),
    ).toBe(200);
    expect(
      (
        await api.post(`${base}/api/samples/${samples[0].id}/rating`, {
          data: { choice: samples[0].tracks[1] },
        })
      ).status(),
    ).toBe(200);
    expect(
      (
        await api.post(`${base}/api/samples/${samples[1].id}/rating`, {
          data: { choice: samples[1].tracks[0] },
        })
      ).status(),
    ).toBe(200);
    const detail = await api.get(`${base}/api/samples/${samples[0].id}`);
    expect(detail.status()).toBe(200);
    const comments = (await detail.json()).comments as { id: string }[];
    expect(comments.length).toBeGreaterThan(0);
    expect(
      (
        await api.post(`${base}/api/samples/${samples[0].id}/comments`, {
          data: {
            start: 0,
            end: 100,
            body: '回复：同意有残噪',
            tag: '听感',
            parent: comments[0].id,
          },
        })
      ).status(),
    ).toBe(200);
  } finally {
    await second.close();
  }

  // 关闭前：管理者通过“评测进度”确认完成情况（只含数量与状态，无偏好内容）。
  await page.goto('/');
  await page.locator('.task-card').filter({ hasText: '复盘验收任务' }).click();
  await page.getByRole('button', { name: '评测进度' }).click();
  const live = page.locator('.progress-numbers span');
  await expect(live.nth(0)).toHaveText('受邀评测者3');
  await expect(live.nth(1)).toHaveText('已完成2');
  await expect(live.nth(2)).toHaveText('进行中0');
  await expect(live.nth(3)).toHaveText('未开始1');
  await expect(page.locator('.progress-rows')).toContainText('复盘乙2/2 片段 · 已完成');
  await expect(page.locator('.progress-rows')).toContainText('复盘甲0/2 片段 · 未开始');
  await expect(page.locator('.task-progress')).not.toContainText('候选A');
  await page.keyboard.press('Escape');

  await page.request.post(`/api/tasks/${created.id}/close`);
  await page.goto('/');
  await page.locator('.task-card').filter({ hasText: '复盘验收任务' }).click();
  await page.getByRole('button', { name: '查看结果' }).click();

  const progress = page.locator('.progress-numbers span');
  await expect(progress.nth(0)).toHaveText('受邀评测者3');
  await expect(progress.nth(1)).toHaveText('已完成2');
  await expect(progress.nth(2)).toHaveText('进行中0');
  await expect(progress.nth(3)).toHaveText('未开始1');
  await expect(page.locator('.review-progress')).toContainText('已完成：复盘乙、浏览器测试');
  await expect(page.locator('.review-progress')).toContainText('未开始：复盘甲');

  const disputed = page.locator('.review-sample').filter({ hasText: '有分歧片段' });
  const consistent = page.locator('.review-sample').filter({ hasText: '一致片段' });
  await expect(disputed.locator('.diff-badge')).toHaveText('存在分歧');
  await expect(consistent.locator('.diff-badge')).toHaveCount(0);
  await expect(disputed).toContainText('2 人已提交（分母）');
  await expect(consistent).toContainText('2 人已提交（分母）');
  await expect(disputed.locator('.result-row').filter({ hasText: '候选A' })).toContainText(
    '50.0%（1/2）',
  );
  await expect(consistent.locator('.result-row').filter({ hasText: '候选A' })).toContainText(
    '100.0%（2/2）',
  );
  await expect(disputed.locator('.result-row').filter({ hasText: '候选B' })).toContainText(
    '50.0%（1/2）',
  );
  await expect(disputed.locator('.result-row').filter({ hasText: '无明显差异' })).toContainText(
    '0.0%（0/2）',
  );

  // 分歧跳回：回到片段试听区并能看到评论楼层。
  await disputed.getByRole('button', { name: '回到此片段试听与标注' }).click();
  await expect(page.locator('.tracks-heading h2')).toHaveText('有分歧片段');
  await expect(page.getByText('字尾残噪明显', { exact: true })).toBeVisible();

  // 标签定位：只统计根评论（回复的“听感”不出现），点击定位回片段。
  await page.getByRole('button', { name: '查看结果' }).click();
  const tagSummary = page.locator('.tag-summary');
  await expect(tagSummary).toHaveCount(1);
  await expect(tagSummary).toContainText('残噪');
  await expect(tagSummary).toContainText('1 条');
  await tagSummary.getByRole('button', { name: '有分歧片段' }).click();
  await expect(page.locator('.tracks-heading h2')).toHaveText('有分歧片段');

  // 重新打开结果弹窗，下载 CSV：UTF-8 BOM、中文表头与片段名可直接被中文 Excel 识别。
  await page.getByRole('button', { name: '查看结果' }).click();
  await expect(page.locator('.report-exports')).toBeVisible();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('link', { name: '结果 CSV（Excel）' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('evaluation-results.csv');
  const raw = fs.readFileSync((await download.path())!);
  expect([...raw.subarray(0, 3)]).toEqual([0xef, 0xbb, 0xbf]);
  const text = raw.toString('utf8');
  expect(text).toContain('有分歧片段');
  expect(text).toContain('残噪');
  expect(text).toContain('存在分歧');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  expect(errors).toEqual([]);
});

test('对齐与响度：分析、应用、恢复、发布确认与导出处理证据', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.request.post('/api/login', {
    data: { name: '浏览器测试', password: 'test-password-only' },
  });

  // 运行时合成 4 秒 16 kHz 宽带信号（xorshift 白噪声 + 线性扫频）：
  // 参考、延迟 320/衰减 0.7 的候选、周期纯音。
  const n = 64000;
  const ref = new Float32Array(n);
  let seed = 123456789;
  const rand = () => {
    seed ^= seed << 13;
    seed ^= seed >>> 17;
    seed ^= seed << 5;
    seed >>>= 0;
    return seed / 2147483648 - 1;
  };
  for (let i = 0; i < n; i++) {
    ref[i] =
      0.6 * rand() +
      0.3 * Math.sin(2 * Math.PI * (40 * (i / 16000) + (900 * (i / 16000) ** 2) / 2));
  }
  let peak = 0;
  for (let i = 0; i < n; i++) peak = Math.max(peak, Math.abs(ref[i]));
  for (let i = 0; i < n; i++) ref[i] = (ref[i] / peak) * 0.5;
  const delayed = new Float32Array(n);
  for (let i = 320; i < n; i++) delayed[i] = ref[i - 320] * 0.7;
  const advanced = new Float32Array(n);
  for (let i = 0; i < n - 157; i++) advanced[i] = ref[i + 157] * 0.9;
  const sine = new Float32Array(n);
  for (let i = 0; i < n; i++) sine[i] = 0.5 * Math.sin((2 * Math.PI * 100 * i) / 16000);
  const wav = (x: Float32Array) => {
    const buf = Buffer.alloc(44 + n * 2);
    buf.write('RIFF', 0);
    buf.writeUInt32LE(buf.length - 8, 4);
    buf.write('WAVEfmt ', 8);
    buf.writeUInt32LE(16, 16);
    buf.writeUInt16LE(1, 20);
    buf.writeUInt16LE(1, 22);
    buf.writeUInt32LE(16000, 24);
    buf.writeUInt32LE(n * 2, 28);
    buf.writeUInt16LE(4, 32);
    buf.writeUInt16LE(16, 34);
    buf.write('data', 36);
    buf.writeUInt32LE(n * 2, 40);
    for (let i = 0; i < n; i++)
      buf.writeInt16LE(Math.max(-32768, Math.min(32767, Math.round(x[i] * 32767))), 44 + i * 2);
    return buf;
  };

  const created = await (
    await page.request.post('/api/tasks', {
      data: { title: '对齐响度验收', kind: '算法版本', mode: 'development' },
    })
  ).json();
  const sample = await (
    await page.request.post(`/api/tasks/${created.id}/samples`, {
      data: { name: '对齐片段', provenance: 'PUBLIC reproducible' },
    })
  ).json();
  const trackIds: string[] = [];
  for (const [name, data] of [
    ['参考宽带', wav(ref)],
    ['延迟衰减', wav(delayed)],
    ['提前157', wav(advanced)],
    ['周期纯音', wav(sine)],
  ] as const) {
    const track = await (
      await page.request.post(`/api/samples/${sample.id}/tracks`, {
        multipart: {
          name,
          version: 'v1',
          file: { name: 'clip.wav', mimeType: 'audio/wav', buffer: data },
        },
      })
    ).json();
    trackIds.push(track.id);
  }

  await page.goto('/');
  await page.locator('.task-card').filter({ hasText: '对齐响度验收' }).click();

  // 草稿修订：纠正任务标题与片段名称。
  await page.getByRole('button', { name: '编辑任务', exact: true }).click();
  await page.getByLabel('任务名称', { exact: true }).fill('对齐响度验收（修订）');
  await page.getByRole('button', { name: '保存任务信息' }).click();
  await expect(page.getByRole('heading', { name: /对齐响度验收（修订）/ })).toBeVisible();
  await page.getByRole('button', { name: '编辑片段', exact: true }).click();
  await page.getByLabel('片段名称', { exact: true }).fill('对齐片段（修订）');
  await page.getByRole('button', { name: '保存片段信息' }).click();
  await expect(page.getByRole('heading', { name: '对齐片段（修订）' })).toBeVisible();

  // 整段删除：无贡献片段经确认后整段移除。
  const sample2 = await (
    await page.request.post(`/api/tasks/${created.id}/samples`, {
      data: { name: '待删除片段', provenance: 'PUBLIC reproducible' },
    })
  ).json();
  await page.request.post(`/api/samples/${sample2.id}/tracks`, {
    multipart: {
      name: '候选甲',
      version: 'v1',
      file: { name: 'c.wav', mimeType: 'audio/wav', buffer: wav(ref) },
    },
  });
  await page.locator('.sample-item').filter({ hasText: '待删除片段' }).click();

  // 精确制造陈旧同步：先拦住一次删除前的任务详情响应，再执行删除。
  // DELETE 完成并刷新为新状态后才放行旧响应；旧同步不得写回已删片段。
  let releaseStale: (() => void) | undefined;
  const staleReleased = new Promise<void>((resolve) => {
    releaseStale = resolve;
  });
  let staleRequestEntered: (() => void) | undefined;
  const staleEntered = new Promise<void>((resolve) => {
    staleRequestEntered = resolve;
  });
  let holdNextTaskDetail = true;
  await page.route(`**/api/tasks/${created.id}`, async (route) => {
    if (holdNextTaskDetail && route.request().method() === 'GET') {
      holdNextTaskDetail = false;
      staleRequestEntered?.();
      await staleReleased;
    }
    await route.continue();
  });
  await page.getByRole('button', { name: '同步', exact: true }).click();
  await staleEntered;

  const deleteDone = page.waitForResponse(
    (response) =>
      response.request().method() === 'DELETE' && response.url().includes('/api/samples/'),
  );
  page.once('dialog', (d) => d.accept());
  await page.getByRole('button', { name: '删除片段', exact: true }).click();
  expect((await deleteDone).status()).toBe(200);
  await expect(page.locator('.sample-item').filter({ hasText: '待删除片段' })).toHaveCount(0);
  releaseStale?.();
  await expect(page.locator('.sample-item').filter({ hasText: '待删除片段' })).toHaveCount(0);
  await expect(page.locator('.tracks-heading h2')).toHaveText(/对齐片段（修订）/);
  await page.unroute(`**/api/tasks/${created.id}`);

  await page.getByRole('button', { name: '对齐与响度' }).click();
  await page.getByLabel(/参考候选/).selectOption({ label: '参考宽带' });
  await page.getByRole('button', { name: '开始分析' }).click();
  // 分析在低端/Windows runner 上可能超过默认 5 秒：三处（首次、恢复后、再次应用）
  // 统一 20 秒显式等待，不降低数值断言。
  await expect(page.locator('.proc-row').filter({ hasText: '延迟衰减' })).toContainText(
    '320 samples',
    { timeout: 20000 },
  );
  await expect(page.locator('.proc-row').filter({ hasText: '延迟衰减' })).toContainText('建议');
  // 负 lag 渲染：-157 samples、早到、应用后移，符号不出现 "+-" 或 "--"。
  const advancedRow = page.locator('.proc-row').filter({ hasText: '提前157' });
  await expect(advancedRow).toContainText('-157 samples');
  await expect(advancedRow).toContainText('-9.8 ms');
  await expect(advancedRow).toContainText('候选早到，应用时后移');
  const advancedText = await advancedRow.textContent();
  expect(advancedText).not.toContain('+-');
  expect(advancedText).not.toContain('--');
  const rejectedRow = page.locator('.proc-row').filter({ hasText: '周期纯音' });
  await expect(rejectedRow.locator('.rejected').first()).toContainText('ERR_LOW_CORRELATION');
  await expect(rejectedRow.locator('.rejected').nth(1)).toContainText('ERR_DELAY_NOT_APPLICABLE');

  // 默认勾选＝判据通过项；应用「先对齐后增益」。
  const row = page.locator('.proc-row').filter({ hasText: '延迟衰减' });
  await expect(row.getByRole('checkbox').nth(0)).toBeChecked();
  await expect(row.getByRole('checkbox').nth(1)).toBeChecked();
  await expect(page.locator('.proc-summary')).toContainText('整数采样对齐（先）');
  await expect(page.locator('.proc-summary')).toContainText('活动段 RMS 固定增益（后）');
  await page.getByRole('button', { name: '确认应用所选处理' }).click();
  // 候选顺序按用户匿名映射随机：先定位明确候选名所在的 .track，再查其派生徽标。
  const processedTrackCard = page.locator('.track', { hasText: '延迟衰减' });
  await expect(processedTrackCard.locator('.proc-live')).toContainText('派生试听');
  await expect(processedTrackCard.locator('.proc-live')).toContainText(
    '对齐 +320 samples（候选晚到，应用时前移）',
  );

  // 恢复原始 → 再应用。
  await page.getByRole('button', { name: '对齐与响度' }).click();
  await page.getByLabel(/参考候选/).selectOption({ label: '参考宽带' });
  await page.getByRole('button', { name: '开始分析' }).click();
  // 分析在低端/Windows runner 上可能超过默认 5 秒：三处（首次、恢复后、再次应用）
  // 统一 20 秒显式等待，不降低数值断言。
  await expect(page.locator('.proc-row').filter({ hasText: '延迟衰减' })).toContainText(
    '320 samples',
    { timeout: 20000 },
  );
  await page.getByRole('button', { name: '确认应用所选处理' }).click();
  await expect(processedTrackCard.locator('.proc-live')).toContainText('派生试听');
  await page.getByRole('button', { name: '对齐与响度' }).click();
  await page.getByRole('button', { name: '恢复原始处理' }).click();
  await expect(page.locator('.proc-live')).toHaveCount(0);
  await page.getByRole('button', { name: '对齐与响度' }).click();
  await page.getByLabel(/参考候选/).selectOption({ label: '参考宽带' });
  await page.getByRole('button', { name: '开始分析' }).click();
  // 分析在低端/Windows runner 上可能超过默认 5 秒：三处（首次、恢复后、再次应用）
  // 统一 20 秒显式等待，不降低数值断言。
  await expect(page.locator('.proc-row').filter({ hasText: '延迟衰减' })).toContainText(
    '320 samples',
    { timeout: 20000 },
  );
  await page.getByRole('button', { name: '确认应用所选处理' }).click();
  await expect(processedTrackCard.locator('.proc-live')).toContainText('派生试听');

  // 发布确认显示混合处理与被拒绝建议；发布后冻结。
  await page.getByRole('button', { name: '发布评测', exact: true }).click();
  await expect(page.locator('.publish-processing')).toContainText('存在混合处理口径');
  await expect(page.locator('.publish-processing')).toContainText('延迟衰减：对齐+响度');
  await expect(page.locator('.publish-processing-rejected')).toContainText('ERR_LOW_CORRELATION');
  await page.locator('input[name="alignment"]').check();
  await page.getByRole('button', { name: '发布并锁定任务' }).click();
  await page.getByRole('button', { name: '关闭并揭晓' }).click();
  await page.getByRole('button', { name: '关闭收集并统一揭晓' }).click();
  await expect(page.getByRole('button', { name: '查看结果' })).toBeVisible({ timeout: 15000 });

  const exported = await (await page.request.get(`/api/tasks/${created.id}/export`)).text();
  const data = JSON.parse(exported);
  const processedTrack = data['样本'][0].tracks.find((t: { id: string }) => t.id === trackIds[1]);
  expect(processedTrack['处理口径']['模式']).toBe('对齐+响度');
  expect(processedTrack['处理口径']['lag']).toBe(320);
  expect(processedTrack['处理口径']['派生资产SHA256']).toMatch(/^[0-9a-f]{64}$/);
  expect(exported).toContain('ERR_LOW_CORRELATION');

  // 软删除 → 管理员永久清除（一次性令牌 + 不可恢复确认）。
  await page.request.delete(`/api/tasks/${created.id}`, {
    data: { title: '对齐响度验收（修订）' },
  });
  await page.goto('/');
  await page.getByRole('button', { name: '回收站', exact: true }).click();
  const trashRow = page.locator('.trash-row').filter({ hasText: '对齐响度验收（修订）' });
  await expect(trashRow).toBeVisible();
  await trashRow.getByRole('button', { name: '永久清除' }).click();
  await expect(page.locator('.purge-confirm')).toContainText('不可恢复');
  await page.locator('.purge-confirm').getByRole('button', { name: '确认永久清除' }).click();
  // 其他任务的软删除记录不受影响；本任务行消失且接口 404。
  await expect(page.locator('.trash-row').filter({ hasText: '对齐响度验收（修订）' })).toHaveCount(
    0,
  );
  await expect(page.locator('.trash-row').filter({ hasText: '合成音试听' })).toBeVisible();
  expect((await page.request.get(`/api/tasks/${created.id}`)).status()).toBe(404);
  expect(errors).toEqual([]);
});
