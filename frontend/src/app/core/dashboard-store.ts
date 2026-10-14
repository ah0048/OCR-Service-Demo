import { Injectable, computed, signal } from '@angular/core';
import { API_URL } from './api';
import { BatchSummary, Snapshot } from './models';

export interface HistoryPoint { t: number; throughput: number; jobsReady: number; jobsUnacked: number; resultsReady: number; inFlight: number; }
export interface Alert { level: 'bad' | 'warn'; text: string; }
export interface Toast { id: number; kind: 'ok' | 'bad' | 'info'; text: string; }

const HISTORY_LIMIT = 150; // ~2.5 minutes at one snapshot per second

/** Holds the live snapshot streamed by the API (server-sent events) plus small client-side histories. */
@Injectable({ providedIn: 'root' })
export class DashboardStore {
  readonly snapshot = signal<Snapshot | null>(null);
  readonly connected = signal(false);
  /** Increments with every snapshot; pages watch it to refresh REST data (jobs table, job drawer). */
  readonly tick = signal(0);
  readonly history = signal<HistoryPoint[]>([]);
  /** completed-jobs-per-second and ETA for each batch, derived from consecutive snapshots */
  readonly batchRates = signal<Record<string, { rate: number; etaSec: number | null }>>({});

  readonly toasts = signal<Toast[]>([]);
  readonly selectedJobId = signal<string | null>(null);
  readonly submitOpen = signal(false);

  private source?: EventSource;
  private samples = new Map<string, { t: number; done: number }[]>();
  private toastId = 0;

  readonly queues = computed(() => this.snapshot()?.queues ?? []);
  readonly jobsQueue = computed(() => this.queues().find(q => q.name === 'ocr-jobs'));
  readonly resultsQueue = computed(() => this.queues().find(q => q.name === 'ocr-results'));
  readonly deadLetters = computed(() => this.queues().filter(q => q.role === 'dead-letter'));

  readonly alerts = computed<Alert[]>(() => {
    const s = this.snapshot();
    const list: Alert[] = [];
    if (!this.connected()) list.push({ level: 'bad', text: 'Live connection to the API is down. Retrying…' });
    if (!s) return list;
    if (!s.rabbitAvailable) list.push({ level: 'bad', text: 'RabbitMQ management API is unreachable: queue and worker data unavailable.' });
    else {
      const jq = this.jobsQueue(), rq = this.resultsQueue();
      if (jq && jq.ready + jq.unacked > 0 && s.ocrWorkers.onlineCount === 0)
        list.push({ level: 'bad', text: `${jq.ready + jq.unacked} jobs are waiting but no OCR worker is online.` });
      if (rq && rq.ready + rq.unacked > 0 && s.businessWorkers.onlineCount === 0)
        list.push({ level: 'bad', text: `${rq.ready + rq.unacked} results are waiting but no business worker is online.` });
      for (const d of this.deadLetters())
        if (d.total > 0) list.push({ level: 'warn', text: `${d.total} message(s) in dead-letter queue ${d.name}.` });
    }
    return list;
  });

  connect(): void {
    if (this.source) return;
    const es = new EventSource(`${API_URL}/api/dashboard/stream`);
    this.source = es;
    es.onopen = () => this.connected.set(true);
    es.onerror = () => this.connected.set(false); // EventSource reconnects by itself
    es.onmessage = e => this.ingest(JSON.parse(e.data) as Snapshot);
  }

  openJob(id: string | null): void { this.selectedJobId.set(id); }

  toast(kind: Toast['kind'], text: string): void {
    const id = ++this.toastId;
    this.toasts.update(t => [...t, { id, kind, text }]);
    setTimeout(() => this.toasts.update(t => t.filter(x => x.id !== id)), 5000);
  }

  private ingest(s: Snapshot): void {
    const now = Date.now();
    const jq = s.queues.find(q => q.name === 'ocr-jobs');
    const rq = s.queues.find(q => q.name === 'ocr-results');
    this.history.update(h => {
      const next = [...h, {
        t: now, throughput: s.throughput.perSecond10s,
        jobsReady: jq?.ready ?? 0, jobsUnacked: jq?.unacked ?? 0, resultsReady: rq?.ready ?? 0,
        inFlight: s.totals.queued + s.totals.inProgress,
      }];
      return next.length > HISTORY_LIMIT ? next.slice(next.length - HISTORY_LIMIT) : next;
    });
    this.updateBatchRates(s.batches, now);
    this.snapshot.set(s);
    this.connected.set(true);
    this.tick.update(v => v + 1);
  }

  private updateBatchRates(batches: BatchSummary[], now: number): void {
    const rates: Record<string, { rate: number; etaSec: number | null }> = {};
    for (const b of batches) {
      const list = this.samples.get(b.id) ?? [];
      list.push({ t: now, done: b.completed });
      while (list.length > 12) list.shift();
      this.samples.set(b.id, list);
      const remaining = b.total - b.completed;
      let rate = 0;
      if (list.length > 1) {
        const first = list[0], last = list[list.length - 1];
        rate = Math.max(0, (last.done - first.done) / ((last.t - first.t) / 1000));
      }
      rates[b.id] = { rate, etaSec: remaining === 0 ? 0 : rate > 0 ? remaining / rate : null };
    }
    this.batchRates.set(rates);
  }
}
