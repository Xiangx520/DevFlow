/** Public API client. Credentials stay in memory unless tab-only storage is requested. */
export class ApiError extends Error {
  constructor(status, detail) { super(detail); this.status = status; }
}
export async function request(path, { method = 'GET', body, key, signal } = {}, token = '') {
  const response = await fetch(`/api/v1${path}`, {
    method, signal, cache: 'no-store',
    headers: { Authorization: `Bearer ${token}`, ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...(key ? { 'Idempotency-Key': key } : {}) },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = typeof data?.detail === 'string' ? data.detail : Array.isArray(data?.detail) ? data.detail.map(x => `${x.loc?.slice(1).join('.')}：${x.msg}`).join('；') : `请求失败（HTTP ${response.status}）`;
    throw new ApiError(response.status, detail);
  }
  return data;
}

/** Decode SSE across arbitrary byte/line boundaries; server event kinds are unrestricted. */
export async function consumeEvents(response, onEvent, signal) {
  if (!response.ok) throw new ApiError(response.status, `事件连接失败（HTTP ${response.status}）`);
  if (!response.headers.get('content-type')?.includes('text/event-stream')) throw new Error('事件服务返回了错误的格式');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '', data = [], id = '';
  try {
    while (!signal?.aborted) {
      const part = await reader.read();
      if (part.done) break;
      buffer += decoder.decode(part.value, { stream: true });
      let newline;
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline).replace(/\r$/, '');
        buffer = buffer.slice(newline + 1);
        if (line === '') {
          if (data.length) { onEvent(JSON.parse(data.join('\n')), id); data = []; }
          id = '';
        } else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
        else if (line.startsWith('id:')) id = line.slice(3).trim();
      }
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}

export const labels = {
  queued: '排队中', running: '执行中', waiting_external: '等待外部', waiting_human: '需要决策',
  ready_for_review: '待审阅', paused: '已暂停', completed: '已完成', failed: '失败', cancelled: '已取消',
  pending: '待处理', sent: '已发送', done: '完成', active: '活动中', replaced: '已替换', interrupted: '已中断',
};
export function escapeHTML(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
export function taskActions(task) {
  if (task.status === 'completed') return [];
  if (task.status === 'cancelled') return ['resume'];
  if (task.status === 'failed') return ['retry', 'cancel'];
  if (task.status === 'paused') return ['resume', 'cancel'];
  return ['pause', 'cancel'];
}
export function mergeEvents(current, incoming, max = 400) {
  const map = new Map([...current, ...incoming].map(event => [event.sequence, event]));
  return [...map.values()].sort((a,b) => a.sequence - b.sequence).slice(-max);
}
export function safeURL(url) {
  try { const u = new URL(url); return ['https:', 'http:'].includes(u.protocol) ? escapeHTML(u.href) : ''; } catch { return ''; }
}
