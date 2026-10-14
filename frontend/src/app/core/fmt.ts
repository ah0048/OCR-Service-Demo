/** Small formatting helpers shared by the pages. */

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : '—';
}

/** "ocr-DESKTOP-79PVAH8-29688" -> "pid 29688" (host is shown separately). */
export function workerPid(id: string | null | undefined): string {
  if (!id) return '—';
  const last = id.split('-').pop();
  return last && /^\d+$/.test(last) ? `#${last}` : id;
}

export function workerHost(id: string): string {
  const parts = id.split('-');
  return parts.length >= 3 ? parts.slice(1, -1).join('-') : '';
}

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || isNaN(ms)) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${Math.round(s % 60)}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

export function eta(sec: number | null | undefined): string {
  if (sec === null || sec === undefined) return '—';
  if (sec === 0) return 'done';
  return duration(sec * 1000);
}

export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return '—';
  const diff = Math.max(0, now - new Date(iso).getTime()) / 1000;
  if (diff < 2) return 'just now';
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}

export function pct(part: number, total: number, digits = 0): string {
  return total > 0 ? `${((part / total) * 100).toFixed(digits)}%` : '0%';
}

export function compact(n: number): string {
  return n >= 10000 ? `${(n / 1000).toFixed(1)}k` : n.toLocaleString();
}

/** Elapsed ms between two ISO timestamps (UTC strings from the API may lack a trailing Z). */
export function between(a: string | null | undefined, b: string | null | undefined): number | null {
  if (!a || !b) return null;
  return new Date(utc(b)).getTime() - new Date(utc(a)).getTime();
}

export function utc(iso: string): string {
  return /[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z';
}
