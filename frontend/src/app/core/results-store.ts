import { Injectable, signal } from '@angular/core';
import { API_URL } from './api';
import { LiveResult, ResultItem } from './models';

const MAX_RESULTS = 300;

/**
 * Receives every finished OCR result over server-sent events, newest first.
 * The connection lives for the whole session, so the Results page is already populated when you open it.
 */
@Injectable({ providedIn: 'root' })
export class ResultsStore {
  readonly results = signal<LiveResult[]>([]);
  readonly connected = signal(false);
  /** total results received since the page was opened (keeps counting after the list is capped) */
  readonly received = signal(0);

  private source?: EventSource;
  private ids = new Set<string>();

  connect(): void {
    if (this.source) return;
    const es = new EventSource(`${API_URL}/api/results/stream?backlog=30`);
    this.source = es;
    es.onopen = () => this.connected.set(true);
    es.onerror = () => this.connected.set(false); // EventSource reconnects by itself and the backlog is de-duplicated below
    es.addEventListener('backlog', e => this.add(JSON.parse((e as MessageEvent).data) as ResultItem, false));
    es.onmessage = e => this.add(JSON.parse(e.data) as ResultItem, true);
  }

  private add(result: ResultItem, live: boolean): void {
    const item: LiveResult = { ...result, live, arrivedAt: Date.now() };
    if (this.ids.has(item.jobId)) return;
    this.ids.add(item.jobId);
    this.received.update(n => n + 1);
    this.results.update(list => {
      const next = [item, ...list];
      if (next.length > MAX_RESULTS) for (const dropped of next.splice(MAX_RESULTS)) this.ids.delete(dropped.jobId);
      return next;
    });
  }
}
