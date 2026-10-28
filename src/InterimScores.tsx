import type { ReportPayload } from './review';

export function InterimScores({ report }: { report: ReportPayload }) {
  const rows = report.样本.flatMap((sample) =>
    sample.MOS
      ? sample.MOS.原始评分.flatMap((submission) =>
          Object.entries(submission.scores).flatMap(([track, values]) =>
            Object.entries(values).map(([dimension, score]) => ({
              key: `${sample.id}-${submission.user_id}-${track}-${dimension}`,
              author: submission.author,
              sample: sample.name,
              candidate: sample.MOS!.候选.find((t) => t.ID === track)?.名称 || track,
              dimension:
                report.任务.dimensions?.find((d) => d.key === dimension)?.name || dimension,
              score: String(score),
              reason: '',
            })),
          ),
        )
      : sample.ratings.map((rating) => ({
          key: `${sample.id}-${rating.user_id}`,
          author: rating.author,
          sample: sample.name,
          candidate:
            rating.choice === 'tie'
              ? '无明显差异'
              : sample.tracks?.find((t) => t.id === rating.choice)?.name || rating.choice,
          dimension: '偏好',
          score: '—',
          reason: rating.reason,
        })),
  );
  return (
    <details className="mos-scale-snapshot">
      <summary>已完成人员原始评分（{report.阶段结果?.纳入人数 || 0} 人）</summary>
      {rows.length ? (
        <table>
          <thead>
            <tr>
              <th>评测者</th>
              <th>片段</th>
              <th>候选</th>
              <th>指标</th>
              <th>分值</th>
              <th>原因</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key}>
                <td>{row.author}</td>
                <td>{row.sample}</td>
                <td>{row.candidate}</td>
                <td>{row.dimension}</td>
                <td>{row.score}</td>
                <td>{row.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p>暂无完成全部片段的评测者，当前结果为空。</p>
      )}
    </details>
  );
}
