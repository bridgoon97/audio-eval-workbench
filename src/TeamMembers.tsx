import { useRef, useState } from 'react';
import { api, type User } from './types';

// 18 个安全随机字节编码成 24 个 URL 安全字符；HTTP 局域网也可用 getRandomValues。
function generatePassword() {
  return btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(18))))
    .replaceAll('+', '-')
    .replaceAll('/', '_');
}
export function TeamMembers({
  members,
  onChange,
  onBusy,
  onDevices,
}: {
  members: User[];
  onChange: (members: User[]) => void;
  onBusy: (busy: boolean) => void;
  onDevices?: (user: User) => void;
}) {
  const [password, setPassword] = useState(generatePassword);
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [credential, setCredential] = useState<{ name: string; password: string } | null>(null);
  const [address, setAddress] = useState(window.location.origin);
  const credentialText = credential
    ? `听鉴登录地址：${address.trim() || window.location.origin}\n账号：${credential.name}\n密码：${credential.password}`
    : '';
  const [copied, setCopied] = useState('');
  const [target, setTarget] = useState<User | null>(null);
  const [resetPassword, setResetPassword] = useState('');
  const credentialRef = useRef<HTMLTextAreaElement>(null);
  function showCredential(name: string, value: string) {
    setCredential({ name, password: value });
    setCopied('');
  }
  async function run(action: () => Promise<void>) {
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
  }
  async function copy() {
    setCopied('');
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(credentialText);
        setCopied('已复制登录凭证');
        return;
      }
    } catch {
      /* HTTP 或浏览器权限限制时使用本地选择复制。 */
    }
    credentialRef.current?.focus();
    credentialRef.current?.select();
    try {
      if (document.execCommand('copy')) {
        setCopied('已复制登录凭证');
        return;
      }
    } catch {
      /* 保留可手动复制的选区。 */
    }
    setCopied('已选中文本，请按 Ctrl+C（Mac 为 ⌘C）复制。');
  }
  return (
    <div className="team-members">
      <label>
        同事登录地址
        <input
          type="url"
          value={address}
          onChange={(e) => setAddress(e.target.value)}
          autoComplete="off"
        />
      </label>
      <p className="muted">
        此地址仅用于凭证卡。如果你用 localhost 或 127.0.0.1
        打开软件，请改成同事实际使用的局域网地址；不会改变服务配置。
      </p>
      {credential && (
        <section className="credential-card" aria-label="本次登录凭证">
          <h3>登录凭证已生效</h3>
          <p>
            请单独交给对应同事。关闭窗口后不再显示明文，软件不提供原密码查询；遗失时可重新生成并重置。
          </p>
          <textarea
            ref={credentialRef}
            aria-label="本次登录凭证内容"
            readOnly
            value={credentialText}
            rows={3}
            spellCheck={false}
          />
          <div className="actions">
            <button type="button" onClick={() => void copy()}>
              复制登录凭证
            </button>
            <button type="button" onClick={() => setCredential(null)}>
              隐藏凭证
            </button>
          </div>
          <small role="status">{copied}</small>
        </section>
      )}
      <div className="member-list">
        {members.map((m) => (
          <div key={m.id}>
            <span>{m.name}</span>
            {m.role === 'admin' ? (
              <span className="muted">管理员</span>
            ) : (
              <>
                <select
                  aria-label={`${m.name}的角色`}
                  value={m.role}
                  disabled={busy || !!target}
                  onChange={(e) => {
                    const role = e.target.value;
                    void run(async () => {
                      await api('/users/' + m.id + '/role', 'PATCH', { role });
                      onChange(await api('/users'));
                    });
                  }}
                >
                  <option value="reviewer">评测者</option>
                  <option value="organizer">组织者（可上传任务）</option>
                </select>
                <button
                  type="button"
                  disabled={busy || !!target}
                  aria-label={`管理 ${m.name} 的登录设备`}
                  onClick={() => onDevices?.(m)}
                >
                  登录设备
                </button>
                <button
                  type="button"
                  disabled={busy || !!target}
                  aria-label={`重置 ${m.name} 的密码`}
                  onClick={() => {
                    setTarget(m);
                    setResetPassword(generatePassword());
                    setCredential(null);
                    setError('');
                  }}
                >
                  重置密码
                </button>
              </>
            )}
          </div>
        ))}
      </div>
      {target ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const name = new FormData(e.currentTarget).get('confirm_name');
            void run(async () => {
              await api('/users/' + target.id + '/password', 'POST', {
                password: resetPassword,
                confirm_name: name,
              });
              showCredential(target.name, resetPassword);
              setTarget(null);
              setResetPassword('');
            });
          }}
        >
          <h3>重置「{target.name}」的密码</h3>
          <p className="muted">
            确认后旧密码失效，该账号在所有设备上退出登录；其角色、任务和评测记录保留。
          </p>
          <label>
            将生效的新密码
            <input value={resetPassword} readOnly autoComplete="off" spellCheck={false} />
          </label>
          <button
            type="button"
            disabled={busy}
            onClick={() => setResetPassword(generatePassword())}
          >
            换一个随机密码
          </button>
          <label>
            输入账号名称确认
            <input name="confirm_name" required autoComplete="off" />
          </label>
          <div className="actions">
            <button className="primary" disabled={busy}>
              确认重置密码
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setTarget(null);
                setResetPassword('');
                setError('');
              }}
            >
              取消重置
            </button>
          </div>
        </form>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const form = e.currentTarget;
            const values = Object.fromEntries(new FormData(form));
            void run(async () => {
              await api('/users', 'POST', values);
              showCredential(String(values.name), String(values.password));
              form.reset();
              setPassword(generatePassword());
              setVisible(false);
              onChange(await api('/users'));
            });
          }}
        >
          <h3>添加团队成员</h3>
          <p className="muted">
            已自动生成随机初始密码，你也可以重新生成或自行填写。组织者可以上传并管理自己的任务；评测者参与分配给自己的任务。
          </p>
          <label>
            角色
            <select name="role">
              <option value="reviewer">评测者：参与分配的任务</option>
              <option value="organizer">组织者：可创建和上传任务</option>
            </select>
          </label>
          <label>
            账号
            <input name="name" required maxLength={60} autoComplete="off" />
          </label>
          <label>
            初始密码
            <input
              type={visible ? 'text' : 'password'}
              name="password"
              required
              minLength={10}
              maxLength={200}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
              spellCheck={false}
            />
          </label>
          <div className="actions">
            <button type="button" disabled={busy} onClick={() => setPassword(generatePassword())}>
              生成随机密码
            </button>
            <button type="button" onClick={() => setVisible(!visible)}>
              {visible ? '隐藏密码' : '显示密码'}
            </button>
          </div>
          <button className="primary full" disabled={busy}>
            {busy ? '正在创建…' : '创建账号'}
          </button>
        </form>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
