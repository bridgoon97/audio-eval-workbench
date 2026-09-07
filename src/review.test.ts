import { describe, expect, it } from 'vitest';
import { distinctChoices, formatShare, shareWidth } from './review';

describe('复盘展示口径', () => {
  it('百分比明确写出分母（该片段有效提交人数）', () => {
    expect(formatShare(2, 3)).toBe('66.7%（2/3）');
    expect(formatShare(0, 3)).toBe('0.0%（0/3）');
    expect(formatShare(1, 6)).toBe('16.7%（1/6）');
  });
  it('没有有效提交时不呈现百分比，不伪造 0% 样本', () => {
    expect(formatShare(0, 0)).toBe('暂无提交');
    expect(shareWidth(0, 0)).toBe('0%');
  });
  it('不同偏好种类只统计有票选项，用于描述性分歧提示', () => {
    expect(
      distinctChoices([
        { ID: 'a', 名称: 'A', 票数: 2 },
        { ID: 'b', 名称: 'B', 票数: 0 },
        { ID: 'tie', 名称: '无明显差异', 票数: 0 },
      ]),
    ).toBe(1);
    expect(
      distinctChoices([
        { ID: 'a', 名称: 'A', 票数: 1 },
        { ID: 'b', 名称: 'B', 票数: 0 },
        { ID: 'tie', 名称: '无明显差异', 票数: 1 },
      ]),
    ).toBe(2);
    expect(distinctChoices([])).toBe(0);
  });
});
