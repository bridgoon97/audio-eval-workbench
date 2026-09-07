import { useRef, useState } from 'react';
import { BookOpen, Download, Search } from 'lucide-react';
import content from './guide-content.json';

type Role = keyof typeof content.roles;
export function UserGuide({ role }: { role: string }) {
  const initial: Role = role in content.roles ? (role as Role) : 'reviewer';
  const [selectedRole, setRole] = useState<Role>(initial);
  const [query, setQuery] = useState('');
  const [chapterId, setChapterId] = useState('');
  const article = useRef<HTMLElement>(null);
  const guide = content.roles[selectedRole];
  const chapters = [...guide.chapters, ...content.common];
  const matches = chapters.filter((c) =>
    [c.title, c.lead, ...c.steps, ...c.notes]
      .join('\n')
      .toLowerCase()
      .includes(query.trim().toLowerCase()),
  );
  const active = matches.find((c) => c.id === chapterId) || matches[0];
  function download() {
    const lines = ['# 听鉴 · 分角色使用手册', '', `适用版本：${content.version}`, ''];
    function append(chapters: typeof content.common) {
      for (const c of chapters) {
        lines.push(`### ${c.title}`, '', c.lead, '');
        lines.push(...c.steps.map((step, i) => `${i + 1}. ${step}`), '');
        lines.push(...c.notes.map((note) => `- ${note}`), '');
      }
    }
    for (const g of Object.values(content.roles)) {
      lines.push(`## ${g.name}`, '', g.intro, '');
      append(g.chapters);
    }
    lines.push('## 通用操作与常见问题', '');
    append(content.common);
    const url = URL.createObjectURL(
      new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' }),
    );
    const a = document.createElement('a');
    a.href = url;
    a.download = '听鉴分角色使用手册.md';
    a.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return (
    <div className="user-guide">
      <div className="guide-role-picker" role="group" aria-label="选择指南角色">
        {(Object.keys(content.roles) as Role[]).map((key) => (
          <button
            key={key}
            aria-pressed={key === selectedRole}
            className={key === selectedRole ? 'active' : ''}
            onClick={() => {
              setRole(key);
              setChapterId('');
              setQuery('');
              if (article.current) article.current.scrollTop = 0;
            }}
          >
            {content.roles[key].name}
            {key === initial && <small>当前角色</small>}
          </button>
        ))}
      </div>
      <p className="guide-intro">{guide.intro}</p>
      <div className="guide-tools">
        <label className="guide-search">
          <Search size={17} />
          <input
            aria-label="搜索当前角色指南"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setChapterId('');
              if (article.current) article.current.scrollTop = 0;
            }}
            placeholder="搜索当前角色指南，例如：共享、评分、恢复"
          />
        </label>
        <button onClick={download}>
          <Download size={16} />
          下载完整手册
        </button>
      </div>
      <div className="guide-layout">
        <nav aria-label="指南章节" className="guide-toc">
          <span className="eyebrow">
            {query ? `匹配 ${matches.length} 个章节` : `${matches.length} 个章节`}
          </span>
          {matches.map((c) => (
            <button
              key={c.id}
              aria-current={active?.id === c.id ? 'page' : undefined}
              onClick={() => {
                setChapterId(c.id);
                if (article.current) {
                  article.current.scrollTop = 0;
                }
              }}
            >
              {c.title}
            </button>
          ))}
        </nav>
        <article className="guide-article" ref={article} tabIndex={-1} aria-label="指南正文">
          {active ? (
            <>
              <div className="guide-kicker">
                <BookOpen size={18} />
                {guide.name}指南 · {chapters.indexOf(active) + 1} / {chapters.length}
              </div>
              <h3>{active.title}</h3>
              <p className="guide-lead">{active.lead}</p>
              <ol>
                {active.steps.map((step, i) => (
                  <li key={i}>{step}</li>
                ))}
              </ol>
              {active.notes.length > 0 && (
                <aside className="guide-notes" aria-label="操作边界">
                  {active.notes.map((note, i) => (
                    <p key={i}>{note}</p>
                  ))}
                </aside>
              )}
            </>
          ) : (
            <div className="guide-empty">
              <Search size={30} />
              <h3>没有匹配的章节</h3>
              <p>尝试更短的关键词，或切换角色查看其他操作。</p>
              <button onClick={() => setQuery('')}>清除搜索</button>
            </div>
          )}
        </article>
      </div>
      <p className="guide-footer">
        查看其他角色的指南不会改变账号权限。内容随版本 {content.version}{' '}
        提供，可离线使用；下载包含全部角色。
      </p>
    </div>
  );
}
