import { useRef, useState } from 'react';
import { api } from './types';
import { matchFiles } from './batch';

type Version = { name: string; version: string; channel: number; files: File[] };
const blank = (i: number): Version => ({
  name: `版本 ${i}`,
  version: '未知',
  channel: 0,
  files: [],
});
export function BatchImport({
  taskId,
  existing,
  onDone,
  onBusy,
}: {
  taskId: string;
  existing: string[];
  onDone: () => Promise<void>;
  onBusy: (busy: boolean) => void;
}) {
  const [versions, setVersions] = useState<Version[]>([blank(1), blank(2)]);
  const [provenance, setProvenance] = useState('PRIVATE local verification');
  const [scene, setScene] = useState('');
  const [statuses, setStatuses] = useState<Record<string, string>>({});
  const [started, setStarted] = useState(false);
  const [running, setRunning] = useState(false);
  const [refreshError, setRefreshError] = useState('');
  const stop = useRef(false);
  const ids = useRef<Record<string, string>>({});
  const rows = matchFiles(versions.map((v) => v.files));
  const ready = rows.filter((r) => !r.issues.length && !existing.includes(r.key));
  function update(i: number, patch: Partial<Version>) {
    setVersions(versions.map((v, n) => (n === i ? { ...v, ...patch } : v)));
  }
  async function submit() {
    setStarted(true);
    setRunning(true);
    onBusy(true);
    stop.current = false;
    try {
      for (const row of ready) {
        if (stop.current) break;
        if (statuses[row.key] === '已导入') continue;
        ids.current[row.key] ||= Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) =>
          b.toString(16).padStart(2, '0'),
        ).join('');
        setStatuses((s) => ({ ...s, [row.key]: '正在体检并上传…' }));
        const form = new FormData();
        form.append(
          'manifest',
          JSON.stringify({
            request_id: ids.current[row.key],
            name: row.key,
            scene,
            provenance,
            tracks: versions.map(({ name, version, channel }) => ({ name, version, channel })),
          }),
        );
        row.files.forEach((file) => form.append('files', file));
        try {
          await api(`/tasks/${taskId}/import-sample`, 'POST', form);
          setStatuses((s) => ({ ...s, [row.key]: '已导入' }));
        } catch (err) {
          setStatuses((s) => ({ ...s, [row.key]: (err as Error).message }));
          // 网络中断或格式错误后暂停，由用户决定重试。
          break;
        }
      }
    } finally {
      setRunning(false);
      onBusy(false);
      try {
        await onDone();
      } catch (err) {
        setRefreshError((err as Error).message);
      }
    }
  }
  return (
    <div className="batch-import">
      {refreshError && (
        <p role="alert">列表刷新失败：{refreshError}。已上传内容仍保留，请关闭窗口后刷新页面。</p>
      )}
      <p className="muted">
        每个版本选择一个目录，例如 AIBF-v1/ 和 AIBF-v2/。目录内部须有相同的相对路径（如
        地铁/001.wav）。仅导入匹配完整的 WAV；不自动对齐或调整增益。
      </p>
      <fieldset disabled={started}>
        <div className="batch-versions">
          {versions.map((v, i) => (
            <section key={i} className="batch-version">
              <label>
                版本 {i + 1} 名称
                <input
                  aria-label={`版本 ${i + 1} 名称`}
                  maxLength={120}
                  value={v.name}
                  onChange={(e) => update(i, { name: e.target.value })}
                />
              </label>
              <label>
                版本证据
                <input
                  maxLength={200}
                  value={v.version}
                  onChange={(e) => update(i, { version: e.target.value })}
                />
              </label>
              <label>
                读取通道
                <select
                  value={v.channel}
                  onChange={(e) => update(i, { channel: Number(e.target.value) })}
                >
                  <option value={0}>单通道 / FB</option>
                  <option value={1}>FF</option>
                  <option value={2}>TT</option>
                  <option value={3}>VPU</option>
                </select>
              </label>
              <label className="upload-zone">
                选择版本目录 · {v.files.filter((f) => /\.wav$/i.test(f.name)).length} 个 WAV
                <input
                  aria-label={`版本 ${i + 1} 目录`}
                  type="file"
                  multiple
                  {...{ webkitdirectory: '' }}
                  onChange={(e) => update(i, { files: Array.from(e.target.files || []) })}
                />
              </label>
            </section>
          ))}
        </div>
        <div className="actions">
          <button
            disabled={versions.length >= 6}
            onClick={() => setVersions([...versions, blank(versions.length + 1)])}
          >
            增加版本
          </button>
          <button
            disabled={versions.length <= 2}
            onClick={() => setVersions(versions.slice(0, -1))}
          >
            移除最后一个版本
          </button>
        </div>
        <label>
          场景
          <input
            maxLength={200}
            value={scene}
            onChange={(e) => setScene(e.target.value)}
            placeholder="例如：车内近端说话"
          />
        </label>
        <label>
          数据来源
          <select value={provenance} onChange={(e) => setProvenance(e.target.value)}>
            <option>PRIVATE local verification</option>
            <option>DECLASSIFIED real-device</option>
            <option>PUBLIC reproducible</option>
          </select>
        </label>
      </fieldset>
      <h3>匹配预览 · {ready.length} 个可导入片段</h3>
      <p className="muted">
        路径区分大小写；缺失、重名、超限或任务内已有的片段会跳过。上传后服务器检查 16
        kHz、通道和采样长度。每个片段全部成功才入库。
      </p>
      <div className="batch-preview">
        <table>
          <thead>
            <tr>
              <th>相对路径 / 片段名称</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key}>
                <td>{row.key}</td>
                <td>
                  {statuses[row.key] ||
                    row.issues.join('；') ||
                    (existing.includes(row.key) ? '任务中已存在，跳过' : '匹配完整')}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!rows.length && <p className="muted">选择两个版本的目录后，将在此显示匹配结果。</p>}
      </div>
      <p className="muted">
        暂停会等待当前片段完成。已完成的片段保留；本窗口内重试不会重复创建。刷新或关闭后，可重新选目录并跳过已有片段。
      </p>
      <div className="actions">
        <button
          className="primary"
          disabled={running || !ready.length || versions.some((v) => !v.name.trim())}
          onClick={() => void submit()}
        >
          {started ? '继续 / 重试未完成片段' : '确认匹配并导入'}
        </button>
        {running && (
          <button
            onClick={() => {
              stop.current = true;
            }}
          >
            当前片段完成后暂停
          </button>
        )}
      </div>
    </div>
  );
}
