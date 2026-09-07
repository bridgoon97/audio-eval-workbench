import { useState } from 'react';
import { Check, X } from 'lucide-react';
import { api, type Track } from './types';

type Verdict = {
  可应用?: boolean;
  拒绝码?: string | null;
  原因?: string;
  lag?: number;
  相关峰?: number;
  峰旁瓣比?: number;
  窗口lag?: number[];
  极差?: number;
  窗口相关峰?: number[];
  建议增益db?: number;
  活动门限?: number;
  覆盖?: number;
  共同活动秒?: number;
  预测峰值?: number;
};
type Entry = { track_id: string; 名称: string; 角色: string; 延迟: Verdict; 响度: Verdict };
type Selection = Record<string, { 对齐: boolean; 响度: boolean }>;

const ms = (lag: number) => `(+${(lag / 16).toFixed(1)} ms)`;

export function ProcessingModal({
  sample,
  onBusy,
  onDone,
}: {
  sample: { id: string; tracks?: Track[] };
  onBusy: (busy: boolean) => void;
  onDone: () => void;
}) {
  const tracks = sample.tracks || [];
  const [reference, setReference] = useState(tracks[0]?.id || '');
  const [entries, setEntries] = useState<Entry[] | null>(null);
  const [selection, setSelection] = useState<Selection>({});
  const [error, setError] = useState('');
  const [busy, setLocalBusy] = useState(false);

  const run = async (action: () => Promise<void>) => {
    setError('');
    setLocalBusy(true);
    onBusy(true);
    try {
      await action();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLocalBusy(false);
      onBusy(false);
    }
  };

  const analyze = () =>
    void run(async () => {
      const data = await api<{ 候选: Entry[] }>(
        `/samples/${sample.id}/alignment?reference=${reference}`,
      );
      setEntries(data['候选']);
      const next: Selection = {};
      for (const e of data['候选']) {
        if (e.角色 === '参考') continue;
        next[e.track_id] = {
          对齐: !!e.延迟.可应用,
          响度: !!e.延迟.可应用 && !!e.响度.可应用,
        };
      }
      setSelection(next);
    });

  const apply = () =>
    void run(async () => {
      const 处理 = Object.entries(selection)
        .filter(([, v]) => v.对齐 || v.响度)
        .map(([track_id, v]) => ({ 候选: track_id, 对齐: v.对齐, 响度: v.响度 }));
      await api(`/samples/${sample.id}/processing`, 'POST', { 参考: reference, 处理 });
      onDone();
    });

  const restore = () =>
    void run(async () => {
      await api(`/samples/${sample.id}/restore-processing`, 'POST', {});
      onDone();
    });

  const hasSelection = Object.values(selection).some((v) => v.对齐 || v.响度);
  const hasProcessing = tracks.some((t) => t.处理);

  return (
    <div className="processing">
      <p className="muted">
        选择参考候选后分析：恒定整数采样延迟（±1600 samples，正数＝候选晚到，应用时前移）与活动段
        RMS 匹配（不是 LUFS/ITU BS.1770 响度校准）。原始 WAV 不会被修改，处理生成独立派生试听资产。
      </p>
      <label>
        参考候选（保持原始，0 samples / 0 dB）
        <select value={reference} onChange={(e) => setReference(e.target.value)}>
          {tracks.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
      </label>
      <button className="full" disabled={busy || !reference} onClick={analyze}>
        {busy && !entries ? '分析中…' : '开始分析'}
      </button>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {entries && (
        <div className="proc-results">
          {entries.map((e) => {
            if (e.角色 === '参考')
              return (
                <div className="proc-row ref" key={e.track_id}>
                  <strong>{e.名称}</strong>
                  <span>参考候选 · 0 samples / 0 dB · 保持原资产</span>
                </div>
              );
            const sel = selection[e.track_id] || { 对齐: false, 响度: false };
            const d = e.延迟;
            const g = e.响度;
            return (
              <div className="proc-row" key={e.track_id}>
                <strong>{e.名称}</strong>
                <dl>
                  <dt>延迟</dt>
                  <dd>
                    {d.可应用 ? (
                      <>
                        {d.lag} samples {ms(d.lag || 0)} · 相关峰 {d.相关峰} · 旁瓣比 {d.峰旁瓣比} ·
                        窗口 lag {d.窗口lag?.join('/')}（极差 {d.极差}）
                      </>
                    ) : (
                      <span className="rejected">
                        拒绝（{d.拒绝码}）：{d.原因}
                      </span>
                    )}
                  </dd>
                  <dt>活动段 RMS</dt>
                  <dd>
                    {g.可应用 ? (
                      <>
                        建议 {g.建议增益db} dB · 预测峰值 {g.预测峰值} · 覆盖 {g.覆盖} · 共同活动{' '}
                        {g.共同活动秒} s
                      </>
                    ) : (
                      <span className="rejected">
                        拒绝（{g.拒绝码}）：{g.原因}
                      </span>
                    )}
                  </dd>
                </dl>
                <div className="proc-options">
                  <label>
                    <input
                      type="checkbox"
                      checked={sel.对齐}
                      disabled={busy || !d.可应用}
                      onChange={(ev) =>
                        setSelection({
                          ...selection,
                          [e.track_id]: { ...sel, 对齐: ev.target.checked },
                        })
                      }
                    />
                    应用整数采样对齐
                  </label>
                  <label>
                    <input
                      type="checkbox"
                      checked={sel.响度}
                      disabled={busy || !g.可应用}
                      onChange={(ev) =>
                        setSelection({
                          ...selection,
                          [e.track_id]: { ...sel, 响度: ev.target.checked },
                        })
                      }
                    />
                    应用活动段 RMS 匹配
                  </label>
                </div>
              </div>
            );
          })}
        </div>
      )}
      {entries && hasSelection && (
        <div className="proc-summary">
          <strong>将应用：</strong>
          <ul>
            {Object.entries(selection)
              .filter(([, v]) => v.对齐 || v.响度)
              .map(([id, v]) => (
                <li key={id}>
                  {tracks.find((t) => t.id === id)?.name}：
                  {[v.对齐 ? '整数采样对齐（先）' : '', v.响度 ? '活动段 RMS 固定增益（后）' : '']
                    .filter(Boolean)
                    .join(' → ')}
                </li>
              ))}
          </ul>
          <button className="primary full" disabled={busy} onClick={apply}>
            <Check size={16} />
            确认应用所选处理
          </button>
        </div>
      )}
      {hasProcessing && (
        <button className="full" disabled={busy} onClick={restore}>
          <X size={16} />
          恢复原始处理
        </button>
      )}
    </div>
  );
}
