import { useEffect, useState } from 'react';
import { api, type Sample, type MosScores } from './types';

export function MosPanel({
  sample,
  userId,
  active,
  onSaved,
  onListen,
  notify,
}: {
  sample: Sample;
  userId: string;
  active: boolean;
  onSaved: () => Promise<void>;
  onListen: (index: number) => void;
  notify: (message: string) => void;
}) {
  const storageKey = `mos-draft:${userId}:${sample.id}`;
  const [scores, setScores] = useState<MosScores>({});
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    let draft = {};
    try {
      draft = JSON.parse(localStorage.getItem(storageKey) || '{}');
    } catch {
      /* 损坏草稿回到未评分状态。 */
    }
    setScores(sample.rating?.scores || draft);
  }, [storageKey, sample.rating]);
  const candidates = sample.tracks?.filter((t) => !t.is_reference) || [];
  const dimensions = sample.dimensions || [];
  const complete =
    candidates.length > 0 &&
    dimensions.length > 0 &&
    candidates.every((t) =>
      dimensions.every(
        (d) =>
          Number.isInteger(scores[t.id]?.[d.key]) &&
          scores[t.id][d.key] >= 1 &&
          scores[t.id][d.key] <= 5,
      ),
    );
  const count = candidates.reduce(
    (n, t) => n + dimensions.filter((d) => scores[t.id]?.[d.key]).length,
    0,
  );
  return (
    <div className="mos-panel">
      <span className="eyebrow">多维 MOS · 1–5 分</span>
      <h3>逐候选、逐维度评分</h3>
      <p className="muted">可随时回听参考，按本任务各指标的说明逐候选打分；不预设分数。</p>
      {sample.tracks?.some((t) => t.is_reference) ? (
        <button
          className="mos-reference"
          onClick={() => onListen(sample.tracks!.findIndex((t) => t.is_reference))}
        >
          切换试听参考音色
        </button>
      ) : (
        <p className="error">请组织者在草稿时间轴下方指定参考音色。</p>
      )}
      {candidates.map((t) => (
        <fieldset className="mos-card" key={t.id} disabled={!!sample.rating || saving}>
          <legend>{sample.blind ? `候选 ${t.label}` : t.name}</legend>
          <button
            type="button"
            onClick={() => onListen(sample.tracks!.findIndex((tr) => tr.id === t.id))}
          >
            切换试听 {t.label}
          </button>
          {dimensions.map((d) => (
            <div className="mos-dimension" key={d.key}>
              <strong>{d.name}</strong>
              <small>{d.question}</small>
              <div className="mos-options" role="radiogroup" aria-label={`${t.label} ${d.name}`}>
                {d.anchors.map((anchor, i) => (
                  <label className={scores[t.id]?.[d.key] === i + 1 ? 'chosen' : ''} key={anchor}>
                    <input
                      type="radio"
                      name={`${sample.id}-${t.id}-${d.key}`}
                      value={i + 1}
                      checked={scores[t.id]?.[d.key] === i + 1}
                      onChange={() => {
                        const next = { ...scores, [t.id]: { ...scores[t.id], [d.key]: i + 1 } };
                        setScores(next);
                        localStorage.setItem(storageKey, JSON.stringify(next));
                      }}
                    />
                    <b>{i + 1}</b>
                    <span>{anchor}</span>
                  </label>
                ))}
              </div>
            </div>
          ))}
        </fieldset>
      ))}
      <p className="muted">
        已填 {count}/{candidates.length * dimensions.length} 项 · 分数草稿保存在当前浏览器。
      </p>
      <button
        className="primary full"
        disabled={saving || !active || !complete || !!sample.rating}
        onClick={async () => {
          setSaving(true);
          try {
            await api(`/samples/${sample.id}/rating`, 'POST', { choice: 'mos', scores });
            localStorage.removeItem(storageKey);
            await onSaved();
            notify('评分已完整提交并锁定。');
          } catch (error) {
            notify(error instanceof Error ? error.message : '提交失败，请重试');
          } finally {
            setSaving(false);
          }
        }}
      >
        {sample.rating
          ? '已提交'
          : !active
            ? '发布后开放评分'
            : saving
              ? '正在提交…'
              : '提交全部评分'}
      </button>
      <small>
        所有候选的全部指标完整填写后一次提交；提交后锁定。各指标分别汇总，不自动合成总分。
      </small>
    </div>
  );
}
