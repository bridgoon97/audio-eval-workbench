export type User = { id: string; name: string; role: string };
export type Task = {
  id: string;
  title: string;
  kind: string;
  mode: string;
  status: string;
  owner: string;
  can_manage: boolean;
  sample_count: number;
  completed: number;
  samples?: Sample[];
  members?: string[];
  review_assignments?: string[];
};
export type Track = {
  id: string;
  label: string;
  name: string;
  version: string;
  meta?: {
    peak: number;
    rms_dbfs: number;
    samples: number;
    channel: number;
    clipped_samples: number;
    sha256: string;
  };
};
export type Comment = {
  id: string;
  track_id: string | null;
  start: number;
  end: number;
  body: string;
  tag: string;
  author: string;
  created: string;
  parent: string | null;
};
export type Sample = {
  id: string;
  name: string;
  scene: string;
  provenance: string;
  samples: number;
  completed: boolean;
  track_count: number;
  tracks?: Track[];
  comments?: Comment[];
  blind?: boolean;
  rating?: { choice: string; reason: string } | null;
};
export type Analysis = { peaks: number[][]; spectrogram: number[][] };
export type Invite = {
  id: string;
  purpose: string;
  kind: string;
  task_id: string | null;
  task_title: string | null;
  expires: string;
  max_uses: number;
  used_count: number;
  active: number;
  created: string;
};
export type Application = {
  id: string;
  display_name: string;
  employee_id: string;
  email: string;
  created: string;
  ip: string;
  device: string;
  status: 'pending' | 'approved' | 'rejected' | 'expired';
  decided: string | null;
  decided_by: string | null;
  user_id: string | null;
  claimed: boolean;
  invite: { purpose: string; kind: string; task_id: string | null; task_title: string | null };
};
export type DeviceInfo = {
  id: string;
  created: string;
  last_used: string | null;
  expires: string;
  revoked: boolean;
  first_ip: string;
  last_ip: string;
  device: string;
};

// CSRF 令牌由服务端随 /api/me 下发，敏感管理操作在请求头中回传。
let csrfToken = '';
export function setCsrfToken(token: string) {
  csrfToken = token;
}
export async function api<T = any>(
  path: string,
  method = 'GET',
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const headers: Record<string, string> = {};
  if (!(body instanceof FormData)) headers['Content-Type'] = 'application/json';
  if (method !== 'GET' && csrfToken) headers['X-CSRF-Token'] = csrfToken;
  const response = await fetch('/api' + path, {
    cache: 'no-store',
    signal,
    method,
    headers,
    body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw Object.assign(
      new Error(
        typeof data.detail === 'string'
          ? data.detail
          : `请求失败（${response.status}），请核对输入`,
      ),
      { status: response.status },
    );
  }
  return response.json();
}
