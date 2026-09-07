import type { Comment, Track } from './types';

function CommentCard({
  comment,
  replies,
  comments,
  tracks,
  onJump,
  onReply,
}: {
  comment: Comment;
  replies: Comment[];
  comments: Comment[];
  tracks: Track[];
  onJump: (comment: Comment) => void;
  onReply: (comment: Comment) => void;
}) {
  return (
    <article className="comment-thread">
      <div className="comment-main">
        <div className="comment-author">
          <span className="comment-avatar">{comment.author.slice(0, 1)}</span>
          <div>
            <strong>{comment.author}</strong>
            <small>{new Date(comment.created).toLocaleString()}</small>
          </div>
          <span className="comment-tag">{comment.tag}</span>
        </div>
        <button className="timestamp" onClick={() => onJump(comment)}>
          {(comment.start / 16000).toFixed(2)} — {(comment.end / 16000).toFixed(2)} ·{' '}
          {tracks.find((track) => track.id === comment.track_id)?.label || '片段'}
        </button>
        <p>{comment.body}</p>
        <button className="text-button reply-button" onClick={() => onReply(comment)}>
          回复
        </button>
      </div>
      {replies.length > 0 && (
        <div className="comment-replies">
          {replies.map((reply) => (
            <div className="comment-reply" key={reply.id}>
              <p>
                <strong>{reply.author}</strong>
                <span>
                  {' '}
                  回复 @
                  {comments.find((item) => item.id === reply.parent)?.author || comment.author}
                </span>
                ：{reply.body}
              </p>
              <div>
                <button className="timestamp" onClick={() => onJump(reply)}>
                  {(reply.start / 16000).toFixed(2)} — {(reply.end / 16000).toFixed(2)}
                </button>
                <button className="text-button" onClick={() => onReply(reply)}>
                  回复
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </article>
  );
}

export function CommentThread({
  comments,
  tracks,
  onJump,
  onReply,
}: {
  comments: Comment[];
  tracks: Track[];
  onJump: (comment: Comment) => void;
  onReply: (comment: Comment) => void;
}) {
  const byId = new Map(comments.map((comment) => [comment.id, comment]));
  const rootOf = (comment: Comment) => {
    let current = comment;
    const seen = new Set<string>();
    while (current.parent && byId.has(current.parent) && !seen.has(current.id)) {
      seen.add(current.id);
      current = byId.get(current.parent)!;
    }
    return current.id;
  };
  const roots = comments.filter((comment) => !comment.parent || !byId.has(comment.parent));
  return (
    <>
      {roots.map((root) => (
        <CommentCard
          key={root.id}
          comment={root}
          replies={comments.filter(
            (comment) => comment.id !== root.id && rootOf(comment) === root.id,
          )}
          comments={comments}
          tracks={tracks}
          onJump={onJump}
          onReply={onReply}
        />
      ))}
    </>
  );
}
