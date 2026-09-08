import { test, expect } from '@playwright/test';

// 与 workbench/z-access 共享同一服务实例；文件名以 z 开头保证最后运行。
// 自举：needs_setup 时自行初始化，否则直接登录（不依赖其他文件的执行顺序）。
const admin = { name: '浏览器测试', password: 'test-password-only' };

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

test('管理员生成一次性恢复凭证 → 用户领取设置新密码 → 再次登录', async ({ page, browser }) => {
  test.setTimeout(120000);
  await ensureAdmin(page.request);
  await page.request.post('/api/users', {
    data: { name: '恢复流程同事', password: 'lost-browser-pass' },
  });

  // 管理员在团队成员里生成一次性恢复凭证。
  await page.goto('/');
  await expect(page.locator('.dashboard')).toBeVisible();
  await page.getByRole('button', { name: '团队成员', exact: true }).click();
  await page.getByRole('button', { name: '生成 恢复流程同事 的恢复凭证' }).click();
  await page.getByLabel('用途 / 备注').fill('浏览器重装');
  await page.getByRole('button', { name: '生成恢复凭证' }).click();
  const codeBox = page.getByLabel('本次恢复凭证内容');
  await expect(codeBox).toBeVisible();
  const code = (await codeBox.inputValue()).trim();
  expect(code).toMatch(/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);
  // 隐藏后不再显示明文。
  await page.getByRole('button', { name: '隐藏凭证' }).click();
  await expect(codeBox).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(page.locator('.overlay')).toHaveCount(0);

  // 用户在丢失的浏览器里用凭证设置新密码并直接登录。
  const user = await browser.newContext();
  const userPage = await user.newPage();
  try {
    await userPage.goto('/');
    await userPage.getByRole('button', { name: '凭恢复凭证设置新密码' }).click();
    await userPage.getByLabel('恢复凭证').fill(code + '-被篡改');
    await userPage.getByLabel('新密码（至少 10 个字符）').fill('recovered-pass-9');
    await userPage.getByRole('button', { name: '设置新密码并登录' }).click();
    await expect(userPage.getByText('恢复凭证无效、已使用或已过期')).toBeVisible();

    await userPage.getByLabel('恢复凭证').fill(code);
    await userPage.getByRole('button', { name: '设置新密码并登录' }).click();
    await expect(userPage.locator('.dashboard')).toBeVisible({ timeout: 20000 });
    // 重复领取同一凭证：稳定拒绝。
    await userPage.getByRole('button', { name: '退出登录' }).click();
    await expect(userPage.getByLabel('账号', { exact: true })).toBeVisible();
    await userPage.getByRole('button', { name: '凭恢复凭证设置新密码' }).click();
    await userPage.getByLabel('恢复凭证').fill(code);
    await userPage.getByLabel('新密码（至少 10 个字符）').fill('another-pass-99');
    await userPage.getByRole('button', { name: '设置新密码并登录' }).click();
    await expect(userPage.getByText('该恢复凭证已被使用，请向管理员重新获取')).toBeVisible();
    await userPage.getByRole('button', { name: '返回登录' }).click();

    // 旧密码失效；新密码可再次登录。
    await userPage.getByLabel('账号', { exact: true }).fill('恢复流程同事');
    await userPage.getByLabel('密码', { exact: true }).fill('lost-browser-pass');
    await userPage.getByRole('button', { name: '进入工作台', exact: true }).click();
    await expect(userPage.getByText('账号或密码错误')).toBeVisible();
    await userPage.getByLabel('密码', { exact: true }).fill('recovered-pass-9');
    await userPage.getByRole('button', { name: '进入工作台', exact: true }).click();
    await expect(userPage.locator('.dashboard')).toBeVisible();
  } finally {
    await user.close();
  }
});

test('管理员停用 → 在线用户立即失效 → 启用后重新登录', async ({ page, browser }) => {
  test.setTimeout(120000);
  await ensureAdmin(page.request);
  await page.request.post('/api/users', {
    data: { name: '停用流程同事', password: 'disable-pass-77' },
  });

  // 用户在线。
  const user = await browser.newContext();
  const userPage = await user.newPage();
  try {
    await userPage.goto('/');
    await userPage.getByLabel('账号', { exact: true }).fill('停用流程同事');
    await userPage.getByLabel('密码', { exact: true }).fill('disable-pass-77');
    await userPage.getByRole('button', { name: '进入工作台', exact: true }).click();
    await expect(userPage.locator('.dashboard')).toBeVisible();

    // 管理员停用（确认对话框）；管理员页面此时才首次打开。
    await page.goto('/');
    await expect(page.locator('.dashboard')).toBeVisible();
    await page.getByRole('button', { name: '团队成员', exact: true }).click();
    page.once('dialog', (dialog) => dialog.accept());
    await page.getByRole('button', { name: '停用 停用流程同事 的账号' }).click();
    await expect(page.getByText('已停用', { exact: true })).toBeVisible();

    // 在线用户 5 秒同步后被送回登录页（模态保持打开，稍后直接启用）。
    await expect(userPage.getByLabel('账号', { exact: true })).toBeVisible({
      timeout: 20000,
    });

    // 管理员在模态内直接启用；旧会话不复活，需重新登录（原密码仍有效）。
    await page.getByRole('button', { name: '启用 停用流程同事 的账号' }).click();
    await expect(page.getByText('已停用', { exact: true })).toHaveCount(0);
    await page.keyboard.press('Escape');
    await expect(page.locator('.overlay')).toHaveCount(0);

    await userPage.getByLabel('账号', { exact: true }).fill('停用流程同事');
    await userPage.getByLabel('密码', { exact: true }).fill('disable-pass-77');
    await userPage.getByRole('button', { name: '进入工作台', exact: true }).click();
    await expect(userPage.locator('.dashboard')).toBeVisible();

    // 停用按钮对管理员自身不可见（管理员行无停用/启用/恢复凭证按钮）。
    await page.getByRole('button', { name: '团队成员', exact: true }).click();
    await expect(page.getByRole('button', { name: '停用 浏览器测试 的账号' })).toHaveCount(0);
  } finally {
    await user.close();
  }
});
