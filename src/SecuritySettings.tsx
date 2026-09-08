import { useState } from 'react';
import { Check, KeyRound } from 'lucide-react';
import { api } from './types';

// 个人安全设置：密码会话改密需核验当前密码；免密设备会话可直接设置新密码。
// 成功后服务端撤销本人其他会话/设备并轮换当前凭证，本浏览器保持在线。
export function SecuritySettings({
  authKind,
  onBusy,
}: {
  authKind: 'session' | 'device';
  onBusy: (busy: boolean) => void;
}) {
  const [done, setDone] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  return (
    <form
      className="security-settings"
      onSubmit={(e) => {
        e.preventDefault();
        const data = Object.fromEntries(new FormData(e.currentTarget));
        setBusy(true);
        onBusy(true);
        setError('');
        setDone('');
        void (async () => {
          try {
            await api('/me/password', 'POST', {
              current_password: authKind === 'session' ? data.current_password : '',
              new_password: data.new_password,
            });
            setDone('新密码已生效；其他浏览器与设备已退出，本浏览器保持登录。');
            (e.target as HTMLFormElement).reset();
          } catch (err) {
            setError((err as Error).message);
          } finally {
            setBusy(false);
            onBusy(false);
          }
        })();
      }}
    >
      <h3>
        <KeyRound size={17} /> 设置新密码
      </h3>
      {authKind === 'session' ? (
        <label>
          当前密码
          <input name="current_password" type="password" required autoComplete="current-password" />
        </label>
      ) : (
        <p className="muted">
          当前是免密设备登录：无需输入旧密码即可设置新密码；设置后请牢记，换浏览器时需要它。
        </p>
      )}
      <label>
        新密码（至少 10 个字符）
        <input
          name="new_password"
          type="password"
          required
          minLength={10}
          maxLength={200}
          autoComplete="new-password"
        />
      </label>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {done && (
        <p role="status" className="muted">
          <Check size={15} /> {done}
        </p>
      )}
      <button className="primary full" disabled={busy}>
        {busy ? '正在保存…' : '保存新密码'}
      </button>
      <small>保存后：本浏览器保持登录；其他浏览器与已撤销设备需重新登录或重新申请。</small>
    </form>
  );
}
