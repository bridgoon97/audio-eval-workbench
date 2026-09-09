import { test, expect } from '@playwright/test';
import fs from 'node:fs';

// 与 workbench.spec.ts 共享同一服务实例；文件名以 z 开头保证在既有用例后运行。
const admin = { name: '浏览器测试', password: 'test-password-only' };

const wav = (() => {
  // 合成 1 秒 16 kHz PCM WAV，不使用真实录音。
  const buffer = Buffer.alloc(44 + 32000);
  buffer.write('RIFF', 0);
  buffer.writeUInt32LE(buffer.length - 8, 4);
  buffer.write('WAVEfmt ', 8);
  buffer.writeUInt32LE(16, 16);
  buffer.writeUInt16LE(1, 20);
  buffer.writeUInt16LE(1, 22);
  buffer.writeUInt32LE(16000, 24);
  buffer.writeUInt32LE(32000, 28);
  buffer.writeUInt16LE(2, 32);
  buffer.writeUInt16LE(16, 34);
  buffer.write('data', 36);
  buffer.writeUInt32LE(32000, 40);
  return buffer;
})();

async function ensureAdmin(request: import('@playwright/test').APIRequestContext) {
  const status = await (await request.get('/api/status')).json();
  if (status.needs_setup) {
    const setup = await request.post('/api/setup', {
      data: { ...admin, setup_key: 'browser-test-setup-key' },
    });
    if (!setup.ok()) throw new Error('setup failed: ' + (await setup.text()));
  }
  const login = await request.post('/api/login', { data: admin });
  if (!login.ok()) throw new Error('login failed');
}

async function csrfToken(request: import('@playwright/test').APIRequestContext) {
  return (await (await request.get('/api/me')).json()).csrf_token as string;
}

async function createActiveTask(
  request: import('@playwright/test').APIRequestContext,
  title: string,
) {
  const token = await csrfToken(request);
  const created = await (
    await request.post('/api/tasks', { data: { title, kind: '算法版本', mode: 'development' } })
  ).json();
  const sample = await (
    await request.post(`/api/tasks/${created.id}/samples`, {
      data: { name: '免密片段', provenance: 'PUBLIC reproducible' },
    })
  ).json();
  for (const name of ['候选甲', '候选乙']) {
    await request.post(`/api/samples/${sample.id}/tracks`, {
      multipart: {
        name,
        version: 'v1',
        file: { name: 'clip.wav', mimeType: 'audio/wav', buffer: wav },
      },
    });
  }
  const published = await request.post(`/api/tasks/${created.id}/publish`, {
    data: { users: [], alignment_confirmed: true },
    headers: { 'x-csrf-token': token },
  });
  if (!published.ok()) throw new Error('publish failed: ' + (await published.text()));
  return created.id as string;
}

test('自助申请全流程：一次性邀请 → 申请 → 批准分配任务 → 免密登录 → 撤销', async ({
  page,
  browser,
}) => {
  test.setTimeout(120000);
  await ensureAdmin(page.request);
  const taskId = await createActiveTask(page.request, '免密登录验收任务');

  // 管理员（请求登录已与页面共享 Cookie）生成一次性邀请，明文只出现一次。
  await page.goto('/');
  await expect(page.locator('.dashboard')).toBeVisible();
  await page.getByRole('button', { name: '加入与邀请' }).click();
  await page.getByRole('button', { name: '邀请凭证' }).click();
  await page.getByLabel('用途 / 备注').fill('声学组第一轮');
  await page.getByRole('button', { name: '生成邀请凭证' }).click();
  const credential = page.getByLabel('邀请凭证明文');
  await expect(credential).toBeVisible();
  const code = (await credential.inputValue()).trim();
  expect(code).toMatch(/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);
  await page.keyboard.press('Escape');
  // 模态关闭与重渲染在慢 runner 上可能超过 5 秒。
  await expect(page.locator('.overlay')).toHaveCount(0, { timeout: 15000 });

  // 无账号浏览器：申请加入并等待。
  const applicant = await browser.newContext();
  const applicantPage = await applicant.newPage();
  try {
    await applicantPage.goto('/');
    await applicantPage.getByRole('button', { name: '申请加入评测' }).click();
    await applicantPage.getByLabel('显示名称').fill('免密申请员');
    await applicantPage.getByLabel('工号（可选）').fill('A-1024');
    await applicantPage.getByLabel('邀请凭证').fill(code);
    await applicantPage.getByRole('button', { name: '提交申请' }).click();
    await expect(applicantPage.getByRole('heading', { name: '等待管理员批准' })).toBeVisible();
    const applicationId = await applicantPage.getByText(/^[0-9a-f]{32}$/).textContent();
    expect(applicationId).toBeTruthy();

    // 管理员看到待审批角标并批准、分配任务。
    await page.getByRole('button', { name: '加入与邀请' }).click();
    const badge = page.locator('.pending-badge');
    await expect(badge).toHaveText('1', { timeout: 15000 });
    const row = page.locator('.application-row', { hasText: '免密申请员' });
    await expect(row).toContainText('声学组第一轮');
    await expect(row).toContainText('工号 A-1024');
    await row.getByRole('button', { name: '批准…' }).click();
    await expect(row.getByLabel('账号名称')).toHaveValue('免密申请员');
    await row.getByLabel('免密登录验收任务').check();
    await row.getByRole('button', { name: '确认批准并创建账号' }).click();
    await expect(
      page.locator('.application-row').filter({ hasText: '免密申请员' }),
    ).not.toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.locator('.overlay')).toHaveCount(0, { timeout: 15000 });

    // 原浏览器自动领取 Cookie 进入工作台；刷新后免密保持。
    await expect(applicantPage.locator('.dashboard')).toBeVisible({ timeout: 20000 });
    await applicantPage.reload();
    await expect(applicantPage.locator('.dashboard')).toBeVisible();
    await expect(
      applicantPage.locator('.task-card', { hasText: '免密登录验收任务' }),
    ).toBeVisible();

    // 管理员查看设备并撤销。
    await page.getByRole('button', { name: '团队成员', exact: true }).click();
    await page.getByRole('button', { name: '管理 免密申请员 的登录设备' }).click();
    const deviceRow = page.locator('.device-row').filter({ hasNotText: '已撤销' });
    await expect(deviceRow).toContainText('首次');
    await deviceRow.getByRole('button', { name: '撤销此设备' }).click();
    await expect(page.locator('.device-row', { hasText: '已撤销' })).toBeVisible();
    await page.keyboard.press('Escape');
    await page.keyboard.press('Escape');

    // 撤销后原浏览器下一次同步立即失去访问。
    await expect(applicantPage.getByLabel('账号', { exact: true })).toBeVisible({ timeout: 20000 });
    await applicantPage.reload();
    await expect(applicantPage.getByLabel('账号', { exact: true })).toBeVisible();
  } finally {
    await applicant.close();
  }
});

test('拒绝申请、无效与停用邀请凭证、普通成员无审批入口', async ({ page, browser }) => {
  test.setTimeout(120000);
  await ensureAdmin(page.request);
  const token = await csrfToken(page.request);
  const invite = await (
    await page.request.post('/api/invites', {
      data: { purpose: '将被拒绝与停用', kind: 'team', expires_days: 7, max_uses: 5 },
      headers: { 'x-csrf-token': token },
    })
  ).json();
  await page.request.post('/api/users', {
    data: { name: '无审批入口的同事', password: 'no-access-password', role: 'reviewer' },
  });

  // 无效凭证：明确报错且不泄露原因细节。
  const applicant = await browser.newContext();
  const applicantPage = await applicant.newPage();
  try {
    await applicantPage.goto('/');
    await applicantPage.getByRole('button', { name: '申请加入评测' }).click();
    await applicantPage.getByLabel('显示名称').fill('无效凭证员');
    await applicantPage.getByLabel('邀请凭证').fill('bad.code');
    await applicantPage.getByRole('button', { name: '提交申请' }).click();
    await expect(applicantPage.getByText('邀请凭证无效或已失效，请向管理员核对')).toBeVisible();

    // 停用邀请后同样无效。
    await applicantPage.getByLabel('邀请凭证').fill(invite.code);
    await applicantPage.getByRole('button', { name: '提交申请' }).click();
    await expect(applicantPage.getByRole('heading', { name: '等待管理员批准' })).toBeVisible({
      timeout: 20000,
    });
    const applicationId = (await applicantPage.getByText(/^[0-9a-f]{32}$/).textContent()) as string;
    const list = await (await page.request.get('/api/applications')).json();
    const entry = list.find((x: { id: string }) => x.id === applicationId);
    const disable = await page.request.patch(`/api/invites/${invite.id}`, {
      data: { active: false },
      headers: { 'x-csrf-token': token },
    });
    expect(disable.ok()).toBeTruthy();
    // 轮询后申请随邀请失效自动过期；批准被拒绝。
    await expect(applicantPage.getByText('邀请凭证已失效，申请已过期')).toBeVisible({
      timeout: 20000,
    });
    const approve = await page.request.post(`/api/applications/${entry.id}/approve`, {
      data: { role: 'reviewer' },
      headers: { 'x-csrf-token': token },
    });
    expect(approve.status()).toBe(409);

    // 拒绝流程：申请人看到终态提示。
    const secondInvite = await (
      await page.request.post('/api/invites', {
        data: { purpose: '拒绝流程', kind: 'team', expires_days: 7, max_uses: 5 },
        headers: { 'x-csrf-token': token },
      })
    ).json();
    await applicantPage.getByLabel('显示名称').fill('将被拒绝的人');
    await applicantPage.getByLabel('邀请凭证').fill(secondInvite.code);
    await applicantPage.getByRole('button', { name: '提交申请' }).click();
    await expect(applicantPage.getByRole('heading', { name: '等待管理员批准' })).toBeVisible({
      timeout: 20000,
    });
    const list2 = await (await page.request.get('/api/applications')).json();
    const entry2 = list2.find(
      (x: { display_name: string; status: string }) =>
        x.display_name === '将被拒绝的人' && x.status === 'pending',
    );
    expect(
      entry2,
      '第二次申请应为 pending：' +
        JSON.stringify(
          list2.map((x: { display_name: string; status: string }) => [x.display_name, x.status]),
        ),
    ).toBeTruthy();
    const reject = await page.request.post(`/api/applications/${entry2.id}/reject`, {
      data: {},
      headers: { 'x-csrf-token': token },
    });
    expect(reject.ok()).toBeTruthy();
    await expect(applicantPage.getByText('申请已被拒绝', { exact: false })).toBeVisible({
      timeout: 20000,
    });
  } finally {
    await applicant.close();
  }

  // 普通成员看不到审批入口。
  const member = await browser.newContext();
  const memberPage = await member.newPage();
  try {
    await memberPage.goto('/');
    await memberPage.getByLabel('账号', { exact: true }).fill('无审批入口的同事');
    await memberPage.getByLabel('密码', { exact: true }).fill('no-access-password');
    await memberPage.getByRole('button', { name: '进入工作台', exact: true }).click();
    await expect(memberPage.getByRole('button', { name: '加入与邀请' })).toHaveCount(0);
    await expect(memberPage.getByRole('button', { name: '团队成员', exact: true })).toHaveCount(0);
  } finally {
    await member.close();
  }
});
