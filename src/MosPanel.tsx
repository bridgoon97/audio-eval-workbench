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
  const step = sample.score_step ?? 1;
  const validScore = (value: number | undefined) =>
    value !== undefined &&
    Number.isFinite(value) &&
    value >= 1 &&
    value <= 5 &&
    Math.abs((value - 1) / step - Math.round((value - 1) / step)) < 1e-8;
  const saveScore = (track: string, key: string, value: number | undefined) => {
    if (!active || sample.rating || saving) return;
    const trackScores = { ...scores[track] };
    if (value === undefined) delete trackScores[key];
    else trackScores[key] = value;
    const next = { ...scores, [track]: trackScores };
    setScores(next);
    localStorage.setItem(storageKey, JSON.stringify(next));
  };
  const complete =
    candidates.length > 0 &&
    dimensions.length > 0 &&
    candidates.every((t) => dimensions.every((d) => validScore(scores[t.id]?.[d.key])));
  const count = candidates.reduce(
    (n, t) => n + dimensions.filter((d) => validScore(scores[t.id]?.[d.key])).length,
    0,
  );
  return (
    <div className="mos-panel">
      <span className="eyebrow">多维 MOS · 1–5 分 · 步进 {step} 分</span>
      <h3>逐候选、逐维度评分</h3>
      <p className="muted">
        按本任务各指标的说明逐候选打分；如有参考可随时回听。滑块初始位置不计分，请逐项操作确认。
      </p>
      {sample.tracks?.some((t) => t.is_reference) ? (
        <button
          className="mos-reference"
          onClick={() => onListen(sample.tracks!.findIndex((t) => t.is_reference))}
        >
          切换试听参考音色
        </button>
      ) : (
        <p className="muted">本片段未设置参考，全部序列参与评分。</p>
      )}
      {candidates.map((t) => (
        <fieldset className="mos-card" key={t.id} disabled={!active || !!sample.rating || saving}>
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
              <div className="mos-slider">
                <div className="mos-score-controls">
                  <label className="mos-range-label">
                    当前分数：{scores[t.id]?.[d.key]?.toFixed(1) ?? '未评分'}
                    <input
                      type="range"
                      aria-label={`${t.label} ${d.name}`}
                      min="1"
                      max="5"
                      step={step}
                      value={validScore(scores[t.id]?.[d.key]) ? scores[t.id][d.key] : 3}
                      onChange={(e) => saveScore(t.id, d.key, Number(e.target.value))}
                      onPointerUp={(e) => saveScore(t.id, d.key, Number(e.currentTarget.value))}
                      onKeyUp={(e) => {
                        if (
                          [
                            'ArrowLeft',
                            'ArrowRight',
                            'ArrowUp',
                            'ArrowDown',
                            'Home',
                            'End',
                          ].includes(e.key)
                        )
                          saveScore(t.id, d.key, Number(e.currentTarget.value));
                      }}
                    />
                  </label>
                  <label className="mos-number-label">
                    输入分数
                    <input
                      type="number"
                      aria-label={`${t.label} ${d.name} 输入分数`}
                      min="1"
                      max="5"
                      step={step}
                      inputMode="decimal"
                      placeholder="未评分"
                      value={scores[t.id]?.[d.key] ?? ''}
                      aria-invalid={
                        scores[t.id]?.[d.key] !== undefined && !validScore(scores[t.id]?.[d.key])
                      }
                      onChange={(e) =>
                        saveScore(
                          t.id,
                          d.key,
                          e.target.value === '' ? undefined : e.target.valueAsNumber,
                        )
                      }
                    />
                  </label>
                </div>
                {scores[t.id]?.[d.key] !== undefined && !validScore(scores[t.id]?.[d.key]) && (
                  <small className="error">请输入 1–5 分，步进 {step} 分；不会自动取整。</small>
                )}
                <div className="mos-anchors">
                  {d.anchors.map((anchor, i) => (
                    <span key={i}>
                      <b>{i + 1}</b> {anchor}
                    </span>
                  ))}
                </div>
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
            ? '当前任务不接受评分'
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
