/** 按目录下的完整相对路径匹配，保留大小写，避免同名子目录误配。 */
export type InputFile = { name: string; webkitRelativePath: string; size: number };
export function matchFiles<T extends InputFile>(groups: T[][]) {
  const maps = groups.map((files) => {
    const map = new Map<string, T[]>();
    for (const file of files) {
      if (!/\.wav$/i.test(file.name)) continue;
      const path = file.webkitRelativePath;
      const key = path ? path.split('/').slice(1).join('/') : file.name;
      map.set(key, [...(map.get(key) || []), file]);
    }
    return map;
  });
  return [...new Set(maps.flatMap((map) => [...map.keys()]))].sort().map((key) => {
    const files = maps.map((map) => map.get(key) || []);
    const issues = files.flatMap((list, i) =>
      list.length === 0
        ? [`版本 ${i + 1} 缺失`]
        : list.length > 1
          ? [`版本 ${i + 1} 重名`]
          : list[0].size > 32 * 1024 * 1024
            ? [`版本 ${i + 1} 超过 32 MB`]
            : [],
    );
    if (key.length > 100) issues.push('相对路径超过 100 字符');
    return { key, files: files.map((list) => list[0]), issues };
  });
}
