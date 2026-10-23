import { useState } from 'react';
import type { Dimension } from './types';

export const defaultDimensions: Dimension[] = [
  {
    key: 'noise_reduction',
    name: '降噪',
    question: '背景噪声抑制得是否充分？',
    anchors: ['非常不足', '不足', '一般', '充分', '非常充分'],
  },
  {
    key: 'timbre_similarity',
    name: '音色相似度',
    question: '与参考音色有多接近？',
    anchors: ['完全不像', '不太接近', '一般', '接近', '非常接近'],
  },
  {
    key: 'quality',
    name: '音质',
    question: '整体是否自然、清晰且无失真？',
    anchors: ['非常差', '较差', '一般', '良好', '优秀'],
  },
  {
    key: 'intelligibility',
    name: '可懂度',
    question: '语音内容是否容易听清？',
    anchors: ['完全听不清', '多数听不清', '部分听清', '多数听清', '完全听清'],
  },
];

export function MosTaskSettings({
  initialMethod = 'preference',
  initialDimensions,
  lockedMethod = false,
}: {
  initialMethod?: string;
  initialDimensions?: Dimension[];
  lockedMethod?: boolean;
}) {
  const [method, setMethod] = useState(initialMethod);
  const [dimensions, setDimensions] = useState<Dimension[]>(() =>
    structuredClone(initialDimensions?.length ? initialDimensions : defaultDimensions),
  );
  const update = (index: number, patch: Partial<Dimension>) =>
    setDimensions((prev) => prev.map((d, i) => (i === index ? { ...d, ...patch } : d)));
  const move = (index: number, step: number) =>
    setDimensions((prev) => {
      const next = [...prev];
      [next[index], next[index + step]] = [next[index + step], next[index]];
      return next;
    });
  return (
    <>
      <label>
        评分方式
        {lockedMethod && <input type="hidden" name="evaluation" value={method} />}
        <select
          name="evaluation"
          value={method}
          disabled={lockedMethod}
          onChange={(e) => setMethod(e.target.value)}
        >
          <option value="preference">偏好判断：选择更倾向的候选</option>
          <option value="mos">多维 MOS：自定义指标与每档说明</option>
        </select>
      </label>
      {method === 'mos' && (
        <div className="mos-config">
          <input type="hidden" name="dimensions" value={JSON.stringify(dimensions)} />
          <p>
            配置 1–8 个指标，每项固定 1–5
            分；为每档写一句简短描述。发布后量表冻结，结果保留此处配置。
          </p>
          {dimensions.map((d, i) => (
            <fieldset className="mos-config-card" key={d.key}>
              <legend>指标 {i + 1}</legend>
              <div className="mos-config-actions">
                <button
                  type="button"
                  disabled={i === 0}
                  aria-label={`上移指标 ${i + 1}`}
                  onClick={() => move(i, -1)}
                >
                  上移
                </button>
                <button
                  type="button"
                  disabled={i === dimensions.length - 1}
                  aria-label={`下移指标 ${i + 1}`}
                  onClick={() => move(i, 1)}
                >
                  下移
                </button>
                <button
                  type="button"
                  disabled={dimensions.length === 1}
                  aria-label={`删除指标 ${i + 1}`}
                  onClick={() => setDimensions((prev) => prev.filter((_, n) => n !== i))}
                >
                  删除
                </button>
              </div>
              <label>
                指标名称
                <input
                  aria-label={`指标名称 ${i + 1}`}
                  value={d.name}
                  required
                  maxLength={40}
                  onChange={(e) => update(i, { name: e.target.value })}
                />
              </label>
              <label>
                评测说明（选填）
                <input
                  aria-label={`指标说明 ${i + 1}`}
                  value={d.question}
                  maxLength={200}
                  placeholder="例如：与参考音色有多接近？"
                  onChange={(e) => update(i, { question: e.target.value })}
                />
              </label>
              <div className="mos-anchor-editor">
                {d.anchors.map((anchor, n) => (
                  <label key={n}>
                    {n + 1} 分
                    <input
                      aria-label={`指标 ${i + 1} 的 ${n + 1} 分说明`}
                      value={anchor}
                      required
                      maxLength={80}
                      onChange={(e) =>
                        update(i, {
                          anchors: d.anchors.map((a, j) => (j === n ? e.target.value : a)),
                        })
                      }
                    />
                  </label>
                ))}
              </div>
            </fieldset>
          ))}
          <button
            type="button"
            disabled={dimensions.length >= 8}
            onClick={() => {
              let n = 1;
              while (dimensions.some((d) => d.key === `dimension_${n}`)) n++;
              setDimensions((prev) => [
                ...prev,
                {
                  key: `dimension_${n}`,
                  name: '新指标',
                  question: '',
                  anchors: ['非常差', '较差', '一般', '良好', '优秀'],
                },
              ]);
            }}
          >
            增加评测指标
          </button>
        </div>
      )}
    </>
  );
}
