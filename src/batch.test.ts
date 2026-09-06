import { describe, expect, it } from 'vitest';
import { matchFiles } from './batch';
const file = (path: string, size = 20) => ({
  name: path.split('/').at(-1)!,
  webkitRelativePath: path,
  size,
});
describe('版本目录匹配', () => {
  it('忽略版本根目录，但保留子目录与大小写防止错误配对', () => {
    const rows = matchFiles([
      [file('v1/室内/a.wav'), file('v1/车内/a.wav')],
      [file('v2/室内/a.wav'), file('v2/车内/A.wav')],
    ]);
    expect(rows.filter((r) => !r.issues.length).map((r) => r.key)).toEqual(['室内/a.wav']);
    expect(rows).toHaveLength(3);
  });
  it('报告缺失、重复和超限文件，忽略非 WAV', () => {
    const rows = matchFiles([
      [file('v1/a.wav'), file('v1/a.wav'), file('v1/说明.txt')],
      [file('v2/b.wav', 32 * 1024 * 1024 + 1)],
    ]);
    expect(rows).toHaveLength(2);
    expect(rows.find((r) => r.key === 'a.wav')?.issues).toEqual(['版本 1 重名', '版本 2 缺失']);
    expect(rows.find((r) => r.key === 'b.wav')?.issues).toEqual([
      '版本 1 缺失',
      '版本 2 超过 32 MB',
    ]);
  });
});
