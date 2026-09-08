import { useEffect, useRef, useState } from 'react';
import { ArrowRight, Check, Clock, XCircle } from 'lucide-react';
import { api } from './types';

// 领取秘密：32 字节安全随机数的 URL 安全编码，仅保存在本浏览器；
// 服务端只存哈希，申请编号本身不是登录凭证。
function generateSecret() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes))
    .replaceAll('+', '-')
    .replaceAll('/', '_');
}

const STORE_KEY = 'apply-application';

type Stored = { id: string; secret: string };

function loadStored(): Stored | null {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw);
    if (typeof value?.id === 'string' && typeof value?.secret === 'string') return value;
  } catch {
    localStorage.removeItem(STORE_KEY);
  }
  return null;
}

export function ApplyFlow({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const stored = useRef(loadStored());
  const [application, setApplication] = useState<Stored | null>(stored.current);
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const finished = useRef(false);

  // 轮询申请状态；批准后立即领取设备 Cookie 自动登录。
  useEffect(() => {
    if (!application || finished.current) return;
    let alive = true;
    let timer = 0;
    const poll = async () => {
      try {
        const result = await api<{ status: string }>('/apply/status', 'POST', {
          application_id: application.id,
          claim_secret: application.secret,
        });
        if (!alive) return;
        setStatus(result.status);
        if (result.status === 'approved') {
          finished.current = true;
          try {
            await api('/apply/claim', 'POST', {
              application_id: application.id,
              claim_secret: application.secret,
            });
            onDone();
          } catch (e) {
            setError((e as Error).message);
          }
          return;
        }
        if (result.status === 'rejected' || result.status === 'expired') {
          // 终态：清除本地领取信息，回到表单并展示终态提示；重新申请会使用新秘密。
          localStorage.removeItem(STORE_KEY);
          setApplication(null);
          setStatus(result.status);
          return;
        }
      } catch (e) {
        if (alive) setError((e as Error).message);
      }
      if (alive) timer = window.setTimeout(poll, 4000);
    };
    void poll();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [application?.id]);

  if (application && status !== 'rejected' && status !== 'expired') {
    return (
      <div className="apply-flow">
        <span className="eyebrow">申请加入评测</span>
        <h2>
          <Clock size={20} /> 等待管理员批准
        </h2>
        <p className="muted">
          申请编号 <code>{application.id}</code>
        </p>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <ul className="apply-notes">
          <li>页面保持打开时会自动进入工作台；也可以关闭浏览器，批准后重新打开本页继续等待。</li>
          <li>此浏览器的登录状态不依赖 IP 地址；请勿把申请编号当作登录凭证分享。</li>
        </ul>
        <button
          className="full"
          onClick={() => {
            localStorage.removeItem(STORE_KEY);
            setApplication(null);
            setStatus('');
            setError('');
          }}
        >
          填写新的申请
        </button>
        <button className="full" onClick={onCancel}>
          返回登录
        </button>
      </div>
    );
  }

  return (
    <form
      className="apply-flow"
      onSubmit={(e) => {
        e.preventDefault();
        const data = Object.fromEntries(new FormData(e.currentTarget));
        setBusy(true);
        setError('');
        void (async () => {
          try {
            // 重试复用同一领取秘密：重复提交返回同一条申请，不会重复建单。
            const secret = application?.secret || generateSecret();
            const result = await api<{ id: string; status: string }>('/apply', 'POST', {
              display_name: data.display_name,
              employee_id: data.employee_id || '',
              email: data.email || '',
              invite_code: data.invite_code,
              claim_secret: secret,
            });
            const storedApp = { id: result.id, secret };
            localStorage.setItem(STORE_KEY, JSON.stringify(storedApp));
            finished.current = false;
            setApplication(storedApp);
            setStatus(result.status);
          } catch (err) {
            setError((err as Error).message);
          } finally {
            setBusy(false);
          }
        })();
      }}
    >
      <span className="eyebrow">申请加入评测</span>
      <h2>填写身份信息</h2>
      <p className="muted">提交后等待管理员批准；批准后本浏览器自动登录，无需密码。</p>
      <label>
        显示名称
        <input name="display_name" required maxLength={60} autoComplete="off" />
      </label>
      <label>
        工号（可选）
        <input name="employee_id" maxLength={60} autoComplete="off" />
      </label>
      <label>
        邮箱（可选）
        <input name="email" type="email" maxLength={120} autoComplete="off" />
      </label>
      <label>
        邀请凭证
        <input
          name="invite_code"
          required
          autoComplete="off"
          spellCheck={false}
          placeholder="向管理员索取"
        />
      </label>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <button className="primary full" disabled={busy}>
        {busy ? '正在提交…' : '提交申请'}
        <ArrowRight size={17} />
      </button>
      {status === 'rejected' && (
        <p className="muted" role="status">
          <XCircle size={15} /> 申请已被拒绝。如有疑问请联系管理员；重新申请需要新的有效邀请凭证。
        </p>
      )}
      {status === 'expired' && (
        <p className="muted" role="status">
          <XCircle size={15} /> 邀请凭证已失效，申请已过期。请向管理员重新获取邀请凭证后再次申请。
        </p>
      )}
      <small>申请结果只通知本人；不会暴露任务成员或其他申请人信息。</small>
      <button type="button" className="full" onClick={onCancel}>
        返回登录
      </button>
    </form>
  );
}
