import { useEffect, useRef, useState } from 'react';
import { Check, Copy, KeyRound, ShieldX, Users } from 'lucide-react';
import { api, type Application, type DeviceInfo, type Invite, type Task, type User } from './types';

const stateName: Record<string, string> = {
  pending: '待审批',
  approved: '已批准',
  rejected: '已拒绝',
  expired: '已过期',
};

function copyText(text: string, done: (message: string) => void, fallback?: HTMLTextAreaElement | null) {
  void (async () => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        done('已复制到剪贴板');
        return;
      }
    } catch {
      /* HTTP 局域网可能限制剪贴板，回退到选区复制。 */
    }
    fallback?.focus();
    fallback?.select();
    try {
      if (document.execCommand('copy')) {
        done('已复制到剪贴板');
        return;
      }
    } catch {
      /* 保留可手动复制的选区。 */
    }
    done('已选中文本，请按 Ctrl+C（Mac 为 ⌘C）复制。');
  })();
}

export function DeviceAdmin({ target }: { target: User }) {
  const [devices, setDevices] = useState<DeviceInfo[] | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const refresh = () =>
    void api<DeviceInfo[]>(`/users/${target.id}/devices`)
      .then(setDevices)
      .catch((e) => setError((e as Error).message));
  useEffect(refresh, [target.id]);
  const revoke = async (path: string) => {
    setBusy(true);
    setError('');
    try {
      await api(path, 'DELETE');
      setDevices(await api(`/users/${target.id}/devices`));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="device-admin">
      <p className="muted">
        设备令牌让「{target.name}」在批准后的浏览器免密登录。撤销立即生效；IP
        只用于审计提示，不是登录凭证。换浏览器登录请先撤销设备，再由管理员重置密码。
      </p>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {!devices?.length && <p className="muted">暂无设备记录。</p>}
      {devices?.map((d) => (
        <div className={'device-row' + (d.revoked ? ' revoked' : '')} key={d.id}>
          <div>
            <strong>{d.device}</strong>
            <small>
              首次 {d.created.slice(0, 16).replace('T', ' ')} · {d.first_ip}
              {d.last_used && (
                <>
                  {' '}
                  · 最近 {d.last_used.slice(0, 16).replace('T', ' ')} · {d.last_ip}
                </>
              )}
            </small>
          </div>
          {d.revoked ? (
            <span className="muted">已撤销</span>
          ) : (
            <button disabled={busy} onClick={() => void revoke('/devices/' + d.id)}>
              撤销此设备
            </button>
          )}
        </div>
      ))}
      {!!devices?.some((d) => !d.revoked) && (
        <button className="full" disabled={busy} onClick={() => void revoke(`/users/${target.id}/devices`)}>
          撤销全部设备
        </button>
      )}
    </div>
  );
}

export function AccessAdmin({ onBusy }: { onBusy: (busy: boolean) => void }) {
  const [tab, setTab] = useState<'applications' | 'invites'>('applications');
  const [filter, setFilter] = useState<'pending' | 'approved' | 'rejected' | 'expired'>('pending');
  const [applications, setApplications] = useState<Application[]>([]);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [credential, setCredential] = useState<{ code: string; purpose: string } | null>(null);
  const [copied, setCopied] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [expanding, setExpanding] = useState<string | null>(null);
  const codeRef = useRef<HTMLTextAreaElement>(null);
  const activeTasks = tasks.filter((t) => t.status === 'active' || t.status === 'draft');

  const refresh = async () => {
    const [apps, invs, tks] = await Promise.all([
      api<Application[]>('/applications?status=' + filter),
      api<Invite[]>('/invites'),
      api<Task[]>('/tasks'),
    ]);
    setApplications(apps);
    setInvites(invs);
    setTasks(tks);
  };
  useEffect(() => {
    void refresh().catch((e) => setError((e as Error).message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter]);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    onBusy(true);
    setError('');
    try {
      await action();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      onBusy(false);
    }
  };

  return (
    <div className="access-admin">
      <div className="access-tabs" role="group" aria-label="管理区切换">
        <button className={tab === 'applications' ? 'selected' : ''} onClick={() => setTab('applications')}>
          <Users size={16} /> 加入申请
        </button>
        <button className={tab === 'invites' ? 'selected' : ''} onClick={() => setTab('invites')}>
          <KeyRound size={16} /> 邀请凭证
        </button>
      </div>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {tab === 'applications' ? (
        <>
          <div className="access-filters" role="group" aria-label="按状态筛选申请">
            {(['pending', 'approved', 'rejected', 'expired'] as const).map((s) => (
              <button
                key={s}
                className={filter === s ? 'selected' : ''}
                onClick={() => setFilter(s)}
              >
                {stateName[s]}
              </button>
            ))}
          </div>
          {!applications.length && <p className="muted">暂无{stateName[filter]}申请。</p>}
          {applications.map((a) => (
            <div className="application-row" key={a.id}>
              <div className="application-main">
                <strong>{a.display_name}</strong>
                <small>
                  {a.employee_id && <>工号 {a.employee_id} · </>}
                  {a.email && <>{a.email} · </>}
                  {a.created.slice(0, 16).replace('T', ' ')} · {a.device || '未知客户端'} · IP {a.ip}
                </small>
                <small>
                  邀请用途：{a.invite.purpose || '—'}
                  {a.invite.kind === 'task' && <> · 绑定任务「{a.invite.task_title}」</>}
                </small>
                {a.status !== 'pending' && (
                  <small className="muted">
                    {stateName[a.status]}
                    {a.decided_by && ` · 由 ${a.decided_by} 处理`}
                    {a.status === 'approved' && (a.claimed ? ' · 已在浏览器登录' : ' · 尚未领取登录状态')}
                  </small>
                )}
              </div>
              {a.status === 'pending' && (
                <div className="application-actions">
                  <button disabled={busy} onClick={() => setExpanding(expanding === a.id ? null : a.id)}>
                    {expanding === a.id ? '收起' : '批准…'}
                  </button>
                  <button
                    disabled={busy}
                    onClick={() =>
                      void run(async () => {
                        await api('/applications/' + a.id + '/reject', 'POST', {});
                        await refresh();
                      })
                    }
                  >
                    <ShieldX size={15} /> 拒绝
                  </button>
                </div>
              )}
              {a.status === 'pending' && expanding === a.id && (
                <form
                  className="approve-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    const form = new FormData(e.currentTarget);
                    void run(async () => {
                      await api('/applications/' + a.id + '/approve', 'POST', {
                        role: form.get('role'),
                        name: form.get('name'),
                        task_ids: form.getAll('task_ids'),
                      });
                      setExpanding(null);
                      await refresh();
                    });
                  }}
                >
                  <label>
                    账号名称
                    <input name="name" required maxLength={60} defaultValue={a.display_name} />
                  </label>
                  <label>
                    角色
                    <select name="role" defaultValue="reviewer">
                      <option value="reviewer">评测者：参与分配的任务</option>
                      <option value="organizer">组织者：可创建和上传任务</option>
                    </select>
                  </label>
                  {a.invite.kind === 'task' ? (
                    <p className="muted">任务邀请只能批准到「{a.invite.task_title}」。</p>
                  ) : (
                    <>
                      <span className="eyebrow">分配可参与的任务（准备中/进行中；已关闭任务冻结名单）</span>
                      {!activeTasks.length && <p className="muted">当前没有可分配的任务；可以稍后在任务里增补。</p>}
                      {activeTasks.map((t) => (
                        <label className="checkbox" key={t.id}>
                          <input type="checkbox" name="task_ids" value={t.id} />
                          {t.title}
                        </label>
                      ))}
                    </>
                  )}
                  <button className="primary full" disabled={busy}>
                    <Check size={16} /> 确认批准并创建账号
                  </button>
                </form>
              )}
            </div>
          ))}
        </>
      ) : (
        <>
          {credential && (
            <section className="credential-card" aria-label="本次邀请凭证">
              <h3>邀请凭证已生成</h3>
              <p>
                用途：{credential.purpose || '—'}。明文只显示这一次，关闭后无法再次查看；请通过可信渠道单独发给申请人。
              </p>
              <textarea
                ref={codeRef}
                aria-label="邀请凭证明文"
                readOnly
                value={credential.code}
                rows={2}
                spellCheck={false}
              />
              <div className="actions">
                <button
                  type="button"
                  onClick={() => copyText(credential.code, setCopied, codeRef.current)}
                >
                  <Copy size={15} /> 复制邀请凭证
                </button>
                <button type="button" onClick={() => setCredential(null)}>
                  隐藏凭证
                </button>
              </div>
              <small role="status">{copied}</small>
            </section>
          )}
          <form
            className="invite-form"
            onSubmit={(e) => {
              e.preventDefault();
              const data = Object.fromEntries(new FormData(e.currentTarget));
              void run(async () => {
                const result = await api<{ code: string }>('/invites', 'POST', {
                  purpose: data.purpose || '',
                  kind: data.kind,
                  task_id: data.task_id || '',
                  expires_days: Number(data.expires_days),
                  max_uses: Number(data.max_uses),
                });
                setCredential({ code: result.code, purpose: String(data.purpose || '') });
                setCopied('');
                await refresh();
              });
            }}
          >
            <h3>生成新邀请</h3>
            <label>
              用途 / 备注
              <input name="purpose" maxLength={120} placeholder="例如：声学组第一轮评测" />
            </label>
            <label>
              邀请类型
              <select name="kind" defaultValue="team">
                <option value="team">团队邀请：批准时由管理员选择任务</option>
                <option value="task">任务邀请：只能申请绑定的任务</option>
              </select>
            </label>
            <label>
              绑定任务（仅任务邀请需要）
              <select name="task_id" defaultValue="">
                <option value="">—</option>
                {tasks.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.title}（{t.status === 'active' ? '进行中' : t.status === 'draft' ? '准备中' : '已揭晓'}）
                  </option>
                ))}
              </select>
            </label>
            <div className="invite-numbers">
              <label>
                有效期（天）
                <input name="expires_days" type="number" min={1} max={365} defaultValue={7} required />
              </label>
              <label>
                最大使用次数
                <input name="max_uses" type="number" min={1} max={500} defaultValue={5} required />
              </label>
            </div>
            <small className="muted">
              使用次数在批准时消耗，拒绝不消耗；凭证明文只在生成时显示一次，服务端只保存加盐哈希。
            </small>
            <button className="primary full" disabled={busy}>
              生成邀请凭证
            </button>
          </form>
          <div className="invite-list">
            {!invites.length && <p className="muted">还没有邀请凭证。</p>}
            {invites.map((i) => {
              const expired = i.expires <= new Date().toISOString();
              return (
                <div className={'invite-row' + (i.active ? '' : ' disabled')} key={i.id}>
                  <div>
                    <strong>{i.purpose || '（无备注）'}</strong>
                    <small>
                      {i.kind === 'task' ? `任务邀请 · ${i.task_title}` : '团队邀请'} · 已使用 {i.used_count}/
                      {i.max_uses} · 创建 {i.created.slice(0, 16).replace('T', ' ')} · 有效期至{' '}
                      {i.expires.slice(0, 16).replace('T', ' ')}
                      {expired && ' · 已过期'}
                    </small>
                  </div>
                  <button
                    disabled={busy}
                    onClick={() =>
                      void run(async () => {
                        await api('/invites/' + i.id, 'PATCH', { active: !i.active });
                        await refresh();
                      })
                    }
                  >
                    {i.active ? '停用' : '启用'}
                  </button>
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
