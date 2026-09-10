import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  Activity,
  ArrowLeft,
  ArrowRight,
  AudioLines,
  Check,
  ChevronRight,
  Download,
  Headphones,
  HelpCircle,
  Layers3,
  LogOut,
  MessageSquare,
  Moon,
  Pause,
  Play,
  Plus,
  Repeat2,
  Settings2,
  Sun,
  Upload,
  UserRoundPlus,
  Users,
  X,
} from 'lucide-react';
import { AudioEngine } from './audio';
import { api, setCsrfToken, type Analysis, type Sample, type Task, type User } from './types';
import { distinctChoices, formatShare, lagText, shareWidth, type ReportPayload } from './review';
import packageInfo from '../package.json';
import { TeamMembers } from './TeamMembers';
import { UserGuide } from './UserGuide';
import { BatchImport } from './BatchImport';
import { CommentThread } from './CommentThread';
import { ProcessingModal } from './ProcessingModal';
import { Waveform } from './Waveform';
import { ApplyFlow } from './ApplyFlow';
import { AccessAdmin, DeviceAdmin } from './AccessAdmin';
import { SecuritySettings } from './SecuritySettings';
import './style.css';

const stateName: Record<string, string> = {
  draft: '准备中',
  active: '评测进行中',
  closed: '已揭晓',
};
const time = (n: number) =>
  `${Math.floor(n / 60)
    .toString()
    .padStart(2, '0')}:${(n % 60).toFixed(2).padStart(5, '0')}`;

function Modal({
  title,
  children,
  onClose,
}: {
  title: string;
  children: React.ReactNode;
  onClose: () => void;
}) {
  const ref = useRef<HTMLElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const selector =
      'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled)';
    (
      ref.current?.querySelector<HTMLElement>('input, textarea, select') ||
      ref.current?.querySelector<HTMLElement>(selector)
    )?.focus();
    const handle = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close.current();
      if (event.key !== 'Tab') return;
      const items = Array.from(ref.current?.querySelectorAll<HTMLElement>(selector) || []);
      if (!items.length) return;
      if (event.shiftKey && document.activeElement === items[0]) {
        event.preventDefault();
        items.at(-1)?.focus();
      } else if (!event.shiftKey && document.activeElement === items.at(-1)) {
        event.preventDefault();
        items[0].focus();
      }
    };
    document.addEventListener('keydown', handle);
    return () => {
      document.removeEventListener('keydown', handle);
      previous?.focus();
    };
  }, []);
  return (
    <div
      className="overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <section ref={ref} className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <header>
          <h2>{title}</h2>
          <button aria-label="关闭" className="icon" onClick={onClose}>
            <X size={20} />
          </button>
        </header>
        {children}
      </section>
    </div>
  );
}

function App() {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [authView, setAuthView] = useState<'login' | 'apply' | 'recover'>('login');
  const [authKind, setAuthKind] = useState<'session' | 'device'>('session');
  const [pendingCount, setPendingCount] = useState(0);
  const [serverVersion, setServerVersion] = useState('');
  const [serverInfo, setServerInfo] = useState<{
    version: string;
    instance_id: string;
    data_id: string;
    data_directory?: string;
  } | null>(null);
  const [syncMessage, setSyncMessage] = useState('每 5 秒同步');
  const [setup, setSetup] = useState(false);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [task, setTask] = useState<Task | null>(null);
  const [sample, setSample] = useState<Sample | null>(null);
  const [selected, setSelected] = useState(0);
  const [engine, setEngine] = useState<AudioEngine | null>(null);
  const [playing, setPlaying] = useState(false);
  const [position, setPosition] = useState(0);
  const [region, setRegion] = useState<[number, number]>([0, 0]);
  const [contentGuide, setContentGuide] = useState<number[]>([]);
  const [loop, setLoop] = useState(false);
  const [volume, setVolume] = useState(0.7);
  const [loaded, setLoaded] = useState(false);
  const [analyses, setAnalyses] = useState<Record<string, Analysis>>({});
  const [spectrum, setSpectrum] = useState(false);
  const [panel, setPanel] = useState<'notes' | 'vote'>('notes');
  const [comment, setComment] = useState('');
  const [tag, setTag] = useState('听感');
  const [reply, setReply] = useState<string | null>(null);
  const [choice, setChoice] = useState('');
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [modal, setModal] = useState('');
  const [trashed, setTrashed] = useState<Task[]>([]);
  const [purgePrep, setPurgePrep] = useState<{
    task_id: string;
    token: string;
    summary: Record<string, any>;
  } | null>(null);
  const [batchBusy, setBatchBusy] = useState(false);
  const [editTrack, setEditTrack] = useState<{ id: string; name: string; version: string } | null>(
    null,
  );
  const [members, setMembers] = useState<User[]>([]);
  const [deviceTarget, setDeviceTarget] = useState<User | null>(null);
  const [publishSummary, setPublishSummary] = useState<any>(null);
  const [progress, setProgress] = useState<ReportPayload['参与进度'] | null>(null);
  const [report, setReport] = useState<ReportPayload | null>(null);
  const [dark, setDark] = useState(localStorage.getItem('theme') === 'dark');
  const [filter, setFilter] = useState('');
  const [saveState, setSaveState] = useState('');
  const activeSample = useRef('');
  const duration = (sample?.samples || 0) / 16000;
  const replyTarget = sample?.comments?.find((item) => item.id === reply);

  useEffect(() => {
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    localStorage.setItem('theme', dark ? 'dark' : 'light');
  }, [dark]);
  const refreshTasks = async () => setTasks(await api<Task[]>('/tasks'));
  const initialize = async () => {
    const status = await api('/status');
    setSetup(status.needs_setup);
    setServerVersion(status.version);
    if (!status.needs_setup) {
      try {
        const me = await api<{
          id: string;
          name: string;
          role: string;
          csrf_token?: string;
          pending_applications?: number;
          auth_kind?: 'session' | 'device';
        }>('/me');
        // CSRF 令牌随 /api/me 下发，供敏感管理操作回传。
        setCsrfToken(me.csrf_token || '');
        setAuthKind(me.auth_kind === 'device' ? 'device' : 'session');
        setPendingCount(me.pending_applications || 0);
        setUser(me);
        await refreshTasks();
      } catch {
        setCsrfToken('');
        setPendingCount(0);
        setUser(null);
      }
    }
    setReady(true);
  };
  useEffect(() => {
    void initialize().catch((e) => {
      setError(e.message);
      setReady(true);
    });
  }, []);
  const live = useRef({ user, task, sample, engine, busy, batchBusy });
  live.current = { user, task, sample, engine, busy, batchBusy };
  const syncing = useRef(false);
  // 同步代次：本地 mutation 成功时递增；在飞同步在每个写 state 点前核对，
  // 陈旧结果直接丢弃（防止删除片段后被旧响应覆盖）。
  const syncEpoch = useRef(0);
  const synchronize = async () => {
    const epoch = syncEpoch.current;
    const current = live.current;
    if (!current.user || syncing.current || current.busy || current.batchBusy) return;
    syncing.current = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 10000);
    const read = <T,>(path: string) => api<T>(path, 'GET', undefined, controller.signal);
    try {
      const [nextUser, nextTasks, status] = await Promise.all([
        read<User>('/me'),
        read<Task[]>('/tasks'),
        read<{ version: string }>('/status'),
      ]);
      if (syncEpoch.current !== epoch || live.current.user?.id !== current.user.id) return;
      setServerVersion(status.version);
      if (nextUser.role === 'admin' && typeof (nextUser as any).pending_applications === 'number')
        setPendingCount((nextUser as any).pending_applications);
      if (typeof (nextUser as any).csrf_token === 'string')
        setCsrfToken((nextUser as any).csrf_token);
      setUser((previous) =>
        previous?.role === nextUser.role && previous.name === nextUser.name ? previous : nextUser,
      );
      setTasks(nextTasks);
      if (current.task && live.current.task?.id === current.task.id) {
        const visibleTask = nextTasks.find((t) => t.id === current.task!.id);
        if (!visibleTask) {
          live.current.engine?.stop();
          setPlaying(false);
          setTask(null);
          setSample(null);
          setModal('');
          setNotice('当前任务已移除或访问权限已变更，列表已同步。');
        } else {
          const nextTask = await read<Task>('/tasks/' + current.task.id);
          if (syncEpoch.current !== epoch) return; // 陈旧同步：本地 mutation 已更新状态
          if (
            live.current.user?.id !== current.user.id ||
            live.current.task?.id !== current.task.id
          )
            return;
          if (
            nextTask.status !== current.task.status ||
            nextTask.can_manage !== current.task.can_manage
          )
            setModal('');
          setTask(nextTask);
          if (current.sample && live.current.sample?.id === current.sample.id) {
            const nextSample = await read<Sample>('/samples/' + current.sample.id).catch(
              () => null, // 片段已被本地 mutation 删除：静默丢弃，不视为连接错误
            );
            if (syncEpoch.current !== epoch || !nextSample) return;
            if (
              live.current.user?.id === current.user.id &&
              live.current.sample?.id === current.sample.id
            )
              setSample(nextSample);
          }
        }
      }
      setSyncMessage('已同步 · ' + new Date().toLocaleTimeString());
    } catch (e) {
      if (live.current.user?.id !== current.user?.id) return;
      if ((e as Error & { status?: number }).status === 401) {
        live.current.engine?.stop();
        setPlaying(false);
        setUser(null);
        setTask(null);
        setSample(null);
        setModal('');
        setTasks([]);
        setCsrfToken('');
        setPendingCount(0);
        setError('登录已失效或密码已重置，请重新登录。');
      } else setSyncMessage('连接暂时失败，稍后重试或点击同步');
    } finally {
      window.clearTimeout(timeout);
      syncing.current = false;
    }
  };
  const syncLatest = useRef(synchronize);
  syncLatest.current = synchronize;
  useEffect(() => {
    if (!user?.id) return;
    const sync = () => {
      if (document.visibilityState !== 'hidden') void syncLatest.current();
    };
    const timer = window.setInterval(sync, 5000);
    window.addEventListener('focus', sync);
    document.addEventListener('visibilitychange', sync);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', sync);
      document.removeEventListener('visibilitychange', sync);
    };
  }, [user?.id]);
  const run = async (action: () => Promise<void>) => {
    setError('');
    setNotice('');
    // action 一旦开始就作废此前启动的后台同步。React 的 busy state
    // 要到下一次 render 才进入 live ref，不能靠它阻止已经在飞的旧响应。
    syncEpoch.current += 1;
    setBusy(true);
    try {
      await action();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const openSample = useCallback(
    async (id: string) => {
      activeSample.current = id;
      const s = await api<Sample>('/samples/' + id);
      if (activeSample.current !== id) return;
      setSample(s);
      setSelected(0);
      setRegion([0, s.samples / 16000]);
      setChoice(s.rating?.choice || '');
      setReason(s.rating?.reason || '');
      setReply(null);
      setLoop(false);
      setPosition(0);
      setSaveState('');
      setComment(localStorage.getItem(`draft:${user?.id}:${id}`) || '');
    },
    [user?.id],
  );
  const openTask = async (id: string) => {
    engine?.stop();
    setPlaying(false);
    setFilter('');
    const t = await api<Task>('/tasks/' + id);
    setTask(t);
    setSample(null);
    if (t.samples?.length) await openSample(t.samples[0].id);
  };
  const refreshSample = async () => {
    if (!sample) return;
    const s = await api<Sample>('/samples/' + sample.id);
    setSample(s);
    if (task) setTask(await api('/tasks/' + task.id));
  };
  useEffect(() => {
    if (!sample?.tracks?.length) {
      setLoaded(false);
      return;
    }
    const next = new AudioEngine();
    next.setVolume(volume);
    setEngine(next);
    setLoaded(false);
    setPlaying(false);
    setAnalyses({});
    setContentGuide([]);
    const abort = new AbortController();
    let valid = true;
    void next
      .load(
        sample.tracks.map((t) => '/api/audio/' + t.id),
        abort.signal,
      )
      .then(() => {
        if (valid) {
          setLoaded(true);
          if (sample.blind) setContentGuide(next.contentGuide());
        }
      })
      .catch((e) => {
        if (valid) setError(e.message);
      });
    if (!sample.blind)
      sample.tracks.forEach((t) => {
        void api<Analysis>('/audio/' + t.id + '/analysis')
          .then((a) => {
            if (valid) setAnalyses((prev) => ({ ...prev, [t.id]: a }));
          })
          .catch((e) => {
            if (valid) setError(e.message);
          });
      });
    return () => {
      valid = false;
      abort.abort();
      next.dispose();
    };
  }, [sample?.id, sample?.tracks?.map((t) => t.id).join(','), sample?.blind]);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (engine?.playing) {
        setPosition(engine.position);
        if (!engine.loop && engine.position >= (engine.stopAt ?? engine.duration)) {
          engine.stop();
          setPlaying(false);
        }
      }
    }, 35);
    return () => clearInterval(timer);
  }, [engine]);
  const play = async () => {
    if (!engine || !loaded) return;
    if (engine.playing) {
      engine.stop();
      setPlaying(false);
    } else {
      const hasRegion = region[1] > region[0];
      const offset =
        hasRegion && (engine.offset < region[0] || engine.offset >= region[1])
          ? region[0]
          : engine.offset;
      await engine.play(
        offset,
        loop && hasRegion ? region : undefined,
        !loop && hasRegion ? region[1] : undefined,
      );
      setPlaying(true);
    }
  };
  const select = (index: number) => {
    setSelected(index);
    engine?.select(index);
  };
  const selectRegion = (r: [number, number]) => {
    setRegion(r);
    if (engine) {
      engine.stop();
      engine.loop = undefined;
      engine.stopAt = undefined;
      engine.offset = r[0];
      setPosition(r[0]);
      setPlaying(false);
    }
  };
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (modal || /INPUT|TEXTAREA|SELECT/.test((event.target as HTMLElement).tagName)) return;
      if (event.code === 'Space') {
        event.preventDefault();
        void play();
      }
      if (/^[1-6]$/.test(event.key) && Number(event.key) <= (sample?.tracks?.length || 0))
        select(Number(event.key) - 1);
      if (event.key.toLowerCase() === 'l') {
        engine?.stop();
        setPlaying(false);
        setLoop(!loop);
      }
      if (event.key.toLowerCase() === 'c') {
        setPanel('notes');
        document.querySelector<HTMLTextAreaElement>('#comment')?.focus();
      }
    };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  });
  const submitComment = () =>
    run(async () => {
      if (!sample) return;
      await api('/samples/' + sample.id + '/comments', 'POST', {
        track_id: sample.tracks?.[selected]?.id || null,
        start: Math.round(region[0] * 16000),
        end: Math.round(region[1] * 16000),
        body: comment,
        tag,
        parent: reply,
      });
      localStorage.removeItem(`draft:${user?.id}:${sample.id}`);
      setComment('');
      setReply(null);
      setSaveState('已保存到主机');
      await refreshSample();
    });
  // 复盘跳回：从结果/标签定位回片段试听区与评论楼层。
  const jumpToSample = (sampleId: string) => {
    setModal('');
    setPanel('notes');
    void run(() => openSample(sampleId));
  };

  if (!ready)
    return (
      <div className="loading">
        <AudioLines size={38} />
        <p>正在连接听鉴…</p>
      </div>
    );
  if (!user)
    return authView === 'recover' ? (
      <div className="auth-shell">
        <div className="auth-brand">
          <AudioLines size={46} />
          <h1>听鉴</h1>
          <p>让每一次听感判断，有据可循。</p>
          <div className="auth-lines">
            {Array.from({ length: 45 }, (_, i) => (
              <i
                key={i}
                style={{
                  height: `${15 + Math.abs(Math.sin(i * 0.7) * Math.cos(i * 0.14)) * 100}px`,
                }}
              />
            ))}
          </div>
          <span>音频版本比较 · 片段标注 · 团队评测</span>
        </div>
        <form
          className="apply-flow"
          onSubmit={(e) => {
            e.preventDefault();
            const data = Object.fromEntries(new FormData(e.currentTarget));
            void run(async () => {
              await api('/recover', 'POST', data);
              setAuthView('login');
              await initialize();
            });
          }}
        >
          <span className="eyebrow">账号恢复</span>
          <h2>凭恢复凭证设置新密码</h2>
          <p className="muted">
            输入管理员提供的一次性恢复凭证并设置新密码；成功后本浏览器直接登录，旧密码作废。
          </p>
          <label>
            恢复凭证
            <input
              name="recovery_code"
              required
              autoComplete="off"
              spellCheck={false}
              placeholder="形如 xxxx.yyyy 的一次性凭证"
            />
          </label>
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
          <button className="primary full" disabled={busy}>
            {busy ? '正在恢复…' : '设置新密码并登录'}
          </button>
          <small>凭证一次性有效；过期或已使用时请联系管理员重新生成。</small>
          <button
            type="button"
            className="full"
            onClick={() => {
              setError('');
              setAuthView('login');
            }}
          >
            返回登录
          </button>
        </form>
      </div>
    ) : authView === 'apply' ? (
      <div className="auth-shell">
        <div className="auth-brand">
          <AudioLines size={46} />
          <h1>听鉴</h1>
          <p>让每一次听感判断，有据可循。</p>
          <div className="auth-lines">
            {Array.from({ length: 45 }, (_, i) => (
              <i
                key={i}
                style={{
                  height: `${15 + Math.abs(Math.sin(i * 0.7) * Math.cos(i * 0.14)) * 100}px`,
                }}
              />
            ))}
          </div>
          <span>音频版本比较 · 片段标注 · 团队评测</span>
        </div>
        <ApplyFlow
          onDone={() => {
            setAuthView('login');
            void initialize();
          }}
          onCancel={() => setAuthView('login')}
        />
      </div>
    ) : (
      <div className="auth-shell">
        <div className="auth-brand">
          <AudioLines size={46} />
          <h1>听鉴</h1>
          <p>让每一次听感判断，有据可循。</p>
          <div className="auth-lines">
            {Array.from({ length: 45 }, (_, i) => (
              <i
                key={i}
                style={{
                  height: `${15 + Math.abs(Math.sin(i * 0.7) * Math.cos(i * 0.14)) * 100}px`,
                }}
              />
            ))}
          </div>
          <span>音频版本比较 · 片段标注 · 团队评测</span>
        </div>
        <form
          className="auth-form"
          onSubmit={(e) => {
            e.preventDefault();
            const f = new FormData(e.currentTarget);
            void run(async () => {
              const body = Object.fromEntries(f);
              if (setup) await api('/setup', 'POST', body);
              await api('/login', 'POST', body);
              await initialize();
            });
          }}
        >
          <span className="eyebrow">本地与局域网工作台</span>
          <h2>{setup ? '创建你的工作台' : '欢迎回来'}</h2>
          <p className="muted">
            {setup
              ? '初始化密钥显示在主机启动窗口，仅首次使用需要。'
              : '使用组织者为你创建的账号登录，或凭邀请凭证申请加入。'}
          </p>
          {setup && (
            <label>
              初始化密钥
              <input name="setup_key" required autoComplete="off" />
            </label>
          )}
          <label>
            账号
            <input name="name" required autoComplete="username" />
          </label>
          <label>
            密码
            <input
              name="password"
              type="password"
              minLength={setup ? 10 : 1}
              required
              autoComplete={setup ? 'new-password' : 'current-password'}
            />
          </label>
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          <button className="primary" disabled={busy}>
            {busy ? '正在连接…' : setup ? '创建并进入' : '进入工作台'}
            <ArrowRight size={18} />
          </button>
          {!setup && (
            <>
              <button
                type="button"
                className="full apply-entry"
                onClick={() => {
                  setError('');
                  setNotice('');
                  setAuthView('apply');
                }}
              >
                申请加入评测
                <ArrowRight size={16} />
              </button>
              <button
                type="button"
                className="text-button"
                onClick={() => {
                  setError('');
                  setNotice('');
                  setAuthView('recover');
                }}
              >
                凭恢复凭证设置新密码
              </button>
            </>
          )}
          <small>音频和评测结果保存在托管这项服务的电脑上。</small>
        </form>
      </div>
    );

  return (
    <div className="app-shell">
      <aside className="rail">
        <button
          className="brand"
          title="听鉴首页"
          onClick={() => {
            engine?.stop();
            setPlaying(false);
            setTask(null);
            setSample(null);
            void refreshTasks();
          }}
        >
          <AudioLines size={27} />
          <strong>听鉴</strong>
        </button>
        <div className="rail-label">工作空间</div>
        <button
          className={!task ? 'nav active' : 'nav'}
          onClick={() => {
            engine?.stop();
            setPlaying(false);
            setTask(null);
            setSample(null);
            void refreshTasks();
          }}
        >
          <Layers3 size={19} />
          评测任务<span>{tasks.length}</span>
        </button>
        <button className="nav" onClick={() => setModal('help')}>
          <HelpCircle size={19} />
          使用指南
        </button>
        {user.role === 'admin' && (
          <button
            className="nav"
            onClick={() => setModal('access')}
            aria-label={`加入与邀请，${pendingCount} 条待审批`}
          >
            <UserRoundPlus size={19} />
            加入与邀请
            {pendingCount > 0 && <span className="pending-badge">{pendingCount}</span>}
          </button>
        )}
        {user.role === 'admin' && (
          <button
            className="nav"
            onClick={() =>
              void run(async () => {
                setMembers(await api('/users'));
                setModal('users');
              })
            }
          >
            <Users size={19} />
            团队成员
          </button>
        )}
        <div className="rail-bottom">
          <div className="server-state">
            <i /> 数据保存在服务主机
          </div>
          <button className="nav" onClick={() => setDark(!dark)}>
            {dark ? <Sun size={18} /> : <Moon size={18} />} {dark ? '浅色外观' : '深色外观'}
          </button>
          <div className="profile">
            <span className="avatar">{user.name.slice(0, 1)}</span>
            <div>
              <strong>{user.name}</strong>
              <small>
                {{ admin: '管理员', organizer: '组织者', reviewer: '评测者' }[user.role]}
              </small>
            </div>
            <button className="text-button security-entry" onClick={() => setModal('security')}>
              账号与安全
            </button>
            <button
              className="icon"
              aria-label="退出登录"
              onClick={() =>
                void run(async () => {
                  engine?.dispose();
                  await api('/logout', 'POST');
                  setCsrfToken('');
                  setPendingCount(0);
                  setAuthView('login');
                  setUser(null);
                  setTask(null);
                  setSample(null);
                })
              }
            >
              <LogOut size={17} />
            </button>
          </div>
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <div className="breadcrumb">
            工作空间 <ChevronRight size={15} />
            <span>{task ? task.title : '评测任务'}</span>
          </div>
          <div className="sync-tools">
            <button className="text-button" onClick={() => void synchronize()} title={syncMessage}>
              同步
            </button>
            <span className="sync-state" role="status">
              {syncMessage}
            </span>
            <button
              className="text-button"
              onClick={() =>
                void run(async () => {
                  setServerInfo(await api('/server-info'));
                  setModal('system');
                })
              }
            >
              服务信息 · {serverVersion || '连接中'}
            </button>
          </div>
        </header>
        {serverVersion && serverVersion !== packageInfo.version && (
          <div className="banner error" role="alert">
            <span>
              页面版本 {packageInfo.version} 与服务版本 {serverVersion}{' '}
              不一致。请先保存标注，再重新加载；若服务版本仍旧，请检查主机是否还在运行旧程序。
            </span>
            <button onClick={() => window.location.reload()}>重新加载页面</button>
          </div>
        )}
        {(error || notice) && (
          <div
            role={error ? 'alert' : 'status'}
            className={error ? 'banner error' : 'banner success'}
          >
            {error || notice}
            <button
              className="icon"
              aria-label="关闭提示"
              onClick={() => {
                setError('');
                setNotice('');
              }}
            >
              <X size={17} />
            </button>
          </div>
        )}
        {!task ? (
          <main className="dashboard">
            <div className="page-heading">
              <div>
                <span className="eyebrow">听音 · 判断 · 复盘</span>
                <h1>评测任务</h1>
                <p className="muted">把同一段声音的不同答案，放在一起听。</p>
              </div>
              {user.role !== 'reviewer' && (
                <button className="primary" onClick={() => setModal('new')}>
                  <Plus size={18} />
                  新建评测
                </button>
              )}
            </div>
            <div className="stats">
              <div>
                <span>全部任务</span>
                <strong>{tasks.length.toString().padStart(2, '0')}</strong>
                <Layers3 />
              </div>
              <div>
                <span>正在评测</span>
                <strong>
                  {tasks
                    .filter((t) => t.status === 'active')
                    .length.toString()
                    .padStart(2, '0')}
                </strong>
                <Headphones />
              </div>
              <div>
                <span>已揭晓</span>
                <strong>
                  {tasks
                    .filter((t) => t.status === 'closed')
                    .length.toString()
                    .padStart(2, '0')}
                </strong>
                <Check />
              </div>
            </div>
            <div className="section-title">
              <h2>最近的评测</h2>
              <span className="muted">{tasks.length} 项任务</span>
            </div>
            {!tasks.length ? (
              <div className="empty">
                <AudioLines size={48} />
                <h2>从一组音频开始</h2>
                <p>导入同一片段的多个算法版本，或先用合成音熟悉操作。</p>
                {user.role !== 'reviewer' && (
                  <button
                    onClick={() =>
                      void run(async () => {
                        const t = await api('/demo', 'POST');
                        await refreshTasks();
                        await openTask(t.id);
                      })
                    }
                    disabled={busy}
                  >
                    创建合成音演示
                    <ArrowRight size={17} />
                  </button>
                )}
              </div>
            ) : (
              <div className="task-grid">
                {tasks.map((t) => (
                  <button
                    className="task-card"
                    key={t.id}
                    onClick={() => void run(() => openTask(t.id))}
                  >
                    <div className="card-top">
                      <span className="task-icon">
                        <AudioLines size={23} />
                      </span>
                      <span className={'badge ' + t.status}>{stateName[t.status]}</span>
                    </div>
                    <h3>{t.title}</h3>
                    <p>
                      {t.kind} · {t.mode === 'blind' ? '隐藏版本偏好评测' : '开发诊断与试听'}
                    </p>
                    <div className="card-footer">
                      <span>{t.sample_count} 个片段</span>
                      <span>
                        {t.completed}/{t.sample_count} 已评
                        <ArrowRight size={16} />
                      </span>
                    </div>
                  </button>
                ))}
              </div>
            )}
            <div className="dashboard-footer">
              <span>听鉴 {serverVersion || packageInfo.version} · 本地优先</span>
              {user.role !== 'reviewer' && (
                <button
                  className="text-button"
                  onClick={() =>
                    void run(async () => {
                      setTrashed(await api('/tasks?deleted=true'));
                      setModal('trash');
                    })
                  }
                >
                  回收站
                </button>
              )}
              {user.role === 'admin' && (
                <>
                  <button
                    className="text-button"
                    disabled={busy}
                    onClick={() =>
                      void run(async () => {
                        const t = await api('/demo', 'POST');
                        await refreshTasks();
                        await openTask(t.id);
                      })
                    }
                  >
                    添加合成演示
                  </button>
                  <a href="/api/backup">下载完整备份（含音频）</a>
                </>
              )}
            </div>
          </main>
        ) : (
          <>
            <section className="task-heading">
              <div>
                <button
                  className="text-button"
                  onClick={() => {
                    engine?.stop();
                    setTask(null);
                    setSample(null);
                    void refreshTasks();
                  }}
                >
                  <ArrowLeft size={15} />
                  全部任务
                </button>
                <h1>
                  {task.title}
                  <span className={'badge ' + task.status}>{stateName[task.status]}</span>
                </h1>
                <p>
                  {task.kind} <span>／</span>{' '}
                  {sample?.blind
                    ? '独立判断 · 关闭任务后统一揭晓'
                    : task.mode === 'blind'
                      ? '隐藏版本评测'
                      : '开发诊断'}{' '}
                  <span>／</span> 原始相对电平
                </p>
              </div>
              <div className="actions">
                {task.can_manage && task.status === 'draft' && (
                  <>
                    <button onClick={() => setModal('edit-task')}>编辑任务</button>
                    <button onClick={() => setModal('batch')}>批量导入</button>
                    <button onClick={() => setModal('sample')}>
                      <Plus size={17} />
                      添加片段
                    </button>
                    <button
                      className="primary"
                      onClick={() =>
                        void run(async () => {
                          setMembers(await api('/users'));
                          setPublishSummary(await api('/tasks/' + task.id + '/processing-summary'));
                          setModal('publish');
                        })
                      }
                    >
                      发布评测
                      <ArrowRight size={17} />
                    </button>
                  </>
                )}
                {task.can_manage && task.status === 'active' && (
                  <>
                    <button
                      onClick={() =>
                        void run(async () => {
                          setMembers(await api('/users'));
                          setModal('members');
                        })
                      }
                    >
                      受邀评测者
                    </button>
                    <button
                      onClick={() =>
                        void run(async () => {
                          const data = await api<{ 参与进度: ReportPayload['参与进度'] }>(
                            '/tasks/' + task.id + '/progress',
                          );
                          setProgress(data['参与进度']);
                          setModal('progress');
                        })
                      }
                    >
                      <Activity size={17} />
                      评测进度
                    </button>
                    <button onClick={() => setModal('close')}>
                      <Check size={17} />
                      关闭并揭晓
                    </button>
                  </>
                )}
                {task.can_manage && (
                  <button onClick={() => setModal('delete-task')}>删除任务</button>
                )}
                {task.status === 'closed' && (
                  <button
                    onClick={() =>
                      void run(async () => {
                        setReport(await api('/tasks/' + task.id + '/report'));
                        setModal('report');
                      })
                    }
                  >
                    <Activity size={17} />
                    查看结果
                  </button>
                )}
              </div>
            </section>
            <main className="workspace">
              <aside className="sample-list">
                <div className="sample-list-title">
                  音频片段 <span>{task.samples?.length || 0}</span>
                </div>
                <input
                  placeholder="搜索片段或场景"
                  aria-label="搜索片段"
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                />
                {task.samples
                  ?.filter((s) => (s.name + s.scene).includes(filter))
                  .map((s, i) => (
                    <button
                      className={'sample-item ' + (sample?.id === s.id ? 'current' : '')}
                      key={s.id}
                      onClick={() => void run(() => openSample(s.id))}
                    >
                      <span className="sample-index">
                        {s.completed ? <Check size={15} /> : (i + 1).toString().padStart(2, '0')}
                      </span>
                      <div>
                        <strong>{s.name}</strong>
                        <small>{s.scene || `${s.track_count} 个版本`}</small>
                      </div>
                    </button>
                  ))}
                <div className="sample-list-tip">
                  <Headphones size={18} />
                  <p>建议使用同一副耳机，保持系统音量和音效设置一致。</p>
                </div>
              </aside>
              {!sample ? (
                <div className="empty workspace-empty">
                  <Upload size={42} />
                  <h2>添加第一段音频</h2>
                  <p>一个片段对应同一时间范围的多个版本。</p>
                  {task.can_manage && <button onClick={() => setModal('sample')}>添加片段</button>}
                </div>
              ) : (
                <>
                  <section className="tracks-area">
                    <div className="tracks-heading">
                      <div>
                        <h2>{sample.name}</h2>
                        <p>{sample.scene || '场景未填写'}</p>
                      </div>
                      <div className="view-tabs">
                        {task.can_manage && task.status === 'draft' && (
                          <>
                            <button onClick={() => setModal('edit-sample')}>编辑片段</button>
                            <button
                              onClick={() => {
                                if (
                                  window.confirm(
                                    `删除整个草稿片段「${sample?.name}」？其全部候选、派生资产与未引用音频将一并移除，已有标注或评分的片段不能删除。`,
                                  )
                                )
                                  void run(async () => {
                                    const deletedId = sample!.id;
                                    await api('/samples/' + deletedId, 'DELETE');
                                    // 第一步：基于本地状态立即清空当前片段（不等
                                    // 二次 GET；删除期间 sync 因 epoch 递增而丢弃
                                    // 陈旧结果，也不会再请求已删片段）。
                                    setSample(null);
                                    setModal('');
                                    // 第二步：重拉任务详情与列表（此时 UI 已离开
                                    // 已删片段，无竞态窗口）。
                                    await refreshTasks();
                                    await openTask(task.id);
                                    setNotice('片段已删除；未被引用的音频空间已释放。');
                                  });
                              }}
                            >
                              删除片段
                            </button>
                            <button
                              className={modal === 'processing' ? 'selected' : ''}
                              onClick={() => setModal('processing')}
                            >
                              对齐与响度
                            </button>
                          </>
                        )}
                        <button
                          className={!spectrum ? 'selected' : ''}
                          disabled={sample.blind}
                          onClick={() => setSpectrum(false)}
                        >
                          波形
                        </button>
                        <button
                          className={spectrum ? 'selected' : ''}
                          disabled={sample.blind}
                          onClick={() => setSpectrum(true)}
                        >
                          频谱
                        </button>
                      </div>
                    </div>
                    <div className="sample-meta">
                      <span>{time(duration)}</span>
                      <span>16 kHz</span>
                      <span>
                        {sample.provenance === 'PUBLIC reproducible'
                          ? '公开 / 合成'
                          : sample.provenance === 'DECLASSIFIED real-device'
                            ? '已脱密真机'
                            : '受限 · 本地验证'}
                      </span>
                      {spectrum && !sample.blind && <span>共用色阶 −100～0 dB</span>}
                    </div>
                    <div className="tracks-scroll">
                      <div className="ruler">
                        <span>00:00</span>
                        <span>{time(duration / 4)}</span>
                        <span>{time(duration / 2)}</span>
                        <span>{time(duration * 0.75)}</span>
                        <span>{time(duration)}</span>
                      </div>
                      {sample.tracks?.map((tr, i) => (
                        <article
                          className={'track ' + (selected === i ? 'selected' : '')}
                          key={tr.id}
                        >
                          <div className="track-head">
                            <button className="track-select" onClick={() => select(i)}>
                              <span className="track-letter">{tr.label}</span>
                              <strong>{tr.name}</strong>
                              {selected === i && <span className="listening">当前试听</span>}
                            </button>
                            <span className="track-version">{tr.version}</span>
                            {task.can_manage && task.status === 'draft' && (
                              <button
                                className="text-button"
                                aria-label={`管理候选 ${tr.name}`}
                                onClick={() => {
                                  setEditTrack(tr);
                                  setModal('edit-track');
                                }}
                              >
                                管理
                              </button>
                            )}
                          </div>
                          <div className="wave-container">
                            <Waveform
                              data={analyses[tr.id]}
                              spectrum={spectrum && !sample.blind}
                              selected={selected === i}
                              duration={duration}
                              region={region}
                              contentGuide={sample.blind ? contentGuide : undefined}
                              onRegion={selectRegion}
                            />
                            <div
                              className="playhead"
                              style={{ left: `${duration ? (position / duration) * 100 : 0}%` }}
                            />
                          </div>
                          {tr.处理 && (
                            <div className="track-meta proc-live">
                              <span className="proc-badge">
                                派生试听 ·{' '}
                                {tr.处理.模式 === '对齐+响度' || tr.处理.模式 === '对齐'
                                  ? `对齐 ${lagText(tr.处理.lag).samples}（${lagText(tr.处理.lag).direction}）`
                                  : '时间位置未变'}
                                {tr.处理.模式 === '对齐+响度' || tr.处理.模式 === '响度'
                                  ? ` · 活动段 RMS ${tr.处理.gain_db > 0 ? '+' : ''}${tr.处理.gain_db.toFixed(2)} dB`
                                  : ''}
                              </span>
                            </div>
                          )}
                          {tr.meta && (
                            <div className="track-meta">
                              <span>RMS {tr.meta.rms_dbfs.toFixed(1)} dBFS</span>
                              <span>峰值 {tr.meta.peak.toFixed(3)}</span>
                              <span>
                                {tr.meta.clipped_samples
                                  ? `${tr.meta.clipped_samples} 个近满幅采样`
                                  : '无近满幅采样'}
                              </span>
                            </div>
                          )}
                        </article>
                      ))}
                      {task.can_manage && task.status === 'draft' && (
                        <button className="add-track" onClick={() => setModal('track')}>
                          <Plus size={18} />
                          导入这个片段的另一个版本
                        </button>
                      )}
                    </div>
                    <div className="region-bar">
                      <span>标注选区</span>
                      <label>
                        起点
                        <input
                          type="number"
                          step=".01"
                          min={0}
                          max={duration}
                          value={Number(region[0].toFixed(3))}
                          onChange={(e) =>
                            selectRegion([
                              Math.max(0, Math.min(Number(e.target.value), region[1])),
                              region[1],
                            ])
                          }
                        />
                      </label>
                      <span>—</span>
                      <label>
                        终点
                        <input
                          type="number"
                          step=".01"
                          min={region[0]}
                          max={duration}
                          value={Number(region[1].toFixed(3))}
                          onChange={(e) =>
                            selectRegion([
                              region[0],
                              Math.max(region[0], Math.min(Number(e.target.value), duration)),
                            ])
                          }
                        />
                      </label>
                      <button
                        className="text-button"
                        onClick={() => {
                          engine?.seek(region[0]);
                          setPosition(region[0]);
                        }}
                      >
                        跳到选区
                      </button>
                    </div>
                  </section>
                  <aside className="notes-panel">
                    <div className="panel-tabs">
                      <button
                        className={panel === 'notes' ? 'selected' : ''}
                        onClick={() => setPanel('notes')}
                      >
                        <MessageSquare size={16} />
                        片段标注
                      </button>
                      <button
                        className={panel === 'vote' ? 'selected' : ''}
                        onClick={() => setPanel('vote')}
                      >
                        <Check size={16} />
                        偏好判断
                      </button>
                    </div>
                    {panel === 'notes' ? (
                      <>
                        <div className="note-editor">
                          <span className="eyebrow">
                            {reply
                              ? `回复 ${replyTarget?.author || '评论'}`
                              : `当前候选 ${sample.tracks?.[selected]?.label || '—'}`}
                          </span>
                          <h3>
                            {time(region[0])} — {time(region[1])}
                          </h3>
                          <select
                            aria-label="问题标签"
                            value={tag}
                            onChange={(e) => setTag(e.target.value)}
                          >
                            {[
                              '听感',
                              '残噪',
                              '压缩感',
                              '音色',
                              '辅音缺失',
                              '乐性噪声',
                              '抽吸感',
                              '断续',
                              '改善',
                            ].map((t) => (
                              <option key={t}>{t}</option>
                            ))}
                          </select>
                          <textarea
                            id="comment"
                            placeholder="这个位置听到了什么？例如：字尾变暗，有短暂断续。"
                            value={comment}
                            onChange={(e) => {
                              setComment(e.target.value);
                              localStorage.setItem(`draft:${user.id}:${sample.id}`, e.target.value);
                              setSaveState('草稿已保存在此浏览器');
                            }}
                          />
                          <button
                            className="primary full"
                            disabled={busy || !comment.trim() || !sample.tracks?.length}
                            onClick={submitComment}
                          >
                            <Plus size={17} />
                            {busy ? '保存中…' : '保存标注'}
                          </button>
                          <small role="status">
                            {saveState ||
                              (sample.blind
                                ? '评测期间只有你能看到自己的标注。'
                                : '保存后，任务成员可以重放此片段。')}
                          </small>
                          {reply && (
                            <button className="text-button" onClick={() => setReply(null)}>
                              取消回复
                            </button>
                          )}
                        </div>
                        <div className="comments-heading">
                          <strong>片段记录 · {sample.comments?.length || 0}</strong>
                          <button className="text-button" onClick={() => void run(refreshSample)}>
                            刷新
                          </button>
                        </div>
                        <div className="comments">
                          {!sample.comments?.length && (
                            <p className="muted">还没有记录。框选一段声音，留下第一条判断。</p>
                          )}
                          <CommentThread
                            comments={sample.comments || []}
                            tracks={sample.tracks || []}
                            onJump={(c) => {
                              const r: [number, number] = [c.start / 16000, c.end / 16000];
                              selectRegion(r);
                              const i = sample.tracks?.findIndex((t) => t.id === c.track_id) ?? -1;
                              if (i >= 0) select(i);
                              engine?.seek(r[0]);
                              setPosition(r[0]);
                            }}
                            onReply={(c) => {
                              setReply(c.id);
                              selectRegion([c.start / 16000, c.end / 16000]);
                              document.querySelector<HTMLTextAreaElement>('#comment')?.focus();
                            }}
                          />
                        </div>
                      </>
                    ) : (
                      <div className="vote-panel">
                        <span className="eyebrow">独立听感判断</span>
                        <h3>你更倾向哪个版本？</h3>
                        <p className="muted">先听完整片段，再对不确定的位置循环比较。</p>
                        {sample.tracks?.map((t) => (
                          <button
                            disabled={!!sample.rating}
                            className={'vote-option ' + (choice === t.id ? 'chosen' : '')}
                            key={t.id}
                            onClick={() => setChoice(t.id)}
                          >
                            <span className="track-letter">{t.label}</span>
                            {sample.blind ? `候选 ${t.label}` : t.name}
                            {choice === t.id && <Check size={17} />}
                          </button>
                        ))}
                        <button
                          disabled={!!sample.rating}
                          className={'vote-option ' + (choice === 'tie' ? 'chosen' : '')}
                          onClick={() => setChoice('tie')}
                        >
                          无明显差异{choice === 'tie' && <Check size={17} />}
                        </button>
                        <textarea
                          aria-label="偏好原因"
                          placeholder="可选：主要原因或取舍"
                          value={reason}
                          disabled={!!sample.rating}
                          onChange={(e) => setReason(e.target.value)}
                        />
                        <button
                          className="primary full"
                          disabled={busy || !choice || !!sample.rating || task.status !== 'active'}
                          onClick={() =>
                            void run(async () => {
                              await api('/samples/' + sample.id + '/rating', 'POST', {
                                choice,
                                reason,
                              });
                              await refreshSample();
                              await refreshTasks();
                              setNotice('判断已保存并锁定，可继续下一个片段。');
                            })
                          }
                        >
                          {sample.rating ? (
                            <>
                              <Check size={17} />
                              已提交
                            </>
                          ) : task.status === 'draft' ? (
                            '发布任务后开放提交'
                          ) : task.status === 'closed' ? (
                            '任务已关闭'
                          ) : (
                            '提交判断'
                          )}
                        </button>
                        <small>提交后锁定；可以在评论中补充。多候选偏好不等同于标准 MUSHRA。</small>
                      </div>
                    )}
                  </aside>
                </>
              )}
            </main>
            <footer className="transport">
              <button
                className="play-button"
                disabled={!loaded}
                aria-label={playing ? '暂停' : '播放'}
                onClick={() => void run(play)}
              >
                {playing ? <Pause size={22} /> : <Play size={22} />}
              </button>
              <button
                className={'icon loop-button ' + (loop ? 'on' : '')}
                title="循环选区（L）"
                disabled={!loaded}
                onClick={() => {
                  engine?.stop();
                  setPlaying(false);
                  setLoop(!loop);
                }}
              >
                <Repeat2 size={21} />
              </button>
              <div className="transport-clock">
                <strong>{time(position)}</strong>
                <span>/ {time(duration)}</span>
              </div>
              <input
                className="seek"
                aria-label="播放位置"
                type="range"
                min={0}
                max={duration || 1}
                step=".01"
                value={position}
                disabled={!loaded}
                onChange={(e) => {
                  engine?.seek(Number(e.target.value));
                  setPosition(Number(e.target.value));
                }}
              />
              <span className="current-label">
                {loaded
                  ? `试听 ${sample?.tracks?.[selected]?.label || '—'}`
                  : sample?.tracks?.length
                    ? '加载音频…'
                    : '等待音频'}
              </span>
              <label className="volume">
                <Headphones size={17} />
                <input
                  aria-label="公共监听音量"
                  type="range"
                  min="0"
                  max="1"
                  step=".01"
                  value={volume}
                  onChange={(e) => {
                    const v = Number(e.target.value);
                    setVolume(v);
                    engine?.setVolume(v);
                  }}
                />
              </label>
              <button className="icon" aria-label="快捷键帮助" onClick={() => setModal('help')}>
                <HelpCircle size={19} />
              </button>
            </footer>
          </>
        )}
      </div>
      {modal && (
        <Modal
          title={
            {
              new: '新建评测任务',
              'edit-task': '编辑评测任务',
              'edit-sample': '编辑音频片段',
              members: '受邀评测者',
              progress: '评测进度',
              batch: '按版本目录批量导入',
              processing: '对齐与响度（草稿）',
              'edit-track': '管理草稿候选',
              sample: '添加音频片段',
              track: '导入候选版本',
              publish: '发布独立评测',
              close: '关闭并揭晓',
              users: '团队成员',
              access: '加入申请与邀请凭证',
              devices: '登录设备',
              security: '账号与安全',
              help: '使用指南',
              system: '当前服务信息',
              report: '评测结果',
              trash: '任务回收站',
              'delete-task': '删除评测任务',
            }[modal] || ''
          }
          onClose={() => {
            if (!batchBusy && !busy) setModal('');
          }}
        >
          {modal === 'batch' && task && (
            <BatchImport
              taskId={task.id}
              existing={task.samples?.map((s) => s.name) || []}
              onBusy={setBatchBusy}
              onDone={async () => {
                await openTask(task.id);
                setTasks(await api('/tasks'));
              }}
            />
          )}
          {modal === 'processing' && sample && (
            <ProcessingModal
              sample={sample}
              onBusy={setBusy}
              onDone={async () => {
                await openTask(task!.id);
                setModal('');
                setNotice('处理已更新；试听已切换到对应资产。');
              }}
            />
          )}
          {modal === 'edit-track' && editTrack && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const data = Object.fromEntries(new FormData(e.currentTarget));
                void run(async () => {
                  await api('/tracks/' + editTrack.id, 'PATCH', data);
                  await refreshSample();
                  setModal('');
                });
              }}
            >
              <p className="muted">
                修改名称和版本证据不会改变音频。有标注的候选不能删除；发布后候选冻结。
              </p>
              <label>
                版本名称
                <input name="name" required maxLength={120} defaultValue={editTrack.name} />
              </label>
              <label>
                版本证据
                <input name="version" maxLength={200} defaultValue={editTrack.version} />
              </label>
              <button className="primary full" disabled={busy}>
                保存修改
              </button>
              <button
                type="button"
                className="full"
                disabled={busy}
                onClick={() => {
                  if (window.confirm('删除这个草稿候选？无法通过界面恢复，有标注的候选会保留。'))
                    void run(async () => {
                      await api('/tracks/' + editTrack.id, 'DELETE');
                      await refreshSample();
                      setModal('');
                    });
                }}
              >
                删除这个候选
              </button>
            </form>
          )}
          {modal === 'delete-task' && task && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const title = new FormData(e.currentTarget).get('title');
                void run(async () => {
                  await api('/tasks/' + task.id, 'DELETE', { title });
                  engine?.stop();
                  setTask(null);
                  setSample(null);
                  setModal('');
                  await refreshTasks();
                  setNotice('任务已移入回收站，可在任务首页恢复。');
                });
              }}
            >
              <p>
                「{task.title}
                」将从任务列表移除，所有成员将无法继续访问或提交。音频、标注和评分保留，恢复后回到删除前的状态。
              </p>
              <label>
                输入完整任务名称确认
                <input name="title" required autoComplete="off" />
              </label>
              <button className="primary full" disabled={busy}>
                确认移入回收站
              </button>
            </form>
          )}
          {modal === 'trash' && (
            <div>
              <p className="muted">
                此处显示你有权管理的已删除任务。恢复保留原有发布状态和成员；管理员可永久清除以释放磁盘空间，清除后不可恢复。
              </p>
              {!trashed.length && <p>回收站为空</p>}
              {user?.role === 'admin' && (
                <div className="cleanup-retry">
                  <button
                    className="text-button"
                    disabled={busy}
                    onClick={() =>
                      void run(async () => {
                        const result = await api('/maintenance/cleanup-retry', 'POST');
                        setNotice(
                          `待清理重试完成：已清理 ${result['已清理'].length} 个文件，保留 ${result['保留'].length} 个，重新被引用 ${result['重新被引用'].length} 个`,
                        );
                      })
                    }
                  >
                    重试清理待删文件
                  </button>
                </div>
              )}
              {trashed.map((t) => (
                <div className="trash-row" key={t.id}>
                  <div>
                    <strong>{t.title}</strong>
                    <p className="muted">
                      {stateName[t.status]} · {t.sample_count} 个片段
                    </p>
                  </div>
                  <div className="trash-actions">
                    <button
                      disabled={busy}
                      onClick={() =>
                        void run(async () => {
                          await api('/tasks/' + t.id + '/restore', 'POST');
                          setTrashed(await api('/tasks?deleted=true'));
                          await refreshTasks();
                        })
                      }
                    >
                      恢复任务
                    </button>
                    {user?.role === 'admin' &&
                      (purgePrep?.task_id === t.id ? (
                        <div className="purge-confirm">
                          <p>
                            将永久删除 {purgePrep.summary['片段数']} 个片段、
                            {purgePrep.summary['候选数']} 个候选，预计释放约{' '}
                            {(purgePrep.summary['预计释放字节'] / 1048576).toFixed(1)} MB。
                            <strong>此操作不可恢复。</strong>
                          </p>
                          <button
                            className="danger"
                            disabled={busy}
                            onClick={() =>
                              void run(async () => {
                                const result = await api('/tasks/' + t.id + '/purge', 'POST', {
                                  确认令牌: purgePrep.token,
                                });
                                setPurgePrep(null);
                                setTrashed(await api('/tasks?deleted=true'));
                                await refreshTasks();
                                setNotice(
                                  result['回收失败']?.length
                                    ? `已清除任务；${result['回收失败'].length} 个文件未能回收，可稍后重试清理`
                                    : '任务已永久清除，独占音频空间已释放',
                                );
                              })
                            }
                          >
                            确认永久清除
                          </button>
                        </div>
                      ) : (
                        <button
                          className="danger-text"
                          disabled={busy}
                          onClick={() =>
                            void run(async () => {
                              const summary = await api(
                                '/tasks/' + t.id + '/purge/prepare',
                                'POST',
                              );
                              setPurgePrep({
                                task_id: t.id,
                                token: summary['确认令牌'],
                                summary,
                              });
                            })
                          }
                        >
                          永久清除
                        </button>
                      ))}
                  </div>
                </div>
              ))}
            </div>
          )}
          {modal === 'new' && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const data = Object.fromEntries(new FormData(e.currentTarget));
                void run(async () => {
                  const t = await api('/tasks', 'POST', data);
                  setModal('');
                  await refreshTasks();
                  await openTask(t.id);
                });
              }}
            >
              <label>
                任务名称
                <input
                  name="title"
                  required
                  placeholder="例如：AIBF 基线与新版 · 第一轮"
                  maxLength={150}
                />
              </label>
              <label>
                比较场景
                <select name="kind">
                  {['算法版本', 'VPU 支路', '级联链路', '竞品算法', '竞品整机'].map((k) => (
                    <option key={k}>{k}</option>
                  ))}
                </select>
              </label>
              <label>
                工作模式
                <select name="mode">
                  <option value="development">开发诊断：显示版本、波形与频谱</option>
                  <option value="blind">独立评测：隐藏版本，关闭后统一揭晓</option>
                </select>
              </label>
              <p className="muted">候选可来自自研或竞品。不同设备采集请选“竞品整机”。</p>
              <button className="primary full" disabled={busy}>
                创建任务
              </button>
            </form>
          )}
          {modal === 'sample' && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const data = Object.fromEntries(new FormData(e.currentTarget));
                void run(async () => {
                  const s = await api('/tasks/' + task!.id + '/samples', 'POST', data);
                  setTask(await api('/tasks/' + task!.id));
                  await openSample(s.id);
                  setModal('track');
                });
              }}
            >
              <label>
                片段名称
                <input name="name" required placeholder="录音编号 · 片段位置" />
              </label>
              <label>
                场景描述
                <input name="scene" placeholder="说话人、噪声、佩戴、音量等" />
              </label>
              <label>
                数据来源
                <select name="provenance">
                  <option value="PRIVATE local verification">受限 · 本地验证</option>
                  <option value="DECLASSIFIED real-device">已脱密真机</option>
                  <option value="PUBLIC reproducible">公开或合成数据</option>
                </select>
              </label>
              <button className="primary full" disabled={busy}>
                下一步：添加版本
                <ArrowRight size={17} />
              </button>
            </form>
          )}
          {modal === 'edit-task' && task && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const data = Object.fromEntries(new FormData(e.currentTarget));
                void run(async () => {
                  await api('/tasks/' + task.id, 'PATCH', data);
                  await openTask(task.id);
                  await refreshTasks();
                  setModal('');
                  setNotice('任务信息已更新。');
                });
              }}
            >
              <p className="muted">仅准备中的任务可修改；发布后名称、比较类型和模式冻结。</p>
              <label>
                任务名称
                <input name="title" required maxLength={150} defaultValue={task.title} />
              </label>
              <label>
                比较场景
                <select name="kind" defaultValue={task.kind}>
                  {['算法版本', 'VPU 支路', '级联链路', '竞品算法', '竞品整机'].map((kind) => (
                    <option key={kind}>{kind}</option>
                  ))}
                </select>
              </label>
              <label>
                工作模式
                <select name="mode" defaultValue={task.mode}>
                  <option value="development">开发诊断：显示版本、波形与频谱</option>
                  <option value="blind">独立评测：隐藏版本，关闭后统一揭晓</option>
                </select>
              </label>
              <button className="primary full" disabled={busy}>
                保存任务信息
              </button>
            </form>
          )}
          {modal === 'edit-sample' && sample && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const data = Object.fromEntries(new FormData(e.currentTarget));
                void run(async () => {
                  await api('/samples/' + sample.id, 'PATCH', data);
                  await refreshSample();
                  setModal('');
                  setNotice('片段信息已更新。');
                });
              }}
            >
              <p className="muted">修改名称、场景和来源标记不会改变音频；发布后这些信息冻结。</p>
              <label>
                片段名称
                <input name="name" required maxLength={100} defaultValue={sample.name} />
              </label>
              <label>
                场景
                <input name="scene" maxLength={200} defaultValue={sample.scene} />
              </label>
              <label>
                数据来源
                <select name="provenance" defaultValue={sample.provenance}>
                  <option>PRIVATE local verification</option>
                  <option>DECLASSIFIED real-device</option>
                  <option>PUBLIC reproducible</option>
                </select>
              </label>
              <button className="primary full" disabled={busy}>
                保存片段信息
              </button>
            </form>
          )}
          {modal === 'members' && task && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const form = new FormData(e.currentTarget);
                void run(async () => {
                  await api('/tasks/' + task.id + '/members', 'PATCH', {
                    users: form.getAll('users'),
                  });
                  await openTask(task.id);
                  setModal('');
                  setNotice('受邀评测者已更新；在线同事将在约 5 秒内看到变化。');
                });
              }}
            >
              <p>
                勾选的同事即受邀完成评测，只有受邀评测者的评分计入结果；负责人自动拥有访问权限，是否参与由勾选决定。只可移除尚未提交评论或偏好判断的人；关闭任务后名单冻结。
              </p>
              {members.map((member) => (
                <label className="checkbox" key={member.id}>
                  <input
                    type="checkbox"
                    name="users"
                    value={member.id}
                    defaultChecked={task.review_assignments?.includes(member.id)}
                  />
                  {member.name}
                  {member.id === task.owner ? '（任务创建者，自动拥有访问权限）' : ''}
                </label>
              ))}
              <button className="primary full" disabled={busy}>
                保存受邀评测者
              </button>
            </form>
          )}
          {modal === 'progress' && progress && (
            <div className="task-progress">
              <p className="muted">
                收集期间可随时确认受邀同事的完成情况；此处只显示完成数量与状态，不含任何偏好内容。
              </p>
              <div className="progress-numbers">
                <span>
                  受邀评测者<strong>{progress.受邀评测者}</strong>
                </span>
                <span>
                  已完成<strong>{progress.已完成}</strong>
                </span>
                <span>
                  进行中<strong>{progress.进行中}</strong>
                </span>
                <span>
                  未开始<strong>{progress.未开始}</strong>
                </span>
              </div>
              <div className="progress-rows">
                {progress.成员.map((m) => (
                  <div key={m.ID}>
                    <strong>{m.名称}</strong>
                    <span>
                      {m.已提交片段数}/{task?.samples?.length || 0} 片段 · {m.状态}
                    </span>
                  </div>
                ))}
                {!progress.成员.length && <p className="muted">还没有受邀评测者。</p>}
              </div>
              <small>
                已完成＝已提交全部片段；进行中＝已提交部分片段；未开始＝尚未提交。评论不算提交。
              </small>
            </div>
          )}
          {modal === 'track' && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const form = e.currentTarget;
                const data = new FormData(form);
                void run(async () => {
                  await api('/samples/' + sample!.id + '/tracks', 'POST', data);
                  await refreshSample();
                  form.reset();
                  setNotice('候选已导入，原始相对电平未改变。');
                  setModal('');
                });
              }}
            >
              <p className="muted">
                为「{sample?.name}」添加候选。须为同一时间段，16 kHz，最长 120
                秒；不自动对齐或归一化。
              </p>
              <label>
                版本名称
                <input name="name" required placeholder="例如：AIBF 版本 02" />
              </label>
              <label>
                版本证据
                <input name="version" placeholder="commit / 模型 SHA / 未知" defaultValue="未知" />
              </label>
              <label>
                读取通道
                <select name="channel">
                  <option value="0">单通道 / 4 通道 FB（索引 0）</option>
                  <option value="1">4 通道 FF（索引 1）</option>
                  <option value="2">4 通道 TT（索引 2）</option>
                  <option value="3">4 通道 VPU（索引 3）</option>
                </select>
              </label>
              <label className="upload-zone">
                <Upload size={27} />
                <strong>选择 WAV 文件</strong>
                <input name="file" type="file" accept=".wav" required />
              </label>
              <button className="primary full" disabled={busy}>
                {busy ? '正在体检并导入…' : '体检并导入'}
              </button>
            </form>
          )}
          {modal === 'publish' && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const f = new FormData(e.currentTarget);
                void run(async () => {
                  await api('/tasks/' + task!.id + '/publish', 'POST', {
                    users: f.getAll('users'),
                    alignment_confirmed: f.has('alignment'),
                  });
                  setModal('');
                  await openTask(task!.id);
                  await refreshTasks();
                });
              }}
            >
              <p>发布后锁定输入文件、候选版本、比较模式与播放处理。关闭任务后统一揭晓。</p>
              {publishSummary && (
                <div className="publish-processing">
                  <h3>播放处理确认</h3>
                  {publishSummary['片段'].map((ps: any) => (
                    <div key={ps.片段ID}>
                      <strong>
                        {ps.片段}
                        {ps.混合处理 && <span className="proc-badge warn">存在混合处理口径</span>}
                      </strong>
                      <div className="publish-processing-lines">
                        {ps.候选.map((e: any) => (
                          <span key={e.track_id}>
                            {e.名称}：{e.模式}
                          </span>
                        ))}
                      </div>
                      {ps.候选.some((e: any) => e.拒绝.length > 0) && (
                        <div className="publish-processing-rejected">
                          {ps.候选.flatMap((e: any) =>
                            e.拒绝.map((r: any) => (
                              <span key={e.track_id + r.判据}>
                                {r.候选} {r.判据}建议被拒绝（{r.拒绝码}）
                              </span>
                            )),
                          )}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
              <h3>分配给同事</h3>
              {members
                .filter((m) => m.id !== user.id)
                .map((m) => (
                  <label className="checkbox" key={m.id}>
                    <input type="checkbox" name="users" value={m.id} />
                    {m.name}
                  </label>
                ))}
              <label className="checkbox">
                <input type="checkbox" name="users" value={user?.id} />
                我也参与评测（提交偏好并计入进度）
              </label>
              {members.length <= 1 && (
                <p className="muted">目前只有你；可先到“团队成员”创建账号。</p>
              )}
              <label className="checkbox confirm">
                <input name="alignment" type="checkbox" required />
                我已核对每个片段来自相同输入与时间范围，确认延迟和增益处理一致。相同长度不代表已同步。
              </label>
              <button className="primary full" disabled={busy}>
                发布并锁定任务
              </button>
            </form>
          )}
          {modal === 'close' && (
            <>
              <p>
                关闭后停止接受判断，并向任务成员公开版本身份、评论和结果。此操作不能撤回；未提交者不会被补记为“无差异”。
              </p>
              <button
                className="primary full"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    await api('/tasks/' + task!.id + '/close', 'POST');
                    setModal('');
                    await openTask(task!.id);
                    await refreshTasks();
                  })
                }
              >
                关闭收集并统一揭晓
              </button>
            </>
          )}
          {modal === 'users' && (
            <TeamMembers
              members={members}
              onChange={setMembers}
              onBusy={setBusy}
              onDevices={(m) => {
                setDeviceTarget(m);
                setModal('devices');
              }}
            />
          )}
          {modal === 'access' && <AccessAdmin onBusy={setBusy} />}
          {modal === 'devices' && deviceTarget && <DeviceAdmin target={deviceTarget} />}
          {modal === 'security' && <SecuritySettings authKind={authKind} onBusy={setBusy} />}
          {modal === 'system' && serverInfo && (
            <div className="server-info">
              <p>
                管理员与同事应核对同一服务地址和数据编号。程序重启会改变实例编号；版本不同或数据编号不同，说明当前页面连接的服务需要核实。
              </p>
              <dl>
                <dt>访问地址</dt>
                <dd>{window.location.origin}</dd>
                <dt>服务版本</dt>
                <dd>{serverInfo.version}</dd>
                <dt>页面版本</dt>
                <dd>{packageInfo.version}</dd>
                <dt>运行实例</dt>
                <dd>{serverInfo.instance_id}</dd>
                <dt>数据编号</dt>
                <dd>{serverInfo.data_id}</dd>
                {serverInfo.data_directory && (
                  <>
                    <dt>数据目录（仅管理员可见）</dt>
                    <dd>{serverInfo.data_directory}</dd>
                  </>
                )}
              </dl>
              <p className="muted">
                更换 exe
                不会关闭旧服务。请先退出旧程序，再用同一数据目录启动新版；只关闭浏览器不会停止服务。
              </p>
            </div>
          )}
          {modal === 'help' && <UserGuide role={user.role} />}
          {modal === 'report' && report && (
            <div className="report">
              {task?.can_manage && (
                <div className="report-exports">
                  <a className="button" href={'/api/tasks/' + task?.id + '/export'}>
                    <Download size={16} />
                    证据 JSON
                  </a>
                  <a className="button" href={'/api/tasks/' + task?.id + '/export.csv'}>
                    <Download size={16} />
                    结果 CSV（Excel）
                  </a>
                  <a className="button" href={'/api/tasks/' + task?.id + '/export.md'}>
                    <Download size={16} />
                    复盘 Markdown
                  </a>
                </div>
              )}
              <section className="review-progress">
                <h3>参与进度</h3>
                <div className="progress-numbers">
                  <span>
                    受邀评测者<strong>{report['参与进度'].受邀评测者}</strong>
                  </span>
                  <span>
                    已完成<strong>{report['参与进度'].已完成}</strong>
                  </span>
                  <span>
                    进行中<strong>{report['参与进度'].进行中}</strong>
                  </span>
                  <span>
                    未开始<strong>{report['参与进度'].未开始}</strong>
                  </span>
                </div>
                <p>已完成：{report['参与进度'].已完成名单.join('、') || '—'}</p>
                <p>进行中：{report['参与进度'].进行中名单.join('、') || '—'}</p>
                <p>未开始：{report['参与进度'].未开始名单.join('、') || '—'}</p>
                <small>
                  受邀名单来自评测分配；负责人自动拥有访问权限，但不自动受邀。已完成＝已提交全部片段；进行中＝部分片段；未开始＝尚未提交；评论不算提交。
                </small>
              </section>
              <section>
                <h3>逐片段偏好</h3>
                <small>百分比以该片段的有效提交人数为分母；缺失判断不计入。</small>
                {report['样本'].map((s) => (
                  <div className="review-sample" key={s.id}>
                    <h4>
                      {s.name}
                      {s.分歧 && <span className="diff-badge">存在分歧</span>}
                    </h4>
                    <small>
                      {s.scene} · {s.分母} 人已提交（分母）
                      {s.分歧 && ` · 出现 ${distinctChoices(s.票数)} 种不同偏好，仅为描述性提示`}
                    </small>
                    {s.票数.map((v) => (
                      <div className="result-row" key={v.ID}>
                        <span>{v.名称}</span>
                        <div>
                          <i style={{ width: shareWidth(v.票数, s.分母) }} />
                        </div>
                        <strong>{v.票数}</strong>
                        <em>{formatShare(v.票数, s.分母)}</em>
                      </div>
                    ))}
                    <button className="text-button" onClick={() => jumpToSample(s.id)}>
                      回到此片段试听与标注
                    </button>
                  </div>
                ))}
              </section>
              <section>
                <h3>问题标签汇总</h3>
                <small>按标签统计根评论数量；回复楼层不计入。点击片段名可定位到对应片段。</small>
                {!report['标签汇总'].length && <p className="muted">还没有根评论标签。</p>}
                {report['标签汇总'].map((t) => (
                  <div className="tag-summary" key={t.标签}>
                    <span className="comment-tag">{t.标签}</span>
                    <strong>{t.根评论数} 条</strong>
                    <div className="tag-locations">
                      {t.片段.map((loc) => (
                        <button
                          className="text-button"
                          key={loc.ID}
                          onClick={() => jumpToSample(loc.ID)}
                        >
                          {loc.名称}
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
              </section>
              <p className="review-note">
                {report['播放口径']}。{report['解释边界']}
                。分歧只表示同一片段出现了不同偏好，不代表统计显著或算法优劣；三种导出（JSON／CSV／Markdown）均包含上述口径与稳定
                ID。
              </p>
            </div>
          )}
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
        </Modal>
      )}
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<App />);
