import { describe, expect, it } from 'vitest';
import { sharedContentGuide, timelinePosition } from './audio';
describe('共同播放时间轴', () => {
  it('调度尚未开始时不产生负时间', () => expect(timelinePosition(2, -0.035, 8)).toBe(2));
  it('连续切换不改变共同时间的计算', () => expect(timelinePosition(2, 3.25, 8)).toBe(5.25));
  it('从选区中间开始后按原始边界循环', () =>
    expect(timelinePosition(3, 5.5, 10, [2, 4])).toBe(2.5));
  it('播放到结尾停在真实时长', () => expect(timelinePosition(0, 12, 8)).toBe(8));
  it('非循环选区停在选区终点', () => expect(timelinePosition(2, 3, 10, undefined, 4)).toBe(4));
  it('共享内容提示只保留候选共同出现的粗粒度活动', () => {
    const a = new Float32Array([0, 0, 1, 1, 0, 0, 0, 0]);
    const b = new Float32Array([0, 0, 1, 1, 0, 0, 1, 1]);
    expect(sharedContentGuide([a, b], 4)).toEqual([0, 1, 0, 0]);
  });
});
